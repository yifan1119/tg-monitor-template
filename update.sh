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

# ===== 0. Caddyfile 异地 IP site block 清理(v3.1.2:提前到最前面,
#         避免 「无需升级」直接 exit 时漏跑 self-heal)=====
# v3.1.2.1 P0-2 fix:加 command -v python3 检查 — 之前硬依赖 python3,
# 客户 VPS 没 python3 (alpine / 极简 ubuntu) 时升级会在 git fetch 前断。
# 现在没 python3 → 打 warning 跳过 self-heal,客户照样能升级。
#
# 历史 bug:f300f64 那次 git add -A 把 demo VPS 的 multi.187.77.157.220.nip.io
# 误 commit 进 git。客户 git pull 拉到 → Caddy 反复试给非本机 IP 签证书 → 卡死。
# 即使 sha 相等(代码无需更新),也要扫一遍清掉历史脏 block。
if [ -f Caddyfile ]; then
    MY_IP_PRECHECK=$(grep "^VPS_PUBLIC_IP=" .env 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'")
    [ -z "$MY_IP_PRECHECK" ] && MY_IP_PRECHECK=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    if [ -n "$MY_IP_PRECHECK" ]; then
        BAD_HOSTS=$(grep -oE '\b[a-zA-Z0-9-]+\.[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\.nip\.io\b' Caddyfile 2>/dev/null \
            | grep -vF ".${MY_IP_PRECHECK}.nip.io" | sort -u || true)
        if [ -n "$BAD_HOSTS" ]; then
            echo "⚠ 检测到 Caddyfile 含异地 IP site block (历史 commit 遗留)"
            echo "$BAD_HOSTS" | sed 's/^/    /'
            # v3.1.2.1 P0-2 round 2:双重检测 python3 — `command -v` 只防"没装",不防
            # "装了但崩"(Codex 提醒)。再加 `python3 -c 'pass'` 验证真能跑。
            # 配合 `|| true` + `set +e/-e` 包住,避免 set -e 在 python3 内部 fail 时
            # 整段 exit。
            PYTHON_OK=0
            if command -v python3 >/dev/null 2>&1; then
                set +e
                python3 -c "pass" >/dev/null 2>&1 && PYTHON_OK=1
                set -e
            fi
            if [ "$PYTHON_OK" = "1" ]; then
                cp Caddyfile Caddyfile.bak.$(date +%s) 2>/dev/null
                set +e
                python3 - <<PYEOF > /tmp/Caddyfile.cleaned 2>/dev/null
import re
content = open("Caddyfile").read()
bad_hosts = """$BAD_HOSTS""".strip().splitlines()
for host in bad_hosts:
    if not host: continue
    content = re.sub(r'# === [^\n]*===\n' + re.escape(host) + r'\s*\{[^}]*\}\n# === end [^\n]*===\n?', '', content, flags=re.DOTALL)
    content = re.sub(re.escape(host) + r'\s*\{[^}]*\}\n?', '', content, flags=re.DOTALL)
print(content, end='')
PYEOF
                set -e
                if [ -s /tmp/Caddyfile.cleaned ]; then
                    cat /tmp/Caddyfile.cleaned > Caddyfile  # > 保 inode
                    echo "  ✅ 已清理 — restart caddy 让 inode 重 attach"
                    # 找到对应 caddy 容器 restart(自家或共享)
                    for cn in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^tg-caddy-'); do
                        if docker exec "$cn" grep -qF "$(basename $(pwd))" /etc/caddy/Caddyfile 2>/dev/null \
                           || [ "$cn" = "tg-caddy-${COMPANY_NAME}" ]; then
                            docker restart "$cn" >/dev/null 2>&1 && echo "  ✅ restarted $cn"
                            break
                        fi
                    done
                else
                    echo "  ⚠ Caddyfile 清理失败 (python3 输出为空) — 跳过 self-heal,不阻塞升级"
                fi
            else
                echo "  ⚠ python3 不可用 — 跳过 Caddyfile self-heal,不阻塞升级"
                echo "     (异地 IP block 仍在 Caddyfile,升级后请手动 vim 删除然后 docker restart tg-caddy-${COMPANY_NAME})"
            fi
        fi
    fi
fi

# ===== 1. 先 fetch 比对远端,已是最新直接退出(不动本地) =====
echo "📥 检查远端版本..."
# v3.0.1: --tags 一起 fetch,让驾驶舱能显示「v3.0.0」这种 tag 名字而不是 raw SHA
# (dashboard_api.code_version 读 .git/refs/tags/ 找 tag matching HEAD SHA)
# v3.1.3.2: 加 --force + 容错。`git fetch --tags` 默认遇到本地 tag 跟远端不一致(retag 场景)
# 会拒绝 overwrite 并 exit 1 → set -e 整段退出 → fanout 升级全失败(已踩过这坑)。
# 加 `--force` 强制 overwrite,加 `|| true` 防本地 git 行为差异(alpine 的 musl git 行为略不同)。
git fetch origin --tags --force || true
OLD_SHA=$(git rev-parse HEAD)
OLD_SHORT=$(git rev-parse --short HEAD)

# v3.1.2.1 P0-3 fix:agent 用 git checkout --detach <tag> 切到 tag 后调 update.sh,
# 这种情况 HEAD 是 detached 不在 main branch。老逻辑会强制 checkout main + reset --hard
# origin/main → 把刚才切到的 tag 拉回 main,tag 升级失效。
#
# 探测 detached HEAD:`git symbolic-ref -q HEAD` 在 detached 时返回非 0。
# detached → 设 SKIP_GIT_REMOTE_SYNC=1,跳过远端比对 + git reset,只走容器重建。
SKIP_GIT_REMOTE_SYNC=""
if ! git symbolic-ref -q HEAD >/dev/null 2>&1; then
    DETACHED_TAG=$(git describe --tags --exact-match HEAD 2>/dev/null || echo "")
    if [ -n "$DETACHED_TAG" ]; then
        echo "  📌 当前 HEAD detached 在 tag ${DETACHED_TAG} (agent 升级到指定 tag)"
        echo "     跳过 git remote sync + 强制容器重建(tag checkout 已改所有 host 文件,"
        echo "     web.py-only NEED_REBUILD 检测不够覆盖)"
        SKIP_GIT_REMOTE_SYNC=1
        NEW_SHA="$OLD_SHA"
        NEW_SHORT="$OLD_SHORT"
        echo "$OLD_SHA" > .last_commit
        SKIP_CODE_PULL=1   # 跳过老的 git reset 路径(下面 stash + checkout main + reset --hard 都跳)
    fi
fi

# v3.1.2.1 P0-3 round 3:detached HEAD on tag 路径下完全跳过「OLD_SHA = REMOTE_SHA」
# 检查 + 「已是最新版 exit 0」分支。Codex round 3 抓出:之前把 REMOTE_SHA = OLD_SHA
# 让脚本进入"已是最新版"分支,NEED_REBUILD 只 diff web.py,如果 tag 升级只改了
# agent.py / 模板 / 文案 → web.py 一样 → exit 0 静默不重建,升级失效。
#
# detached 路径 = SKIP_GIT_REMOTE_SYNC=1 + SKIP_CODE_PULL=1 → 直接 fall through 到
# 容器重建步骤(本文件后面的 docker compose up --build),不再做 sha 对比。
if [ -z "$SKIP_GIT_REMOTE_SYNC" ]; then
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

        # v3.0.29: 即使 sha 相等 + 容器都在,也要 sanity check「容器内 image 是不是基于当前
        # git 的内容 build 的」。检测方法:容器内 /app/web.py 跟 host /app/repo/web.py 不一致
        # → 说明 docker compose up 没真 rebuild image,跳过 exit 走 force rebuild。
        # 修「sha 相等但 image 没 rebuild」根因 — 之前的 v3.0.13/14/15/26/27/28 升级失败
        # 大多卡在这里。
        NEED_REBUILD=0
        if [ -z "$MISSING" ]; then
            for c in "tg-web-${COMPANY_NAME}"; do
                if ! docker exec "$c" diff /app/web.py /app/repo/web.py >/dev/null 2>&1; then
                    NEED_REBUILD=1
                    echo "  ⚠ 容器 ${c} 内 web.py 跟 host 不一致 → 需要 rebuild image"
                    break
                fi
            done
        fi

        if [ -z "$MISSING" ] && [ "$NEED_REBUILD" = "0" ]; then
            echo ""
            echo "ℹ 当前已是最新版 (${OLD_SHORT}),容器内代码也已同步,无需升级"
            exit 0
        elif [ -z "$MISSING" ]; then
            # NEED_REBUILD=1:sha 相等但容器代码旧
            echo ""
            echo "ℹ 代码最新 (${OLD_SHORT}) 但容器内代码旧,触发 image 重建..."
            NEW_SHA="$OLD_SHA"
            NEW_SHORT="$OLD_SHORT"
            echo "$OLD_SHA" > .last_commit
            SKIP_CODE_PULL=1
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
    # v3.0.28: 修「git 卡 feature 分支」根因 — 之前部分客户 git working tree 卡在某个
    # feature 分支上,git pull 拉的是那个分支不是 main → README/templates 永远不更新。
    # 强制切回 main 再 reset,保证拿到的是 origin/main 的代码。
    CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "")
    if [ "$CURRENT_BRANCH" != "main" ]; then
        echo "  ⚠ 当前分支 '${CURRENT_BRANCH}' 不是 main,强制切回 main"
        git checkout main 2>&1 | sed 's/^/    /' || git checkout -B main origin/main
    fi
    git reset --hard origin/main
    NEW_SHA=$(git rev-parse HEAD)
    NEW_SHORT=$(git rev-parse --short HEAD)
    echo "  ✅ 代码已同步到 ${NEW_SHORT} (branch=main)"
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

