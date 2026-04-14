#!/bin/bash
# TG 监控 — 一键热更新脚本
# 用法:
#   cd /root/tg-monitor-<部门名>
#   ./update.sh
#
# 或 curl 方式(repo public 后):
#   curl -fsSL https://raw.githubusercontent.com/PINK1119ZZ/tg-monitor-template/main/update.sh | bash
#
# 行为:
#   1. git fetch + 硬重置到 origin/main (用户本地改动会被丢弃)
#   2. 保留 .env / data/ / sessions/ / service-account.json (这些在 volume 或 .gitignore)
#   3. docker compose up -d --build 重建镜像
#   4. 等容器起来并打印版本信息

set -e

INSTALL_DIR="$(pwd)"
COMPANY_NAME=""

# 自动侦测部门名: 从目录名推
if [[ "$(basename "$INSTALL_DIR")" =~ ^tg-monitor-(.+)$ ]]; then
    COMPANY_NAME="${BASH_REMATCH[1]}"
else
    # 从 .env 读
    if [ -f ".env" ]; then
        COMPANY_NAME=$(grep "^COMPANY_NAME=" .env | cut -d= -f2 | head -1)
    fi
fi

if [ -z "$COMPANY_NAME" ]; then
    echo "❌ 侦测不到部门名"
    echo "   请确认:"
    echo "     1. 当前目录是 /root/tg-monitor-<部门名>"
    echo "     2. 或 .env 内有 COMPANY_NAME=xxx"
    exit 1
fi

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║     TG 监控 — 热更新                         ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  部门: ${COMPANY_NAME}"
echo "  路径: ${INSTALL_DIR}"
echo ""

# 1. 拉最新代码
if [ -d ".git" ]; then
    echo "📥 拉取最新代码..."
    git fetch origin
    # 保留 .env 等本地文件,硬同步代码到 origin/main
    git reset --hard origin/main
    echo "  ✅ 代码已同步到 $(git rev-parse --short HEAD)"
else
    echo "❌ 这不是一个 git 仓库,无法 git pull"
    echo "   建议重新跑 install.sh 或手动 git clone"
    exit 1
fi

# 2. 重建容器
echo ""
echo "🐳 重建 Docker 镜像并重启容器..."
docker compose -p "tg-${COMPANY_NAME}" up -d --build

# 3. 等 web 起来
WEB_PORT=$(grep "^WEB_PORT=" .env 2>/dev/null | cut -d= -f2 | head -1)
if [ -n "$WEB_PORT" ]; then
    echo ""
    echo "⏳ 等待 web 服务就绪..."
    for i in {1..60}; do
        code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${WEB_PORT}/login" 2>/dev/null || echo "")
        if [ "$code" = "200" ]; then
            break
        fi
        sleep 1
    done
fi

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║              ✅ 热更新完成                    ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  版本: $(git rev-parse --short HEAD)"
echo "  提交: $(git log -1 --pretty=format:'%s')"
echo ""
echo "  查看日志:"
echo "    docker compose -p tg-${COMPANY_NAME} logs -f web"
echo "    docker compose -p tg-${COMPANY_NAME} logs -f tg-monitor"
echo ""
