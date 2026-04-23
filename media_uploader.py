"""把 Telegram 媒体下载到内存并上传到 Google Drive，返回可在 Sheets 直显/下载的 URL。

设计要点：
- 只在 config.MEDIA_FOLDER_ID 非空时上传，否则返回空字符串（调用方 fallback 到「[图片]/[文件]」占位）。
- service account 的 Drive 没有自己的存储配额，必须把客户提供的文件夹分享给 SA。
- 上传后把文件设为「任何拥有连结即可查看」，让 Google Sheets 的 =IMAGE() 公式能直接显示。
  （客户的 Sheet 已经分享给 SA，但 =IMAGE() 是浏览器侧加载图片，必须公开可读。）
- 文件上传一次失败不阻塞消息流，返回 ""，调用方继续走文字占位。
"""
import io
import logging
import mimetypes
import os
import threading
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)
TZ_BJ = timezone(timedelta(hours=8))

# 可执行/脚本类扩展名 — 打开就可能跑代码,在 Sheet 里加 ⚠️ 前缀提醒客户下载前确认
# 客户业务审查场景几乎不会收到这类文件,一旦收到大概率是钓鱼 / 诈骗附件
DANGER_EXTS = {
    ".exe", ".bat", ".cmd", ".scr", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".ps1", ".psm1", ".msi", ".jar", ".com", ".pif",
    ".hta", ".cpl", ".reg", ".lnk", ".dll", ".apk", ".app", ".dmg",
}

_lock = threading.Lock()
_drive_service = None  # lazy 初始化 googleapiclient.discovery.Resource

# v2.10.25(ADR-0014):tg_archive 模式用 aiogram Bot 转发到 TG 档案群
# main.py 启动时调 set_archive_bot(alert_bot.bot) 注入
_archive_bot = None


def set_archive_bot(bot):
    """v2.10.25:main.py 启动 AlertBot 后调用,把 aiogram Bot 实例注入供转发用。
    tg_archive 模式依赖这个引用;drive / off 模式不调也不影响。
    """
    global _archive_bot
    _archive_bot = bot


_TG_ARCHIVE_WARNED_BAD_GID = False


def is_tg_archive_enabled():
    """v2.10.25(Codex P1 round1 修复):检查 tg_archive 模式四要素齐备。

    必须同时满足:
      1. MEDIA_STORAGE_MODE == "tg_archive"
      2. aiogram Bot 实例已由 main.py 注入
      3. MEDIA_ARCHIVE_GROUP_ID 是负数 且 abs(gid) 以 "100" 开头(supergroup/channel 格式)
         — 只有 supergroup 才能拼 t.me/c 深链;正数(user / private chat)或非 -100 前缀
         (普通 small group)都不支持,否则:
           - 深链打开 404
           - 更糟:正 chat_id 如果恰好是某个 bot 能触达的用户,会把媒体发到错聊天
                  (敏感数据泄漏 — Codex P1 指出的真实风险)

    首次检测到配置错误会在日志里 warn 一次(不 spam),之后静默返回 False,
    调用方 fallback 到文字占位「[图片]」「[文件]」。
    """
    global _TG_ARCHIVE_WARNED_BAD_GID
    if getattr(config, "MEDIA_STORAGE_MODE", "drive") != "tg_archive":
        return False
    if _archive_bot is None:
        return False
    gid = int(getattr(config, "MEDIA_ARCHIVE_GROUP_ID", 0) or 0)
    if gid == 0:
        return False
    # 必须是 supergroup/channel:chat_id 负数且 abs 以 100 开头(对应 -100xxxxxxxxxx)
    if gid >= 0 or not str(abs(gid)).startswith("100"):
        if not _TG_ARCHIVE_WARNED_BAD_GID:
            logger.warning(
                "MEDIA_ARCHIVE_GROUP_ID=%s 不是合法的 supergroup ID (必须 -100 开头)"
                "— tg_archive 模式已禁用,媒体 fallback 文字占位。请确认把档案群"
                "设为 supergroup 并填写 -100xxxxxxxxxx 格式 ID",
                gid,
            )
            _TG_ARCHIVE_WARNED_BAD_GID = True
        return False
    return True


