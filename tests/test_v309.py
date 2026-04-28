#!/usr/bin/env python3
"""v3.0.9 中央台扩展 API 单元测试 — 不依赖 docker,直接 import dashboard_api + 内存 SQLite。

跑法:
    python3 tests/test_v309.py

CI 集成在 `.github/workflows/ci.yml`,每次 push / PR 都会跑一次。
覆盖 ADR-0026 关键决策点 + Codex P1 修复后的失败语义。
"""
from __future__ import annotations
import os
import sys
import sqlite3
import tempfile
import threading
import time
import traceback
from pathlib import Path

# 注入最小 env(防 config.py 报错;CI 没 .env 也能跑)
os.environ.update({
    "METRICS_TOKEN": "test_token",
    "SHEET_ID": "dummy",
    "COMPANY_NAME": "testdept",
    "COMPANY_DISPLAY": "测试部门",
    "TG_API_ID": "1",
    "TG_API_HASH": "x",
    "BOT_TOKEN": "111:xxx",
})

# 让 import 找到仓库根目录
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import dashboard_api  # noqa: E402
import database as db  # noqa: E402

# ============================================================
# 测试 fixture — 临时文件 SQLite + 线程本地连接
# (跟生产 database.py 一样:每线程一个 connection,共享同一个文件 DB)
# 这样并发测试不会撞 SQLite "同一 connection 多线程访问" segfault
# ============================================================
_TEST_DB_FILE = Path(tempfile.mkdtemp(prefix="tg_test_v309_")) / "test.db"

_TLS = threading.local()


def _get_test_conn():
    """每线程独立 connection,共享同一个文件 DB(模拟生产 db.get_conn 行为)。"""
    c = getattr(_TLS, "conn", None)
    if c is None:
        c = sqlite3.connect(str(_TEST_DB_FILE), check_same_thread=False)
        c.row_factory = sqlite3.Row
        _TLS.conn = c
    return c


# 主线程的 connection 留给 schema init / 数据 seed 用
_test_conn = _get_test_conn()

# Monkey-patch db.get_conn() — 测试期间走我们的线程本地 connection
db._local = _TLS
db.get_conn = _get_test_conn

# 走 init_db 建表(database.py 自己建 schema + 跑 migrations)
db.init_db()

# 插入测试数据 — 覆盖各 status / stage / type
_test_conn.execute("""
    INSERT INTO accounts (id, phone, name, username, tg_id, company, operator,
                          sheet_tab, business_tg_id, owner_tg_id,
                          remind_30min_text, remind_40min_text)
    VALUES
    (1, '+85511111', '账号A', 'a_user', 1001, '测试部门', '伊凡', 'A_tab',
     '@biz_a', '@owner_a', '请回复', '请负责人回复'),
    (2, '+85522222', '账号B', 'b_user', 1002, '测试部门', '小李', 'B_tab',
     '@biz_b', '@owner_b', '', '')
""")

_test_conn.execute("""
    INSERT INTO peers (id, tg_id, account_id, name, username, col_group)
    VALUES
    (1, 5001, 1, '客户1', 'cust1', 0),
    (2, 5002, 1, '客户2', 'cust2', 0),
    (3, 5003, 1, '客户3', 'cust3', 1),
    (4, 5004, 2, '客户4', 'cust4', 0)
""")

_test_conn.executescript("""
    INSERT INTO alerts (id, type, account_id, peer_id, msg_id, message_text,
                        status, stage, keyword, created_at, reviewed_at,
                        sheet_written, last_write_error)
    VALUES
    (1, 'no_reply', 1, 1, 100, '客户没回复消息1',
     'pending',          1, '',     '2026-04-29 10:00:00', NULL,                    0, ''),
    (2, 'no_reply', 1, 1, 101, '客户没回复消息2',
     'violation_logged', 2, '',     '2026-04-29 10:30:00', '2026-04-29 11:00:00',   1, ''),
    (3, 'deleted',  1, 2, 102, '客户删了消息',
     'violation_logged', 0, '',     '2026-04-28 14:00:00', '2026-04-28 14:30:00',   1, ''),
    (4, 'keyword',  2, 4, 103, '出现关键词:打款',
     'pending',          0, '打款', '2026-04-29 09:00:00', NULL,                    0, ''),
    (5, 'no_reply', 2, 4, 104, '另一未回复',
     'approved',         1, '',     '2026-04-25 10:00:00', '2026-04-25 11:00:00',   1, ''),
    (6, 'deleted',  1, 3, 105, '又一删除',
     'rejected',         0, '',     '2026-04-20 10:00:00', '2026-04-20 10:05:00',   0, ''),
    (7, 'no_reply', 1, 1, 106, '历史 violation',
     'violation_logged', 2, '',     '2026-04-15 10:00:00', '2026-04-15 11:00:00',   1, '');

    INSERT INTO messages (id, msg_id, account_id, peer_id, direction, text,
                          media_type, timestamp, sheet_row, sheet_written,
                          deleted, deleted_at, delete_mark_pending,
                          media_seq, archive_msg_id)
    VALUES
    (1, 100, 1, 1, 'B', '客户消息 1', '', '2026-04-29 09:55:00', 5, 1, 0, NULL, 0, 0, 0),
    (2, 101, 1, 1, 'B', '客户追问',   '', '2026-04-29 10:25:00', 6, 1, 0, NULL, 0, 0, 0),
    (3, 102, 1, 2, 'B', '被删除的',   '', '2026-04-28 13:55:00', 7, 1, 1, '2026-04-28 14:00:00', 0, 1, 999),
    (4, 103, 2, 4, 'B', '关键词消息', '', '2026-04-29 08:55:00', 8, 1, 0, NULL, 0, 0, 0);
""")
_test_conn.commit()