#   v3.0.16 + v3.0.28: CENTRAL_PUSH_URL + CENTRAL_PUSH_TOKEN (实时预警走中央台路由)
#   旧 VPS 升级时自动接入,客户/IT 不用 SSH 改
#   v3.0.28 修「空值卡住」根因:之前用 `grep -q "^CENTRAL_PUSH_URL="`,只要这行存在(哪怕是空值
#   `CENTRAL_PUSH_URL=`)就不覆盖,导致部分 VPS 永远接不上中央台。改用 `grep -qE ".+$"` 判断
#   等号后至少 1 字符,空值也强制覆盖。
DEFAULT_CENTRAL_PUSH_URL="https://tg.13-193-143-29.nip.io/api/v1/push_alert"
DEFAULT_CENTRAL_PUSH_TOKEN="d282d167d178d292e1098027ce911b23df13e6f0305f061bc6fa023bd3abd2d7"
if [ -f ".env" ] && ! grep -qE "^CENTRAL_PUSH_URL=.+$" .env; then
    # 删掉可能存在的空值行(防止再追加导致两行重复)
    sed -i.bak '/^CENTRAL_PUSH_URL=$/d; /^CENTRAL_PUSH_TOKEN=$/d' .env 2>/dev/null
    rm -f .env.bak
    [ -n "$(tail -c 1 .env)" ] && echo "" >> .env
    echo "" >> .env
    echo "# v3.0.16+v3.0.28: 实时预警走中央台路由(改 company → 自动推对应公司+中心 bot 群)" >> .env
    echo "CENTRAL_PUSH_URL=${DEFAULT_CENTRAL_PUSH_URL}" >> .env
    echo "CENTRAL_PUSH_TOKEN=${DEFAULT_CENTRAL_PUSH_TOKEN}" >> .env
    echo "  ✅ 已接入中央台路由 — 改 company 后实时预警自动推到新公司群"
