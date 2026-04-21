"""SQLite 数据库 — 消息存储、对话追踪、预警记录"""
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from config import DB_PATH, TIMEZONE

TZ_BJ = timezone(timedelta(hours=8))

_local = threading.local()


def get_conn():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        # v2.10.23: SQLite 并发锁等待 5 秒(避免多协程/多进程写冲突直接抛 locked)
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


def init_db():
    conn = get_conn()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL UNIQUE,
            name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            tg_id INTEGER,
            company TEXT DEFAULT '',
            operator TEXT DEFAULT '',
            sheet_tab TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS peers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            col_group INTEGER DEFAULT -1,
            UNIQUE(tg_id, account_id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            text TEXT DEFAULT '',
            media_type TEXT DEFAULT '',
            timestamp TEXT NOT NULL,
            sheet_row INTEGER DEFAULT 0,
            sheet_written INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0,
            deleted_at TEXT,
            UNIQUE(msg_id, account_id),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(peer_id) REFERENCES peers(id)
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            peer_id INTEGER,
            msg_id INTEGER,
            message_text TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            bot_message_id INTEGER,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS idx_msg_account ON messages(account_id);
        CREATE INDEX IF NOT EXISTS idx_msg_peer ON messages(peer_id);
        CREATE INDEX IF NOT EXISTS idx_msg_deleted ON messages(deleted);
        CREATE INDEX IF NOT EXISTS idx_msg_sheet ON messages(sheet_written);
        CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
    """)
    # v2.10.23: 显式 migration 框架(幂等,按 user_version 增量升级)
    _run_migrations(conn)
    conn.commit()


def _run_migrations(conn):
    """v2.10.23: 显式 migration — 基于 PRAGMA user_version 幂等升级。
    每个增量迁移只跑一次,跑完把 user_version 顶上去。回滚不删列(nullable 保留)。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    if version < 1:
        _migrate_to_1(conn)
        conn.execute("PRAGMA user_version=1")


def _migrate_to_1(conn):
    """v2.10.23 → user_version=1:
    - messages 加 delete_mark_pending(删除时序修复:消息写 Sheet 前被删 → 写完立刻补标红)
    """
    _safe_add_column(conn, "messages", "delete_mark_pending", "INTEGER DEFAULT 0")


def _safe_add_column(conn, table, column, definition):
    """幂等 ADD COLUMN — 列已存在则跳过,避免 migration 重复跑炸"""
    existing = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column in existing:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def now_bj():
    return datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S")


# ===== 账号 =====

def upsert_account(phone, name="", username="", tg_id=None, company="", operator=""):
    """v2.10.23:ON CONFLICT 只更新 TG 身份字段(name/username/tg_id),
    不再覆盖业务字段(company/operator)— 避免 listener 启动登录时空值覆盖客户填的业务配置。

    业务字段更新走 update_account_business(account_id, company=..., operator=...)。
    INSERT 时(第一次创建)仍然会写入 company/operator 初始值。"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO accounts (phone, name, username, tg_id, company, operator)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            name=excluded.name, username=excluded.username, tg_id=excluded.tg_id
    """, (phone, name, username, tg_id, company, operator))
    conn.commit()
    return conn.execute("SELECT * FROM accounts WHERE phone=?", (phone,)).fetchone()


def update_account_business(account_id, company=None, operator=None):
    """v2.10.23:单独更新业务字段 — 只有显式传入(非 None)的字段会被更新。
    给 Web 后台 / 配置页用,listener 启动登录路径不要调这个。"""
    sets = []
    vals = []
    if company is not None:
        sets.append("company=?")
        vals.append(company)
    if operator is not None:
        sets.append("operator=?")
        vals.append(operator)
    if not sets:
        return
    vals.append(account_id)
    conn = get_conn()
    conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", tuple(vals))
    conn.commit()


def get_account_by_tg_id(tg_id):
    return get_conn().execute("SELECT * FROM accounts WHERE tg_id=?", (tg_id,)).fetchone()


