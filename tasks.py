"""定时任务 — 巡检、未回复检查、日报"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import gspread

import config
import database as db
from sheets import _col_letter

logger = logging.getLogger(__name__)

TZ_BJ = timezone(timedelta(hours=8))


class TaskScheduler:
    def __init__(self, listener, sheets_writer, alert_bot, startup_time=None):
        self.listener = listener
        self.sheets = sheets_writer
        self.bot = alert_bot
        self.startup_time = startup_time  # 只对启动后的消息触发预警
        self._running = False

    async def start(self):
        self._running = True
        logger.info("定时任务启动")
        await asyncio.gather(
            self._sheets_flush_loop(),
            self._patrol_loop(),
            self._no_reply_loop(),
            self._daily_report_loop(),
            self._peer_name_consistency_loop(),
        )

    async def stop(self):
        self._running = False

    async def _sync_account_names(self):
        """检查 TG 账号昵称是否改变，同步更新 DB + Sheets 分页名 + 表头"""
        for phone, client in self.listener.clients.items():
            try:
                me = await client.get_me()
                new_name = ((me.first_name or "") + " " + (me.last_name or "")).strip()
                account = db.get_account_by_phone(phone)
                if not account or not new_name:
                    continue

                old_name = account["name"] or ""
                if new_name == old_name:
                    continue

                # 昵称变了
                logger.info("[%s] 昵称变更: %s → %s", phone, old_name, new_name)

                # 1. 更新 DB
                conn = db.get_conn()
                conn.execute("UPDATE accounts SET name=? WHERE id=?", (new_name, account["id"]))
                conn.commit()

                # 2. 同步 Sheets 分页名
                old_tab = account["sheet_tab"] or old_name
                try:
                    ws = self.sheets.spreadsheet.worksheet(old_tab)
                    self.sheets._rate_limit()
                    ws.update_title(new_name)
                    logger.info("分页名同步: %s → %s", old_tab, new_name)
                except Exception as e:
                    logger.warning("分页名同步失败: %s", e)

                # 3. 更新 DB 的 sheet_tab
                conn.execute("UPDATE accounts SET sheet_tab=? WHERE id=?", (new_name, account["id"]))
                conn.commit()

                # 4. 同步表头中的外事号名称（第5行）
                try:
                    ws = self.sheets.spreadsheet.worksheet(new_name)
                    self.sheets._rate_limit()
                    all_vals = ws.row_values(5)
                    for i, val in enumerate(all_vals):
                        if val == old_name:
                            col_letter = chr(65 + i) if i < 26 else chr(64 + i // 26) + chr(65 + i % 26)
                            self.sheets._rate_limit()
                            ws.update_acell(f"{col_letter}5", new_name)
                    logger.info("表头外事号名称已同步")
                except Exception as e:
                    logger.warning("表头同步失败: %s", e)

            except Exception as e:
                logger.warning("昵称同步失败 [%s]: %s", phone, e)

    async def _enforce_sheet_tab_consistency(self):
        """确保 Sheets 分页名 = account.name。用户手改分页名会被自动改回 TG 名字。"""
        for account in db.get_all_accounts():
            target = account["name"]
            if not target:
                continue
            # 已存在正确分页 → OK
            try:
                self.sheets._rate_limit()
                self.sheets.spreadsheet.worksheet(target)
                # 顺手把 sheet_tab 对齐
                if account["sheet_tab"] != target:
                    conn = db.get_conn()
                    conn.execute("UPDATE accounts SET sheet_tab=? WHERE id=?", (target, account["id"]))
                    conn.commit()
                continue
            except gspread.WorksheetNotFound:
                pass

            # 找旧 sheet_tab 对应的分页，改名回 target
            old = account["sheet_tab"]
            if not old or old == target:
                continue
            try:
                self.sheets._rate_limit()
                ws = self.sheets.spreadsheet.worksheet(old)
                self.sheets._rate_limit()
                ws.update_title(target)
                conn = db.get_conn()
                conn.execute("UPDATE accounts SET sheet_tab=? WHERE id=?", (target, account["id"]))
                conn.commit()
                logger.info("分页名自动修正: %s → %s", old, target)

                # 同步表头第 5 行
                try:
                    self.sheets._rate_limit()
                    row5 = ws.row_values(5)
                    for i, val in enumerate(row5):
                        if val == old:
                            col = chr(65 + i) if i < 26 else chr(64 + i // 26) + chr(65 + i % 26)
                            self.sheets._rate_limit()
                            ws.update_acell(f"{col}5", target)
                    logger.info("表头外事号名称已修正")
                except Exception as e:
                    logger.warning("表头修正失败: %s", e)
            except gspread.WorksheetNotFound:
                logger.warning("分页名修正：旧分页「%s」也不存在，跳过", old)
            except Exception as e:
                logger.warning("分页名修正失败 %s → %s: %s", old, target, e)

    async def _sheets_flush_loop(self):
        """每 N 秒批量写入 Sheets"""
        while self._running:
            try:
                count = self.sheets.flush_pending()
                if count:
                    logger.info("批量写入 %d 条消息到 Sheets", count)
            except Exception as e:
                logger.error("Sheets 写入失败: %s", e)
            await asyncio.sleep(config.SHEETS_FLUSH_INTERVAL)

    async def _patrol_loop(self):
        """每 60 秒巡检：补漏 + 删除检测 + 同步表头 + 昵称同步"""
        # 启动后等一会儿再开始巡检
        await asyncio.sleep(30)
        while self._running:
            try:
                # 从 Sheets 同步表头（商务人员、所属公司）
                self.sheets.sync_headers()

                # 检查账号昵称是否改变
                await self._sync_account_names()

                # 分页名一致性检查：手改的分页名会被自动改回 TG 名字
                await self._enforce_sheet_tab_consistency()

                for phone, client in self.listener.clients.items():
                    # 删除检测
                    deleted_msgs = await self.listener.check_deleted(phone, days=config.PATROL_DAYS)
                    for m in deleted_msgs:
                        peer = db.get_conn().execute(
                            "SELECT * FROM peers WHERE id=?", (m["peer_id"],)
                        ).fetchone()
                        if peer and self.bot:
                            await self.bot.send_delete_alert(
                                m["account_id"], peer, m["text"], m["msg_id"]
                            )
                            # 标记表格中的删除
                            account = db.get_conn().execute(
                                "SELECT * FROM accounts WHERE id=?", (m["account_id"],)
                            ).fetchone()
                            if account:
                                ws = self.sheets.get_or_create_sheet(account)
                                self.sheets.mark_deleted_in_sheet(ws, m)
            except Exception as e:
                logger.error("巡检失败: %s", e)
            await asyncio.sleep(config.PATROL_INTERVAL)

    async def _no_reply_loop(self):
        """每分钟检查未回复。按 WORK_SCHEDULE 精确判断工作时段：
        - 当前不在工作时段（午休 13-15 / 晚休 19-20 / 周日等）→ 跳过
        - 累计未回复分钟按工作时段算（非工作时段不累计）"""
        await asyncio.sleep(15)
        while self._running:
            try:
                now = datetime.now(TZ_BJ)

                # 当前不在工作时段 → 这一轮不发任何预警
                if not config.is_work_time(now):
                    await asyncio.sleep(60)
                    continue

                accounts = db.get_all_accounts()
                for account in accounts:
                    candidates = db.get_unanswered_candidates(account["id"])
                    for row in candidates:
                        try:
                            last_dt = datetime.strptime(row["last_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_BJ)
                        except Exception:
                            continue

                        # 按工作时段累计分钟（午休/晚休/周日不算）
                        elapsed = config.work_elapsed_minutes(last_dt, now)
                        if elapsed < config.NO_REPLY_MINUTES:
                            continue

                        # 启动前已远超阈值的远古消息不补推
                        if self.startup_time:
                            try:
                                startup_dt = datetime.strptime(self.startup_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_BJ)
                                if config.work_elapsed_minutes(last_dt, startup_dt) > config.NO_REPLY_MINUTES:
                                    continue
                            except Exception:
                                pass

                        peer = db.get_peer(row["tg_id"], account["id"])
                        if peer and self.bot:
                            await self.bot.send_no_reply_alert(
                                account["id"], peer,
                                row["last_text"], row["last_msg_id"]
                            )
                            logger.info("未回复预警: %s <- %s (累计 %.1f 工作分钟)",
                                        account['name'], peer['name'], elapsed)
            except Exception as e:
                logger.error("未回复检查失败: %s", e)
            await asyncio.sleep(60)

    async def _peer_name_consistency_loop(self):
        """每 10 分钟巡检广告主名是否跟 DB 一致，对方在 TG 改名会通过新消息更新到 DB，
        这个 loop 把 DB 的最新名字同步回 Sheets 表头的第 6 行 C 列（或 F6, I6, L6...）"""
        # 启动后等一会儿
        await asyncio.sleep(120)
        while self._running:
            try:
                for account in db.get_all_accounts():
                    ws = self.sheets.get_or_create_sheet(account)
                    if not ws:
                        continue
                    peers = db.get_peers_by_account(account["id"])
                    peers = [p for p in peers if p["col_group"] >= 0]
                    if not peers:
                        continue

                    # 一次读取第 6 行
                    try:
                        self.sheets._rate_limit()
                        row6 = ws.row_values(6)
                    except Exception as e:
                        logger.warning("读 row 6 失败 [%s]: %s", account["name"], e)
                        continue

                    updates = []  # [(range, value)]
                    for peer in peers:
                        col_idx = peer["col_group"] * 3 + 2  # 0-based
                        current = row6[col_idx] if col_idx < len(row6) else ""
                        target = peer["name"] or f"用户{peer['tg_id']}"
                        # 只改已存在广告主格的（current 不空），避免覆盖尚未 setup 的列组
                        if current and current != target:
                            cell = f"{_col_letter(col_idx)}6"
                            updates.append((cell, target))

                    if updates:
                        try:
                            self.sheets._rate_limit()
                            ws.batch_update([
                                {"range": cell, "values": [[val]]}
                                for cell, val in updates
                            ])
                            for cell, val in updates:
                                logger.info("广告主名同步 [%s] %s → %s", account["name"], cell, val)
                        except Exception as e:
                            logger.warning("广告主名批量更新失败 [%s]: %s", account["name"], e)
            except Exception as e:
                logger.error("广告主名一致性巡检失败: %s", e)
            await asyncio.sleep(600)  # 10 分钟

    async def _daily_report_loop(self):
        """每天北京时间 00:00 发日报"""
        while self._running:
            now = datetime.now(TZ_BJ)
            # 计算到下一个 00:00 的秒数
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_seconds = (tomorrow - now).total_seconds()
            logger.info("下次日报在 %s (北京时间)，等待 %d秒", tomorrow.strftime('%Y-%m-%d %H:%M:%S'), int(wait_seconds))
            await asyncio.sleep(wait_seconds)

            try:
                if self.bot:
                    await self.bot.send_daily_report()
            except Exception as e:
                logger.error("日报发送失败: %s", e)
