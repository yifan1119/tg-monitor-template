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
REPO="https://github.com/yifan1119/tg-monitor-template.git"

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

# Telegram API（每个部门自己去 my.telegram.org 申请,设置页填入）
API_ID=
API_HASH=

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
KEYWORDS=到期,续费,暂停,下架,上架,地址,打款,欠费,返点,返利,回扣
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

# 拿真实公网 IPv4
# 1) 从默认路由源地址拿(最可靠,就是出口 eth0 的 IPv4)
# 2) fallback 到 hostname -I / 外网服务(强制 -4)
VPS_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
if [ -z "$VPS_IP" ]; then
    VPS_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
if [ -z "$VPS_IP" ] || [[ "$VPS_IP" =~ ^(10\.|172\.1[6-9]\.|172\.2[0-9]\.|172\.3[0-1]\.|192\.168\.|127\.) ]]; then
    VPS_IP=$(curl -4 -s --max-time 5 ifconfig.me 2>/dev/null || curl -4 -s --max-time 5 ipinfo.io/ip 2>/dev/null || echo "<VPS_IP>")
fi

# 自检:从公网 IP 能不能访问到 web
EXTERNAL_OK="unknown"
if [ "$VPS_IP" != "<VPS_IP>" ]; then
    ext_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://${VPS_IP}:${WEB_PORT}/setup" 2>/dev/null || echo "")
    [ "$ext_code" = "200" ] && EXTERNAL_OK="yes" || EXTERNAL_OK="no"
fi

# VPS 内部 ufw 兜底(有些镜像默认装了 ufw 且 active)
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "${WEB_PORT}/tcp" >/dev/null 2>&1 || true
fi

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║                ✅ 安装完成                    ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  📌 下一步：打开浏览器完成设置精灵"
echo ""
echo "     👉  http://${VPS_IP}:${WEB_PORT}/setup"
echo ""

if [ "$EXTERNAL_OK" = "yes" ]; then
    echo "  ✅ 外网自检通过，直接打开上面链接即可开始设置"
    echo ""
elif [ "$EXTERNAL_OK" = "no" ]; then
    echo "  ⚠️  外网自检未通过（本机 HTTP 200，外部 IP 访问不到）"
    echo ""
    echo "     99% 是云厂商的云端防火墙挡了端口 ${WEB_PORT}"
    echo "     解决：去云厂商控制台开放 TCP ${WEB_PORT} 入站规则"
    echo "       • Hostinger : hPanel → VPS → 安全 → 防火墙"
    echo "       • AWS       : EC2 → Security Groups → Inbound Rules"
    echo "       • GCP       : VPC Network → Firewall → Create Rule"
    echo "       • DO/Vultr  : Networking → Firewalls"
    echo "       • 阿里/腾讯 : 云服务器控制台 → 安全组 → 入方向"
    echo ""
    echo "     开完后再打开上面的链接即可"
    echo ""
fi

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
