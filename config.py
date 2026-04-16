"""配置文件 — 从 .env 加载所有设置"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
_ENV_PATH = BASE_DIR / ".env"
load_dotenv(_ENV_PATH, override=True)


def reload_if_env_changed():
    """v2.6.2: 检测 .env 文件变更,有变化则重新加载并刷新本模块热字段。
    用于 tg-monitor 容器感知 web 容器改过的开关(两者共用同一个 .env volume)。
    调用开销 = 一次 os.stat,极低,可以每次发预警前调用。
    """
    global _env_mtime_cache, ALERTS_ENABLED, DAILY_REPORT_ENABLED
    try:
        m = _ENV_PATH.stat().st_mtime
    except OSError:
        return False
    if m == _env_mtime_cache:
        return False
    _env_mtime_cache = m
    # 重新载入 .env(override=True 覆盖旧值)
    load_dotenv(_ENV_PATH, override=True)
    # 只刷新「运行时可切换」字段,其他配置改了本来就要重启容器
    ALERTS_ENABLED = os.environ.get("ALERTS_ENABLED", "true").lower() == "true"
    _daily_env_new = os.environ.get("DAILY_REPORT_ENABLED", "").strip().lower()
    if _daily_env_new in ("true", "false"):
        DAILY_REPORT_ENABLED = (_daily_env_new == "true")
    else:
        DAILY_REPORT_ENABLED = ALERTS_ENABLED
    return True


try:
    _env_mtime_cache = _ENV_PATH.stat().st_mtime
except OSError:
    _env_mtime_cache = 0.0

# Telegram API (有共用预设，但可改)
API_ID = int(os.environ.get("API_ID", "0") or "0")
API_HASH = os.environ.get("API_HASH", "")

# Google Sheets
# SHEET_ID 可留空 — 首次 OAuth 授权完成后,setup 精灵会自动建表并把 ID 写回 .env
SHEET_ID = os.environ.get("SHEET_ID", "")
# Service Account 已移除 — Level 1 架构只走客户 OAuth 授权(客户本人 15GB Drive 配额)
# SA 在非 Workspace 帐户下没有 Drive 存储配额,上传必 403,没救。

# 首次设置完成标志：true 才会让 tg-monitor 正常启动
SETUP_COMPLETE = os.environ.get("SETUP_COMPLETE", "false").lower() == "true"

# TG Bot
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
_group_id = os.environ.get("ALERT_GROUP_ID", "0").strip()
ALERT_GROUP_ID = int(_group_id) if _group_id else 0

# 预警推送总开关(v2.6.2+)
# true = 关键词/未回复/删除 三类预警都往 TG 群推(默认,旧部署升级后保持原行为)
# false = 三类预警全部静音,但:
#   - 原始消息依旧完整写 Sheets(listener 不看这个开关)
#   - 关键词监听 Sheet 分表依旧写(方便主管看板回溯)
#   - alerts DB 表依旧写(开回来时历史完整,不断层)
# 日报单独受 DAILY_REPORT_ENABLED 控制(默认跟随 ALERTS_ENABLED,可独立关)
ALERTS_ENABLED = os.environ.get("ALERTS_ENABLED", "true").lower() == "true"
# 日报推送开关。留空 = 跟随 ALERTS_ENABLED;显式 true/false 则独立
_daily_env = os.environ.get("DAILY_REPORT_ENABLED", "").strip().lower()
if _daily_env in ("true", "false"):
    DAILY_REPORT_ENABLED = (_daily_env == "true")
else:
    DAILY_REPORT_ENABLED = ALERTS_ENABLED

# 业务设置
KEYWORDS = [k.strip() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]
NO_REPLY_MINUTES = int(os.environ.get("NO_REPLY_MINUTES", "30"))
# 保留向后兼容（旧单段时段，已弃用，实际以 WORK_SCHEDULE 为准）
WORK_HOUR_START = int(os.environ.get("WORK_HOUR_START", "11"))
WORK_HOUR_END = int(os.environ.get("WORK_HOUR_END", "23"))
PATROL_DAYS = int(os.environ.get("PATROL_DAYS", "7"))
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "2"))  # 首次拉取天数

# 工作时段（北京时间，周一=0, 周日=6）
# 每段格式: (开始小时, 开始分钟, 结束小时, 结束分钟)
WORK_SCHEDULE = {
    0: [(11, 0, 13, 0), (15, 0, 19, 0), (20, 0, 23, 0)],  # 周一
    1: [(11, 0, 13, 0), (15, 0, 19, 0), (20, 0, 23, 0)],  # 周二
    2: [(11, 0, 13, 0), (15, 0, 19, 0), (20, 0, 23, 0)],  # 周三
    3: [(11, 0, 13, 0), (15, 0, 19, 0), (20, 0, 23, 0)],  # 周四
    4: [(11, 0, 13, 0), (15, 0, 19, 0), (20, 0, 23, 0)],  # 周五
    5: [(11, 0, 13, 0), (15, 0, 20, 0)],                   # 周六
    6: [],                                                  # 周日休息
}


def is_work_time(dt):
    """判断某个时间点是否在工作时间内（dt 需为北京时间 aware datetime）"""
    segments = WORK_SCHEDULE.get(dt.weekday(), [])
    minutes_of_day = dt.hour * 60 + dt.minute
    for sh, sm, eh, em in segments:
        if sh * 60 + sm <= minutes_of_day < eh * 60 + em:
            return True
    return False


def work_elapsed_minutes(start_dt, end_dt):
    """计算 start_dt → end_dt 之间累计的工作时间（分钟）。
    下班时段会被跳过。start_dt、end_dt 均为北京时间 aware datetime。"""
    from datetime import datetime as _dt, timedelta as _td
    if end_dt <= start_dt:
        return 0
    total = 0.0
    cur = start_dt
    # 以天为单位扫描，最多扫 14 天保底
    for _ in range(14):
        day_start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        segments = WORK_SCHEDULE.get(cur.weekday(), [])
        for sh, sm, eh, em in segments:
            seg_start = day_start.replace(hour=sh, minute=sm)
            seg_end = day_start.replace(hour=eh, minute=em)
            # 取 [cur, end_dt] 与 [seg_start, seg_end] 的交集
            lo = max(cur, seg_start)
            hi = min(end_dt, seg_end)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60.0
        # 推进到隔天 00:00
        next_day = day_start + _td(days=1)
        if next_day >= end_dt:
            break
        cur = next_day
    return total


# 时区
TIMEZONE = "Asia/Shanghai"

# 账号列表 (格式: 手机号|名称|公司|商务人员|分表名，多个用逗号分隔)
ACCOUNTS = []
_phones = os.environ.get("PHONES", "")
if _phones:
    for entry in _phones.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        ACCOUNTS.append({
            "phone": parts[0].strip(),
            "name": parts[1].strip() if len(parts) > 1 else "",
            "company": parts[2].strip() if len(parts) > 2 else "",
            "operator": parts[3].strip() if len(parts) > 3 else "",
            "sheet_tab": parts[4].strip() if len(parts) > 4 else "",
        })

# 部门/公司名称
COMPANY_NAME = os.environ.get("COMPANY_NAME", "")  # 用于容器名（英文）
COMPANY_DISPLAY = os.environ.get("COMPANY_DISPLAY", COMPANY_NAME)  # 用于页面显示（中文）
DEVICE_NAME = os.environ.get("DEVICE_NAME", "shencha")  # TG 设备名称

# 对话方角色称谓（不同部门可能叫「广告主」「客户」「合作方」等）
PEER_ROLE_LABEL = os.environ.get("PEER_ROLE_LABEL", "广告主")

# 媒体文件直显（图片/文件/语音/视频上传到 Drive 后展示在 Sheets）
# - MEDIA_FOLDER_ID：OAuth 授权完成后由 setup 精灵自动在客户 Drive 根目录建 "tg-monitor-媒体"
#   文件夹并把 ID 写回 .env。留空 = 不上传，仍显示文字占位。
# - MEDIA_RETENTION_DAYS：保留天数。>0 = 每天凌晨 3 点自动删超期 Drive 文件
#   （Drive 回收站再留 30 天可恢复），0 = 永不删（默认，opt-in 才清理，升级不会误删老文件）。
MEDIA_FOLDER_ID = os.environ.get("MEDIA_FOLDER_ID", "").strip()
MEDIA_RETENTION_DAYS = int(os.environ.get("MEDIA_RETENTION_DAYS", "0") or "0")
# 单文件上传大小上限（MB），防止大文件刷爆 Drive 配额
MEDIA_MAX_MB = int(os.environ.get("MEDIA_MAX_MB", "20") or "20")

# Sheets 刷写间隔（秒）
SHEETS_FLUSH_INTERVAL = int(os.environ.get("SHEETS_FLUSH_INTERVAL", "5"))

# 巡检间隔（秒）
PATROL_INTERVAL = int(os.environ.get("PATROL_INTERVAL", "60"))

# 会话文件目录
SESSION_DIR = BASE_DIR / "sessions"
SESSION_DIR.mkdir(exist_ok=True)

# 数据库
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "data.db"