def _archive_deep_link(archive_msg_id):
    """v2.10.25:把 MEDIA_ARCHIVE_GROUP_ID + 消息 ID 转成 t.me/c 深链。

    - supergroup ID 通常是 -100xxxxxxxx,链接要去掉 -100 前缀 → t.me/c/xxxxxxxx/{msg_id}
    - 正常群(不含 -100 前缀)直接用绝对值
    """
    gid = int(getattr(config, "MEDIA_ARCHIVE_GROUP_ID", 0) or 0)
    if gid == 0:
        return ""
    s = str(abs(gid))
    if s.startswith("100"):
        s = s[3:]
    return f"https://t.me/c/{s}/{archive_msg_id}"


# v2.10.25:Bot API 强限制 — photo 10MB / document 50MB(超出直接被 TG 拒)
_BOT_PHOTO_LIMIT_BYTES = 10 * 1024 * 1024
_BOT_DOCUMENT_LIMIT_BYTES = 50 * 1024 * 1024


def _get_drive():
    """lazy 创建 Drive v3 client。

    Level 1 架构后:只走 OAuth 用户凭证(drive.file scope),用客户 15GB 免费配额。
    SA 路径已移除 — SA 没 Drive 存储配额,在非 Workspace 帐户下必然 403。
    """
    global _drive_service
    if _drive_service is not None:
        return _drive_service
    with _lock:
        if _drive_service is not None:
            return _drive_service
        try:
            from googleapiclient.discovery import build
            import oauth_helper
            creds = oauth_helper.get_credentials()
            if not creds:
                logger.warning("Drive 未初始化: 缺 OAuth 凭证,请先在 setup 精灵授权")
                return None
            _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
            logger.info("Drive 服务初始化成功 (OAuth 用户授权)")
            return _drive_service
        except Exception as e:
            logger.warning("Drive 服务初始化失败: %s", e)
            return None


def reset_drive_cache():
    """OAuth 状态变化时调用,下次上传重建连接"""
    global _drive_service
    with _lock:
        _drive_service = None


def is_enabled():
    return bool(getattr(config, "MEDIA_FOLDER_ID", "").strip())


