"""密码重置 / TG 绑定 — 纯辅助函数，不含 Flask 依赖。

数据文件:
  data/pending_binds.json   — {code: {"username": str, "expires_at": float}}
  data/pending_resets.json  — {username: {"code": str, "expires_at": float,
                                           "created_at": float, "used": bool,
                                           "attempts": int}}
  data/auth_audit.log       — JSON lines (append-only)

users.json 由 web.load_users / save_users 负责读写（本模块通过 lazy import 调用），
写入走临时文件 + os.replace 保证原子性。
"""
import json
import os
import secrets
import string
import time
import urllib.parse
import urllib.request
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_PENDING_BINDS = config.DATA_DIR / "pending_binds.json"
_PENDING_RESETS = config.DATA_DIR / "pending_resets.json"
_AUDIT_LOG = config.DATA_DIR / "auth_audit.log"


# ---------------- 通用 I/O ----------------

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json_atomic(path: Path, data: dict):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_pending_binds() -> dict:
    return _load_json(_PENDING_BINDS)


def save_pending_binds(data: dict):
    _save_json_atomic(_PENDING_BINDS, data)


def load_pending_resets() -> dict:
    return _load_json(_PENDING_RESETS)


def save_pending_resets(data: dict):
    _save_json_atomic(_PENDING_RESETS, data)


def cleanup_expired(pending: dict) -> dict:
    now = time.time()
    return {k: v for k, v in pending.items()
            if isinstance(v, dict) and float(v.get("expires_at", 0)) >= now}


def audit_log(event_type: str, detail: dict):
    try:
        _AUDIT_LOG.parent.mkdir(exist_ok=True)
        entry = {"ts": time.time(), "event": event_type, **detail}
        with _AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("audit_log 写入失败: %s", e)


# ---------------- 验证码生成 ----------------

_BIND_ALPHABET = string.ascii_uppercase + string.digits


def generate_bind_code() -> str:
    suffix = "".join(secrets.choice(_BIND_ALPHABET) for _ in range(6))
    return f"BIND-{suffix}"


def generate_reset_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


# ---------------- TG DM ----------------

def tg_send_dm(bot_token: str, user_id: int, text: str) -> bool:
    if not bot_token or not user_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": int(user_id),
            "text": text,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read()
            j = json.loads(body.decode("utf-8"))
            return bool(j.get("ok"))
    except Exception as e:
        logger.error("tg_send_dm 失败 user_id=%s err=%s", user_id, e)
        return False


# ---------------- 绑定流程 ----------------

def create_bind_pending(username: str, ttl_sec: int = 600) -> str:
    """给 username 生成绑定码；若已有未过期的 pending，复用它。"""
    pending = cleanup_expired(load_pending_binds())
    # 同用户已有未过期绑定码 → 复用
    for code, info in pending.items():
        if info.get("username") == username:
            return code
    # 生成唯一码
    for _ in range(10):
        code = generate_bind_code()
        if code not in pending:
            break
    pending[code] = {
        "username": username,
        "expires_at": time.time() + ttl_sec,
        "created_at": time.time(),
    }
    save_pending_binds(pending)
    return code


def try_complete_bind(code: str, tg_user_id: int, tg_username: str):
    """匹配 pending 则写入 users.json，返回绑定的 username；否则 None。"""
    code = (code or "").strip().upper()
    pending = cleanup_expired(load_pending_binds())
    info = pending.get(code)
    if not info:
        # 即使过期也清理一下磁盘
        save_pending_binds(pending)
        return None
    username = info.get("username")
    # lazy import 避免循环
    import web
    users = web.load_users()
    if username not in users:
        pending.pop(code, None)
        save_pending_binds(pending)
        return None
    users[username]["tg_user_id"] = int(tg_user_id)
    users[username]["tg_username"] = tg_username or ""
    users[username]["tg_bound_at"] = time.time()
    web.save_users(users)
    pending.pop(code, None)
    save_pending_binds(pending)
    audit_log("bind", {"username": username, "tg_user_id": int(tg_user_id),
                       "tg_username": tg_username or ""})
    return username


def unbind_user(username: str) -> bool:
    import web
    users = web.load_users()
    if username not in users:
        return False
    prev_id = users[username].pop("tg_user_id", None)
    prev_name = users[username].pop("tg_username", None)
    users[username].pop("tg_bound_at", None)
    web.save_users(users)
    audit_log("unbind", {"username": username,
                         "prev_tg_user_id": prev_id,
                         "prev_tg_username": prev_name})
    return True


# ---------------- 密码重置 ----------------

RESET_RATE_LIMIT_SEC = 60


def create_reset_pending(username: str, ttl_sec: int = 300):
    """为 username 产生 6 位验证码；rate-limit 60s 内重复请求则返回 None。"""
    pending = load_pending_resets()
    now = time.time()
    # 清掉过期 + 已使用（超过 1 小时的 used 条目也丢）
    cleaned = {}
    for u, info in pending.items():
        if not isinstance(info, dict):
            continue
        if info.get("used") and now - float(info.get("created_at", 0)) > 3600:
            continue
        if float(info.get("expires_at", 0)) < now and not info.get("used"):
            continue
        cleaned[u] = info
    pending = cleaned

    existing = pending.get(username)
    if existing and not existing.get("used"):
        created = float(existing.get("created_at", 0))
        if now - created < RESET_RATE_LIMIT_SEC:
            return None  # rate-limited

    code = generate_reset_code()
    pending[username] = {
        "code": code,
        "expires_at": now + ttl_sec,
        "created_at": now,
        "used": False,
        "attempts": 0,
    }
    save_pending_resets(pending)
    return code


def consume_reset_code(code: str, username: str) -> bool:
    """原子校验+标记 used；成功 True，失败 False。"""
    code = (code or "").strip()
    username = (username or "").strip()
    pending = load_pending_resets()
    info = pending.get(username)
    if not isinstance(info, dict):
        return False
    if info.get("used"):
        return False
    if float(info.get("expires_at", 0)) < time.time():
        return False
    info["attempts"] = int(info.get("attempts", 0)) + 1
    if info.get("code") != code:
        pending[username] = info
        save_pending_resets(pending)
        return False
    info["used"] = True
    info["used_at"] = time.time()
    pending[username] = info
    save_pending_resets(pending)
    return True
