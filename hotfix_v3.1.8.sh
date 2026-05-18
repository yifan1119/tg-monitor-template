#!/bin/bash
# v3.1.8 hotfix 升级脚本 — 只 recreate tg-monitor 容器,不动 caddy / tg-web
#
# 用法:
#   cd /root/tg-monitor-<部门>
#   bash hotfix_v3.1.8.sh
#
# 跟 update.sh 区别:
#   - update.sh:docker compose up --build (recreate 所有容器:tg-monitor + tg-web + caddy)
#                → 升级面广,适合架构改动 / Caddy 改动
#   - hotfix_v3.1.8.sh:docker compose up --no-deps --force-recreate tg-monitor (只动 1 个容器)
#                → HTTPS 零变动,caddy 不重启不会断 dept network connect
#                → 适合纯应用层小改动(v3.1.8 只动 media_uploader.py)
#
# 行为:
#   1. fetch v3.1.8 tag,比对当前 sha
#   2. 自动 detect docker compose project name(支持目录名 ≠ project name)
#   3. 备份当前 commit sha 到 .last_commit_hotfix(回滚用)
#   4. git reset --hard v3.1.8
#   5. docker compose -p <project> up -d --no-deps --build --force-recreate tg-monitor
#   6. 健康检查 + v3.1.8 代码确认
#   7. 失败 → 自动回滚到升级前 sha
#
# 保留:.env / data/ / sessions/ / Caddyfile / conf.d/(完全不动)

set -e

INSTALL_DIR="$(pwd)"
TARGET_TAG="${TARGET_TAG:-v3.1.9}"
TARGET_FILE="media_uploader.py"
SERVICE_NAME="tg-monitor"
HEALTH_TIMEOUT=60
LAST_HOTFIX_FILE=".last_commit_hotfix"

log() { echo "[hotfix_v3.1.8] $*"; }
err() { echo "[hotfix_v3.1.8] ❌ $*" >&2; }

# ===== 1. Sanity check =====
if [ ! -f "docker-compose.yml" ]; then
    err "当前目录 $INSTALL_DIR 没 docker-compose.yml,不是 tg-monitor 部署目录"
    exit 1
fi
if [ ! -f "$TARGET_FILE" ]; then
    err "当前目录没 $TARGET_FILE,不是 tg-monitor 仓库"
    exit 1
fi
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    err "当前目录不是 git 仓库"
    exit 1
fi

