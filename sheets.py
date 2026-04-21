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
        # v2.10.23: 按账号的 flush 退避状态 — 单账号 429 不影响其他账号写入
        # {account_id: unix_ts_next_retry}
        self._flush_backoff = {}
        # {account_id: 当前退避级别,用于指数退避}
        self._flush_backoff_level = {}
        logger.info("Google Sheets 连接成功 (OAuth): %s", self.spreadsheet.title)
        self.ensure_alert_tabs()
        self.ensure_account_tabs()  # v2.10.10: 启动时给 DB 里每个账号补缺失的分页

    def ensure_account_tabs(self):
        """v2.10.10: 扫 DB 所有账号,分页不存在就建一个(对齐登录时 _create_sheet_tab 的承诺)。
        解决升级前登录的账号没有对应分页的历史遗留问题。

        v2.10.16: 改用 create_account_tab_full 建完整模板(青头 + 对话槽 + 冻结
        + 斑马纹),跟登录时 web._create_sheet_tab 产出一致,不再是 3 行阉割版。

        v2.10.17: 对已存在的账号分页也扫一遍,如果是阉割版(frozen<6)就原地升级成完整模板,
        数据不丢(row 7+ 保留)。解决存量客户已经有阉割版分页的历史遗留问题。"""
        try:
            accounts = db.get_conn().execute(
                "SELECT id, phone, name, sheet_tab, operator, company FROM accounts"
            ).fetchall()
        except Exception as e:
            logger.warning("ensure_account_tabs: 读 DB 失败 %s", e)
            return
        if not accounts:
            return
        existing_ws = {ws.title: ws for ws in self.spreadsheet.worksheets()}
        # v2.10.17: 先把所有分页的 frozenRowCount 拉下来, 避免循环里逐个 fetch metadata
        frozen_map = self._fetch_frozen_rows_map()
        created = 0
        upgraded = 0
        for a in accounts:
            tab_name = a["sheet_tab"] or a["name"] or a["phone"]
            if not tab_name:
                continue
            if tab_name in existing_ws:
                # v2.10.17: 已存在 → 检查是不是阉割版,是就升级
                ws = existing_ws[tab_name]
                if frozen_map.get(ws.id, 0) < 6:
                    try:
                        self.upgrade_minimal_tab(ws)
                        logger.info("ensure_account_tabs: 升级阉割版「%s」→ 完整模板", tab_name)
                        upgraded += 1
                    except Exception as e:
                        logger.warning("ensure_account_tabs: 升级「%s」失败 %s", tab_name, e)
                continue
            # 不存在 → 建完整版
            try:
                self.create_account_tab_full(
                    name=tab_name,
                    operator=a["operator"] or "",
                    company=a["company"] or "",
                )
                logger.info("ensure_account_tabs: 补建分页「%s」", tab_name)
                created += 1
            except Exception as e:
                logger.warning("ensure_account_tabs: 补建「%s」失败 %s", tab_name, e)
        if created:
            logger.info("ensure_account_tabs: 共补建 %d 个账号分页", created)
        if upgraded:
            logger.info("ensure_account_tabs: 共升级 %d 个阉割版分页", upgraded)

    def _fetch_frozen_rows_map(self):
        """拉一次 metadata,返回 {sheetId: frozenRowCount} dict。
        v2.10.17: 升级检测用,避免循环里 N 次 API 调用。"""
        try:
            self._rate_limit()
            meta = self.spreadsheet.fetch_sheet_metadata()
            result = {}
            for s in meta.get("sheets", []):
                sid = s.get("properties", {}).get("sheetId")
                frozen = s.get("properties", {}).get("gridProperties", {}).get("frozenRowCount", 0)
                if sid is not None:
                    result[sid] = frozen
            return result
        except Exception as e:
            logger.warning("_fetch_frozen_rows_map 失败: %s", e)
            return {}

    def _fetch_banded_ranges(self, sheet_id):
        """拉指定 sheet 的所有 bandedRange id, 用于升级前清掉旧斑马纹。"""
        try:
            self._rate_limit()
            meta = self.spreadsheet.fetch_sheet_metadata()
            for s in meta.get("sheets", []):
                if s.get("properties", {}).get("sheetId") == sheet_id:
                    return [b["bandedRangeId"] for b in s.get("bandedRanges", []) if "bandedRangeId" in b]
        except Exception as e:
            logger.warning("_fetch_banded_ranges[%s] 失败: %s", sheet_id, e)
        return []

    def upgrade_minimal_tab(self, ws):
        """v2.10.17: 把阉割版账号分页(只有 row 1-3 粗糙 header,无 frozen/对话槽/斑马纹)
        原地升级成完整模板,消息数据(row 7+)完全保留。

        安全前提: tg-monitor 消息写入从 row 7 开始(见 write_messages 里 current_row=max(len+1, 7)),
        阉割版 row 4-6 本来就是空的,可以直接覆盖不影响数据。

        做的事:
        1. 补 row 5-6 对话槽 header(A/B + 外事号/PEER_ROLE_LABEL + 外事号 TG 名;C6 保护已有 peer 名)
        2. 重画 row 1-6 的背景色 + 整张表 center_middle
        3. 冻结 6 行
        4. 列宽 A=180 / B=192 / C=350
        5. 清旧斑马纹,加 10 槽新斑马纹
        """
        sheet_id = ws.id
        tab_name = ws.title
        TOTAL_COLS = 30
        TOTAL_ROWS = max(ws.row_count, 1000)

        # 颜色常量(跟 create_account_tab_full 一致)
        CYAN = {"red": 0.3019608, "green": 0.8156863, "blue": 0.88235295}
        WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
        LIGHT_BLUE = {"red": 0.8784314, "green": 0.96862745, "blue": 0.98039216}
        TEAL = {"red": 0.29803923, "green": 0.69803923, "blue": 0.69803923}
        center_middle = {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}

        # Step 1: 补 row 5-6 对话槽 header,保护 C6(可能已经被 setup_dialog_columns 填过 peer 名)
        try:
            self._rate_limit()
            c6_existing = ws.acell("C6").value or ""
        except Exception:
            c6_existing = ""
        self._rate_limit()
        ws.update("A5:C6", [
            ["A", "外事号", tab_name],
            ["B", config.PEER_ROLE_LABEL, c6_existing],
        ])

        # Step 2: 先清掉现有 banding(否则 addBanding 会冲突)
        banding_ids = self._fetch_banded_ranges(sheet_id)

        def _repeat(r0, r1, c0, c1, fmt):
            return {"repeatCell": {
                "range": {"sheetId": sheet_id,
                          "startRowIndex": r0, "endRowIndex": r1,
                          "startColumnIndex": c0, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": fmt},
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
            }}

        def _col_dim(c0, c1, size):
            return {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": c0, "endIndex": c1},
                "properties": {"pixelSize": size}, "fields": "pixelSize",
            }}

        requests = []
        # 2a: 清旧 banding
        for bid in banding_ids:
            requests.append({"deleteBanding": {"bandedRangeId": bid}})
        # 2b: 整张表 WHITE + center — fields 遮罩**不含 textFormat**,
        # 避免把 row 7+ 已删除消息的红字+删除线(mark_deleted_in_sheet 打的)清掉
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id,
                      "startRowIndex": 0, "endRowIndex": TOTAL_ROWS,
                      "startColumnIndex": 0, "endColumnIndex": TOTAL_COLS},
            "cell": {"userEnteredFormat": {"backgroundColor": WHITE, **center_middle}},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment)",
        }})
        # 2c: row 1-6 的背景色(header 区域,随便覆盖 textFormat)
        requests.extend([
            _repeat(0, 1, 0, TOTAL_COLS, {"backgroundColor": CYAN, **center_middle}),
            _repeat(1, 2, 0, TOTAL_COLS, {"backgroundColor": WHITE, **center_middle}),
            _repeat(2, 3, 0, TOTAL_COLS, {"backgroundColor": LIGHT_BLUE, **center_middle}),
            _repeat(3, 4, 0, TOTAL_COLS, {
                "backgroundColor": WHITE,
                "textFormat": {"bold": True, "foregroundColor": WHITE},
                **center_middle,
            }),
            _repeat(4, 6, 0, 3, {
                "backgroundColor": TEAL,
                "textFormat": {"bold": True},
                **center_middle,
            }),
        ])
        # 2d: 冻结 6 行
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 6}},
            "fields": "gridProperties.frozenRowCount",
        }})
        # 2e: 列宽 A=180 B=192 C=350
        requests.extend([
            _col_dim(0, 1, 180),
            _col_dim(1, 2, 192),
            _col_dim(2, 3, 350),
        ])
        # 2f: 10 槽斑马纹(row 7+)
        for slot in range(TOTAL_COLS // 3):
            requests.append({"addBanding": {
                "bandedRange": {
                    "range": {"sheetId": sheet_id,
                              "startRowIndex": 6, "endRowIndex": TOTAL_ROWS,
                              "startColumnIndex": slot * 3, "endColumnIndex": slot * 3 + 3},
                    "rowProperties": {
                        "firstBandColor": LIGHT_BLUE,
                        "secondBandColor": WHITE,
                    },
                },
            }})

        self._rate_limit()
        self.spreadsheet.batch_update({"requests": requests})

    def create_account_tab_full(self, name, operator="", company=""):
        """v2.10.16: 账号分页完整模板 — 统一登录时 web._create_sheet_tab 和
        sweep ensure_account_tabs 两路的建分页逻辑,保证格式一致。

        name: 外事号 TG 昵称 (会写到 row 5 col C)
        operator: 商务人员 (B2;空就留白等用户自己填)
        company: 所属中心/部门 (B3;空就默认用 .env 的 COMPANY_DISPLAY)

        若分页已存在 → 直接返回 existing worksheet(幂等)。
        """
        # 幂等:已存在就返回
        existing = {ws.title: ws for ws in self.spreadsheet.worksheets()}
        if name in existing:
            return existing[name]

        # company 默认读 env COMPANY_DISPLAY(tg-monitor 进程里 config.COMPANY_DISPLAY 已注入)
        if not company:
            company = config.COMPANY_DISPLAY or config.COMPANY_NAME or ""

        TOTAL_ROWS = 1000
        TOTAL_COLS = 30

        self._rate_limit()
        ws = self.spreadsheet.add_worksheet(title=name, rows=TOTAL_ROWS, cols=TOTAL_COLS)
        sheet_id = ws.id

        # 颜色常量
        CYAN = {"red": 0.3019608, "green": 0.8156863, "blue": 0.88235295}
        WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
        LIGHT_BLUE = {"red": 0.8784314, "green": 0.96862745, "blue": 0.98039216}
        TEAL = {"red": 0.29803923, "green": 0.69803923, "blue": 0.69803923}

        # 文字内容: label A2/A3 + value B2/B3 + 对话槽标题 row5-6
        self._rate_limit()
        ws.update("A2:B3", [
            [config.OPERATOR_LABEL, operator],
            ["中心/部门", company],
        ])
        # C6 留空(第一条消息进来时 setup_dialog_columns 会填真实 peer 名)
        self._rate_limit()
        ws.update("A5:C6", [
            ["A", "外事号", name],
            ["B", config.PEER_ROLE_LABEL, ""],
        ])

        center_middle = {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}

        def _repeat(r0, r1, c0, c1, fmt):
            return {"repeatCell": {
                "range": {"sheetId": sheet_id,
                          "startRowIndex": r0, "endRowIndex": r1,
                          "startColumnIndex": c0, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": fmt},
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
            }}

        def _col_dim(c0, c1, size):
            return {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": c0, "endIndex": c1},
                "properties": {"pixelSize": size}, "fields": "pixelSize",
            }}

        requests = [
            # 整张表置中
            _repeat(0, TOTAL_ROWS, 0, TOTAL_COLS, {"backgroundColor": WHITE, **center_middle}),
            # Row 1: 青色横条
            _repeat(0, 1, 0, TOTAL_COLS, {"backgroundColor": CYAN, **center_middle}),
            # Row 2: 白底
            _repeat(1, 2, 0, TOTAL_COLS, {"backgroundColor": WHITE, **center_middle}),
            # Row 3: 淡蓝底
            _repeat(2, 3, 0, TOTAL_COLS, {"backgroundColor": LIGHT_BLUE, **center_middle}),
            # Row 4: 白底白字 spacer
            _repeat(3, 4, 0, TOTAL_COLS, {
                "backgroundColor": WHITE,
                "textFormat": {"bold": True, "foregroundColor": WHITE},
                **center_middle,
            }),
            # Row 5-6 第一个对话槽 A-C: 青绿 + 粗体
            _repeat(4, 6, 0, 3, {
                "backgroundColor": TEAL,
                "textFormat": {"bold": True},
                **center_middle,
            }),
            # 冻结前 6 行
            {"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 6}},
                "fields": "gridProperties.frozenRowCount",
            }},
            # 列宽: A=180, B=192, C=350
            _col_dim(0, 1, 180),
            _col_dim(1, 2, 192),
            _col_dim(2, 3, 350),
            # 斑马纹: 10 个对话槽都预先带
            *[
                {"addBanding": {
                    "bandedRange": {
                        "range": {"sheetId": sheet_id,
                                  "startRowIndex": 6, "endRowIndex": TOTAL_ROWS,
                                  "startColumnIndex": slot * 3, "endColumnIndex": slot * 3 + 3},
                        "rowProperties": {
                            "firstBandColor": LIGHT_BLUE,
                            "secondBandColor": WHITE,
                        },
                    },
                }}
                for slot in range(TOTAL_COLS // 3)
            ],
        ]

        self._rate_limit()
        self.spreadsheet.batch_update({"requests": requests})
        return ws

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
        """获取账号分表,v2.10.10 起真的会 auto-create(以前只 get)。

        创建出来只填基础 A2/A3 表头(商务人员 label + 中心/部门 label),
        B2/B3 留空给商务自己填。第一条消息进来时对话槽列会自动填。
        """
        tab_name = account["sheet_tab"] or account["name"] or account["phone"]
        try:
            return self.spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            pass
        # v2.10.10: 自动建
        try:
            logger.info("账号分页「%s」不存在 → 自动建立", tab_name)
            self._rate_limit()
            ws = self.spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=30)
            self._init_sheet_header(ws, account)
            return ws
        except Exception as e:
            logger.warning("建立账号分页失败「%s」: %s", tab_name, e)
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
        v2.6.5: 同时检查每个对话槽 row 6 的 PEER_ROLE_LABEL (B6/E6/H6/K6/...),
        不一致就批量 update。覆盖客户改 PEER_ROLE_LABEL 后,把现有所有对话槽角色字样
        从「广告主」改成新 label 的需求。
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

            # v2.6.5: 同步对话槽 row 6 的 PEER_ROLE_LABEL (B6/E6/H6/K6/...)
            # 对话槽布局: 每槽占 3 列,起始列 = col_group * 3, 角色 label 在 col_group * 3 + 1
            try:
                self._rate_limit()
                row6 = ws.row_values(6)
                updates = []
                for i, val in enumerate(row6):
                    # 位置 1, 4, 7, 10, ... (i % 3 == 1) 是角色 label 单元格
                    if i % 3 == 1 and val and val != config.PEER_ROLE_LABEL:
                        col = _col_letter(i)
                        updates.append({"range": f"{col}6", "values": [[config.PEER_ROLE_LABEL]]})
                if updates:
                    self._rate_limit()
                    ws.batch_update(updates)
                    logger.info("外事号分页对话槽角色标签同步 [%s] %d 处 → %s",
                                ws.title, len(updates), config.PEER_ROLE_LABEL)
            except Exception as e:
                logger.warning("外事号分页 row6 角色标签同步失败 [%s]: %s", ws.title, e)

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

            # v2.10.23: 写完立刻标 sheet_written=1,同时回头看「写入期间是否被删」— 如果
            # 有消息在 sheet_row 设好前已经删了(delete_mark_pending=1),这里立刻补标红删除线。
            # 修正之前「消息未写就被删 → 永远不会红字标记」的 bug。
            pending_delete_rows = []  # [(row, msg_db_id)]
            for i, m in enumerate(messages):
                db.mark_written(m["id"], current_row + i)
                if db.check_delete_mark_pending(m["id"]):
                    pending_delete_rows.append((current_row + i, m["id"]))

            logger.info("写入 %d 条到 %s 列组%d (%s-%s列)", len(rows), ws.title, col_group, col_a, col_c)

            # 补红(可能失败但不致命,下次 patrol 也会补)
            for row, msg_db_id in pending_delete_rows:
                try:
                    self._mark_row_red_strikethrough(ws, row, col_group)
                    db.clear_delete_mark_pending(msg_db_id)
                    logger.info("[delete_backfill] 补标红 msg_db_id=%s row=%s", msg_db_id, row)
                except Exception as e:
                    logger.warning("[delete_backfill] 补标红失败 msg_db_id=%s: %s", msg_db_id, e)

    def mark_deleted_in_sheet(self, ws, msg):
        """把被删除的消息标红+删除线"""
        if not msg["sheet_row"] or msg["sheet_row"] == 0:
            return

        peer = db.get_conn().execute(
            "SELECT * FROM peers WHERE id=?", (msg["peer_id"],)
        ).fetchone()
        if not peer:
            return

        self._mark_row_red_strikethrough(ws, msg["sheet_row"], peer["col_group"])

    def _mark_row_red_strikethrough(self, ws, row, col_group):
        """v2.10.23: 红字+删除线格式化(3 列宽) — 内部 helper,避免重复查 peer。
        供 mark_deleted_in_sheet 和 write_messages 的 delete_mark_pending 补红用。"""
        col_start = col_group * 3
        col_a = _col_letter(col_start)
        col_c = _col_letter(col_start + 2)
        self._rate_limit()
        ws.format(f"{col_a}{row}:{col_c}{row}", {
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": {"red": 1, "green": 0, "blue": 0}},
                "strikethrough": True,
            }
        })

    def flush_pending(self):
        """批量写入所有未写入的消息。

        v2.10.23:改成按账号分桶。以前全局 LIMIT 500,任一账号撞 429 / 出错
        都会中断整批,下一轮又从同一批老消息卡起 → 苏总看到的「表格空白但 DB
        有」就是这样来的。现在:
        - 每账号独立桶,每轮最多 100 条/账号
        - 单账号失败 try/except 隔离,不影响其他账号
        - 429 per-account 指数退避(5s → 10s → 20s → ... → 600s),不卡全局
        """
        account_ids = db.get_accounts_with_unwritten()
        if not account_ids:
            return 0

        total = 0
        now = time.time()
        with self._write_lock:
            for account_id in account_ids:
                # 在退避中的账号本轮跳过
                next_retry = self._flush_backoff.get(account_id, 0)
                if now < next_retry:
                    continue

                try:
                    msgs = db.get_unwritten_messages_by_account(account_id, limit=100)
                    if not msgs:
                        continue
                    written = self._flush_account(account_id, msgs)
                    total += written
                    # 成功 → 重置退避
                    if self._flush_backoff_level.get(account_id, 0) > 0:
                        logger.info("[sheets_flush] account=%s 恢复正常", account_id)
                    self._flush_backoff_level[account_id] = 0
                    self._flush_backoff[account_id] = 0
                except gspread.exceptions.APIError as e:
                    msg = str(e)
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
                        lvl = self._flush_backoff_level.get(account_id, 0)
                        wait = min(5 * (2 ** lvl), 600)
                        self._flush_backoff[account_id] = now + wait
                        self._flush_backoff_level[account_id] = lvl + 1
                        logger.warning(
                            "[sheets_flush] account=%s 触 429/quota,退避 %ds (level=%d)",
                            account_id, wait, lvl + 1,
                        )
                    else:
                        logger.error("[sheets_flush] account=%s gspread 异常: %s", account_id, e)
                    continue
                except Exception as e:
                    logger.error(
                        "[sheets_flush] account=%s flush 失败,下次重试: %s",
                        account_id, e,
                    )
                    continue
        return total

    def _flush_account(self, account_id, msgs):
        """v2.10.23:单账号 flush — 这个账号的所有 peer 都在这里处理。
        任何异常 raise 出去由 flush_pending 的 try/except 处理退避,不污染其他账号。"""
        account = db.get_conn().execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not account:
            return 0

        ws = self.get_or_create_sheet(account)
        if not ws:
            return 0

        by_peer = {}
        for m in msgs:
            pid = m["peer_id"]
            if pid not in by_peer:
                by_peer[pid] = []
            by_peer[pid].append(m)

        total = 0
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

    def _do_flush(self, unwritten):
        """[Legacy] 老版本全局 flush — 保留签名向后兼容,内部走按账号分桶。
        新代码请直接用 flush_pending()。"""
        return self.flush_pending()


def _col_letter(index):
    """0-based index -> Excel 列字母"""
    result = ""
    while True:
        result = chr(65 + index % 26) + result
        index = index // 26 - 1
        if index < 0:
            break
    return result
