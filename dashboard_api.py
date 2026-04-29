"""v2.7 驾驶舱 — 只读数据聚合层

设计原则:
- 完全旁路 listener / bot / sheets,不改任何运行时代码
- 所有数据来自现有 DB (alerts / messages / accounts / peers) 的 SELECT
- container 健康通过 docker SDK 直查
- 配置快照来自 config 模块和 .env 文件 mtime

不向 DB 写任何东西,失败时 fallback 默认值,绝不抛异常给上层。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import database as db

TZ_BJ = timezone(timedelta(hours=8))


# ============ 工具 ============

def _today_bj():
    return datetime.now(TZ_BJ).strftime("%Y-%m-%d")


def _now_bj_iso():
    return datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S")


def _safe(fn, default):
    """所有聚合查询都包一层 — 失败返回 default,不让 dashboard 因为单个 card 挂掉而 500"""
    try:
        return fn()
    except Exception as e:
        # 用 print 不引日志依赖
        print(f"  ⚠️ dashboard_api {fn.__name__} 失败: {e}")
        return default


def _human_age(ts_str):
    """把 '2026-04-16 12:30:11' → '12s 前' / '4 分钟前' / '2 小时前'"""
    if not ts_str:
        return "—"
    try:
        # DB 里时间不带时区,统一当成 BJ
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_BJ)
    except Exception:
        return "—"
    delta = datetime.now(TZ_BJ) - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "刚刚"
    if secs < 60:
        return f"{secs}s 前"
    if secs < 3600:
        return f"{secs // 60} 分钟前"
    if secs < 86400:
        return f"{secs // 3600} 小时前"
    return f"{secs // 86400} 天前"


def _classify_heartbeat(ts_str, session_status=None):
    """根据上次心跳时间分级:online / warn (>10min) / dead (>4h)
    v2.10.8: 新增 waiting 状态 — session 健康但还没收到首条消息(刚登入/群里没人发言)
    不能一律 dead,会误报"""
    if not ts_str:
        # 完全没消息历史:session 健康 → 等待中(绿);异常/吊销 → 交给 session_status 判定
        if session_status in ("healthy", None):
            return "waiting"
        return "dead"
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_BJ)
    except Exception:
        return "dead"
    secs = (datetime.now(TZ_BJ) - dt).total_seconds()
    if secs < 600:        # 10 分钟内
        return "online"
    if secs < 14400:      # 4 小时内
        return "warn"
    return "dead"


# ============ 系统状态 ============

def listener_status():
    """检查 tg-monitor-{COMPANY_NAME} 容器是否在线"""
    def _check():
        import docker as docker_sdk
        client = docker_sdk.from_env()
        name = "tg-monitor-" + (config.COMPANY_NAME or "default")
        c = client.containers.get(name)
        c.reload()
        return {
            "name": name,
            "status": c.status,           # "running" / "exited" / ...
            "alive": c.status == "running",
            "started_at": c.attrs.get("State", {}).get("StartedAt", ""),
            "health": c.attrs.get("State", {}).get("Health", {}).get("Status", "—"),
        }
    return _safe(_check, {
        "name": "tg-monitor-" + (config.COMPANY_NAME or "default"),
        "status": "unknown", "alive": False,
        "started_at": "", "health": "—",
    })


def _read_git_commit_subject(git_root: Path, full_sha: str) -> str:
    """直接 zlib 解压 .git/objects/<xx>/<sha> 拿到 commit subject — 不依赖 git CLI"""
    import zlib
    if not full_sha or len(full_sha) < 4:
        return ""
    obj_dir = git_root / "objects" / full_sha[:2]
    if not obj_dir.exists():
        return ""
    matches = list(obj_dir.glob(full_sha[2:] + "*"))
    if not matches:
        return ""
    try:
        raw = zlib.decompress(matches[0].read_bytes())
        null = raw.index(b"\x00")
        body = raw[null + 1:].decode(errors="replace")
        # commit 体: "tree X\nparent X\nauthor ...\ncommitter ...\n\n<subject>\n<body>"
        parts = body.split("\n\n", 1)
        if len(parts) > 1:
            return parts[1].split("\n", 1)[0].strip()
    except Exception:
        pass
    return ""


def _deref_tag(git_root: Path, tag_sha: str) -> str:
    """annotated tag(`git tag -a`)指向 tag object,不是 commit。
    解压 tag object 从 `object <commit_sha>\\n` 行拿到真正的 commit SHA。
    lightweight tag(`git tag`)直接指 commit,传入 commit SHA 返回不了 object 头,
    那种情况调用方外层直接比 tag_sha == commit_sha 就够。"""
    import zlib
    if not tag_sha or len(tag_sha) < 4:
        return ""
    obj_path = git_root / "objects" / tag_sha[:2] / tag_sha[2:]
    if not obj_path.exists():
        return ""
    try:
        raw = zlib.decompress(obj_path.read_bytes())
        null = raw.index(b"\x00")
        body = raw[null + 1:].decode(errors="replace")
        # tag object 第一行: "object <commit_sha>"
        first_line = body.split("\n", 1)[0]
        if first_line.startswith("object "):
            return first_line.split(" ", 1)[1].strip()
    except Exception:
        pass
    return ""


def _find_tag_for_sha(git_root: Path, full_sha: str) -> str:
    """v3.0.1: 找指向 full_sha 的 tag(refs/tags/ 目录 + packed-refs)。
    同时处理 annotated tag(需要解引用 tag object 拿真 commit SHA)跟 lightweight tag
    (直接指 commit SHA)两种情况。比如 HEAD 正好是 v3.0.0 tag 指向的 commit → 返回 'v3.0.0'。
    找不到返回空串。跟 _read_git_commit_subject 互补 — pack file 里的 commit subject
    在本 container 读不到(没 git CLI + 不解析 pack),但 tag 通常是 loose refs 好读。"""
    if not full_sha:
        return ""

    def _matches(ref_sha: str) -> bool:
        """判断 ref_sha 是否(直接 或 解引用后)等于 full_sha。"""
        if ref_sha == full_sha:
            return True
        # annotated tag:解引用 tag object 拿真 commit
        commit_sha = _deref_tag(git_root, ref_sha)
        return commit_sha == full_sha

    candidates = []
    # 1. 优先 loose refs
    tags_dir = git_root / "refs" / "tags"
    if tags_dir.exists():
        try:
            for tag_path in tags_dir.iterdir():
                if tag_path.is_file():
                    try:
                        tag_sha = tag_path.read_text().strip()
                        if _matches(tag_sha):
                            candidates.append(tag_path.name)
                    except Exception:
                        continue
        except Exception:
            pass
    # 2. packed-refs
    pr = git_root / "packed-refs"
    if pr.exists():
        try:
            for line in pr.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split(" ", 1)
                if len(parts) != 2:
                    continue
                sha, ref = parts
                if ref.startswith("refs/tags/") and _matches(sha):
                    candidates.append(ref[len("refs/tags/"):])
        except Exception:
            pass
    if not candidates:
        return ""
    # 偏好 v 开头的 semver(v3.0.0 优先于 before-dashboard-push)
    candidates.sort(key=lambda t: (not t.startswith("v"), t), reverse=True)
    # reverse=True 让 v 开头的排前,且同开头里字典序大的(v3 > v2)排前
    return candidates[0]


def code_version():
    """从 /app/repo/.git 读 sha + 最近一次 commit subject。
    fallback 顺序: git object → git reflog → .env mtime → 未知"""
    def _v():
        for git_root_str in ("/app/repo/.git", str(Path(__file__).parent / ".git")):
            git_root = Path(git_root_str)
            head_path = git_root / "HEAD"
            if not head_path.exists():
                continue
            head = head_path.read_text().strip()
            full_sha = ""
            if head.startswith("ref:"):
                ref = head.split(" ", 1)[1].strip()
                ref_file = git_root / ref
                if ref_file.exists():
                    full_sha = ref_file.read_text().strip()
                else:
                    pr = git_root / "packed-refs"
                    if pr.exists():
                        for line in pr.read_text().splitlines():
                            if line.endswith(ref):
                                full_sha = line.split()[0]
                                break
            else:
                full_sha = head
            if not full_sha:
                continue
            sha = full_sha[:7]
            # v3.0.1: container 没 git CLI + pack file 读不了 → _read_git_commit_subject
            # 对 squash merge 的新 commit 会 fallback 到 reflog 拿错的老 commit subject
            # (如一直显示「v2.10.13: enable_https.sh...」即使 HEAD 是 v3.0.0)。
            # 多路径尝试取真正的版本号:
            # 1. _find_tag_for_sha 找 tag matching HEAD SHA(annotated tag 也要能解引用 —
            #    但解引用需要读 tag object,container 里如果是 pack file 读不了 → 失败)
            tag_name = _find_tag_for_sha(git_root, full_sha)
            if tag_name:
                return {"sha": sha, "subject": tag_name, "label": tag_name}
            # 2. 退路径:读 release_notes.json 拿最新业务版本号
            # release_notes.json 按时间追加 key(最新在末尾),Python 3.7+ 保留插入顺序
            rn_path = Path(__file__).parent / "release_notes.json"
            if rn_path.exists():
                try:
                    import json as _json
                    rn = _json.loads(rn_path.read_text())
                    # 拿最后一个非 _ 开头的 key(最新加的版本)
                    biz_keys = [k for k in rn.keys() if not k.startswith("_")]
                    if biz_keys:
                        latest = biz_keys[-1]
                        return {"sha": sha, "subject": latest, "label": latest}
                except Exception:
                    pass
            # 无 tag:走老路径 loose object → reflog
            subject = _read_git_commit_subject(git_root, full_sha)
            # fallback: 从 reflog 找最近一条 commit 行
            if not subject:
                log_path = git_root / "logs" / "HEAD"
                if log_path.exists():
                    try:
                        for line in reversed(log_path.read_text().splitlines()):
                            if "\t" not in line:
                                continue
                            msg = line.split("\t", 1)[1]
                            if msg.startswith("commit"):
                                subject = msg.split(":", 1)[1].strip() if ":" in msg else msg
                                break
                    except Exception:
                        pass
            return {
                "sha": sha,
                "subject": (subject or "")[:40],
                "label": sha,
            }
        # fallback: .env mtime
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            m = env_path.stat().st_mtime
            return {
                "sha": "",
                "subject": ".env 配置时间",
                "label": datetime.fromtimestamp(m, TZ_BJ).strftime("%m-%d %H:%M"),
            }
        return {"sha": "", "subject": "—", "label": "—"}
    return _safe(_v, {"sha": "", "subject": "—", "label": "—"})


# 保留旧名字别名 — 防止外部调用炸掉
def env_version():
    return code_version()


# ============ 账号矩阵 ============

def _load_session_status_for(phone):
    """v2.10.4: 读 /app/data/.session_states.json 拿某个手机号的 session 状态"""
    if not phone:
        return "unknown"
    try:
        import json
        p = Path("/app/data/.session_states.json")
        if not p.exists():
            return "unknown"
        data = json.loads(p.read_text())
        entry = data.get(phone) or {}
        return entry.get("status", "unknown")
    except Exception:
        return "unknown"


SLOT_CAPACITY = 16   # 每账号最多 16 个 peer 槽位


def accounts_matrix():
    """每个账号:基本信息 + 心跳 + 槽位占比 + 今日 in/out + 今日告警分类计数"""
    def _q():
        today = _today_bj()
        conn = db.get_conn()
        # v3.0.9: SELECT 加 tg_id / business_tg_id / owner_tg_id / remind_*_text
        # 中央台需要这些字段做 stage1/stage2 @对象反查 + 提醒模板审计
        accounts = conn.execute(
            "SELECT id, phone, name, username, sheet_tab, company, operator, "
            "       tg_id, business_tg_id, owner_tg_id, "
            "       remind_30min_text, remind_40min_text "
            "FROM accounts ORDER BY id"
        ).fetchall()
        out = []
        for a in accounts:
            aid = a["id"]
            # 心跳
            hb = conn.execute(
                "SELECT MAX(timestamp) AS last FROM messages WHERE account_id=?",
                (aid,)
            ).fetchone()
            last_msg_ts = hb["last"] if hb else None
            # 槽位占用 + 总配置上限
            slots_used = conn.execute(
                "SELECT COUNT(*) AS n FROM peers WHERE account_id=? AND col_group>=0",
                (aid,)
            ).fetchone()["n"]
            # 今日 in/out
            mrows = conn.execute(
                "SELECT direction, COUNT(*) AS n FROM messages "
                "WHERE account_id=? AND timestamp LIKE ? GROUP BY direction",
                (aid, f"{today}%")
            ).fetchall()
            today_in = today_out = 0
            for r in mrows:
                if r["direction"] == "B":
                    today_in = r["n"]
                elif r["direction"] == "A":
                    today_out = r["n"]
            # 今日告警分类
            alerts_today = conn.execute(
                "SELECT type, COUNT(*) AS n FROM alerts "
                "WHERE account_id=? AND created_at LIKE ? GROUP BY type",
                (aid, f"{today}%")
            ).fetchall()
            ac = {"keyword": 0, "no_reply": 0, "deleted": 0}
            for r in alerts_today:
                t = r["type"] or "unknown"
                ac[t] = r["n"]
            # v2.10.4: session 健康状态(由 tasks._session_health_loop 维护)
            session_status = _load_session_status_for(a["phone"] or "")
            out.append({
                "id": aid,
                "phone": a["phone"] or "",
                "session_status": session_status,
                "name": a["name"] or "—",
                "username": a["username"] or "",
                "company": a["company"] or "",
                "operator": a["operator"] or "",
                "slots": slots_used,
                "slots_used": slots_used,
                "slots_total": SLOT_CAPACITY,
                "slots_pct": int(round(slots_used * 100 / SLOT_CAPACITY)) if SLOT_CAPACITY else 0,
                "today_in": today_in,
                "today_out": today_out,
                "today_msgs": today_in + today_out,
                "alerts_today": ac,
                "last_heartbeat": last_msg_ts,
                "last_heartbeat_human": _human_age(last_msg_ts),
                "heartbeat_status": _classify_heartbeat(last_msg_ts, session_status),
                # v3.0.9: 中央台需要的额外字段(纯加,不破坏老前端)
                "tg_id": a["tg_id"],   # int 或 None,中央台用作账号唯一识别
                "business_tg_id": (a["business_tg_id"] or "").strip(),   # stage1 商务 @对象
                "owner_tg_id": (a["owner_tg_id"] or "").strip(),         # stage2 负责人 @对象
                "remind_30min_text": (a["remind_30min_text"] or "").strip(),
                "remind_40min_text": (a["remind_40min_text"] or "").strip(),
                "sheet_tab": (a["sheet_tab"] or "").strip(),
            })
        return out
    return _safe(_q, [])


# ============ 广告主活跃榜 ============

def top_peers_active(limit=5, hours=24):
    """过去 N 小时入站消息数 desc — 即「最近最爱说话的广告主」"""
    def _q():
        cutoff = (datetime.now(TZ_BJ) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        rows = db.get_conn().execute(
            "SELECT p.id AS pid, p.name AS peer_name, "
            "       COALESCE(ac.name, '') AS account_name, "
            "       COUNT(m.id) AS n, "
            "       MAX(m.timestamp) AS last_ts "
            "FROM messages m "
            "JOIN peers p ON m.peer_id = p.id "
            "LEFT JOIN accounts ac ON m.account_id = ac.id "
            "WHERE m.direction='B' AND m.timestamp >= ? "
            "GROUP BY p.id ORDER BY n DESC LIMIT ?",
            (cutoff, int(limit))
        ).fetchall()
        return [{
            "peer_id": r["pid"],
            "peer_name": r["peer_name"] or "(未命名)",
            "account_name": r["account_name"] or "—",
            "count": r["n"],
            "last_human": _human_age(r["last_ts"]),
        } for r in rows]
    return _safe(_q, [])


def top_peers_silent(limit=5, min_hours=24):
    """已配槽位但 >= min_hours 没收到入站消息的广告主 — 越久越靠前"""
    def _q():
        cutoff = (datetime.now(TZ_BJ) - timedelta(hours=min_hours)).strftime("%Y-%m-%d %H:%M:%S")
        rows = db.get_conn().execute(
            "SELECT p.id AS pid, p.name AS peer_name, "
            "       COALESCE(ac.name, '') AS account_name, "
            "       (SELECT MAX(timestamp) FROM messages "
            "        WHERE peer_id=p.id AND direction='B') AS last_in "
            "FROM peers p "
            "LEFT JOIN accounts ac ON p.account_id = ac.id "
            "WHERE p.col_group >= 0",
        ).fetchall()
        # 在 Python 端筛 — peers 表小,几十条
        out = []
        for r in rows:
            last = r["last_in"]
            # 没说过话 或 上次入站早于 cutoff
            if (not last) or (last < cutoff):
                out.append({
                    "peer_id": r["pid"],
                    "peer_name": r["peer_name"] or "(未命名)",
                    "account_name": r["account_name"] or "—",
                    "last_in": last,
                    "last_human": _human_age(last) if last else "从未说话",
                })
        # 按 last_in 升序(越早越优先,None 视为最早)
        out.sort(key=lambda x: x["last_in"] or "0000")
        return out[:int(limit)]
    return _safe(_q, [])


# ============ 消息吞吐 ============

def messages_today():
    """今日消息吞吐 — direction A=出站(我发) / B=入站(对方发) + 媒体 + 删除"""
    def _q():
        today = _today_bj()
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT direction, COUNT(*) AS n FROM messages "
            "WHERE timestamp LIKE ? GROUP BY direction",
            (f"{today}%",)
        ).fetchall()
        a_out = b_in = 0
        for r in rows:
            d = r["direction"] or ""
            if d == "A":
                a_out = r["n"]
            elif d == "B":
                b_in = r["n"]
        # 带媒体
        media = conn.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE timestamp LIKE ? AND media_type != ''",
            (f"{today}%",)
        ).fetchone()["n"]
        # 已删除(今日删除的)
        deleted = conn.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE deleted=1 AND deleted_at LIKE ?",
            (f"{today}%",)
        ).fetchone()["n"]
        return {
            "in": b_in,
            "out": a_out,
            "media": media,
            "deleted": deleted,
            "total": a_out + b_in,
        }
    return _safe(_q, {"in": 0, "out": 0, "media": 0, "deleted": 0, "total": 0})


# ============ 告警相关 ============

def alerts_today_summary():
    """今日预警分类汇总"""
    def _q():
        today = _today_bj()
        rows = db.get_conn().execute(
            "SELECT type, COUNT(*) AS n FROM alerts WHERE created_at LIKE ? GROUP BY type",
            (f"{today}%",)
        ).fetchall()
        out = {"keyword": 0, "no_reply": 0, "deleted": 0}
        for r in rows:
            t = r["type"] or "unknown"
            out[t] = r["n"]
        out["total"] = sum(out.values())
        # 推送成功 = bot_message_id 非空 (说明 bot 真的发了消息)
        sent = db.get_conn().execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE created_at LIKE ? AND bot_message_id IS NOT NULL",
            (f"{today}%",)
        ).fetchone()["n"]
        out["sent"] = sent
        out["silenced"] = out["total"] - sent
        return out
    return _safe(_q, {"keyword": 0, "no_reply": 0, "deleted": 0, "total": 0, "sent": 0, "silenced": 0})


def alerts_recent(limit=50):
    """最近 N 条告警 (倒序),含账号 + 对方名

    v3.0.9: SELECT 加 status/stage/keyword/reviewed_at/sheet_written/claimed_at/
    last_write_error + account_id/peer_id/msg_id 给中央台精确 join 和违规登记筛选用。
    新字段都是纯加,老前端读老字段不受影响。"""
    def _q():
        rows = db.get_conn().execute(
            "SELECT a.id, a.type, a.message_text, a.created_at, a.bot_message_id, "
            "       a.account_id, a.peer_id, a.msg_id, "
            "       a.status, a.stage, a.keyword, a.reviewed_at, "
            "       a.sheet_written, a.claimed_at, a.last_write_error, "
            "       COALESCE(p.name, '(未知)') AS peer_name, "
            "       COALESCE(ac.name, '(未知)') AS account_name "
            "FROM alerts a "
            "LEFT JOIN peers p ON a.peer_id = p.id "
            "LEFT JOIN accounts ac ON a.account_id = ac.id "
            "ORDER BY a.id DESC LIMIT ?",
            (int(limit),)
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "type": r["type"] or "unknown",
                "peer_name": r["peer_name"],
                "account_name": r["account_name"],
                "message_text": (r["message_text"] or "")[:80],
                "created_at": r["created_at"],
                "time_short": (r["created_at"] or "")[-8:],   # HH:MM:SS
                "pushed": bool(r["bot_message_id"]),
                # v3.0.9 中央台扩展字段
                "account_id": r["account_id"],
                "peer_id": r["peer_id"],
                "msg_id": r["msg_id"],
                "status": r["status"] or "pending",
                "stage": r["stage"] if r["stage"] is not None else 0,
                "keyword": r["keyword"] or "",
                "reviewed_at": r["reviewed_at"],
                "sheet_written": bool(r["sheet_written"]),
                "claimed_at": r["claimed_at"],
                "last_write_error": r["last_write_error"] or "",
            })
        return out
    return _safe(_q, [])


def alerts_24h_buckets():
    """过去 24 小时,按小时桶分类的三色堆叠数据 + 消息吞吐量(灰色背景)"""
    def _q():
        cutoff_dt = datetime.now(TZ_BJ) - timedelta(hours=24)
        cutoff = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
        conn = db.get_conn()
        # 告警按小时
        alert_rows = conn.execute(
            "SELECT type, created_at FROM alerts WHERE created_at >= ?",
            (cutoff,)
        ).fetchall()
        a_buckets = {}
        for r in alert_rows:
            ts = r["created_at"]
            if not ts or len(ts) < 13:
                continue
            hk = ts[:13]
            b = a_buckets.setdefault(hk, {"keyword": 0, "no_reply": 0, "deleted": 0})
            t = r["type"] or "unknown"
            if t in b:
                b[t] += 1
        # 消息按小时(只数入站,因为入站才反映「客户活跃度」)
        msg_rows = conn.execute(
            "SELECT timestamp, direction FROM messages WHERE timestamp >= ?",
            (cutoff,)
        ).fetchall()
        m_buckets = {}  # hour -> {in, out}
        for r in msg_rows:
            ts = r["timestamp"]
            if not ts or len(ts) < 13:
                continue
            hk = ts[:13]
            mb = m_buckets.setdefault(hk, {"in": 0, "out": 0})
            if r["direction"] == "B":
                mb["in"] += 1
            elif r["direction"] == "A":
                mb["out"] += 1
        # 输出 24 个连续小时
        out = []
        now = datetime.now(TZ_BJ).replace(minute=0, second=0, microsecond=0)
        for i in range(23, -1, -1):
            h = now - timedelta(hours=i)
            key = h.strftime("%Y-%m-%d %H")
            ab = a_buckets.get(key, {"keyword": 0, "no_reply": 0, "deleted": 0})
            mb = m_buckets.get(key, {"in": 0, "out": 0})
            out.append({
                "hour": h.strftime("%H:%M"),
                "kw": ab["keyword"],
                "nr": ab["no_reply"],
                "del": ab["deleted"],
                "msg_in": mb["in"],
                "msg_out": mb["out"],
                "msg_total": mb["in"] + mb["out"],
            })
        return out
    return _safe(_q, [])


# ============ Sheet / Bot 健康 ============

def sheets_health():
    """从 messages 表反推 sheet 写入状态。
    v3.0.6: 增加具体故障原因识别(OAuth 失效 / 429 限流 / Sheet 无权限),
    便于客户在驾驶舱直接看懂卡在哪一步,不用 SSH 查 log。"""
    def _q():
        today = _today_bj()
        conn = db.get_conn()
        today_writes = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE sheet_written=1 AND timestamp LIKE ?",
            (f"{today}%",)
        ).fetchone()["n"]
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE sheet_written=0",
        ).fetchone()["n"]
        last = conn.execute(
            "SELECT MAX(timestamp) AS t FROM messages WHERE sheet_written=1"
        ).fetchone()["t"]
        # v3.0.6: 诊断具体原因
        status, status_msg, action = _diagnose_sheets_stuck(pending, last)
        # v3.1 (ADR-0027): 后台 resync 状态(跨容器走 DB MAX(next_sheet_row_resynced_at))
        resync_enabled = bool(getattr(config, "SHEET_RESYNC_ENABLED", True))
        resync_interval_min = int(getattr(config, "SHEET_RESYNC_INTERVAL_MINUTES", 15))
        last_resync = db.get_max_resynced_at() if hasattr(db, "get_max_resynced_at") else None
        return {
            "today_writes": today_writes,
            "pending": pending,
            "last_write": last,
            "last_write_human": _human_age(last),
            "sheet_id": (config.SHEET_ID or "")[-8:] if config.SHEET_ID else "—",
            "status": status,          # "ok" / "warning" / "error"
            "status_msg": status_msg,  # 给客户看的白话说明
            "action": action,          # None / "reauth" / "wait" / "check_sheet"
            # v3.1 后台扫描状态(给驾驶舱看 + 中央台用)
            "resync_enabled": resync_enabled,
            "resync_interval_min": resync_interval_min,
            "last_resync": last_resync,
            "last_resync_human": _human_age(last_resync) if last_resync else "—",
        }
    return _safe(_q, {
        "today_writes": 0, "pending": 0,
        "last_write": None, "last_write_human": "—", "sheet_id": "—",
        "status": "unknown", "status_msg": "—", "action": None,
        "resync_enabled": False, "resync_interval_min": 0,
        "last_resync": None, "last_resync_human": "—",
    })


def _diagnose_sheets_stuck(pending, last_write_ts):
    """v3.0.6: 判断 Sheets 是否堵塞 + 为什么。
    策略: pending 过多 + 上次写入超过 N 分钟前 → 扫最近 tg-monitor log 找错误模式。
    无堵塞 → ("ok", "正常", None),不扫 log 省开销。"""
    from datetime import datetime as _dt
    # 阈值: 积压超 50 条 + 最近一次写入超 15 分钟前 才算"堵"
    if pending < 50:
        return "ok", "正常", None
    try:
        last_dt = _dt.strptime(last_write_ts, "%Y-%m-%d %H:%M:%S") if last_write_ts else None
    except Exception:
        last_dt = None
    if last_dt:
        now = _dt.now()
        age_min = (now - last_dt).total_seconds() / 60
        if age_min < 15:
            # 积压多但还在写,可能只是慢不是卡
            return "warning", f"积压 {pending} 条,但还在写入(最近 {int(age_min)} 分钟前)", None

    # 确认堵塞 → 扫 log
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        # v3.0.8.3 (Codex P1 修, 多部门 VPS 安全): 跟 web.py:_start_tg_monitor 同策略 —
        # 1. tg-monitor-{COMPANY_NAME} (老路径)
        # 2. 同 compose project label 的 tg-monitor service (多部门 VPS 安全)
        # 3. 全 tg-monitor-* prefix 第一个 (单部门 VPS 终极兜底)
        primary = f"tg-monitor-{config.COMPANY_NAME or 'default'}"
        c = None
        try:
            c = client.containers.get(primary)
        except docker_sdk.errors.NotFound:
            try:
                import socket as _socket
                me = client.containers.get(_socket.gethostname())
                project = (me.labels or {}).get("com.docker.compose.project", "")
                if project:
                    siblings = client.containers.list(all=True, filters={
                        "label": [
                            f"com.docker.compose.project={project}",
                            "com.docker.compose.service=tg-monitor",
                        ]
                    })
                    if siblings:
                        c = siblings[0]
            except Exception:
                pass
            if c is None:
                cands = [x for x in client.containers.list(all=True) if x.name.startswith("tg-monitor-")]
                if not cands:
                    return "warning", f"⚠ 积压 {pending} 条,无法自动诊断(找不到 tg-monitor 容器)", None
                c = cands[0]
        # 只看最近 1 小时 log,避免拉太大
        import time as _time
        since = int(_time.time() - 3600)
        log_bytes = c.logs(tail=300, since=since)
        log_text = log_bytes.decode("utf-8", errors="replace").lower()

        # OAuth 失效(最常见) — v3.0.7 改用 oauth_helper.is_oauth_failure 跟 sheets.py
        # 自愈逻辑共用同一份关键词清单,避免诊断说"OAuth 失效"但自愈识别不出。
        # log_text 已经 .lower() 过, helper 内部还会再 lower 一次(幂等)。
        import oauth_helper as _oh
        if _oh.is_oauth_failure(log_text):
            return "error", f"❌ Google 授权失效 — 积压 {pending} 条,写不进 Sheet", "reauth"

        # 429 限流 — v3.0.7.1: 加「立刻重启监听器」按钮兜底,
        # 因为 per-account 退避到 600s 后还撞 429 会卡死(yueda 案例),
        # 重启 = 退避状态全清零 + 立刻重试,客户能自助救自己。
        if any(k in log_text for k in [
            "429", "rate_limit", "quota exceeded", "user_rate_limit_exceeded", "rate limit",
        ]):
            return "warning", f"⏳ 触发 Google 配额限流,正在自愈(最长 30 分钟) — 积压 {pending} 条", "wait_or_restart"

        # v3.0.8: 关键词更精确 — 区分"分页(worksheet)被删/改"vs"整表(spreadsheet)不存在"
        # vs 通用 404/403。原 v3.0.6 关键词太松, 任何 404/403/permission denied 都被
        # 误判成 "Sheet 不存在",实际可能是 Drive 媒体上传 404 / 单一 peer col_group 写错。
        #
        # 策略:精确强信号优先匹配,通用弱信号只在拼上下文时才匹配。

        # 分页(worksheet)级别问题 — 单个账号分页被删/改
        if "worksheetnotfound" in log_text or "worksheet not found" in log_text:
            return "error", f"❌ 某账号 Sheet 分页被删或重命名 — 积压 {pending} 条", "check_sheet"

        # 整张 spreadsheet 不存在 — 强信号
        if "spreadsheet not found" in log_text or "requested entity was not found" in log_text:
            return "error", f"❌ 整个 Spreadsheet 不存在或被删 — 积压 {pending} 条", "check_sheet"

        # 写入权限被改 — 必须配 sheet/range/spreadsheet 上下文,避免 Drive 上传 403 误判
        if any(k in log_text for k in ["permission_denied", "the caller does not have permission"]) or \
           ("does not have access" in log_text and ("sheet" in log_text or "range" in log_text)):
            return "error", f"❌ Sheet 写权限被改或 OAuth scope 不足 — 积压 {pending} 条", "check_sheet"

        # 范围被锁定/受保护
        if "protected" in log_text and ("range" in log_text or "sheet" in log_text):
            return "error", f"❌ Sheet 某范围被锁定/受保护 — 积压 {pending} 条", "check_sheet"

        # 其他错误 — 通用 — v3.0.7.1+: 给「立刻重启监听器」兜底按钮,
        # 不知道具体原因但重启常常能解开卡住状态(SheetsWriter 退避卡死等)
        return "warning", f"⚠ 积压 {pending} 条 >15 分钟没写入,原因待查", "restart_monitor"
    except Exception:
        return "warning", f"⚠ 积压 {pending} 条,无法自动诊断(容器不可访问)", None


def sheets_stuck_detail():
    """v3.0.8: 后台跑 SQL 把"为什么积压不写"的明细列出来,纯 web 看,不用 SSH。

    返回 dict, 4 块:
    - per_account_unwritten: [{'account_id': N, 'phone': '+xxx', 'name': 'xxx', 'unwritten_count': K}]
    - orphan_messages: [{'account_id': N, 'count': K, 'sample_msg_ids': [...]}]
       — 有未写消息但 peer FK 拉不到 (peers 表里 id 缺失) → INNER JOIN 永远拉不出 → 永远不会 flush
    - peers_no_col_group: [{'peer_id': N, 'account_id': N, 'tg_user_id': N}]
       — peer.col_group IS NULL → _flush_account 内 col_start = None * 3 必崩
    - missing_worksheets: [{'account_id': N, 'expected_tab_name': 'xxx', 'phone': '+xxx'}]
       — accounts 表有 sheet_tab 但 spreadsheet 里没这分页 → get_or_create_sheet 撞失败

    任一非空 → 客户驾驶舱看到具体哪条卡住,有明确修复目标。
    """
    out = {
        "per_account_unwritten": [],
        "orphan_messages": [],
        "peers_no_col_group": [],
        "missing_worksheets": [],
        "errors": [],   # 自诊断本身报错,降级到诊断卡片普通信息
    }

    # 1. 各账号未写数量
    try:
        rows = db.get_conn().execute(
            "SELECT m.account_id, a.phone, a.name, COUNT(*) as c "
            "FROM messages m LEFT JOIN accounts a ON m.account_id=a.id "
            "WHERE m.sheet_written=0 GROUP BY m.account_id ORDER BY c DESC LIMIT 50"
        ).fetchall()
        for r in rows:
            out["per_account_unwritten"].append({
                "account_id": r["account_id"],
                "phone": r["phone"] or "",
                "name": r["name"] or "",
                "unwritten_count": r["c"],
            })
    except Exception as e:
        out["errors"].append(f"per_account 查询失败: {e}")

    # 2. 孤儿消息 (peer 缺失) — 这是 #2 真根因路径
    try:
        rows = db.get_conn().execute(
            "SELECT m.account_id, COUNT(*) as c, GROUP_CONCAT(m.id) as ids "
            "FROM messages m LEFT JOIN peers p ON m.peer_id=p.id "
            "WHERE m.sheet_written=0 AND p.id IS NULL "
            "GROUP BY m.account_id"
        ).fetchall()
        for r in rows:
            sample = (r["ids"] or "").split(",")[:5]
            out["orphan_messages"].append({
                "account_id": r["account_id"],
                "count": r["c"],
                "sample_msg_ids": [int(x) for x in sample if x.isdigit()],
            })
    except Exception as e:
        out["errors"].append(f"orphan 查询失败: {e}")

    # 3. col_group=NULL 的 peer (写入时会 None * 3 TypeError 崩)
    try:
        rows = db.get_conn().execute(
            "SELECT p.id, p.account_id, p.tg_id, COUNT(m.id) as msg_count "
            "FROM peers p LEFT JOIN messages m ON m.peer_id=p.id AND m.sheet_written=0 "
            "WHERE p.col_group IS NULL "
            "GROUP BY p.id"
        ).fetchall()
        for r in rows:
            out["peers_no_col_group"].append({
                "peer_id": r["id"],
                "account_id": r["account_id"],
                "tg_id": r["tg_id"],
                "stuck_message_count": r["msg_count"],
            })
    except Exception as e:
        out["errors"].append(f"col_group 查询失败: {e}")

    # 4. accounts 表有 sheet_tab 但 spreadsheet 里没这分页
    # 需要 SheetsWriter 引用 — 跨进程,所以这步走不到。改成只列出 accounts.sheet_tab 让前端
    # 跟客户在 Google Sheet 里看到的对比 (人工)。或者依赖 sheets.py 启动时 ensure_account_tabs 自动补建,
    # 这里就不做检测了避免引入跨进程依赖。
    try:
        accounts = db.get_conn().execute(
            "SELECT id, phone, name, sheet_tab FROM accounts ORDER BY id"
        ).fetchall()
        # 只列出有未写消息但 sheet_tab 为空的账号 (这些必然撞 get_or_create_sheet)
        unwritten_acc_ids = {x["account_id"] for x in out["per_account_unwritten"]}
        for a in accounts:
            if a["id"] in unwritten_acc_ids and not (a["sheet_tab"] or "").strip():
                out["missing_worksheets"].append({
                    "account_id": a["id"],
                    "phone": a["phone"] or "",
                    "name": a["name"] or "",
                    "expected_tab_name": a["name"] or a["phone"] or f"account-{a['id']}",
                })
    except Exception as e:
        out["errors"].append(f"missing_worksheets 查询失败: {e}")

    return out


def fix_orphan_messages():
    """v3.0.8: 一键修复孤儿消息 — 把 sheet_written=0 但 peer FK 失效的消息标 sheet_written=1
    (放弃,反正永远写不进 Sheet),解开 flush_pending 死循环。

    返回 dict: {ok, fixed_count, fixed_account_ids}

    Codex P0 修: 不再写 messages.last_write_error 列 (该列只在 alerts 表上有, messages
    表没有,会 UPDATE 炸)。改为 logger 记录被放弃的 message id,留追溯依据。
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        # 找出所有孤儿消息 id
        rows = db.get_conn().execute(
            "SELECT m.id, m.account_id FROM messages m "
            "LEFT JOIN peers p ON m.peer_id=p.id "
            "WHERE m.sheet_written=0 AND p.id IS NULL"
        ).fetchall()
        if not rows:
            return {"ok": True, "fixed_count": 0, "fixed_account_ids": [], "msg": "无孤儿消息"}
        ids = [r["id"] for r in rows]
        account_ids = sorted({r["account_id"] for r in rows})
        # 留 log 追溯 (避免依赖 messages 表里没有的 last_write_error 列)
        _log.warning(
            "[fix_orphan_messages] 放弃 %d 条孤儿消息 (sheet_written 0→1), "
            "account_ids=%s, message_ids 前 20 = %s",
            len(ids), account_ids, ids[:20],
        )
        # 批量标 sheet_written=1 (放弃)
        # 注意: 不能用 WHERE id IN (...) 接太长 → 分批 200
        BATCH = 200
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            db.get_conn().execute(
                f"UPDATE messages SET sheet_written=1 WHERE id IN ({placeholders})",
                chunk,
            )
        db.get_conn().commit()
        return {
            "ok": True,
            "fixed_count": len(ids),
            "fixed_account_ids": account_ids,
            "msg": f"已放弃 {len(ids)} 条孤儿消息 (account_ids: {account_ids})。详细 message id 列表已写入容器日志便于追溯",
        }
    except Exception as e:
        return {"ok": False, "msg": f"修复失败: {e}"}


