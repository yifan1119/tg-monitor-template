#!/bin/bash
# TG 监控 — 一键安装脚本（支持同台 VPS 部署多个部门）
# 用法:
#   curl -fsSL .../install.sh | bash -s -- <COMPANY_NAME> [WEB_PORT]
#   ./install.sh <COMPANY_NAME> [WEB_PORT]
#
# 行为:
#   - 未指定 WEB_PORT 会自动从 5001 起扫描找下一个没被占用的
#   - COMPANY_NAME 已存在会提示是否重装（保留数据）
#   - 安装完 web 在 http://VPS:PORT/setup，业务配置到 web 填

set -e

COMPANY_NAME="${1:-}"
REQUESTED_PORT="${2:-}"
REPO="https://github.com/PINK1119ZZ/tg-monitor-template.git"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║       TG 监控系统 — 一键安装                 ║"
echo "╚══════════════════════════════════════════════╝"

# 没给部门名 → 先列现有部门
if [ -z "$COMPANY_NAME" ]; then
    echo ""
    echo "用法: $0 <COMPANY_NAME> [WEB_PORT]"
    echo ""
    echo "例如:"
    echo "  $0 yueda           # 自动选端口"
    echo "  $0 dingfeng 5002   # 指定端口"
    echo ""
    if ls -d /root/tg-monitor-* 2>/dev/null | head -1 > /dev/null; then
        echo "当前 VPS 已部署的部门:"
        for d in /root/tg-monitor-*/; do
            name=$(basename "$d" | sed 's/^tg-monitor-//')
            port=$(grep "^WEB_PORT=" "$d/.env" 2>/dev/null | cut -d= -f2 | head -1)
            running=$(docker ps --format "{{.Names}}" 2>/dev/null | grep -c "^tg-web-$name$" || echo 0)
            status=$([ "$running" = "1" ] && echo "✅ running" || echo "⚠️  stopped")
            echo "  - $name  (port $port)  $status"
        done
        echo ""
    fi
    exit 1
fi

INSTALL_DIR="/root/tg-monitor-${COMPANY_NAME}"

# 自动选端口（没指定的话）
if [ -z "$REQUESTED_PORT" ]; then
    WEB_PORT=5001
    while :; do
        # 检查端口是否被占用
        in_use_by_docker=$(docker ps --format "{{.Ports}}" 2>/dev/null | grep -c ":${WEB_PORT}->" || true)
        in_use_by_system=$(ss -tlnp 2>/dev/null | grep -c ":${WEB_PORT} " || true)
        if [ "$in_use_by_docker" = "0" ] && [ "$in_use_by_system" = "0" ]; then
            break
        fi
        WEB_PORT=$((WEB_PORT + 1))
        if [ "$WEB_PORT" -gt 5099 ]; then
            echo "❌ 5001-5099 端口全部被占用，请手动指定端口"
            exit 1
        fi
    done
    echo "  自动选择端口: ${WEB_PORT}"
else
    WEB_PORT="${REQUESTED_PORT}"
    # 检查用户指定的端口是否被占用（不是自己占的）
    occupying=$(docker ps --format "{{.Names}}\t{{.Ports}}" 2>/dev/null | grep ":${WEB_PORT}->" | awk '{print $1}' || true)
    if [ -n "$occupying" ] && [ "$occupying" != "tg-web-${COMPANY_NAME}" ]; then
        echo "❌ 端口 ${WEB_PORT} 已被容器 ${occupying} 占用"
        exit 1
    fi
fi

echo ""
echo "  部门名称: ${COMPANY_NAME}"
echo "  Web 端口: ${WEB_PORT}"
echo "  安装路径: ${INSTALL_DIR}"
echo ""

# 1. 检查/安装 Docker
if ! command -v docker &> /dev/null; then
    echo "📦 未检测到 Docker，开始安装..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
else
    echo "✅ Docker 已安装: $(docker --version)"
fi

# 2. 检查 docker compose
if ! docker compose version &> /dev/null; then
    echo "❌ 没找到 docker compose（v2），请先安装。老版的 docker-compose (v1) 不支持。"
    exit 1
fi

