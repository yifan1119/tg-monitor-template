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
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import database as db

# v3.1.2.1 P0-1 修复:caddy_self_heal_loop 和其他日志调用都用这个 logger;
# 之前模块顶部没声明,daemon 启动即 NameError。
logger = logging.getLogger(__name__)


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


def _find_host_repo_path():
    """通过 docker SDK 找当前 tg-web 容器 /app/repo bind-mount 对应的 host 路径。
    需要这个路径,才能在临时容器里挂同一目录跑 git。"""
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        # 当前容器 hostname 通常就是 short container ID(docker 默认)
        me_id = socket.gethostname()
        c = client.containers.get(me_id)
        for m in c.attrs.get("Mounts", []) or []:
            if m.get("Destination") == "/app/repo":
                return m.get("Source")
    except Exception:
        pass
    return None


def action_upgrade(params: dict) -> tuple[bool, dict]:
    """v3.0.28:触发完整升级。
    优先级:
      1. 容器内有 git(v3.0.28+ Dockerfile 已装)→ 直接跑 subprocess git
      2. 否则起临时 alpine 容器(挂 docker socket + host_repo)→ apk add git
         → 跑完整 update.sh(含 docker compose up --build)
    第二条路解决 v3.0.26/27 升级到 v3.0.28 时容器内无 git 的死循环。

    params:
      - target_tag (str):main / vX.Y.Z(regex 校验防注入)
      - dry_run (bool):不真升级,只返「would run」
    """
    target = (params.get("target_tag") or "main").strip()
    dry_run = bool(params.get("dry_run", False))

    import re as _re
    if not _re.match(r'^(main|v\d+\.\d+(?:\.\d+){0,2})$', target):
        return False, {"error": f"invalid target_tag: {target!r}"}

    repo = Path("/app/repo")
    if not repo.exists():
        repo = Path(__file__).parent

    # v3.1.2.1 P0-3 fix:tag 和 branch 走不同 checkout 路径。
    # 老代码用 `git reset --hard origin/<target>`,但 git tag 不存在 `origin/<tag>`
    # 这个 ref(只有 branch 才有),Codex 复现 `git rev-parse origin/v3.0.16` 失败。
    # tag → `git checkout --detach <tag>`(读 refs/tags/<tag>)
    # branch → 仍走老的 `checkout + reset --hard origin/<branch>`
    import re as _re_internal
    is_tag = bool(_re_internal.match(r'^v\d', target))

    # v3.1.3 P0-4 fix:必须 git AND docker 都在容器内,才走 container_git 路径。
    # 之前只查 git,但 Dockerfile 没装 docker-cli → update.sh 里所有 docker ps /
    # docker exec / docker compose up 全 fail。fanout 28 台全失败的根因。
    # 现在缺 docker → fall through 到 alpine fallback 路径(alpine 容器 apk add docker-cli)。
    have_container_tools = bool(shutil.which("git") and shutil.which("docker"))

    if dry_run:
        path = "container_git" if have_container_tools else "alpine_container"
        return True, {"target": target, "repo": str(repo), "method": path,
                       "is_tag": is_tag,
                       "container_has_git": bool(shutil.which("git")),
                       "container_has_docker": bool(shutil.which("docker")),
                       "would_run": (
                           f"git fetch --tags + git checkout --detach {target} + bash update.sh"
                           if is_tag else
                           f"git fetch + git checkout {target} + git reset --hard origin/{target} + bash update.sh"
                       )}

    # ---- 路径 1:容器内有 git + docker(只有两个都有才走) ----
    if have_container_tools:
        env = os.environ.copy()
        env["NO_INTERACTIVE"] = "1"
        steps = []
        if is_tag:
            cmds = [
                ["git", "-C", str(repo), "fetch", "--tags", "--prune"],
                ["git", "-C", str(repo), "checkout", "--detach", target],
                ["bash", str(repo / "update.sh")],
            ]
        else:
            cmds = [
                ["git", "-C", str(repo), "fetch", "--tags", "--prune"],
                ["git", "-C", str(repo), "checkout", target],
                ["git", "-C", str(repo), "reset", "--hard", f"origin/{target}"],
                ["bash", str(repo / "update.sh")],
            ]
        for cmd in cmds:
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
                    return False, {"steps": steps, "failed_at": cmd[0], "method": "container_git",
                                    "is_tag": is_tag}
            except subprocess.TimeoutExpired:
                steps.append({"cmd": " ".join(cmd), "error": "timeout 8min"})
                return False, {"steps": steps, "failed_at": cmd[0], "method": "container_git",
                                "is_tag": is_tag}
        return True, {"target": target, "method": "container_git", "steps": steps,
                       "is_tag": is_tag,
                       "new_version": _read_version_string()}

    # ---- 路径 2:容器内无 git → 起临时 alpine 容器 ----
    host_repo = _find_host_repo_path()
    if not host_repo:
        return False, {"error": "container has no git AND can't find host /app/repo bind-mount",
                        "fix": "客户 SSH 一次跑: cd /root/tg-monitor-<dept> && git pull && ./update.sh"}
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        # v3.1.2.1 P0-3 fix:tag 走 --detach,branch 走 reset --hard origin/
        if is_tag:
            checkout_seq = f"git checkout --detach {target}"
        else:
            checkout_seq = (
                f"(git checkout {target} 2>/dev/null || git checkout -B {target} origin/{target}); "
                f"git reset --hard origin/{target}"
            )
        script = (
            f"set -e; "
            f"apk add --no-cache --quiet git bash docker-cli docker-cli-compose >/dev/null 2>&1; "
            f"cd /repo; "
            f"git fetch --tags --prune; "
            f"{checkout_seq}; "
            f"bash update.sh"
        )
        out_bytes = client.containers.run(
            "alpine:3.20",
            ["sh", "-c", script],
            volumes={
                host_repo: {"bind": "/repo", "mode": "rw"},
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
            },
            environment={"NO_INTERACTIVE": "1"},
            remove=True,
            stdout=True, stderr=True,
        )
        out_text = out_bytes.decode("utf-8", "ignore") if isinstance(out_bytes, bytes) else str(out_bytes)
        return True, {"target": target, "method": "alpine_container",
                       "host_repo": host_repo,
                       "log_tail": out_text[-1500:],
                       "new_version": _read_version_string()}
    except Exception as e:
        return False, {"error": f"alpine container: {type(e).__name__}: {e}",
                        "host_repo": host_repo}


