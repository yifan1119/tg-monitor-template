"""Web 登录管理介面 — 账号登录 + 启用/停用 + 首次设置 + 设置修改"""
import asyncio
import json
import logging
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from flask import Flask, render_template, request, jsonify, redirect, url_for
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# v2.10.21: 防御性 import — 不同 Telethon 版本下某些异常类可能缺失,
# 直接 `from telethon.errors import X, Y, Z` 一个缺失整个 web.py 就 ImportError 起不来。
# 逐个 getattr,缺的用占位 Exception(isinstance 检查永远 False),_humanize_tg_error
# 里的分支就走不到,走兜底 str(e) 也够用。
class _MissingTgError(Exception):
    """Telethon 此版本缺的异常占位, isinstance 检查永远为 False"""
import telethon.errors as _tg_errors
def _tg_err(name):
    return getattr(_tg_errors, name, _MissingTgError)
PasswordHashInvalidError  = _tg_err("PasswordHashInvalidError")
PhoneCodeInvalidError     = _tg_err("PhoneCodeInvalidError")
PhoneCodeExpiredError     = _tg_err("PhoneCodeExpiredError")
PhoneCodeEmptyError       = _tg_err("PhoneCodeEmptyError")
PhoneNumberInvalidError   = _tg_err("PhoneNumberInvalidError")
PhoneNumberBannedError    = _tg_err("PhoneNumberBannedError")
PhoneNumberFloodError     = _tg_err("PhoneNumberFloodError")
FloodWaitError            = _tg_err("FloodWaitError")

import os
from functools import wraps
from flask import session as flask_session

import config
import database as db
import gspread
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

# ============ 设置文件读写 ============
# 纯 OAuth:Drive + Sheets 共用 oauth_helper 维护的 token,不再有 service-account.json
ENV_PATH = config.BASE_DIR / ".env"
USERS_PATH = config.DATA_DIR / "users.json"


# ============ 多用户账号系统 + 三层角色 ============
# users.json 格式: {username: {"password_hash": str, "is_admin": bool, "is_super": bool}}
# 角色体系:
#   主帐号 (is_super=True)   — 首次 setup 创建,唯一可新增/移除账号,不可被任何人删除
#   管理员 (is_admin=True)   — 可改系统配置,不能碰账号管理
#   普通成员 (两者皆 False) — 仅能改自己密码
# 兼容旧格式 {username: "password_hash_str"} — 旧数据视为主帐号(is_super + is_admin)
def load_users():
    if not USERS_PATH.exists():
        return {}
    try:
        raw = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    normalized = {}
    for u, v in raw.items():
        if isinstance(v, str):
            normalized[u] = {"password_hash": v, "is_admin": True, "is_super": True}
        elif isinstance(v, dict):
            is_super = bool(v.get("is_super", False))
            normalized[u] = {
                "password_hash": v.get("password_hash", ""),
                "is_admin": bool(v.get("is_admin", False)) or is_super,  # super 必定也是 admin
                "is_super": is_super,
                "tg_user_id": v.get("tg_user_id"),
                "tg_username": v.get("tg_username"),
                "tg_bound_at": v.get("tg_bound_at"),
            }
    return normalized


def save_users(users):
    USERS_PATH.parent.mkdir(exist_ok=True)
    USERS_PATH.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")


def verify_user(username, password):
    users = load_users()
    u = users.get(username)
    if not u:
        return False
    return check_password_hash(u["password_hash"], password)


def is_admin(username):
    """管理员或主帐号都视为 admin"""
    users = load_users()
    u = users.get(username, {})
    return bool(u.get("is_admin", False) or u.get("is_super", False))


def is_super(username):
    """是否是主帐号"""
    users = load_users()
    return bool(users.get(username, {}).get("is_super", False))


def migrate_legacy_password():
    """旧部署只有 WEB_PASSWORD 没 users.json — 自动迁移:主帐号 = WEB_PASSWORD"""
    if load_users():
        ensure_super_exists()
        return
    env = read_env()
    pwd = env.get("WEB_PASSWORD", "") or os.environ.get("WEB_PASSWORD", "")
    if pwd:
        save_users({"admin": {
            "password_hash": generate_password_hash(pwd),
            "is_admin": True,
            "is_super": True,
        }})
        print("🔄 旧 WEB_PASSWORD 已迁移到 users.json (账号: admin, 角色: 主帐号)")


def ensure_super_exists():
    """确保至少有一个主帐号 — 若 users.json 里没人是 is_super
    (RBAC 升级前建的 admin 账号),把第一个 is_admin 升级为主帐号"""
    users = load_users()
    if not users:
        return
    if any(u.get("is_super") for u in users.values()):
        return
    # 没主帐号 → 找第一个 admin 或第一个账号升级
    target = None
    for name, data in users.items():
        if data.get("is_admin"):
            target = name
            break
    if not target:
        target = next(iter(users))
    users[target]["is_super"] = True
    users[target]["is_admin"] = True
    save_users(users)
    print(f"🔄 账号「{target}」升级为主帐号(无主帐号自动修复)")


# ============ 防暴力破解 ============
# 规则：10 分钟内失败 5 次 → IP 锁定 15 分钟
import time as _time
_login_fails = {}   # {ip: [ts1, ts2, ...]}
_lockouts = {}      # {ip: lockout_until_ts}
BRUTE_WINDOW = 600        # 10 分钟观察窗口
BRUTE_THRESHOLD = 5       # 5 次失败触发锁定
BRUTE_LOCKOUT = 900       # 锁定 15 分钟


def _lockout_remaining(ip):
    """返回该 IP 还要锁多久（秒）；0 表示没锁定"""
    now = _time.time()
    until = _lockouts.get(ip, 0)
    if until > now:
        return int(until - now)
    if until:
        _lockouts.pop(ip, None)
    return 0


def _record_login_attempt(ip, success):
    now = _time.time()
    if success:
        _login_fails.pop(ip, None)
        _lockouts.pop(ip, None)
        return
    fails = [t for t in _login_fails.get(ip, []) if now - t < BRUTE_WINDOW]
    fails.append(now)
    _login_fails[ip] = fails
    if len(fails) >= BRUTE_THRESHOLD:
        _lockouts[ip] = now + BRUTE_LOCKOUT
        _login_fails.pop(ip, None)
        print(f"🔒 IP {ip} 连续 {BRUTE_THRESHOLD} 次登录失败,锁定 {BRUTE_LOCKOUT//60} 分钟")


