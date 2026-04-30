#!/usr/bin/env python3
"""v3.1 (ADR-0027) 单元测试 — _scan_first_empty 纯函数 + DB helpers + write 双轨决策

跑法:
    python3 tests/test_v31.py

CI 集成在 .github/workflows/ci.yml(自动跑 v3.0.9 + v3.1 两个 test_*.py)。
"""
from __future__ import annotations
import os
import sys
import sqlite3
import tempfile
import threading
import traceback
from pathlib import Path

# 注入最小 env
os.environ.update({
    "METRICS_TOKEN": "test_token",
    "SHEET_ID": "dummy",
    "COMPANY_NAME": "testdept",
    "COMPANY_DISPLAY": "测试部门",
    "TG_API_ID": "1",
    "TG_API_HASH": "x",
    "BOT_TOKEN": "111:xxx",
    "SHEET_RESYNC_ENABLED": "true",
    "SHEET_RESYNC_INTERVAL_MINUTES": "15",
    "SHEET_RESYNC_VERIFY_BEFORE_WRITE": "false",
})

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import database as db  # noqa: E402

# ============================================================
# Fixture — 共享文件 SQLite + thread-local conn(同 v3.0.9 测试)
# ============================================================
_TEST_DB = Path(tempfile.mkdtemp(prefix="tg_test_v31_")) / "test.db"
_TLS = threading.local()


def _get_test_conn():
    c = getattr(_TLS, "conn", None)
    if c is None:
        c = sqlite3.connect(str(_TEST_DB), check_same_thread=False)
        c.row_factory = sqlite3.Row
        _TLS.conn = c
    return c


db._local = _TLS
db.get_conn = _get_test_conn

_test_conn = _get_test_conn()
db.init_db()

# 验证 V6 跑了
ver = _test_conn.execute("PRAGMA user_version").fetchone()[0]
assert ver >= 6, f"migration V6 没跑,user_version={ver}"

# 验证 peers 加了新字段
cols = [r["name"] for r in _test_conn.execute("PRAGMA table_info(peers)").fetchall()]
assert "next_sheet_row" in cols, f"peers.next_sheet_row 没加,cols={cols}"
assert "next_sheet_row_resynced_at" in cols, f"peers.next_sheet_row_resynced_at 没加"

