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
# v3.0.1: --tags 一起 fetch,让驾驶舱能显示「v3.0.0」这种 tag 名字而不是 raw SHA
# (dashboard_api.code_version 读 .git/refs/tags/ 找 tag matching HEAD SHA)
git fetch origin --tags
OLD_SHA=$(git rev-parse HEAD)
OLD_SHORT=$(git rev-parse --short HEAD)
REMOTE_SHA=$(git rev-parse origin/main)
REMOTE_SHORT=$(git rev-parse --short origin/main)

if [ "$OLD_SHA" = "$REMOTE_SHA" ]; then
    # v2.10.24: 即使代码是最新,也要检查容器是否缺失。
    # 如果 tg-monitor / tg-web 任一容器不存在(可能被 docker rm 清过),跳过 pull 但继续重建。
    # 避免客户误删容器后跑 update.sh 被「已是最新版」误导不知道下一步。
    MISSING=""
    for c in "tg-monitor-${COMPANY_NAME}" "tg-web-${COMPANY_NAME}"; do
        if ! docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${c}$"; then
            MISSING="${MISSING} ${c}"
        fi
    done

    if [ -z "$MISSING" ]; then
        echo ""
        echo "ℹ 当前已是最新版 (${OLD_SHORT}),所有容器存在,无需升级"
        echo "  如需强制重建容器: docker compose -p tg-${COMPANY_NAME} up -d --build"
        exit 0
    else
        echo ""
        echo "ℹ 当前代码已是最新 (${OLD_SHORT}),但检测到容器缺失:${MISSING}"
        echo "  跳过 git pull,继续重建容器..."
        NEW_SHA="$OLD_SHA"
        NEW_SHORT="$OLD_SHORT"
        echo "$OLD_SHA" > .last_commit
        # 跳到重建步骤,不走 git stash / git reset 流程
        SKIP_CODE_PULL=1
    fi
fi

# v2.10.24: 如果是「代码最新但容器缺失」走过来的,跳过 stash / git reset
if [ -z "$SKIP_CODE_PULL" ]; then
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
fi

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
# v2.10.24: 放宽 orphan cleanup 逻辑,解决「label 匹配但容器异常」场景下还是撞
#   "container name already in use" 的问题(客户反馈)。
#   - 不再检查 compose project label:tg-(monitor|web)-<部门> 这两个名字本来就是
#     当前部门独占的,见到同名容器无条件清。(跟 install.sh v2.10.20+ 对齐)
#   - 不清 tg-caddy-<部门>:Caddy 是 profile 服务,v2.10.22 末端的 HTTPS 保护
#     块需要它"存在"才能检测 + 拉起;清了反而破坏 HTTPS 自恢复机制。
ORPHANS=$(docker ps -a --format '{{.Names}}' 2>/dev/null \
    | grep -E "^tg-(monitor|web)-${COMPANY_NAME}$" || true)
if [ -n "$ORPHANS" ]; then
    for c in $ORPHANS; do
        proj=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$c" 2>/dev/null || echo "")
        echo "  🧹 清理同名容器: $c (compose project=\"$proj\")"
        docker rm -f "$c" >/dev/null 2>&1 || true
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

# ===== 5.5 HTTPS 保护 (v2.10.22) — Caddy 挂了就拉起来 =====
# 背景: docker-compose.yml 里 caddy 挂了 profiles: ["https"],
#       `compose up -d --build` 不带 --profile 不会动它 → 正常运行的 Caddy 不受影响,
#       但是如果历史上有人跑过 `docker compose down`(例如 debug 时),
#       Caddy 就永远起不来了,update.sh 也不会救它 → 客户发现 HTTPS 打不开。
# 策略: 看到 tg-caddy-<部门> 容器存在但不在跑,才主动拉 (profile https up -d caddy)。
#       正在跑的不碰 (避免没必要的 recreate 导致 HTTPS 瞬断)。
#       失败只打 warning,不让 set -e 把整个升级标成失败。
CADDY_NAME="tg-caddy-${COMPANY_NAME}"
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CADDY_NAME}$"; then
    CADDY_STATE=$(docker inspect -f '{{.State.Running}}' "$CADDY_NAME" 2>/dev/null || echo "false")
    if [ "$CADDY_STATE" != "true" ]; then
        echo ""
        echo "🔒 检测到 HTTPS(Caddy)容器未在运行,尝试恢复..."
        if docker compose -p "tg-${COMPANY_NAME}" --profile https up -d caddy 2>&1; then
            echo "  ✅ Caddy 已恢复"
        else
            echo "  ⚠ Caddy 拉起失败 — 不影响主服务"
            echo "     手动恢复: docker compose -p tg-${COMPANY_NAME} --profile https up -d caddy"
            echo "     查 Caddy 日志: docker logs tg-caddy-${COMPANY_NAME} --tail 50"
        fi
    fi
fi

