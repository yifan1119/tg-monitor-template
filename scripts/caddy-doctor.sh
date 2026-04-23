#!/usr/bin/env bash
# Caddy 自查工具 (v3.0.2+)
# 用法: bash scripts/caddy-doctor.sh
#
# 检查项:
#   1. 容器是否运行
#   2. host 和容器内 Caddyfile 是否一致 (inode 同步 bug 最常见症结)
#   3. Caddyfile 语法是否合法
#   4. 有几个 site block / 哪些死站 (对应的后端容器不存在)
#   5. 证书目录: 哪些域签下来了 / 哪些没签
#   6. 最近 ACME 错误摘要
#
# 退出码: 0 = 全部正常 / 1 = 发现问题

set -u

RED=$(printf '\033[31m')
GREEN=$(printf '\033[32m')
YELLOW=$(printf '\033[33m')
BOLD=$(printf '\033[1m')
RESET=$(printf '\033[0m')

ISSUES=0

# 找 Caddy 容器 (优先 tg-caddy-*,fallback 任意 caddy 镜像的容器)
CADDY=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^tg-caddy-' | head -1)
if [ -z "$CADDY" ]; then
    CADDY=$(docker ps --format '{{.Names}}|{{.Image}}' 2>/dev/null | awk -F'|' '$2 ~ /caddy/i {print $1; exit}')
fi

echo "${BOLD}=== Caddy 自查工具 ===${RESET}"
echo ""

# ─── 1. 容器状态 ───
echo "${BOLD}[1/6] 容器状态${RESET}"
if [ -z "$CADDY" ]; then
    echo "  ${RED}✗ 没找到运行中的 Caddy 容器${RESET}"
    echo "     检查: docker ps -a | grep caddy"
    exit 1
fi
STATUS=$(docker inspect "$CADDY" -f '{{.State.Status}}' 2>/dev/null)
UPTIME=$(docker inspect "$CADDY" -f '{{.State.StartedAt}}' 2>/dev/null)
echo "  容器: ${CADDY} (状态: ${STATUS}, 启动于: ${UPTIME})"
[ "$STATUS" != "running" ] && { echo "  ${RED}✗ 不在运行中${RESET}"; ISSUES=$((ISSUES+1)); }
echo ""

# ─── 2. Caddyfile inode 同步 ───
echo "${BOLD}[2/6] Caddyfile 同步 (host vs 容器)${RESET}"
MOUNT=$(docker inspect "$CADDY" \
    --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)

if [ -z "$MOUNT" ]; then
    echo "  ${YELLOW}⚠ 没有 /etc/caddy/Caddyfile 的 bind mount (可能镜像内置或 volume 挂载)${RESET}"
else
    echo "  Host 文件: $MOUNT"
    HOST_SIZE=$(wc -c < "$MOUNT" 2>/dev/null | tr -d ' ')
    CONT_SIZE=$(docker exec "$CADDY" wc -c /etc/caddy/Caddyfile 2>/dev/null | awk '{print $1}')
    HOST_HASH=$(sha256sum "$MOUNT" 2>/dev/null | cut -c1-16)
    CONT_HASH=$(docker exec "$CADDY" sha256sum /etc/caddy/Caddyfile 2>/dev/null | cut -c1-16)
    echo "  Host size/hash:      ${HOST_SIZE:-?}B  ${HOST_HASH:-?}"
    echo "  Container size/hash: ${CONT_SIZE:-?}B  ${CONT_HASH:-?}"
    if [ "$HOST_SIZE" = "$CONT_SIZE" ] && [ "$HOST_HASH" = "$CONT_HASH" ]; then
        echo "  ${GREEN}✓ 一致${RESET}"
    else
        echo "  ${RED}✗ 不一致 — docker file bind mount inode 断裂${RESET}"
        echo "     原因: 历史上用 sed -i / cp / vim 原子替换过 Caddyfile"
        echo "     修法: docker restart $CADDY"
        ISSUES=$((ISSUES+1))
    fi
fi
echo ""

# ─── 3. 语法校验 ───
echo "${BOLD}[3/6] Caddyfile 语法${RESET}"
VALIDATE=$(docker exec "$CADDY" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1)
if echo "$VALIDATE" | grep -qi "valid"; then
    echo "  ${GREEN}✓ 语法合法${RESET}"
else
    echo "  ${RED}✗ 语法有问题:${RESET}"
    echo "$VALIDATE" | head -10 | sed 's/^/     /'
    ISSUES=$((ISSUES+1))
fi
echo ""