async def upload_media(message, media_type, peer_name=""):
    """下载 Telethon message 的媒体并上传到 Drive。

    返回 (display_text, file_url)
    - display_text: 写到 Sheet 单元格的内容
        - 图片：=IMAGE("url")  → Sheet 直接显示缩略图
        - 文件/语音/视频/贴纸：=HYPERLINK("url", "📎 文件名")  → 点击下载
    - file_url: 原始文件 URL（也写库备查；目前数据库 schema 没新字段，先丢弃）
    上传失败/未启用 → 返回 ("", "") 让调用方 fallback 文字占位。
    """
    if not is_enabled():
        return "", ""
    drive = _get_drive()
    if not drive:
        return "", ""

    try:
        # 大小限制：Telethon 的 message.file.size 单位为字节
        size = 0
        try:
            size = int(getattr(message.file, "size", 0) or 0)
        except Exception:
            size = 0
        max_bytes = config.MEDIA_MAX_MB * 1024 * 1024
        if size and size > max_bytes:
            logger.info("跳过大文件上传 (%.1f MB > %d MB)", size / 1024 / 1024, config.MEDIA_MAX_MB)
            return "", ""

        # 下载到内存
        buf = io.BytesIO()
        await message.download_media(file=buf)
        data = buf.getvalue()
        if not data:
            return "", ""

        # 文件名：优先 Telethon 给的；图片/语音类没有名 → 自己命名
        ts = datetime.now(TZ_BJ).strftime("%Y%m%d_%H%M%S")
        ext_map = {"photo": ".jpg", "voice": ".ogg", "video": ".mp4", "sticker": ".webp"}
        original_name = ""
        try:
            original_name = getattr(message.file, "name", "") or ""
        except Exception:
            pass
        if original_name:
            filename = f"{ts}_{original_name}"
        else:
            filename = f"{ts}_{media_type}{ext_map.get(media_type, '.bin')}"
        # 清掉文件名里的换行/控制字符
        filename = "".join(c for c in filename if c.isprintable()).strip() or f"{ts}_{media_type}"

        # MIME
        mime = None
        try:
            mime = getattr(message.file, "mime_type", None)
        except Exception:
            pass
        if not mime:
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        # 上传
        from googleapiclient.http import MediaIoBaseUpload
        media_body = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        meta = {
            "name": filename,
            "parents": [config.MEDIA_FOLDER_ID],
        }
        f = drive.files().create(
            body=meta, media_body=media_body,
            fields="id,webViewLink,webContentLink",
            supportsAllDrives=True,
        ).execute()
        file_id = f["id"]

        # 设为公开（anyone with link 可读）—— =IMAGE() 必须能匿名访问
        try:
            drive.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                supportsAllDrives=True,
            ).execute()
        except Exception as e:
            logger.warning("设公开权限失败 (file=%s): %s", filename, e)

        # 全部用 =HYPERLINK 超链接,不渲染缩略图(缩略图挤在单元格里太小看不清)
        # 点链接 → 跳 Drive 预览页,看大图/下载都方便
        view_url = f.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        label_emoji = {
            "photo": "🖼",
            "voice": "🎙",
            "video": "🎬",
            "sticker": "🌟",
            "file": "📎",
        }.get(media_type, "📎")
        # 危险扩展名加 ⚠️ 前缀 — 客户一眼看出来别点下载
        # 只检扩展名,不做深度扫描(Drive 自己会扫病毒;这里只是提醒层)
        ext = os.path.splitext(filename)[1].lower()
        danger_prefix = "⚠️ " if ext in DANGER_EXTS else ""
        label = f"{danger_prefix}{label_emoji} {filename}"
        # Sheets 公式里的双引号要 escape
        safe_label = label.replace('"', '""')
        display = f'=HYPERLINK("{view_url}", "{safe_label}")'

        url = f.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        logger.info("媒体上传成功 [%s] %s → %s", media_type, filename, url)
        return display, url

    except Exception as e:
        logger.warning("媒体上传失败 [%s]: %s", media_type, e)
        return "", ""


