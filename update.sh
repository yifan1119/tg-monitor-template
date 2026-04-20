#!/bin/bash
# TG 监控 — 一键热更新脚本(带回滚保护)
# 用法:
#   cd /root/tg-monitor-<部门名>
#   ./update.sh
#
# 或 curl 方式:
#   curl -fsSL https://raw.githubusercontent.com/yifan1119/tg-monitor-template/main/update.sh | bash
#
# 行为:
#   1. 先 fetch,比对远端 SHA,已是最新 → 直接退出,不动本地任何东西
#   2. 需要升级时:
#        - 把当前 commit sha 写入 .last_commit (rollback.sh 用)
#        - 检测到本地修改的 tracked 文件 → 自动 git stash 保护
#        - .env 自动补新字段 (METRICS_TOKEN / INSTALL_DIR / VPS_PUBLIC_IP)
#   3. git reset --hard origin/main
#   4. docker compose up -d --build 重建镜像
#   5. 健康检查 60 秒,失败 → 自动回退到升级前 sha (含 stash 还原)
#
#   保留:.env / data/ / sessions/ / data/google_oauth_token.json
#   untracked 文件不会被动 (你的本地新增文件安全)

set -e

INSTALL_DIR="$(pwd)"
COMPANY_NAME=""
STASH_TAG=""    # 如果做了 stash,记下 tag,失败时还原

# ===== 部门名侦测 =====
if [[ "$(basename "$INSTALL_DIR")" =~ ^tg-monitor-(.+)$ ]]; then
    COMPANY_NAME="${BASH_REMATCH[1]}"
else
    if [ -f ".env" ]; then
        COMPANY_NAME=$(grep "^COMPANY_NAME=" .env | cut -d= -f2 | head -1)
    fi
fi

if [ -z "$COMPANY_NAME" ]; then
    echo "❌ 侦测不到部门名"
    echo "   请确认: 当前目录是 /root/tg-monitor-<部门名>,或 .env 内有 COMPANY_NAME=xxx"
    exit 1
fi

if [ ! -d ".git" ]; then
    echo "❌ 这不是一个 git 仓库,无法 git pull"
    echo "   建议重新跑 install.sh 或手动 git clone"
    exit 1
fi

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║     TG 监控 — 热更新 (带回滚保护)            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  部门: ${COMPANY_NAME}"
echo "  路径: ${INSTALL_DIR}"
echo ""

# ===== 1. 先 fetch 比对远端,已是最新直接退出(不动本地) =====
echo "📥 检查远端版本..."
git fetch origin
OLD_SHA=$(git rev-parse HEAD)
OLD_SHORT=$(git rev-parse --short HEAD)
REMOTE_SHA=$(git rev-parse origin/main)
REMOTE_SHORT=$(git rev-parse --short origin/main)

if [ "$OLD_SHA" = "$REMOTE_SHA" ]; then
    echo ""
    echo "ℹ 当前已是最新版 (${OLD_SHORT}),无需升级"
    echo "  如需强制重建容器: docker compose -p tg-${COMPANY_NAME} up -d --build"
    exit 0
fi

echo "  本地: ${OLD_SHORT}  →  远端: ${REMOTE_SHORT}"
echo "$OLD_SHA" > .last_commit
echo "📌 升级前版本: ${OLD_SHORT}"

# ===== 2. 本地修改保护:有 modified 的 tracked 文件就 stash =====
if git status --porcelain | grep -qE "^[ MARC]M|^M[ MARC]"; then
    STASH_TAG="auto-stash-update-$(date +%Y%m%d-%H%M%S)"
    echo ""
    echo "⚠ 检测到本地修改的 tracked 文件,自动 stash 保护:"
    git status --porcelain | grep -E "^[ MARC]M|^M[ MARC]" | sed 's/^/   /'
    if git stash push -m "$STASH_TAG" >/dev/null 2>&1; then
        echo "   ✅ 已 stash (标签: ${STASH_TAG})"
        echo "   想还原本地修改: git stash list / git stash pop"
    else
        echo "   ⚠ stash 失败,仍会强制覆盖"
        STASH_TAG=""
    fi
fi

# ===== 3. 拉最新代码 =====
echo ""
echo "📥 拉取最新代码..."
git reset --hard origin/main
NEW_SHA=$(git rev-parse HEAD)
NEW_SHORT=$(git rev-parse --short HEAD)
echo "  ✅ 代码已同步到 ${NEW_SHORT}"

# ===== 3.5 .env migrate — 老部署升级时自动补新字段 =====
#   v2.8.0: METRICS_TOKEN (中央台接入)
if [ -f ".env" ] && ! grep -q "^METRICS_TOKEN=" .env; then
    NEW_TOKEN=$(openssl rand -hex 24 2>/dev/null || head -c 48 /dev/urandom | base64 | tr -dc 'a-z0-9' | head -c 48)
    [ -n "$(tail -c 1 .env)" ] && echo "" >> .env
    echo "" >> .env
    echo "# 中央台接入 Token (v2.8+; 设置页可重置)" >> .env
    echo "METRICS_TOKEN=${NEW_TOKEN}" >> .env
    echo "  ✅ 已为本部署生成 METRICS_TOKEN (登入后在设置页「中央台接入」复制)"