# ===== 2. Detect docker compose project name =====
# 客户 VPS 的 project name 可能不等于目录名(docker-compose.yml 里 container_name 跟项目名脱钩)
# 用 docker compose ls 查实际跑着的 project name
PROJECT_NAME=$(docker compose ls --format json 2>/dev/null \
    | python3 -c "
import sys, json, os
try:
    data = json.load(sys.stdin)
    target_dir = os.path.realpath('$INSTALL_DIR')
    for p in data:
        cfg = p.get('ConfigFiles', '')
        if cfg and os.path.realpath(os.path.dirname(cfg.split(',')[0])) == target_dir:
            print(p['Name']); break
except Exception:
    pass
" 2>/dev/null || true)

if [ -z "$PROJECT_NAME" ]; then
    log "⚠ docker compose ls 找不到对应 project,fallback 用目录名"
    PROJECT_NAME=$(basename "$INSTALL_DIR")
fi
log "docker compose project=$PROJECT_NAME"

# ===== 3. Fetch + compare =====
git fetch origin --tags --force 2>&1 | tail -3 || true
TARGET_SHA=$(git rev-parse "$TARGET_TAG^{commit}" 2>/dev/null || true)
if [ -z "$TARGET_SHA" ]; then
    err "tag $TARGET_TAG 不存在(fetch 失败?)"
    exit 1
fi
CURRENT_SHA=$(git rev-parse HEAD)
if [ "$CURRENT_SHA" = "$TARGET_SHA" ]; then
    log "已经在 $TARGET_TAG ($CURRENT_SHA),无需升级"
    exit 0
fi
log "当前 $CURRENT_SHA → 目标 $TARGET_TAG ($TARGET_SHA)"

# ===== 4. Backup + reset =====
echo "$CURRENT_SHA" > "$LAST_HOTFIX_FILE"
log "备份当前 sha 到 $LAST_HOTFIX_FILE"

# 自动 stash 本地修改(rollback 还原用)
STASH_TAG=""
if ! git diff --quiet || ! git diff --cached --quiet; then
    STASH_TAG="hotfix-v3.1.8-$(date +%s)"
    git stash push -m "$STASH_TAG" --include-untracked 2>&1 | tail -2 || true
    log "本地修改已 stash: $STASH_TAG"
fi

log "git reset --hard $TARGET_TAG..."
git reset --hard "$TARGET_TAG"

# ===== 5. 升级前 baseline:记录 caddy / tg-web 当前状态 =====
CADDY_BEFORE=$(docker ps --filter "name=tg-caddy-" --format "{{.Names}}:{{.Status}}" 2>/dev/null | head -1)
WEB_BEFORE=$(docker ps --filter "name=tg-web-" --format "{{.Names}}:{{.Status}}" 2>/dev/null | head -1)
log "升级前 caddy=$CADDY_BEFORE"
log "升级前 web=$WEB_BEFORE"

# ===== 6. Recreate 仅 tg-monitor =====
log "docker compose -p $PROJECT_NAME up -d --no-deps --build --force-recreate $SERVICE_NAME"
if ! docker compose -p "$PROJECT_NAME" up -d --no-deps --build --force-recreate "$SERVICE_NAME" 2>&1 | tail -10; then
    err "docker compose up 失败,回滚..."
    git reset --hard "$CURRENT_SHA"
    [ -n "$STASH_TAG" ] && git stash pop 2>&1 | tail -2 || true
    exit 1
fi

# ===== 7. 健康检查 =====
log "等待 tg-monitor 启动..."
sleep 5

# 找 tg-monitor 容器实例名
TG_MONITOR_CONTAINER=$(docker ps --filter "name=tg-monitor-" --format "{{.Names}}" \
    | grep -v "tg-monitor-multi\|tg-monitor-freshtest\|tg-monitor-v225test\|tg-monitor-sim" \
    | head -1)
if [ -z "$TG_MONITOR_CONTAINER" ]; then
    TG_MONITOR_CONTAINER=$(docker compose -p "$PROJECT_NAME" ps -q "$SERVICE_NAME" | head -1)
fi
log "tg-monitor 容器: $TG_MONITOR_CONTAINER"

# 健康轮询
HEALTHY=0
for i in $(seq 1 $((HEALTH_TIMEOUT/3))); do
    STATUS=$(docker inspect "$TG_MONITOR_CONTAINER" --format '{{.State.Health.Status}}' 2>/dev/null || echo "no-health")
    if [ "$STATUS" = "healthy" ] || [ "$STATUS" = "no-health" ]; then
        # no-health = 容器没配 healthcheck,只看 running 就行
        RUNNING=$(docker inspect "$TG_MONITOR_CONTAINER" --format '{{.State.Running}}' 2>/dev/null)
        if [ "$RUNNING" = "true" ]; then
            HEALTHY=1
            break
        fi
    fi
    sleep 3
done

if [ $HEALTHY -ne 1 ]; then
    err "tg-monitor 健康检查超时 ($HEALTH_TIMEOUT s),容器状态:"
    docker inspect "$TG_MONITOR_CONTAINER" --format '{{.State.Status}} health={{.State.Health.Status}}' 2>&1 || true
    err "log 最后 20 行:"
    docker logs --tail 20 "$TG_MONITOR_CONTAINER" 2>&1
    err "回滚..."
    git reset --hard "$CURRENT_SHA"
    docker compose -p "$PROJECT_NAME" up -d --no-deps --build --force-recreate "$SERVICE_NAME" 2>&1 | tail -5
    [ -n "$STASH_TAG" ] && git stash pop 2>&1 | tail -2 || true
    exit 1
fi

# ===== 8. 确认 v3.1.8 代码真在容器里 =====
if ! docker exec "$TG_MONITOR_CONTAINER" grep -q "_lookup_central_archive_chat_id" "/app/$TARGET_FILE" 2>/dev/null; then
    err "v3.1.8 代码没进容器(容器内 $TARGET_FILE 不含 _lookup_central_archive_chat_id)"
    err "可能是 docker-compose.yml 的 command 没 cp /app/repo/*.py 到 /app/,看下容器启动 log:"
    docker logs --tail 30 "$TG_MONITOR_CONTAINER" 2>&1
    err "回滚..."
    git reset --hard "$CURRENT_SHA"
    docker compose -p "$PROJECT_NAME" up -d --no-deps --build --force-recreate "$SERVICE_NAME"
    [ -n "$STASH_TAG" ] && git stash pop 2>&1 | tail -2 || true
    exit 1
fi
log "✓ v3.1.8 代码已在容器里生效"

# ===== 9. 验证 caddy / tg-web 没被动 =====
CADDY_AFTER=$(docker ps --filter "name=tg-caddy-" --format "{{.Names}}:{{.Status}}" 2>/dev/null | head -1)
WEB_AFTER=$(docker ps --filter "name=tg-web-" --format "{{.Names}}:{{.Status}}" 2>/dev/null | head -1)
log "升级后 caddy=$CADDY_AFTER"
log "升级后 web=$WEB_AFTER"

# Status 字符串里时间戳会变(Up 7 hours → Up 7 hours),但容器 ID 不变就说明没 recreate
# 简单检查:容器仍 running 且 RunningFor 没变成「秒级」
CADDY_RUNNING_FOR=$(docker ps --filter "name=tg-caddy-" --format "{{.RunningFor}}" 2>/dev/null | head -1)
WEB_RUNNING_FOR=$(docker ps --filter "name=tg-web-" --format "{{.RunningFor}}" 2>/dev/null | head -1)
if echo "$CADDY_RUNNING_FOR" | grep -qE "second|^[0-9]+ seconds"; then
    err "⚠ caddy 容器 RunningFor=$CADDY_RUNNING_FOR — 看起来被 recreate 了(应该不会发生),HTTPS 可能受影响"
fi
if echo "$WEB_RUNNING_FOR" | grep -qE "second|^[0-9]+ seconds"; then
    err "⚠ tg-web 容器 RunningFor=$WEB_RUNNING_FOR — 看起来被 recreate 了"
fi

log "✅ v3.1.8 hotfix 升级完成"
log ""
log "总结:"
log "  - tg-monitor 已 recreate,运行 v3.1.8 代码"
log "  - caddy 仍 RunningFor=$CADDY_RUNNING_FOR (HTTPS 零变动)"
log "  - tg-web 仍 RunningFor=$WEB_RUNNING_FOR (web 后台零变动)"
log "  - .env / data / sessions / Caddyfile / conf.d/ 完全没动"
log ""
log "回滚命令(如果发现问题):"
log "  cd $INSTALL_DIR"
log "  git reset --hard \$(cat $LAST_HOTFIX_FILE)"
log "  docker compose -p $PROJECT_NAME up -d --no-deps --force-recreate $SERVICE_NAME"
