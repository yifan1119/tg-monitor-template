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
    conn.commit()


def now_bj():
    return datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S")


# ===== 账号 =====

def upsert_account(phone, name="", username="", tg_id=None, company="", operator=""):
    conn = get_conn()
    conn.execute("""
        INSERT INTO accounts (phone, name, username, tg_id, company, operator)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            name=excluded.name, username=excluded.username,
            tg_id=excluded.tg_id, company=excluded.company, operator=excluded.operator
    """, (phone, name, username, tg_id, company, operator))
    conn.commit()
    return conn.execute("SELECT * FROM accounts WHERE phone=?", (phone,)).fetchone()


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
    return get_conn().execute(
        "SELECT m.*, p.col_group, a.phone FROM messages m "
        "JOIN peers p ON m.peer_id = p.id "
        "JOIN accounts a ON m.account_id = a.id "
        "WHERE m.sheet_written = 0 ORDER BY m.timestamp LIMIT 500"
    ).fetchall()


def mark_written(msg_db_id, sheet_row):
    conn = get_conn()
    conn.execute(
        "UPDATE messages SET sheet_written=1, sheet_row=? WHERE id=?",
        (sheet_row, msg_db_id)
    )
    conn.commit()


def mark_deleted(msg_id, account_id):
    conn = get_conn()
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


def update_alert_bot_msg(alert_id, bot_message_id):
    conn = get_conn()
    conn.execute("UPDATE alerts SET bot_message_id=? WHERE id=?", (bot_message_id, alert_id))
    conn.commit()


def get_alert(alert_id):
    return get_conn().execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()


def has_alert_today(alert_type, peer_id):
    """检查某个对话今天是否已经有过此类预警"""
    today = datetime.now(TZ_BJ).strftime("%Y-%m-%d")
    row = get_conn().execute(
        "SELECT COUNT(*) FROM alerts WHERE type=? AND peer_id=? AND created_at LIKE ?",
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
