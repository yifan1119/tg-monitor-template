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
REPO_ROOT = Path("/app/repo")
TZ_BJ = timezone(timedelta(hours=8))
RELEASE_NOTES_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/release_notes.json"


def _fetch_release_notes():
    """从 GitHub raw 拉最新白话说明 — 本地可能还是旧的,remote 才是权威"""
    try:
        headers = {"User-Agent": "tg-monitor-update-checker"}
        req = urllib.request.Request(RELEASE_NOTES_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.warning(f"fetch release_notes failed: {e}")
    # fallback:读本地的
    try:
        f = REPO_ROOT / "release_notes.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _notes_for(short_sha: str, all_notes: dict, commit_subject: str = "") -> dict:
    """给一个 commit 短 sha,拿白话说明;没有就从 commit subject 尝试匹配版本号"""
    if short_sha in all_notes:
        return all_notes[short_sha]
    # 从 commit subject 找 v2.x.x 这种版本号
    import re
    m = re.search(r"v\d+\.\d+\.\d+", commit_subject or "")
    if m and m.group(0) in all_notes:
        return all_notes[m.group(0)]
    meta = all_notes.get("_meta", {})
    return {
        "title": "📦 常规更新",
        "body": meta.get("fallback_text", "本次是内部优化,不影响现有功能。"),
    }


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
        commits = _fetch_commits_between(local, remote["sha"])
        notes = _fetch_release_notes()
        # 给每条 commit 挂上白话说明
        for c in commits:
            n = _notes_for(c["sha"], notes, c.get("subject", ""))
            c["user_title"] = n.get("title", "")
            c["user_body"] = n.get("body", "")
        # 顶层也挂一个"最新版"的白话说明(给 TG 推送主标题用)
        latest_note = _notes_for(remote["short"], notes, remote["subject"])
        state["latest_user_title"] = latest_note.get("title", "")
        state["latest_user_body"] = latest_note.get("body", "")
        state["new_commits"] = commits
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