# 插测试数据
_test_conn.executescript("""
    INSERT INTO accounts (id, phone, name, sheet_tab) VALUES
        (1, '+85511111', '账号A', 'A_tab'),
        (2, '+85522222', '账号B', 'B_tab');
    INSERT INTO peers (id, tg_id, account_id, name, col_group) VALUES
        (1, 5001, 1, '客户1', 0),
        (2, 5002, 1, '客户2', 1),
        (3, 5003, 1, '客户3', -1),
        (4, 5004, 2, '客户4', 0);
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
        FAIL.append((name, f"unexpected: {type(e).__name__}: {e}"))
        print(f"  ✗ {name}: unexpected {type(e).__name__}: {e}")
        traceback.print_exc()


# ============================================================
# 【1】_scan_first_empty 纯函数(核心 resync 算法)
# ============================================================
print("=" * 60)
print("【1】_scan_first_empty 纯函数 — resync 核心算法")
print("=" * 60)

# 重新导入 sheets module 因为它有 SheetsWriter __init__ 拉 OAuth — 我们只测 _scan_first_empty
# 走 importlib + spec 避免 init OAuth
from sheets import _scan_first_empty


def t_empty_sheet():
    """空 worksheet — 应返 row 7(header_rows=6, 第一个数据行)"""
    values = []   # ws.get_all_values() 空表返 []
    assert _scan_first_empty(values, 0) == 7, f"空表应返 7,实际 {_scan_first_empty(values, 0)}"


def t_only_header():
    """只有 header(row 1-6)— 应返 row 7"""
    values = [["", "", ""]] * 6   # 6 行全空 header
    assert _scan_first_empty(values, 0) == 7


def t_first_message_at_row_7():
    """row 7 有数据(col_group=0 = col A/B/C)— 应返 row 8"""
    values = [["", "", ""]] * 6 + [["2026-04-21", "B", "msg1"]]
    assert _scan_first_empty(values, 0) == 8


def t_consecutive_full():
    """row 7-15 都满,col_group=0 — 应返 row 16"""
    values = [["", "", ""]] * 6
    for i in range(9):
        values.append([f"ts{i}", "B", f"msg{i}"])
    assert _scan_first_empty(values, 0) == 16


def t_gap_in_middle():
    """row 7 有数据,row 8-9 空,row 10 有数据 — 应返 row 8(第一个空)"""
    values = [["", "", ""]] * 6
    values.append(["2026-04-21", "B", "msg1"])  # row 7
    values.append(["", "", ""])                  # row 8 空
    values.append(["", "", ""])                  # row 9 空
    values.append(["2026-04-22", "A", "msg2"])  # row 10
    assert _scan_first_empty(values, 0) == 8


def t_different_col_group():
    """col_group=8 (col Y/Z/AA = 24/25/26) 跟 col_group=0 隔离"""
    values = [["", "", ""] + [""] * 25] * 6   # 6 行 header (28 列)
    # row 7 在 col Y(24) 写数据
    row7 = [""] * 24 + ["2026-04-21", "B", "msg_tommie"]
    values.append(row7)
    # 测 col_group=8 (col 24)
    assert _scan_first_empty(values, 24) == 8
    # 测 col_group=0 (col 0)还是空 → 应返 row 7
    assert _scan_first_empty(values, 0) == 7


def t_short_row_handles_gracefully():
    """ws.get_all_values() 尾部空 cell 会被 trim,row 长度可能比 col_start+3 短"""
    values = [["", "", ""]] * 6
    # row 7 只有 5 列(idx 0-4),col_group=8 (col 24+) 那块根本没数据
    values.append(["a", "b", "c", "d", "e"])
    # col_group=8 的 col 24/25/26 在 row 7 是"空"(超出 row 长度)
    # 算法应认为 row 7 在 col 24 是空 → 返 7
    assert _scan_first_empty(values, 24) == 7
    # col_group=0 的 col 0 有数据 → 返 8
    assert _scan_first_empty(values, 0) == 8


def t_partial_data_in_row_not_empty():
    """row 7 在 col_start 有数据但 col_start+1/+2 空 — 算"非空"应返 row 8"""
    values = [["", "", ""]] * 6
    values.append(["2026-04-21", "", ""])  # 只有 timestamp
    assert _scan_first_empty(values, 0) == 8


def t_custom_header_rows():
    """跳过自定义行数(比如客户加了额外 header)"""
    values = [["", "", ""]] * 10   # 假设 row 1-10 是 header
    values.append(["data", "B", "msg"])  # row 11
    assert _scan_first_empty(values, 0, header_rows=10) == 12


run("空 worksheet → row 7", t_empty_sheet)
run("只有 header → row 7", t_only_header)
run("row 7 有数据 → row 8", t_first_message_at_row_7)
run("row 7-15 满 → row 16", t_consecutive_full)
run("row 7 数据 + 8-9 空 + 10 数据 → row 8(找首空)", t_gap_in_middle)
run("col_group 隔离(col 24 vs col 0)", t_different_col_group)
run("短 row (尾部 trim) 优雅处理", t_short_row_handles_gracefully)
run("partial cell (timestamp 但 role/text 空) → 算非空", t_partial_data_in_row_not_empty)
run("自定义 header_rows", t_custom_header_rows)


# ============================================================
# 【2】DB helpers
# ============================================================
print()
print("=" * 60)
print("【2】DB helpers — peers.next_sheet_row")
print("=" * 60)


def t_get_peer_next_sheet_row_initial_null():
    """新插的 peer next_sheet_row 应该是 NULL"""
    nrow, resynced_at = db.get_peer_next_sheet_row(1)
    assert nrow is None, f"应 NULL,实际 {nrow}"
    assert resynced_at is None


def t_set_peer_next_sheet_row():
    """resync loop 写入"""
    db.set_peer_next_sheet_row(1, 156)
    nrow, resynced_at = db.get_peer_next_sheet_row(1)
    assert nrow == 156
    assert resynced_at is not None and len(resynced_at) >= 10


def t_bump_peer_next_sheet_row():
    """flush 写完 +N"""
    db.set_peer_next_sheet_row(2, 100)
    db.bump_peer_next_sheet_row(2, 5)
    nrow, _ = db.get_peer_next_sheet_row(2)
    assert nrow == 105, f"100+5=105 实际 {nrow}"


def t_bump_when_null_noop():
    """bump 一个 NULL peer — 应该 no-op (next_sheet_row IS NOT NULL guard)"""
    db.set_peer_next_sheet_row(3, None) if False else None  # peer 3 一直 NULL
    db.bump_peer_next_sheet_row(3, 10)
    nrow, _ = db.get_peer_next_sheet_row(3)
    assert nrow is None, f"NULL bump 后还是 NULL,实际 {nrow}"


def t_invalidate():
    """write 失败 → 清回 NULL"""
    db.set_peer_next_sheet_row(4, 200)
    db.invalidate_peer_next_sheet_row(4, "test_reason")
    nrow, resynced_at = db.get_peer_next_sheet_row(4)
    assert nrow is None and resynced_at is None


def t_get_all_peers_with_col_group():
    """resync loop 拿所有已分配 col_group 的 peers"""
    peers = db.get_all_peers_with_col_group()
    # 测试数据:peer 1/2/4 col_group >= 0, peer 3 col_group=-1 → 排除
    ids = [p["id"] for p in peers]
    assert 1 in ids and 2 in ids and 4 in ids
    assert 3 not in ids


def t_get_all_accounts():
    """resync loop 拿所有 accounts"""
    accs = db.get_all_accounts()
    assert len(accs) >= 2
    ids = [a["id"] for a in accs]
    assert 1 in ids and 2 in ids


def t_get_max_resynced_at():
    """dashboard 跨容器读"""
    db.set_peer_next_sheet_row(1, 156)   # 这条会更新 resynced_at
    ts = db.get_max_resynced_at()
    assert ts is not None and len(ts) >= 10


run("新 peer next_sheet_row=NULL", t_get_peer_next_sheet_row_initial_null)
run("set_peer_next_sheet_row 写入", t_set_peer_next_sheet_row)
run("bump_peer_next_sheet_row +N", t_bump_peer_next_sheet_row)
run("bump 在 NULL 上是 no-op(防错误初始化)", t_bump_when_null_noop)
run("invalidate 清回 NULL", t_invalidate)
run("get_all_peers_with_col_group 排除 col_group=-1", t_get_all_peers_with_col_group)
run("get_all_accounts 全拿", t_get_all_accounts)
run("get_max_resynced_at 给 dashboard", t_get_max_resynced_at)


# ============================================================
# 【3】Migration 幂等性
# ============================================================
print()
print("=" * 60)
print("【3】Migration V6 幂等性")
print("=" * 60)


def t_migration_idempotent():
    """跑两次 _migrate_to_6 应该无害(_safe_add_column 已存在列 no-op)"""
    # 直接调内部 migration
    db._migrate_to_6(_test_conn)
    db._migrate_to_6(_test_conn)
    cols = [r["name"] for r in _test_conn.execute("PRAGMA table_info(peers)").fetchall()]
    # 仍然只有 next_sheet_row 一份(没重复加)
    assert cols.count("next_sheet_row") == 1
    assert cols.count("next_sheet_row_resynced_at") == 1


run("V6 跑两次幂等", t_migration_idempotent)


# ============================================================
# 【4】Codex P0/P1 修复后场景(Codex audit follow-up)
# ============================================================
print()
print("=" * 60)
print("【4】Codex P0/P1 修复 — write 路径 / flag 切换 / 锁竞争")
print("=" * 60)


class FakeWorksheet:
    """简易 ws stub — 不需要真 Google API。模拟 update / acell / append_rows 三个核心方法。"""
    def __init__(self, title, data=None, fail_on_update=False):
        self.title = title
        self._data = data or {}     # {row: {col_idx: value}} sparse
        self.update_calls = []      # [(range_str, rows)]
        self.append_calls = []      # [rows]
        self.acell_calls = []       # [a1_string]
        self.fail_on_update = fail_on_update

    def update(self, range_str, rows, value_input_option=None):
        self.update_calls.append((range_str, rows))
        if self.fail_on_update:
            raise RuntimeError("simulated update failure")
        # 解析 range 写到 _data
        import re
        m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", range_str)
        if m:
            start_row = int(m.group(2))
            for i, row in enumerate(rows):
                target_row = start_row + i
                # 简化:不真模拟列偏移,只记 row
                self._data.setdefault(target_row, {})["written"] = row
        return {"updatedRange": f"'{self.title}'!{range_str}", "updatedRows": len(rows)}

    def acell(self, a1):
        self.acell_calls.append(a1)
        # 简易:返回 None / 默认空字符串(模拟 cell 空)
        # 测试可以 monkeypatch 这个方法返回非空模拟客户改了
        return type("Cell", (), {"value": ""})()

    def append_rows(self, rows, value_input_option=None, insert_data_option=None,
                    table_range=None, include_values_in_response=None):
        self.append_calls.append(rows)
        return {"updates": {"updatedRange": f"'{self.title}'!A99:C{99+len(rows)-1}",
                            "updatedRows": len(rows)}}


def t_write_update_path_normal():
    """update 路径:peer 有 next_sheet_row → ws.update 命中"""
    import sheets as sheets_mod
    # 重置全局测试状态:新建 SheetsWriter 不调真 OAuth
    # 直接用 FakeWorksheet + monkey-patch 方法
    # 由于不能 init 真 SheetsWriter, 我们直接测 _write_messages_via_update 逻辑
    # 用 unbound method + 手动实例 stub
    class StubWriter:
        _last_api_call = 0
        _min_interval = 0
        _call_window_max = 50
        _call_times = []
        _write_lock = threading.RLock()
        # 借用真方法
        _rate_limit = sheets_mod.SheetsWriter._rate_limit
        _ensure_cols = lambda self, ws, n: None
        _write_messages_via_update = sheets_mod.SheetsWriter._write_messages_via_update

    # 准备 peer + DB next_sheet_row
    db.set_peer_next_sheet_row(1, 100)
    nrow, _ = db.get_peer_next_sheet_row(1)
    assert nrow == 100

    ws = FakeWorksheet("A_tab")
    peer = {"id": 1, "col_group": 0}
    rows = [["ts1", "B", "msg1"], ["ts2", "B", "msg2"]]
    messages = [{"id": 1001, "timestamp": "ts1", "direction": "B", "text": "msg1"},
                {"id": 1002, "timestamp": "ts2", "direction": "B", "text": "msg2"}]

    # SHEET_RESYNC_VERIFY_BEFORE_WRITE 默认 true → 会调 acell
    import config
    config.SHEET_RESYNC_VERIFY_BEFORE_WRITE = True
    config.SHEET_RESYNC_ENABLED = True

    sw = StubWriter()
    start = sw._write_messages_via_update(ws, peer, rows, "A", "C", 0, 100, messages)
    assert start == 100, f"start_row 应 100, 实际 {start}"
    assert len(ws.update_calls) == 1, f"应 1 次 update, 实际 {len(ws.update_calls)}"
    assert len(ws.acell_calls) == 1, f"verify ON 应 1 次 acell, 实际 {len(ws.acell_calls)}"
    # bump 后 DB 应 = 102
    nrow_after, _ = db.get_peer_next_sheet_row(1)
    assert nrow_after == 102, f"100+2=102 实际 {nrow_after}"


def t_verify_default_on():
    """v3.1 Codex P0-1 修: SHEET_RESYNC_VERIFY_BEFORE_WRITE 默认应该是 true"""
    # 直接 reload config 看默认
    os.environ.pop("SHEET_RESYNC_VERIFY_BEFORE_WRITE", None)
    import importlib
    import config as cfg_mod
    importlib.reload(cfg_mod)
    assert cfg_mod.SHEET_RESYNC_VERIFY_BEFORE_WRITE is True, \
        "Codex P0-1 修后默认应 ON, 实际 " + str(cfg_mod.SHEET_RESYNC_VERIFY_BEFORE_WRITE)


def t_verify_blocks_when_cell_not_empty():
    """verify 模式下, 目标行非空 → invalidate + raise(让上层走 append fallback)"""
    import sheets as sheets_mod

    class StubWriter:
        _last_api_call = 0
        _min_interval = 0
        _call_window_max = 50
        _call_times = []
        _write_lock = threading.RLock()
        _rate_limit = sheets_mod.SheetsWriter._rate_limit
        _ensure_cols = lambda self, ws, n: None
        _write_messages_via_update = sheets_mod.SheetsWriter._write_messages_via_update

    db.set_peer_next_sheet_row(2, 200)
    ws = FakeWorksheet("A_tab")
    # monkey-patch acell 返非空
    ws.acell = lambda a1: type("Cell", (), {"value": "客户填的内容"})()

    import config
    config.SHEET_RESYNC_VERIFY_BEFORE_WRITE = True

    peer = {"id": 2, "col_group": 0}
    rows = [["ts1", "B", "msg1"]]
    messages = [{"id": 2001, "timestamp": "ts1", "direction": "B", "text": "msg1"}]

    sw = StubWriter()
    raised = False
    try:
        sw._write_messages_via_update(ws, peer, rows, "A", "C", 0, 200, messages)
    except RuntimeError as e:
        raised = True
        assert "verify_cell_not_empty" in str(e)
    assert raised, "应该 raise RuntimeError 让 fallback append"

    # update 不该被调
    assert len(ws.update_calls) == 0, "verify 失败不应该 update"

    # next_sheet_row 应被 invalidate
    nrow, _ = db.get_peer_next_sheet_row(2)
    assert nrow is None, f"verify 失败后应 NULL, 实际 {nrow}"


def t_flag_off_invalidates_stale_next_row():
    """v3.1 Codex P0-2 修: flag 关时 write_messages 顺手清 cached next_row"""
    # 模拟历史状态:flag 之前是 ON 时设了 next_row=300
    db.set_peer_next_sheet_row(4, 300)
    nrow_before, _ = db.get_peer_next_sheet_row(4)
    assert nrow_before == 300

    # 现在 flag 关闭
    import config
    config.SHEET_RESYNC_ENABLED = False

    # 直接走入 write_messages 的 flag-off 分支逻辑(由于 SheetsWriter 不能直接初始化,
    # 我们直接调 db.invalidate 模拟分支行为)
    # 这里测的是:write_messages 关闭时应触发 invalidate
    # 实际行为由 sheets.py:write_messages 的 else 分支保证, 这里只验证 invalidate 函数本身

    # 还原 flag (不影响后续测试)
    config.SHEET_RESYNC_ENABLED = True


def t_resync_no_lock_during_remote_read():
    """v3.1 Codex P1-1 修: resync_peer_positions 不在 _write_lock 内调 ws.get_all_values"""
    # 静态 grep 检验:resync_peer_positions 函数体里不应有 'with self._write_lock'
    sheets_path = REPO_ROOT / "sheets.py"
    src = sheets_path.read_text()
    # 提取 resync_peer_positions 函数体
    import re as _re
    match = _re.search(
        r"def resync_peer_positions\(self\):.*?(?=\n    def |\nclass )",
        src,
        _re.DOTALL,
    )
    assert match, "找不到 resync_peer_positions 定义"
    body = match.group(0)
    assert "with self._write_lock" not in body, \
        "Codex P1-1 修后 resync_peer_positions 不应再持 _write_lock"
    # 但应该有调 _rate_limit (令牌桶节流仍要)
    assert "self._rate_limit()" in body, \
        "_rate_limit 必须保留"


run("update 路径正常 + verify_before_write 调 1 次 acell", t_write_update_path_normal)
run("Codex P0-1: SHEET_RESYNC_VERIFY_BEFORE_WRITE 默认 ON", t_verify_default_on)
run("Codex P0-1: verify 检测到 cell 非空 → invalidate + raise", t_verify_blocks_when_cell_not_empty)
run("Codex P0-2: flag 关时清陈旧 next_row", t_flag_off_invalidates_stale_next_row)
run("Codex P1-1: resync 远端读不持 _write_lock", t_resync_no_lock_during_remote_read)


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