def get_account_by_phone(phone):
    return get_conn().execute("SELECT * FROM accounts WHERE phone=?", (phone,)).fetchone()


def get_all_accounts():
    return get_conn().execute("SELECT * FROM accounts").fetchall()


# ===== 对话对象 (Peers / 广告主) =====

def upsert_peer(tg_id, account_id, name="", username=""):
    conn = get_conn()
    conn.execute("""
        INSERT INTO peers (tg_id, account_id, name, username)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tg_id, account_id) DO UPDATE SET
            name=excluded.name, username=excluded.username
    """, (tg_id, account_id, name, username))
    conn.commit()
    return conn.execute(
        "SELECT * FROM peers WHERE tg_id=? AND account_id=?", (tg_id, account_id)
    ).fetchone()


def get_peer(tg_id, account_id):
    return get_conn().execute(
        "SELECT * FROM peers WHERE tg_id=? AND account_id=?", (tg_id, account_id)
    ).fetchone()


def get_peers_by_account(account_id):
    return get_conn().execute(
        "SELECT * FROM peers WHERE account_id=? ORDER BY col_group", (account_id,)
    ).fetchall()


def assign_peer_col_group(peer_id, col_group):
    conn = get_conn()
    conn.execute("UPDATE peers SET col_group=? WHERE id=?", (col_group, peer_id))
    conn.commit()


def get_next_col_group(account_id):
    row = get_conn().execute(
        "SELECT COALESCE(MAX(col_group), -1) + 1 AS next_col FROM peers WHERE account_id=? AND col_group >= 0",
        (account_id,)
    ).fetchone()
    return row["next_col"]


# ===== 消息 =====

