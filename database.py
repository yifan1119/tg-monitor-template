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
        conn.commit()  # v2.10.24.3 Codex Minor 3:每步独立 commit,缩小崩溃窗口

    if version < 2:
        _migrate_to_2(conn)
        conn.execute("PRAGMA user_version=2")
        conn.commit()

    if version < 3:
        _migrate_to_3(conn)
        conn.execute("PRAGMA user_version=3")
        conn.commit()

    if version < 4:
        _migrate_to_4(conn)
        conn.execute("PRAGMA user_version=4")
        conn.commit()


def _migrate_to_1(conn):
    """v2.10.23 → user_version=1:
    - messages 加 delete_mark_pending(删除时序修复:消息写 Sheet 前被删 → 写完立刻补标红)
    """
    _safe_add_column(conn, "messages", "delete_mark_pending", "INTEGER DEFAULT 0")


def _migrate_to_2(conn):
    """v2.10.24.3 → user_version=2(ADR-0010):
    - alerts 加 sheet_written / keyword / last_write_error
      → 支持预警分页写入失败自动 writeback(429 > 6 秒 / worksheet 短暂不可达时保零丢失)
    - 历史 alerts 全标 sheet_written=1(不追补,仅保障升级后新产生的零损失)
    - 历史 keyword 类型 message_text 保持 `[kw] text` 不动;新产生的拆 keyword + message_text
    """
    _safe_add_column(conn, "alerts", "sheet_written", "INTEGER DEFAULT 0")
    _safe_add_column(conn, "alerts", "keyword", "TEXT DEFAULT ''")
    _safe_add_column(conn, "alerts", "last_write_error", "TEXT DEFAULT ''")
    # 历史 alerts 全部标 1 —— 避免 writeback loop 拿历史去重复写分页
    conn.execute("UPDATE alerts SET sheet_written=1 WHERE sheet_written=0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_sheet_written ON alerts(sheet_written)")


def _migrate_to_3(conn):
    """v2.10.24.3 → user_version=3(ADR-0010 Codex round3 P0 修复):
    - alerts 加 claimed_at 时间戳:claim-first 语义下,claim 成功后 crash 会让
      sheet_written 卡 1 → 永久跳过 writeback。引入 claimed_at 形成三态:
        sheet_written=0              → 未写(writeback 捡起)
        sheet_written=1 + claimed_at !=NULL → 进行中(stale 时 writeback 捡起重试)
        sheet_written=1 + claimed_at IS NULL → 写完(writeback 不再触碰)
    - 历史 alerts 升级到 user_version=2 时已全 sheet_written=1,claimed_at 默认 NULL →
      天然视为 "已写完",不会被 writeback 误拾。
    """
    _safe_add_column(conn, "alerts", "claimed_at", "TEXT DEFAULT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_claimed_at ON alerts(claimed_at)")


def _migrate_to_4(conn):
    """v2.10.25 → user_version=4(ADR-0014):
    - messages 加 media_seq (每账号内单调递增的媒体编号)
      + archive_msg_id (TG 档案群里对应的消息 ID)
      → 支持 MEDIA_STORAGE_MODE=tg_archive 模式:Bot 转发到档案群,
        Sheet 显示「图片 #N / 文件 #N」超链接点到档案群对应消息。
    - 历史行 media_seq=0 / archive_msg_id=0,不回填(仍保留 Drive 链接或占位文字)
    - drive / off 模式不写这两列(默认 0)
    - 新建 account_seq 计数表:原子分配 media_seq,避免 MAX+1 在并发下派出重复编号
      (Codex P1 round1:_handle_message / _backfill_peer / pull_history 三路 coroutine
       对同账号可能交错,旧 MAX+1 在 await forward 期间会读到同一最大值 → 重复编号)
    """
    _safe_add_column(conn, "messages", "media_seq", "INTEGER DEFAULT 0")
    _safe_add_column(conn, "messages", "archive_msg_id", "INTEGER DEFAULT 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_seq (
            account_id INTEGER PRIMARY KEY,
            media_seq INTEGER NOT NULL DEFAULT 0
        )
    """)


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

def insert_message(msg_id, account_id, peer_id, direction, text, media_type="", timestamp=None,
                   media_seq=0, archive_msg_id=0):
    """v2.10.25: media_seq / archive_msg_id 只在 MEDIA_STORAGE_MODE=tg_archive 时由调用方填,
    其他模式保持默认 0(向后兼容)。"""
    conn = get_conn()
    ts = timestamp or now_bj()
    try:
        conn.execute("""
            INSERT INTO messages (msg_id, account_id, peer_id, direction, text, media_type, timestamp,
                                  media_seq, archive_msg_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (msg_id, account_id, peer_id, direction, text, media_type, ts,
              media_seq, archive_msg_id))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # 已存在