def action_set_env(params: dict) -> tuple[bool, dict]:
    """v3.0.28:改 .env 白名单 key + 重启 tg-web 应用。
    params: {key: 'KEYWORDS' | ..., value: 'xxx'} 或 {kvs: {k:v, k2:v2}}
    白名单:KEYWORDS / SKIP_NO_REPLY_TEXTS / SKIP_NO_REPLY_MIN_LEN / SKIP_NO_REPLY_PURE_EMOJI
           / NO_REPLY_MINUTES / WORK_HOUR_START / WORK_HOUR_END
           / CENTRAL_PUSH_URL / CENTRAL_PUSH_TOKEN
           / ALERT_KEYWORD_ENABLED / ALERT_NO_REPLY_ENABLED / ALERT_DELETE_ENABLED
           / SHEET_RESYNC_ENABLED / BUSINESS_FIELD_SYNC_ENABLED
    永禁:BOT_TOKEN / API_ID / API_HASH / WEB_PASSWORD / METRICS_TOKEN / SHEET_ID / OAUTH_* 等"""
    ALLOWED_KEYS = {
        "KEYWORDS", "SKIP_NO_REPLY_TEXTS", "SKIP_NO_REPLY_MIN_LEN", "SKIP_NO_REPLY_PURE_EMOJI",
        "NO_REPLY_MINUTES", "WORK_HOUR_START", "WORK_HOUR_END",
        "CENTRAL_PUSH_URL", "CENTRAL_PUSH_TOKEN",
        "ALERT_KEYWORD_ENABLED", "ALERT_NO_REPLY_ENABLED", "ALERT_DELETE_ENABLED",
        "SHEET_RESYNC_ENABLED", "BUSINESS_FIELD_SYNC_ENABLED",
        "REMIND_30MIN_TEXT", "REMIND_40MIN_TEXT", "REMIND_DELETE_TEXT",
    }
    kvs = params.get("kvs") or {}
    if not kvs and params.get("key"):
        kvs = {params["key"]: params.get("value", "")}
    if not isinstance(kvs, dict) or not kvs:
        return False, {"error": "需要 kvs:{k:v} 或 key+value"}
    bad = [k for k in kvs if k not in ALLOWED_KEYS]
    if bad:
        return False, {"error": f"不在白名单: {bad}", "allowed": sorted(ALLOWED_KEYS)}

    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return False, {"error": ".env not found"}

    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=False)
    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in kvs:
            new_lines.append(f"{k}={kvs[k]}")
            seen.add(k)
        else:
            new_lines.append(line)
    for k, v in kvs.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # 重启 tg-web 让新 .env 生效(config.reload_if_env_changed 也会自己热加载,
    # 但显式 restart 兜底,确保所有变更立即可见)
    restarted = []
    if params.get("restart_web", True):
        try:
            import docker as docker_sdk
            client = docker_sdk.from_env()
            company = os.environ.get("COMPANY_NAME", "").strip()
            if company:
                c = client.containers.get(f"tg-web-{company}")
                c.restart(timeout=15)
                restarted.append(f"tg-web-{company}")
        except Exception as e:
            return True, {"updated": list(kvs.keys()), "restart_err": str(e)}

    return True, {"updated": list(kvs.keys()), "restarted": restarted}