def insert_message(msg_id, account_id, peer_id, direction, text, media_type="", timestamp=None):
    conn = get_conn()
    ts = timestamp or now_bj()
    try:
        conn.execute("""
            INSERT INTO messages (msg_id, account_id, peer_id, direction, text, media_type, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (msg_id, account_id, peer_id, direction, text, media_type, ts))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # 已存在


def get_message(msg_id, account_id):
    return get_conn().execute(
        "SELECT * FROM messages WHERE msg_id=? AND account_id=?", (msg_id, account_id)
    ).fetchone()


def get_unwritten_messages():
    """[Legacy] 全局 LIMIT 500 — 单账号失败会拖死全部,v2.10.23 起 sheets.py 改用
    get_unwritten_messages_by_account 分桶取,保留这个函数是为了向后兼容。"""
    return get_conn().execute(
        "SELECT m.*, p.col_group, a.phone FROM messages m "
        "JOIN peers p ON m.peer_id = p.id "
        "JOIN accounts a ON m.account_id = a.id "
        "WHERE m.sheet_written = 0 ORDER BY m.timestamp LIMIT 500"
    ).fetchall()


def get_unwritten_messages_by_account(account_id, limit=100):
    """v2.10.23:按账号分桶取未写消息 — 每账号独立 LIMIT,
    单账号失败不会卡住别的账号的队列(修 Sheets 空白 Critical bug)。"""
    return get_conn().execute(
        "SELECT m.*, p.col_group, a.phone FROM messages m "
        "JOIN peers p ON m.peer_id = p.id "
        "JOIN accounts a ON m.account_id = a.id "
        "WHERE m.sheet_written = 0 AND m.account_id = ? "
        "ORDER BY m.timestamp LIMIT ?",
        (account_id, limit)
    ).fetchall()


def get_accounts_with_unwritten():
    """v2.10.23:返回有未写消息的所有 account_id(供 sheets.py 遍历分桶)"""
    return [r[0] for r in get_conn().execute(
        "SELECT DISTINCT account_id FROM messages WHERE sheet_written = 0"
    ).fetchall()]


def count_unwritten_older_than(minutes=10):
    """v2.10.23:数超过 N 分钟还没写进 Sheet 的消息 — 供积压告警用"""
    cutoff = (datetime.now(TZ_BJ) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    row = get_conn().execute(
        "SELECT COUNT(*) FROM messages WHERE sheet_written = 0 AND timestamp <= ?",
        (cutoff,)
    ).fetchone()
    return row[0]


def mark_written(msg_db_id, sheet_row):
    conn = get_conn()
    conn.execute(
        "UPDATE messages SET sheet_written=1, sheet_row=? WHERE id=?",
        (sheet_row, msg_db_id)
    )
    conn.commit()


def check_delete_mark_pending(msg_db_id):
    """v2.10.23:写完 Sheet 后检查这条消息是不是同时被删(sheet_row 设好之前删的),
    返回 True 表示 sheets.py 需要补调 mark_deleted_in_sheet 标红删除线。"""
    row = get_conn().execute(
        "SELECT delete_mark_pending, deleted FROM messages WHERE id=?", (msg_db_id,)
    ).fetchone()
    return bool(row and row["delete_mark_pending"] == 1 and row["deleted"] == 1)


def clear_delete_mark_pending(msg_db_id):
    conn = get_conn()
    conn.execute("UPDATE messages SET delete_mark_pending=0 WHERE id=?", (msg_db_id,))
    conn.commit()


def mark_deleted(msg_id, account_id):
    """v2.10.23:如果消息还没写进 Sheet(sheet_written=0),标 delete_mark_pending=1 —
    让后续 mark_written 调 Sheet 写入后立刻补调 mark_deleted_in_sheet 标红。
    以前这种时序下删除检测彻底失效,UI 看不到删除线。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT sheet_written FROM messages WHERE msg_id=? AND account_id=?",
        (msg_id, account_id)
    ).fetchone()
    if row is None:
        return
    if row["sheet_written"] == 0:
        conn.execute(
            "UPDATE messages SET deleted=1, deleted_at=?, delete_mark_pending=1 "
            "WHERE msg_id=? AND account_id=?",
            (now_bj(), msg_id, account_id)
        )
    else:
        conn.execute(
            "UPDATE messages SET deleted=1, deleted_at=? WHERE msg_id=? AND account_id=?",
            (now_bj(), msg_id, account_id)
        )
    conn.commit()


