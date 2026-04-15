"""Web 登录管理介面 — 账号登录 + 启用/停用 + 首次设置 + 设置修改"""
import asyncio
import json
import threading
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from flask import Flask, render_template, request, jsonify, redirect, url_for
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

import os
from functools import wraps
from flask import session as flask_session

import config
import database as db
import gspread
from google.oauth2.service_account import Credentials
from werkzeug.security import generate_password_hash, check_password_hash

# ============ 设置文件读写 ============
ENV_PATH = config.BASE_DIR / ".env"
SA_PATH = config.BASE_DIR / "service-account.json"
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
        "COMPANY_NAME", "COMPANY_DISPLAY", "PEER_ROLE_LABEL",
        "SHEET_ID", "MEDIA_FOLDER_ID", "MEDIA_RETENTION_DAYS", "MEDIA_MAX_MB",
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
        "BOT_TOKEN", "ALERT_GROUP_ID",
        "WEB_PORT", "WEB_PASSWORD",
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
    """是否已完成首次设置。条件：关键字段都有值 + SETUP_COMPLETE=true + service-account.json 存在 + 至少 1 个用户"""
    env = read_env()
    required = ["COMPANY_NAME", "SHEET_ID", "BOT_TOKEN", "ALERT_GROUP_ID"]
    for k in required:
        if not env.get(k) or env.get(k) in ("__pending__", "0"):
            return False
    if env.get("SETUP_COMPLETE", "false").lower() != "true":
        return False
    if not SA_PATH.exists() or SA_PATH.stat().st_size < 100:
        return False
    # 旧部署自动迁移
    migrate_legacy_password()
    if not load_users():
        return False
    return True


