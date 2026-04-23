#!/usr/bin/env bash
# 一键启用 HTTPS(给 Google OAuth 用)
# 用法:./enable_https.sh [domain]
#   不带参数 → 自动用 nip.io 把 IP 转成域名
#   带参数   → 用你自己的域名(必须先把 DNS A 记录指到本机)
#
# 智能行为:
#   - 80/443 空闲 → 启动自建 Caddy 容器,自动拿 Let's Encrypt 证书
#   - 80/443 被「现成 Caddy 容器」占 → 切「外部反代模式」:
#       · 不启动自建 Caddy
#       · tg-web 自动加入现成 Caddy 的 Docker network
#       · 自动追加 site block 到现成 Caddyfile + docker exec reload
#   - 80/443 被非 Caddy 程序占(nginx/apache/systemd-native-caddy 等)
#       → 打印操作说明,手动处理
set -e

cd "$(dirname "$0")"

# 读 .env
COMPANY=$(grep -E "^COMPANY_NAME=" .env 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" || echo "default")
COMPANY="${COMPANY:-default}"
PROJECT="tg-${COMPANY}"
WEB_PORT=$(grep "^WEB_PORT=" .env 2>/dev/null | cut -d= -f2 | head -1)
WEB_PORT="${WEB_PORT:-5001}"
WEB_CONTAINER="tg-web-${COMPANY}"

# 1. 决定域名
# v2.10.13: 支持同 VPS 多部门 HTTPS 共存 — 新部门用 <company>.<IP>.nip.io 子域
#   向后兼容: .env 已有 PUBLIC_DOMAIN 的老部门继续沿用,不破坏 OAuth redirect URI
if [ -n "$1" ]; then
    DOMAIN="$1"
    echo "▸ 使用自定义域名: $DOMAIN"
    echo "  ⚠ 请确认 $DOMAIN 的 DNS A 记录已指向本机 IP"
elif grep -q "^PUBLIC_DOMAIN=" .env 2>/dev/null; then
    # 老部门已有 domain → 沿用,避免改动 OAuth 配置
    DOMAIN=$(grep "^PUBLIC_DOMAIN=" .env | head -1 | cut -d= -f2 | tr -d '"' | tr -d "'")
    echo "▸ 沿用 .env 已有 domain: $DOMAIN"
else
    IP=$(curl -s4 --max-time 5 ifconfig.me 2>/dev/null || curl -s4 --max-time 5 ipinfo.io/ip 2>/dev/null)
    if [ -z "$IP" ]; then
        echo "✗ 无法自动获取公网 IP,请手动指定:./enable_https.sh your-domain.com"
        exit 1
    fi
    # v2.10.13: 子域区分不同部门 — 避免多部门同 domain 导致 Caddy site block 冲突
    # nip.io wildcard 解析: 任意前缀.IP.nip.io 都解析到 IP
    DOMAIN="${COMPANY}.${IP}.nip.io"
    echo "▸ 自动子域: $DOMAIN  (解析到 $IP)"
fi

# 2. 写入/更新 .env
if grep -q "^PUBLIC_DOMAIN=" .env; then
    sed -i.bak "s|^PUBLIC_DOMAIN=.*|PUBLIC_DOMAIN=$DOMAIN|" .env && rm -f .env.bak
else
    echo "PUBLIC_DOMAIN=$DOMAIN" >> .env
fi
echo "▸ .env 已更新 PUBLIC_DOMAIN=$DOMAIN"

# 3. 检测 80/443 占用情况
echo ""
echo "▸ 检查 80/443 端口占用情况..."
EXTERNAL_CADDY=""
NON_CADDY_OCCUPIER=""

for port in 80 443; do
    # 先看 Docker 容器是否占这个端口
    occupier=$(docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null | \
        awk -F'|' -v p=":${port}->" '$2 ~ p {print $1; exit}')
    if [ -n "$occupier" ]; then
        # 跳过自建 Caddy
        if [ "$occupier" = "tg-caddy-${COMPANY}" ]; then
            continue
        fi
        # 是不是 Caddy 镜像
        image=$(docker inspect "$occupier" --format '{{.Config.Image}}' 2>/dev/null || echo "")
        if echo "$image" | grep -qi caddy; then
            EXTERNAL_CADDY="$occupier"
            echo "  ↳ 端口 $port 被现成 Caddy 容器占用: $occupier ($image)"
            break
        else
            NON_CADDY_OCCUPIER="$occupier ($image)"
            echo "  ↳ 端口 $port 被非 Caddy 容器占用: $NON_CADDY_OCCUPIER"
            break
        fi
    fi
    # 非 Docker 占用(systemd 原生 caddy / nginx / apache 等)
    sys_line=$(ss -tlnp 2>/dev/null | grep ":${port} " | head -1 || true)
    if [ -n "$sys_line" ]; then
        proc=$(echo "$sys_line" | grep -oP 'users:\(\(\K[^,]+' | tr -d '"' || echo "unknown")
        if echo "$proc" | grep -qi caddy; then
            NON_CADDY_OCCUPIER="systemd-native-caddy ($proc)"
            echo "  ↳ 端口 $port 被系统原生 Caddy 占用"
        else
            NON_CADDY_OCCUPIER="$proc"
            echo "  ↳ 端口 $port 被系统进程占用: $proc"
        fi
        break
    fi
done

# 4. 分支:外部 Caddy 模式 vs 非 Caddy 占用 vs 自建 Caddy 模式
if [ -n "$EXTERNAL_CADDY" ]; then
    # ─────────────────────────────────────────
    # 外部 Caddy 模式:接入现成 Caddy 做反代
    # ─────────────────────────────────────────
    echo ""
    echo "▸ 切换「外部反代」模式(复用现成 Caddy,不启动自建 Caddy)"

    # 4a. 确保 tg-web / tg-monitor 已起
    if ! docker ps --format '{{.Names}}' | grep -q "^${WEB_CONTAINER}$"; then
        echo "  起 tg-web / tg-monitor(不含 caddy profile)..."
        docker compose -p "$PROJECT" up -d web tg-monitor
        sleep 3
    fi

    # 4b. 找外部 Caddy 的 Docker network
    EXT_NET=$(docker inspect "$EXTERNAL_CADDY" \
        --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' \
        2>/dev/null | grep -v '^$' | grep -v '^bridge$' | head -1)
    if [ -z "$EXT_NET" ]; then
        EXT_NET=$(docker inspect "$EXTERNAL_CADDY" \
            --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' \
            2>/dev/null | grep -v '^$' | head -1)
    fi

    UPSTREAM=""
    if [ -n "$EXT_NET" ]; then
        echo "  把 $WEB_CONTAINER 接入 Caddy 所在 network: $EXT_NET"
        if docker network connect "$EXT_NET" "$WEB_CONTAINER" 2>&1 | grep -v "already exists" | grep -v "^$" >/dev/null; then
            echo "  (连接结果:见上)"
        fi
        # 确认真连上了
        if docker inspect "$WEB_CONTAINER" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | grep -q "$EXT_NET"; then
            UPSTREAM="${WEB_CONTAINER}:5001"
            echo "  ✓ 反代 upstream 设为 container name: $UPSTREAM"
        fi
    fi

    # 如果没接上 network,fallback 到 host 模式
    if [ -z "$UPSTREAM" ]; then
        HOST_GW=$(ip -4 route | awk '/default/ {print $3; exit}')
        if [ -n "$HOST_GW" ]; then
            UPSTREAM="${HOST_GW}:${WEB_PORT}"
            echo "  fallback 反代 upstream: $UPSTREAM (Docker bridge gateway)"
        else
            UPSTREAM="host.docker.internal:${WEB_PORT}"
            echo "  fallback 反代 upstream: $UPSTREAM"
        fi
    fi

    # 4c. 找外部 Caddyfile 挂载路径
    EXT_CADDYFILE=$(docker inspect "$EXTERNAL_CADDY" \
        --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' \
        2>/dev/null || echo "")

    # 4d. 生成 site block
    SITE_BLOCK=$(cat <<EOF

# === TG Monitor (${COMPANY}) — auto-added by enable_https.sh ===
${DOMAIN} {
    reverse_proxy ${UPSTREAM} {
        header_up Host {host}
        header_up X-Real-IP {remote}
    }
    encode gzip
}
# === end TG Monitor (${COMPANY}) ===
EOF
)

    # v3.0.2: 没找到 bind mount → 明确报错不静默
    if [ -z "$EXT_CADDYFILE" ] || [ ! -f "$EXT_CADDYFILE" ]; then
        echo ""
        echo "  ✗ 找不到外部 Caddy 的 Caddyfile bind mount(可能用 JSON/API 配置,或镜像内置)"
        echo "    请手动把下面这段加到你的 Caddy 配置里,然后 reload:"
        echo ""
        echo "$SITE_BLOCK"
        echo ""
        exit 1
    fi

    # 4e. 去重 + 追加 site block(in-place append,同 inode)
    if grep -q "TG Monitor (${COMPANY})" "$EXT_CADDYFILE"; then
        echo "  ✓ $EXT_CADDYFILE 已包含本部门站点配置,跳过追加"
    else
        echo "  追加 site block 到 $EXT_CADDYFILE"
        # 用 printf + >> 保证 append 不改 inode(避免 docker file bind mount 脱节)
        printf '%s\n' "$SITE_BLOCK" >> "$EXT_CADDYFILE"

        # 校验: 追加后 host 文件确实含新 block
        if ! grep -q "TG Monitor (${COMPANY})" "$EXT_CADDYFILE"; then
            echo "  ✗ 追加后校验失败,host 文件里没看到 ${COMPANY} 的 site block"
            exit 1
        fi
    fi

    # 4f. 🔴 v3.0.2 核心修复: 检查容器内 Caddyfile 是否跟 host 同步
    # Docker file bind mount 按 inode 绑,host 上如果有人 sed -i / cp / vim 换过
    # Caddyfile (原子替换),容器的 mount 仍指老 inode → 永远看不到新内容
    # 诊断: 对比 host size vs 容器 size,不一致 → restart 容器重建 mount
    HOST_SIZE=$(wc -c < "$EXT_CADDYFILE" 2>/dev/null | tr -d ' ')
    CONTAINER_SIZE=$(docker exec "$EXTERNAL_CADDY" wc -c /etc/caddy/Caddyfile 2>/dev/null | awk '{print $1}')
    if [ -n "$HOST_SIZE" ] && [ -n "$CONTAINER_SIZE" ] && [ "$HOST_SIZE" != "$CONTAINER_SIZE" ]; then
        echo "  ⚠ host Caddyfile=${HOST_SIZE}B vs 容器 Caddyfile=${CONTAINER_SIZE}B 不一致"
        echo "    这是 docker file bind mount 的 inode 断裂问题(历史上 sed -i/cp/vim 导致)"
        echo "    重启 Caddy 容器重建 mount → 约 5-10 秒 HTTPS 短暂中断..."
        docker restart "$EXTERNAL_CADDY" >/dev/null 2>&1
        sleep 5
        # 再校验
        CONTAINER_SIZE=$(docker exec "$EXTERNAL_CADDY" wc -c /etc/caddy/Caddyfile 2>/dev/null | awk '{print $1}')
        if [ "$HOST_SIZE" = "$CONTAINER_SIZE" ]; then
            echo "  ✓ 重启后容器 Caddyfile 已同步 (${CONTAINER_SIZE}B)"
        else
            echo "  ✗ 重启后仍不一致 (host=${HOST_SIZE} container=${CONTAINER_SIZE}),请手动处理"
            exit 1
        fi
    else
        # 4g. 容器已同步 → 正常 reload (不中断服务)
        echo "  执行 Caddy reload..."
        if docker exec "$EXTERNAL_CADDY" caddy reload --config /etc/caddy/Caddyfile 2>&1 | grep -qE "error|fail"; then
            echo "  ⚠ reload 报错,改用 restart 兜底..."
            docker restart "$EXTERNAL_CADDY" >/dev/null 2>&1 && echo "  ✓ Caddy 已重启" || \
                { echo "  ✗ Caddy 重启也失败,请手动处理"; exit 1; }
        else
            echo "  ✓ Caddy reload 成功"
        fi
    fi

    # 4h. 等证书拿到 (失败不静默,明确告知)
    echo "▸ 等 Let's Encrypt 给 $DOMAIN 签证书(最多 90 秒)..."
    CERT_OK=0
    for i in $(seq 1 18); do
        sleep 5
        if docker exec "$EXTERNAL_CADDY" ls "/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/${DOMAIN}" >/dev/null 2>&1; then
            echo "  ✓ 证书签发成功 (${i}×5s)"
            CERT_OK=1
            break
        fi
        echo "  ... 等待中 (${i}/18)"
    done
    if [ "$CERT_OK" = "0" ]; then
        echo ""
        echo "  ⚠ 90 秒内证书没签下来,可能原因:"
        echo "     1. 80/443 被云厂商防火墙挡(Hostinger/AWS 等云端安全组)"
        echo "     2. Let's Encrypt 限流 — 看 docker logs $EXTERNAL_CADDY | grep -i 'rate\\|too many'"
        echo "     3. Caddyfile 有其他死站把 ACME 队列堵住 — 用 ./scripts/caddy-doctor.sh 自查"
        echo ""
        echo "  后台仍可用 (证书装好前浏览器会报 SSL 错误)"
    fi

elif [ -n "$NON_CADDY_OCCUPIER" ]; then
    # ─────────────────────────────────────────
    # 非 Caddy 占用 → 无法自动接管,给指引
    # ─────────────────────────────────────────
    echo ""
    echo "✗ 80/443 被非 Caddy 程序占用: $NON_CADDY_OCCUPIER"
    echo ""
    echo "  三种处理方式(选一):"
    echo "  1. 停掉该服务释放端口,再重跑:./enable_https.sh"
    echo "  2. 让该服务(nginx/apache 等)反代到 http://localhost:${WEB_PORT}"
    echo "     示例 nginx site block:"
    echo "       server {"
    echo "         server_name ${DOMAIN};"
    echo "         listen 443 ssl;"
    echo "         # ... 你自己的 ssl_certificate 配置"
    echo "         location / { proxy_pass http://localhost:${WEB_PORT}; }"
    echo "       }"
    echo "  3. 改用另一台 VPS 专门跑 TG 监控"
    exit 1

else
    # ─────────────────────────────────────────
    # 自建 Caddy 模式(原逻辑)
    # ─────────────────────────────────────────
    echo "  ✓ 80/443 空闲,启动自建 Caddy"
    echo ""
    echo "▸ 启动自建 Caddy 容器..."
    docker compose -p "$PROJECT" --profile https up -d caddy

    echo "▸ 等 Caddy 申请证书(最多 60 秒)..."
    for i in $(seq 1 12); do
        sleep 5
        if docker logs "tg-caddy-${COMPANY}" 2>&1 | grep -q "certificate obtained successfully\|served key\|tls cert"; then
            echo "  ✓ 证书申请成功"
            break
        fi
        echo "  ... 等待中 (${i}/12)"
    done
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "✓ HTTPS 已启用"
echo ""
echo "▸ 后台访问:        https://$DOMAIN/setup"
echo "▸ OAuth 重定向 URI: https://$DOMAIN/api/oauth/callback"
echo "▸ JavaScript 来源:  https://$DOMAIN"
echo ""
echo "把上面这两个 URL 填到 Google Cloud Console → OAuth 客户端 →"
echo "  - 已获授权的重定向 URI"
echo "  - 已获授权的 JavaScript 来源"
echo "═══════════════════════════════════════════════════════"
echo ""
if [ -n "$EXTERNAL_CADDY" ]; then
    echo "模式:外部反代(复用 $EXTERNAL_CADDY)"
    echo "排查:docker logs $EXTERNAL_CADDY --tail 50"
else
    echo "模式:自建 Caddy"
    echo "排查:docker logs tg-caddy-${COMPANY} --tail 50"
fi
