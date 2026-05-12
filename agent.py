"""v3.0.26 Remote Agent — 中央台远程运维 endpoint 的 action 分发 + 安全护栏。

设计:
- HTTP endpoint 由 web.py 提供(`/api/v1/admin/cmd`),仅做 routing
- 真正逻辑放本模块,便于单测 + 不与 Flask 强耦合
- 白名单 action 制:`ACTION_HANDLERS` 字典每个 action 独立函数,任意 shell 永禁

鉴权(web.py 已做 token + HMAC + nonce + timestamp 校验,本模块假设已通过):
- Bearer = METRICS_TOKEN(.env)
- X-Agent-Ts:±5 分钟时钟差
- X-Agent-Nonce:5 分钟去重
- X-Agent-Sig:HMAC_SHA256(token, f"{ts}.{nonce}.{body}")

3 个 v0 action(MVP):
- `inspect`     只读,返版本 + 容器 + .env 摘要 + sessions + disk + 最近 error
- `upgrade`     触发 ./update.sh(同步等完成,timeout 8 min)
- `restart_svc` docker restart tg-monitor / tg-web

后续扩展(留接口,不实现):set_env / tail_logs / diag_sheets / fix_sheets / reload_oauth / ...
"""
from __future__ import annotations

import hmac
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import database as db


# ============================================================
# 鉴权 helpers — web.py 收到请求后调这些校验
# ============================================================

NONCE_CACHE: dict[str, float] = {}   # nonce → 见到时间
NONCE_TTL = 300                       # 5 分钟去重窗
CLOCK_SKEW_TOLERANCE = 300            # 客户端时钟最多差 5 分钟


def verify_hmac(token: str, ts: str, nonce: str, body_bytes: bytes, provided_sig: str) -> bool:
    """HMAC_SHA256(token, f"{ts}.{nonce}.{body_bytes_hex}")  比对 provided_sig(hex)。
    body_bytes 取 hex 是为了避免直接拼字节流的编码问题。"""
    if not token or not ts or not nonce or not provided_sig:
        return False
    msg = f"{ts}.{nonce}.{body_bytes.hex()}".encode()
    mac = hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, provided_sig)


def check_timestamp(ts: str) -> bool:
    """±5 分钟"""
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return False
    return abs(time.time() - ts_int) <= CLOCK_SKEW_TOLERANCE


def check_nonce(nonce: str) -> bool:
    """5 分钟内 nonce 唯一。OK 返 True 并记录;重放返 False。
    简单实现:进程内 dict,GC 老 nonce。生产可加 sqlite 持久层。"""
    if not nonce or len(nonce) < 16:
        return False
    now = time.time()
    # GC
    expired = [k for k, t in NONCE_CACHE.items() if now - t > NONCE_TTL]
    for k in expired:
        NONCE_CACHE.pop(k, None)
    if nonce in NONCE_CACHE:
        return False
    NONCE_CACHE[nonce] = now
    return True


# ============================================================
# Rate limit — 简单进程内 token bucket(同 token 5 次/分钟)
# ============================================================

_RATE_BUCKET: dict[str, list[float]] = {}
_RATE_LIMIT = 5     # 次
_RATE_WINDOW = 60   # 秒


def check_rate_limit(token: str) -> bool:
    """同 token 1 分钟最多 5 次。OK 返 True;超返 False。"""
    now = time.time()
    bucket = _RATE_BUCKET.setdefault(token, [])
    # 清窗外
    while bucket and now - bucket[0] > _RATE_WINDOW:
        bucket.pop(0)
    if len(bucket) >= _RATE_LIMIT:
        return False
    bucket.append(now)
    return True


# ============================================================
# Action 实现 — 每个函数收 dict params,返 (ok: bool, result: dict)
# ============================================================

def _read_version_string() -> str:
    """从 README.md 顶部找版本号(_app_version_string 同 source)。"""
    try:
        readme = Path(__file__).parent / "README.md"
        if readme.exists():
            import re
            txt = readme.read_text(encoding="utf-8")[:2000]
            m = re.search(r'v\d+\.\d+(?:\.\d+){1,2}', txt)
            if m:
                return m.group(0)
    except Exception:
        pass
    return "unknown"


