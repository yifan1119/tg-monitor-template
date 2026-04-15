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
import threading
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)
TZ_BJ = timezone(timedelta(hours=8))

_lock = threading.Lock()
_drive_service = None  # lazy 初始化 googleapiclient.discovery.Resource


def _get_drive():
    """lazy 创建 Drive v3 client（service account 凭证已在 sheets.py 配置过同款 scopes）"""
    global _drive_service
    if _drive_service is not None:
        return _drive_service
    with _lock:
        if _drive_service is not None:
            return _drive_service
        try:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
            creds = Credentials.from_service_account_file(
                str(config.SERVICE_ACCOUNT_FILE),
                scopes=[
                    "https://www.googleapis.com/auth/drive",
                    "https://www.googleapis.com/auth/spreadsheets",
                ],
            )
            _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
            logger.info("Drive 服务初始化成功")
            return _drive_service
        except Exception as e:
            logger.warning("Drive 服务初始化失败: %s", e)
            return None


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

        # 构 URL
        # 图片：用 thumbnail / direct view URL，=IMAGE() 才能渲染
        # 文件：用 webViewLink，点了开 Drive 预览页（带下载按钮）
        if media_type == "photo":
            # 旧版直链（=IMAGE 可吃）
            img_url = f"https://drive.google.com/thumbnail?id={file_id}&sz=w400"
            display = f'=IMAGE("{img_url}")'
        else:
            view_url = f.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
            label_emoji = {
                "voice": "🎙",
                "video": "🎬",
                "sticker": "🌟",
                "file": "📎",
            }.get(media_type, "📎")
            label = f"{label_emoji} {filename}"
            # Sheets 公式里的双引号要 escape
            safe_label = label.replace('"', '""')
            display = f'=HYPERLINK("{view_url}", "{safe_label}")'

        url = f.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        logger.info("媒体上传成功 [%s] %s → %s", media_type, filename, url)
        return display, url

    except Exception as e:
        logger.warning("媒体上传失败 [%s]: %s", media_type, e)
        return "", ""