fi
# CENTRAL_PUSH_TOKEN 单独 check(防止只有 URL 有值 / TOKEN 空)
if [ -f ".env" ] && ! grep -qE "^CENTRAL_PUSH_TOKEN=.+$" .env; then
    sed -i.bak '/^CENTRAL_PUSH_TOKEN=$/d' .env 2>/dev/null
    rm -f .env.bak
    [ -n "$(tail -c 1 .env)" ] && echo "" >> .env
    echo "CENTRAL_PUSH_TOKEN=${DEFAULT_CENTRAL_PUSH_TOKEN}" >> .env
    echo "  ✅ 已补 CENTRAL_PUSH_TOKEN"
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
# v3.0.29: 加 --force-recreate 确保 image rebuild 后容器也 recreate
# (之前的 bug:image 改了但 docker compose up 看容器 running 没 recreate,
# 导致老容器仍用老 image)
docker compose -p "tg-${COMPANY_NAME}" up -d --build --force-recreate

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

# ===== 5.2 v3.0.24: templates 同步性 self-check + 兜底 docker cp =====
# 背景:`docker compose up -d --build` 在 docker-compose.yml 跟 Dockerfile 都没变时
#       不重建容器,容器 command 的 cp templates 不重跑 → 客户跑完 update.sh 但
#       UI 还是老 modal(v3.0.15+ 的「编辑账号配置」5 字段不出现)。
# 修法:升级后 docker exec grep 容器里 templates 关键 string,匹配不到就主动
#       docker cp host 文件 → 强制 restart 容器,确保新 UI 上线。
echo ""
echo "🔍 v3.0.24 self-check: 验证 templates 已同步进容器..."
WEB_CONTAINER="tg-web-${COMPANY_NAME}"
MONITOR_CONTAINER="tg-monitor-${COMPANY_NAME}"
NEEDS_FORCE_CP=0
# 用 v3.0.15+ 才有的字段名 + v3.0.23 才有的 reLogin function 双保险
for KEYWORD in "nc_inspector_tg_id" "function reLogin" "🔗 打开后台"; do
    if ! docker exec "$WEB_CONTAINER" grep -q -F -- "$KEYWORD" /app/templates/index.html /app/templates/audit.html 2>/dev/null; then
        : # 这个 keyword 没找到不一定是 bug(audit.html 是 v3.0.18+),只检测 index.html
    fi