def _test_bot_api(token, group_id):
    """用 Bot API 发一则测试消息，验证 token 有效 + bot 在群里 + 是管理员"""
    token = (token or "").strip()
    group_id = (group_id or "").strip()
    if not token or ":" not in token:
        return False, "Bot Token 格式错误"
    if not group_id:
        return False, "群 ID 不能为空"
    try:
        data = urllib.parse.urlencode({
            "chat_id": group_id,
            "text": "✅ TG 监控 — 连线测试成功！设置完成后将开始接收预警。",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            return True, "Bot 已发送测试消息到群组"
        return False, result.get("description", "未知错误")
    except Exception as e:
        return False, str(e)


def _test_sheets_access(sheet_id, sa_json_bytes):
    """验证 Google Sheets 访问权限。sa_json_bytes 是上传的服务账号 json 原始字节"""
    sheet_id = (sheet_id or "").strip()
    if not sheet_id:
        return False, "Sheet ID 不能为空"
    try:
        sa_data = json.loads(sa_json_bytes)
        if "client_email" not in sa_data or "private_key" not in sa_data:
            return False, "service-account.json 不是有效的服务账号凭证"
    except Exception:
        return False, "service-account.json 不是合法的 JSON"
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(sa_data, tmp)
        tmp.close()
        creds = Credentials.from_service_account_file(
            tmp.name,
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"],
        )
        gc = gspread.authorize(creds)
        sp = gc.open_by_key(sheet_id)
        return True, f"成功访问 Sheet: {sp.title}（服务账号 {sa_data['client_email']}）"
    except gspread.exceptions.SpreadsheetNotFound:
        return False, (f"Sheet ID「{sheet_id}」找不到或没访问权限。"
                       f"请确认：① ID 正确；② service account「{sa_data.get('client_email','?')}」已加为该 Sheet 的编辑者")
    except gspread.exceptions.APIError as e:
        body = getattr(e, "args", [""])[0] or str(e) or repr(e)
        return False, f"Sheet API 错误: {body}"
    except Exception as e:
        err = str(e) or repr(e) or type(e).__name__
        return False, f"{type(e).__name__}: {err}"
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


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


def _detect_public_ip():
    """检测 VPS 公网 IP — 多源 fallback"""
    for url in ("https://api.ipify.org", "https://ifconfig.me", "https://ipinfo.io/ip"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                # 简单校验是 IPv4
                parts = ip.split(".")
                if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                    return ip
        except Exception:
            continue
    return ""


def _https_status():
    """检查 Caddy 容器是否在跑 + 当前 PUBLIC_DOMAIN"""
    env = read_env()
    domain = env.get("PUBLIC_DOMAIN", "").strip()
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        for c in client.containers.list(all=True):
            if c.name.startswith("tg-caddy-"):
                running = c.status == "running"
                return {
                    "enabled": running,
                    "domain": domain,
                    "container": c.name,
                    "status": c.status,
                }
    except Exception as e:
        return {"enabled": False, "domain": domain, "error": str(e)}
    return {"enabled": False, "domain": domain}


def _check_ports_busy(client, ports):
    """检查 host 上的端口是否被占用 — 用 host network 跑 alpine ss。
    返回被占用的端口列表(空表示全部空闲)。"""
    try:
        out = client.containers.run(
            "alpine:3.20",
            command=["sh", "-c", "ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null"],
            network_mode="host",
            remove=True,
            detach=False,
            stdout=True,
            stderr=False,
        )
        text = out.decode("utf-8", "ignore") if isinstance(out, (bytes, bytearray)) else str(out)
        busy = []
        for p in ports:
            # 匹配 :80 / :443 在行首端 (LISTEN 行的本地 address 末尾)
            if f":{p} " in text or f":{p}\n" in text or text.endswith(f":{p}"):
                busy.append(p)
        return busy
    except Exception as e:
        logger.warning("端口预检失败,跳过: %s", e)
        return []


def _enable_https_now(custom_domain=""):
    """一键启用 HTTPS — 在 docker socket 上启 Caddy 容器,自动 Let's Encrypt 签证书

    流程:
    1. 决定域名:custom_domain 优先,否则自动检测 IP → nip.io
    2. 写 PUBLIC_DOMAIN 到 .env
    3. 找 web 容器的 host 挂载路径(给 Caddy 挂 Caddyfile)+ 网络名(让 Caddy 能 reverse_proxy web:5001)
    4. 删旧 caddy 容器(如果有)
    5. 启 caddy 容器,挂 Caddyfile + named volumes
    6. 等几秒返回新域名给前端
    """
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()

        # 0. 端口预检 — VPS 80/443 必须空闲(Let's Encrypt HTTP-01/TLS-ALPN 用)
        # 如果已被占用,要么是别的 Caddy/Nginx 在跑,要么是系统服务,Docker 启不了
        busy = _check_ports_busy(client, [80, 443])
        if busy:
            return False, (
                f"VPS 端口 {busy} 已被占用,Caddy 启不了。\n"
                "请在 VPS 上跑 `ss -ltnp | grep -E ':(80|443)'` 找出占用的进程,"
                "停掉它再试;或者改用已有的反代(把 tg-web-* 的 5001 端口反代到一个域名上)。"
            )

        # 1. 域名
        domain = (custom_domain or "").strip()
        if not domain:
            ip = _detect_public_ip()
            if not ip:
                return False, "无法自动获取公网 IP,请手动指定域名"
            domain = f"{ip.replace('.', '-')}.nip.io"

        company = read_env().get("COMPANY_NAME", "default")
        caddy_name = f"tg-caddy-{company}"
        web_name = f"tg-web-{company}"

        # 2. 找 web 容器,拿到它的网络名 + host 挂载路径
        try:
            web_c = client.containers.get(web_name)
        except docker_sdk.errors.NotFound:
            # 兜底:找任何 tg-web- 开头的
            web_c = None
            for c in client.containers.list(all=True):
                if c.name.startswith("tg-web-"):
                    web_c = c
                    break
            if not web_c:
                return False, "找不到 web 容器,请先 docker compose up -d"

        # 网络名(取第一个非 bridge 的)
        nets = list(web_c.attrs["NetworkSettings"]["Networks"].keys())
        web_net = next((n for n in nets if n != "bridge"), None) or (nets[0] if nets else None)
        if not web_net:
            return False, "找不到 web 容器的网络"

        # web 容器在自己网络里的 service 别名(docker compose 会注册 service name 当 alias)
        web_alias = "web"
        net_info = web_c.attrs["NetworkSettings"]["Networks"].get(web_net, {})
        aliases = net_info.get("Aliases", []) or []
        # 优先用 web alias,否则用容器名
        upstream = "web" if "web" in aliases else web_c.name

        # host 上的项目目录(找 /app/repo 或 /app 的 Source)
        host_dir = None
        for m in web_c.attrs.get("Mounts", []):
            if m.get("Destination") == "/app/repo":
                host_dir = m.get("Source")
                break
        if not host_dir:
            # 退路:找任何 destination 是 /app/.env 的,取它的 dirname
            for m in web_c.attrs.get("Mounts", []):
                if m.get("Destination") == "/app/.env":
                    host_dir = str(Path(m.get("Source", "")).parent)
                    break
        if not host_dir:
            return False, "找不到项目目录的 host 路径,请确认 docker-compose.yml 有挂 .:/app/repo"

        caddyfile_host = f"{host_dir}/Caddyfile"

        # 3. 写 .env
        write_env({"PUBLIC_DOMAIN": domain})

        # 4. 删旧 caddy
        try:
            old = client.containers.get(caddy_name)
            old.stop(timeout=5)
            old.remove(force=True)
        except docker_sdk.errors.NotFound:
            pass

        # 5. 准备 named volumes
        for vol_name in (f"{caddy_name}_data", f"{caddy_name}_config"):
            try:
                client.volumes.get(vol_name)
            except docker_sdk.errors.NotFound:
                client.volumes.create(name=vol_name)

        # 6. 启动 caddy
        # Caddyfile 用 {$PUBLIC_DOMAIN} 占位,这里通过 env 传入;upstream 也通过 env 传(改 Caddyfile 兼容)
        caddy = client.containers.run(
            "caddy:2-alpine",
            name=caddy_name,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            ports={"80/tcp": 80, "443/tcp": 443},
            volumes={
                caddyfile_host: {"bind": "/etc/caddy/Caddyfile", "mode": "ro"},
                f"{caddy_name}_data": {"bind": "/data", "mode": "rw"},
                f"{caddy_name}_config": {"bind": "/config", "mode": "rw"},
            },
            environment={
                "PUBLIC_DOMAIN": domain,
                "WEB_UPSTREAM": f"{upstream}:5001",
                "TZ": "Asia/Shanghai",
            },
            network=web_net,
        )

        return True, {
            "domain": domain,
            "container": caddy_name,
            "redirect_uri": f"https://{domain}/api/oauth/callback",
            "js_origin": f"https://{domain}",
            "settings_url": f"https://{domain}/settings",
        }

    except Exception as e:
        import traceback
        return False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}"


def _get_spreadsheet():
    """获取 Google Sheets 连接"""
    creds = Credentials.from_service_account_file(
        str(config.SERVICE_ACCOUNT_FILE),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(config.SHEET_ID)


def _create_sheet_tab(name, operator="", company=""):
    """登录成功后自动建分页（格式与舒舒一致：全部置中 + 斑马纹 + 冻结 6 行）

    name: 外事号 TG 昵称 (会写到 row 5 col C)
    operator: 商务人员 (会写到 B2；空就留白等用户自己填)
    company: 所属中心/部门 (会写到 B3；空就默认用 .env 的 COMPANY_DISPLAY)
    """
    try:
        sp = _get_spreadsheet()
        existing = [ws.title for ws in sp.worksheets()]
        if name in existing:
            return

        # company 默认读 env COMPANY_DISPLAY
        if not company:
            company = read_env().get("COMPANY_DISPLAY") or read_env().get("COMPANY_NAME") or ""

        TOTAL_ROWS = 1000
        TOTAL_COLS = 30
        ws = sp.add_worksheet(title=name, rows=TOTAL_ROWS, cols=TOTAL_COLS)
        sheet_id = ws.id

        # 颜色常量
        CYAN = {"red": 0.3019608, "green": 0.8156863, "blue": 0.88235295}
        WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
        LIGHT_BLUE = {"red": 0.8784314, "green": 0.96862745, "blue": 0.98039216}
        TEAL = {"red": 0.29803923, "green": 0.69803923, "blue": 0.69803923}

        # 文字内容: label A2/A3 + value B2/B3 + 对话槽标题 row5-6
        ws.update("A2:B3", [
            ["商务人员", operator],
            ["中心/部门", company],
        ])
        # C6 留空（第一条消息进来时 setup_dialog_columns 会填真实 peer 名）
        # 之前预填「（等消息进来自动填）」会让 sheets.py 的空检查失效，导致 B6 永远不同步
        ws.update("A5:C6", [
            ["A", "外事号", name],
            ["B", config.PEER_ROLE_LABEL, ""],
        ])

        center_middle = {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}

        def _repeat(r0, r1, c0, c1, fmt):
            return {"repeatCell": {
                "range": {"sheetId": sheet_id,
                          "startRowIndex": r0, "endRowIndex": r1,
                          "startColumnIndex": c0, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": fmt},
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
            }}

        def _col_dim(c0, c1, size):
            return {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": c0, "endIndex": c1},
                "properties": {"pixelSize": size}, "fields": "pixelSize",
            }}

        # 整张表全部置中 (row 1..TOTAL_ROWS, col 1..TOTAL_COLS)
        requests = [
            _repeat(0, TOTAL_ROWS, 0, TOTAL_COLS, {"backgroundColor": WHITE, **center_middle}),
            # Row 1: 青色横条 (全宽)
            _repeat(0, 1, 0, TOTAL_COLS, {"backgroundColor": CYAN, **center_middle}),
            # Row 2: 白底 + 置中 (全宽)
            _repeat(1, 2, 0, TOTAL_COLS, {"backgroundColor": WHITE, **center_middle}),
            # Row 3: 淡蓝底 + 置中 (全宽)
            _repeat(2, 3, 0, TOTAL_COLS, {"backgroundColor": LIGHT_BLUE, **center_middle}),
            # Row 4: 白底 + 白字 + 粗体 + 置中 (spacer, 全宽)
            _repeat(3, 4, 0, TOTAL_COLS, {
                "backgroundColor": WHITE,
                "textFormat": {"bold": True, "foregroundColor": WHITE},
                **center_middle,
            }),
            # Row 5-6 第一个对话槽 A-C: 青绿 + 粗体 + 置中
            _repeat(4, 6, 0, 3, {
                "backgroundColor": TEAL,
                "textFormat": {"bold": True},
                **center_middle,
            }),
            # 冻结前 6 行
            {"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 6}},
                "fields": "gridProperties.frozenRowCount",
            }},
            # 列宽: A=180, B=192, C=350 (与舒舒一致)
            _col_dim(0, 1, 180),
            _col_dim(1, 2, 192),
            _col_dim(2, 3, 350),
            # 斑马纹: row 7+ 全部 30 列统一双色（淡蓝 / 白交替），一次铺满
            # 每 3 列一个对话槽独立 banding，之后新 peer 进来的 slot 视觉会自然统一
            *[
                {"addBanding": {
                    "bandedRange": {
                        "range": {"sheetId": sheet_id,
                                  "startRowIndex": 6, "endRowIndex": TOTAL_ROWS,
                                  "startColumnIndex": slot * 3, "endColumnIndex": slot * 3 + 3},
                        "rowProperties": {
                            "firstBandColor": LIGHT_BLUE,
                            "secondBandColor": WHITE,
                        },
                    },
                }}
                for slot in range(TOTAL_COLS // 3)  # 10 个对话槽都预先带斑马纹
            ],
        ]

        sp.batch_update({"requests": requests})
        print(f"✅ 自动建分页成功: {name}")
    except Exception as e:
        print(f"❌ 自动建分页失败: {e}")


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
app.secret_key = os.environ.get("WEB_SECRET_KEY", "tg-monitor-web-2026")

# 反代场景(Caddy 加 HTTPS 给 OAuth 用):识别 X-Forwarded-* 头,让 request.host_url 拿到 https://
# 没反代时这个不会有副作用,因为浏览器不会自己加这些头
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

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


def get_sessions():
    """扫描 sessions 目录，返回已有的 session 列表"""
    sessions = []
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
                    })
                    run_async(_disconnect(client))
                else:
                    sessions.append({
                        "phone": phone, "name": "", "username": "",
                        "tg_id": "", "company": "", "operator": "", "status": "expired",
                    })
                    run_async(_disconnect(client))
            except Exception as e:
                sessions.append({
                    "phone": phone, "name": "", "username": "",
                    "tg_id": "", "company": "", "operator": "", "status": "error",
                })
    return sessions


# ============ 首次设置与设置修改 ============

@app.before_request
def _redirect_to_setup_if_needed():
    """未完成首次设置的话，把所有请求导到 /setup（除了静态资源和 setup 自身）"""
    path = request.path
    if path.startswith("/setup") or path.startswith("/api/setup") or path.startswith("/api/test-") or path.startswith("/static"):
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
        "bot_token": env.get("BOT_TOKEN", ""),
        "alert_group_id": env.get("ALERT_GROUP_ID", ""),
        "sheet_id": env.get("SHEET_ID", ""),
        "media_folder_id": env.get("MEDIA_FOLDER_ID", ""),
        "media_max_mb": env.get("MEDIA_MAX_MB", "20"),
        "oauth_client_id": env.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        "oauth_client_secret": env.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        "oauth_status": _get_oauth_status(),
        "web_password": env.get("WEB_PASSWORD", DEFAULT_WEB_PASSWORD),
        "keywords": env.get("KEYWORDS", DEFAULT_KEYWORDS),
        "no_reply_minutes": env.get("NO_REPLY_MINUTES", DEFAULT_NO_REPLY_MINUTES),
        "api_id": env.get("API_ID", DEFAULT_API_ID),
        "api_hash": env.get("API_HASH", DEFAULT_API_HASH),
        "sa_uploaded": SA_PATH.exists() and SA_PATH.stat().st_size > 100,
    }
    return render_template("setup.html", d=defaults, mode="setup")


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
        "bot_token": env.get("BOT_TOKEN", ""),
        "alert_group_id": env.get("ALERT_GROUP_ID", ""),
        "sheet_id": env.get("SHEET_ID", ""),
        "media_folder_id": env.get("MEDIA_FOLDER_ID", ""),
        "media_max_mb": env.get("MEDIA_MAX_MB", "20"),
        "oauth_client_id": env.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        "oauth_client_secret": env.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        "oauth_status": _get_oauth_status(),
        "keywords": env.get("KEYWORDS", DEFAULT_KEYWORDS),
        "no_reply_minutes": env.get("NO_REPLY_MINUTES", DEFAULT_NO_REPLY_MINUTES),
        "api_id": env.get("API_ID", DEFAULT_API_ID),
        "api_hash": env.get("API_HASH", DEFAULT_API_HASH),
        "sa_uploaded": SA_PATH.exists() and SA_PATH.stat().st_size > 100,
        "is_admin": is_admin(me),
        "is_super": is_super(me),
        "me": me,
    }
    return render_template("setup.html", d=current, mode="settings")