def fix_peers_no_col_group():
    """v3.0.8: 一键修复 peer.col_group=NULL — 给每个 NULL peer 分配下一个空闲列组。

    分配策略: 跟 listener._next_col_group 对齐 — 看本账号已用过的最大 col_group + 1。
    返回 dict: {ok, fixed_count, fixed_peers}
    """
    try:
        nulls = db.get_conn().execute(
            "SELECT id, account_id FROM peers WHERE col_group IS NULL ORDER BY account_id, id"
        ).fetchall()
        if not nulls:
            return {"ok": True, "fixed_count": 0, "fixed_peers": [], "msg": "无 NULL col_group peer"}
        fixed = []
        for p in nulls:
            # 该账号下一个空闲列组
            row = db.get_conn().execute(
                "SELECT COALESCE(MAX(col_group), -1) + 1 AS next_g FROM peers WHERE account_id=? AND col_group IS NOT NULL",
                (p["account_id"],),
            ).fetchone()
            next_g = row["next_g"] if row and row["next_g"] is not None else 0
            db.get_conn().execute("UPDATE peers SET col_group=? WHERE id=?", (next_g, p["id"]))
            fixed.append({"peer_id": p["id"], "account_id": p["account_id"], "assigned_col_group": next_g})
        db.get_conn().commit()
        return {
            "ok": True,
            "fixed_count": len(fixed),
            "fixed_peers": fixed,
            "msg": f"已分配 {len(fixed)} 个 peer 的 col_group",
        }
    except Exception as e:
        return {"ok": False, "msg": f"修复失败: {e}"}


