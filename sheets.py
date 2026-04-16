"""Google Sheets 写入器 — 3 列横排格式，从 A 列开始

Level 1 架构后:认证路径只走 OAuth 用户凭证(跟 media_uploader.py 一致)。
Service Account 路径已完全移除。

SHEET_ID 允许首次为空 — 上层 web.py 会在 OAuth 授权完成后,通过
`/api/sheets/auto-create` 路由调用 oauth_helper.auto_create_sheet()
建好一个新的 Spreadsheet,把 id 写回 .env 再启动 tg-monitor 容器。
所以 tg-monitor 启动时 SHEET_ID 必然已经有值。
"""
import logging
import time
import threading
from datetime import datetime, timedelta, timezone
import gspread

import config
import database as db
import oauth_helper

TZ_BJ = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


class SheetsWriter:
    def __init__(self):
        if not config.SHEET_ID:
            raise RuntimeError(
                "SHEET_ID 为空 — 请先在 setup 精灵完成 Google 授权并点「自动建表格」"
            )
        creds = oauth_helper.get_credentials()
        if not creds:
            raise RuntimeError(
                "OAuth 凭证不存在 — 请先在 setup 精灵完成 Google 授权"
            )
        self.gc = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(config.SHEET_ID)
        self._last_api_call = 0
        self._min_interval = 1.5
        self._write_lock = threading.Lock()
        logger.info("Google Sheets 连接成功 (OAuth): %s", self.spreadsheet.title)
        self.ensure_alert_tabs()

    # 告警分页表头（跟苏总现有 Sheet 格式 1:1 对齐）
    # 注意: "广告主" 这个位置用 config.PEER_ROLE_LABEL 动态替换，各部门可能叫「广告主/客户/合作方」等
    @property
    def ALERT_HEADERS(self):
        role = config.PEER_ROLE_LABEL
        op = config.OPERATOR_LABEL
        return {
            "信息未回复预警": ["所属公司", op, "外事号", role, "未回复消息", "记录时间"],
            "关键词监听":   ["所属公司", op, "外事号", role, "关键词", "消息内容", "记录时间"],
            "信息删除预警": ["所属公司", op, "外事号", role, "删除前消息内容", "记录时间"],
        }

    # 各列宽度（按表头文本映射，像素）— 角色列 + 操作人员列都用动态 label 做 key
    @property
    def ALERT_COL_WIDTHS(self):
        return {
            "所属公司":       160,
            config.OPERATOR_LABEL: 100,
            "外事号":         100,
            config.PEER_ROLE_LABEL: 160,
            "未回复消息":     350,
            "消息内容":       350,
            "删除前消息内容": 350,
            "关键词":         100,
            "记录时间":       180,
        }

    def _write_alert_header(self, ws, prefix):
        """给告警分页写表头 + 上色 + 冻结首行 + 调列宽（幂等：只在空白分页执行）"""
        headers = self.ALERT_HEADERS[prefix]
        try:
            self._rate_limit()
            first_row = ws.row_values(1)
        except Exception:
            first_row = []
        if first_row:  # 已有表头
            # 角色列（第 4 列，index=3）label 可能被 /settings 改过 → 同步
            role_idx = 3
            if len(first_row) > role_idx and first_row[role_idx] and first_row[role_idx] != config.PEER_ROLE_LABEL:
                try:
                    self._rate_limit()
                    cell = f"{chr(65 + role_idx)}1"  # D1
                    ws.update(cell, [[config.PEER_ROLE_LABEL]])
                    logger.info("告警分页角色列标签同步 [%s] %s → %s", ws.title, first_row[role_idx], config.PEER_ROLE_LABEL)
                except Exception as e:
                    logger.warning("告警分页角色列标签同步失败 [%s]: %s", ws.title, e)
            # v2.6.4: 操作人员列(第 2 列, index=1)label 同步,逻辑跟角色列一致
            op_idx = 1
            if len(first_row) > op_idx and first_row[op_idx] and first_row[op_idx] != config.OPERATOR_LABEL:
                try:
                    self._rate_limit()
                    cell = f"{chr(65 + op_idx)}1"  # B1
                    ws.update(cell, [[config.OPERATOR_LABEL]])
                    logger.info("告警分页操作人员列标签同步 [%s] %s → %s", ws.title, first_row[op_idx], config.OPERATOR_LABEL)
                except Exception as e:
                    logger.warning("告警分页操作人员列标签同步失败 [%s]: %s", ws.title, e)
            return
        try:
            self._rate_limit()
            ws.update("A1", [headers])
            self._rate_limit()
            ws.format(f"A1:{chr(64+len(headers))}1", {
                "backgroundColor": {"red": 0.3, "green": 0.7, "blue": 0.7},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "horizontalAlignment": "CENTER",
            })
            self._rate_limit()
            ws.freeze(rows=1)
            # 设列宽（batch_update 一次搞定）
            width_requests = []
            for i, h in enumerate(headers):
                w = self.ALERT_COL_WIDTHS.get(h, 120)
                width_requests.append({
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": i,
                            "endIndex": i + 1,
                        },
                        "properties": {"pixelSize": w},
                        "fields": "pixelSize",
                    }
                })
            # 给 row 2+ 加双色斑马纹，跟分表统一视觉
            width_requests.append({
                "addBanding": {
                    "bandedRange": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 1,  # row 2 起（0-based = 1）
                            "endRowIndex": ws.row_count,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(headers),
                        },
                        "rowProperties": {
                            "firstBandColor": {"red": 0.8784314, "green": 0.96862745, "blue": 0.98039216},  # 浅蓝
                            "secondBandColor": {"red": 1.0, "green": 1.0, "blue": 1.0},  # 白
                        },
                    },
                }
            })
            if width_requests:
                self._rate_limit()
                try:
                    self.spreadsheet.batch_update({"requests": width_requests})
                except Exception as e:
                    # banding 可能已存在，去掉 addBanding 重试
                    logger.warning("预警分页 batch_update 失败，退回无斑马纹: %s", e)
                    self._rate_limit()
                    self.spreadsheet.batch_update({
                        "requests": [r for r in width_requests if "addBanding" not in r]
                    })
            logger.info("预警分页表头写入 + 列宽调整 + 斑马纹: %s", ws.title)
        except Exception as e:
            logger.warning("预警分页表头写入失败 (%s): %s", ws.title, e)

    def ensure_alert_tabs(self):
        """自动建立/修正预警分页（信息未回复预警、关键词监听、信息删除预警）"""
        suffix = config.COMPANY_DISPLAY
        needed = [
            (f"信息未回复预警{suffix}", "信息未回复预警"),
            (f"关键词监听{suffix}",     "关键词监听"),
            (f"信息删除预警{suffix}",   "信息删除预警"),
        ]
        existing = {ws.title: ws for ws in self.spreadsheet.worksheets()}

        for tab_name, prefix in needed:
            if tab_name in existing:
                # 已存在 → 检查表头是否存在，补写
                self._write_alert_header(existing[tab_name], prefix)
                continue

            # 检查是否有旧名（比如 YD 后缀的）需要改名
            base = prefix
            renamed = False
            for old_title, ws in existing.items():
                if old_title.startswith(base) and old_title != tab_name:
                    try:
                        self._rate_limit()
                        ws.update_title(tab_name)
                        logger.info("预警分页改名: %s → %s", old_title, tab_name)
                        self._write_alert_header(ws, prefix)
                        renamed = True
                    except Exception as e:
                        logger.warning("预警分页改名失败: %s", e)
                    break

            if not renamed:
                try:
                    self._rate_limit()
                    ws = self.spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=10)
                    logger.info("自动建立预警分页: %s", tab_name)
                    self._write_alert_header(ws, prefix)
                except Exception as e:
                    logger.warning("建立预警分页失败: %s", e)

    def _rate_limit(self):
        elapsed = time.time() - self._last_api_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_api_call = time.time()

    def get_or_create_sheet(self, account):
        """获取已有的分表（不自动创建，需要商务手动建好）"""
        tab_name = account["sheet_tab"] or account["name"] or account["phone"]
        try:
            ws = self.spreadsheet.worksheet(tab_name)
            return ws
        except gspread.WorksheetNotFound:
            logger.warning("找不到分表「%s」", tab_name)
            return None

    def _ensure_cols(self, ws, needed_cols):
        """确保工作表有足够的列数"""
        if ws.col_count < needed_cols:
            self._rate_limit()
            ws.resize(cols=needed_cols)

    def _init_sheet_header(self, ws, account):
        """初始化分表表头 (行1-3)"""
        self._rate_limit()
        ws.update("A1:B3", [
            ["", ""],
            [config.OPERATOR_LABEL, account["operator"] or ""],
            ["中心/部门", account["company"] or ""],
        ])
        self._rate_limit()
        ws.format("A1:Z1", {"backgroundColor": {"red": 0.3, "green": 0.7, "blue": 0.7}})
        ws.format("A2:A3", {"textFormat": {"bold": True}})

    def sync_headers(self):
        """从 Sheets 读取表头，同步更新数据库（商务人员、所属公司）

        v2.6.4: 同时检查 A2 单元格 label 是否跟 OPERATOR_LABEL 一致,不一致就 update。
        覆盖客户改 OPERATOR_LABEL 后,把现有所有外事号分页 A2 字样从「商务人员」改成新 label 的需求。
        """
        accounts = db.get_conn().execute("SELECT * FROM accounts").fetchall()
        for account in accounts:
            ws = self.get_or_create_sheet(account)
            if not ws:
                continue
            # 读取行2-3的 A 列(label) + B 列(value)
            self._rate_limit()
            header_data = ws.get("A2:B3")
            if header_data:
                a2_label = header_data[0][0] if len(header_data) > 0 and len(header_data[0]) > 0 else ""
                operator = header_data[0][1] if len(header_data) > 0 and len(header_data[0]) > 1 else ""
                company  = header_data[1][1] if len(header_data) > 1 and len(header_data[1]) > 1 else ""
                # 更新数据库
                if operator != (account["operator"] or "") or company != (account["company"] or ""):
                    db.get_conn().execute(
                        "UPDATE accounts SET operator=?, company=? WHERE id=?",
                        (operator, company, account["id"])
                    )
                    db.get_conn().commit()
                    logger.info("从 Sheets 同步: 操作人员=%s, 所属公司=%s", operator, company)
                # v2.6.4: 同步 A2 label(操作人员标签)
                if a2_label and a2_label != config.OPERATOR_LABEL:
                    try:
                        self._rate_limit()
                        ws.update("A2", [[config.OPERATOR_LABEL]])
                        logger.info("外事号分页操作人员标签同步 [%s] %s → %s",
                                    ws.title, a2_label, config.OPERATOR_LABEL)
                    except Exception as e:
                        logger.warning("外事号分页 A2 标签同步失败 [%s]: %s", ws.title, e)

    def setup_dialog_columns(self, ws, peer, col_group):
        """设置某个对话框的表头 (行5-6) + 整列置中 + 斑马纹 (与舒舒格式一致)"""
        col_start = col_group * 3
        account = db.get_conn().execute(
            "SELECT * FROM accounts WHERE id=?", (peer["account_id"],)
        ).fetchone()

        account_name = account["name"] if account else ""
        peer_name = peer["name"] or f"用户{peer['tg_id']}"

        col_a = _col_letter(col_start)
        col_c = _col_letter(col_start + 2)

        self._rate_limit()
        ws.update(f"{col_a}5:{col_c}6", [
            ["A", "外事号", account_name],
            ["B", config.PEER_ROLE_LABEL, peer_name],
        ])

        # 颜色常量
        TEAL = {"red": 0.29803923, "green": 0.69803923, "blue": 0.69803923}
        WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
        LIGHT_BLUE = {"red": 0.8784314, "green": 0.96862745, "blue": 0.98039216}
        center_middle = {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}

        total_rows = ws.row_count

        requests = [
            # Row 5-6 对话槽标题: 青绿 + 粗体 + 置中
            {"repeatCell": {
                "range": {"sheetId": ws.id,
                          "startRowIndex": 4, "endRowIndex": 6,
                          "startColumnIndex": col_start, "endColumnIndex": col_start + 3},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": TEAL,
                    "textFormat": {"bold": True},
                    **center_middle,
                }},
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
            }},
            # Row 7+ 置中对齐
            {"repeatCell": {
                "range": {"sheetId": ws.id,
                          "startRowIndex": 6, "endRowIndex": total_rows,
                          "startColumnIndex": col_start, "endColumnIndex": col_start + 3},
                "cell": {"userEnteredFormat": center_middle},
                "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment)",
            }},
            # 列宽
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": col_start, "endIndex": col_start + 1},
                "properties": {"pixelSize": 180}, "fields": "pixelSize"
            }},
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": col_start + 1, "endIndex": col_start + 2},
                "properties": {"pixelSize": 192}, "fields": "pixelSize"
            }},
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": col_start + 2, "endIndex": col_start + 3},
                "properties": {"pixelSize": 350}, "fields": "pixelSize"
            }},
            # 斑马纹: row 7+ 交替浅蓝/白
            {"addBanding": {
                "bandedRange": {
                    "range": {"sheetId": ws.id,
                              "startRowIndex": 6, "endRowIndex": total_rows,
                              "startColumnIndex": col_start, "endColumnIndex": col_start + 3},
                    "rowProperties": {
                        "firstBandColor": LIGHT_BLUE,
                        "secondBandColor": WHITE,
                    },
                },
            }},
        ]

        self._rate_limit()
        try:
            self.spreadsheet.batch_update({"requests": requests})
        except Exception as e:
            # 斑马纹可能已存在，去掉 addBanding 重试
            logger.warning("setup_dialog_columns batch_update 失败，退回无斑马纹: %s", e)
            self._rate_limit()
            self.spreadsheet.batch_update({"requests": [r for r in requests if "addBanding" not in r]})

    def write_messages(self, ws, peer, messages):
        """把消息写入对话框对应的列组"""
        if not messages:
            return

        col_group = peer["col_group"]
        col_start = col_group * 3
        col_a = _col_letter(col_start)
        col_c = _col_letter(col_start + 2)

        # 找出当前列组已有多少行数据（从 row 7 开始）
        self._rate_limit()
        existing = ws.col_values(col_start + 1)  # 1-based
        current_row = max(len(existing) + 1, 7)

        rows = []
        for m in messages:
            rows.append([m["timestamp"], m["direction"], m["text"]])

        if rows:
            end_row = current_row + len(rows) - 1
            self._rate_limit()
            # value_input_option=USER_ENTERED → =IMAGE() / =HYPERLINK() 公式才会渲染（启用 MEDIA_FOLDER_ID 后）
            # 普通文字消息不受影响：USER_ENTERED 对纯文本等同 RAW，只是不会自动转日期/数字
            ws.update(f"{col_a}{current_row}:{col_c}{end_row}", rows, value_input_option="USER_ENTERED")

            for i, m in enumerate(messages):
                db.mark_written(m["id"], current_row + i)

            logger.info("写入 %d 条到 %s 列组%d (%s-%s列)", len(rows), ws.title, col_group, col_a, col_c)

    def mark_deleted_in_sheet(self, ws, msg):
        """把被删除的消息标红+删除线"""
        if not msg["sheet_row"] or msg["sheet_row"] == 0:
            return

        peer = db.get_conn().execute(
            "SELECT * FROM peers WHERE id=?", (msg["peer_id"],)
        ).fetchone()
        if not peer:
            return

        col_start = peer["col_group"] * 3
        col_a = _col_letter(col_start)
        col_c = _col_letter(col_start + 2)
        row = msg["sheet_row"]

        self._rate_limit()
        ws.format(f"{col_a}{row}:{col_c}{row}", {
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": {"red": 1, "green": 0, "blue": 0}},
                "strikethrough": True,
            }
        })

    def flush_pending(self):
        """批量写入所有未写入的消息"""
        unwritten = db.get_unwritten_messages()
        if not unwritten:
            return 0

        with self._write_lock:
            return self._do_flush(unwritten)

    def _do_flush(self, unwritten):
        by_account = {}
        for m in unwritten:
            aid = m["account_id"]
            if aid not in by_account:
                by_account[aid] = []
            by_account[aid].append(m)

        total = 0
        for account_id, msgs in by_account.items():
            account = db.get_conn().execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if not account:
                continue

            ws = self.get_or_create_sheet(account)
            if not ws:
                continue

            by_peer = {}
            for m in msgs:
                pid = m["peer_id"]
                if pid not in by_peer:
                    by_peer[pid] = []
                by_peer[pid].append(m)

            for peer_id, peer_msgs in by_peer.items():
                peer = db.get_conn().execute(
                    "SELECT * FROM peers WHERE id=?", (peer_id,)
                ).fetchone()
                if not peer:
                    continue

                col_group = peer["col_group"]
                col_start = col_group * 3
                needed_cols = col_start + 3
                self._ensure_cols(ws, needed_cols)

                col_c = _col_letter(col_start + 2)
                self._rate_limit()
                # 检查 C6 (广告主名) 而不是 A5，避免预建分页时的空标题让检查误判
                cell_val = ws.acell(f"{col_c}6").value
                if not cell_val:
                    self.setup_dialog_columns(ws, peer, col_group)

                self.write_messages(ws, peer, peer_msgs)
                total += len(peer_msgs)

        return total


def _col_letter(index):
    """0-based index -> Excel 列字母"""
    result = ""
    while True:
        result = chr(65 + index % 26) + result
        index = index // 26 - 1
        if index < 0:
            break
    return result
