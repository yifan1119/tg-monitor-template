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


def _get_drive():
    """lazy 创建 Drive v3 client。

    优先级:
      1. OAuth 用户授权 (data/google_oauth_token.json) — 推荐,用客户 15GB 配额
      2. Service Account — fallback,只有客户开了 Workspace Shared Drive 时才能成功
         (普通帐户 SA 没配额会 403 「Service Accounts do not have storage quota」)
    """
    global _drive_service
    if _drive_service is not None:
        return _drive_service
    with _lock:
        if _drive_service is not None:
            return _drive_service
        try:
            from googleapiclient.discovery import build
            # 路径 1: OAuth 用户凭证
            try:
                import oauth_helper
                creds = oauth_helper.get_credentials()
                if creds:
                    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
                    logger.info("Drive 服务初始化成功 (OAuth 用户授权)")
                    return _drive_service
            except Exception as e:
                logger.warning("OAuth 凭证加载失败,fallback SA: %s", e)
            # 路径 2: Service Account fallback
            from google.oauth2.service_account import Credentials
            sa_creds = Credentials.from_service_account_file(
                str(config.SERVICE_ACCOUNT_FILE),
                scopes=[
                    "https://www.googleapis.com/auth/drive",
                    "https://www.googleapis.com/auth/spreadsheets",
                ],
            )
            _drive_service = build("drive", "v3", credentials=sa_creds, cache_discovery=False)
            logger.info("Drive 服务初始化成功 (Service Account fallback)")
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
