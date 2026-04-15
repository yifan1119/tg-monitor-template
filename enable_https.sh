#!/usr/bin/env bash
# 一键启用 HTTPS(给 Google OAuth 用)
# 用法:./enable_https.sh [domain]
#   不带参数 → 自动用 nip.io 把 IP 转成域名
#   带参数   → 用你自己的域名(必须先把 DNS A 记录指到本机)
set -e

cd "$(dirname "$0")"

# 拿到 .env 里的 project name(给 docker compose -p 用)
COMPANY=$(grep -E "^COMPANY_NAME=" .env 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" || echo "default")
PROJECT="tg-${COMPANY:-default}"

# 1. 决定域名
if [ -n "$1" ]; then
    DOMAIN="$1"
    echo "▸ 使用自定义域名: $DOMAIN"
    echo "  ⚠ 请确认 $DOMAIN 的 DNS A 记录已指向本机 IP"
else
    # 自动检测公网 IP
    IP=$(curl -s4 --max-time 5 ifconfig.me 2>/dev/null || curl -s4 --max-time 5 ipinfo.io/ip 2>/dev/null)
    if [ -z "$IP" ]; then
        echo "✗ 无法自动获取公网 IP,请手动指定:./enable_https.sh your-domain.com"
        exit 1
    fi
    DOMAIN="${IP//./-}.nip.io"
    echo "▸ 自动用 nip.io 域名: $DOMAIN  (会解析到 $IP)"
fi

# 2. 写入/更新 .env
if grep -q "^PUBLIC_DOMAIN=" .env; then
    sed -i.bak "s|^PUBLIC_DOMAIN=.*|PUBLIC_DOMAIN=$DOMAIN|" .env
else
    echo "PUBLIC_DOMAIN=$DOMAIN" >> .env
fi
echo "▸ .env 已更新 PUBLIC_DOMAIN=$DOMAIN"

# 3. 检查 80 / 443 端口
echo "▸ 检查 80 / 443 端口..."
for port in 80 443; do
    if ss -ltn 2>/dev/null | grep -q ":$port " || netstat -ltn 2>/dev/null | grep -q ":$port "; then
        echo "  ⚠ 端口 $port 已被占用,Caddy 可能起不来。先停掉占用的进程"
    fi
done

# 4. 启动 Caddy(profile=https)
echo "▸ 启动 Caddy 容器..."
docker compose -p "$PROJECT" --profile https up -d caddy

# 5. 等 Let's Encrypt 拿证书
echo "▸ 等 Caddy 申请证书(最多 60 秒)..."
for i in $(seq 1 12); do
    sleep 5
    if docker logs "tg-caddy-${COMPANY:-default}" 2>&1 | grep -q "certificate obtained successfully\|served key\|tls cert"; then
        echo "  ✓ 证书申请成功"
        break
    fi
    echo "  ... 等待中 (${i}/12)"
done

echo ""
echo "═══════════════════════════════════════════════════════"
echo "✓ HTTPS 已启用"
echo ""
echo "▸ 后台访问:        https://$DOMAIN/settings"
echo "▸ OAuth 重定向 URI: https://$DOMAIN/api/oauth/callback"
echo "▸ JavaScript 来源:  https://$DOMAIN"
echo ""
echo "把上面这两个 URL 填到 Google Cloud Console → OAuth 客户端 →"
echo "  - 已获授权的重定向 URI"
echo "  - 已获授权的 JavaScript 来源"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "排查证书没拿到:"
echo "  docker logs tg-caddy-${COMPANY:-default} --tail 50"