def container_logs(container_name, tail=200, grep=""):
    """v3.0.6: 给后台"日志查看"面板用的 — 读容器 log,可选 grep 过滤。
    安全约束: 只允许读 tg-*-<本部门> 和 tg-caddy-* 容器,不越权看其他项目。"""
    import re as _re
    # 白名单校验先于 docker 导入,保证越权请求永远被拦,跟 docker 模块可用性无关
    company = config.COMPANY_NAME or "default"
    allowed_exact = {
        f"tg-monitor-{company}",
        f"tg-web-{company}",
        f"tg-caddy-{company}",
    }
    if container_name not in allowed_exact and not _re.fullmatch(r"tg-caddy-[A-Za-z0-9_\-]+", container_name):
        return {
            "logs": "",
            "error": f"不允许查看容器 {container_name}(只能看本部门的 tg-monitor/tg-web/tg-caddy)",
            "container": container_name,
        }

    def _q():
        import docker as docker_sdk
        client = docker_sdk.from_env()
        try:
            c = client.containers.get(container_name)
        except Exception:
            return {"logs": "", "error": f"容器 {container_name} 不存在或未运行", "container": container_name}
        # 防爆: tail 夹到 10-2000
        tail_n = max(10, min(int(tail or 200), 2000))
        try:
            log_bytes = c.logs(tail=tail_n, timestamps=True)
            text = log_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            return {"logs": "", "error": f"读 log 失败: {e}", "container": container_name}
        if grep:
            kw = grep.lower()
            lines = [l for l in text.split("\n") if kw in l.lower()]
            text = "\n".join(lines) if lines else f"(无匹配 '{grep}' 的日志行)"
        return {"logs": text, "error": None, "container": container_name}
    return _safe(_q, {"logs": "", "error": "内部错误", "container": container_name})