def _read_env_summary() -> dict:
    """读 .env,只返 safe 字段 + 关键 bool flag(token / pwd 不返)。"""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return {"error": ".env not found"}
    env = {}
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    except Exception as e:
        return {"error": f"read env: {e}"}
    SAFE_KEYS = {
        "COMPANY_NAME", "COMPANY_DISPLAY", "PEER_ROLE_LABEL", "OPERATOR_LABEL",
        "VPS_PUBLIC_IP", "WEB_PORT",
        "NO_REPLY_MINUTES", "WORK_HOUR_START", "WORK_HOUR_END",
        "PATROL_DAYS", "HISTORY_DAYS",
        "SHEETS_FLUSH_INTERVAL", "SHEETS_RATE_LIMIT_PER_MIN",
        "MEDIA_STORAGE_MODE", "MEDIA_RETENTION_DAYS",
        "SETUP_COMPLETE", "ALERTS_ENABLED",
        "ALERT_KEYWORD_ENABLED", "ALERT_NO_REPLY_ENABLED", "ALERT_DELETE_ENABLED",
        "SHEET_RESYNC_ENABLED", "BUSINESS_FIELD_SYNC_ENABLED",
    }
    out = {k: env.get(k, "") for k in SAFE_KEYS if k in env}
    # 关键字 / 闲聊词 — 只返数量,不返内容(可能含客户敏感数据)
    kw = env.get("KEYWORDS", "")
    out["KEYWORDS_count"] = len([x for x in kw.replace(",", ",").split(",") if x.strip()]) if kw else 0
    snr = env.get("SKIP_NO_REPLY_TEXTS", "")
    out["SKIP_NO_REPLY_TEXTS_count"] = len([x for x in snr.split(",") if x.strip()]) if snr else 0
    # token / 密码类只返 bool 是否配置
    for k in ("API_ID", "API_HASH", "BOT_TOKEN", "ALERT_GROUP_ID", "SHEET_ID",
              "CENTRAL_PUSH_URL", "CENTRAL_PUSH_TOKEN", "WEB_PASSWORD",
              "METRICS_TOKEN", "GOOGLE_OAUTH_CLIENT_ID"):
        out[f"{k}_set"] = bool(env.get(k, "").strip())
    return out


def _read_session_states() -> dict:
    """汇总 session_states.json:N healthy / N revoked / N hijacked / N error。"""
    p = Path(__file__).parent / "data" / "session_states.json"
    if not p.exists():
        return {"total": 0}
    try:
        states = json.loads(p.read_text(encoding="utf-8"))
        counts = {"healthy": 0, "revoked": 0, "hijacked": 0, "error": 0, "other": 0}
        for phone, info in states.items():
            s = (info.get("status") or "other") if isinstance(info, dict) else "other"
            counts[s] = counts.get(s, 0) + 1
        return {"total": len(states), **counts}
    except Exception as e:
        return {"error": str(e)}


def _read_disk_info() -> dict:
    try:
        total, used, free = shutil.disk_usage("/")
        return {
            "total_gb": round(total / 1024**3, 1),
            "used_gb":  round(used  / 1024**3, 1),
            "free_gb":  round(free  / 1024**3, 1),
            "used_pct": round(used * 100 / total, 1),
        }
    except Exception as e:
        return {"error": str(e)}


def _docker_container_status(name_pattern: str) -> dict:
    """走 docker SDK(/var/run/docker.sock 已被 web 容器挂载,用 SDK 而非 CLI)。"""
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        rows = {}
        for c in client.containers.list(all=True):
            if name_pattern in c.name:
                rows[c.name] = c.status
        return rows
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _recent_error_log(tail: int = 20) -> list:
    """读容器内 tg-monitor stderr 最近 N 条 ERROR / EXCEPTION 行。
    走 docker logs 的话依赖 docker socket。这里偷懒读 data/ 下的 .log 文件(若有)。
    无 log 文件 → 返空 list,不阻塞 inspect。"""
    out = []
    log_dirs = [Path(__file__).parent / "data", Path(__file__).parent]
    for d in log_dirs:
        for log_file in d.glob("*.log"):
            try:
                lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]
                for ln in lines[-tail:]:
                    if any(k in ln for k in ("ERROR", "Exception", "Traceback")):
                        out.append({"file": log_file.name, "line": ln[:200]})
            except Exception:
                continue
            if len(out) >= tail:
                break
    return out[-tail:]


def action_inspect(params: dict) -> tuple[bool, dict]:
    """只读 — 综合健康检查。"""
    try:
        accounts_count = db.get_conn().execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
    except Exception:
        accounts_count = -1
    try:
        alerts_24h = db.get_conn().execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE created_at > datetime('now', '-1 day')"
        ).fetchone()["n"]
    except Exception:
        alerts_24h = -1

    return True, {
        "version":           _read_version_string(),
        "hostname":          socket.gethostname(),
        "env":               _read_env_summary(),
        "sessions":          _read_session_states(),
        "disk":              _read_disk_info(),
        "containers":        _docker_container_status("tg-"),
        "accounts_count":    accounts_count,
        "alerts_last_24h":   alerts_24h,
        "ts":                int(time.time()),
    }


