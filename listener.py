"""Telethon 监听器 — 多账号私聊消息监听"""
import asyncio
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events
from telethon.tl.types import PeerUser

import config
import database as db

TZ_BJ = timezone(timedelta(hours=8))


class Listener:
    def __init__(self, on_new_message=None, on_keyword=None):
        self.clients = {}  # phone -> TelegramClient
        self.on_new_message = on_new_message  # callback(account, peer, message_row)
        self.on_keyword = on_keyword  # callback(account, peer, keyword, text)

    async def add_account(self, phone):
        session_path = str(config.SESSION_DIR / phone.replace("+", ""))
        client = TelegramClient(
            session_path, config.API_ID, config.API_HASH,
            device_model=config.DEVICE_NAME,
            system_version="1.0",
            app_version="1.0",
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(f"session 未认证，请先在 web 后台完成验证码登录")
        me = await client.get_me()

        # 更新数据库
        account = db.upsert_account(
            phone=phone,
            name=(me.first_name or "") + " " + (me.last_name or ""),
            username=me.username or "",
            tg_id=me.id,
        )
        account_id = account["id"]
        print(f"  ✅ {phone} 登录成功: {me.first_name} (@{me.username}) id={me.id}")

        # 注册事件处理
        @client.on(events.NewMessage(incoming=True))
        async def on_incoming(event, _aid=account_id, _phone=phone):
            if not event.is_private:
                return
            await self._handle_message(event, _aid, _phone, direction="B")

        @client.on(events.NewMessage(outgoing=True))
        async def on_outgoing(event, _aid=account_id, _phone=phone):
            if not event.is_private:
                return
            await self._handle_message(event, _aid, _phone, direction="A")

        self.clients[phone] = client
        return account

    async def _handle_message(self, event, account_id, phone, direction):
        try:
            if direction == "B":
                sender = await event.get_sender()
                peer_tg_id = sender.id
                peer_name = ((sender.first_name or "") + " " + (sender.last_name or "")).strip()
                peer_username = sender.username or ""
            else:
                chat = await event.get_chat()
                peer_tg_id = chat.id
                peer_name = ((getattr(chat, "first_name", "") or "") + " " + (getattr(chat, "last_name", "") or "")).strip()
                peer_username = getattr(chat, "username", "") or ""

            # 排除 Telegram 系统消息 (id=777000)
            if peer_tg_id == 777000:
                return

            # 排除自己（收藏夹/Saved Messages）
            account = db.get_conn().execute("SELECT tg_id FROM accounts WHERE id=?", (account_id,)).fetchone()
            if account and peer_tg_id == account["tg_id"]:
                return

            # 排除 Bot 帐号
            if direction == "B":
                if getattr(sender, 'bot', False):
                    return
            else:
                if getattr(chat, 'bot', False):
                    return

            # 获取或创建 peer
            peer = db.get_peer(peer_tg_id, account_id)
            is_new_peer = False
            if not peer:
                peer = db.upsert_peer(peer_tg_id, account_id, peer_name, peer_username)
                is_new_peer = True
            # 分配列组（未分配时 col_group = -1）
            if peer["col_group"] < 0:
                col_group = db.get_next_col_group(account_id)
                db.assign_peer_col_group(peer["id"], col_group)
                peer = db.get_peer(peer_tg_id, account_id)
                is_new_peer = True
            else:
                # 更新名字
                if peer_name and peer_name != peer["name"]:
                    db.upsert_peer(peer_tg_id, account_id, peer_name, peer_username)

            # 消息内容
            text = event.message.text or ""
            media_type = ""
            if not text:
                if event.message.photo:
                    media_type = "photo"
                    text = "[图片]"
                elif event.message.voice:
                    media_type = "voice"
                    text = "[语音]"
                elif event.message.document:
                    media_type = "file"
                    text = "[文件]"
                elif event.message.video:
                    media_type = "video"
                    text = "[视频]"
                elif event.message.sticker:
                    media_type = "sticker"
                    text = "[贴纸]"
                else:
                    text = "[其他消息]"

            timestamp = event.message.date.astimezone(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S")

            # 存入数据库
            inserted = db.insert_message(
                msg_id=event.message.id,
                account_id=account_id,
                peer_id=peer["id"],
                direction=direction,
                text=text,
                media_type=media_type,
                timestamp=timestamp,
            )

            if inserted:
                arrow = "📥" if direction == "B" else "📤"
                print(f"  {arrow} [{phone}] {direction}: {peer_name} -> {text[:40]}")

                # 回调: 新消息
                if self.on_new_message:
                    account = db.get_account_by_tg_id(
                        db.get_conn().execute("SELECT tg_id FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
                    )
                    await self.on_new_message(account_id, peer, direction, text, timestamp)

                # 关键词检测
                for kw in config.KEYWORDS:
                    if kw in text:
                        print(f"  🔔 关键词命中: [{kw}] in {text[:40]}")
                        if self.on_keyword:
                            await self.on_keyword(account_id, peer, kw, text)
                        break

                # 新对话：补拉最近 20 条历史消息
                if is_new_peer:
                    asyncio.create_task(
                        self._backfill_peer(phone, account_id, peer_tg_id, peer["id"])
                    )

        except Exception as e:
            print(f"  ❌ 处理消息失败 [{phone}]: {e}")

    async def _backfill_peer(self, phone, account_id, peer_tg_id, peer_id):
        """新对话出现时，补拉最近 20 条历史消息"""
        try:
            client = self.clients.get(phone)
            if not client:
                return

            entity = await client.get_entity(peer_tg_id)
            peer = db.get_conn().execute("SELECT * FROM peers WHERE id=?", (peer_id,)).fetchone()
            if not peer:
                return

            count = 0
            async for msg in client.iter_messages(entity, limit=20):
                direction = "A" if msg.out else "B"
                text = msg.text or ""
                media_type = ""
                if not text:
                    if msg.photo:
                        media_type, text = "photo", "[图片]"
                    elif msg.voice:
                        media_type, text = "voice", "[语音]"
                    elif msg.document:
                        media_type, text = "file", "[文件]"
                    elif msg.video:
                        media_type, text = "video", "[视频]"
                    elif msg.sticker:
                        media_type, text = "sticker", "[贴纸]"
                    else:
                        text = "[其他消息]"

                timestamp = msg.date.astimezone(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S")
                inserted = db.insert_message(
                    msg_id=msg.id, account_id=account_id, peer_id=peer_id,
                    direction=direction, text=text, media_type=media_type, timestamp=timestamp,
                )
                if inserted:
                    count += 1

            if count:
                print(f"  📜 [{phone}] 新对话补拉 {count} 条历史消息 (peer={peer['name']})")
        except Exception as e:
            print(f"  ⚠️ 补拉历史失败 [{phone}]: {e}")

    async def pull_history(self, phone, days=2):
        """拉取最近 N 天的历史私聊消息"""
        client = self.clients.get(phone)
        if not client:
            return

        account = db.get_account_by_phone(phone)
        if not account:
            return

        account_id = account["id"]
        cutoff = datetime.now(TZ_BJ) - timedelta(days=days)
        print(f"  📜 [{phone}] 拉取最近 {days} 天的历史消息...")

        count = 0
        async for dialog in client.iter_dialogs():
            if not dialog.is_user:
                continue
            peer_entity = dialog.entity
            peer_tg_id = peer_entity.id
            # 排除 Telegram 系统消息
            if peer_tg_id == 777000:
                continue
            # 排除自己
            if peer_tg_id == account["tg_id"]:
                continue
            # 排除 Bot 帐号
            if getattr(peer_entity, 'bot', False):
                continue
            peer_name = ((peer_entity.first_name or "") + " " + (peer_entity.last_name or "")).strip()
            peer_username = peer_entity.username or ""

            # 创建 peer（不预分列组，等第一条消息写入时才 lazy 分配）
            peer = db.upsert_peer(peer_tg_id, account_id, peer_name, peer_username)

            # 拉取消息：从最新迭代到最旧，遇到早于 cutoff 就 break
            # 注意：不能用 offset_date + reverse=True —— Telethon 语义是
            # "拉取 offset_date 之前的消息，由旧到新排序"，会漏掉 cutoff 之后的新消息
            async for msg in client.iter_messages(peer_entity):
                msg_dt = msg.date.replace(tzinfo=timezone.utc).astimezone(TZ_BJ)
                if msg_dt < cutoff:
                    break

                direction = "A" if msg.out else "B"
                text = msg.text or ""
                media_type = ""
                if not text:
                    if msg.photo:
                        media_type, text = "photo", "[图片]"
                    elif msg.voice:
                        media_type, text = "voice", "[语音]"
                    elif msg.document:
                        media_type, text = "file", "[文件]"
                    elif msg.video:
                        media_type, text = "video", "[视频]"
                    elif msg.sticker:
                        media_type, text = "sticker", "[贴纸]"
                    else:
                        text = "[其他消息]"

                timestamp = msg_dt.strftime("%Y-%m-%d %H:%M:%S")
                inserted = db.insert_message(
                    msg_id=msg.id, account_id=account_id, peer_id=peer["id"],
                    direction=direction, text=text, media_type=media_type, timestamp=timestamp,
                )
                if inserted:
                    count += 1
                    # lazy 分配：第一条消息写入时才分列组，避免空对话占位
                    if peer["col_group"] < 0:
                        col_group = db.get_next_col_group(account_id)
                        db.assign_peer_col_group(peer["id"], col_group)
                        peer = db.get_peer(peer_tg_id, account_id)

                    # 补推关键词预警：只对启动前 NO_REPLY_MINUTES 分钟内的 B 方消息检测
                    # 避免远古消息炸群；has_alert_today 会去重，同一对话同一天同一关键词只推一次
                    if direction == "B" and self.on_keyword:
                        grace_cutoff = datetime.now(TZ_BJ) - timedelta(minutes=config.NO_REPLY_MINUTES)
                        if msg_dt >= grace_cutoff:
                            for kw in config.KEYWORDS:
                                if kw in text:
                                    print(f"  🔔 [补推] 关键词命中: [{kw}] in {text[:40]}")
                                    await self.on_keyword(account_id, peer, kw, text)
                                    break

        print(f"  📜 [{phone}] 历史拉取完成，共 {count} 条新消息")
        return count

    async def check_deleted(self, phone, days=7):
        """巡检：检查最近 N 天消息是否被删除"""
        client = self.clients.get(phone)
        if not client:
            return []

        account = db.get_account_by_phone(phone)
        if not account:
            return []

        deleted_list = []
        recent = db.get_recent_messages(account["id"], days=days)

        # 按 peer 分组
        peer_msgs = {}
        for m in recent:
            pid = m["peer_id"]
            if pid not in peer_msgs:
                peer_msgs[pid] = []
            peer_msgs[pid].append(m)

        for peer_id, msgs in peer_msgs.items():
            peer = db.get_conn().execute("SELECT * FROM peers WHERE id=?", (peer_id,)).fetchone()
            if not peer:
                continue
            try:
                entity = await client.get_entity(peer["tg_id"])
                msg_ids = [m["msg_id"] for m in msgs]
                # 批量检查消息是否存在
                existing = await client.get_messages(entity, ids=msg_ids)
                for i, result in enumerate(existing):
                    if result is None:
                        # 消息被删除了
                        m = msgs[i]
                        if not m["deleted"]:
                            db.mark_deleted(m["msg_id"], account["id"])
                            deleted_list.append(m)
                            print(f"  🗑️ [{phone}] 发现删除消息: {m['text'][:30]}")
            except Exception as e:
                print(f"  ⚠️ 巡检 peer {peer_id} 失败: {e}")

        return deleted_list

    async def run_all(self):
        """保持所有 client 运行"""
        if not self.clients:
            print("⚠️ 没有任何账号在运行")
            return
        print(f"\n👂 共 {len(self.clients)} 个账号在监听... (Ctrl+C 退出)\n")
        await asyncio.gather(
            *[c.run_until_disconnected() for c in self.clients.values()]
        )
