"""定时任务 — 巡检、未回复检查、日报"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

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
            self._media_cleanup_loop(),
            self._update_check_loop(),
            self._session_health_loop(),      # v2.10.4: TG session 吊销检测
            self._sheets_backlog_loop(),      # v2.10.23: Sheets 写入积压告警
            self._alert_backfill_loop(),      # v2.10.24.2: 预警分页历史空白回填巡检
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
        """确保 Sheets 分页名 = account.name。用户手改分页名会被自动改回 TG 名字。

        v2.10.24.1(ADR-0008 P0 修复):原实现对每个账号调一次 spreadsheet.worksheet(title),
        gspread 6.x 每次都会 fetch_sheet_metadata(真 API 读),150 账号 = 150 reads/轮。
        改成一次 worksheets() 拿全部标题,内存里 set lookup(150 读 → 1 读)。
        """
        try:
            self.sheets._rate_limit()
            all_worksheets = {ws.title: ws for ws in self.sheets.spreadsheet.worksheets()}
        except Exception as e:
            logger.warning("分页列表读取失败,跳过本轮巡检: %s", e)
            return

        for account in db.get_all_accounts():
            target = account["name"]
            if not target:
                continue
            # 已存在正确分页 → OK
            if target in all_worksheets:
                # 顺手把 sheet_tab 对齐
                if account["sheet_tab"] != target:
                    conn = db.get_conn()
                    conn.execute("UPDATE accounts SET sheet_tab=? WHERE id=?", (target, account["id"]))
                    conn.commit()
                continue

            # 找旧 sheet_tab 对应的分页,改名回 target
            old = account["sheet_tab"]
            if not old or old == target:
                continue
            ws = all_worksheets.get(old)
            if ws is None:
                logger.warning("分页名修正：旧分页「%s」也不存在，跳过", old)
                continue
            try:
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
        """每 60 秒巡检：补漏 + 删除检测 + 同步表头(节流)+ 昵称同步

        v2.10.24.1: sync_headers 从跟随 PATROL_INTERVAL 改成独立节流
        (SYNC_HEADERS_INTERVAL_SEC,默认 600s),保护 Google Sheets 读配额。
        """
        # 启动后等一会儿再开始巡检
        await asyncio.sleep(30)
        # v2.10.24.1(Codex P1 修复): 时间戳式节流,避免 sync_headers 异常时 counter 卡住
        # → 下一轮 60s 再试 → 429 自我循环。用 monotonic 记上次运行时间,异常也会更新。
        _last_sync_headers_at = 0.0
        while self._running:
            try:
                # v2.10.24.1: sync_headers 独立节流,默认每 10 分钟一次。
                # 原每 60 秒一次,150 账号 × 2 reads / 60s = 300 reads/min,
                # 打爆 Google Sheets 60/min/user 读配额。
                if not config.SYNC_HEADERS_DISABLED:
                    _now = time.monotonic()
                    if (_now - _last_sync_headers_at) >= config.SYNC_HEADERS_INTERVAL_SEC:
                        try:
                            self.sheets.sync_headers()
                        finally:
                            # 异常也要更新(不然下一轮立刻又重试 → 429 loop)
                            _last_sync_headers_at = _now

                # 检查账号昵称是否改变
                await self._sync_account_names()

                # 分页名一致性检查：手改的分页名会被自动改回 TG 名字
                await self._enforce_sheet_tab_consistency()

                # 预警分页名一致性:手改「信息删除预警YD」会被自动改回「信息删除预警{COMPANY_DISPLAY}」
                # 防止客户手贱改名后系统找不到分页 → 写入失败
                try:
                    self.sheets.ensure_alert_tabs()
                except Exception as e:
                    logger.warning("预警分页一致性巡检失败: %s", e)

                # v2.10.15: 账号分页自愈巡检 — 登录时 _create_sheet_tab 可能因为
                # Sheets API 429 / 瞬时网络 / OAuth token 过期而静默失败,
                # tg-monitor 启动 sweep 只跑一次。这里每轮巡检再扫一次,
                # 补建 DB 里有但 Sheet 里没有的账号分页(幂等)
                try:
                    self.sheets.ensure_account_tabs()
                except Exception as e:
                    logger.warning("账号分页自愈巡检失败: %s", e)

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
        """每 PEER_NAME_CONSISTENCY_INTERVAL_SEC 秒(默认 600 = 10 分钟)巡检广告主名是否跟 DB 一致。

        对方在 TG 改名会通过新消息更新到 DB,这个 loop 把 DB 的最新名字同步回 Sheets
        表头的第 6 行 C 列(或 F6, I6, L6...)。

        v2.10.24.1: 间隔从 PATROL_INTERVAL (60s) 拆出独立配置 (默认 600s),
        保护 Google Sheets 读配额(每账号 1 read,原 150/min 已能打爆 60/min 配额)。
        原来 docstring 写「每 10 分钟」但实际代码用 PATROL_INTERVAL(60s),文档-代码不一致(ADR-0008 顺手修)。
        """
        # 启动后等一会儿
        await asyncio.sleep(120)
        while self._running:
            if config.PEER_NAME_CONSISTENCY_DISABLED:
                await asyncio.sleep(config.PEER_NAME_CONSISTENCY_INTERVAL_SEC)
                continue
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

                    role_label = config.PEER_ROLE_LABEL
                    updates = []  # [(range, value)]
                    for peer in peers:
                        col_group_b = peer["col_group"] * 3 + 1  # B 列索引（role 标签）
                        col_group_c = peer["col_group"] * 3 + 2  # C 列索引（peer name）
                        # 同步 peer 名
                        current_name = row6[col_group_c] if col_group_c < len(row6) else ""
                        target_name = peer["name"] or f"用户{peer['tg_id']}"
                        if current_name and current_name != target_name:
                            updates.append((f"{_col_letter(col_group_c)}6", target_name))
                        # 同步 role 标签（广告主 → 客户/合作方等）
                        current_label = row6[col_group_b] if col_group_b < len(row6) else ""
                        if current_label and current_label != role_label:
                            updates.append((f"{_col_letter(col_group_b)}6", role_label))

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
            await asyncio.sleep(config.PEER_NAME_CONSISTENCY_INTERVAL_SEC)  # v2.10.24.1: 从 PATROL_INTERVAL 独立,默认 600s

    async def _media_cleanup_loop(self):
        """每天北京时间 03:00 清理 Drive 里超过 MEDIA_RETENTION_DAYS 天的旧媒体。
        MEDIA_RETENTION_DAYS=0 时 loop 还是照跑(睡到点再 check),客户改设置后下一轮立刻生效。"""
        while self._running:
            now = datetime.now(TZ_BJ)
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            wait_s = (target - now).total_seconds()
            logger.info("下次媒体清理在 %s (北京时间)", target.strftime('%Y-%m-%d %H:%M:%S'))
            await asyncio.sleep(wait_s)

            try:
                # 动态读(客户可能在 web 后台改了 MEDIA_RETENTION_DAYS,但 config 没 reload)
                # → 每次跑都从 .env 重新读一次
                import importlib
                importlib.reload(config)
                days = int(getattr(config, "MEDIA_RETENTION_DAYS", 0) or 0)
                if days <= 0:
                    logger.info("MEDIA_RETENTION_DAYS=0, 跳过自动清理")
                    continue
                import media_uploader
                deleted, failed = media_uploader.cleanup_old_media(days)
                logger.info("每日媒体清理完成: 删 %d 失败 %d", deleted, failed)
            except Exception as e:
                logger.error("每日媒体清理失败: %s", e)

    async def _update_check_loop(self):
        """v2.9.0: 每 6 小时查一次 GitHub 有没有新版,有就推 TG 通知(同版本只推一次)。
        启动 60 秒后第一次 check,之后每 6 小时一次(一天 4 次;急用户 Dashboard 有手动刷新按钮)。"""
        import update_checker
        await asyncio.sleep(60)  # 启动后稍等一下,让 listener/bot 都就位
        while self._running:
            try:
                await update_checker.check_and_notify(self.bot)
            except Exception as e:
                logger.warning(f"update check loop error: {e}")
            await asyncio.sleep(6 * 3600)  # 6 小时

    # v2.10.4: session 吊销检测 ----------------------------------------
    SESSION_STATE_FILE = "/app/data/.session_states.json"
    SESSION_CHECK_INTERVAL = 300   # 5 分钟一次
    SESSION_FIRST_DELAY = 90       # 启动后等 90s 再开始(避开启动期的 flaky)

    def _load_session_states(self):
        import json, os
        try:
            if os.path.exists(self.SESSION_STATE_FILE):
                with open(self.SESSION_STATE_FILE, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning("载入 session_states 失败: %s", e)
        return {}

    def _save_session_states(self, states):
        import json, os
        try:
            os.makedirs(os.path.dirname(self.SESSION_STATE_FILE), exist_ok=True)
            tmp = self.SESSION_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(states, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.SESSION_STATE_FILE)
        except Exception as e:
            logger.warning("保存 session_states 失败: %s", e)

    async def _check_single_session(self, phone, client):
        """返回 'healthy' | 'revoked' | 'error'。

        v2.10.23:加真 RPC 探测(get_me)—— 以前只靠 is_user_authorized() 判活,
        但 TG 冻结/封号账号的 session key 仍然「有效」,is_user_authorized 返回 True,
        只有在真正调 API 时才抛 UserDeactivatedBanError / UserDeactivatedError。
        所以必须加 get_me() 这一步才能识别冻结/封号场景。
        FloodWait 不算死,当 healthy(限流而已)。"""
        try:
            if not client.is_connected():
                try:
                    await asyncio.wait_for(client.connect(), timeout=15)
                except Exception as e:
                    logger.warning("[session_check] %s connect 失败: %s", phone, e)
                    return "error"
            authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=10)
            if not authorized:
                return "revoked"

            # v2.10.23: 真 RPC 探测 — 冻结/封号账号在这步才会抛
            try:
                await asyncio.wait_for(client.get_me(), timeout=10)
            except asyncio.TimeoutError:
                # 网络抖动,本轮当 error(不触发转场,避免 flap)
                logger.warning("[session_check] %s get_me 超时 (当 error)", phone)
                return "error"
            except Exception as e:
                cls = type(e).__name__
                msg = str(e)
                if "FloodWait" in cls:
                    # 限流 ≠ 死,账号还活着只是 TG 暂时不让查
                    logger.info("[session_check] %s get_me FloodWait (视为 healthy): %s", phone, msg)
                    return "healthy"
                # 关键判定:异常类名或消息命中以下任何关键字 = session 已失效
                dead_kw = ("Deactivated", "AuthKey", "Revoked", "Banned", "Unauthorized", "SessionExpired")
                if any(k in cls for k in dead_kw) or any(k in msg for k in dead_kw) or \
                        any(k in msg for k in ("unauthorized", "deactivated", "banned", "revoked")):
                    logger.warning("[session_check] %s 判定已失效: %s: %s", phone, cls, msg)
                    return "revoked"
                # 其他异常当 error(网络抖动、TG 服务器不稳等)
                logger.warning("[session_check] %s get_me 异常 (当 error): %s: %s", phone, cls, msg)
                return "error"
            return "healthy"
        except Exception as e:
            msg = str(e)
            if any(k in msg for k in ("AuthKey", "Unauthorized", "Deactivated", "Revoked")):
                return "revoked"
            logger.warning("[session_check] %s 检查异常: %s", phone, e)
            return "error"

    async def _session_health_loop(self):
        """每 5 分钟检查每个 client 的 is_user_authorized()。
        healthy→revoked 推吊销预警;revoked→healthy 推恢复通知。
        首次启动用现状作基线,不报警,避免启动期炸群。
        'error' 状态不触发转场(可能是临时网络问题)。"""
        await asyncio.sleep(self.SESSION_FIRST_DELAY)
        prev = self._load_session_states()
        first_round = not prev   # 没状态文件 = 第一次跑
        while self._running:
            try:
                current = {}
                for phone, client in list(self.listener.clients.items()):
                    status = await self._check_single_session(phone, client)
                    current[phone] = {
                        "status": status,
                        "last_check": db.now_bj(),
                    }
                    prev_status = (prev.get(phone) or {}).get("status")
                    if first_round:
                        continue
                    if status == "revoked" and prev_status != "revoked":
                        logger.warning("[session] %s 吊销 (prev=%s)", phone, prev_status)
                        await self._emit_session_alert("revoked", phone)
                    elif status == "healthy" and prev_status == "revoked":
                        logger.info("[session] %s 已恢复", phone)
                        await self._emit_session_alert("restored", phone)
                for phone, st in prev.items():
                    if phone not in current:
                        current[phone] = st
                self._save_session_states(current)
                prev = current
                first_round = False
            except Exception as e:
                logger.error("session_health_loop 异常: %s", e)
            await asyncio.sleep(self.SESSION_CHECK_INTERVAL)

    async def _emit_session_alert(self, kind, phone):
        """找到 account_id + name 再丢给 bot.send_session_alert"""
        try:
            row = db.get_conn().execute(
                "SELECT id, name FROM accounts WHERE phone=?", (phone,)
            ).fetchone()
            aid = row["id"] if row else 0
            name = row["name"] if row else ""
            if self.bot:
                await self.bot.send_session_alert(kind, phone, account_id=aid, account_name=name)
        except Exception as e:
            logger.warning("_emit_session_alert 失败 phone=%s kind=%s: %s", phone, kind, e)
    # -----------------------------------------------------------------

    async def _sheets_backlog_loop(self):
        """v2.10.23:每 5 分钟检查 Sheets 写入积压 — 有 messages.sheet_written=0
        且超过 10 分钟没补上去的,且积压数超过阈值 → 推预警群告警。
        防止客户长时间不知道「Sheets 配额爆了导致表格空白」这种情况。
        冷却时间:同一积压告警每小时最多推 1 次,避免刷屏。"""
        await asyncio.sleep(300)   # 启动后等服务稳定
        last_alert_ts = 0
        cooldown_sec = 3600         # 1 小时只推 1 次
        while self._running:
            try:
                count = db.count_unwritten_older_than(minutes=10)
                now = time.time()
                if count > config.SHEETS_BACKLOG_ALERT_THRESHOLD and (now - last_alert_ts) > cooldown_sec:
                    if self.bot:
                        try:
                            await self.bot.send_sheets_backlog_warning(count)
                            last_alert_ts = now
                        except Exception as e:
                            logger.error("积压告警推送失败: %s", e)
            except Exception as e:
                logger.error("sheets_backlog_loop 异常: %s", e)
            await asyncio.sleep(300)

    async def _alert_backfill_loop(self):
        """v2.10.24.2(ADR-0009):定期扫三个预警分页,把空 A/B 栏用 DB 值补上。

        背景:v2.10.24.1 之前 sync_headers 被 429 / sed 止血卡住时,新登录外事号在
        分页 B2/B3 填的值同步不到 DB,后续预警写入时 A/B 栏为空。启动时 sheets.__init__
        已经补过一次历史,这个 loop 负责补新命中的漏。

        频率默认 1 小时一次,7 次 API/轮,对配额几乎无影响。
        """
        await asyncio.sleep(600)  # 启动后等 10 分钟,让 sync_headers 先同步到 DB 再回填
        while self._running:
            try:
                if config.BACKFILL_ALERT_HISTORY and self.sheets:
                    self.sheets.backfill_alert_history()
            except Exception as e:
                logger.error("alert_backfill_loop 异常: %s", e)
            await asyncio.sleep(config.BACKFILL_ALERT_INTERVAL_SEC)

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
