"""TG Bot — 预警推送 + 审核按钮"""
import json
import logging
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

import config
import database as db
import templates

logger = logging.getLogger(__name__)


class AlertBot:
    def __init__(self, sheets_writer=None):
        self.sheets = sheets_writer
        if not config.BOT_TOKEN:
            logger.warning("BOT_TOKEN 未设置，Bot 功能暂时关闭")
            self.bot = None
            self.dp = None
            return

        self.bot = Bot(token=config.BOT_TOKEN)
        self.dp = Dispatcher()
        self._register_handlers()

    def _register_handlers(self):
        @self.dp.message(F.text == "/start")
        async def on_start(message):
            await message.reply(
                "👋 这是 TG 监控的预警 Bot。\n"
                "如果你要绑定账号做密码找回,请在网页设置页获取绑定码,\n"
                "然后发 /bind BIND-XXXXXX"
            )

        @self.dp.message(F.text.startswith("/bind"))
        async def on_bind(message):
            if message.chat.type != "private":
                await message.reply("请私聊我发送此指令。")
                return
            parts = message.text.strip().split(maxsplit=1)
            if len(parts) != 2:
                await message.reply("用法: /bind BIND-XXXXXX")
                return
            code = parts[1].strip().upper()
            import auth_reset
            tg_user_id = message.from_user.id
            tg_username = (message.from_user.username
                           or message.from_user.full_name
                           or str(tg_user_id))
            bound_user = auth_reset.try_complete_bind(code, tg_user_id, tg_username)
            if bound_user:
                await message.reply(
                    f"✅ 已绑定到帐号 {bound_user}\n"
                    "以后忘记密码可在登入页点「忘记密码」通过我私信收到验证码。"
                )
            else:
                await message.reply("❌ 绑定码无效或已过期。请回网页刷新获取新绑定码。")

        @self.dp.message(Command(commands=["chatid", "id"]))
        async def on_chatid(message):
            """v2.10.25:客户在群 / 私聊发 /chatid 让 bot 自动回 chat_id 并按上下文给用途提示。
            替代第三方 @RawDataBot / @userinfobot,少一个外部依赖。

            判别逻辑:
              supergroup → 可作档案群 / 告警群
              group      → 可作告警群,不能作档案群(需升 supergroup)
              channel    → 不建议,两者都用 supergroup / 普通群替代
              private    → 这是个人 user_id,可作审核白名单
            """
            chat = message.chat
            chat_id = chat.id
            chat_type = chat.type
            title = chat.title or (message.from_user.full_name if message.from_user else "") or "?"
            if chat_type == "supergroup":
                reply = (
                    f"🆔 <b>Chat ID</b>:<code>{chat_id}</code>\n"
                    f"群名:{title}\n"
                    f"类型:Supergroup ✅\n\n"
                    f"<b>用途</b>:\n"
                    f"• ✅ 可作<b>档案群</b> → 复制到后台「设置 → TG 档案群 Chat ID」\n"
                    f"• ✅ 可作<b>告警群</b> → 复制到「设置 → 预警群 Chat ID」"
                )
            elif chat_type == "group":
                reply = (
                    f"🆔 <b>Chat ID</b>:<code>{chat_id}</code>\n"
                    f"群名:{title}\n"
                    f"类型:普通群(basic group)\n\n"
                    f"<b>用途</b>:\n"
                    f"• ✅ 可作<b>告警群</b> → 复制到「设置 → 预警群 Chat ID」\n"
                    f"• ❌ <b>不能作档案群</b>:档案群必须是 Supergroup,否则 t.me/c 深链 404\n\n"
                    f"<b>想当档案群 → 先升级成 Supergroup</b>:\n"
                    f"群信息 → 管理群组 → 打开「新成员可见历史消息」开关 → 确认 → 立刻升级\n\n"
                    f"升级后 chat.id <b>会变</b>成 <code>-100xxxxxxxxxx</code>,再发 /chatid 拿新 ID。"
                )
            elif chat_type == "channel":
                reply = (
                    f"🆔 <b>Chat ID</b>:<code>{chat_id}</code>\n"
                    f"类型:Channel ⚠\n\n"
                    f"Channel 不太适合做档案群或告警群(bot 权限 + 对话模型都不同)。\n"
                    f"建议:新建一个 <b>Supergroup</b> 做档案群,告警群用普通群或 supergroup 都可。"
                )
            else:
                reply = (
                    f"🆔 你的个人 <b>Chat ID</b>(= user ID):<code>{chat_id}</code>\n"
                    f"(私聊时 chat.id = 你的 user.id)\n\n"
                    f"<b>用途</b>:\n"
                    f"• ✅ 可填到「设置 → 审核按钮白名单」(限制谁能点预警群的通过/拒绝)\n"
                    f"• ❌ 不能作群 ID(档案群 / 告警群请在<b>群里</b>发 /chatid)"
                )
            try:
                await message.reply(reply, parse_mode="HTML")
            except Exception as e:
                logger.warning("/chatid 回复失败 chat_id=%s: %s", chat_id, e)

        @self.dp.callback_query(F.data.startswith("approve:") | F.data.startswith("reject:"))
        async def on_audit(callback: CallbackQuery):
            data = callback.data
            action, alert_id_str = data.split(":", 1)
            alert_id = int(alert_id_str)

            # v2.10.23: 身份校验(可选,白名单为空则不校验,保持老部署兼容)
            config.reload_if_env_changed()
            if config.CALLBACK_AUTH_USER_IDS:
                uid = callback.from_user.id if callback.from_user else 0
                if uid not in config.CALLBACK_AUTH_USER_IDS:
                    await callback.answer("⛔ 你没有权限处理这个预警", show_alert=True)
                    logger.warning(
                        "[callback_deny] 未授权用户尝试处理预警: tg_id=%s name=%s alert_id=%s",
                        uid, callback.from_user.full_name if callback.from_user else "?", alert_id,
                    )
                    return

            alert = db.get_alert(alert_id)
            if not alert:
                await callback.answer("预警不存在")
                return

            # v2.10.23: 原子抢占状态转移 — 两人同时点按钮只有一人能抢到 pending→approved
            new_status = "approved" if action == "approve" else "rejected"
            claimed = db.claim_alert_for_review(alert_id, new_status)
            if not claimed:
                await callback.answer("已处理过了")
                return

            try:
                if action == "approve":
                    # 写入对应的预警分表
                    self._write_alert_to_sheet(alert)
                    await callback.message.edit_text(
                        callback.message.text + "\n\n✅ 已通过 — " + (callback.from_user.full_name or ""),
                    )
                    await callback.answer("已通过")
                else:
                    await callback.message.edit_text(
                        callback.message.text + "\n\n❌ 已拒绝 — " + (callback.from_user.full_name or ""),
                    )
                    await callback.answer("已拒绝")
            except Exception as e:
                logger.error("审核 callback 异常 alert_id=%s: %s", alert_id, e)
                try:
                    await callback.answer("处理时出错了,请再试一次或看日志")
                except Exception:
                    pass

    def _make_keyboard(self, alert_id):
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="通过", callback_data=f"approve:{alert_id}"),
                InlineKeyboardButton(text="拒绝", callback_data=f"reject:{alert_id}"),
            ]
        ])

    def _write_alert_to_sheet(self, alert):
        """审核通过后写入预警分表。

        v2.10.24.3(ADR-0010 + Codex round2 Major A 修复):claim-first 语义 ——
        - 先 claim_alert_for_sheet_write(sheet_written=1)抢到写权
        - 写成功 → 保留 sheet_written=1 返回(DB 已经持久,不会重写)
        - 写失败 3 次 → rollback_alert_sheet_claim(sheet_written=0 + last_write_error)
          交给 _alert_writeback_loop 接力重试(无限次,保零丢失)
        - 没 claim 到(sheet_written 已是 1)→ 别的路径已写,直接跳过
        """
        if not self.sheets:
            return
        if not db.claim_alert_for_sheet_write(alert["id"]):
            logger.debug("alert_id=%s 已被其他路径 claim,跳过 _write_alert_to_sheet", alert["id"])
            return
        last_err = None
        append_ok = False
        for attempt in range(3):
            try:
                account = db.get_conn().execute(
                    "SELECT * FROM accounts WHERE id=?", (alert["account_id"],)
                ).fetchone()
                peer = db.get_conn().execute(
                    "SELECT * FROM peers WHERE id=?", (alert["peer_id"],)
                ).fetchone() if alert["peer_id"] else None

                company = account["company"] if account else ""
                operator = account["operator"] if account else ""
                account_name = account["name"] if account else ""
                peer_name = peer["name"] if peer else ""
                now = db.now_bj()

                with self.sheets._write_lock:
                    if alert["type"] == "no_reply":
                        ws = self.sheets.spreadsheet.worksheet(f"信息未回复预警{config.COMPANY_DISPLAY}")
                        self.sheets._rate_limit()
                        ws.append_row([company, operator, account_name, peer_name, alert["message_text"], now])
                    elif alert["type"] == "deleted":
                        ws = self.sheets.spreadsheet.worksheet(f"信息删除预警{config.COMPANY_DISPLAY}")
                        self.sheets._rate_limit()
                        # 第 5 列写入"删除前消息内容"方便主管看板回溯
                        ws.append_row([company, operator, account_name, peer_name,
                                       alert["message_text"] or "", now])
                append_ok = True
                break
            except Exception as e:
                last_err = e
                logger.error("写入预警分表失败 alert_id=%s (尝试 %d/3): %s", alert["id"], attempt + 1, e)
                if attempt < 2:
                    import time; time.sleep(2)
        if append_ok:
            # claim-first + final done:清 claimed_at,此后 writeback 不再触碰这条
            db.mark_alert_sheet_done(alert["id"])
        else:
            # claim 已成功但写分页 3 次失败 → 回滚让 writeback loop 接力
            db.rollback_alert_sheet_claim(alert["id"], last_err)

    async def send_keyword_alert(self, account_id, peer, keyword, text):
        """发送关键词预警（每个对话框每天只推一次）

        v2.10.24.3(ADR-0010):顺序反转 — 先 insert_alert 拿 id(sheet_written=0),
        再写分页,成功 mark 为 1;失败静默保留 0,交 _alert_writeback_loop 接力
        无限重试(保证 429 > 6 秒或 worksheet 短暂不可达时零丢失)。
        message_text 只存 text,keyword 单独栏位(之前 `[kw] text` 混存,writeback 时拆不干净)。
        """
        if not self.bot or not config.ALERT_GROUP_ID:
            return
        if db.has_alert_today("keyword", peer["id"]):
            return

        account = db.get_conn().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not account:
            return

        # v2.10.24.3: 先插 DB 拿 alert_id,写入分页失败由 writeback loop 接力
        alert_id = db.insert_alert(
            "keyword", account_id, peer["id"], message_text=text, keyword=keyword,
        )

        msg = templates.keyword_alert(
            company=account["company"],
            operator=account["operator"],
            account_name=account["name"],
            peer_name=peer["name"],
            keyword=keyword,
            message_text=text,
        )
        try:
            # v2.6.2: 预警推送总开关 — 关闭时跳过 TG 发送,但分表 + DB 照常记录
            # 每次先 reload 感知 web 容器改过的 .env(两容器独立进程,靠文件 mtime 同步)
            # v2.6.6: 拆出独立子开关 ALERT_KEYWORD_ENABLED
            config.reload_if_env_changed()
            if config.ALERT_KEYWORD_ENABLED:
                await self.bot.send_message(config.ALERT_GROUP_ID, msg)
            else:
                logger.info("[ALERT_KEYWORD_DISABLED] 跳过关键词推送 peer=%s keyword=%s (Sheet 仍写入)", peer["id"], keyword)
        except Exception as e:
            logger.error("发送关键词预警(TG 推送)失败 alert_id=%s: %s", alert_id, e)

        # 写入关键词监听分表: 所属公司,商务人员,外事号,广告主,关键词,消息内容,记录时间
        # v2.10.24.3 Codex round2 Major A 修复:claim-first,append 前 mark,append 失败 rollback
        if self.sheets:
            if not db.claim_alert_for_sheet_write(alert_id):
                logger.debug("alert_id=%s 已被其他路径 claim,跳过关键词分页写入", alert_id)
                return
            last_err = None
            append_ok = False
            for attempt in range(3):
                try:
                    with self.sheets._write_lock:
                        ws = self.sheets.spreadsheet.worksheet(f"关键词监听{config.COMPANY_DISPLAY}")
                        self.sheets._rate_limit()
                        ws.append_row([
                            account["company"], account["operator"], account["name"],
                            peer["name"], keyword, text, db.now_bj()
                        ])
                    append_ok = True
                    break
                except Exception as e2:
                    last_err = e2
                    logger.error("写入关键词分表失败 alert_id=%s (尝试 %d/3): %s", alert_id, attempt + 1, e2)
                    if attempt < 2:
                        import time; time.sleep(2)
            if append_ok:
                # claim-first + final done:清 claimed_at,此后 writeback 不再触碰这条
                db.mark_alert_sheet_done(alert_id)
            else:
                # claim 已成功但写分页 3 次失败 → 回滚让 writeback loop 接力
                db.rollback_alert_sheet_claim(alert_id, last_err)

    async def send_no_reply_alert(self, account_id, peer, message_text, msg_id):
        """发送未回复预警（每个广告主每天只推一次）

        v2.10.23:
        - 静默(开关关)→ 插入 alert 并标 status='silenced',has_alert_today 当「已处理」
        - 推送失败 → 保留 bot_message_id=null,has_alert_today 不认作已推,下次扫描重试
          (修之前「第一次推送失败后一整天不再重试」的 bug)
        """
        if not self.bot or not config.ALERT_GROUP_ID:
            return
        if db.has_alert_today("no_reply", peer["id"]):
            return

        account = db.get_conn().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not account:
            return

        # 先读开关,再决定要不要真插入 alert — 避免「静默模式又插入又不推」占掉今天的去重名额
        config.reload_if_env_changed()

        if not config.ALERT_NO_REPLY_ENABLED:
            # 静默:插入 alert 但标 silenced,当天不再重复扫
            alert_id = db.insert_alert(
                alert_type="no_reply",
                account_id=account_id,
                peer_id=peer["id"],
                msg_id=msg_id,
                message_text=message_text,
            )
            db.update_alert_status(alert_id, "silenced")
            logger.info("[ALERT_NO_REPLY_DISABLED] 跳过未回复推送 peer=%s (静默记录)", peer["id"])
            return

        # 正常路径:创建 pending alert(bot_message_id=null),推送,成功后 update_alert_bot_msg
        alert_id = db.insert_alert(
            alert_type="no_reply",
            account_id=account_id,
            peer_id=peer["id"],
            msg_id=msg_id,
            message_text=message_text,
        )

        msg = templates.no_reply_alert(
            company=account["company"],
            operator=account["operator"],
            account_name=account["name"],
            peer_name=peer["name"],
            message_text=message_text,
        )
        try:
            sent = await self.bot.send_message(
                config.ALERT_GROUP_ID, msg,
                reply_markup=self._make_keyboard(alert_id),
            )
            db.update_alert_bot_msg(alert_id, sent.message_id)
        except Exception as e:
            # v2.10.23: 推送失败 → bot_message_id 保持 null,has_alert_today 不认作已推
            # 下次 _no_reply_loop 扫到会重试;这里额外写个日志带 alert_id 方便排查
            logger.error("发送未回复预警失败 alert_id=%s: %s (下次扫描会重试)", alert_id, e)

    async def send_delete_alert(self, account_id, peer, message_text, msg_id):
        """发送删除预警（每个广告主每天只推一次)

        v2.10.23:同 send_no_reply_alert 的逻辑 — 静默走 silenced,失败留 pending 下次重试。
        """
        if not self.bot or not config.ALERT_GROUP_ID:
            return
        if db.has_alert_today("deleted", peer["id"]):
            return

        account = db.get_conn().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not account:
            return

        config.reload_if_env_changed()

        if not config.ALERT_DELETE_ENABLED:
            alert_id = db.insert_alert(
                alert_type="deleted",
                account_id=account_id,
                peer_id=peer["id"],
                msg_id=msg_id,
                message_text=message_text,
            )
            db.update_alert_status(alert_id, "silenced")
            logger.info("[ALERT_DELETE_DISABLED] 跳过删除推送 peer=%s (静默记录)", peer["id"])
            return

        alert_id = db.insert_alert(
            alert_type="deleted",
            account_id=account_id,
            peer_id=peer["id"],
            msg_id=msg_id,
            message_text=message_text,
        )

        msg = templates.delete_alert(
            company=account["company"],
            operator=account["operator"],
            account_name=account["name"],
            peer_name=peer["name"],
            message_text=message_text,
        )
        try:
            sent = await self.bot.send_message(
                config.ALERT_GROUP_ID, msg,
                reply_markup=self._make_keyboard(alert_id),
            )
            db.update_alert_bot_msg(alert_id, sent.message_id)
        except Exception as e:
            logger.error("发送删除预警失败 alert_id=%s: %s (下次会重试)", alert_id, e)

    async def send_daily_report(self):
        """发送每日总结"""
        if not self.bot or not config.ALERT_GROUP_ID:
            return
        # v2.6.2: 日报独立开关
        config.reload_if_env_changed()
        if not config.DAILY_REPORT_ENABLED:
            logger.info("[DAILY_REPORT_DISABLED] 跳过日报推送")
            return

        from database import TZ_BJ
        from datetime import datetime

        now = datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S")

        # 统计昨天的数据（00:00 发送时统计前一天）
        from datetime import timedelta
        yesterday = (datetime.now(TZ_BJ) - timedelta(days=1)).strftime("%Y-%m-%d")
        conn = db.get_conn()

        chat_count = conn.execute(
            "SELECT COUNT(DISTINCT peer_id) FROM messages WHERE timestamp LIKE ?",
            (f"{yesterday}%",)
        ).fetchone()[0]

        def _status_breakdown(type_name):
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM alerts WHERE type=? AND created_at LIKE ? GROUP BY status",
                (type_name, f"{yesterday}%")
            ).fetchall()
            bucket = {"approved": 0, "pending": 0, "rejected": 0}
            for status, cnt in rows:
                if status in bucket:
                    bucket[status] = cnt
            return bucket

        no_reply_detail = _status_breakdown("no_reply")
        no_reply = sum(no_reply_detail.values())

        delete_detail = _status_breakdown("deleted")
        deleted = sum(delete_detail.values())

        keyword_count = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE type='keyword' AND created_at LIKE ?",
            (f"{yesterday}%",)
        ).fetchone()[0]

        msg = templates.daily_report(
            yesterday, now, chat_count, no_reply, deleted, keyword_count,
            no_reply_detail=no_reply_detail, delete_detail=delete_detail,
        )
        try:
            await self.bot.send_message(config.ALERT_GROUP_ID, msg)
            logger.info("日报已推送")
        except Exception as e:
            logger.error("发送日报失败: %s", e)

    async def send_session_alert(self, kind: str, phone: str, account_id: int = 0, account_name: str = ""):
        """v2.10.4: 推送 session 吊销/恢复预警。kind: 'revoked' | 'restored'
        - 走主开关 ALERTS_ENABLED(任何子开关都独立;这是系统级告警)
        - DB 写 alerts 表,实时告警流会显示
        - kind='revoked' 直接推;kind='restored' 也推一条,让客户知道已恢复"""
        if not self.bot or not config.ALERT_GROUP_ID:
            logger.warning("[session_%s] bot 未配或未配预警群,不推送 phone=%s", kind, phone)
            return
        try:
            config.reload_if_env_changed()
            if kind == "revoked":
                msg = templates.session_revoked_alert(phone, account_name)
                alert_type = "session_revoked"
            elif kind == "restored":
                msg = templates.session_restored_alert(phone, account_name)
                alert_type = "session_restored"
            else:
                return
            # DB 记录(不受 ALERTS_ENABLED 影响,审计留痕)
            try:
                if account_id:
                    db.insert_alert(alert_type, account_id, peer_id=None, message_text=f"[{phone}] {account_name}")
            except Exception as e:
                logger.warning("[session_%s] insert_alert 失败: %s", kind, e)
            # v2.10.5: 运维告警,永远推 — 不受 ALERTS_ENABLED 影响
            # (ALERTS_ENABLED 只管业务告警:关键词/未回复/删除。session 吊销是系统性故障,必须让客户知道)
            await self.bot.send_message(config.ALERT_GROUP_ID, msg)
            logger.info("[session_%s] 已推送 phone=%s", kind, phone)
        except Exception as e:
            logger.error("[session_%s] 推送失败 phone=%s: %s", kind, phone, e)

    async def send_update_notice(self, state: dict):
        """v2.9.0: 发现 GitHub 有新版 → 推送到预警群(同版本只推一次,update_checker 控制去重)"""
        if not self.bot or not config.ALERT_GROUP_ID:
            return

        new_commits = state.get("new_commits", [])
        latest_title = state.get("latest_user_title") or "📦 有新版本可升级"
        latest_body = state.get("latest_user_body") or ""

        company = getattr(config, "COMPANY_NAME", "")
        company_display = getattr(config, "COMPANY_DISPLAY", company)

        lines = [f"<b>{latest_title}</b>", "", f"部门:{company_display}"]
        if latest_body:
            lines.append("")
            lines.append(latest_body)

        # 如果中间跨了多个版本,把每个版本的白话也列出来
        if len(new_commits) > 1:
            lines.append("")
            lines.append(f"<b>本次更新涵盖 {len(new_commits)} 个版本:</b>")
            for c in new_commits[-6:]:
                t = c.get("user_title", "") or "更新"
                b = c.get("user_body", "")
                lines.append(f"  {t}")
                if b:
                    lines.append(f"    {b}")
            if len(new_commits) > 6:
                lines.append(f"  ...(还有 {len(new_commits)-6} 个,登入管理页看完整列表)")

        lines.append("")
        lines.append("<b>如何升级</b>(复制这行到服务器跑):")
        lines.append(f"<code>cd /root/tg-monitor-{company} && bash update.sh</code>")
        lines.append("")
        lines.append("✓ 有回滚保护,万一有问题会自动退回旧版")
        lines.append("✓ 升级期间网页会短暂刷新,大约 30 秒恢复")

        msg = "\n".join(lines)
        try:
            await self.bot.send_message(
                config.ALERT_GROUP_ID, msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            _short = state.get("latest_short", "")
            logger.info(f"版本更新通知已推送: {_short}")
        except Exception as e:
            logger.error(f"版本更新通知推送失败: {e}")

    async def send_sheets_backlog_warning(self, backlog_count):
        """v2.10.23:Sheets 写入积压告警 — 超过阈值时推预警群。
        1 小时内最多推 1 次(在 tasks.py 侧做 cooldown)。"""
        if not self.bot or not config.ALERT_GROUP_ID:
            return
        company = getattr(config, "COMPANY_NAME", "")
        company_display = getattr(config, "COMPANY_DISPLAY", company)
        msg = (
            f"⚠ <b>Sheets 写入积压告警</b>\n\n"
            f"部门:{company_display}\n"
            f"当前有 <b>{backlog_count}</b> 条消息超过 10 分钟还没写进 Google Sheets。\n\n"
            f"可能原因:Google API 配额用光、OAuth token 失效、Sheet 权限异常。\n"
            f"系统会自动重试(指数退避),如果持续不下降:\n"
            f"  • 检查 Google API 控制台的 Sheets 配额\n"
            f"  • 登入后台 /setup 页重新授权 OAuth\n"
            f"  • 查看容器日志 <code>docker compose -p tg-{company} logs tg-monitor</code>"
        )
        try:
            await self.bot.send_message(
                config.ALERT_GROUP_ID, msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            logger.warning("[sheets_backlog] 告警已推送: %d 条积压", backlog_count)
        except Exception as e:
            logger.error("Sheets 积压告警推送失败: %s", e)

    async def start(self):
        """启动 Bot 轮询"""
        if not self.dp:
            return
        logger.info("Bot 启动...")
        await self.dp.start_polling(self.bot)