def next_media_seq(account_id):
    """v2.10.25(ADR-0014)+ Codex P1 round1 修复:原子分配下一个媒体编号。

    用 account_seq 计数表替代 MAX+1,避免并发 coroutine(`_handle_message` /
    `_backfill_peer` / `pull_history` 三路对同账号可能交错 await)读到同一最大值
    → 派出重复编号。

    原子性保证:
      - asyncio 默认单线程执行,INSERT/UPDATE/SELECT/commit 四步同步调用期间
        不会被其他 coroutine 打断(没有 await)
      - 调用返回前必 commit → 后续 coroutine 看得到递增后的值
      - Python sqlite3 默认 "deferred" transaction,自动 BEGIN;这里 commit 关
        transaction,不影响同一 conn 上其他实现路径的 implicit transaction

    语义:
      - 同账号:严格单调递增(5, 6, 7, ...),编号不重复
      - 跨账号:独立(account_id=1 和 account_id=2 的 seq 互不影响)
      - 转发失败不回收:允许跳号(客户看 #41 之后可能是 #43),ADR-0014 已接受
    """
    conn = get_conn()
    # INSERT OR IGNORE 幂等确保有 row;UPDATE 原子 +1;SELECT 拿回新值;commit 关 tx
    conn.execute(
        "INSERT OR IGNORE INTO account_seq (account_id, media_seq) VALUES (?, 0)",
        (account_id,)
    )
    conn.execute(
        "UPDATE account_seq SET media_seq = media_seq + 1 WHERE account_id=?",
        (account_id,)
    )
    row = conn.execute(
        "SELECT media_seq FROM account_seq WHERE account_id=?",
        (account_id,)
    ).fetchone()
    conn.commit()
    return int(row["media_seq"])


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

