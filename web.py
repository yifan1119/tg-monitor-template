"""Web 登录管理介面 — 账号登录 + 启用/停用"""
import asyncio
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, redirect, url_for
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

import os
from functools import wraps
from flask import session as flask_session

import config
import database as db
import gspread
from google.oauth2.service_account import Credentials


def _get_spreadsheet():
    """获取 Google Sheets 连接"""
    creds = Credentials.from_service_account_file(
        str(config.SERVICE_ACCOUNT_FILE),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(config.SHEET_ID)


def _create_sheet_tab(name):
    """登录成功后自动建分页（格式与舒舒一致：全部置中 + 斑马纹 + 冻结 6 行）"""
    try:
        sp = _get_spreadsheet()
        existing = [ws.title for ws in sp.worksheets()]
        if name in existing:
            return

        TOTAL_ROWS = 1000
        TOTAL_COLS = 30
        ws = sp.add_worksheet(title=name, rows=TOTAL_ROWS, cols=TOTAL_COLS)
        sheet_id = ws.id

        # 颜色常量
        CYAN = {"red": 0.3019608, "green": 0.8156863, "blue": 0.88235295}
        WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
        LIGHT_BLUE = {"red": 0.8784314, "green": 0.96862745, "blue": 0.98039216}
        TEAL = {"red": 0.29803923, "green": 0.69803923, "blue": 0.69803923}

        # 文字内容 (labels + 第一个对话槽标题)
        ws.update("A2:A3", [["商务人员"], ["中心/部门"]])
        ws.update("A5:C6", [
            ["A", "外事号", name],
            ["B", "广告主", ""],
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

        # 整张表全部置中 (row 1..TOTAL_ROWS, col 1..TOTAL_COLS)
        requests = [
            _repeat(0, TOTAL_ROWS, 0, TOTAL_COLS, {"backgroundColor": WHITE, **center_middle}),
            # Row 1: 青色横条 (全宽)
            _repeat(0, 1, 0, TOTAL_COLS, {"backgroundColor": CYAN, **center_middle}),
            # Row 2: 白底 + 置中 (全宽)
            _repeat(1, 2, 0, TOTAL_COLS, {"backgroundColor": WHITE, **center_middle}),
            # Row 3: 淡蓝底 + 置中 (全宽)
            _repeat(2, 3, 0, TOTAL_COLS, {"backgroundColor": LIGHT_BLUE, **center_middle}),
            # Row 4: 白底 + 白字 + 粗体 + 置中 (spacer, 全宽)
            _repeat(3, 4, 0, TOTAL_COLS, {
                "backgroundColor": WHITE,
                "textFormat": {"bold": True, "foregroundColor": WHITE},
                **center_middle,
            }),
            # Row 5-6 第一个对话槽 A-C: 青绿 + 粗体 + 置中
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
            # 列宽: A=180, B=192, C=350 (与舒舒一致)
            _col_dim(0, 1, 180),
            _col_dim(1, 2, 192),
            _col_dim(2, 3, 350),
            # 斑马纹: row 7+ A-C 交替浅蓝/白
            {"addBanding": {
                "bandedRange": {
                    "range": {"sheetId": sheet_id,
                              "startRowIndex": 6, "endRowIndex": TOTAL_ROWS,
                              "startColumnIndex": 0, "endColumnIndex": 3},
                    "rowProperties": {
                        "firstBandColor": LIGHT_BLUE,
                        "secondBandColor": WHITE,
                    },
                },
            }},
        ]

        sp.batch_update({"requests": requests})
        print(f"✅ 自动建分页成功: {name}")
    except Exception as e:
        print(f"❌ 自动建分页失败: {e}")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not flask_session.get("authed"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("WEB_SECRET_KEY", "tg-monitor-web-2026")

# 管理密码（从 .env 读取 WEB_PASSWORD，默认 tg@monitor2026）
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "tg@monitor2026")

# 登录中的 client 暂存 {phone: {"client": client, "phone_hash": hash}}
_pending = {}

# asyncio event loop for telethon
_loop = asyncio.new_event_loop()


def _start_loop():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


threading.Thread(target=_start_loop, daemon=True).start()


def run_async(coro):
    """在后台 event loop 中执行 async 函数"""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=30)


async def _make_client(session_path):
    """在 _loop 线程内创建 TelegramClient（避免 no event loop 错误）"""
    return TelegramClient(
        session_path, config.API_ID, config.API_HASH,
        device_model=config.DEVICE_NAME,
        system_version="1.0", app_version="1.0",
    )


async def _sign_in_code(client, phone, code, phone_hash):
    """验证码 sign_in + get_me。整个流程在 _loop 线程执行，避免 no event loop 错误"""
    await client.sign_in(phone, code, phone_code_hash=phone_hash)
    return await client.get_me()


async def _sign_in_password(client, password):
    """两步验证密码 sign_in + get_me，同样包在 _loop 线程"""
    await client.sign_in(password=password)
    return await client.get_me()


async def _disconnect(client):
    """关闭 client，必须在 _loop 线程执行"""
    await client.disconnect()


async def _connect(client):
    """连接 client，必须在 _loop 线程执行"""
    await client.connect()


async def _send_code_req(client, phone):
    """发送验证码，必须在 _loop 线程执行"""
    return await client.send_code_request(phone)


async def _is_authorized(client):
    return await client.is_user_authorized()


async def _get_me(client):
    return await client.get_me()


def get_sessions():
    """扫描 sessions 目录，返回已有的 session 列表"""
    sessions = []
    for f in config.SESSION_DIR.glob("*.session"):
        phone = "+" + f.stem
        # 先查 DB
        account = db.get_account_by_phone(phone)
        if account and account["name"]:
            sessions.append({
                "phone": phone,
                "name": account["name"],
                "username": account["username"] or "",
                "tg_id": account["tg_id"] or "",
                "company": account["company"] or "",
                "operator": account["operator"] or "",
                "status": "active",
            })
        else:
            # DB 没有，尝试连 Telegram 获取
            try:
                session_path = str(config.SESSION_DIR / f.stem)
                client = run_async(_make_client(session_path))
                run_async(_connect(client))
                if run_async(_is_authorized(client)):
                    me = run_async(_get_me(client))
                    name = ((me.first_name or "") + " " + (me.last_name or "")).strip()
                    username = me.username or ""
                    # 写入 DB
                    db.upsert_account(phone=phone, name=name, username=username, tg_id=me.id)
                    sessions.append({
                        "phone": phone, "name": name, "username": username,
                        "tg_id": me.id, "company": "", "operator": "", "status": "active",
                    })
                    run_async(_disconnect(client))
                else:
                    sessions.append({
                        "phone": phone, "name": "", "username": "",
                        "tg_id": "", "company": "", "operator": "", "status": "expired",
                    })
                    run_async(_disconnect(client))
            except Exception as e:
                sessions.append({
                    "phone": phone, "name": "", "username": "",
                    "tg_id": "", "company": "", "operator": "", "status": "error",
                })
    return sessions


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == WEB_PASSWORD:
            flask_session["authed"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="密码错误", company=config.COMPANY_DISPLAY)
    return render_template("login.html", error=None, company=config.COMPANY_DISPLAY)


@app.route("/logout")
def logout():
    flask_session.pop("authed", None)
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def index():
    db.init_db()
    sessions = get_sessions()
    return render_template("index.html", sessions=sessions, company=config.COMPANY_DISPLAY)


@app.route("/api/send-code", methods=["POST"])
@login_required
def send_code():
    """第一步：发送验证码"""
    phone = request.json.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "请输入手机号"})
    if not phone.startswith("+"):
        phone = "+" + phone

    try:
        session_path = str(config.SESSION_DIR / phone.replace("+", ""))
        client = run_async(_make_client(session_path))
        run_async(_connect(client))

        result = run_async(_send_code_req(client, phone))
        _pending[phone] = {
            "client": client,
            "phone_hash": result.phone_code_hash,
        }
        return jsonify({"ok": True, "phone": phone})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/verify-code", methods=["POST"])