done
# 主验证:index.html 必须含 v3.0.15 字段(老客户没升级会缺)
if ! docker exec "$WEB_CONTAINER" grep -q "nc_inspector_tg_id" /app/templates/index.html 2>/dev/null; then
    NEEDS_FORCE_CP=1
    echo "  ⚠ 容器内 index.html 没含 v3.0.15 监察员字段 — templates 没同步"
fi
if [ $NEEDS_FORCE_CP -eq 1 ]; then
    echo "  🔧 主动 docker cp host templates 强制同步..."
    docker cp templates/. "${WEB_CONTAINER}:/app/templates/" 2>&1 | tail -3
    docker cp README.md "${WEB_CONTAINER}:/app/README.md" 2>/dev/null || true
    docker cp release_notes.json "${WEB_CONTAINER}:/app/release_notes.json" 2>/dev/null || true
    echo "  🔄 重启 web 容器让 Flask 重读 templates..."
    docker restart "$WEB_CONTAINER" >/dev/null 2>&1 || true
    sleep 5
    if docker exec "$WEB_CONTAINER" grep -q "nc_inspector_tg_id" /app/templates/index.html 2>/dev/null; then
        echo "  ✅ templates 强制同步成功"
    else
        echo "  ❌ 强制同步仍失败 — 请手动 docker compose down && docker compose up -d --build"
    fi
else
    echo "  ✅ templates 已同步(包含 v3.0.15+ 字段)"
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

