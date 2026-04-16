#!/bin/bash
# TG 监控 — 一键回滚脚本
# 用法:
#   bash rollback.sh             # 回退到上一个版本(读 .last_commit)
#   bash rollback.sh <sha>       # 回退到指定 commit
#   bash rollback.sh --list      # 列出最近 10 个 commit
#
# 行为:
#   1. 读 .last_commit (update.sh 升级前自动写入) 或命令行 sha
#   2. git reset --hard 到目标 sha
#   3. docker compose up -d --build 重建容器
#   4. 健康检查 60 秒
#   5. 提示如果有 stash 是否要还原

set -e

INSTALL_DIR="$(pwd)"
COMPANY_NAME=""

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
    exit 1
fi

if [ ! -d ".git" ]; then
    echo "❌ 这不是一个 git 仓库"
    exit 1
fi

# ===== --list 模式: 列出最近 10 个 commit =====
if [ "$1" = "--list" ] || [ "$1" = "-l" ]; then
    echo ""
    echo "最近 10 个 commit:"
    echo ""
    git log --oneline -10 | sed 's/^/   /'
    echo ""
    echo "回退到指定 commit: bash rollback.sh <sha>"
    exit 0
fi

# ===== 决定目标 sha =====
TARGET_SHA=""
SOURCE=""
if [ -n "$1" ]; then
    TARGET_SHA="$1"
    SOURCE="命令行参数"
elif [ -f ".last_commit" ]; then
    TARGET_SHA=$(cat .last_commit | tr -d '[:space:]')
    SOURCE=".last_commit (上次 update.sh 升级前的版本)"
else
    echo "❌ 没有 .last_commit 文件,也没指定 sha"
    echo ""
    echo "用法:"
    echo "  bash rollback.sh             # 自动读 .last_commit"
    echo "  bash rollback.sh <sha>       # 回退到指定 commit"
    echo "  bash rollback.sh --list      # 列出最近 commit 让你选"
    exit 1
fi

# 验证 sha 存在
if ! git rev-parse --verify "$TARGET_SHA^{commit}" >/dev/null 2>&1; then
    echo "❌ 找不到 commit: $TARGET_SHA"
    echo "   先 git fetch origin 拉一下,或用 bash rollback.sh --list 看可用 commit"
    exit 1
fi

CURRENT_SHA=$(git rev-parse HEAD)
CURRENT_SHORT=$(git rev-parse --short HEAD)
TARGET_FULL=$(git rev-parse "$TARGET_SHA")
TARGET_SHORT=$(git rev-parse --short "$TARGET_SHA")
TARGET_MSG=$(git log -1 --pretty=format:'%s' "$TARGET_SHA")

if [ "$CURRENT_SHA" = "$TARGET_FULL" ]; then
    echo "ℹ 当前已经在 ${TARGET_SHORT},无需回退"
    exit 0
fi

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║     TG 监控 — 一键回滚                       ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  部门: ${COMPANY_NAME}"
echo "  当前: ${CURRENT_SHORT}"
echo "  目标: ${TARGET_SHORT}  ($(git log -1 --pretty=format:'%s' "$TARGET_SHA"))"
echo "  来源: ${SOURCE}"
echo ""

# 二次确认(交互式 tty 才问)
if [ -t 0 ]; then
    read -p "确认回退? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "取消"
        exit 0
    fi
fi

# ===== 本地修改保护 =====
ROLLBACK_STASH_TAG=""
if git status --porcelain | grep -qE "^[ MARC]M|^M[ MARC]"; then
    ROLLBACK_STASH_TAG="auto-stash-rollback-$(date +%Y%m%d-%H%M%S)"
    echo ""
    echo "⚠ 检测到本地修改的 tracked 文件,自动 stash:"
    git status --porcelain | grep -E "^[ MARC]M|^M[ MARC]" | sed 's/^/   /'
    git stash push -m "$ROLLBACK_STASH_TAG" >/dev/null 2>&1 && \
      echo "   ✅ stash 标签: ${ROLLBACK_STASH_TAG}"
fi

# ===== 1. 把当前 sha 也存一下,以防客户后悔回退 =====
echo "$CURRENT_SHA" > .last_commit
echo "📌 当前版本 ${CURRENT_SHORT} 已记入 .last_commit (再跑 rollback.sh 可前进回去)"

# ===== 2. git reset --hard =====
echo ""
echo "🔄 git reset --hard ${TARGET_SHORT} ..."
git reset --hard "$TARGET_FULL"

# ===== 3. 重建容器 =====
echo ""
echo "🐳 重建 Docker 镜像并重启容器..."
docker compose -p "tg-${COMPANY_NAME}" up -d --build

# ===== 4. 健康检查 =====
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
        echo "  ⚠ 健康检查超时 — 但代码已回退,自查容器日志"
        echo "    docker compose -p tg-${COMPANY_NAME} logs --tail 50 web"
    fi
fi

# ===== 5. 完成 =====
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║              ✅ 回滚完成                      ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  ${CURRENT_SHORT}  →  ${TARGET_SHORT}"
echo "  提交: ${TARGET_MSG}"
echo ""
if [ -n "$ROLLBACK_STASH_TAG" ]; then
    echo "  📦 回滚前的本地修改已 stash:${ROLLBACK_STASH_TAG}"
    echo "     还原: git stash list / git stash pop"
    echo ""
fi
echo "  想重新升级到最新版: bash update.sh"
echo "  想回到 ${CURRENT_SHORT}: bash rollback.sh ${CURRENT_SHORT}  (或直接 bash rollback.sh)"
echo ""