# ─── 4. site block 扫描 ───
echo "${BOLD}[4/6] Site block 清单 (对比运行中容器)${RESET}"
# 抓出容器里 Caddyfile 的顶层域名 (必须顶格无缩进,过滤 reverse_proxy / handle 等指令)
SITES=$(docker exec "$CADDY" cat /etc/caddy/Caddyfile 2>/dev/null \
    | grep -E '^[a-zA-Z0-9_.*{$-][a-zA-Z0-9_.*{}$:-]* *\{ *$' \
    | grep -vE '^\s*#' \
    | awk '{print $1}')

if [ -z "$SITES" ]; then
    echo "  ${YELLOW}⚠ 没扫到任何 site block${RESET}"
else
    RUNNING=$(docker ps --format '{{.Names}}' 2>/dev/null)
    for site in $SITES; do
        # 从 site 附近 3 行内抓 reverse_proxy upstream
        UP=$(docker exec "$CADDY" sh -c "grep -A3 '^${site//./\\.} {' /etc/caddy/Caddyfile 2>/dev/null | grep reverse_proxy | head -1" | awk '{print $2}' | cut -d: -f1)
        # 如果 upstream 是容器名,查容器是否活
        if [ -n "$UP" ] && [[ "$UP" =~ ^[a-zA-Z] ]] && [[ ! "$UP" =~ ^\$ ]]; then
            if echo "$RUNNING" | grep -qx "$UP"; then
                echo "  ${GREEN}✓${RESET} ${site}  →  ${UP} (running)"
            else
                echo "  ${RED}✗${RESET} ${site}  →  ${UP} ${RED}(容器不存在 — 死站)${RESET}"
                ISSUES=$((ISSUES+1))
            fi
        else
            # upstream 是 IP / host.docker.internal / env var → 信任
            echo "  ${GREEN}·${RESET} ${site}  →  ${UP:-<ENV>}"
        fi
    done
fi
echo ""

# ─── 5. 证书目录 ───
echo "${BOLD}[5/6] Let's Encrypt 证书状态${RESET}"
CERT_DIR="/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory"
CERTS=$(docker exec "$CADDY" ls "$CERT_DIR" 2>/dev/null)
if [ -z "$CERTS" ]; then
    echo "  ${YELLOW}⚠ 证书目录为空 (没签过或在用其他 CA)${RESET}"
else
    for c in $CERTS; do
        echo "  ${GREEN}✓${RESET} $c"
    done
fi
# 对比 site block 和证书,找出"有 site 但没证书"
for site in $SITES; do
    # 去掉 env var/wildcard 这类特殊 site
    [[ "$site" =~ ^\{.*\}$ ]] && continue
    [[ "$site" =~ ^\$ ]] && continue
    if ! echo "$CERTS" | grep -qx "$site"; then
        echo "  ${YELLOW}⚠${RESET} ${site} ${YELLOW}有 site 但没证书 (可能还在签 / 被限流 / 后端死)${RESET}"
    fi
done
echo ""

# ─── 6. 最近 ACME 错误 ───
echo "${BOLD}[6/6] 最近 5 分钟 ACME / TLS 错误摘要${RESET}"
ERRORS=$(docker logs "$CADDY" --since 5m 2>&1 | grep -iE '"level":"error"|rate limit|too many' | tail -5)
if [ -z "$ERRORS" ]; then
    echo "  ${GREEN}✓ 没有错误${RESET}"
else
    echo "$ERRORS" | while IFS= read -r line; do
        # 提取关键字
        msg=$(echo "$line" | grep -oE '"msg":"[^"]*"' | head -1 | sed 's/"msg":"//;s/"$//')
        ident=$(echo "$line" | grep -oE '"identifier":"[^"]*"' | head -1 | sed 's/"identifier":"//;s/"$//')
        err=$(echo "$line" | grep -oE '"error":"[^"]*"' | head -1 | sed 's/"error":"//;s/"$//')
        echo "  ${RED}✗${RESET} ${ident:-?}: ${msg:-?} ${err:+— $err}" | head -c 200
        echo ""
    done
    ISSUES=$((ISSUES+1))
fi
echo ""

# ─── 总结 ───
if [ "$ISSUES" = "0" ]; then
    echo "${GREEN}${BOLD}=== 全部正常 ===${RESET}"
    exit 0
else
    echo "${RED}${BOLD}=== 发现 ${ISSUES} 个问题 ===${RESET}"
    echo ""
    echo "常见修法:"
    echo "  • inode 不一致 → docker restart $CADDY"
    echo "  • 死站 → 编辑 Caddyfile 删掉对应 site block 再 docker restart $CADDY"
    echo "  • 限流 → 等 1-7 天冷却,或用另一个域名"
    echo "  • 签不下来 → 查云厂商防火墙 80/443 有没有开"
    exit 1
fi