def _client_ip():
    """获取客户端 IP,优先 X-Forwarded-For(Tunnel / Nginx 场景)"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"

# 共用预设（所有部门通用）
# API_ID / API_HASH 必须客户自己申请 — 不在 repo 里预填,避免 public 仓库暴露私钥
DEFAULT_API_ID = ""
DEFAULT_API_HASH = ""
DEFAULT_KEYWORDS = "到期,续费,暂停,下架,上架,地址,打款,欠费,返点,返利,回扣"
DEFAULT_WEB_PASSWORD = "tg@monitor2026"
DEFAULT_NO_REPLY_MINUTES = "30"
DEFAULT_PATROL_DAYS = "7"
DEFAULT_HISTORY_DAYS = "2"
DEFAULT_SHEETS_FLUSH_INTERVAL = "5"
DEFAULT_PATROL_INTERVAL = "60"


def read_env():
    """读 .env 到 dict，保留原顺序"""
    env = {}
    if not ENV_PATH.exists():
        return env
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def write_env(updates):
    """合并 updates 到 .env（保留已有的其他字段）"""
    existing = read_env()
    existing.update({k: str(v) for k, v in updates.items()})
    # 固定顺序，读起来舒服
    order = [
        "API_ID", "API_HASH",
        "COMPANY_NAME", "COMPANY_DISPLAY", "PEER_ROLE_LABEL", "OPERATOR_LABEL",
        "SHEET_ID", "MEDIA_FOLDER_ID", "MEDIA_RETENTION_DAYS", "MEDIA_MAX_MB",
        "MEDIA_STORAGE_MODE", "MEDIA_ARCHIVE_GROUP_ID",
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
        "BOT_TOKEN", "ALERT_GROUP_ID",
        "WEB_PORT", "WEB_PASSWORD",
        "METRICS_TOKEN",
        "KEYWORDS", "NO_REPLY_MINUTES",
        "WORK_HOUR_START", "WORK_HOUR_END",
        "PATROL_DAYS", "HISTORY_DAYS",
        "SHEETS_FLUSH_INTERVAL", "PATROL_INTERVAL",
        "SETUP_COMPLETE",
    ]
    lines = []
    seen = set()
    for k in order:
        if k in existing:
            lines.append(f"{k}={existing[k]}")
            seen.add(k)
    for k, v in existing.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def is_setup_complete():
    """是否已完成首次设置。条件:关键字段都有值 + SETUP_COMPLETE=true + 至少 1 个用户。

    注意 OAuth token 不放在这里检查 —— 精灵保存后用户还没登录,
    根本没机会做 OAuth。把 has_token() 当硬条件会死锁:
    没 token → 被踢回 /setup → /setup 本身又不需登录 → 循环填表。
    OAuth 作为登入后在 /settings 页完成的「第二步」,监控服务启动时再检查凭证。
    """
    env = read_env()
    required = ["COMPANY_NAME", "BOT_TOKEN", "ALERT_GROUP_ID"]
    for k in required:
        if not env.get(k) or env.get(k) in ("__pending__", "0"):
            return False
    if env.get("SETUP_COMPLETE", "false").lower() != "true":
        return False
    # 旧部署自动迁移
    migrate_legacy_password()
    if not load_users():
        return False
    return True


def _test_bot_api(token, group_id):
    """验证 Bot token 有效 + bot 在群里(且是管理员)。
    用 getMe + getChatMember 只读调用,不骚扰客户群(每次保存都发测试消息会刷屏)。"""
    token = (token or "").strip()
    group_id = (group_id or "").strip()
    if not token or ":" not in token:
        return False, "Bot Token 格式错误"
    if not group_id:
        return False, "群 ID 不能为空"
    try:
        # 1. getMe — 验证 token 有效
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getMe", timeout=10
        ) as resp:
            me = json.loads(resp.read())
        if not me.get("ok"):
            return False, f"Token 无效: {me.get('description', '未知错误')}"
        bot_info = me["result"]
        bot_id = bot_info["id"]
        bot_username = bot_info.get("username", "?")

        # 2. getChat — 验证 bot 能看到这个群
        data = urllib.parse.urlencode({"chat_id": group_id}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getChat", data=data
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                chat = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            return False, f"群 ID「{group_id}」bot 看不到。确认: ① bot 已加进群 ② 群 ID 正确(含负号)。细节: {body}"
        if not chat.get("ok"):
            return False, f"群验证失败: {chat.get('description', '未知错误')}"
        chat_title = chat["result"].get("title", "?")

        # 3. getChatMember — 验证 bot 是管理员(能看到所有消息)
        data = urllib.parse.urlencode({"chat_id": group_id, "user_id": bot_id}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getChatMember", data=data
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            member = json.loads(resp.read())
        status = member.get("result", {}).get("status", "")
        if status not in ("administrator", "creator"):
            return False, f"@{bot_username} 在群「{chat_title}」,但不是管理员(当前: {status})。请把 bot 设为管理员才能收到所有消息。"

        return True, f"✅ @{bot_username} 已在群「{chat_title}」并具管理员权限"
    except Exception as e:
        return False, str(e)


def _test_sheets_access(sheet_id):
    """验证 Google Sheets 访问权限 — 使用当前 OAuth 授权凭证。

    调用前提:调用方已确认 OAuth 已授权(oauth_helper.has_token() 为 True)。
    若 sheet_id 为空或 OAuth 未授权,请在调用方里处理,本函数不做这类分支。
    """
    sheet_id = (sheet_id or "").strip()
    if not sheet_id:
        return False, "Sheet ID 不能为空"
    try:
        import oauth_helper
        creds = oauth_helper.get_credentials()
        if not creds:
            return False, "尚未完成 Google 授权,请先在上方点「连接 Google Drive」"
    except Exception as e:
        return False, f"获取 OAuth 凭证失败: {e}"
    try:
        gc = gspread.authorize(creds)
        sp = gc.open_by_key(sheet_id)
        # 能 open 只代表有「读」权限 — 必须验「写」,否则实际写入会 PermissionError
        # 方法: 读第 1 页 A1 再写回去(幂等,对客户数据无副作用)
        try:
            ws = sp.sheet1
            orig = ws.acell("A1").value
            ws.update_acell("A1", orig if orig is not None else "")
        except gspread.exceptions.APIError as e:
            body = getattr(e, "args", [""])[0] or str(e)
            if "PERMISSION_DENIED" in str(body) or "403" in str(body):
                return False, ("Sheet 可读但<b>无写权限</b>。请确认此 Sheet 是当前 OAuth 授权帐号"
                               "自己建的,或已被分享为<b>编辑者</b>")
            return False, f"写入测试失败: {body}"
        return True, f"✅ 成功访问 Sheet: {sp.title}(已验证读+写权限)"
    except gspread.exceptions.SpreadsheetNotFound:
        return False, (f"Sheet ID「{sheet_id}」找不到或没访问权限。"
                       f"请确认:① ID 正确;② 当前 OAuth 授权帐号对该 Sheet 有编辑者权限"
                       f"(或留空让系统自动建一个)")
    except gspread.exceptions.APIError as e:
        body = getattr(e, "args", [""])[0] or str(e) or repr(e)
        return False, f"Sheet API 错误: {body}"
    except Exception as e:
        err = str(e) or repr(e) or type(e).__name__
        return False, f"{type(e).__name__}: {err}"


_restart_debounce_lock = __import__("threading").Lock()
_auto_create_sheet_lock = __import__("threading").Lock()  # v2.10.12


def _synchronized(lock):
    """v2.10.12: 简单 sync decorator — 用于防双击建 sheet 之类的并发保护"""
    def deco(f):
        from functools import wraps
        @wraps(f)
        def wrapper(*args, **kwargs):
            with lock:
                return f(*args, **kwargs)
        return wrapper
    return deco
_restart_debounce_timer = {"t": None}

def _mark_session_healthy(phone):
    """v2.10.7: 登录成功 → 立刻 session_states.json 该手机号改 healthy
    返回 (prev_status, states) — prev_status 用来判断要不要推恢复通知"""
    if not phone:
        return None, None
    import json
    from pathlib import Path as _P
    sp = _P("/app/data/.session_states.json")
    if not sp.exists():
        alt = _P(__file__).parent / "data" / ".session_states.json"
        if alt.exists():
            sp = alt
    states = {}
    prev_status = None
    try:
        if sp.exists():
            states = json.loads(sp.read_text())
        prev_status = (states.get(phone) or {}).get("status")
        states[phone] = {"status": "healthy", "last_check": db.now_bj()}
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(states, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[_mark_session_healthy] {e}")
    return prev_status, states


def _push_session_restored(phone, account_name):
    """v2.10.7: 登录成功且前一状态是 revoked → 推恢复通知到预警群
    直接调 Telegram Bot HTTP API (web 容器没 aiogram 实例)"""
    import urllib.request, urllib.parse
    token = (os.environ.get("BOT_TOKEN", "") or getattr(config, "BOT_TOKEN", "")).strip()
    chat_id = (os.environ.get("ALERT_GROUP_ID", "") or str(getattr(config, "ALERT_GROUP_ID", ""))).strip()
    if not token or not chat_id:
        return
    try:
        text = (
            f"【外事号恢复通知{config.COMPANY_DISPLAY}】\n\n"
            f"外事号:{account_name or '—'} ({phone})\n"
            f"状态:✅ 监听已恢复正常\n"
            f"(客户通过 web 后台重新验证码登录;tg-monitor 正在自动重启读取新 session)"
        )
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[_push_session_restored] {e}")


def _schedule_listener_restart(delay=4.0):
    """v2.6.3: 帐号增删后自动重启 tg-monitor 容器,debounce 合并 N 秒内的连续操作。
    用例:
      - verify_code 成功 → 调用一次 → 4 秒后才真正 restart
      - 用户在 4 秒内继续 verify_code(批量加号) → 计时器被重置 → 仍只重启一次
      - remove_session 同理
    避免每加一个号就重启一次容器。
    """
    import threading
    def _do_restart():
        ok, msg = _start_tg_monitor()
        print(f"[auto-restart] {'ok' if ok else 'FAILED'}: {msg}")
    with _restart_debounce_lock:
        if _restart_debounce_timer["t"] is not None:
            _restart_debounce_timer["t"].cancel()
        t = threading.Timer(delay, _do_restart)
        t.daemon = True
        _restart_debounce_timer["t"] = t
        t.start()


def _start_tg_monitor():
    """通过 docker API 重启 tg-monitor 容器（install.sh 已建好，只需 restart 就会读新 .env）"""
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        company = read_env().get("COMPANY_NAME", "")
        if not company:
            return False, "COMPANY_NAME 未设置"
        container_name = f"tg-monitor-{company}"
        try:
            c = client.containers.get(container_name)
            c.restart(timeout=10)
            return True, f"{container_name} 已重启，开始读取新配置"
        except docker_sdk.errors.NotFound:
            # 找旧名字（首次设置时 container_name 可能是 default）
            for c in client.containers.list(all=True):
                if c.name.startswith("tg-monitor-"):
                    c.restart(timeout=10)
                    return True, f"{c.name} 已重启（建议之后手动 docker compose up -d --build 重命名为 {container_name}）"
            return False, f"找不到 tg-monitor 容器，请手动 docker compose up -d --build"
    except Exception as e:
        return False, str(e)


def _get_spreadsheet():
    """获取 Google Sheets 连接 — 使用 OAuth 用户授权凭证"""
    import oauth_helper
    creds = oauth_helper.get_credentials()
    if not creds:
        raise RuntimeError("OAuth 未授权,请到设置页点「连接 Google Drive」")
    gc = gspread.authorize(creds)
    return gc.open_by_key(config.SHEET_ID)


def _create_sheet_tab(name, operator="", company=""):
    """登录成功后自动建分页。

    v2.10.16: 改用 SheetsWriter.create_account_tab_full 统一模板 —
    之前 web.py 和 sheets.py ensure_account_tabs 各自有一套建分页逻辑
    (一套完整、一套阉割 3 行),sweep 补建的分页看起来跟登录建的不一样。
    现在两路都走同一个方法,格式保证一致。
    """
    try:
        from sheets import SheetsWriter
        writer = SheetsWriter()  # __init__ 顺便 sweep 一轮(幂等)
        writer.create_account_tab_full(name=name, operator=operator, company=company)
        print(f"✅ 自动建分页成功: {name}")
    except Exception as e:
        # v2.10.10: 打完整 stack, 方便 docker logs tg-web 追
        import traceback
        print(f"❌ 自动建分页失败 {name!r}: {e}")
        traceback.print_exc()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not flask_session.get("authed"):
            return redirect(url_for("login_page"))
        # 旧 session 没 username(RBAC 上线前登入的) → 强制重新登入
        if not flask_session.get("username"):
            flask_session.clear()
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """仅管理员/主帐号可访问;非管理员访问 API 回 403,访问页面导回 /settings"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not flask_session.get("authed"):
            return redirect(url_for("login_page"))
        if not is_admin(flask_session.get("username", "")):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "msg": "需要管理员权限"}), 403
            return redirect(url_for("settings_page"))
        return f(*args, **kwargs)
    return decorated