def list_containers():
    """v3.0.6: 给前端 dropdown 用 — 列本部门相关容器。"""
    def _q():
        import docker as docker_sdk
        import re as _re
        company = config.COMPANY_NAME or "default"
        client = docker_sdk.from_env()
        out = []
        for c in client.containers.list(all=True):
            name = c.name
            if name in (f"tg-monitor-{company}", f"tg-web-{company}") or \
               _re.fullmatch(r"tg-caddy-[A-Za-z0-9_\-]+", name):
                out.append({"name": name, "status": c.status})
        # 排序: monitor → web → caddy
        def _sort_key(x):
            n = x["name"]
            if "tg-monitor-" in n: return 0
            if "tg-web-" in n: return 1
            return 2
        out.sort(key=_sort_key)
        return out
    return _safe(_q, [])


def bot_health():
    """从 alerts 表反推 bot 推送状态"""
    def _q():
        today = _today_bj()
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT type, COUNT(*) AS n FROM alerts "
            "WHERE created_at LIKE ? AND bot_message_id IS NOT NULL "
            "GROUP BY type",
            (f"{today}%",)
        ).fetchall()
        sent = {"keyword": 0, "no_reply": 0, "deleted": 0}
        for r in rows:
            t = r["type"] or "unknown"
            if t in sent:
                sent[t] = r["n"]
        last = conn.execute(
            "SELECT MAX(created_at) AS t FROM alerts WHERE bot_message_id IS NOT NULL"
        ).fetchone()["t"]
        return {
            "today_keyword_pushed": sent["keyword"],
            "today_no_reply_pushed": sent["no_reply"],
            "today_delete_pushed": sent["deleted"],
            "last_push": last,
            "last_push_human": _human_age(last),
            "bot_token_tail": (config.BOT_TOKEN or "")[-4:] if config.BOT_TOKEN else "—",
            "alert_group_id": str(config.ALERT_GROUP_ID) if config.ALERT_GROUP_ID else "—",
        }
    return _safe(_q, {
        "today_keyword_pushed": 0, "today_no_reply_pushed": 0, "today_delete_pushed": 0,
        "last_push": None, "last_push_human": "—",
        "bot_token_tail": "—", "alert_group_id": "—",
    })