def action_tail_logs(params: dict) -> tuple[bool, dict]:
    """v3.0.28:docker logs 拉最近 N 行(可选 since)。
    params:
      - service: 'tg-monitor' | 'tg-web' | 'tg-caddy'(必填)
      - tail (int, default 100, max 500)
      - since (str, optional):'5m' / '1h' 等"""
    svc = (params.get("service") or "").strip()
    if svc not in ("tg-monitor", "tg-web", "tg-caddy"):
        return False, {"error": f"invalid service: {svc!r}",
                        "allowed": ["tg-monitor", "tg-web", "tg-caddy"]}
    tail = min(max(int(params.get("tail", 100)), 1), 500)
    since = (params.get("since") or "").strip() or None

    company = os.environ.get("COMPANY_NAME", "").strip()
    if not company:
        return False, {"error": "COMPANY_NAME not set"}
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        c = client.containers.get(f"{svc}-{company}")
        kw = {"tail": tail, "stdout": True, "stderr": True, "timestamps": True}
        if since:
            kw["since"] = since
        out = c.logs(**kw)
        return True, {
            "container": f"{svc}-{company}",
            "lines": out.decode("utf-8", "ignore").splitlines()[-tail:],
        }
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


def action_reload_oauth(params: dict) -> tuple[bool, dict]:
    """v3.0.30:OAuth credentials 重载 — 异步触发 tg-monitor 重启读最新 oauth token 文件。
    本质就是让 tg-monitor 进程重新 init SheetsWriter(读 data/google_oauth_token.json)。
    异步发起后立刻返 200,不等 restart 完成(避免 HTTP client 超时)。"""
    company = os.environ.get("COMPANY_NAME", "").strip()
    if not company:
        return False, {"error": "COMPANY_NAME not set"}
    try:
        import docker as docker_sdk, threading
        client = docker_sdk.from_env()
        c = client.containers.get(f"tg-monitor-{company}")
        # 后台线程触发 restart,不阻塞 HTTP 响应
        threading.Thread(target=lambda: c.restart(timeout=20), daemon=True).start()
        return True, {
            "container": f"tg-monitor-{company}",
            "status": "restart_triggered_async",
            "note": "tg-monitor 后台重启,约 10 秒后生效。期间 tg-web/dashboard 不受影响"
        }
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


def action_restart_caddy(params: dict) -> tuple[bool, dict]:
    """v3.1:重启 dept VPS 的 Caddy 容器(走 docker SDK,不依赖 Caddy 反代)。
    用于 TLS 故障 / 证书续期失败时手动恢复。注意:Caddy 完全挂时外部到 agent
    的路径也断,这条 endpoint 形同虚设;真正自愈靠下面的 caddy_self_heal_loop
    daemon 在 dept 容器内自启动定时跑。"""
    company = os.environ.get("COMPANY_NAME", "").strip()
    if not company:
        return False, {"error": "COMPANY_NAME not set"}
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        # 部门 caddy 容器名:tg-caddy-<company>(共享 caddy 模式可能不同名)
        candidates = [f"tg-caddy-{company}", f"caddy-{company}", "tg-caddy", "caddy"]
        for cn in candidates:
            try:
                c = client.containers.get(cn)
                c.restart(timeout=20)
                c.reload()
                return True, {"container": cn, "status": c.status}
            except docker_sdk.errors.NotFound:
                continue
        return False, {"error": f"caddy container not found, tried: {candidates}"}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