@login_required
def verify_code():
    """第二步：验证码确认"""
    phone = request.json.get("phone", "").strip()
    code = request.json.get("code", "").strip()

    if phone not in _pending:
        return jsonify({"ok": False, "error": "请先发送验证码"})

    client = _pending[phone]["client"]
    phone_hash = _pending[phone]["phone_hash"]

    try:
        me = run_async(_sign_in_code(client, phone, code, phone_hash))

        tg_name = ((me.first_name or "") + " " + (me.last_name or "")).strip()

        # 存入数据库
        db.init_db()
        db.upsert_account(phone=phone, name=tg_name, username=me.username or "", tg_id=me.id)

        # 自动建 Sheets 分页
        _create_sheet_tab(tg_name)

        run_async(_disconnect(client))
        del _pending[phone]

        return jsonify({
            "ok": True,
            "name": me.first_name or "",
            "username": me.username or "",
        })
    except SessionPasswordNeededError:
        _pending[phone]["need_password"] = True
        return jsonify({"ok": False, "need_password": True, "error": "此账号有两步验证，请输入密码"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/verify-password", methods=["POST"])
@login_required
def verify_password():
    """两步验证密码"""
    phone = request.json.get("phone", "").strip()
    password = request.json.get("password", "").strip()

    if phone not in _pending:
        return jsonify({"ok": False, "error": "请先发送验证码"})

    client = _pending[phone]["client"]
    try:
        me = run_async(_sign_in_password(client, password))

        tg_name = ((me.first_name or "") + " " + (me.last_name or "")).strip()

        db.init_db()
        db.upsert_account(phone=phone, name=tg_name, username=me.username or "", tg_id=me.id)

        # 自动建 Sheets 分页
        _create_sheet_tab(tg_name)

        run_async(_disconnect(client))
        del _pending[phone]

        return jsonify({
            "ok": True,
            "name": me.first_name or "",
            "username": me.username or "",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/restart", methods=["POST"])
@login_required
def restart_monitor():
    """重启监控容器，加载新 session (via docker.sock)"""
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        container_name = "tg-monitor-" + config.COMPANY_NAME
        container = client.containers.get(container_name)
        container.restart(timeout=10)
        return jsonify({"ok": True, "msg": "监控已重启，新账号将自动开始监听"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/remove", methods=["POST"])
@login_required
def remove_session():
    """完整删除账号：session + DB 记录（peers/messages/alerts 级联）+ Sheets 分页"""
    phone = request.json.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "缺少手机号"})

    # 1. 删 session 文件
    session_file = config.SESSION_DIR / (phone.replace("+", "") + ".session")
    if session_file.exists():
        session_file.unlink()
        journal = session_file.with_suffix(".session-journal")
        if journal.exists():
            journal.unlink()

    # 2. 清 DB + 删 Sheets 分页
    account = db.get_account_by_phone(phone)
    deleted_db = False
    deleted_sheet = False
    if account:
        acc_id = account["id"]
        tab = account["sheet_tab"] or account["name"]
        conn = db.get_conn()
        # 级联清：alerts → messages → peers → accounts
        conn.execute("DELETE FROM alerts WHERE account_id=?", (acc_id,))
        conn.execute("DELETE FROM messages WHERE account_id=?", (acc_id,))
        conn.execute("DELETE FROM peers WHERE account_id=?", (acc_id,))
        conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        conn.commit()
        deleted_db = True

        # 3. 删 Sheets 分页（失败不影响主流程）
        if tab:
            try:
                import gspread
                from google.oauth2.service_account import Credentials
                creds = Credentials.from_service_account_file(
                    str(config.SERVICE_ACCOUNT_FILE),
                    scopes=["https://www.googleapis.com/auth/spreadsheets",
                            "https://www.googleapis.com/auth/drive"],
                )
                gc = gspread.authorize(creds)
                sh = gc.open_by_key(config.SHEET_ID)
                ws = sh.worksheet(tab)
                sh.del_worksheet(ws)
                deleted_sheet = True
            except Exception as e:
                print(f"删 Sheets 分页失败（可忽略）: {e}")

    return jsonify({
        "ok": True,
        "deleted_db": deleted_db,
        "deleted_sheet": deleted_sheet,
    })


if __name__ == "__main__":
    db.init_db()
    print("🌐 登录管理介面启动: http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