fi

#   v2.10.0: INSTALL_DIR + VPS_PUBLIC_IP (驾驶舱 + 升级按钮用)
if [ -f ".env" ] && ! grep -q "^INSTALL_DIR=" .env; then
    [ -n "$(tail -c 1 .env)" ] && echo "" >> .env
    echo "INSTALL_DIR=${INSTALL_DIR}" >> .env
    echo "  ✅ 已补 INSTALL_DIR=${INSTALL_DIR}"
fi
if [ -f ".env" ] && ! grep -q "^VPS_PUBLIC_IP=" .env; then
    VPS_IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || curl -s --max-time 3 ipinfo.io/ip 2>/dev/null || hostname -I | awk '{print $1}')
    [ -n "$(tail -c 1 .env)" ] && echo "" >> .env
    echo "VPS_PUBLIC_IP=${VPS_IP}" >> .env
    echo "  ✅ 已补 VPS_PUBLIC_IP=${VPS_IP}"
fi

# ===== 4. 重建容器 =====
echo ""
# v2.10.20: compose up --build 遇到 "container name already in use" 时不会自动清,
#   常见于:旧版本 compose 文件 project label 跟当前 -p 参数不一致,或者 label 丢失。
#   强制清跟当前 project 不一致的同名容器(跟 install.sh 逻辑对齐),
#   tg-(monitor|web|caddy)-<部门> 三个名字本来就是当前部门独占。
ORPHANS=$(docker ps -a --format '{{.Names}}' 2>/dev/null \
    | grep -E "^tg-(monitor|web|caddy)-${COMPANY_NAME}$" || true)
if [ -n "$ORPHANS" ]; then
    for c in $ORPHANS; do
        proj=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$c" 2>/dev/null || echo "")
        if [ "$proj" != "tg-${COMPANY_NAME}" ]; then
            echo "  🧹 清理同名容器: $c (compose project=\"$proj\")"
            docker rm -f "$c" >/dev/null 2>&1 || true
        fi
    done
fi

echo "🐳 重建 Docker 镜像并重启容器..."
docker compose -p "tg-${COMPANY_NAME}" up -d --build

# ===== 5. 健康检查 — 失败自动回滚 =====
WEB_PORT=$(grep "^WEB_PORT=" .env 2>/dev/null | cut -d= -f2 | head -1)
if [ -n "$WEB_PORT" ]; then
    echo ""
    echo "⏳ 健康检查 (最多 60 秒)..."
    HEALTHY=0
    for i in {1..60}; do
        code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${WEB_PORT}/login" 2>/dev/null || echo "")
        if [ "$code" = "200" ]; then
            HEALTHY=1
            echo "  ✅ web 已就绪 (${i}s)"
            break
        fi
        sleep 1
    done

    if [ $HEALTHY -eq 0 ]; then
        echo ""
        echo "╔══════════════════════════════════════════════╗"
        echo "║   ❌ 健康检查失败 — 自动回退到 ${OLD_SHORT}    ║"
        echo "╚══════════════════════════════════════════════╝"
        echo ""
        echo "🔄 git reset --hard ${OLD_SHORT} ..."
        git reset --hard "$OLD_SHA"
        echo "🐳 重建旧版本容器..."
        docker compose -p "tg-${COMPANY_NAME}" up -d --build

        if [ -n "$STASH_TAG" ]; then
            STASH_REF=$(git stash list | grep "$STASH_TAG" | head -1 | awk -F: '{print $1}')
            if [ -n "$STASH_REF" ]; then
                echo "📦 还原 stash (${STASH_TAG})..."
                git stash pop "$STASH_REF" >/dev/null 2>&1 || \
                  echo "  ⚠ stash 还原失败,请手动跑: git stash list / git stash pop"
            fi
        fi

        echo ""
        echo "已回退到升级前版本 ${OLD_SHORT}"
        echo "看新版本日志找原因: docker compose -p tg-${COMPANY_NAME} logs --tail 50 web"
        exit 1
    fi
fi

# ===== 6. 升级成功 =====
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║              ✅ 热更新完成                    ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  ${OLD_SHORT}  →  ${NEW_SHORT}"
echo "  提交: $(git log -1 --pretty=format:'%s')"
echo ""
if [ -n "$STASH_TAG" ]; then
    echo "  📦 升级前的本地修改已 stash:${STASH_TAG}"
    echo "     还原: git stash list / git stash pop"
    echo ""
fi
echo "  万一新版有问题,一键回退:"
echo "    bash rollback.sh"
echo ""
echo "  查看日志:"
echo "    docker compose -p tg-${COMPANY_NAME} logs -f web"
echo "    docker compose -p tg-${COMPANY_NAME} logs -f tg-monitor"
echo ""