def super_required(f):
    """仅主帐号可访问 — 新增/移除账号专用"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not flask_session.get("authed"):
            return redirect(url_for("login_page"))
        if not is_super(flask_session.get("username", "")):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "msg": "仅主帐号可执行此操作"}), 403
            return redirect(url_for("settings_page"))
        return f(*args, **kwargs)
    return decorated


app = Flask(__name__, template_folder="templates")
# v2.10.2: Flask secret 随机化 — 每个部署一个,持久化到 data/.flask_secret
#   优先 env var (install.sh/update.sh 写的),否则首次启动随机生成持久化
def _load_or_gen_secret():
    env_key = os.environ.get("WEB_SECRET_KEY", "").strip()
    if env_key and env_key != "tg-monitor-web-2026":
        return env_key
    import secrets as _secrets
    from pathlib import Path as _Path
    sf = _Path("/app/data/.flask_secret")
    try:
        if sf.exists():
            val = sf.read_text().strip()
            if val:
                return val
        key = _secrets.token_hex(32)
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(key)
        try: sf.chmod(0o600)
        except Exception: pass
        return key
    except Exception:
        # fallback 到一次性 runtime key(容器重启会让现有 session 失效,但不会用固定值)
        return _secrets.token_hex(32)
app.secret_key = _load_or_gen_secret()

# 反代场景(Caddy 加 HTTPS 给 OAuth 用):识别 X-Forwarded-* 头,让 request.host_url 拿到 https://
# 没反代时这个不会有副作用,因为浏览器不会自己加这些头
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# v2.10.3: 全局注入 app_version,登入页/设置页不再硬编码版本号
_APP_VERSION_CACHE = {"value": None, "mtime": 0}

def _app_version_string():
    """SSOT: README.md 最新版 banner。fallback: commit subject 找 vX.Y.Z → short sha。
    带 mtime 缓存,省文件 IO;模板里直接 {{ app_version }} 用。"""
    import re
    from pathlib import Path as _P
    try:
        readme = _P(__file__).parent / "README.md"
        if readme.exists():
            mtime = readme.stat().st_mtime
            if _APP_VERSION_CACHE["value"] and _APP_VERSION_CACHE["mtime"] == mtime:
                return _APP_VERSION_CACHE["value"]
            text = readme.read_text(errors="replace")
            m = re.search(r"最新版[^v]*?(v\d+\.\d+\.\d+)", text)
            if m:
                _APP_VERSION_CACHE["value"] = m.group(1)
                _APP_VERSION_CACHE["mtime"] = mtime
                return m.group(1)
    except Exception:
        pass
    # fallback: dashboard_api code_version
    try:
        import dashboard_api
        info = dashboard_api.code_version() or {}
        subject = (info.get("subject") or "").strip()
        sha = (info.get("sha") or "").strip()
        m = re.search(r"v\d+\.\d+\.\d+", subject)
        if m:
            return m.group(0)
        if sha:
            return sha
    except Exception:
        pass
    return ""


@app.context_processor
def _inject_app_version():
    return {"app_version": _app_version_string()}


# 管理密码（从 .env 读取 WEB_PASSWORD，默认 tg@monitor2026）
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "tg@monitor2026")

# 登录中的 client 暂存 {phone: {"client": client, "phone_hash": hash}}
_pending = {}

# asyncio event loop for telethon
_loop = asyncio.new_event_loop()


def _start_loop():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


threading.Thread(target=_start_loop, daemon=True).start()


def run_async(coro):
    """在后台 event loop 中执行 async 函数"""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=30)


async def _make_client(session_path):
    """在 _loop 线程内创建 TelegramClient（避免 no event loop 错误）"""
    return TelegramClient(
        session_path, config.API_ID, config.API_HASH,
        device_model=config.DEVICE_NAME,
        system_version="1.0", app_version="1.0",
    )


async def _sign_in_code(client, phone, code, phone_hash):
    """验证码 sign_in + get_me。整个流程在 _loop 线程执行，避免 no event loop 错误"""
    await client.sign_in(phone, code, phone_code_hash=phone_hash)
    return await client.get_me()


async def _sign_in_password(client, password):
    """两步验证密码 sign_in + get_me，同样包在 _loop 线程"""
    await client.sign_in(password=password)
    return await client.get_me()


async def _disconnect(client):
    """关闭 client，必须在 _loop 线程执行"""
    await client.disconnect()


async def _connect(client):
    """连接 client，必须在 _loop 线程执行"""
    await client.connect()


async def _send_code_req(client, phone):
    """发送验证码，必须在 _loop 线程执行"""
    return await client.send_code_request(phone)


async def _is_authorized(client):
    return await client.is_user_authorized()


async def _get_me(client):
    return await client.get_me()


def _load_session_states_map():
    """v2.10.6: 读 data/.session_states.json — 账号管理页用"""
    import json
    try:
        from pathlib import Path as _P
        sp = _P("/app/data/.session_states.json")
        if not sp.exists():
            sp = _P(__file__).parent / "data" / ".session_states.json"
        if sp.exists():
            return json.loads(sp.read_text())
    except Exception:
        pass
    return {}


def get_sessions():
    """扫描 sessions 目录，返回已有的 session 列表"""
    sessions = []
    _session_states = _load_session_states_map()
    for f in config.SESSION_DIR.glob("*.session"):
        phone = "+" + f.stem
        # 先查 DB
        account = db.get_account_by_phone(phone)
        if account and account["name"]:
            sessions.append({
                "phone": phone,
                "name": account["name"],
                "username": account["username"] or "",
                "tg_id": account["tg_id"] or "",
                "company": account["company"] or "",
                "operator": account["operator"] or "",
                "status": "active",
                "session_status": (_session_states.get(phone) or {}).get("status", "unknown"),
            })
        else:
            # DB 没有，尝试连 Telegram 获取
            try:
                session_path = str(config.SESSION_DIR / f.stem)
                client = run_async(_make_client(session_path))
                run_async(_connect(client))
                if run_async(_is_authorized(client)):
                    me = run_async(_get_me(client))
                    name = ((me.first_name or "") + " " + (me.last_name or "")).strip()
                    username = me.username or ""
                    # 写入 DB
                    db.upsert_account(phone=phone, name=name, username=username, tg_id=me.id)
                    sessions.append({
                        "phone": phone, "name": name, "username": username,
                        "tg_id": me.id, "company": "", "operator": "", "status": "active",
                        "session_status": (_session_states.get(phone) or {}).get("status", "healthy"),
                    })
                    run_async(_disconnect(client))
                else:
                    sessions.append({
                        "phone": phone, "name": "", "username": "",
                        "tg_id": "", "company": "", "operator": "", "status": "expired",
                        "session_status": "revoked",
                    })
                    run_async(_disconnect(client))
            except Exception as e:
                sessions.append({
                    "phone": phone, "name": "", "username": "",
                    "tg_id": "", "company": "", "operator": "", "status": "error",
                    "session_status": "error",
                })
    return sessions


# ============ 首次设置与设置修改 ============

@app.before_request
def _redirect_to_setup_if_needed():
    """未完成首次设置的话，把所有请求导到 /setup（除了静态资源和 setup 自身）。

    注意:/api/test-* 只在 setup 未完成时免登录(setup 页需要调用)。
    setup 完成后这些端点走正常登录流程,不能永久裸露 — 否则攻击者可以用 token 扫 Sheet/Bot。"""
    path = request.path
    # 静态和 setup 本体永远放行
    if path.startswith("/setup") or path.startswith("/api/setup") or path.startswith("/static"):
        return None
    # OAuth 流程(setup 页要跳转授权 + 回调)永远放行
    # /api/oauth/callback 更是必须放行,否则 Google 回调回来直接被重定向丢 code
    if path.startswith("/api/oauth/"):
        return None
    # /api/sheets/* 和 /api/drive/* 都是 setup 流程的一环(自动建表格 / 自动建文件夹)
    # setup 未完成时放行,完成后需登录
    if path.startswith("/api/sheets/") or path.startswith("/api/drive/") or path.startswith("/api/setup/"):
        if not is_setup_complete():
            return None
        if not flask_session.get("authed"):
            return jsonify({"ok": False, "msg": "未登录"}), 401
        return None
    # /api/test-* 仅在 setup 未完成时放行(setup 页 JS 要调);完成后必须登录才能调
    if path.startswith("/api/test-"):
        if not is_setup_complete():
            return None
        if not flask_session.get("authed"):
            return jsonify({"ok": False, "msg": "未登录"}), 401
        return None
    if not is_setup_complete():
        return redirect(url_for("setup_page"))


@app.route("/setup", methods=["GET"])
def setup_page():
    """首次设置页。若已完成则导回首页"""
    if is_setup_complete():
        return redirect(url_for("index"))
    env = read_env()
    # 预设值填进去
    defaults = {
        "company_name": env.get("COMPANY_NAME", ""),
        "company_display": env.get("COMPANY_DISPLAY", ""),
        "peer_role_label": env.get("PEER_ROLE_LABEL", "广告主"),
        "operator_label": env.get("OPERATOR_LABEL", "商务人员"),
        "bot_token": env.get("BOT_TOKEN", ""),
        "alert_group_id": env.get("ALERT_GROUP_ID", ""),
        # v2.10.23: 审核按钮白名单(空=不校验)
        "callback_auth_user_ids": env.get("CALLBACK_AUTH_USER_IDS", ""),
        "sheet_id": env.get("SHEET_ID", ""),
        "media_folder_id": env.get("MEDIA_FOLDER_ID", ""),
        "media_max_mb": env.get("MEDIA_MAX_MB", "20"),
        "media_retention_days": env.get("MEDIA_RETENTION_DAYS", "0"),
        # v2.10.25(ADR-0014):媒体存储模式 + TG 档案群 ID
        "media_storage_mode": (env.get("MEDIA_STORAGE_MODE", "drive") or "drive").lower(),
        "media_archive_group_id": env.get("MEDIA_ARCHIVE_GROUP_ID", ""),
        "oauth_client_id": env.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        "oauth_client_secret": env.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        "oauth_status": _get_oauth_status(),
        "web_password": env.get("WEB_PASSWORD", DEFAULT_WEB_PASSWORD),
        "keywords": env.get("KEYWORDS", DEFAULT_KEYWORDS),
        "no_reply_minutes": env.get("NO_REPLY_MINUTES", DEFAULT_NO_REPLY_MINUTES),
        "api_id": env.get("API_ID", DEFAULT_API_ID),
        "api_hash": env.get("API_HASH", DEFAULT_API_HASH),
    }
    return render_template("setup.html", d=defaults, mode="setup", company=config.COMPANY_DISPLAY)


@app.route("/settings", methods=["GET"])
@login_required
def settings_page():
    """登入后修改设置 — 管理员看全部,普通用户只能改自己密码"""
    env = read_env()
    me = flask_session.get("username", "")
    current = {
        "company_name": env.get("COMPANY_NAME", ""),
        "company_display": env.get("COMPANY_DISPLAY", ""),
        "peer_role_label": env.get("PEER_ROLE_LABEL", "广告主"),
        "operator_label": env.get("OPERATOR_LABEL", "商务人员"),
        "bot_token": env.get("BOT_TOKEN", ""),
        "alert_group_id": env.get("ALERT_GROUP_ID", ""),
        # v2.10.23: 审核按钮白名单(空=不校验)
        "callback_auth_user_ids": env.get("CALLBACK_AUTH_USER_IDS", ""),
        "sheet_id": env.get("SHEET_ID", ""),
        "media_folder_id": env.get("MEDIA_FOLDER_ID", ""),
        "media_max_mb": env.get("MEDIA_MAX_MB", "20"),
        "media_retention_days": env.get("MEDIA_RETENTION_DAYS", "0"),
        # v2.10.25(ADR-0014):媒体存储模式 + TG 档案群 ID
        "media_storage_mode": (env.get("MEDIA_STORAGE_MODE", "drive") or "drive").lower(),
        "media_archive_group_id": env.get("MEDIA_ARCHIVE_GROUP_ID", ""),
        "oauth_client_id": env.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        "oauth_client_secret": env.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        "oauth_status": _get_oauth_status(),
        "keywords": env.get("KEYWORDS", DEFAULT_KEYWORDS),
        "no_reply_minutes": env.get("NO_REPLY_MINUTES", DEFAULT_NO_REPLY_MINUTES),
        "api_id": env.get("API_ID", DEFAULT_API_ID),
        "api_hash": env.get("API_HASH", DEFAULT_API_HASH),
        # v2.6.2: 预警 / 日报开关(settings 页可改,dashboard 也能切换预警)
        # v2.6.6: 三个独立子开关 — 留空跟随 ALERTS_ENABLED 总开关
        "alerts_enabled": env.get("ALERTS_ENABLED", "true").lower() != "false",
        "alert_keyword_enabled": (
            env.get("ALERT_KEYWORD_ENABLED", "").lower() == "true"
            if env.get("ALERT_KEYWORD_ENABLED", "").lower() in ("true", "false")
            else env.get("ALERTS_ENABLED", "true").lower() != "false"
        ),
        "alert_no_reply_enabled": (
            env.get("ALERT_NO_REPLY_ENABLED", "").lower() == "true"
            if env.get("ALERT_NO_REPLY_ENABLED", "").lower() in ("true", "false")
            else env.get("ALERTS_ENABLED", "true").lower() != "false"
        ),
        "alert_delete_enabled": (
            env.get("ALERT_DELETE_ENABLED", "").lower() == "true"
            if env.get("ALERT_DELETE_ENABLED", "").lower() in ("true", "false")
            else env.get("ALERTS_ENABLED", "true").lower() != "false"
        ),
        "daily_report_enabled": (
            env.get("DAILY_REPORT_ENABLED", "").lower() == "true"
            if env.get("DAILY_REPORT_ENABLED", "").lower() in ("true", "false")
            else env.get("ALERTS_ENABLED", "true").lower() != "false"
        ),
        "is_admin": is_admin(me),
        "is_super": is_super(me),
        "me": me,
        # v2.8.0: 中央台接入 Token(兜底:空就自动生成并写回,保证打开设置页永远有值)
        "metrics_token": _ensure_metrics_token(env),
        "metrics_access_count_24h": _metrics_access_count(24),
        "metrics_last_access": _metrics_last_access(),
    }
    return render_template("setup.html", d=current, mode="settings", company=config.COMPANY_DISPLAY)


@app.route("/api/test-bot", methods=["POST"])
def api_test_bot():
    """测试 Bot Token + 预警群（发一则测试消息）"""
    data = request.get_json(silent=True) or request.form
    ok, msg = _test_bot_api(data.get("bot_token"), data.get("alert_group_id"))
    return jsonify({"ok": ok, "msg": msg})


def _get_oauth_status():
    """返回 OAuth 当前状态给前端展示"""
    try:
        import oauth_helper
        if not oauth_helper.has_token():
            return {"connected": False, "email": ""}
        # token 文件里没存 email,简化:连接状态 = 文件存在
        return {"connected": True, "email": oauth_helper.load_token().get("email", "")}
    except Exception as e:
        return {"connected": False, "email": "", "error": str(e)}


def _oauth_redirect_uri():
    """根据当前请求构造回调 URI(必须跟 Google Cloud Console 配的完全一致)"""
    # 优先用环境变量(客户可指定),否则从请求 host 推导
    explicit = read_env().get("OAUTH_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    # request.host_url 形如 http://76.13.219.163:5002/
    return request.host_url.rstrip("/") + "/api/oauth/callback"


@app.route("/api/oauth/start", methods=["GET"])
def api_oauth_start():
    """开始 Google OAuth 流程 — 跳转到 Google 授权页

    注意:不挂 @admin_required — 首次 setup 时还没建账号,setup 页要调这个接口,
    挂上会卡死客户在 setup 永远跨不过 OAuth 这步。
    setup 完成后再检查:只有管理员能重新授权,避免普通用户把授权换到自己账号。
    """
    if is_setup_complete():
        # setup 完成后必须已登录且是管理员才能重走 OAuth
        if not flask_session.get("authed"):
            return redirect(url_for("login_page"))
        if not is_admin(flask_session.get("username", "")):
            return "需要管理员权限", 403
    env = read_env()
    cid = env.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    csec = env.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        return "请先在设置里填写 OAuth Client ID 和 Secret 并保存", 400
    # 用 state 带回「授权完要去哪」— Google 会原样回传
    # 白名单 /setup 和 /settings,避免 open-redirect
    return_to = request.args.get("return", "").strip()
    if return_to not in ("/setup", "/settings"):
        return_to = ""
    try:
        import oauth_helper
        url = oauth_helper.build_auth_url(cid, csec, _oauth_redirect_uri(), state=return_to)
        return redirect(url)
    except Exception as e:
        return f"授权 URL 生成失败: {e}", 500


@app.route("/api/drive/auto-create-folder", methods=["POST"])
def api_drive_auto_create_folder():
    """OAuth 已连接但 MEDIA_FOLDER_ID 空时调一下,自动建文件夹并回写 .env。

    OAuth callback 里本来就会建,但如果 Drive API 当时没启用会失败。
    API 启用后调这个端点就能补建。幂等(有就复用)。
    """
    import oauth_helper
    if not oauth_helper.has_token():
        return jsonify({"ok": False, "msg": "请先完成 Google 授权"})
    existing = (read_env().get("MEDIA_FOLDER_ID") or "").strip()
    if existing:
        return jsonify({"ok": True, "folder_id": existing, "created": False})
    try:
        folder_id = oauth_helper.auto_create_folder("tg-monitor-媒体")
    except Exception as e:
        return jsonify({"ok": False, "msg": f"建立文件夹失败: {e}"})
    if not folder_id:
        return jsonify({"ok": False, "msg": "建立文件夹失败,查看日志(常见:Drive API 未启用)"})
    write_env({"MEDIA_FOLDER_ID": folder_id})
    try:
        import importlib
        importlib.reload(config)
    except Exception as e:
        logger.warning(f"reload config 失败(不影响): {e}")
    return jsonify({"ok": True, "folder_id": folder_id, "created": True})


@app.route("/api/setup/save-oauth-creds", methods=["POST"])
def api_setup_save_oauth_creds():
    """setup 精灵内:在跳 Google 前暂存 Client ID/Secret 到 .env。

    只在 setup 未完成时放行 — 不然任何人都能改 OAuth 凭证。
    不设 SETUP_COMPLETE=true,精灵其余字段还没填。
    """
    if is_setup_complete():
        return jsonify({"ok": False, "msg": "已完成首次设置,请到设置页修改"})
    cid = (request.form.get("oauth_client_id") or "").strip()
    csec = (request.form.get("oauth_client_secret") or "").strip()
    if not cid or not csec:
        return jsonify({"ok": False, "msg": "Client ID 和 Secret 不能为空"})
    try:
        write_env({
            "GOOGLE_OAUTH_CLIENT_ID": cid,
            "GOOGLE_OAUTH_CLIENT_SECRET": csec,
        })
        return jsonify({"ok": True, "msg": "已暂存,跳转 Google 授权..."})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"写入 .env 失败: {e}"})


@app.route("/api/oauth/callback", methods=["GET"])
def api_oauth_callback():
    """Google 授权完成后回调到这里 — 用 code 换 token 并存盘"""
    code = request.args.get("code", "")
    err = request.args.get("error", "")
    # state 里是之前 build_auth_url 带过来的「回到哪」— 白名单过一道防 open-redirect
    return_to = (request.args.get("state") or "").strip()
    if return_to not in ("/setup", "/settings"):
        return_to = ""
    if err:
        if return_to == "/setup":
            return redirect(f"/setup?oauth_error={err}")
        return f"<h2>❌ 授权失败:{err}</h2><a href='/settings'>← 返回设置</a>", 400
    if not code:
        return "<h2>❌ 缺少 authorization code</h2>", 400
    env = read_env()
    cid = env.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    csec = env.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        return "<h2>❌ 配置丢失,请回到设置重填</h2>", 400
    try:
        import oauth_helper
        email = oauth_helper.exchange_code(cid, csec, _oauth_redirect_uri(), code)
        # token 里加上 email 给 UI 展示
        t = oauth_helper.load_token() or {}
        t["email"] = email
        oauth_helper.save_token(t)
        # 重置 Drive client 让下次上传用新 token
        try:
            import media_uploader
            media_uploader.reset_drive_cache()
        except Exception:
            pass

        # 自动建 Drive 文件夹(如果客户没填) → 客户连建文件夹这步都免了
        existing_folder = read_env().get("MEDIA_FOLDER_ID", "").strip()
        auto_folder_msg = ""
        folder_changed = False
        if not existing_folder:
            try:
                folder_id = oauth_helper.auto_create_folder("tg-monitor-媒体")
                if folder_id:
                    write_env({"MEDIA_FOLDER_ID": folder_id})
                    folder_changed = True
                    try:
                        import importlib
                        importlib.reload(config)
                    except Exception:
                        pass
                    auto_folder_msg = f"<p style='color:#9ef0b8;'>✓ 自动建好了 Drive 文件夹「tg-monitor-媒体」(id: <code>{folder_id[:20]}...</code>)</p>"
            except Exception as e:
                auto_folder_msg = f"<p style='color:#ff9b3d;'>⚠ 自动建文件夹失败({e}),请手动到设置页填 Drive 文件夹 ID</p>"

        # 新 MEDIA_FOLDER_ID 写进 .env 了,tg-monitor 进程里的 config 还是旧的空值
        # → 必须 restart 容器才能读到,否则后续收到的媒体依然走不进 Drive 上传分支
        # (今天就是这个坑:OAuth 连完后所有图片都显示 [图片] 占位)
        if folder_changed:
            try:
                ok_r, msg_r = _start_tg_monitor()
                if ok_r:
                    auto_folder_msg += "<p style='color:#9ef0b8;'>✓ 已重启监控服务,新配置即刻生效</p>"
                else:
                    auto_folder_msg += f"<p style='color:#ff9b3d;'>⚠ 重启监控服务失败({msg_r}),请手动 docker compose restart tg-monitor</p>"
            except Exception as e:
                auto_folder_msg += f"<p style='color:#ff9b3d;'>⚠ 重启监控服务异常({e})</p>"
        # 精灵触发的 OAuth → 直接跳回 /setup?oauth_done=1,让用户继续填剩下的字段
        # 设置页触发的 OAuth → 显示老的成功页(有详细信息)
        if return_to == "/setup":
            return redirect("/setup?oauth_done=1")
        return f"""
        <html><head><meta charset='utf-8'><title>授权成功</title></head>
        <body style='font-family:sans-serif;background:#0a0e14;color:#cfe3f5;padding:60px;text-align:center;'>
          <h1 style='color:#4ade80;'>✓ Google Drive 已连接</h1>
          <p style='font-size:18px;margin:20px 0;'>授权账号:<code style='background:#1a2230;padding:4px 10px;border-radius:4px;'>{email or '(unknown)'}</code></p>
          <p style='color:#7c8a9a;'>后续客户发的图片/文件会上传到这个账号的 Drive,使用其 15GB 免费配额。</p>
          {auto_folder_msg}
          <p style='margin-top:40px;'><a href='/settings' style='color:#7ec9ff;font-size:16px;'>← 返回设置页</a></p>
        </body></html>
        """
    except Exception as e:
        return f"<h2>❌ 换取 token 失败</h2><pre style='background:#222;padding:14px;'>{e}</pre><a href='/settings'>← 返回</a>", 500


@app.route("/api/oauth/revoke", methods=["POST"])
def api_oauth_revoke():
    """撤销 OAuth 授权

    同 /api/oauth/start — setup 未完成时放行(客户可能授权错帐号要重来);
    setup 完成后只有管理员能撤。
    """
    if is_setup_complete():
        if not flask_session.get("authed"):
            return jsonify({"ok": False, "msg": "请先登录"}), 401
        if not is_admin(flask_session.get("username", "")):
            return jsonify({"ok": False, "msg": "需要管理员权限"}), 403
    try:
        import oauth_helper
        ok, msg = oauth_helper.revoke_token()
        try:
            import media_uploader
            media_uploader.reset_drive_cache()
        except Exception:
            pass
        return jsonify({"ok": ok, "msg": msg})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/oauth/status", methods=["GET"])
@admin_required
def api_oauth_status():
    return jsonify(_get_oauth_status())


@app.route("/api/sheets/auto-create", methods=["POST"])
@_synchronized(_auto_create_sheet_lock)
def api_sheets_auto_create():
    """OAuth 授权后,自动建一个 Spreadsheet,ID 写回 .env。
    已有 SHEET_ID 则直接返回现有的,不重复建。
    v2.10.12: 加全局锁防用户双击按钮触发并发建两个 sheet。"""
    import oauth_helper
    if not oauth_helper.has_token():
        return jsonify({"ok": False, "msg": "请先完成 Google 授权"})
    # 读最新 .env(优先)而不是依赖模块级 config,避免同一进程内缓存过期
    existing = (read_env().get("SHEET_ID") or "").strip()
    if existing:
        # v2.10.11: 即使 SHEET_ID 已存在也 heal 一次预警分页(应对旧部门空白 sheet)
        try:
            from sheets import SheetsWriter
            SheetsWriter()
            logger.info("v2.10.11: 已有 sheet 重新 heal 预警分页完成")
        except Exception as e:
            logger.warning(f"heal 预警分页失败(不影响): {e}")
        return jsonify({
            "ok": True,
            "sheet_id": existing,
            "url": f"https://docs.google.com/spreadsheets/d/{existing}/edit",
            "created": False,
        })
    # 标题用部门显示名
    env = read_env()
    title = "TG监控-" + (env.get("COMPANY_DISPLAY") or env.get("COMPANY_NAME") or "default")
    try:
        sheet_id = oauth_helper.auto_create_sheet(title)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"建立 Spreadsheet 失败: {e}"})
    if not sheet_id:
        return jsonify({"ok": False, "msg": "建立 Spreadsheet 失败,查看日志"})
    # 写回 .env 并 reload config
    write_env({"SHEET_ID": sheet_id})
    try:
        import importlib
        importlib.reload(config)
    except Exception as e:
        logger.warning(f"reload config 失败(不影响写入): {e}")

    # v2.10.11: 立刻建预警分页 (不用等 tg-monitor 启动)
    # 以前预警分页只在 SheetsWriter.__init__ 建,但 tg-monitor 没 session 会
    # 直接 return 不实例化 SheetsWriter → 新部门还没登入账号时 sheet 一片空白
    try:
        from sheets import SheetsWriter
        SheetsWriter()  # __init__ 自动调 ensure_alert_tabs + ensure_account_tabs
        logger.info("v2.10.11: sheet 建好后自动初始化预警分页完成")
    except Exception as e:
        logger.warning(f"自动初始化预警分页失败(不影响建 Sheet,登入账号后会补): {e}")

    return jsonify({
        "ok": True,
        "sheet_id": sheet_id,
        "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
        "created": True,
    })


@app.route("/api/test-media-folder", methods=["POST"])
def api_test_media_folder():
    """测试 Drive 文件夹访问权限 — 使用当前 OAuth 授权凭证(建探针文件再删掉)"""
    folder_id = (request.form.get("media_folder_id") or "").strip()
    if not folder_id:
        return jsonify({"ok": False, "msg": "请填写 Drive 文件夹 ID"})
    # 必须已授权 OAuth
    try:
        import oauth_helper
        if not oauth_helper.has_token():
            return jsonify({"ok": False, "msg": "请先完成 Google 授权(上方「连接 Google Drive」)"})
        creds = oauth_helper.get_credentials()
        if not creds:
            return jsonify({"ok": False, "msg": "OAuth 凭证失效,请重新授权"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"获取 OAuth 凭证失败: {e}"})
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload
        import io as _io
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        # 1. 文件夹存在性 + 访问权限
        meta = drive.files().get(fileId=folder_id, fields="id,name,mimeType",
                                 supportsAllDrives=True).execute()
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            return jsonify({"ok": False, "msg": f"ID「{folder_id}」不是文件夹"})
        folder_name = meta.get("name", "")
        # 2. 写权限测试:建一个 4 字节探针文件,立即删掉
        probe = MediaIoBaseUpload(_io.BytesIO(b"ping"), mimetype="text/plain", resumable=False)
        f = drive.files().create(
            body={"name": "_tg_monitor_probe.txt", "parents": [folder_id]},
            media_body=probe, fields="id", supportsAllDrives=True,
        ).execute()
        try:
            drive.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
        except Exception:
            pass
        return jsonify({"ok": True, "msg": f"成功访问文件夹「{folder_name}」并通过写入测试 ✓"})
    except Exception as e:
        err = str(e)
        if "File not found" in err or "notFound" in err:
            return jsonify({"ok": False, "msg": "文件夹找不到或没权限。请确认这个文件夹 ID 是当前 OAuth 授权帐号自己的(或已被分享为编辑者)。"})
        return jsonify({"ok": False, "msg": f"{type(e).__name__}: {err}"})


@app.route("/api/media/cleanup-now", methods=["POST"])
@login_required
def api_media_cleanup_now():
    """手动触发一次 Drive 旧媒体清理。

    天数优先级:表单 days > .env MEDIA_RETENTION_DAYS
    (允许客户在设置页改完输入框,不保存直接点「立即清理」用新值试水)
    """
    try:
        days_str = (request.form.get("days", "") or "").strip()
        if not days_str:
            days_str = read_env().get("MEDIA_RETENTION_DAYS", "0") or "0"
        try:
            days = int(days_str)
        except ValueError:
            return jsonify({"ok": False, "msg": f"保留天数非法: {days_str}"})
        if days <= 0:
            return jsonify({"ok": False, "msg": "保留天数需 > 0 (0 表示永不清理)"})
        # 让 media_uploader 用最新 config
        try:
            import importlib
            importlib.reload(config)
        except Exception:
            pass
        import media_uploader
        if not media_uploader.is_enabled():
            return jsonify({"ok": False, "msg": "MEDIA_FOLDER_ID 未配置,请先连接 Google Drive"})
        deleted, failed = media_uploader.cleanup_old_media(days)
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "failed": failed,
            "msg": f"清理完成:删除 {deleted} 个文件" + (f",失败 {failed} 个" if failed else "")
                   + (" (超过 " + str(days) + " 天的旧文件)"),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": f"{type(e).__name__}: {e}"})


@app.route("/api/test-sheets", methods=["POST"])
def api_test_sheets():
    """测试 Sheet ID 能否用当前 OAuth 凭证访问。
    SHEET_ID 为空时直接返回 ok(会在授权后由 /api/sheets/auto-create 自动建)。"""
    sheet_id = (request.form.get("sheet_id") or "").strip()
    if not sheet_id:
        return jsonify({"ok": True, "msg": "Sheet ID 留空 — 将在 Google 授权后自动建一份"})
    # SHEET_ID 已填 → 必须已授权才能验证
    try:
        import oauth_helper
        if not oauth_helper.has_token():
            return jsonify({"ok": False, "msg": "请先完成 Google 授权,才能验证 Sheet 访问权限"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"获取 OAuth 状态失败: {e}"})
    ok, msg = _test_sheets_access(sheet_id)
    # v2.10.14: 验证通过就立刻持久化 SHEET_ID,避免用户只点「测试」没点「保存并启动」
    # 导致 .env SHEET_ID 留空 — tg-monitor 启动报 `RuntimeError: SHEET_ID 为空`
    if ok:
        try:
            write_env({"SHEET_ID": sheet_id})
        except Exception as e:
            logger.warning(f"[test-sheets] 持久化 SHEET_ID 失败(不影响验证结果): {e}")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/setup", methods=["POST"])
def api_setup():
    """首次设置提交"""
    if is_setup_complete():
        return jsonify({"ok": False, "msg": "已完成首次设置，请到设置页修改"})
    return _save_settings(is_first=True)


@app.route("/api/update-settings", methods=["POST"])
@admin_required
def api_update_settings():
    """后续修改设置（仅管理员）"""
    return _save_settings(is_first=False)


def _save_settings(is_first):
    """共用的保存逻辑"""
    form = request.form
    company_name = (form.get("company_name") or "").strip()
    bot_token = (form.get("bot_token") or "").strip()
    alert_group_id = (form.get("alert_group_id") or "").strip()
    sheet_id = (form.get("sheet_id") or "").strip()

    # 必填验证(SHEET_ID 纯 OAuth 后允许留空,授权后由 /api/sheets/auto-create 自动建)
    if not company_name or not bot_token or not alert_group_id:
        return jsonify({"ok": False, "msg": "部门名称、Bot Token、预警群 ID 都必填"})

    # 首次设置：必须提供管理员账号+密码
    if is_first:
        admin_user = (form.get("admin_username") or "").strip()
        admin_pwd = (form.get("admin_password") or "").strip()
        if not admin_user or not admin_pwd:
            return jsonify({"ok": False, "msg": "主帐号和密码都不能为空"})
        if len(admin_pwd) < 6:
            return jsonify({"ok": False, "msg": "密码至少 6 位"})

    # API_ID / API_HASH 必填(默认空,客户必须自己去 my.telegram.org 申请)
    api_id_val = (form.get("api_id") or "").strip()
    api_hash_val = (form.get("api_hash") or "").strip()
    if not api_id_val or not api_hash_val:
        return jsonify({"ok": False, "msg": "Telegram API ID 和 API Hash 都不能为空,请到 my.telegram.org 申请"})
    if not api_id_val.isdigit():
        return jsonify({"ok": False, "msg": "API ID 应为纯数字"})

    # Sheet 验证:纯 OAuth。只在 SHEET_ID 已填 且 OAuth 已授权 时才尝试验证。
    # 首次 setup 时通常 SHEET_ID 留空 + OAuth 尚未授权 — 直接跳过,后续由 auto-create 补齐。
    if sheet_id:
        try:
            import oauth_helper
            if oauth_helper.has_token():
                ok, msg = _test_sheets_access(sheet_id)
                if not ok:
                    return jsonify({"ok": False, "msg": f"Sheet 验证失败:{msg}"})
        except Exception as e:
            logger.warning(f"Sheet 验证跳过(OAuth 不可用): {e}")

    # 验证 Bot
    ok, msg = _test_bot_api(bot_token, alert_group_id)
    if not ok:
        return jsonify({"ok": False, "msg": f"Bot 验证失败：{msg}"})

    # 首次:建立主帐号(is_super=True,is_admin=True)
    if is_first:
        save_users({admin_user: {
            "password_hash": generate_password_hash(admin_pwd),
            "is_admin": True,
            "is_super": True,
        }})

    # 关键词 diff（让前端 banner 显示新增/移除了哪些）
    def _parse_kw(s):
        return [k.strip() for k in (s or "").split(",") if k.strip()]
    old_keywords = _parse_kw(read_env().get("KEYWORDS", ""))
    new_keywords_str = form.get("keywords", DEFAULT_KEYWORDS)
    new_keywords = _parse_kw(new_keywords_str)
    kw_added = [k for k in new_keywords if k not in old_keywords]
    kw_removed = [k for k in old_keywords if k not in new_keywords]

    # v2.10.23: 审核按钮白名单 — 归一化 (去空格 + 过滤非数字),空值留空字符串(= 不校验)
    callback_auth_raw = (form.get("callback_auth_user_ids") or "").strip()
    callback_auth_normalized = ",".join(
        x.strip() for x in callback_auth_raw.split(",")
        if x.strip() and x.strip().isdigit()
    )

    # 写 .env
    updates = {
        "COMPANY_NAME": company_name,
        "COMPANY_DISPLAY": form.get("company_display", "").strip() or company_name,
        "PEER_ROLE_LABEL": form.get("peer_role_label", "").strip() or "广告主",
        "OPERATOR_LABEL": form.get("operator_label", "").strip()[:10] or "商务人员",
        "BOT_TOKEN": bot_token,
        "ALERT_GROUP_ID": alert_group_id,
        "CALLBACK_AUTH_USER_IDS": callback_auth_normalized,
        "SHEET_ID": sheet_id,
        "MEDIA_FOLDER_ID": form.get("media_folder_id", "").strip(),
        "MEDIA_MAX_MB": form.get("media_max_mb", "20").strip() or "20",
        "MEDIA_RETENTION_DAYS": form.get("media_retention_days", "0").strip() or "0",
        # v2.10.25(ADR-0014):媒体存储模式 + TG 档案群 ID(模式枚举只允许 drive/tg_archive/off,其他值回退 drive 保兼容)
        "MEDIA_STORAGE_MODE": (form.get("media_storage_mode", "drive").strip().lower()
                               if form.get("media_storage_mode", "drive").strip().lower() in ("drive", "tg_archive", "off")
                               else "drive"),
        "MEDIA_ARCHIVE_GROUP_ID": form.get("media_archive_group_id", "").strip(),
        "GOOGLE_OAUTH_CLIENT_ID": form.get("oauth_client_id", "").strip(),
        "GOOGLE_OAUTH_CLIENT_SECRET": form.get("oauth_client_secret", "").strip(),
        "KEYWORDS": new_keywords_str,
        "NO_REPLY_MINUTES": form.get("no_reply_minutes", DEFAULT_NO_REPLY_MINUTES),
        "API_ID": form.get("api_id", DEFAULT_API_ID),
        "API_HASH": form.get("api_hash", DEFAULT_API_HASH),
        "SETUP_COMPLETE": "true",
    }
    if is_first:
        updates["WEB_PORT"] = read_env().get("WEB_PORT", "5001")
    write_env(updates)

    # 重新加载 config 模块，让 web 这一进程立即看到新的 API_ID / API_HASH / BOT_TOKEN / SHEET_ID
    # 否则用户配置完后立刻去「添加账号」会拿到旧（空）值，要手动 docker restart
    try:
        import importlib
        importlib.reload(config)
    except Exception as e:
        print(f"[warn] 重新加载 config 失败（不影响 tg-monitor）: {e}")

    # OAuth client 配置可能改了 → 让 Drive client 下次重建
    try:
        import media_uploader
        media_uploader.reset_drive_cache()
    except Exception:
        pass

    # 启动/重启 tg-monitor
    ok, msg_docker = _start_tg_monitor()
    return jsonify({
        "ok": True,
        "msg": "设置已保存" + ("并启动监控服务" if is_first else "并重启监控服务"),
        "docker_ok": ok,
        "docker_msg": msg_docker,
        "kw_added": kw_added,
        "kw_removed": kw_removed,
        "redirect": url_for("login_page"),
    })


# ============ 用户账号管理 API ============
@app.route("/api/users/list", methods=["GET"])
@login_required
def api_users_list():
    """主帐号看全部,其他人只能看自己"""
    me = flask_session.get("username", "")
    users = load_users()
    me_is_super = is_super(me)
    if not me_is_super:
        my_data = users.get(me, {})
        return jsonify({
            "users": [{
                "username": me,
                "is_admin": bool(my_data.get("is_admin", False) or my_data.get("is_super", False)),
                "is_super": bool(my_data.get("is_super", False)),
            }],
            "me": me,
            "is_admin": is_admin(me),
            "is_super": False,
        })
    user_list = [
        {
            "username": u,
            "is_admin": bool(data.get("is_admin", False) or data.get("is_super", False)),
            "is_super": bool(data.get("is_super", False)),
        }
        for u, data in sorted(users.items())
    ]
    return jsonify({"users": user_list, "me": me, "is_admin": True, "is_super": True})


@app.route("/api/users/add", methods=["POST"])
@super_required
def api_users_add():
    """新增账号(仅主帐号)— 可指定是管理员还是普通成员"""
    data = request.get_json(silent=True) or request.form
    u = (data.get("username") or "").strip()
    p = (data.get("password") or "").strip()
    role_is_admin = bool(data.get("is_admin", False))
    if not u or not p:
        return jsonify({"ok": False, "msg": "账号和密码都不能为空"})
    if len(p) < 6:
        return jsonify({"ok": False, "msg": "密码至少 6 位"})
    if not u.replace("_", "").replace("-", "").isalnum():
        return jsonify({"ok": False, "msg": "账号只能包含字母、数字、下划线、横线"})
    users = load_users()
    if u in users:
        return jsonify({"ok": False, "msg": f"账号「{u}」已存在"})
    users[u] = {
        "password_hash": generate_password_hash(p),
        "is_admin": role_is_admin,
        "is_super": False,  # 新增的账号永不是主帐号
    }
    save_users(users)
    role_label = "管理员" if role_is_admin else "普通成员"
    return jsonify({"ok": True, "msg": f"账号「{u}」已添加({role_label})"})


@app.route("/api/users/remove", methods=["POST"])
@super_required
def api_users_remove():
    """移除账号(仅主帐号)— 主帐号不能被删除"""
    data = request.get_json(silent=True) or request.form
    u = (data.get("username") or "").strip()
    users = load_users()
    if u not in users:
        return jsonify({"ok": False, "msg": "账号不存在"})
    if users[u].get("is_super"):
        return jsonify({"ok": False, "msg": "主帐号不可被移除"})
    del users[u]
    save_users(users)
    return jsonify({"ok": True, "msg": f"账号「{u}」已移除"})


@app.route("/api/users/change-password", methods=["POST"])
@login_required
def api_users_change_password():
    """改自己的密码(所有用户)"""
    data = request.get_json(silent=True) or request.form
    old = (data.get("old_password") or "").strip()
    new = (data.get("new_password") or "").strip()
    me = flask_session.get("username", "")
    if not me:
        return jsonify({"ok": False, "msg": "未登录"})
    if not verify_user(me, old):
        return jsonify({"ok": False, "msg": "旧密码错误"})
    if len(new) < 6:
        return jsonify({"ok": False, "msg": "新密码至少 6 位"})
    users = load_users()
    if me not in users:
        return jsonify({"ok": False, "msg": "账号不存在"})
    users[me]["password_hash"] = generate_password_hash(new)
    save_users(users)
    return jsonify({"ok": True, "msg": "密码已更新"})


# ============ TG 绑定 / 密码重置 ============
import auth_reset  # noqa: E402

_bot_username_cache = {"name": None, "ts": 0.0}


def get_bot_username():
    """查询 bot 的 @username,缓存 1 小时"""
    now = _time.time()
    if _bot_username_cache["name"] and now - _bot_username_cache["ts"] < 3600:
        return _bot_username_cache["name"]
    token = os.environ.get("BOT_TOKEN", "") or config.BOT_TOKEN
    if not token:
        return None
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(url, timeout=6) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        if j.get("ok"):
            name = j["result"].get("username")
            if name:
                _bot_username_cache["name"] = name
                _bot_username_cache["ts"] = now
                return name
    except Exception as e:
        logger.warning("getMe 失败: %s", e)
    return None


@app.route("/api/auth/bind_status", methods=["GET"])
@login_required
def api_bind_status():
    me = flask_session.get("username", "")
    users = load_users()
    u = users.get(me, {})
    bot_username = get_bot_username()
    if u.get("tg_user_id"):
        return jsonify({
            "ok": True,
            "bound": True,
            "tg_username": u.get("tg_username") or "",
            "tg_user_id": u.get("tg_user_id"),
            "bot_username": bot_username,
            "pending_code": None,
            "pending_expires_in": None,
        })
    # 生成 / 复用 pending 绑定码
    code = auth_reset.create_bind_pending(me)
    pending = auth_reset.load_pending_binds()
    info = pending.get(code, {})
    expires_in = max(0, int(float(info.get("expires_at", 0)) - _time.time()))
    return jsonify({
        "ok": True,
        "bound": False,
        "tg_username": None,
        "bot_username": bot_username,
        "pending_code": code,
        "pending_expires_in": expires_in,
    })


@app.route("/api/auth/unbind", methods=["POST"])
@login_required
def api_unbind():
    me = flask_session.get("username", "")
    if not me:
        return jsonify({"ok": False, "msg": "未登录"}), 401
    ok = auth_reset.unbind_user(me)
    return jsonify({"ok": ok})


@app.route("/api/auth/forgot_password", methods=["POST"])
def api_forgot_password():
    ip = _client_ip()
    # 复用现有 IP 锁定: 如果 IP 已被锁,拒绝
    if _lockout_remaining(ip) > 0:
        return jsonify({"ok": False, "error": "此 IP 请求次数过多,请稍后再试"}), 429
    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "请输入用户名"})
    users = load_users()
    u = users.get(username)
    # 无论账号是否存在都返回 ok: true (防枚举),但实际只在绑定了 TG 时才发码
    if not u or not u.get("tg_user_id"):
        auth_reset.audit_log("reset_request_unbound", {"username": username, "ip": ip,
                                                        "found": bool(u)})
        # 轻微记一次失败,避免无限轰炸(但不触发完全锁定)
        _record_login_attempt(ip, False)
        return jsonify({"ok": True})  # silent
    code = auth_reset.create_reset_pending(username)
    if code is None:
        return jsonify({"ok": False, "error": "请求过于频繁,请 60 秒后再试"})
    token = os.environ.get("BOT_TOKEN", "") or config.BOT_TOKEN
    sent = auth_reset.tg_send_dm(
        token, u["tg_user_id"],
        f"您的密码重置验证码: {code}\n5 分钟有效。如非本人操作请忽略。"
    )
    auth_reset.audit_log("reset_request_sent", {
        "username": username, "ip": ip, "tg_user_id": u["tg_user_id"],
        "dm_sent": sent,
    })
    if not sent:
        return jsonify({"ok": False, "error": "验证码发送失败,请先在 TG 私聊 Bot 发 /start 后重试"})
    return jsonify({"ok": True})


@app.route("/api/auth/reset_password", methods=["POST"])
def api_reset_password():
    ip = _client_ip()
    if _lockout_remaining(ip) > 0:
        return jsonify({"ok": False, "error": "此 IP 请求次数过多,请稍后再试"}), 429
    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    code = (data.get("code") or "").strip()
    new_pwd = data.get("new_password") or ""
    if not username or not code or not new_pwd:
        return jsonify({"ok": False, "error": "参数不完整"})
    if len(new_pwd) < 8:
        return jsonify({"ok": False, "error": "新密码至少 8 位"})
    if not auth_reset.consume_reset_code(code, username):
        _record_login_attempt(ip, False)
        auth_reset.audit_log("reset_fail", {"username": username, "ip": ip})
        return jsonify({"ok": False, "error": "验证码错误或已过期"})
    users = load_users()
    if username not in users:
        auth_reset.audit_log("reset_fail_nouser", {"username": username, "ip": ip})
        return jsonify({"ok": False, "error": "账号不存在"})
    users[username]["password_hash"] = generate_password_hash(new_pwd)
    save_users(users)
    _record_login_attempt(ip, True)  # 清掉此 IP 的失败记录
    auth_reset.audit_log("reset_success", {"username": username, "ip": ip})
    return jsonify({"ok": True})


@app.route("/login", methods=["GET", "POST"])
def login_page():
    ip = _client_ip()
    if request.method == "POST":
        # 防暴力破解：先查锁定
        locked = _lockout_remaining(ip)
        if locked > 0:
            mins = (locked + 59) // 60
            return render_template("login.html",
                                   error=f"此 IP 因多次失败已被锁定,请 {mins} 分钟后再试",
                                   company=config.COMPANY_DISPLAY)
        username = (request.form.get("username") or "").strip()
        pwd = request.form.get("password", "")
        if not username or not pwd:
            _record_login_attempt(ip, False)
            return render_template("login.html", error="账号和密码都必填",
                                   company=config.COMPANY_DISPLAY)
        if verify_user(username, pwd):
            _record_login_attempt(ip, True)
            flask_session["authed"] = True
            flask_session["username"] = username
            return redirect(url_for("index"))
        _record_login_attempt(ip, False)
        remaining_fails = BRUTE_THRESHOLD - len(_login_fails.get(ip, []))
        if remaining_fails <= 0:
            locked = _lockout_remaining(ip)
            mins = (locked + 59) // 60
            msg = f"失败次数过多,IP 已锁定 {mins} 分钟"
        elif remaining_fails <= 2:
            msg = f"账号或密码错误(再错 {remaining_fails} 次将锁定此 IP 15 分钟)"
        else:
            msg = "账号或密码错误"
        return render_template("login.html", error=msg, company=config.COMPANY_DISPLAY)
    # GET
    locked = _lockout_remaining(ip)
    if locked > 0:
        mins = (locked + 59) // 60
        return render_template("login.html",
                               error=f"此 IP 已被锁定,请 {mins} 分钟后再试",
                               company=config.COMPANY_DISPLAY)
    reset_msg = "密码已重置,请用新密码登入" if request.args.get("reset") == "1" else None
    return render_template("login.html", error=None, reset_msg=reset_msg, company=config.COMPANY_DISPLAY)


@app.route("/logout")
def logout():
    flask_session.pop("authed", None)
    flask_session.pop("username", None)
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def index():
    db.init_db()
    sessions = get_sessions()
    return render_template(
        "index.html",
        sessions=sessions,
        company=config.COMPANY_DISPLAY,
        alerts_enabled=config.ALERTS_ENABLED,
        # v2.6.6: 三个独立子开关传给 dashboard
        alert_keyword_enabled=config.ALERT_KEYWORD_ENABLED,
        alert_no_reply_enabled=config.ALERT_NO_REPLY_ENABLED,
        alert_delete_enabled=config.ALERT_DELETE_ENABLED,
        operator_label=config.OPERATOR_LABEL,
    )


# v2.6.2: 预警推送总开关 — 热切换,无需重启容器
# v2.6.6: 改成「全开/全关」一键操作 — 同时显式写入 3 个独立子开关,避免 fallback 歧义
@app.route("/api/alerts/toggle", methods=["POST"])
@login_required
def toggle_alerts():
    """一键开关三类预警推送(关键词 + 未回复 + 删除)。
    会同时把 ALERTS_ENABLED 和 3 个独立子开关都写成同一个值。"""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    val = "true" if enabled else "false"
    try:
        write_env({
            "ALERTS_ENABLED": val,
            "ALERT_KEYWORD_ENABLED": val,
            "ALERT_NO_REPLY_ENABLED": val,
            "ALERT_DELETE_ENABLED": val,
        })
        import importlib
        importlib.reload(config)
        logger.info("ALERTS_ENABLED + 3 子开关 已切换 → %s", enabled)
        return jsonify({
            "ok": True,
            "enabled": config.ALERTS_ENABLED,
            "alert_keyword_enabled": config.ALERT_KEYWORD_ENABLED,
            "alert_no_reply_enabled": config.ALERT_NO_REPLY_ENABLED,
            "alert_delete_enabled": config.ALERT_DELETE_ENABLED,
        })
    except Exception as e:
        logger.error("切换 ALERTS_ENABLED 失败: %s", e)
        return jsonify({"ok": False, "msg": str(e)})


# v2.6.6: 三个独立预警子开关 — 关键词 / 未回复 / 删除
_ALERT_SUBSWITCH_KEYS = {
    "keyword":  "ALERT_KEYWORD_ENABLED",
    "no_reply": "ALERT_NO_REPLY_ENABLED",
    "delete":   "ALERT_DELETE_ENABLED",
}


@app.route("/api/alerts/subswitch/toggle", methods=["POST"])
@login_required
def toggle_alert_subswitch():
    """切换单个预警子开关。body = {"type": "keyword|no_reply|delete", "enabled": bool}"""
    data = request.get_json(silent=True) or {}
    sub_type = (data.get("type") or "").strip().lower()
    enabled = bool(data.get("enabled"))
    env_key = _ALERT_SUBSWITCH_KEYS.get(sub_type)
    if not env_key:
        return jsonify({"ok": False, "msg": f"未知预警类型: {sub_type}"})
    try:
        write_env({env_key: "true" if enabled else "false"})
        import importlib
        importlib.reload(config)
        logger.info("%s 已切换 → %s", env_key, enabled)
        return jsonify({
            "ok": True,
            "type": sub_type,
            "enabled": getattr(config, env_key),
        })
    except Exception as e:
        logger.error("切换 %s 失败: %s", env_key, e)
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/alerts/status", methods=["GET"])
@login_required
def alerts_status():
    """当前预警开关状态(给其他页面查询用)"""
    return jsonify({
        "alerts_enabled": config.ALERTS_ENABLED,
        "alert_keyword_enabled": config.ALERT_KEYWORD_ENABLED,
        "alert_no_reply_enabled": config.ALERT_NO_REPLY_ENABLED,
        "alert_delete_enabled": config.ALERT_DELETE_ENABLED,
        "daily_report_enabled": config.DAILY_REPORT_ENABLED,
    })


# v2.6.2: 日报独立开关
@app.route("/api/daily-report/toggle", methods=["POST"])
@login_required
def toggle_daily_report():
    """切换每日零点日报推送开关"""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    try:
        write_env({"DAILY_REPORT_ENABLED": "true" if enabled else "false"})
        import importlib
        importlib.reload(config)
        logger.info("DAILY_REPORT_ENABLED 已切换 → %s", enabled)
        return jsonify({"ok": True, "enabled": config.DAILY_REPORT_ENABLED})
    except Exception as e:
        logger.error("切换 DAILY_REPORT_ENABLED 失败: %s", e)
        return jsonify({"ok": False, "msg": str(e)})


def _humanize_tg_error(e):
    """v2.10.19: 把 Telethon 原始异常翻成客户能看懂的白话。
    之前 send-code / verify-code / verify-password 三个路由的 except 都是
    `str(e)` 直接吐 Telethon 原文 (例如 "The password (and thus its hash value)
    you entered is invalid (caused by CheckPasswordRequest)"), 客户看不懂。"""
    if isinstance(e, PasswordHashInvalidError):
        return "两步验证密码错误,请重新输入(区分大小写;跟 Telegram 客户端设置里的密码要一致)"
    if isinstance(e, PhoneCodeInvalidError):
        return "验证码错误,请重新输入(别漏数字或复制多空格)"
    if isinstance(e, PhoneCodeExpiredError):
        return "验证码已过期,请点「发送验证码」重新获取"
    if isinstance(e, PhoneCodeEmptyError):
        return "请输入验证码"
    if isinstance(e, PhoneNumberInvalidError):
        return "手机号格式错误,请加国家区号 + 号码(例如 +85512345678)"
    if isinstance(e, PhoneNumberBannedError):
        return "此手机号已被 Telegram 封禁,无法登录"
    if isinstance(e, (PhoneNumberFloodError, FloodWaitError)):
        seconds = getattr(e, "seconds", 0) or 0
        if seconds >= 3600:
            return f"请求太频繁,Telegram 要求等约 {seconds // 3600} 小时后再试"
        if seconds >= 60:
            return f"请求太频繁,Telegram 要求等约 {seconds // 60} 分钟后再试"
        if seconds:
            return f"请求太频繁,请等 {seconds} 秒后再试"
        return "请求太频繁,请稍后再试"
    # 兜底: 保留原始 message 但剥掉 "(caused by XxxRequest)" 技术尾巴
    import re as _re
    msg = str(e) or type(e).__name__
    msg = _re.sub(r"\s*\(caused by \w+\)", "", msg).strip()
    return msg


@app.route("/api/send-code", methods=["POST"])
@login_required
def send_code():
    """第一步：发送验证码"""
    phone = request.json.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "请输入手机号"})
    if not phone.startswith("+"):
        phone = "+" + phone

    try:
        session_path = str(config.SESSION_DIR / phone.replace("+", ""))
        client = run_async(_make_client(session_path))
        run_async(_connect(client))

        result = run_async(_send_code_req(client, phone))
        _pending[phone] = {
            "client": client,
            "phone_hash": result.phone_code_hash,
        }
        return jsonify({"ok": True, "phone": phone})
    except Exception as e:
        return jsonify({"ok": False, "error": _humanize_tg_error(e)})