def insert_alert(alert_type, account_id, peer_id=None, msg_id=None, message_text="", keyword=""):
    """v2.10.24.3:新增 keyword 参数(ADR-0010)— keyword 类型的关键词单独存栏位,
    方便 writeback loop 在分页 append_row 时填正确列(以前 message_text 含 `[kw] text`
    混存,拆不干净。历史 alerts 不动,迁移后新插入的 keyword 类型才分开存)。"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO alerts (type, account_id, peer_id, msg_id, message_text, keyword, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (alert_type, account_id, peer_id, msg_id, message_text, keyword, now_bj()))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# v2.10.24.3(ADR-0010):预警分页写入成功/失败/巡检的 helper

def claim_alert_for_sheet_write(alert_id):
    """v2.10.24.3(ADR-0010,Codex round3 P0 修复):claim-first + stale 回收。

    三态:
      sheet_written=0                       → 未写(可 claim)
      sheet_written=1 + claimed_at=NULL     → 写完(跳过)
      sheet_written=1 + claimed_at!=NULL    → 进行中(超过 stale 阈值可重 claim)

    SQL:
      UPDATE sheet_written=1, claimed_at=now
       WHERE id=? AND (sheet_written=0 OR claimed_at < stale_threshold)

    返回 True:本调用 rowcount=1,caller 必须 append Sheet;成功调 mark_alert_sheet_done
      (把 claimed_at=NULL)final done;失败调 rollback_alert_sheet_claim(回 0)。
    返回 False:别路径已 claim 且未超 stale(或已 done)→ 跳过。

    crash 场景:claim 后 append 前 crash → sheet_written=1 + claimed_at=<crash时>
      → 过 stale 阈值(config.ALERT_WRITEBACK_CLAIM_STALE_SEC,默认 300s)后,
      writeback loop 会再次 claim 重试。at-least-once 语义:极端 crash 窗内
      「append 成功未 mark done」会导致下轮重复写一行(选重复 vs 永久漏行)。
    """
    import time as _time
    import config as _config
    conn = get_conn()
    now_str = now_bj()
    # stale_threshold 用字符串比较(格式 YYYY-MM-DD HH:MM:SS,可字典序)
    stale_seconds = getattr(_config, "ALERT_WRITEBACK_CLAIM_STALE_SEC", 300)
    stale_dt = datetime.now(TZ_BJ) - timedelta(seconds=stale_seconds)
    stale_str = stale_dt.strftime("%Y-%m-%d %H:%M:%S")
    for attempt in range(3):
        try:
            cur = conn.execute(
                "UPDATE alerts SET sheet_written=1, claimed_at=?, last_write_error='' "
                "WHERE id=? AND ("
                "   sheet_written=0 "
                "   OR (sheet_written=1 AND claimed_at IS NOT NULL AND claimed_at < ?)"
                ")",
                (now_str, alert_id, stale_str),
            )
            conn.commit()
            return cur.rowcount == 1
        except sqlite3.OperationalError as e:
            if attempt == 2:
                import logging as _logging
                _logging.getLogger(__name__).error(
                    "claim_alert_for_sheet_write 3 次都 locked alert_id=%s: %s — "
                    "放弃 claim(本路径不写分页,下轮 loop 再试)",
                    alert_id, e,
                )
                return False
            _time.sleep(0.5 * (attempt + 1))
    return False


def mark_alert_sheet_done(alert_id):
    """v2.10.24.3(ADR-0010,Codex round3 P0):claim + append 成功后标 final done
    (清 claimed_at)。之后 writeback 不再触碰(即便 sheet_written=1 但 claimed_at=NULL
    视为完成状态)。3 次 retry 扛 SQLite locked;全败返回 False 时 claimed_at 留着
    → stale 阈值(5 min)后 writeback 会重试,造成最多多写一行(at-least-once)。
    """
    import time as _time
    conn = get_conn()
    for attempt in range(3):
        try:
            conn.execute(
                "UPDATE alerts SET claimed_at=NULL, last_write_error='' WHERE id=?",
                (alert_id,),
            )
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            if attempt == 2:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "mark_alert_sheet_done 3 次都 locked alert_id=%s: %s — "
                    "claimed_at 留着,stale 后会被 writeback 重试(可能多写一行)",
                    alert_id, e,
                )
                return False
            _time.sleep(0.5 * (attempt + 1))
    return False


def rollback_alert_sheet_claim(alert_id, error_text=None):
    """v2.10.24.3(ADR-0010):claim 后 Sheet 写入失败 → 把 sheet_written 回 0,
    claimed_at 也清,让后续 loop / bot 能立刻重新 claim 重试(不用等 stale 阈值)。
    同时记 last_write_error。

    注意:rollback 若失败(SQLite 坏 / 磁盘满,3 retry 都 locked)→ sheet_written 卡 1
    + claimed_at 留着 —— 不会永久丢,stale 阈值后 writeback 自动重拾(代价:可能多写一行)。
    """
    import time as _time
    conn = get_conn()
    err_str = str(error_text)[:500] if error_text is not None else ''
    for attempt in range(3):
        try:
            conn.execute(
                "UPDATE alerts SET sheet_written=0, claimed_at=NULL, last_write_error=? WHERE id=?",
                (err_str, alert_id),
            )
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            if attempt == 2:
                import logging as _logging
                _logging.getLogger(__name__).error(
                    "rollback_alert_sheet_claim 3 次都 locked alert_id=%s: %s "
                    "(claim 留着,stale 后 writeback 会重试 —— 见 ADR-0010)",
                    alert_id, e,
                )
                return False
            _time.sleep(0.5 * (attempt + 1))
    return False


# 保留兼容别名(v2.10.24.3 round1 代码引入 mark_alert_sheet_written)→ round3 之后
# 等价于 mark_alert_sheet_done,维持 API 不破。新代码请直接用 mark_alert_sheet_done。
def mark_alert_sheet_written(alert_id):
    """v2.10.24.3 兼容别名 → mark_alert_sheet_done"""
    return mark_alert_sheet_done(alert_id)


def record_alert_write_error(alert_id, error_text):
    """v2.10.24.3:记录写入失败原因(供排查用,不影响 writeback 重试)。
    错误字符串截 500 字避免异常 traceback 撑爆 DB。"""
    conn = get_conn()
    conn.execute(
        "UPDATE alerts SET last_write_error=? WHERE id=?",
        (str(error_text)[:500], alert_id),
    )
    conn.commit()


def _writeback_candidate_where_clause():
    """v2.10.24.3 Codex round3 P0:sheet_written=0 OR stale claim(sheet_written=1
    但 claimed_at 已超过 stale 阈值 = 进程 crash 遗留) —— 两者都应被 writeback 重拾。

    claimed_at IS NULL 的 sheet_written=1 视为"已写完",永不重拾。
    """
    import config as _config
    stale_seconds = getattr(_config, "ALERT_WRITEBACK_CLAIM_STALE_SEC", 300)
    stale_dt = datetime.now(TZ_BJ) - timedelta(seconds=stale_seconds)
    stale_str = stale_dt.strftime("%Y-%m-%d %H:%M:%S")
    clause = (
        "((a.sheet_written = 0) OR "
        " (a.sheet_written = 1 AND a.claimed_at IS NOT NULL AND a.claimed_at < ?))"
    )
    return clause, stale_str


def get_unwritten_alerts(limit=100):
    """v2.10.24.3:取出待 writeback 的预警供 loop 扫。

    规则:
    - sheet_written=0(从未 claim)+ stale claim(sheet_written=1 且 claimed_at 超期,
      即进程 crash 后 claim 没清也没完成)—— 两者都捡
    - keyword 类型:status!='silenced' 就撈(命中即应写分页)
    - no_reply / deleted 类型:只撈 status='approved'
    - JOIN accounts + peers 一次撈齐 company/operator/account_name/peer_name
    - LIMIT 控制一轮量(默认 100),避免一次掏光撞配额
    """
    where_clause, stale_str = _writeback_candidate_where_clause()
    sql = f"""
        SELECT
            a.id, a.type, a.keyword, a.message_text, a.created_at, a.status,
            acc.company AS account_company,
            acc.operator AS account_operator,
            acc.name AS account_name,
            p.name AS peer_name
        FROM alerts a
        JOIN accounts acc ON acc.id = a.account_id
        LEFT JOIN peers p ON p.id = a.peer_id
        WHERE {where_clause}
          AND a.status != 'silenced'
          AND (
              a.type = 'keyword'
              OR (a.type IN ('no_reply', 'deleted') AND a.status = 'approved')
          )
        ORDER BY a.id
        LIMIT ?
    """
    return get_conn().execute(sql, (stale_str, limit)).fetchall()


def count_unwritten_alerts():
    """v2.10.24.3:统计待 writeback 积压量(含 stale claim)供日志/监控用。"""
    where_clause, stale_str = _writeback_candidate_where_clause()
    sql = f"""
        SELECT COUNT(*) FROM alerts a
        WHERE {where_clause}
          AND a.status != 'silenced'
          AND (a.type = 'keyword' OR (a.type IN ('no_reply','deleted') AND a.status = 'approved'))
    """
    return get_conn().execute(sql, (stale_str,)).fetchone()[0]


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

    v2.10.23:no_reply / deleted 走审批流 — 只认真正送达(bot_message_id IS NOT NULL)
    或明确静默(status='silenced')的记录参与去重 — 以前发送失败也会占去重记录导致
    当天不再重试,还有 ALERT_XXX_ENABLED=False 时也会写 DB 记录静默掉整天。

    v2.10.24.3:keyword 类型没有审批流,v2.10.24.3 改成先 insert_alert 再推 TG 再写 Sheet
    (保证 Sheet 不丢行的 writeback 前提)。此时 bot_message_id 可能一直 NULL(keyword
    路径没记 message id),只要 DB 里有今日的 keyword 行,就算已处理 — 不然 TG 推送失败
    时下一次触发会再插一条、再写一行 Sheet 造成重复。"""
    today = datetime.now(TZ_BJ).strftime("%Y-%m-%d")
    if alert_type == "keyword":
        row = get_conn().execute(
            "SELECT COUNT(*) FROM alerts WHERE type='keyword' AND peer_id=? AND created_at LIKE ?",
            (peer_id, f"{today}%")
        ).fetchone()
    else:
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
