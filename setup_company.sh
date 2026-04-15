#!/bin/bash
# 用法: ./setup_company.sh <公司名> <预警群ID> <BOT_TOKEN> <SHEET_ID> [WEB_PORT]
# 示例: ./setup_company.sh yueda -1003789623248 8778xxx:AAxxxx 1TDNxxxYH3xxx 5001

COMPANY=$1
GROUP_ID=$2
BOT_TOKEN=$3
SHEET_ID=$4
WEB_PORT=${5:-5001}

if [ -z "$COMPANY" ] || [ -z "$GROUP_ID" ] || [ -z "$BOT_TOKEN" ] || [ -z "$SHEET_ID" ]; then
    echo "用法: ./setup_company.sh <公司名> <预警群ID> <BOT_TOKEN> <SHEET_ID> [WEB_PORT]"
    echo "示例: ./setup_company.sh yueda -1003789623248 8778xxx:AAxxxx 1TDNxxxYH3xxx 5001"
    echo ""
    echo "参数说明:"
    echo "  公司名       英文/拼音，用作容器名后缀，例如 yueda, dept1"
    echo "  预警群ID     TG 预警群的 chat_id（通常是负数）"
    echo "  BOT_TOKEN    @BotFather 建的 bot token"
    echo "  SHEET_ID     Google Sheet 的 ID（URL 中 /d/ 后面那段）"
    echo "  WEB_PORT     web 后台端口，默认 5001。多部门共用 VPS 时要给不同端口"
    exit 1
fi

TARGET="/root/tg-monitor-${COMPANY}"
TEMPLATE_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -d "$TARGET" ]; then
    echo "❌ 目录 $TARGET 已存在，请先手动处理"
    exit 1
fi

echo "📁 复制模板到 $TARGET ..."
cp -r "$TEMPLATE_DIR" "$TARGET"

# 清空实例专属目录（防止把模板里可能残留的数据带过去）
rm -rf "$TARGET/.git" "$TARGET/sessions"/* "$TARGET/data"/* "$TARGET/__pycache__"
mkdir -p "$TARGET/sessions" "$TARGET/data"

# 生成 .env
cat > "$TARGET/.env" << EOF
# ========== ${COMPANY} ==========
# Telegram API（所有部门共用同一组 API 凭证）
API_ID=31462192
API_HASH=ab9a38defa8c7421ac9afc9e1a7f00f4

# 公司/部门标识
COMPANY_NAME=${COMPANY}
COMPANY_DISPLAY=${COMPANY}

# Google Sheets
SHEET_ID=${SHEET_ID}

# Bot / 预警群
BOT_TOKEN=${BOT_TOKEN}
ALERT_GROUP_ID=${GROUP_ID}

# Web 后台端口（多部门共用 VPS 时必须各不相同）
WEB_PORT=${WEB_PORT}

# 关键词（逗号分隔）
KEYWORDS=到期,续费,暂停,下架,上架,地址,打款,欠费,返点,返利,回扣

# 未回复预警（分钟）
NO_REPLY_MINUTES=30

# 工作时间
WORK_HOUR_START=11
WORK_HOUR_END=23

# 巡检
PATROL_DAYS=7
HISTORY_DAYS=2
SHEETS_FLUSH_INTERVAL=5
PATROL_INTERVAL=60
EOF

# 提示 service-account.json
if [ ! -f "$TARGET/service-account.json" ]; then
    echo "⚠️  service-account.json 不在目录里，待会儿记得放过来（所有部门可共用同一个 Google 服务账号）"
fi

echo ""
echo "✅ ${COMPANY} 环境已建好: $TARGET"
echo ""
echo "接下来的步骤:"
echo ""
echo "  1. 放入 service-account.json (Google Sheets 服务账号凭证):"
echo "     cp /path/to/service-account.json $TARGET/"
echo ""
echo "  2. 在目标 Google Sheet 里把服务账号加为编辑者"
echo "     (服务账号邮箱在 service-account.json 的 client_email 字段)"
echo ""
echo "  3. 启动服务:"
echo "     cd $TARGET"
echo "     docker compose up -d --build"
echo ""
echo "  4. 浏览器打开 http://<VPS_IP>:${WEB_PORT} 登录外事号:"
echo "     - 默认密码在 web.py 的 LOGIN_PASSWORD 常量"
echo "     - 输入手机号 → 收验证码 → 登录"
echo "     - 登录后系统会自动建对应的 Sheets 分页"
echo ""
echo "  5. 在 TG 预警群里让 Bot 成为群管理员，并发送任意消息测试"
