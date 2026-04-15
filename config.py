"""配置文件 — 从 .env 加载所有设置"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env", override=True)

# Telegram API (有共用预设，但可改)
API_ID = int(os.environ.get("API_ID", "0") or "0")
API_HASH = os.environ.get("API_HASH", "")

# Google Sheets
SHEET_ID = os.environ.get("SHEET_ID", "")
SERVICE_ACCOUNT_FILE = BASE_DIR / "service-account.json"

# 首次设置完成标志：true 才会让 tg-monitor 正常启动
SETUP_COMPLETE = os.environ.get("SETUP_COMPLETE", "false").lower() == "true"

# TG Bot
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
_group_id = os.environ.get("ALERT_GROUP_ID", "0").strip()
ALERT_GROUP_ID = int(_group_id) if _group_id else 0

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