# ===== 5.6 Caddy inode 自愈 (v3.0.2) =====
# 背景: docker file bind mount (./Caddyfile:/etc/caddy/Caddyfile:ro) 按 inode 绑定。
#       历史上有人用 sed -i / cp / vim 原子替换过 Caddyfile → 新 inode →
#       容器 mount 仍指旧 inode → 容器里永远看老 Caddyfile → 新追加的 site block
#       永不生效 → 新部门 HTTPS 打不开。
# 安全策略: 只碰"跟当前部门直接相关"的 Caddy,不动 VPS 上其他项目的容器。
#   1. 本部门有自己的 Caddy (tg-caddy-${COMPANY_NAME}) → 检查它
#   2. 本部门用 shared Caddy (own 没有但 .env 里有 PUBLIC_DOMAIN) → 找 Caddyfile
#      里包含本部门 PUBLIC_DOMAIN 的 tg-caddy-* 容器 → 只检查这个
#   其他 Caddy 容器一概不动,保证不搞坏客户 VPS 上的其他服务。
MY_CADDY=""

# 情况 1: 本部门有自己的 Caddy
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "tg-caddy-${COMPANY_NAME}"; then
    MY_CADDY="tg-caddy-${COMPANY_NAME}"
else
    # 情况 2: shared mode — 本部门的 PUBLIC_DOMAIN 被其他 Caddy 反代
    MY_DOMAIN=$(grep "^PUBLIC_DOMAIN=" .env 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'")
    if [ -n "$MY_DOMAIN" ]; then
        # 扫所有 tg-caddy-* 容器,看哪个 Caddyfile 里有我们的 domain
        for caddy in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^tg-caddy-' || true); do
            if docker exec "$caddy" grep -qF "$MY_DOMAIN" /etc/caddy/Caddyfile 2>/dev/null; then
                MY_CADDY="$caddy"
                break
            fi
        done
    fi
fi

# 只对本部门相关的那一个 Caddy 做 inode 检查 + 自愈
if [ -n "$MY_CADDY" ]; then
    host_file=$(docker inspect "$MY_CADDY" \
        --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' \
        2>/dev/null)
    if [ -n "$host_file" ] && [ -f "$host_file" ]; then
        host_size=$(wc -c < "$host_file" 2>/dev/null | tr -d ' ')
        cont_size=$(docker exec "$MY_CADDY" wc -c /etc/caddy/Caddyfile 2>/dev/null | awk '{print $1}')
        if [ -n "$host_size" ] && [ -n "$cont_size" ] && [ "$host_size" != "$cont_size" ]; then
            echo ""
            echo "🔧 检测到本部门使用的 Caddy (${MY_CADDY}) Caddyfile 跟 host 不一致"
            echo "   (host=${host_size}B vs 容器=${cont_size}B,docker bind mount inode 断裂)"
            echo "   自动重启 ${MY_CADDY} 修复 (约 5-10 秒 HTTPS 短暂中断)..."
            docker restart "$MY_CADDY" >/dev/null 2>&1 || true
            sleep 3
            cont_size2=$(docker exec "$MY_CADDY" wc -c /etc/caddy/Caddyfile 2>/dev/null | awk '{print $1}')
            if [ "$host_size" = "$cont_size2" ]; then
                echo "   ✅ ${MY_CADDY} 已修复"
            else
                echo "   ⚠ 重启后仍不一致,请手动排查:"
                echo "     bash ${INSTALL_DIR}/scripts/caddy-doctor.sh"
            fi
        fi
    fi
fi

# ===== 5b. v3.0.13: shared Caddy 模式下,确保 web 容器跟 Caddy 在同一 docker network =====
# 背景: docker compose up -d 可能 recreate tg-web 容器,
#       enable_https.sh 之前加的 docker network connect 会随旧容器一起丢。
#       新 web 容器跟 shared Caddy 不在同一 network → Caddy DNS 解析容器名失败 → 502。
# 自愈: 检测后自动 docker network connect + caddy reload。
if [ -n "$MY_CADDY" ] && [ "$MY_CADDY" != "tg-caddy-${COMPANY_NAME}" ]; then
    # 仅 shared 外部 Caddy 模式才需要(自建 Caddy 走 docker compose default network 不会断)
    WEB_CONTAINER="tg-web-${COMPANY_NAME}"
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$WEB_CONTAINER"; then
        CADDY_NETS=$(docker inspect "$MY_CADDY" \
            --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)
        WEB_NETS=$(docker inspect "$WEB_CONTAINER" \
            --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)
        SHARED=""
        for net in $CADDY_NETS; do
            [ "$net" = "bridge" ] && continue
            if echo " $WEB_NETS " | grep -q " $net "; then
                SHARED="$net"
                break
            fi
        done
        if [ -z "$SHARED" ]; then
            # 优先选 web 默认 network 把 Caddy 接进去(语义更对 — 让 Caddy 进部门 net)
            WEB_PRIMARY=$(echo "$WEB_NETS" | awk '{print $1}')
            if [ -n "$WEB_PRIMARY" ] && [ "$WEB_PRIMARY" != "bridge" ]; then
                echo ""
                echo "🔧 检测到 ${WEB_CONTAINER} 跟 ${MY_CADDY} 不在同一 docker network"
                echo "   (升级 recreate 容器后断开,会导致 502)"
                echo "   自动把 ${MY_CADDY} 接入 ${WEB_PRIMARY}..."
                docker network connect "$WEB_PRIMARY" "$MY_CADDY" 2>&1 \
                    | grep -v "already exists" | grep -v "^$" || true
                docker exec "$MY_CADDY" caddy reload --config /etc/caddy/Caddyfile 2>&1 \
                    | grep -iE 'error|fail' || echo "   ✅ Caddy 已 reload"
            fi
        fi
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