def action_upgrade(params: dict) -> tuple[bool, dict]:
    """触发 ./update.sh(同步等完成 timeout 8 min)。
    params:
      - target_tag (str, optional):git checkout <tag> 之前 pull。空 = main
      - dry_run (bool, default False):不真升级,只返「will run」
    """
    target = (params.get("target_tag") or "main").strip()
    dry_run = bool(params.get("dry_run", False))

    # 安全 check — target 必须 main 或 v 开头的 tag,防 shell injection
    import re as _re
    if not _re.match(r'^(main|v\d+\.\d+(?:\.\d+){0,2})$', target):
        return False, {"error": f"invalid target_tag: {target!r}"}

    # 找仓库路径 — update.sh 通常在容器外的 host /root/tg-monitor-<dept>
    # 容器内通过 docker.sock 调 host 路径,或者直接 cd /app/repo(bind-mount)
    # 简单 prefer /app/repo(容器内 bind-mount 路径)
    repo = Path("/app/repo")
    if not repo.exists():
        repo = Path(__file__).parent

    if dry_run:
        return True, {"target": target, "repo": str(repo), "would_run": "./update.sh"}

    try:
        # git fetch + checkout target + pull + ./update.sh
        # 用 subprocess.run 同步等
        env = os.environ.copy()
        env["NO_INTERACTIVE"] = "1"
        steps = []
        for cmd in [
            ["git", "-C", str(repo), "fetch", "--tags", "--prune"],
            ["git", "-C", str(repo), "checkout", target],
            ["git", "-C", str(repo), "pull", "--ff-only"],
            ["bash", str(repo / "update.sh")],
        ]:
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=480, env=env,
                                   cwd=str(repo))
                steps.append({
                    "cmd": " ".join(cmd),
                    "exit": r.returncode,
                    "stdout_tail": r.stdout.decode("utf-8", "ignore")[-500:],
                    "stderr_tail": r.stderr.decode("utf-8", "ignore")[-500:],
                })
                if r.returncode != 0:
                    return False, {"steps": steps, "failed_at": cmd[0]}
            except subprocess.TimeoutExpired:
                steps.append({"cmd": " ".join(cmd), "error": "timeout 8min"})
                return False, {"steps": steps, "failed_at": cmd[0]}
        return True, {"target": target, "steps": steps,
                      "new_version": _read_version_string()}
    except Exception as e:
        return False, {"error": str(e)}


def action_restart_svc(params: dict) -> tuple[bool, dict]:
    """docker restart tg-monitor-<dept> 或 tg-web-<dept>。
    params.service: 'tg-monitor' | 'tg-web' | 'both'"""
    svc = (params.get("service") or "both").strip()
    if svc not in ("tg-monitor", "tg-web", "both"):
        return False, {"error": f"invalid service: {svc!r}"}

    targets = ["tg-monitor", "tg-web"] if svc == "both" else [svc]
    company = os.environ.get("COMPANY_NAME", "").strip()
    if not company:
        return False, {"error": "COMPANY_NAME not set in env"}

    results = {}
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
    except Exception as e:
        return False, {"error": f"docker SDK init: {e}"}

    for t in targets:
        container = f"{t}-{company}"
        try:
            c = client.containers.get(container)
            c.restart(timeout=20)
            c.reload()
            results[container] = {"status": c.status, "id": c.short_id}
        except docker_sdk.errors.NotFound:
            results[container] = {"error": "container not found"}
        except Exception as e:
            results[container] = {"error": f"{type(e).__name__}: {e}"}
    ok = all("error" not in r for r in results.values())
    return ok, {"results": results}


# ============================================================
# Dispatch + audit
# ============================================================

ACTION_HANDLERS = {
    "inspect":     action_inspect,
    "upgrade":     action_upgrade,
    "restart_svc": action_restart_svc,
}


def dispatch(action: str, params: dict, actor_ip: str = "") -> tuple[bool, dict]:
    """主分发入口 — web.py 鉴权通过后调这个。写 audit_logs 留痕。"""
    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        return False, {"error": f"unknown action: {action!r}",
                        "available": sorted(ACTION_HANDLERS.keys())}
    started = time.time()
    try:
        ok, result = handler(params or {})
    except Exception as e:
        ok, result = False, {"error": f"handler exception: {type(e).__name__}: {e}"}
    elapsed_ms = int((time.time() - started) * 1000)
    # audit log
    try:
        db.audit_log(
            event_type="fleet_cmd",
            actor_username="fleet_admin",
            actor_ip=actor_ip,
            target_type="agent",
            target_id=action,
            payload={"action": action, "params": params or {},
                     "ok": ok, "elapsed_ms": elapsed_ms,
                     "result_preview": str(result)[:300]},
            result="ok" if ok else "fail",
        )
    except Exception:
        pass  # audit 失败不阻塞 action 返回
    return ok, result
