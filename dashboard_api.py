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


def _classify_heartbeat(ts_str):
    """根据上次心跳时间分级:online / warn (>10min) / dead (>4h or 无)"""
    if not ts_str:
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
            # 优先直接读 commit object
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
        accounts = conn.execute(
            "SELECT id, phone, name, username, sheet_tab, company, operator FROM accounts ORDER BY id"
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
                "heartbeat_status": _classify_heartbeat(last_msg_ts),
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
    """最近 N 条告警 (倒序),含账号 + 对方名"""
    def _q():
        rows = db.get_conn().execute(
            "SELECT a.id, a.type, a.message_text, a.created_at, a.bot_message_id, "
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
    """从 messages 表反推 sheet 写入状态"""
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
        return {
            "today_writes": today_writes,
            "pending": pending,
            "last_write": last,
            "last_write_human": _human_age(last),
            "sheet_id": (config.SHEET_ID or "")[-8:] if config.SHEET_ID else "—",
        }
    return _safe(_q, {
        "today_writes": 0, "pending": 0,
        "last_write": None, "last_write_human": "—", "sheet_id": "—",
    })


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
    online = sum(1 for a in accounts if a["heartbeat_status"] == "online")
    warn = sum(1 for a in accounts if a["heartbeat_status"] == "warn")
    dead = sum(1 for a in accounts if a["heartbeat_status"] == "dead")
    return {
        "ok": True,
        "ts": _now_bj_iso(),
        "product": "tg-monitor-template",  # 中央看板区分来源 (对齐 tg-monitor-multi)
        "system": {
            "listener": listener_status(),
            "config_version": code_version(),
            "accounts_online": online,
            "accounts_warn": warn,
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
