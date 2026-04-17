"""版本更新检查 — 每小时查 GitHub 有没有新版,有的话通知 TG Bot + 写状态文件给 Dashboard 读。

读本机 sha: /app/repo/.git/refs/heads/main (fallback: packed-refs)
读远端 sha: GitHub API (60 req/hr 匿名额度对单部署够用)
状态文件:    data/update_status.json
"""
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config

logger = logging.getLogger(__name__)

REPO = "yifan1119/tg-monitor-template"
BRANCH = "main"
STATE_PATH = config.DATA_DIR / "update_status.json"
GIT_DIR = Path("/app/repo/.git")  # host 代码目录,容器里是 :ro 挂载
TZ_BJ = timezone(timedelta(hours=8))


def _read_local_sha() -> str:
    """读本地 HEAD — 优先 refs/heads/main,fallback packed-refs"""
    try:
        head = GIT_DIR / "refs" / "heads" / BRANCH
        if head.exists():
            return head.read_text().strip()
        packed = GIT_DIR / "packed-refs"
        if packed.exists():
            for line in packed.read_text().splitlines():
                if line.endswith(f"refs/heads/{BRANCH}"):
                    return line.split()[0].strip()
    except Exception as e:
        logger.warning(f"read local sha failed: {e}")
    return ""


def _fetch_github_info():
    """GitHub API:最新 commit + HEAD..latest 之间的 commit 列表"""
    headers = {"User-Agent": "tg-monitor-update-checker"}
    # 最新 commit
    url = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        latest = json.loads(r.read().decode())
    return {
        "sha": latest["sha"],
        "short": latest["sha"][:7],
        "subject": (latest["commit"]["message"] or "").splitlines()[0],
        "date": latest["commit"]["author"]["date"],
    }


def _fetch_commits_between(base_sha: str, head_sha: str, limit: int = 15):
    """GitHub compare API:base..head 之间的 commit"""
    try:
        headers = {"User-Agent": "tg-monitor-update-checker"}
        url = f"https://api.github.com/repos/{REPO}/compare/{base_sha}...{head_sha}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        out = []
        for c in data.get("commits", [])[-limit:]:
            out.append({
                "sha": c["sha"][:7],
                "subject": (c["commit"]["message"] or "").splitlines()[0],
            })
        return out
    except Exception as e:
        logger.warning(f"compare api failed: {e}")
        return []


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict):
    try:
        STATE_PATH.parent.mkdir(exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"save update state failed: {e}")


def check_once():
    """单次检查 — 返回 (has_update: bool, state: dict)
    state keys: local_sha, latest_sha, latest_subject, latest_date, new_commits,
                last_check, last_notified_sha, error
    """
    state = load_state()
    state["last_check"] = datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S")
    state.pop("error", None)

    local = _read_local_sha()
    state["local_sha"] = local
    state["local_short"] = local[:7] if local else ""

    if not local:
        state["error"] = "读不到本机 git sha"
        save_state(state)
        return False, state

    try:
        remote = _fetch_github_info()
    except urllib.error.HTTPError as e:
        state["error"] = f"GitHub API {e.code}"
        save_state(state)
        return False, state
    except Exception as e:
        state["error"] = f"GitHub API 失败: {e}"
        save_state(state)
        return False, state

    state["latest_sha"] = remote["sha"]
    state["latest_short"] = remote["short"]
    state["latest_subject"] = remote["subject"]
    state["latest_date"] = remote["date"]

    has_update = remote["sha"] != local
    state["has_update"] = has_update

    if has_update:
        state["new_commits"] = _fetch_commits_between(local, remote["sha"])
    else:
        state["new_commits"] = []

    save_state(state)
    return has_update, state


async def check_and_notify(alert_bot):
    """调 check_once,若有新版 且 本版本还没推送过 → 推 TG 通知"""
    has_update, state = check_once()
    if not has_update:
        return

    latest_sha = state.get("latest_sha", "")
    if state.get("last_notified_sha") == latest_sha:
        # 已经推过这个版本的通知了,不再推
        return

    if alert_bot:
        try:
            await alert_bot.send_update_notice(state)
            state["last_notified_sha"] = latest_sha
            state["last_notified_at"] = datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S")
            save_state(state)
            logger.info(f"update notice sent for {latest_sha[:7]}")
        except Exception as e:
            logger.error(f"update notice send failed: {e}")