# ============================================================
# 测试运行器
# ============================================================
PASS = []
FAIL = []


def run(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f"  ✗ {name}: {e}")
    except Exception as e:
        FAIL.append((name, f"unexpected exception: {type(e).__name__}: {e}"))
        print(f"  ✗ {name}: unexpected {type(e).__name__}: {e}")
        traceback.print_exc()


# ============================================================
# 【1】4 个新端点 — JSON 字段完整
# ============================================================
print("=" * 60)
print("【1】4 个新端点 — JSON 字段完整")
print("=" * 60)


def t_violations_fields():
    items = dashboard_api.violations(from_date="2026-04-29", to_date="2026-04-29")
    assert len(items) == 1, f"今天应有 1 条 violation,实际 {len(items)}"
    v = items[0]
    expected = {"id", "type", "stage", "status", "keyword", "message_text",
                "created_at", "reviewed_at", "account_id", "account_name",
                "account_phone", "account_company", "dept", "peer_id",
                "peer_name", "peer_username", "msg_id", "business_tg_id",
                "owner_tg_id"}
    missing = expected - set(v.keys())
    assert not missing, f"violations 缺字段: {missing}"
    assert v["status"] == "violation_logged"
    assert v["dept"] == "测试部门"
    assert v["business_tg_id"] == "@biz_a"
    assert v["owner_tg_id"] == "@owner_a"


def t_alerts_filtered_fields():
    res = dashboard_api.alerts_filtered(limit=100)
    for k in ("items", "limit", "offset", "returned"):
        assert k in res, f"alerts_filtered 返回缺 key: {k}"
    assert res["returned"] == 7
    a = res["items"][0]
    expected = {"id", "type", "status", "stage", "keyword", "message_text",
                "created_at", "reviewed_at", "claimed_at", "sheet_written",
                "last_write_error", "bot_message_id", "account_id",
                "account_name", "account_phone", "account_company", "dept",
                "peer_id", "peer_name", "peer_username", "msg_id"}
    missing = expected - set(a.keys())
    assert not missing, f"alerts_filtered 缺字段: {missing}"


def t_peers_fields():
    res = dashboard_api.peers_all()
    assert "items" in res
    assert res["returned"] == 4
    p = res["items"][0]
    expected = {"id", "tg_id", "account_id", "account_name", "account_phone",
                "name", "username", "col_group"}
    missing = expected - set(p.keys())
    assert not missing, f"peers 缺字段: {missing}"


def t_messages_fields():
    res = dashboard_api.messages_filtered(account_id=1, peer_id=1)
    assert "items" in res
    assert res["returned"] == 2
    m = res["items"][0]
    expected = {"id", "msg_id", "direction", "text", "media_type", "timestamp",
                "sheet_row", "sheet_written", "deleted", "deleted_at",
                "delete_mark_pending", "media_seq", "archive_msg_id"}
    missing = expected - set(m.keys())
    assert not missing, f"messages 缺字段: {missing}"


run("violations 字段完整", t_violations_fields)
run("alerts_filtered 字段完整", t_alerts_filtered_fields)
run("peers_all 字段完整", t_peers_fields)
run("messages_filtered 字段完整", t_messages_fields)


# ============================================================
# 【2-3】参数非法 → ValueError(endpoint 转 400)
# ============================================================
print()
print("=" * 60)
print("【2-3】参数非法 → ValueError(endpoint 转 400)")
print("=" * 60)