def get_recent_messages(account_id, days=7):
    cutoff = (datetime.now(TZ_BJ) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return get_conn().execute(
        "SELECT * FROM messages WHERE account_id=? AND timestamp>=? AND deleted=0 ORDER BY timestamp",
        (account_id, cutoff)
    ).fetchall()


def get_peer_messages(peer_id, limit=500):
    return get_conn().execute(
        "SELECT * FROM messages WHERE peer_id=? ORDER BY timestamp", (peer_id,)
    ).fetchall()


def get_last_message_by_peer(peer_id):
    """获取某对话最后一条消息"""
    return get_conn().execute(
        "SELECT * FROM messages WHERE peer_id=? AND deleted=0 ORDER BY timestamp DESC LIMIT 1",
        (peer_id,)
    ).fetchone()


def get_unanswered_peers(account_id, minutes=30):
    """[DEPRECATED] 旧接口，按墙钟时间过滤。新代码改用 get_unanswered_candidates + config.work_elapsed_minutes"""
    cutoff = (datetime.now(TZ_BJ) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    return get_conn().execute("""
        SELECT p.*, m.text as last_text, m.timestamp as last_time, m.msg_id as last_msg_id
        FROM peers p
        JOIN messages m ON m.peer_id = p.id
        WHERE p.account_id = ?
          AND m.id = (SELECT id FROM messages WHERE peer_id = p.id AND deleted=0 ORDER BY timestamp DESC LIMIT 1)
          AND m.direction = 'B'
          AND m.timestamp <= ?
          AND NOT EXISTS (
              SELECT 1 FROM alerts
              WHERE alerts.peer_id = p.id AND alerts.type = 'no_reply'
              AND alerts.msg_id = m.msg_id AND alerts.status != 'rejected'
          )
    """, (account_id, cutoff)).fetchall()


def get_unanswered_candidates(account_id):
    """找出 "最后一条是 B 发、且尚未推过 no_reply 预警" 的所有对话。
    不做时间过滤，由调用方按工作时段累计计算是否超时。"""
    return get_conn().execute("""
        SELECT p.*, m.text as last_text, m.timestamp as last_time, m.msg_id as last_msg_id
        FROM peers p
        JOIN messages m ON m.peer_id = p.id
        WHERE p.account_id = ?
          AND m.id = (SELECT id FROM messages WHERE peer_id = p.id AND deleted=0 ORDER BY timestamp DESC LIMIT 1)
          AND m.direction = 'B'
          AND NOT EXISTS (
              SELECT 1 FROM alerts
              WHERE alerts.peer_id = p.id AND alerts.type = 'no_reply'
              AND alerts.msg_id = m.msg_id AND alerts.status != 'rejected'
          )
    """, (account_id,)).fetchall()


# ===== 预警 =====

def insert_alert(alert_type, account_id, peer_id=None, msg_id=None, message_text=""):
    conn = get_conn()
    conn.execute("""
        INSERT INTO alerts (type, account_id, peer_id, msg_id, message_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (alert_type, account_id, peer_id, msg_id, message_text, now_bj()))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_alert_status(alert_id, status, bot_message_id=None):
    conn = get_conn()
    if bot_message_id:
        conn.execute(
            "UPDATE alerts SET status=?, bot_message_id=?, reviewed_at=? WHERE id=?",
            (status, bot_message_id, now_bj(), alert_id)
        )
    else:
        conn.execute(
            "UPDATE alerts SET status=?, reviewed_at=? WHERE id=?",
            (status, now_bj(), alert_id)
        )
    conn.commit()


def claim_alert_for_review(alert_id, new_status):
    """v2.10.23:原子抢占状态转移 — WHERE status='pending' 才 update,rowcount=1 代表抢到。
    用于 callback 避免两个群成员同时点按钮重复处理同一条预警。"""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE alerts SET status=?, reviewed_at=? WHERE id=? AND status='pending'",
        (new_status, now_bj(), alert_id)
    )
    conn.commit()
    return cur.rowcount == 1


def update_alert_bot_msg(alert_id, bot_message_id):
    conn = get_conn()
    conn.execute("UPDATE alerts SET bot_message_id=? WHERE id=?", (bot_message_id, alert_id))
    conn.commit()


def get_alert(alert_id):
    return get_conn().execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()


def has_alert_today(alert_type, peer_id):
    """检查某个对话今天是否已经有过此类预警。

    v2.10.23:只认真正送达(bot_message_id IS NOT NULL)或明确静默(status='silenced')
    的记录参与去重 — 以前发送失败也会占去重记录导致当天不再重试,
    还有 ALERT_XXX_ENABLED=False 时也会写 DB 记录静默掉整天。"""
    today = datetime.now(TZ_BJ).strftime("%Y-%m-%d")
    row = get_conn().execute(
        "SELECT COUNT(*) FROM alerts WHERE type=? AND peer_id=? AND created_at LIKE ? "
        "AND (bot_message_id IS NOT NULL OR status='silenced')",
        (alert_type, peer_id, f"{today}%")
    ).fetchone()
    return row[0] > 0


def get_today_alerts(alert_type):
    today = datetime.now(TZ_BJ).strftime("%Y-%m-%d")
    return get_conn().execute(
        "SELECT a.*, p.name as peer_name, ac.name as account_name, ac.company "
        "FROM alerts a "
        "LEFT JOIN peers p ON a.peer_id = p.id "
        "LEFT JOIN accounts ac ON a.account_id = ac.id "
        "WHERE a.type=? AND a.created_at LIKE ? AND a.status='approved'",
        (alert_type, f"{today}%")
    ).fetchall()