# ============ 配置快照 ============

def config_snapshot():
    """直接从 config 模块读 — 依赖 web 进程已加载的内存值"""
    def _q():
        return {
            "company_display": config.COMPANY_DISPLAY or "",
            "company_name": config.COMPANY_NAME or "",
            "peer_role_label": config.PEER_ROLE_LABEL or "广告主",
            "operator_label": config.OPERATOR_LABEL or "商务人员",
            "keywords": list(config.KEYWORDS or []),
            "no_reply_minutes": int(getattr(config, "NO_REPLY_MINUTES", 30)),
            "patrol_days": int(getattr(config, "PATROL_DAYS", 7)),
            "history_days": int(getattr(config, "HISTORY_DAYS", 2)),
            "media_enabled": bool(getattr(config, "MEDIA_FOLDER_ID", "")),
            "media_retention_days": int(getattr(config, "MEDIA_RETENTION_DAYS", 0)),
        }
    return _safe(_q, {
        "company_display": "", "company_name": "",
        "peer_role_label": "广告主", "operator_label": "商务人员",
        "keywords": [], "no_reply_minutes": 30,
        "patrol_days": 7, "history_days": 2,
        "media_enabled": False, "media_retention_days": 0,
    })


# ============ 整合接口 ============

def snapshot():
    """单一聚合接口 — 给前端一次性 fetch 全部 dashboard 数据"""
    accounts = accounts_matrix()
    # v2.10.9: KPI 看「连线健康度」(session), 不是「消息活跃度」(heartbeat)
    # 群里没人发言不代表账号掛了 → 以 session_status 为主 KPI,heartbeat 降为副信息
    connected = sum(1 for a in accounts if a.get("session_status") == "healthy")
    revoked   = sum(1 for a in accounts if a.get("session_status") == "revoked")
    # 活跃度细分(副标信息用,不影响 X/Y 主 KPI)
    active   = sum(1 for a in accounts if a["heartbeat_status"] == "online")
    slow     = sum(1 for a in accounts if a["heartbeat_status"] == "warn")
    waiting  = sum(1 for a in accounts if a["heartbeat_status"] == "waiting")
    # silent = session 好但 >4h 无消息 (群真安静,非账号问题)
    silent   = sum(1 for a in accounts if a["heartbeat_status"] == "dead" and a.get("session_status") != "revoked")
    # 向后兼容字段(旧前端读这几个)
    online = active
    warn   = slow
    dead   = revoked   # v2.10.9 起 dead 只等于 revoked
    return {
        "ok": True,
        "ts": _now_bj_iso(),
        "product": "tg-monitor-template",  # 中央看板区分来源 (对齐 tg-monitor-multi)
        "system": {
            "listener": listener_status(),
            "config_version": code_version(),
            # v2.10.9 新字段(连线为主)
            "accounts_connected": connected,
            "accounts_revoked":   revoked,
            "accounts_active":    active,
            "accounts_slow":      slow,
            "accounts_silent":    silent,
            # 向后兼容(老 dashboard.html 还会读这几个)
            "accounts_online": online,
            "accounts_warn": warn,
            "accounts_waiting": waiting,
            "accounts_dead": dead,
            "accounts_total": len(accounts),
            "alert_switches": {
                "keyword":  bool(getattr(config, "ALERT_KEYWORD_ENABLED", True)),
                "no_reply": bool(getattr(config, "ALERT_NO_REPLY_ENABLED", True)),
                "delete":   bool(getattr(config, "ALERT_DELETE_ENABLED", True)),
            },
            "daily_report_enabled": bool(getattr(config, "DAILY_REPORT_ENABLED", True)),
        },
        "alerts_today": alerts_today_summary(),
        "messages_today": messages_today(),
        "accounts": accounts,
        "alerts_recent": alerts_recent(50),
        "alerts_24h": alerts_24h_buckets(),
        "top_active": top_peers_active(5, 24),
        "top_silent": top_peers_silent(5, 24),
        "sheets": sheets_health(),
        "bot": bot_health(),
        "config": config_snapshot(),
        "update": _update_info(),
    }


