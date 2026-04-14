"""
TG 监听系统 — 主入口
功能: 多账号私聊监听 + Google Sheets 记录 + 关键词/未回复/删除 预警 + 日报
"""
import asyncio
import logging
import signal
import sys

import config
import database as db
from listener import Listener
from sheets import SheetsWriter
from bot import AlertBot
from tasks import TaskScheduler

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{config.COMPANY_DISPLAY or 'TG'}] %(name)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    logger.info("=" * 40)
    logger.info("TG 监听系统 启动中...")
    logger.info("=" * 40)

    # 1. 初始化数据库
    logger.info("初始化数据库...")
    db.init_db()
    logger.info("数据库就绪")

    # 2. 初始化 Sheets
    logger.info("连接 Google Sheets...")
    sheets = SheetsWriter()

    # 3. 初始化 Bot
    logger.info("初始化 Bot...")
    bot = AlertBot(sheets_writer=sheets)

    # 4. 初始化监听器
    logger.info("初始化监听器...")
    listener = Listener(
        on_keyword=bot.send_keyword_alert if bot.bot else None,
    )

    # 5. 自动扫描 sessions 目录，登录所有账号
    session_files = list(config.SESSION_DIR.glob("*.session"))
    if not session_files:
        logger.warning("没有找到任何 session 文件，请先通过 Web 界面登录")
        return

    # 从 session 文件名提取手机号
    phones = []
    for sf in session_files:
        phone = "+" + sf.stem
        phones.append(phone)

    # 同时检查 .env 里的 PHONES 配置（兼容旧配置）
    phones_cfg = {acfg["phone"]: acfg for acfg in config.ACCOUNTS}

    logger.info("准备登录 %d 个账号...", len(phones))
    for phone in phones:
        try:
            acfg = phones_cfg.get(phone, {})
            logger.info("登录 %s...", phone)
            account = await listener.add_account(phone)
            tg_name = account["name"].strip()
            db.upsert_account(
                phone=phone,
                name=acfg.get("name") or tg_name,
                username=account["username"],
                tg_id=account["tg_id"],
                company=acfg.get("company", ""),
                operator=acfg.get("operator", ""),
            )
            sheet_tab = acfg.get("sheet_tab") or tg_name
            if sheet_tab:
                conn = db.get_conn()
                conn.execute("UPDATE accounts SET sheet_tab=? WHERE phone=?", (sheet_tab, phone))
                conn.commit()
        except Exception as e:
            logger.error("%s 登录失败: %s", phone, e)
            continue

    if not listener.clients:
        logger.error("没有任何账号登录成功，退出")
        return

    # 6. 拉取历史消息
    logger.info("拉取最近 %d 天历史消息...", config.HISTORY_DAYS)
    for phone in listener.clients:
        await listener.pull_history(phone, days=config.HISTORY_DAYS)

    # 7. 同步表头（商务人员、所属公司等）
    logger.info("同步 Sheets 表头...")
    sheets.sync_headers()

    # 8. 首次批量写入 Sheets
    logger.info("首次写入 Sheets...")
    count = sheets.flush_pending()
    logger.info("写入 %d 条历史消息", count)

    # 9. 记录启动时间（只对启动后的新消息触发预警）
    from database import TZ_BJ, now_bj
    startup_time = now_bj()
    logger.info("启动时间: %s", startup_time)

    # 11. 启动定时任务
    scheduler = TaskScheduler(listener, sheets, bot, startup_time=startup_time)

    # 12. 并行运行所有服务
    logger.info("全部服务启动！监听 %d 个账号, 关键词: %s", len(listener.clients), config.KEYWORDS)

    tasks = [
        asyncio.create_task(listener.run_all()),
        asyncio.create_task(scheduler.start()),
    ]
    if bot.dp:
        tasks.append(asyncio.create_task(bot.start()))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到退出信号，关闭中...")
        await scheduler.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("已退出")
