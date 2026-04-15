"""Google OAuth 2.0 用户授权助手 — 用于 Drive 媒体上传。

为什么需要 OAuth:
  Service Account 没有 Drive 存储配额(0 GB),上传到客户文件夹会 403
  「Service Accounts do not have storage quota」。
  改用客户本人 OAuth 授权 → 用客户 15GB 免费配额。

流程:
  1. 客户在 Google Cloud Console 创建 OAuth 2.0 Client ID (Web application 类型)
     - Authorized redirect URI: http://VPS_IP:WEB_PORT/api/oauth/callback
  2. Client ID + Secret 填到 .env (GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET)
  3. 后台点「连接 Google Drive」→ 跳转 Google 授权页
  4. 客户同意 → callback 拿 refresh_token → 存 data/google_oauth_token.json
  5. 上传时用 refresh_token 换 access_token,用客户身份调 Drive API
"""
import json
import logging
import os
from pathlib import Path

# Google 有时会返回比请求更多的 scope(include_granted_scopes 副作用 / consent screen 多配)
# 不放宽 oauthlib 的严格 scope 比对就会 "Scope has changed from ... to ..." 直接报错
# 必须是字面量 "true",oauthlib 内部是 .lower() == 'true' 比对的,写 "1" 不生效
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "true"
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "0"  # 强制 HTTPS,正常应该是这样

logger = logging.getLogger(__name__)

# token 存储:跟 data.db 同目录,Docker volume 覆盖 → rebuild 不丢
TOKEN_PATH = Path(__file__).parent / "data" / "google_oauth_token.json"
TOKEN_PATH.parent.mkdir(exist_ok=True)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# drive.file = 只能访问本应用创建/打开的文件 → 比 drive 全权限安全很多


def _client_config(client_id, client_secret, redirect_uri):
    """Web application 类型的 OAuth client config 字典(给 google-auth-oauthlib 用)"""
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def build_auth_url(client_id, client_secret, redirect_uri, state=""):
    """生成授权 URL 让用户跳转过去"""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        _client_config(client_id, client_secret, redirect_uri),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",       # 必须,才能拿到 refresh_token
        include_granted_scopes="false",  # 不要把客户之前授权过的其他 scope 塞回来,避免 scope 比对炸
        prompt="consent",            # 强制每次都返 refresh_token(否则二次授权可能没有)
        state=state,
    )
    return auth_url


def exchange_code(client_id, client_secret, redirect_uri, code):
    """用授权码换 refresh_token + access_token,存到 TOKEN_PATH"""
    import warnings
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        _client_config(client_id, client_secret, redirect_uri),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    # 兜底 1:把所有 oauthlib 的 Warning 当 warning 处理(不让它升成 error)
    # 兜底 2:scope 不一致会 raise Warning 子类,catchall
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            flow.fetch_token(code=code)
        except Warning as w:
            # oauthlib 把 scope 不匹配当 Warning 抛 — 忽略,只要拿到 token 就行
            logger.warning("OAuth scope warning(已忽略): %s", w)
    creds = flow.credentials
    if not creds.refresh_token:
        raise RuntimeError("Google 没返回 refresh_token,请到 myaccount.google.com/permissions 撤销旧授权后重试")
    save_token({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": creds.refresh_token,
        "token": creds.token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": SCOPES,
    })
    # 拿一下用户邮箱给 UI 展示
    email = ""
    try:
        from googleapiclient.discovery import build
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        about = svc.about().get(fields="user(emailAddress)").execute()
        email = about.get("user", {}).get("emailAddress", "")
    except Exception as e:
        logger.warning("拿用户邮箱失败: %s", e)
    return email


def save_token(data):
    TOKEN_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("OAuth token 已保存到 %s", TOKEN_PATH)


def load_token():
    """读 token 文件;不存在或损坏返 None"""
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读 OAuth token 失败: %s", e)
        return None


def has_token():
    t = load_token()
    return bool(t and t.get("refresh_token"))


def get_credentials():
    """构造一个可直接给 googleapiclient 用的 Credentials 对象;auto-refresh access_token"""
    from google.oauth2.credentials import Credentials
    t = load_token()
    if not t or not t.get("refresh_token"):
        return None
    return Credentials(
        token=t.get("token"),
        refresh_token=t["refresh_token"],
        token_uri=t.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=t["client_id"],
        client_secret=t["client_secret"],
        scopes=t.get("scopes", SCOPES),
    )


def auto_create_folder(folder_name="tg-monitor-媒体"):
    """OAuth 授权完成后,自动在用户 Drive 根目录建一个文件夹,返回 folder_id。
    不让客户自己跑去 Drive 建。"""
    creds = get_credentials()
    if not creds:
        return ""
    try:
        from googleapiclient.discovery import build
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        # 先查同名文件夹,有就复用
        q = (f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' "
             f"and trashed=false")
        existing = drive.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
        if existing.get("files"):
            return existing["files"][0]["id"]
        # 没有就建
        f = drive.files().create(
            body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
            fields="id",
        ).execute()
        logger.info("自动建了 Drive 文件夹: %s (id=%s)", folder_name, f["id"])
        return f["id"]
    except Exception as e:
        logger.warning("自动建文件夹失败: %s", e)
        return ""


def revoke_token():
    """撤销并删除本地 token"""
    t = load_token()
    if not t:
        return False, "没有 token 可撤销"
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"token": t["refresh_token"]}).encode()
        urllib.request.urlopen(
            urllib.request.Request("https://oauth2.googleapis.com/revoke", data=data),
            timeout=10,
        )
    except Exception as e:
        logger.warning("Google 撤销 API 报错(本地 token 仍会删): %s", e)
    try:
        TOKEN_PATH.unlink()
    except Exception:
        pass
    return True, "已撤销"