@app.route("/api/verify-code", methods=["POST"])
@login_required
def verify_code():
    """第二步：验证码确认"""
    phone = request.json.get("phone", "").strip()
    code = request.json.get("code", "").strip()

    if phone not in _pending:
        return jsonify({"ok": False, "error": "请先发送验证码"})

    client = _pending[phone]["client"]
    phone_hash = _pending[phone]["phone_hash"]

    try:
        me = run_async(_sign_in_code(client, phone, code, phone_hash))

        tg_name = ((me.first_name or "") + " " + (me.last_name or "")).strip()

        # 存入数据库
        db.init_db()
        db.upsert_account(phone=phone, name=tg_name, username=me.username or "", tg_id=me.id)

        # 自动建 Sheets 分页
        _create_sheet_tab(tg_name)

        run_async(_disconnect(client))
        del _pending[phone]

        # v2.10.7: 登录成功 → session_states 立刻标 healthy(UI 不再显示吊销)+ 推恢复通知
        try:
            prev_status, _ = _mark_session_healthy(phone)
            if prev_status == "revoked":
                _push_session_restored(phone, tg_name)
        except Exception as e:
            print(f"[verify_code] session heal 失败: {e}")

        # v2.6.3: 帐号添加成功 → 后台自动重启监听(debounce 4 秒,批量加号只触发一次)
        _schedule_listener_restart()

        return jsonify({
            "ok": True,
            "name": me.first_name or "",
            "username": me.username or "",
            "auto_restart": True,
        })
    except SessionPasswordNeededError:
        _pending[phone]["need_password"] = True
        return jsonify({"ok": False, "need_password": True, "error": "此账号有两步验证，请输入密码"})
    except Exception as e:
        return jsonify({"ok": False, "error": _humanize_tg_error(e)})