@app.route("/api/enable-https", methods=["POST"])
@admin_required
def api_enable_https():
    custom = (request.form.get("domain") or "").strip()
    ok, payload = _enable_https_now(custom)
    if ok:
        return jsonify({"ok": True, "msg": "Caddy 已启动,正在申请 Let's Encrypt 证书(约 30-60 秒)", **payload})
    return jsonify({"ok": False, "msg": str(payload)})


@app.route("/api/https-status", methods=["GET"])
@admin_required
def api_https_status():
    return jsonify(_https_status())


@app.route("/api/disable-https", methods=["POST"])
@admin_required
def api_disable_https():
    """关掉 Caddy(回退到 http://IP:5002 直连)"""
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        for c in client.containers.list(all=True):
            if c.name.startswith("tg-caddy-"):
                c.stop(timeout=5)
                c.remove(force=True)
        return jsonify({"ok": True, "msg": "Caddy 已停止"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


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
@admin_required
def api_oauth_start():
    """开始 Google OAuth 流程 — 跳转到 Google 授权页"""
    env = read_env()
    cid = env.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    csec = env.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        return "请先在设置里填写 OAuth Client ID 和 Secret 并保存", 400
    try:
        import oauth_helper
        url = oauth_helper.build_auth_url(cid, csec, _oauth_redirect_uri())
        return redirect(url)
    except Exception as e:
        return f"授权 URL 生成失败: {e}", 500


@app.route("/api/oauth/callback", methods=["GET"])
def api_oauth_callback():
    """Google 授权完成后回调到这里 — 用 code 换 token 并存盘"""
    code = request.args.get("code", "")
    err = request.args.get("error", "")
    if err:
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
@admin_required
def api_oauth_revoke():
    """撤销 OAuth 授权"""
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


@app.route("/api/test-media-folder", methods=["POST"])
def api_test_media_folder():
    """测试 Drive 文件夹访问权限：用现有 service-account.json 检查能否在该文件夹建文件"""
    folder_id = (request.form.get("media_folder_id") or "").strip()
    if not folder_id:
        return jsonify({"ok": False, "msg": "请填写 Drive 文件夹 ID"})
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload
        import io as _io
        # 优先用 OAuth(实际上传时也是这样)
        creds = None
        creds_source = ""
        try:
            import oauth_helper
            creds = oauth_helper.get_credentials()
            if creds:
                creds_source = "OAuth 用户授权"
        except Exception:
            pass
        if not creds:
            if not SA_PATH.exists():
                return jsonify({"ok": False, "msg": "请先上传 service-account.json,或先连接 Google Drive (OAuth)"})
            from google.oauth2.service_account import Credentials
            creds = Credentials.from_service_account_file(
                str(SA_PATH),
                scopes=["https://www.googleapis.com/auth/drive"],
            )
            creds_source = "Service Account"
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        # 1. 文件夹存在性 + 访问权限
        meta = drive.files().get(fileId=folder_id, fields="id,name,mimeType",
                                 supportsAllDrives=True).execute()
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            return jsonify({"ok": False, "msg": f"ID「{folder_id}」不是文件夹"})
        folder_name = meta.get("name", "")
        # 2. 写权限测试：建一个 4 字节探针文件，立即删掉
        probe = MediaIoBaseUpload(_io.BytesIO(b"ping"), mimetype="text/plain", resumable=False)
        f = drive.files().create(
            body={"name": "_tg_monitor_probe.txt", "parents": [folder_id]},
            media_body=probe, fields="id", supportsAllDrives=True,
        ).execute()
        try:
            drive.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
        except Exception:
            pass
        return jsonify({"ok": True, "msg": f"成功访问文件夹「{folder_name}」并通过写入测试 ✓ (用 {creds_source})"})
    except Exception as e:
        err = str(e)
        if "storageQuotaExceeded" in err or "do not have storage quota" in err:
            return jsonify({"ok": False, "msg": "Service Account 没有 Drive 配额。请到上方点「连接 Google Drive」用客户帐号 OAuth 授权,这样会用客户的 15GB 配额。"})
        if "File not found" in err or "notFound" in err:
            hint = ""
            try:
                if creds_source == "Service Account" and SA_PATH.exists():
                    sa = json.loads(SA_PATH.read_bytes())
                    hint = f"请把文件夹分享给 service account:{sa.get('client_email','?')}(编辑者)"
                elif creds_source == "OAuth 用户授权":
                    hint = "请确认这个文件夹 ID 是 OAuth 授权账号自己的,或者已被分享给该账号(编辑者)"
            except Exception:
                pass
            return jsonify({"ok": False, "msg": f"文件夹找不到或没权限。{hint}"})
        return jsonify({"ok": False, "msg": f"{type(e).__name__}: {err}"})


@app.route("/api/test-sheets", methods=["POST"])
def api_test_sheets():
    """测试 Sheet ID + service-account.json"""
    sheet_id = request.form.get("sheet_id", "")
    sa_file = request.files.get("service_account")
    if sa_file:
        sa_bytes = sa_file.read()
    elif SA_PATH.exists():
        sa_bytes = SA_PATH.read_bytes()
    else:
        return jsonify({"ok": False, "msg": "请先上传 service-account.json"})
    ok, msg = _test_sheets_access(sheet_id, sa_bytes)
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

    # 必填验证
    if not company_name or not bot_token or not alert_group_id or not sheet_id:
        return jsonify({"ok": False, "msg": "部门名称、Bot Token、预警群 ID、Sheet ID 都必填"})

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

    # service-account.json：首次必须上传；修改时如果上传了就覆盖，没上传就保留
    sa_file = request.files.get("service_account")
    if sa_file and sa_file.filename:
        sa_bytes = sa_file.read()
        # 先验证再写入
        ok, msg = _test_sheets_access(sheet_id, sa_bytes)
        if not ok:
            return jsonify({"ok": False, "msg": f"Sheet 验证失败：{msg}"})
        SA_PATH.write_bytes(sa_bytes)
    elif is_first and not SA_PATH.exists():
        return jsonify({"ok": False, "msg": "首次设置必须上传 service-account.json"})
    else:
        # 用现有凭证验证 sheet_id
        sa_bytes = SA_PATH.read_bytes()
        ok, msg = _test_sheets_access(sheet_id, sa_bytes)
        if not ok:
            return jsonify({"ok": False, "msg": f"Sheet 验证失败：{msg}"})

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

    # 写 .env
    updates = {
        "COMPANY_NAME": company_name,
        "COMPANY_DISPLAY": form.get("company_display", "").strip() or company_name,
        "PEER_ROLE_LABEL": form.get("peer_role_label", "").strip() or "广告主",
        "BOT_TOKEN": bot_token,
        "ALERT_GROUP_ID": alert_group_id,
        "SHEET_ID": sheet_id,
        "MEDIA_FOLDER_ID": form.get("media_folder_id", "").strip(),
        "MEDIA_MAX_MB": form.get("media_max_mb", "20").strip() or "20",
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
    return render_template("login.html", error=None, company=config.COMPANY_DISPLAY)


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
    return render_template("index.html", sessions=sessions, company=config.COMPANY_DISPLAY)


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
        return jsonify({"ok": False, "error": str(e)})


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

        return jsonify({
            "ok": True,
            "name": me.first_name or "",
            "username": me.username or "",
        })
    except SessionPasswordNeededError:
        _pending[phone]["need_password"] = True
        return jsonify({"ok": False, "need_password": True, "error": "此账号有两步验证，请输入密码"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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

        return jsonify({
            "ok": True,
            "name": me.first_name or "",
            "username": me.username or "",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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

        # 3. 删 Sheets 分页（失败不影响主流程）
        if tab:
            try:
                import gspread
                from google.oauth2.service_account import Credentials
                creds = Credentials.from_service_account_file(
                    str(config.SERVICE_ACCOUNT_FILE),
                    scopes=["https://www.googleapis.com/auth/spreadsheets",
                            "https://www.googleapis.com/auth/drive"],
                )
                gc = gspread.authorize(creds)
                sh = gc.open_by_key(config.SHEET_ID)
                ws = sh.worksheet(tab)
                sh.del_worksheet(ws)
                deleted_sheet = True
            except Exception as e:
                print(f"删 Sheets 分页失败（可忽略）: {e}")

    return jsonify({
        "ok": True,
        "deleted_db": deleted_db,
        "deleted_sheet": deleted_sheet,
    })


if __name__ == "__main__":
    db.init_db()
    print("🌐 登录管理介面启动: http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