# ===== 5.5 Caddyfile 异地 IP site block 清理 (v3.1.1) =====
# 背景:历史上 git 仓库 Caddyfile 末尾误 commit 了 demo VPS 的 multi.187.77.157.220.nip.io
#       site block(`f300f64` 那次 git add -A 副作用)。客户 git pull 拉到这一行,
#       Caddy 试给这个不属于自己 IP 的域名签 Let's Encrypt → 验证失败 → 整张 caddy
#       TLS 卡死,自己合法域名也续不下来。
# 自愈:扫 Caddyfile 每个 nip.io site block,IP 不是本机 IP 的 block 整段删,用
#       cat > redirect 保 inode(避免 bind-mount inode 断)。
if [ -f Caddyfile ]; then
    MY_IP=$(grep "^VPS_PUBLIC_IP=" .env 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'")
    if [ -z "$MY_IP" ]; then
        MY_IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    fi
    if [ -n "$MY_IP" ]; then
        # 扫 Caddyfile 找含 nip.io 但 IP 跟本机不一致的 host
        BAD_HOSTS=$(grep -oE '\b[a-zA-Z0-9-]+\.[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\.nip\.io\b' Caddyfile 2>/dev/null \
            | grep -vF ".${MY_IP}.nip.io" | sort -u || true)
        if [ -n "$BAD_HOSTS" ]; then
            echo ""
            echo "⚠ 发现 Caddyfile 含**不属于本机 IP** 的 nip.io site block (历史 commit 遗留 bug):"
            echo "$BAD_HOSTS" | sed 's/^/    /'
            # v3.1.2.1 P0-2 round 2:同顶部段,双重检测 python3
            PYTHON_OK_2=0
            if command -v python3 >/dev/null 2>&1; then
                set +e
                python3 -c "pass" >/dev/null 2>&1 && PYTHON_OK_2=1
                set -e
            fi
            if [ "$PYTHON_OK_2" = "1" ]; then
                echo "  → 自动清理(防 Caddy 续证书卡死)..."
                cp Caddyfile Caddyfile.bak.$(date +%s)
                set +e
                python3 - <<PYEOF > /tmp/Caddyfile.cleaned
import re, sys
content = open("Caddyfile").read()
bad_hosts = """$BAD_HOSTS""".strip().splitlines()
for host in bad_hosts:
    if not host: continue
    # 删 "# === xxx ===\n<host> { ... }\n# === end xxx ===" 整段
    content = re.sub(
        r'# === [^\n]*===\n' + re.escape(host) + r'\s*\{[^}]*\}\n# === end [^\n]*===\n?',
        '', content, flags=re.DOTALL)
    # fallback: 也删 host { ... } 单独 block(没注释包围的)
    content = re.sub(
        re.escape(host) + r'\s*\{[^}]*\}\n?',
        '', content, flags=re.DOTALL)
print(content, end='')
PYEOF
                set -e
                if [ -s /tmp/Caddyfile.cleaned ]; then
                    cat /tmp/Caddyfile.cleaned > Caddyfile   # > redirect 保 inode
                    echo "  ✅ 已清理"
                else
                    echo "  ⚠ 清理失败 (python3 输出为空),跳过不阻塞升级"
                fi
            else
                echo "  ⚠ python3 不可用 — 跳过 Caddyfile self-heal,不阻塞升级"
                echo "     (升级后请手动清理: vim Caddyfile 删除异地 host 段 + docker restart tg-caddy-${COMPANY_NAME})"
            fi
        fi
    fi
fi

# ===== 5.5b Caddyfile upstream 显式化 (v3.1.3.2) =====
# 背景:历史 Caddyfile 模板第一个 site block 用 {$WEB_UPSTREAM:web:5001} 模糊别名。
#       一台 VPS 只跑一部门时没事,共用 Caddy 给同 VPS 第二个部门反代时(运维
#       手动 docker network connect 把 Caddy 拉进第二个 dept 的 network),每个
#       network 里都有名为 "web" 的容器 → Docker DNS 撞车 → 部门 A 子域被路由到
#       部门 B 的后端(2026-05-13 线上复现,客户截图同 IP 不同子域显示相同内容)。
#       详见 ADR-0044。
# 自愈:两个替换 — 新部署模板的 __COMPANY_NAME__ 占位符 + 老部署的裸 web:5001。
#       全部替换成显式 tg-web-<部门>:5001,跟 enable_https.sh 加的子部门 site block
#       写法一致,跨 network 不撞 DNS。
#       enable_https.sh 加的 site block 本来就显式(tg-web-XXX:5001 含 "web-XXX:5001"
#       不含 "web:5001" 子串),不会被误伤。
if [ -f Caddyfile ]; then
    NEED_UPSTREAM_REWRITE=0
    if grep -q "__COMPANY_NAME__" Caddyfile 2>/dev/null; then
        NEED_UPSTREAM_REWRITE=1
    fi
    # 用 grep 子串"web:5001"判断是否要改 — 已显式的 tg-web-XXX:5001 含子串
    # "web-XXX:5001" 不含 "web:5001"(部门名隔开),所以不会误判已显式行。
    if grep -q "web:5001" Caddyfile 2>/dev/null; then
        NEED_UPSTREAM_REWRITE=1
    fi
    if [ "$NEED_UPSTREAM_REWRITE" = "1" ]; then
        echo ""
        echo "🔧 Caddyfile upstream 显式化(防共用 Caddy DNS 撞车,v3.1.3.2)..."
        # v3.1.3.3 P1 fix: cp 失败(罕见盘满)加 || true 防 set -e 整段挂
        cp Caddyfile Caddyfile.bak.upstream.$(date +%s) 2>/dev/null || true
        # 两个替换合并到一次 sed,顺序无关:
        # 1) __COMPANY_NAME__ → 实际部门名(新模板占位符)
        # 2) 限定 reverse_proxy 行内 web:5001 → tg-web-<部门>:5001。
        #    BSD/GNU sed 都兼容,不用 \b 或捕获组。已显式 tg-web-XXX:5001 行内含子串
        #    "web-XXX:5001" 不含 "web:5001",sed 不会误替换。
        sed -e "s/__COMPANY_NAME__/${COMPANY_NAME}/g" \
            -e "/reverse_proxy/s|web:5001|tg-web-${COMPANY_NAME}:5001|g" \
            Caddyfile > /tmp/Caddyfile.upstream
        if [ -s /tmp/Caddyfile.upstream ]; then
            cat /tmp/Caddyfile.upstream > Caddyfile   # > redirect 保 inode
            rm -f /tmp/Caddyfile.upstream
            echo "  ✅ upstream 已绑定到 tg-web-${COMPANY_NAME}:5001"

            # ===== v3.1.3.3 P0 fix:显式 reload Caddy 让新 upstream 立刻生效 =====
            # 修 v3.1.3.2 设计漏洞:之前以为 section 5.6 inode 自愈会兜底 restart,
            # 但 5.6 比 host vs container 文件 size,而 cat > redirect 保 inode
            # → size 始终一致 → 5.6 永不触发 → Caddy 进程内存仍是老 routes
            # → DNS 撞车 bug 没修。
            #
            # reload exit code 显式判定,不用 `|| true` 吞错(Codex P0 fix:防撒谎日志)。
            MY_CADDY_5_5B=""
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "tg-caddy-${COMPANY_NAME}"; then
                MY_CADDY_5_5B="tg-caddy-${COMPANY_NAME}"
            else
                MY_DOMAIN_5_5B=$(grep "^PUBLIC_DOMAIN=" .env 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'")
                if [ -n "$MY_DOMAIN_5_5B" ]; then
                    for caddy in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^tg-caddy-' || true); do
                        if docker exec "$caddy" grep -qF "$MY_DOMAIN_5_5B" /etc/caddy/Caddyfile 2>/dev/null; then
                            MY_CADDY_5_5B="$caddy"
                            break
                        fi
                    done
                fi
            fi
            if [ -n "$MY_CADDY_5_5B" ]; then
                # 捕获 stdout/stderr 跟 exit code 分开判定 — 不吞错不撒谎日志
                set +e
                reload_out_5_5b=$(docker exec "$MY_CADDY_5_5B" caddy reload --config /etc/caddy/Caddyfile 2>&1)
                reload_rc_5_5b=$?
                set -e
                if [ "$reload_rc_5_5b" = "0" ]; then
                    echo "  ✅ Caddy 已 reload (${MY_CADDY_5_5B})"
                else
                    echo "  ⚠ Caddy reload 失败 exit=${reload_rc_5_5b}(${MY_CADDY_5_5B}):"
                    echo "$reload_out_5_5b" | tail -5 | sed 's/^/      /'
                    echo "  ⚠ Caddyfile host 端已更新但 Caddy 进程仍跑老配置,请人工排查"
                fi
            else
                # 部门没自家 Caddy 也没识别到共享 Caddy → 本次 5.5b 改写不会即时生效
                # 这种部门通常是「裸 HTTP 部署」或 v3.0.1 之前老部署,不影响升级流程
                echo "  ⚠ 未识别到本部门相关的 Caddy 容器,本次 reload 跳过"
                echo "     (Caddyfile host 端已更新,下次 Caddy 重启或手动 reload 会生效)"
            fi
        else
            echo "  ⚠ sed 输出为空,跳过(已备份 Caddyfile.bak.upstream.*)"
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