@app.route("/api/verify-password", methods=["POST"])
@login_required
def verify_password():
    """两步验证密码"""
    phone = request.json.get("phone", "").strip()
    password = request.json.get("password", "").strip()

    if phone not in _pending:
        return jsonify({"ok": False, "error": "请先发送验证码"})

    client = _pending[phone]["client"]
    try:
        me = run_async(_sign_in_password(client, password))

        tg_name = ((me.first_name or "") + " " + (me.last_name or "")).strip()

        db.init_db()
        db.upsert_account(phone=phone, name=tg_name, username=me.username or "", tg_id=me.id)

        # 自动建 Sheets 分页
        _create_sheet_tab(tg_name)

        run_async(_disconnect(client))
        del _pending[phone]

        # v2.10.7: 同上 — session_states 标 healthy + 推恢复通知
        try:
            prev_status, _ = _mark_session_healthy(phone)
            if prev_status == "revoked":
                _push_session_restored(phone, tg_name)
        except Exception as e:
            print(f"[verify_password] session heal 失败: {e}")

        # v2.6.3: 同上,二步验证通过后也触发自动重启
        _schedule_listener_restart()

        return jsonify({
            "ok": True,
            "name": me.first_name or "",
            "username": me.username or "",
            "auto_restart": True,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": _humanize_tg_error(e)})


@app.route("/api/dedup/today", methods=["GET"])
@login_required
def api_dedup_today():
    """查看今日已推送的预警记录(即每日去重状态)

    alerts 表 = 去重来源。has_alert_today() 通过查当日 alerts 记录判断是否已推过。
    客户修改关键词后如果想立刻对已聊过的广告主重测,需要先清掉今日对应记录。

    返回格式:
        {
            "date": "2026-04-15",
            "total": 12,
            "by_type": {"keyword": 8, "no_reply": 3, "deleted": 1},
            "entries": [
                {"type": "keyword", "peer_name": "王总(广告主A)", "account_name": "外事号1",
                 "message_text": "这个月返点给多少...", "created_at": "2026-04-15 10:23:11"},
                ...
            ]
        }
    """
    from datetime import datetime as _dt
    today = _dt.now(db.TZ_BJ).strftime("%Y-%m-%d")
    rows = db.get_conn().execute(
        "SELECT a.type, a.message_text, a.created_at, "
        "       COALESCE(p.name, '(未知客户)') AS peer_name, "
        "       COALESCE(ac.name, '(未知账号)') AS account_name "
        "FROM alerts a "
        "LEFT JOIN peers p ON a.peer_id = p.id "
        "LEFT JOIN accounts ac ON a.account_id = ac.id "
        "WHERE a.created_at LIKE ? "
        "ORDER BY a.created_at DESC",
        (f"{today}%",)
    ).fetchall()

    by_type = {"keyword": 0, "no_reply": 0, "deleted": 0}
    entries = []
    for r in rows:
        t = r["type"] or "unknown"
        by_type[t] = by_type.get(t, 0) + 1
        entries.append({
            "type": t,
            "peer_name": r["peer_name"],
            "account_name": r["account_name"],
            "message_text": (r["message_text"] or "")[:120],
            "created_at": r["created_at"],
        })

    return jsonify({
        "ok": True,
        "date": today,
        "total": len(entries),
        "by_type": by_type,
        "entries": entries[:50],  # 最多回 50 条避免过大
    })


@app.route("/api/dedup/clear", methods=["POST"])
@login_required
def api_dedup_clear():
    """清空今日指定类型的去重记录,让相同关键词/客户可以重新触发预警

    入参: { "type": "keyword" | "no_reply" | "deleted" | "all" }
    仅管理员可调用,避免普通用户误操作把主管的告警清光。
    """
    me = flask_session.get("username", "")
    if not is_admin(me):
        return jsonify({"ok": False, "msg": "只有管理员可清空去重"}), 403

    data = request.get_json(silent=True) or {}
    alert_type = (data.get("type") or "").strip().lower()
    if alert_type not in ("keyword", "no_reply", "deleted", "all"):
        return jsonify({"ok": False, "msg": "type 必须是 keyword / no_reply / deleted / all 其中之一"})

    from datetime import datetime as _dt
    today = _dt.now(db.TZ_BJ).strftime("%Y-%m-%d")
    conn = db.get_conn()
    if alert_type == "all":
        cur = conn.execute(
            "DELETE FROM alerts WHERE created_at LIKE ?",
            (f"{today}%",)
        )
    else:
        cur = conn.execute(
            "DELETE FROM alerts WHERE type=? AND created_at LIKE ?",
            (alert_type, f"{today}%")
        )
    deleted = cur.rowcount
    conn.commit()

    return jsonify({
        "ok": True,
        "msg": f"已清空今日 {alert_type} 类型 {deleted} 条去重记录,相关客户今天可以重新触发该类预警",
        "deleted": deleted,
        "type": alert_type,
    })


@app.route("/api/restart", methods=["POST"])
@login_required
def restart_monitor():
    """重启监控容器，加载新 session (via docker.sock)"""
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        container_name = "tg-monitor-" + config.COMPANY_NAME
        container = client.containers.get(container_name)
        container.restart(timeout=10)
        return jsonify({"ok": True, "msg": "监控已重启，新账号将自动开始监听"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/remove", methods=["POST"])
@login_required
def remove_session():
    """完整删除账号：session + DB 记录（peers/messages/alerts 级联）+ Sheets 分页"""
    phone = request.json.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "缺少手机号"})

    # 1. 删 session 文件
    session_file = config.SESSION_DIR / (phone.replace("+", "") + ".session")
    if session_file.exists():
        session_file.unlink()
        journal = session_file.with_suffix(".session-journal")
        if journal.exists():
            journal.unlink()

    # 2. 清 DB + 删 Sheets 分页
    account = db.get_account_by_phone(phone)
    deleted_db = False
    deleted_sheet = False
    if account:
        acc_id = account["id"]
        tab = account["sheet_tab"] or account["name"]
        conn = db.get_conn()
        # 级联清：alerts → messages → peers → accounts
        conn.execute("DELETE FROM alerts WHERE account_id=?", (acc_id,))
        conn.execute("DELETE FROM messages WHERE account_id=?", (acc_id,))
        conn.execute("DELETE FROM peers WHERE account_id=?", (acc_id,))
        conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        conn.commit()
        deleted_db = True

        # 3. 删 Sheets 分页（失败不影响主流程）— 用 OAuth 凭证
        if tab:
            try:
                import gspread
                import oauth_helper
                creds = oauth_helper.get_credentials()
                if not creds:
                    raise RuntimeError("OAuth 未授权,跳过删除 Sheets 分页")
                gc = gspread.authorize(creds)
                sh = gc.open_by_key(config.SHEET_ID)
                ws = sh.worksheet(tab)
                sh.del_worksheet(ws)
                deleted_sheet = True
            except Exception as e:
                print(f"删 Sheets 分页失败（可忽略）: {e}")

    # v2.6.3: 删除帐号也触发自动重启,避免 listener 还在监听已删 session
    _schedule_listener_restart()

    return jsonify({
        "ok": True,
        "deleted_db": deleted_db,
        "deleted_sheet": deleted_sheet,
        "auto_restart": True,
    })


# ===================================================================
# v2.8.0 — 中央台接入:只读 metrics API (Bearer token 鉴权)
# ===================================================================
import secrets as _secrets
from datetime import datetime as _dt, timedelta as _td

_METRICS_ACCESS_LOG_PATH = config.DATA_DIR / "metrics_access.log"
_METRICS_LOG_MAX = 200


def _metrics_log_access(ok: bool, reason: str = ""):
    """记录一次 metrics API 访问 — 保留最近 200 条"""
    try:
        _METRICS_ACCESS_LOG_PATH.parent.mkdir(exist_ok=True)
        entries = []
        if _METRICS_ACCESS_LOG_PATH.exists():
            try:
                entries = json.loads(_METRICS_ACCESS_LOG_PATH.read_text(encoding="utf-8"))
                if not isinstance(entries, list):
                    entries = []
            except Exception:
                entries = []
        entries.append({
            "ts": _dt.utcnow().isoformat() + "Z",
            "ip": request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            "ok": bool(ok),
            "reason": reason or ("ok" if ok else "unauthorized"),
        })
        entries = entries[-_METRICS_LOG_MAX:]
        _METRICS_ACCESS_LOG_PATH.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"metrics access log write failed: {e}")


def _metrics_load_log():
    try:
        if not _METRICS_ACCESS_LOG_PATH.exists():
            return []
        return json.loads(_METRICS_ACCESS_LOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _metrics_access_count(hours: int = 24) -> int:
    entries = _metrics_load_log()
    cutoff = _dt.utcnow() - _td(hours=hours)
    count = 0
    for e in entries:
        if not e.get("ok"):
            continue
        try:
            ts = _dt.fromisoformat(e["ts"].rstrip("Z"))
            if ts >= cutoff:
                count += 1
        except Exception:
            pass
    return count


def _metrics_last_access():
    entries = [e for e in _metrics_load_log() if e.get("ok")]
    if not entries:
        return ""
    return entries[-1].get("ts", "")


def _gen_metrics_token() -> str:
    return _secrets.token_hex(24)


def _ensure_metrics_token(env: dict = None) -> str:
    """确保 METRICS_TOKEN 存在 — 空就生成并写回 .env。
    任何读 token 的地方都走这个,update.sh 忘补或 .env 手改丢了都兜得住。
    """
    if env is None:
        env = read_env()
    token = (env.get("METRICS_TOKEN") or "").strip()
    if token:
        return token
    new_token = _gen_metrics_token()
    try:
        write_env({"METRICS_TOKEN": new_token})
        logger.info("METRICS_TOKEN was missing — auto-generated and persisted")
    except Exception as e:
        logger.warning(f"METRICS_TOKEN auto-gen persist failed: {e}")
    return new_token


@app.route("/api/v1/metrics", methods=["GET"])
def api_v1_metrics():
    """中央台拉数据接口 — Bearer token 鉴权,返回 dashboard_api.snapshot()"""
    env = read_env()
    token = _ensure_metrics_token(env)
    auth = request.headers.get("Authorization", "")
    provided = ""
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    else:
        provided = request.args.get("token", "").strip()

    if not provided or not _secrets.compare_digest(provided, token):
        _metrics_log_access(False, "unauthorized")
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    try:
        import dashboard_api, re
        data = dashboard_api.snapshot()
        data["company_name"] = env.get("COMPANY_NAME", "")
        data["company_display"] = env.get("COMPANY_DISPLAY", "")
        _metrics_log_access(True, "ok")
        return jsonify(data)
    except Exception as e:
        logger.exception("metrics snapshot failed")
        _metrics_log_access(False, f"snapshot_error:{e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/v1/metrics/access_log", methods=["GET"])
@login_required
def api_v1_metrics_access_log():
    """设置页面用 — 展示 metrics API 最近调用记录"""
    entries = _metrics_load_log()
    return jsonify({
        "ok": True,
        "count_24h": _metrics_access_count(24),
        "last_access": _metrics_last_access(),
        "recent": entries[-20:][::-1],
    })


@app.route("/api/settings/metrics_token/regenerate", methods=["POST"])
@login_required
def api_metrics_token_regenerate():
    """重置 METRICS_TOKEN — 仅管理员 + 需再次输入密码确认"""
    me = flask_session.get("username", "")
    if not is_admin(me):
        return jsonify({"ok": False, "error": "仅管理员可重置"}), 403
    data = request.get_json(silent=True) or request.form
    password = (data.get("password") or "").strip()
    if not password or not verify_user(me, password):
        return jsonify({"ok": False, "error": "密码错误"}), 401

    new_token = _gen_metrics_token()
    write_env({"METRICS_TOKEN": new_token})
    logger.info(f"METRICS_TOKEN regenerated by {me}")
    return jsonify({"ok": True, "token": new_token})


# ===================================================================
# v2.9.0 — 版本更新检查 (Dashboard 横幅 + 手动触发)
# ===================================================================
@app.route("/api/update/status", methods=["GET"])
@login_required
def api_update_status():
    """Dashboard 读这个拿横幅数据"""
    try:
        import update_checker
        state = update_checker.load_state()
        env = read_env()
        company = env.get("COMPANY_NAME", "")
        state["company_name"] = company
        import upgrader as _up; state["upgrade_cmd"] = _up.build_upgrade_cmd(company)
        return jsonify({"ok": True, "state": state})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/update/check_now", methods=["POST"])
@login_required
def api_update_check_now():
    """手动触发一次 check(不推 TG,只刷状态)— Dashboard 右上角「检查更新」按钮调"""
    try:
        import update_checker
        has_update, state = update_checker.check_once()
        env = read_env()
        company = env.get("COMPANY_NAME", "")
        state["company_name"] = company
        import upgrader as _up; state["upgrade_cmd"] = _up.build_upgrade_cmd(company)
        return jsonify({"ok": True, "has_update": has_update, "state": state})
    except Exception as e:
        logger.exception("update check_now failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/update/soft_upgrade", methods=["POST"])
@login_required
def api_update_soft_upgrade():
    """一键软升级 — 拉 tarball 覆盖代码 + 重启容器.
    如果 Dockerfile/requirements.txt 改了会返回 need_rebuild=True,让前端显示 SSH 命令."""
    me = flask_session.get("username", "")
    if not is_admin(me):
        return jsonify({"ok": False, "error": "仅管理员可执行升级"}), 403
    try:
        import upgrader
        env = read_env()
        company = env.get("COMPANY_NAME", "") or "demo"
        result = upgrader.start_soft_upgrade(company)
        return jsonify(result)
    except Exception as e:
        logger.exception("soft upgrade start failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/update/upgrade_status", methods=["GET"])
@login_required
def api_update_upgrade_status():
    try:
        import upgrader
        return jsonify({"ok": True, "state": upgrader.load_state()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



# ============ 驾驶舱 (demo 预览,暂不 push GIT) ============
@app.route("/dashboard")
@login_required
def dashboard_page():
    db.init_db()
    return render_template(
        "dashboard.html",
        company=config.COMPANY_DISPLAY,
        operator_label=config.OPERATOR_LABEL,
        peer_role_label=config.PEER_ROLE_LABEL,
    )


@app.route("/api/dashboard/snapshot", methods=["GET"])
@login_required
def dashboard_snapshot():
    try:
        import dashboard_api, re
        return jsonify(dashboard_api.snapshot())
    except Exception as e:
        logger.error("dashboard snapshot 失败: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    db.init_db()
    print("🌐 登录管理介面启动: http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
