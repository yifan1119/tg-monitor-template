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
#   1. 升级前自动:
#        - 把当前 commit sha 写入 .last_commit (rollback.sh 用)
#        - 检测到本地修改的 tracked 文件 → 自动 git stash 保护
#   2. git fetch + git reset --hard origin/main
#   3. docker compose up -d --build 重建镜像
#   4. 健康检查 60 秒,失败 → 自动回退到升级前 sha (含 stash 还原)
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

# ===== 1. 记录升级前 sha (rollback.sh 读这个文件) =====
OLD_SHA=$(git rev-parse HEAD)
OLD_SHORT=$(git rev-parse --short HEAD)
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
git fetch origin
git reset --hard origin/main
NEW_SHA=$(git rev-parse HEAD)
NEW_SHORT=$(git rev-parse --short HEAD)
echo "  ✅ 代码已同步到 ${NEW_SHORT}"

# 没变化:不用动 docker
if [ "$OLD_SHA" = "$NEW_SHA" ]; then
    echo ""
    echo "ℹ 已经是最新版,无需重建容器"
    # stash 还原(因为没升级,本地修改要还回去)
    if [ -n "$STASH_TAG" ]; then
        STASH_REF=$(git stash list | grep "$STASH_TAG" | head -1 | awk -F: '{print $1}')
        if [ -n "$STASH_REF" ]; then
            git stash pop "$STASH_REF" >/dev/null 2>&1 && \
              echo "📦 本地修改已还原 (${STASH_TAG})"
        fi
    fi
    exit 0
fi

# ===== 4. 重建容器 =====
echo ""
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

        # 还原 stash
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