# ============================================================
# v3.0.9 中央台扩展查询 — 4 个新接口的查询函数
# ============================================================
# 设计原则(v3.0.9 Codex P1 修):
# 1. 纯只读,共用现有 db.get_conn() 连接,跟主业务 listener/sheets/bot 完全解耦
# 2. 所有 limit 都 clamp 到硬上限(防滥用 + 防整表扫)
# 3. 日期参数走 LIKE / >= 索引友好,不用 strftime 函数防 index 退化
# 4. **失败大声**:非法用户输入 → ValueError(endpoint 转 400);DB 异常往上抛(endpoint 转 500)
#    不再 _safe() 静默吞 → 防"中央台脚本拼错参数,服务端返空数组,客户端误判'今天 0 条违规'"
# 5. messages_filtered 必填 account_id+peer_id 用 ValueError 硬失败,不静默退化

import re as _re_v309
from datetime import datetime as _dt_v309

# 硬上限(防滥用)
_MAX_ALERTS_LIMIT   = 1000
_MAX_PEERS_LIMIT    = 5000   # 单部门 peers 上限大约 1500,5000 留 buffer
_MAX_MESSAGES_LIMIT = 1000

# 严格 YYYY-MM-DD 正则(防 '2026-99-99' / '2026-04-01junk' 之类伪日期)
_DATE_RE_V309 = _re_v309.compile(r"^\d{4}-\d{2}-\d{2}$")

