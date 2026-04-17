"""软升级模块 — 拉 GitHub tarball 覆盖代码 + 重启容器 (无需 git/docker CLI)."""
import io
import json
import logging
import os
import shutil
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

import config
import update_checker

logger = logging.getLogger(__name__)

TZ_BJ = timezone(timedelta(hours=8))
REPO_ROOT = Path("/app/repo")
STATE_PATH = config.DATA_DIR / "upgrade_status.json"
LOCK_PATH = config.DATA_DIR / ".upgrade.lock"

REBUILD_TRIGGER_FILES = {"Dockerfile", "requirements.txt", "docker-compose.yml"}
# 升级时保留的 host 文件/目录 (不能被 tarball 覆盖)
PRESERVE = {".env", "data", "sessions", ".git", "logs"}


def _now():
    return datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S")


def _save_state(state: dict):
    try:
        STATE_PATH.parent.mkdir(exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"save upgrade state failed: {e}")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"phase": "idle"}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"phase": "idle"}


def _lock_acquire() -> bool:
    if LOCK_PATH.exists():
        # 锁超过 10 分钟视为僵死
        try:
            age = time.time() - LOCK_PATH.stat().st_mtime
            if age < 600:
                return False
        except Exception:
            return False
    try:
        LOCK_PATH.parent.mkdir(exist_ok=True)
        LOCK_PATH.write_text(str(os.getpid()))
        return True
    except Exception:
        return False


def _lock_release():
    try:
        LOCK_PATH.unlink()
    except Exception:
        pass


def check_rebuild_needed(base_sha: str, head_sha: str) -> tuple[bool, list]:
    """用 compare API 看 base..head 之间 Dockerfile / requirements.txt / compose 有没有改."""
    try:
        url = f"https://api.github.com/repos/{update_checker.REPO}/compare/{base_sha}...{head_sha}"
        req = urllib.request.Request(url, headers={"User-Agent": "tg-monitor-upgrader"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
        changed = [f["filename"] for f in data.get("files", [])]
        triggers = [f for f in changed if f in REBUILD_TRIGGER_FILES]
        return bool(triggers), triggers
    except Exception as e:
        logger.warning(f"compare api failed: {e}")
        return False, []


def _download_tarball(sha: str) -> bytes:
    url = f"https://api.github.com/repos/{update_checker.REPO}/tarball/{sha}"
    req = urllib.request.Request(url, headers={"User-Agent": "tg-monitor-upgrader"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _apply_tarball(tar_bytes: bytes):
    """解压到 REPO_ROOT,跳过 PRESERVE 里的顶层目录/文件."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        members = tf.getmembers()
        # tarball 根目录是 <owner>-<repo>-<shortsha>/...
        if not members:
            raise RuntimeError("empty tarball")
        root_prefix = members[0].name.split("/")[0] + "/"
        applied = 0
        for m in members:
            if not m.name.startswith(root_prefix):
                continue
            rel = m.name[len(root_prefix):]
            if not rel:
                continue
            top = rel.split("/")[0]
            if top in PRESERVE:
                continue
            dest = REPO_ROOT / rel
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                f = tf.extractfile(m)
                if f:
                    dest.write_bytes(f.read())
                    try:
                        dest.chmod(m.mode or 0o644)
                    except Exception:
                        pass
                    applied += 1
        return applied


def _restart_containers(company: str):
    """用 docker SDK 重启 tg-monitor-<company> + tg-web-<company>."""
    import docker
    client = docker.from_env()
    # 先监控,再 web(web 自杀前先刷状态)
    for name in [f"tg-monitor-{company}", f"tg-web-{company}"]:
        try:
            c = client.containers.get(name)
            logger.info(f"restarting {name}...")
            c.restart(timeout=10)
        except Exception as e:
            logger.error(f"restart {name} failed: {e}")


def _run_upgrade(company: str, latest_sha: str):
    state = {"phase": "running", "started_at": _now(), "latest_sha": latest_sha, "logs": []}
    def log(msg):
        state["logs"].append(f"[{_now()}] {msg}")
        _save_state(state)
        logger.info(msg)

    _save_state(state)
    try:
        log(f"下载版本包 {latest_sha[:7]}...")
        tb = _download_tarball(latest_sha)
        log(f"下载完成,{len(tb)//1024} KB")

        log("覆盖代码文件(保留 .env/data/sessions/.git)...")
        n = _apply_tarball(tb)
        log(f"覆盖 {n} 个文件")

        # 同步到 /app (web 容器启动时会再 cp 一次,这里不用手动)
        state["phase"] = "restarting"
        state["finished_files_at"] = _now()
        _save_state(state)

        # 用 delay 线程重启,让 HTTP 响应先回到前端
        def delayed_restart():
            time.sleep(1.5)
            log("重启监控 + web 容器...")
            _restart_containers(company)
        threading.Thread(target=delayed_restart, daemon=True).start()

        state["phase"] = "done"
        state["finished_at"] = _now()
        _save_state(state)
    except Exception as e:
        logger.exception("upgrade failed")
        state["phase"] = "error"
        state["error"] = str(e)
        state["finished_at"] = _now()
        _save_state(state)
    finally:
        _lock_release()



def build_upgrade_cmd(company: str = "") -> str:
    """生成升级命令 — 读 .env 的 INSTALL_DIR 和 VPS_PUBLIC_IP,非标准安装/带 IP 都兼容"""
    env_path = REPO_ROOT / ".env"
    env = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    company = company or env.get("COMPANY_NAME", "") or "default"
    install_dir = env.get("INSTALL_DIR") or f"/root/tg-monitor-{company}"
    ip = env.get("VPS_PUBLIC_IP", "").strip()
    if ip:
        return f'ssh root@{ip} "cd {install_dir} && bash update.sh"'
    return f"cd {install_dir} && bash update.sh"


def start_soft_upgrade(company: str) -> dict:
    """返回 {ok, need_rebuild?, rebuild_files?, ssh_cmd?, started?}"""
    has_update, st = update_checker.check_once()
    if not has_update:
        return {"ok": True, "has_update": False, "msg": "当前已是最新版本"}

    local = st.get("local_sha", "")
    latest = st.get("latest_sha", "")
    need_rebuild, trigger_files = check_rebuild_needed(local, latest)
    if need_rebuild:
        return {
            "ok": True, "has_update": True, "need_rebuild": True,
            "rebuild_files": trigger_files,
            "ssh_cmd": build_upgrade_cmd(company),
            "msg": "此版本涉及镜像重建,请 SSH 到服务器跑命令",
        }

    if not _lock_acquire():
        return {"ok": False, "error": "已有升级在执行,请稍后再试"}

    t = threading.Thread(target=_run_upgrade, args=(company, latest), daemon=True)
    t.start()
    return {"ok": True, "has_update": True, "need_rebuild": False, "started": True}