def _expect_value_error(fn, expected_in_msg=None, name=""):
    try:
        fn()
        raise AssertionError(f"{name} 应该 raise ValueError 但没")
    except ValueError as e:
        if expected_in_msg and expected_in_msg not in str(e):
            raise AssertionError(f"{name} 错误信息应含 '{expected_in_msg}',实际: {e}")


run("status=DROP_TABLE → ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.alerts_filtered(status="DROP_TABLE"),
        "status", "status=DROP_TABLE"))

run("type=NOT_REAL → ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.violations(alert_type="NOT_REAL"),
        None, "type=NOT_REAL"))

run("from=2026-99-99 → ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.violations(from_date="2026-99-99"),
        "日期", "from=2026-99-99"))

run("from=2026-04-01junk → ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.violations(from_date="2026-04-01junk"),
        None, "from=2026-04-01junk"))

run("from=20260429(无连字符)→ ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.violations(from_date="20260429"),
        None, "from=20260429"))

run("stage=99 → ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.alerts_filtered(stage=99),
        None, "stage=99"))


# ============================================================
# 【4-5】messages_filtered 必填 + ID 校验
# ============================================================
print()
print("=" * 60)
print("【4-5】messages_filtered 必填 + ID 校验")
print("=" * 60)

run("messages_filtered None account_id → ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.messages_filtered(None, 1),
        "account_id", "missing account_id"))

run("messages_filtered account_id='abc' → ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.messages_filtered("abc", 1),
        "account_id", "non-int account_id"))

run("messages_filtered peer_id='def' → ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.messages_filtered(1, "def"),
        "peer_id", "non-int peer_id"))


# ============================================================
# 【6】peers_all 非法过滤参数 → ValueError(防静默退化全表)
# ============================================================
print()
print("=" * 60)
print("【6】peers_all 非法过滤 → ValueError")
print("=" * 60)

run("peers_all account_id='abc' → ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.peers_all(account_id="abc"),
        "account_id", "peers non-int account_id"))

run("peers_all group='xyz' → ValueError",
    lambda: _expect_value_error(
        lambda: dashboard_api.peers_all(group="xyz"),
        "group", "peers non-int group"))


def t_peers_valid_filter():
    res = dashboard_api.peers_all(account_id=1)
    assert res["returned"] == 3, f"账号 1 应有 3 个 peer,实际 {res['returned']}"
    assert all(p["account_id"] == 1 for p in res["items"])


run("peers_all account_id=1 → 3 peers(过滤生效)", t_peers_valid_filter)


# ============================================================
# 【7】limit clamp
# ============================================================
print()
print("=" * 60)
print("【7】limit clamp 边界")
print("=" * 60)


def t_alerts_limit_high():
    assert dashboard_api.alerts_filtered(limit=99999)["limit"] == 1000


def t_alerts_limit_low():
    assert dashboard_api.alerts_filtered(limit=-1)["limit"] == 1


def t_alerts_limit_invalid():
    assert dashboard_api.alerts_filtered(limit="abc")["limit"] == 200


def t_messages_limit_high():
    assert dashboard_api.messages_filtered(account_id=1, peer_id=1,
                                           limit=99999)["limit"] == 1000


run("alerts limit=99999 → 1000", t_alerts_limit_high)
run("alerts limit=-1 → 1(下限 clamp)", t_alerts_limit_low)
run("alerts limit='abc' → default 200", t_alerts_limit_invalid)
run("messages limit=99999 → 1000", t_messages_limit_high)


# ============================================================
# 【8】并发 — SQLite 不锁
# ============================================================
print()
print("=" * 60)
print("【8】并发 10 请求不锁 DB")
print("=" * 60)


def t_concurrent():
    results = []
    errors = []

    def worker():
        try:
            for _ in range(5):
                r = dashboard_api.alerts_filtered(limit=100)
                results.append(r["returned"])
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0
    assert not errors, f"并发出错: {errors[:3]}"
    assert len(results) == 50, f"应有 50 结果,实际 {len(results)}"
    assert all(r == 7 for r in results), "所有结果应该 = 7 alerts"
    print(f"     50 个并发查询 完成耗时 {elapsed:.3f}s")


run("10 并发 × 5 query 共 50 请求不锁 DB", t_concurrent)


# ============================================================
# 总结
# ============================================================
print()
print("=" * 60)
print(f"结果:✓ {len(PASS)} pass / ✗ {len(FAIL)} fail")
print("=" * 60)
if FAIL:
    print()
    print("失败明细:")
    for name, err in FAIL:
        print(f"  ✗ {name}: {err}")
    sys.exit(1)
else:
    print("🎉 全部通过!")
    sys.exit(0)