# 字段白名单(集中维护,endpoint + 查询函数共用,一致性)
_VALID_ALERT_STATUS = ("pending", "approved", "rejected", "violation_logged",
                       "cancelled", "handled_by_reply", "silenced")
_VALID_ALERT_TYPE   = ("no_reply", "deleted", "keyword")
_VALID_ALERT_STAGE  = (0, 1, 2)


def _clamp_int(val, default, hi, lo=1):
    """把 val 转 int 并 clamp 到 [lo, hi],非法值(None / 'abc' / 空字符串)返 default。

    注意:这只用在 limit/offset 这种"非法值降级"的语义合理的参数上。
    必填参数(account_id / peer_id)走 _require_int 硬失败。
    """
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _require_int(val, name):
    """必填 int 校验 — 非法直接 ValueError,不降级。

    用在 messages_filtered(account_id, peer_id) 这种"防全表扫的关键过滤参数"。
    """
    try:
        return int(val)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是合法 int")


def _parse_date(d, name):
    """严格 YYYY-MM-DD 解析。空 / None → None;非法格式 → ValueError。

    用 datetime.strptime 真正解析(防 2026-99-99 之类伪日期),不只是 regex 匹配。
    返回标准化 'YYYY-MM-DD' 字符串供 SQL 范围比较。
    """
    if d is None or d == "":
        return None
    if not isinstance(d, str):
        raise ValueError(f"{name} 必须是 YYYY-MM-DD 字符串")
    if not _DATE_RE_V309.match(d):
        raise ValueError(f"{name} 格式必须是 YYYY-MM-DD")
    try:
        _dt_v309.strptime(d, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{name} 不是合法日期")
    return d


def _validate_choice(val, choices, name, optional=True):
    """白名单校验。optional=True 时空值返 None,非空但不在 choices 里报 ValueError。"""
    if val is None or val == "":
        if optional:
            return None
        raise ValueError(f"{name} 必填")
    if val not in choices:
        raise ValueError(f"{name} 必须是 {choices} 之一")
    return val


def violations(from_date=None, to_date=None, alert_type=None):
    """v3.0.9: 违规登记明细 — status='violation_logged' 的 alert 列表。

    给中央台日/周/月违规报表用。包含部门(dept) / 账号 / 客户 / 消息 / 类型 / stage /
    创建 / 审核时间 / owner。

    参数:
      from_date: 'YYYY-MM-DD'(可选),默认今天
      to_date:   'YYYY-MM-DD'(可选),默认今天
      alert_type: 'no_reply' | 'deleted' | 'keyword'(可选过滤)

    异常(v3.0.9 Codex P1 改 — 失败大声):
      ValueError: 参数格式错误(endpoint 转 400)
      其它异常: DB 故障(endpoint 转 500,不静默吞)

    返回字段(对齐 ADR-0026 中央台报表契约):
      id / dept / account_name / account_phone / peer_name / type / stage /
      message_text / created_at / reviewed_at / owner_tg_id / business_tg_id /
      keyword / status / msg_id / peer_id / account_id / peer_username
    """
    today = _today_bj()
    f_clean = _parse_date(from_date, "from") or today
    t_clean = _parse_date(to_date, "to") or today
    a_type  = _validate_choice(alert_type, _VALID_ALERT_TYPE, "type")

    f_low  = f_clean
    t_high = t_clean + "Z"  # 'YYYY-MM-DDZ' 比 'YYYY-MM-DD 23:59:59' 字典序大

    sql = (
        "SELECT a.id, a.type, a.message_text, a.created_at, a.reviewed_at, "
        "       a.stage, a.keyword, a.account_id, a.peer_id, a.msg_id, "
        "       a.status, "
        "       COALESCE(p.name, '(未知)')  AS peer_name, "
        "       COALESCE(p.username, '')   AS peer_username, "
        "       COALESCE(ac.name, '(未知)') AS account_name, "
        "       COALESCE(ac.phone, '')     AS account_phone, "
        "       COALESCE(ac.company, '')   AS account_company, "
        "       COALESCE(ac.business_tg_id, '') AS business_tg_id, "
        "       COALESCE(ac.owner_tg_id, '')    AS owner_tg_id "
        "FROM alerts a "
        "LEFT JOIN peers p   ON a.peer_id = p.id "
        "LEFT JOIN accounts ac ON a.account_id = ac.id "
        "WHERE a.status = 'violation_logged' "
        "  AND a.created_at >= ? AND a.created_at < ? "
    )
    params = [f_low, t_high]
    if a_type:
        sql += " AND a.type = ? "
        params.append(a_type)
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(_MAX_ALERTS_LIMIT)

    rows = db.get_conn().execute(sql, tuple(params)).fetchall()
    out = []
    for r in rows:
        company = r["account_company"]
        out.append({
            "id": r["id"],
            "type": r["type"] or "unknown",
            "stage": r["stage"] if r["stage"] is not None else 0,
            "status": r["status"],
            "keyword": r["keyword"] or "",
            "message_text": r["message_text"] or "",
            "created_at": r["created_at"],
            "reviewed_at": r["reviewed_at"],
            "account_id": r["account_id"],
            "account_name": r["account_name"],
            "account_phone": r["account_phone"],
            "account_company": company,         # 老字段名(向后兼容)
            "dept": company,                    # ADR-0026 契约字段(中央台报表使用)
            "peer_id": r["peer_id"],
            "peer_name": r["peer_name"],
            "peer_username": r["peer_username"],
            "msg_id": r["msg_id"],
            "business_tg_id": r["business_tg_id"],
            "owner_tg_id": r["owner_tg_id"],
        })
    return out


def alerts_filtered(from_date=None, to_date=None, status=None, stage=None,
                    alert_type=None, limit=200, offset=0):
    """v3.0.9: 通用 alerts 查询 — 多条件筛选 + 分页。

    跟 violations() 区别:violations 只返 status='violation_logged';alerts_filtered
    可以筛任何 status / stage / type 组合,给中央台做趋势分析 / 状态流转分析。

    异常(v3.0.9 Codex P1 改):非法参数 ValueError(→400);DB 异常上抛(→500)。
    limit/offset 用 _clamp_int 软降级是合理的(数值范围语义,不影响安全)。
    """
    f_clean = _parse_date(from_date, "from")
    t_clean = _parse_date(to_date, "to")
    s_clean = _validate_choice(status, _VALID_ALERT_STATUS, "status")
    a_clean = _validate_choice(alert_type, _VALID_ALERT_TYPE, "type")

    # stage 是数值白名单,空值放过,非空必须在 (0,1,2)
    stage_clean = None
    if stage is not None and stage != "":
        try:
            stage_int = int(stage)
        except (TypeError, ValueError):
            raise ValueError("stage 必须是 0 / 1 / 2")
        if stage_int not in _VALID_ALERT_STAGE:
            raise ValueError("stage 必须是 0 / 1 / 2")
        stage_clean = stage_int

    sql_parts = [
        "SELECT a.id, a.type, a.message_text, a.created_at, a.reviewed_at, "
        "       a.bot_message_id, a.account_id, a.peer_id, a.msg_id, "
        "       a.status, a.stage, a.keyword, a.sheet_written, "
        "       a.claimed_at, a.last_write_error, "
        "       COALESCE(p.name, '(未知)')  AS peer_name, "
        "       COALESCE(p.username, '')   AS peer_username, "
        "       COALESCE(ac.name, '(未知)') AS account_name, "
        "       COALESCE(ac.phone, '')     AS account_phone, "
        "       COALESCE(ac.company, '')   AS account_company "
        "FROM alerts a "
        "LEFT JOIN peers p   ON a.peer_id = p.id "
        "LEFT JOIN accounts ac ON a.account_id = ac.id "
        "WHERE 1=1 "
    ]
    params = []
    if f_clean:
        sql_parts.append("AND a.created_at >= ? ")
        params.append(f_clean)
    if t_clean:
        sql_parts.append("AND a.created_at < ? ")
        params.append(t_clean + "Z")
    if s_clean:
        sql_parts.append("AND a.status = ? ")
        params.append(s_clean)
    if a_clean:
        sql_parts.append("AND a.type = ? ")
        params.append(a_clean)
    if stage_clean is not None:
        sql_parts.append("AND a.stage = ? ")
        params.append(stage_clean)

    lim = _clamp_int(limit, 200, _MAX_ALERTS_LIMIT)
    off = _clamp_int(offset, 0, 10**6, lo=0)
    sql_parts.append("ORDER BY a.id DESC LIMIT ? OFFSET ?")
    params.extend([lim, off])

    sql = "".join(sql_parts)
    rows = db.get_conn().execute(sql, tuple(params)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "type": r["type"] or "unknown",
            "status": r["status"] or "pending",
            "stage": r["stage"] if r["stage"] is not None else 0,
            "keyword": r["keyword"] or "",
            "message_text": r["message_text"] or "",
            "created_at": r["created_at"],
            "reviewed_at": r["reviewed_at"],
            "claimed_at": r["claimed_at"],
            "sheet_written": bool(r["sheet_written"]),
            "last_write_error": r["last_write_error"] or "",
            "bot_message_id": r["bot_message_id"],
            "account_id": r["account_id"],
            "account_name": r["account_name"],
            "account_phone": r["account_phone"],
            "account_company": r["account_company"],
            "dept": r["account_company"],   # ADR-0026 中央台契约字段
            "peer_id": r["peer_id"],
            "peer_name": r["peer_name"],
            "peer_username": r["peer_username"],
            "msg_id": r["msg_id"],
        })
    return {
        "items": out,
        "limit": lim,
        "offset": off,
        "returned": len(out),
    }


