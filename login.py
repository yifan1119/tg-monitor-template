"""单独登录脚本 — 用来交互式输入验证码，生成 session 文件"""
import sys
from telethon.sync import TelegramClient
import config
import database as db

def login(phone):
    session_path = str(config.SESSION_DIR / phone.replace("+", ""))
    client = TelegramClient(session_path, config.API_ID, config.API_HASH)
    client.start(phone=phone)
    me = client.get_me()
    print(f"\n✅ 登录成功: {me.first_name} (@{me.username}) id={me.id}")
    print(f"   Session 文件已保存到: {session_path}.session")
    client.disconnect()

if __name__ == "__main__":
    phone = sys.argv[1] if len(sys.argv) > 1 else input("请输入手机号（带国码）: ")
    db.init_db()
    login(phone.strip())