# ============================================================
# v3.1 Caddy self-heal daemon — 内部自检 + 自愈
# ============================================================

_CADDY_HEAL_LAST_ATTEMPT = {"ts": 0, "fail_count": 0}
CADDY_HEAL_COOLDOWN = 600   # 同 Caddy 重启冷却 10 min(防 rapid loop)
CADDY_HEAL_MAX_FAILS = 5    # 5 次自愈失败放弃,推 TG 求救
CADDY_HEAL_CHECK_INTERVAL = 300   # 5 min 自检一次


def _caddy_self_test() -> tuple[bool, str]:
    """从 dept 容器内 self-test:HTTPS 访问自己看 TLS 是否 work。
    通过 PUBLIC_DOMAIN 或 VPS_PUBLIC_IP+nip.io 拼出自己的 URL。"""
    domain = (os.environ.get("PUBLIC_DOMAIN") or "").strip()
    if not domain:
        ip = (os.environ.get("VPS_PUBLIC_IP") or "").strip()
        company = (os.environ.get("COMPANY_NAME") or "").strip()
        if not ip or not company:
            return True, "skip: no domain/ip"  # 没法判断,跳过
        domain = f"{company}.{ip.replace('.', '-')}.nip.io"  # 大概率不准,只 best-effort
    import urllib.request as _req, ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False; ctx.verify_mode = _ssl.CERT_NONE
    try:
        with _req.urlopen(f"https://{domain}/login", timeout=10, context=ctx) as r:
            return True, f"HTTPS OK ({r.status})"
    except _ssl.SSLError as e:
        return False, f"SSL: {str(e)[:80]}"
    except Exception as e:
        msg = str(e)[:80]
        if "SSL" in msg or "TLS" in msg or "tlsv1" in msg.lower():
            return False, f"TLS: {msg}"
        # 网络层错(443 端口不通等)— 不算 TLS 错,但 Caddy 也可能挂
        return False, f"net: {type(e).__name__}: {msg}"


def caddy_self_heal_loop():
    """v3.1 daemon:每 5 分钟 self-test TLS,失败立刻 docker restart caddy。
    冷却 10 min(防 rapid loop)。连续 5 次失败放弃 + 写 log(后续 TG 推)。"""
    if os.environ.get("CADDY_SELF_HEAL_ENABLED", "true").lower() != "true":
        logger.info("[caddy_heal] CADDY_SELF_HEAL_ENABLED=false,跳过")
        return
    time.sleep(120)  # 等启动稳定
    logger.info("[caddy_heal] daemon 启动 interval=%ds cooldown=%ds",
                CADDY_HEAL_CHECK_INTERVAL, CADDY_HEAL_COOLDOWN)
    while True:
        try:
            ok, detail = _caddy_self_test()
            if ok:
                if _CADDY_HEAL_LAST_ATTEMPT["fail_count"] > 0:
                    logger.info("[caddy_heal] 自愈成功 ✓ %s", detail)
                _CADDY_HEAL_LAST_ATTEMPT["fail_count"] = 0
            else:
                now = time.time()
                if now - _CADDY_HEAL_LAST_ATTEMPT["ts"] < CADDY_HEAL_COOLDOWN:
                    logger.info("[caddy_heal] 检测到故障但在冷却内,跳过本轮: %s", detail)
                else:
                    _CADDY_HEAL_LAST_ATTEMPT["ts"] = now
                    _CADDY_HEAL_LAST_ATTEMPT["fail_count"] += 1
                    fc = _CADDY_HEAL_LAST_ATTEMPT["fail_count"]
                    if fc <= CADDY_HEAL_MAX_FAILS:
                        logger.warning("[caddy_heal] TLS 故障 (#%d/%d): %s — restart caddy",
                                       fc, CADDY_HEAL_MAX_FAILS, detail)
                        ok2, res = action_restart_caddy({})
                        logger.info("[caddy_heal] restart 结果: ok=%s res=%s", ok2, res)
                    else:
                        logger.error("[caddy_heal] %d 次自愈失败,放弃 — 需 SSH 介入", fc)
        except Exception as e:
            logger.exception("[caddy_heal] loop 异常: %s", e)
        time.sleep(CADDY_HEAL_CHECK_INTERVAL)