def peers_all(account_id=None, group=None):
    """v3.0.9: 监控聊天(peers)全表 — 中央台需要看完整聊天列表,不止 top10。

    参数:
      account_id: 可选,过滤单账号的 peers(传非法值 → ValueError 防静默退化成全表查)
      group:      可选,过滤特定分组(col_group 整数,同上)

    无分页设计:单账号 peers 一般 ≤200,全部门也最多几千,JSON 几百 KB 可接受。

    异常(v3.0.9 Codex P1 改):非法 account_id/group 直接 ValueError(→400),
    不再静默 pass — 防"中央台脚本拼错 account_id 拼成 'abc',服务端忽略过滤
    返全部门数据"的安全风险。
    """
    sql_parts = [
        "SELECT p.id, p.tg_id, p.account_id, p.name, p.username, p.col_group, "
        "       COALESCE(ac.name, '')  AS account_name, "
        "       COALESCE(ac.phone, '') AS account_phone "
        "FROM peers p "
        "LEFT JOIN accounts ac ON p.account_id = ac.id "
        "WHERE 1=1 "
    ]
    params = []
    if account_id is not None and account_id != "":
        try:
            aid = int(account_id)
        except (TypeError, ValueError):
            raise ValueError("account_id 必须是合法 int")
        params.append(aid)
        sql_parts.append("AND p.account_id = ? ")
    if group is not None and group != "":
        try:
            grp = int(group)
        except (TypeError, ValueError):
            raise ValueError("group 必须是合法 int")
        params.append(grp)
        sql_parts.append("AND p.col_group = ? ")

    sql_parts.append("ORDER BY p.account_id, p.col_group, p.id LIMIT ?")
    params.append(_MAX_PEERS_LIMIT)

    rows = db.get_conn().execute("".join(sql_parts), tuple(params)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "tg_id": r["tg_id"],
            "account_id": r["account_id"],
            "account_name": r["account_name"],
            "account_phone": r["account_phone"],
            "name": r["name"] or "",
            "username": r["username"] or "",
            "col_group": r["col_group"] if r["col_group"] is not None else -1,
        })
    return {
        "items": out,
        "returned": len(out),
        "limit": _MAX_PEERS_LIMIT,
    }


def messages_filtered(account_id, peer_id, from_date=None, to_date=None,
                      deleted_only=False, limit=500, offset=0):
    """v3.0.9: 消息明细查询 — 强制 account_id+peer_id 必填防 messages 整表扫。

    单 (account_id, peer_id) 对一般几千条;加日期范围更小。SQLite 走
    UNIQUE(msg_id, account_id) + idx_msg_peer 索引,毫秒返回。

    异常(v3.0.9 Codex P1 改 — 失败大声):
      ValueError: account_id/peer_id 缺失或非法 int(防静默退化绕过整表扫保护)
                  或 from/to 日期格式错误
      其它异常: DB 故障 → 上抛(endpoint 转 500)

    返回字段:
      id / msg_id / direction / text / media_type / timestamp / sheet_row /
      sheet_written / deleted / deleted_at / delete_mark_pending /
      media_seq / archive_msg_id
    """
    # 必填校验 — 走 _require_int 硬失败,不静默
    aid = _require_int(account_id, "account_id")
    pid = _require_int(peer_id, "peer_id")

    f_clean = _parse_date(from_date, "from")
    t_clean = _parse_date(to_date, "to")

    sql_parts = [
        "SELECT id, msg_id, direction, text, media_type, timestamp, "
        "       sheet_row, sheet_written, deleted, deleted_at, "
        "       delete_mark_pending, media_seq, archive_msg_id "
        "FROM messages "
        "WHERE account_id = ? AND peer_id = ? "
    ]
    params = [aid, pid]
    if f_clean:
        sql_parts.append("AND timestamp >= ? ")
        params.append(f_clean)
    if t_clean:
        sql_parts.append("AND timestamp < ? ")
        params.append(t_clean + "Z")
    if deleted_only:
        sql_parts.append("AND deleted = 1 ")

    lim = _clamp_int(limit, 500, _MAX_MESSAGES_LIMIT)
    off = _clamp_int(offset, 0, 10**6, lo=0)
    sql_parts.append("ORDER BY id DESC LIMIT ? OFFSET ?")
    params.extend([lim, off])

    rows = db.get_conn().execute("".join(sql_parts), tuple(params)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "msg_id": r["msg_id"],
            "direction": r["direction"],
            "text": r["text"] or "",
            "media_type": r["media_type"] or "",
            "timestamp": r["timestamp"],
            "sheet_row": r["sheet_row"] if r["sheet_row"] is not None else 0,
            "sheet_written": bool(r["sheet_written"]),
            "deleted": bool(r["deleted"]),
            "deleted_at": r["deleted_at"],
            "delete_mark_pending": bool(r["delete_mark_pending"]) if r["delete_mark_pending"] is not None else False,
            "media_seq": r["media_seq"] if r["media_seq"] is not None else 0,
            "archive_msg_id": r["archive_msg_id"] if r["archive_msg_id"] is not None else 0,
        })
    return {
        "items": out,
        "limit": lim,
        "offset": off,
        "returned": len(out),
    }


def _update_info():
    """v2.9.0: 版本更新信息(从 update_checker 状态文件读,中央台也能看到)"""
    try:
        import update_checker
        state = update_checker.load_state()
        return {
            "has_update": bool(state.get("has_update")),
            "local_short": state.get("local_short", ""),
            "latest_short": state.get("latest_short", ""),
            "latest_subject": state.get("latest_subject", ""),
            "new_commits": state.get("new_commits", []),
            "last_check": state.get("last_check", ""),
            "error": state.get("error", ""),
        }
    except Exception:
        return {"has_update": False}