# 3. 目录检查
if [ -d "${INSTALL_DIR}" ]; then
    echo "⚠️  目录 ${INSTALL_DIR} 已存在（可能是同部门重装）"
    read -p "     是否继续（保留现有 sessions/data/凭证）？[y/N] " yn
    [[ "$yn" != "y" && "$yn" != "Y" ]] && exit 1
    echo "  保留现有数据，仅更新代码..."
    cd "${INSTALL_DIR}"
    git pull --rebase 2>/dev/null || echo "  (git pull 失败或不是 git 仓，跳过)"
else
    echo "📥 从 GitHub 拉取模板..."
    git clone "${REPO}" "${INSTALL_DIR}"
    cd "${INSTALL_DIR}"
fi

# 4. 生成骨架 .env（保留已有凭证字段不覆盖）
if [ ! -f ".env" ]; then
    echo "📝 生成骨架 .env..."
    cat > .env << EOF
# ========== ${COMPANY_NAME} ==========
# 首次设置未完成 — 请打开 http://<VPS>:${WEB_PORT}/setup 完成设置精灵

# Telegram API（所有部门共用预设，可在设置页修改）
API_ID=31462192
API_HASH=ab9a38defa8c7421ac9afc9e1a7f00f4

# 部门标识
COMPANY_NAME=${COMPANY_NAME}
COMPANY_DISPLAY=${COMPANY_NAME}

# Web 后台端口
WEB_PORT=${WEB_PORT}
WEB_PASSWORD=tg@monitor2026

# 以下字段由设置精灵填写
SHEET_ID=
BOT_TOKEN=
ALERT_GROUP_ID=0

# 业务默认值
KEYWORDS=到期,续费,暂停,下架,上架,地址,打款,欠费
NO_REPLY_MINUTES=30
WORK_HOUR_START=11
WORK_HOUR_END=23
PATROL_DAYS=7
HISTORY_DAYS=2
SHEETS_FLUSH_INTERVAL=5
PATROL_INTERVAL=60

# 完成标志（设置精灵填完会改成 true）
SETUP_COMPLETE=false
EOF
else
    # 已有 .env，只更新 WEB_PORT（以命令行为准）
    if grep -q "^WEB_PORT=" .env; then
        sed -i.bak "s/^WEB_PORT=.*/WEB_PORT=${WEB_PORT}/" .env && rm -f .env.bak
    else
        echo "WEB_PORT=${WEB_PORT}" >> .env
    fi
fi

# 5. 建空 service-account.json 占位（让 docker compose 不报 mount error）
if [ ! -f "service-account.json" ]; then
    echo "{}" > service-account.json
fi

# 6. 启动（project 名带部门名避免 compose 把同部门跨安装合并）
echo "🐳 构建镜像 + 启动容器..."
export COMPANY_NAME WEB_PORT
docker compose -p "tg-${COMPANY_NAME}" up -d --build

# 7. 等 web 起来
echo "⏳ 等待 web 服务就绪..."
for i in {1..60}; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${WEB_PORT}/setup" 2>/dev/null || echo "")
    if [ "$code" = "200" ]; then
        break
    fi
    sleep 1
done

VPS_IP=$(curl -s ifconfig.me 2>/dev/null || curl -s ipinfo.io/ip 2>/dev/null || echo "<VPS_IP>")

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║                ✅ 安装完成                    ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  📌 下一步：打开浏览器完成设置精灵"
echo ""
echo "     👉  http://${VPS_IP}:${WEB_PORT}/setup"
echo ""
echo "  当前 VPS 已部署部门:"
for d in /root/tg-monitor-*/; do
    name=$(basename "$d" | sed 's/^tg-monitor-//')
    port=$(grep "^WEB_PORT=" "$d/.env" 2>/dev/null | cut -d= -f2 | head -1)
    running=$(docker ps --format "{{.Names}}" 2>/dev/null | grep -c "^tg-web-$name$" || echo 0)
    mark=$([ "$running" = "1" ] && echo "✅" || echo "⚠️ ")
    echo "     ${mark} ${name}  →  http://${VPS_IP}:${port}"
done
echo ""
echo "  常用指令:"
echo "    cd ${INSTALL_DIR}"
echo "    docker compose -p tg-${COMPANY_NAME} logs -f web"
echo "    docker compose -p tg-${COMPANY_NAME} logs -f tg-monitor"
echo "    docker compose -p tg-${COMPANY_NAME} restart"
echo "    docker compose -p tg-${COMPANY_NAME} down        # 停止该部门"
echo ""