def start_caddy_self_heal_in_thread():
    """main.py 启动时调一次"""
    import threading as _th
    t = _th.Thread(target=caddy_self_heal_loop, daemon=True, name="caddy_self_heal")
    t.start()
    return t


def action_fix_sheets(params: dict) -> tuple[bool, dict]:
    """v3.0.30:直接调 dashboard_api 函数,绕过 HTTP cookie 鉴权问题。
    agent 跟 web.py 同 Python 进程,可直接 import。
    params.action: 'orphan_messages' | 'col_group_null' | 'all'(默认 all)"""
    action = (params.get("action") or "all").strip().lower()
    if action not in ("orphan_messages", "col_group_null", "all"):
        return False, {"error": f"invalid action: {action!r}"}
    try:
        import dashboard_api
        results = {}
        if action in ("orphan_messages", "all"):
            results["orphan_messages"] = dashboard_api.fix_orphan_messages()
        if action in ("col_group_null", "all"):
            results["col_group_null"] = dashboard_api.fix_peers_no_col_group()
        all_ok = all(r.get("ok", False) for r in results.values())
        return all_ok, {"results": results}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


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

def action_verify_ui_version(params: dict) -> tuple[bool, dict]:
    """v3.0.27:通过 docker SDK 在 tg-web 容器内 grep templates/index.html,
    验证 UI 是否同步到最新版(各版本特征字段)。

    返:
      - markers: {label: {marker, count, present}}
      - is_up_to_date: 所有 markers 都 present
    """
    company = os.environ.get("COMPANY_NAME", "").strip()
    if not company:
        return False, {"error": "COMPANY_NAME not set"}
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        c = client.containers.get(f"tg-web-{company}")
        version_markers = {
            "v3.0.15+": "nc_inspector_tg_id",   # 监察员 TG ID 字段
            "v3.0.21+": "company_options",      # 公司中心下拉数据源
            "v3.0.25+": "autoOpenConfig",       # 登录后自动 open 配置 modal
        }
        markers = {}
        for label, needle in version_markers.items():
            exit_code, out = c.exec_run(
                ["sh", "-c", f"grep -c '{needle}' /app/templates/index.html 2>/dev/null || echo 0"]
            )
            try:
                count = int(out.decode("utf-8", "ignore").strip())
            except ValueError:
                count = 0
            markers[label] = {"marker": needle, "count": count, "present": count > 0}

        return True, {
            "container":     f"tg-web-{company}",
            "markers":       markers,
            "is_up_to_date": all(m["present"] for m in markers.values()),
        }
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


def action_get_web_credentials(params: dict) -> tuple[bool, dict]:
    """v3.0.27 ⚠ 高敏感 — 返 .env 里的 WEB_USERNAME / WEB_PASSWORD 明文。
    用途:中央台运维远程登入 dept VPS web 后台 debug。
    安全:走 metrics_token + HMAC 鉴权,audit_log 完整留痕。"""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return False, {"error": ".env not found"}
    user, pw = "", ""
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("WEB_USERNAME="):
                user = line.split("=", 1)[1].strip()
            elif line.startswith("WEB_PASSWORD="):
                pw = line.split("=", 1)[1].strip()
    except Exception as e:
        return False, {"error": f"read .env: {e}"}
    return True, {
        "username": user,
        "password": pw,
        "note": "⚠ .env 初始凭据。客户在 web /settings/users 改过密码,users 表才是真值。",
    }


ACTION_HANDLERS = {
    "inspect":             action_inspect,
    "upgrade":             action_upgrade,
    "restart_svc":         action_restart_svc,
    "restart_caddy":       action_restart_caddy,        # v3.1
    "verify_ui_version":   action_verify_ui_version,
    "get_web_credentials": action_get_web_credentials,
    "set_env":             action_set_env,
    "tail_logs":           action_tail_logs,
    "reload_oauth":        action_reload_oauth,
    "fix_sheets":          action_fix_sheets,
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