def list_old_files(retention_days):
    """列出 MEDIA_FOLDER_ID 里 createdTime 早于 cutoff 的文件。
    Drive API 的 createdTime < 'RFC3339' 直接在服务端过滤,不用全拉下来自己比。
    """
    if not is_enabled():
        return []
    drive = _get_drive()
    if not drive:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    q = (
        f"'{config.MEDIA_FOLDER_ID}' in parents "
        f"and trashed=false "
        f"and createdTime < '{cutoff_iso}'"
    )
    old = []
    page_token = None
    while True:
        try:
            resp = drive.files().list(
                q=q,
                fields="nextPageToken, files(id, name, createdTime)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
        except Exception as e:
            logger.warning("list 旧媒体失败: %s", e)
            break
        old.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return old


def _build_archive_caption(media_type, account_row, peer_name, media_seq, file_name=""):
    """v2.10.25(ADR-0014):档案群消息 caption 统一格式。

    示例(photo,无文件名):
      【文件提醒】
      中心/部门：恒睿公司-渠道
      商务人员：江羽
      外事号：大兵
      广告主：杨幂
      文件编号：#42
      文件内容：图片

    account_row:sqlite3.Row,有 name / operator 字段(company 字段可能空 → fallback COMPANY_DISPLAY)
    """
    company = ""
    operator = ""
    account_name = ""
    try:
        if account_row is not None:
            company = (account_row["company"] or "").strip() if "company" in account_row.keys() else ""
            operator = (account_row["operator"] or "").strip() if "operator" in account_row.keys() else ""
            account_name = (account_row["name"] or "").strip() if "name" in account_row.keys() else ""
    except Exception:
        pass
    if not company:
        company = (getattr(config, "COMPANY_DISPLAY", "") or
                   getattr(config, "COMPANY_NAME", "") or "").strip()
    operator_label = getattr(config, "OPERATOR_LABEL", "商务人员") or "商务人员"
    peer_role_label = getattr(config, "PEER_ROLE_LABEL", "广告主") or "广告主"

    if media_type == "photo":
        content_desc = "图片"
    elif media_type == "file":
        # 文件有名字 → 附在后面,没名字 → 只写「文件」
        content_desc = f"文件:{file_name}" if file_name else "文件"
    elif media_type == "voice":
        content_desc = "语音"
    else:
        content_desc = media_type or "媒体"

    lines = [
        "【文件提醒】",
        f"中心/部门:{company}",
        f"{operator_label}:{operator}",
        f"外事号:{account_name}",
        f"{peer_role_label}:{peer_name}",
        f"文件编号:#{media_seq}",
        f"文件内容:{content_desc}",
    ]
    return "\n".join(lines)


async def forward_to_tg_archive(message, media_type, account_row, peer_name, media_seq):
    """v2.10.25(ADR-0014):把 Telethon 收到的媒体下载到内存,用 aiogram Bot 转发到
    MEDIA_ARCHIVE_GROUP_ID 指定的 TG 群,caption 带业务上下文。

    v2.10.25 首版只转 photo + file。v2.10.25 测试期用户补充要求加上 voice —
    所以当前支持:photo + file + voice(video / sticker 仍由调用方过滤)。

    返回 (display_text, archive_msg_id):
      - 成功:display = '=HYPERLINK("t.me/c/.../N", "图片 #42")' /
                        '=HYPERLINK(..., "文件 #42")' / '=HYPERLINK(..., "语音 #42")'
             archive_msg_id = TG 档案群里的 msg_id
      - 失败:("", 0) → 调用方 fallback 到文字占位「[图片]」「[文件]」「[语音]」

    Bot API 限制:photo 10MB / document 50MB / voice 50MB。photo 超出 → 降级用
    send_document 转发(仍能保留,只是档案群里显示为文件不是图片缩图)。
    voice 发送失败 → 降级 send_document(仍然保留 .ogg 文件供下载播放)。
    document 超限 → 放弃返回 ("", 0)。
    """
    if not is_tg_archive_enabled():
        return "", 0
    if media_type not in ("photo", "file", "voice"):
        return "", 0

    try:
        from aiogram.types import BufferedInputFile
    except Exception as e:
        logger.warning("aiogram 未安装或导入失败,无法转发到档案群: %s", e)
        return "", 0

    try:
        size = 0
        try:
            size = int(getattr(message.file, "size", 0) or 0)
        except Exception:
            size = 0
        # 用户可配置的总体大小上限(复用 MEDIA_MAX_MB,默认 20MB)
        max_bytes = config.MEDIA_MAX_MB * 1024 * 1024
        if size and size > max_bytes:
            logger.info("跳过大媒体转发 (%.1f MB > %d MB)", size / 1024 / 1024, config.MEDIA_MAX_MB)
            return "", 0
        # Document 上限保护(Bot API 硬限制 50MB)— 即便客户配更大也挡住
        if size and size > _BOT_DOCUMENT_LIMIT_BYTES:
            logger.info("跳过大媒体转发 (%.1f MB > Bot API 50MB 硬限制)", size / 1024 / 1024)
            return "", 0

        # 下载到内存
        buf = io.BytesIO()
        await message.download_media(file=buf)
        data = buf.getvalue()
        if not data:
            return "", 0

        # 文件名:优先 Telethon 给的;photo 没名 → 自己命名
        ts = datetime.now(TZ_BJ).strftime("%Y%m%d_%H%M%S")
        original_name = ""
        try:
            original_name = getattr(message.file, "name", "") or ""
        except Exception:
            pass
        ext_map = {"photo": ".jpg", "file": ".bin", "voice": ".ogg"}
        if original_name:
            filename = f"{ts}_{original_name}"
        else:
            filename = f"{ts}_{media_type}{ext_map.get(media_type, '.bin')}"
        filename = "".join(c for c in filename if c.isprintable()).strip() or f"{ts}_{media_type}"

        caption = _build_archive_caption(
            media_type, account_row, peer_name, media_seq,
            file_name=original_name,
        )

        # 转发到档案群
        input_file = BufferedInputFile(data, filename=filename)
        chat_id = config.MEDIA_ARCHIVE_GROUP_ID
        sent = None
        if media_type == "photo" and (size == 0 or size <= _BOT_PHOTO_LIMIT_BYTES):
            # 走 send_photo — TG 会生成缩图,档案群里直接预览
            try:
                sent = await _archive_bot.send_photo(chat_id, input_file, caption=caption)
            except Exception as e:
                logger.warning("send_photo 失败,降级用 send_document: %s", e)
                # 降级前要重建 BufferedInputFile(原 input_file 可能已被消费)
                input_file = BufferedInputFile(data, filename=filename)
                sent = await _archive_bot.send_document(chat_id, input_file, caption=caption)
        elif media_type == "voice":
            # 语音:走 send_voice 保留档案群里的波形播放体验;
            # 非 opus/ogg 格式可能触发 send_voice 报错 → 降级 send_document 仍能保留文件
            try:
                sent = await _archive_bot.send_voice(chat_id, input_file, caption=caption)
            except Exception as e:
                logger.warning("send_voice 失败,降级用 send_document: %s", e)
                input_file = BufferedInputFile(data, filename=filename)
                sent = await _archive_bot.send_document(chat_id, input_file, caption=caption)
        else:
            # file 类或 photo 超 10MB → send_document
            sent = await _archive_bot.send_document(chat_id, input_file, caption=caption)

        if sent is None or not getattr(sent, "message_id", 0):
            logger.warning("转发到档案群返回空结果")
            return "", 0

        archive_msg_id = int(sent.message_id)
        link = _archive_deep_link(archive_msg_id)
        label_map = {"photo": "图片", "file": "文件", "voice": "语音"}
        label_prefix = label_map.get(media_type, "媒体")
        label = f"{label_prefix} #{media_seq}"
        safe_label = label.replace('"', '""')
        display = f'=HYPERLINK("{link}", "{safe_label}")'
        logger.info("媒体转发档案群成功 [%s #%d] msg_id=%d", media_type, media_seq, archive_msg_id)
        return display, archive_msg_id

    except Exception as e:
        logger.warning("媒体转发档案群失败 [%s]: %s", media_type, e)
        return "", 0


def cleanup_old_media(retention_days=None):
    """删掉 MEDIA_FOLDER_ID 里超过 retention_days 天的文件。

    - retention_days=None → 读 config.MEDIA_RETENTION_DAYS
    - retention_days<=0 → 跳过(等于「永不删」)
    - 返回 (deleted_count, failed_count)
    - drive.file scope 只能删本应用上传的文件,不会误删客户 Drive 其他东西
    - Drive 回收站还留 30 天,真删错了还能恢复
    """
    if retention_days is None:
        retention_days = int(getattr(config, "MEDIA_RETENTION_DAYS", 0) or 0)
    if retention_days <= 0:
        logger.info("MEDIA_RETENTION_DAYS<=0, 跳过清理")
        return 0, 0
    if not is_enabled():
        logger.info("MEDIA_FOLDER_ID 未配置, 跳过清理")
        return 0, 0
    drive = _get_drive()
    if not drive:
        return 0, 0
    old_files = list_old_files(retention_days)
    if not old_files:
        logger.info("没有超过 %d 天的旧媒体文件", retention_days)
        return 0, 0
    deleted = 0
    failed = 0
    for f in old_files:
        try:
            drive.files().delete(
                fileId=f["id"],
                supportsAllDrives=True,
            ).execute()
            deleted += 1
        except Exception as e:
            logger.warning("删除文件失败 %s: %s", f.get("name"), e)
            failed += 1
    logger.info("清理旧媒体: 删 %d 个, 失败 %d 个 (> %d 天)", deleted, failed, retention_days)
    return deleted, failed
