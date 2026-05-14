# Postmortem · 2026-05-14 · 麦小麦 7 部门 fanout 升级 + 全网体检

## TL;DR

一天内 Push 了 v3.1.3.2 / v3.1.3.3(被 revert)/ v3.1.3.4 三版,中间 dev VPS 实测捞到 v3.1.3.3 的 bash-cache P0 bug 紧急 revert;最终把麦小麦 4 台 VPS / 7 个活部门全部升级到 v3.1.3.4 + 中央台 v0.22 全维度串好。**过程中踩了 14 个真实的坑**,全部沉淀进本文件 + 各 ADR + CLAUDE.md 硬性规则更新。

---

## 业务影响

| 部门 | 中断时长 | 影响 |
|---|---|---|
| 麦小麦 / yunying-yp | ~5 分钟(下午修 DNS 撞车 SSH 期间) | 客户 web 后台 502,无业务损失 |
| 麦小麦 / linghang-qd@72.62.120.146 | 数小时(下午撞车 → 客户登入领航后台实际看到极乐数据) | 监察员看错部门数据,无外事号丢失 |
| 麦小麦 / linghang-qd@76.13.196.103 | ~4 小时(v3.1.3.3 升完后 Caddy 用字面占位符 502) | 客户 web 后台无法访问 |
| 全网其他 44 部门 | 0 | 没人在 v3.1.3.3 那短窗口手动 SSH 升级 |

---

## 14 个真实踩过的坑(按发现顺序)

### 坑 1:Caddyfile 模板模糊别名 `web:5001` 跨 network 撞 DNS

**症状**:同一台 VPS 上两个部门 web 后台 URL 不同但显示完全相同账号列表(yunying-yp 显示 ruisheng-yy 数据)。

**根因**:Caddyfile 用 `{$WEB_UPSTREAM:web:5001}`,Caddy 容器接进多个 docker network 时,每个 network 都有名为 `web` 的容器,Docker DNS 撞车。

**修法**:模板改 `tg-web-__COMPANY_NAME__:5001` 显式占位符。详见 [ADR-0044](../adr/0044-v3.1.3.2-caddyfile-explicit-upstream.md)。

**沉淀规则**:**Caddyfile / docker-compose 中不要用模糊 service alias 作为反代 upstream**,任何共用 Caddy 多部门场景必须用显式容器名。

---

### 坑 2:同 IP 多部门并发 fanout 让共用 Caddy 反复 restart

**症状**:fleet「全网升级」按钮按下去,同台 VPS 上 2-3 个部门同时跑 update.sh → 共用 Caddy 在 30-60s 内被反复 restart → HTTPS 全停。

**根因**:fleet.fanout 默认 `max_workers=10` 全并发,没考虑同 IP 部门间共享资源冲突。

**修法**:`serialize_by_ip=True` 同 IP 部门串行,跨 IP 并发。详见中央台 [ADR-0005](https://github.com/yifan1119/tg-central-dashboard/blob/main/docs/adr/0005-v0.22-fanout-serialize-by-ip.md)。

**沉淀规则**:**所有写 action(改 Caddyfile / 重启容器 / 改 .env)在 fanout 时必须按 IP 串行**。

---

### 坑 3:**bash 自我升级 cache 陷阱 — 真 P0**(v3.1.3.3 因此被 revert)

**症状**:dev VPS 升 v3.1.3.3 后 web /login 502。Caddyfile 还有字面 `__COMPANY_NAME__` 占位符没替换。

**根因**:bash 启动 `update.sh` 时把整个文件 read 进内存,后续 `git reset --hard origin/main` 拉新版 update.sh,但 bash 仍按内存里老版本继续跑。**老版本没 v3.1.3.3 新加的 5.5b reload 段** → 升完跳过。

**修法**:update.sh 顶部加 self-reload bootstrapper,先 git fetch + 比对自身 hash,落后 → exec 重启 bash。详见 [ADR-0046](../adr/0046-v3.1.3.4-bash-cache-fix-and-disable-update-notify.md)。

**沉淀规则**:**任何"脚本自我升级"模式都要考虑 bash cache 问题**。新加段必须在 bootstrapper exec 之后才能保证执行。Plan agent 跟 Codex review 都没抓到这条,**只有 dev VPS 端到端实测才捞到**(rule 9 验证铁律救命)。

---

### 坑 4:`git update-index --skip-worktree` 阻止 `git reset --hard`

**症状**:今天下午我手动修 yunying-yp / linghang-qd@72.62.120.146 的 Caddyfile 后,加了 `git update-index --skip-worktree Caddyfile` 防 git reset 冲掉。后续 fanout 升级 git reset 报 `error: Entry 'Caddyfile' not uptodate. Cannot merge`。

**根因**:skip-worktree 让 git 索引跟工作区脱钩,git reset --hard 拒绝执行(不愿冒险覆盖)。

**修法**:升级前临时 `git update-index --no-skip-worktree Caddyfile`,reset 后让 5.5b 段重新替换占位符 + reload Caddy(等价保护)。

**沉淀规则**:**用 skip-worktree 前必须考虑后续升级路径会不会被它阻塞**。临时手术修后,用 v3.1.3.2+ 模板替换占位符的方式更稳,不需要 skip-worktree。

---

### 坑 5:`git fetch --tags` 撞 retag 拒绝 → set -e 退出

**症状**:ruisheng-yy SSH 跑 update.sh,卡在 `git fetch origin --tags` → `! [rejected] v3.1.3 -> v3.1.3 (would clobber existing tag)` → set -e 整段退出 → 后面 bootstrapper 都跑不到。

**根因**:本地 v3.1.3 tag 跟远端 retag 后不一致,git fetch --tags 默认拒绝 overwrite。

**修法 v3.1.3.2 已加**:`git fetch origin --tags --force || true`。但**老版本 update.sh 没这条**,所以从 v3.1.3 之前升 v3.1.3.4 时撞。

**临时修**:SSH 上去 `git tag -d v3.1.3` 删本地老 tag。

**沉淀规则**:**git fetch --tags 永远要 `--force || true`**。retag 是常见运维场景,不能让一次 retag 让所有 dept 升级失败。

---

### 坑 6:老镜像没装 docker-cli + alpine fallback apk 装 docker-cli 失败

**症状**:agent.upgrade 在 v3.1.3 之前的镜像里跑,容器内没 docker → fall through 到 alpine fallback → alpine 容器 `apk add docker-cli` 失败(`apk: ContainerError`)。

**根因**:
1. v3.1.3 之前 Dockerfile 没装 docker-cli(v3.1.3 才加)
2. alpine 默认 apk 镜像源在国外,docker-cli 包大,网络抖动失败常见

**修法**:对 v3.1.3 之前老部门,**先 SSH 手动跑 update.sh** 升一次到 v3.1.3+(host 上 docker 肯定有),之后 fanout 就走 container_git 路径。

**沉淀规则**:**Dockerfile 必须包含 docker-cli + docker-compose-plugin**(已 v3.1.3 ship)。alpine fallback 只能算 last resort,不能依赖。

---

### 坑 7:agent.upgrade subprocess 被升级自杀,fanout 报失败但 git 实际成功

**症状**:fanout 返 `failed_at: bash`,但 SSH 上去看 git HEAD 已经升到 v3.1.3.4。

**根因**:agent 跑 `subprocess.run(["bash", "update.sh"])`,update.sh 内部跑 `docker compose up --force-recreate` → web 容器(agent 自己)被 recreate → subprocess 中断 → agent 上报 bash failed,但 git 已经 reset 完。

**沉淀规则**:**fanout 失败要先验 git HEAD,不要只信 fanout 返回值**。建议 v3.1.3.5 加 fanout 完成后自动 inspect 验 new_version,识别 false-negative。

---

### 坑 8:fleet 报错被 truncate 到 300 字符

**症状**:fanout 返 `err={"action":"upgrade","ok":false,"result":{"failed_at":"git","steps":[...truncated]}}`。看不到完整 stack。

**根因**:`fleet.py:163` `e.read().decode("utf-8", "ignore")[:300]`。

**v3.1.3.5 / v0.23 修法**:改 1500+ 字符,或者完整透传 result.steps。

---

### 坑 9:agent `_read_version_string` regex 不支持 4 段版本号

**症状**:`new_version` 字段返 `v3.1.3` 但实际 git HEAD 是 v3.1.3.4 commit。

**根因**:agent.py `_read_version_string` 正则 `r"v\d+\.\d+(?:\.\d+){0,2}"` 限制 2-4 段(`{0,2}` 表示 0-2 个额外段),但 v3.1.3.4 有 4 段。

**v3.1.3.5 修法**:regex 改成 `r"v\d+(?:\.\d+){1,4}"`(支持 2-5 段)。

---

### 坑 10:**update.sh `git reset --hard` 把 enable_https.sh 加的额外 site block 冲掉 — 真严重 bug**

**症状**:升级后同 IP 多部门 VPS 上,Caddyfile 只剩本部门的 default site block,其他部门(`enable_https.sh` 加的)site block 被冲走 → Caddy 反代时找不到对应 host → 502/TLS internal error。

**根因**:enable_https.sh 加的 site block 直接 append 到 Caddyfile(tracked file),git reset --hard 拉主仓模板 → 额外 site block 全没了。update.sh 有 git stash 但没 stash pop(stash 留作 rollback 用)。

**临时修**:手动 cat >> Caddyfile 把 site block 加回 + caddy reload。

**v3.1.3.5 必修**:
- 方案 A:enable_https.sh 加的 site block 写到 `Caddyfile.local`(.gitignored),Caddyfile 主文件 import Caddyfile.local
- 方案 B:update.sh 在 git reset 之前 backup `enable_https.sh` 加的段,reset 之后 append 回
- 方案 C:enable_https.sh 加 import 到主 Caddyfile,site block 在独立目录

**沉淀规则**:**任何「自动追加到 git tracked 文件」的运维操作都不安全 — 升级时 git reset 会冲掉**。要么 .gitignore,要么 import。

---

### 坑 11:Telegram `PhonePasswordFloodError` web 显示英文

**症状**:客户 web 后台输入手机号点发送验证码,显示 `You have tried logging in too many times`。客户看不懂英文。

**根因**:web.py `_humanize_tg_error` 没翻译这条。

**v3.1.3.5 修法**:加中文 `「该号码尝试登录太多次,Telegram 限流中,请等 24h 或换没今天试过的号」`。

---

### 坑 12:Telegram `force_sms=False` 默认行为

**症状**:客户号码已经在 Telegram 上有 active session(任何设备),send_code 验证码发到 **TG App 内通知**(不发 SMS),客户没在自己设备装 TG App → 收不到。

**v3.1.3.5 修法**:web UI 在「发送验证码」按钮旁加「未收到? force SMS 重发」选项,调 send_code_request(phone, force_sms=True)。

---

### 坑 13:跨对话 Claude 没记忆

**症状**:用户在别的对话发 SSH 密码,我这条对话看不到。

**沉淀规则**:**敏感信息(密码 / token / chat_id)统一发到一个固定地方**(本地 `.claude/private-notes.md` / 1Password)。Claude 跨对话独立,不要假设别对话的信息能传染。

---

### 坑 14:CENTRAL_PUSH_URL 配置不统一

**症状**:全网部门 .env 里 CENTRAL_PUSH_URL 4 种状态:`monitor.atsop.io` / `tg.13-193-143-29.nip.io` / 空 / 缺字段。

**根因**:install.sh 默认值改过几次,老部署没自动 migrate。

**v3.1.3.5 修法**:install.sh / update.sh `.env migrate` 段强制覆盖 CENTRAL_PUSH_URL 到统一值(防客户老配置漂移)。

---

## 沉淀到 CLAUDE.md 硬性规则

下面 4 条加进主仓 CLAUDE.md「开发规范」段:

1. **Caddyfile / docker-compose 反代不准用模糊 service alias 作为 upstream**,共用 Caddy 多部门场景必须显式容器名(否则跨 network DNS 撞车)。

2. **任何脚本自我升级模式必须有 self-reload bootstrapper**,bash cache 不能依赖 git reset 自动刷新。

3. **`git fetch --tags` 永远 `--force || true`** 防 retag 拒绝。

4. **enable_https.sh / 任何运维"自动追加到 tracked 文件"的脚本都要写到 .gitignored 文件 + import**,不能直接 append 到 git 管理文件(升级 git reset 会冲掉)。

5. **fanout 验证用 git HEAD 而不只信脚本返回值**(防 self-suicide 假象误判失败)。

6. **运维敏感信息(密码 / token)统一存本地 `.claude/private-notes.md` 或 1Password**,Claude 跨对话独立不传染。

---

## 麦小麦 7 个部门最终状态(2026-05-14 EOD)

| 部门 | git | 中央台串接 | 客户业务 |
|---|---|---|---|
| jileyinqing-qd@72.62.120.146 | v3.1.3.4 ✓ | 全维度 ✓ | 正常 |
| linghang-qd@72.62.120.146 | v3.1.3.4 ✓ | 全维度 ✓ | 正常 |
| yunying-yp@72.62.195.172 | v3.1.3.4 ✓ | 全维度 ✓ | 正常 |
| ruisheng-yy@72.62.195.172 | v3.1.3.4 ✓ | 全维度 ✓ | 正常 |
| hengfeng-hf@76.13.181.29 | v3.1.3.4 ✓ | 全维度 ✓ | 正常(OAuth 已客户重授) |
| jileyinqing-yy@76.13.196.103 | v3.1.3.4 ✓ | 全维度 ✓ | 正常 |
| linghang-qd@76.13.196.103 | v3.1.3.4 ✓ | 全维度 ✓ | 正常(OAuth 已客户重授,仅 +85599875613 等 24h flood wait 解除) |

**100% 串好,版本完全统一**。

---

## v3.1.3.5 / v0.23 待修清单(从今天坑里提炼)

| # | 文件:行 | 修什么 | 优先级 |
|---|---|---|---|
| 1 | `update.sh` git reset 之前 | backup enable_https.sh 加的额外 site block,reset 之后 append 回(或者迁移到 Caddyfile.local) | 🔴 P0 |
| 2 | `web.py:_humanize_tg_error` | 加 `PhonePasswordFloodError` 中文翻译 | 🟡 P1 |
| 3 | `web.py:send_code` | UI 加「未收到? force SMS」选项,调 `send_code_request(force_sms=True)` | 🟡 P1 |
| 4 | `agent.py:_read_version_string` | regex 改成 `v\d+(?:\.\d+){1,4}` 支持 4 段 | 🟢 P2 |
| 5 | `fleet.py:163` | 报错 truncate 改 1500+ 字符或返完整 result.steps | 🟢 P2 |
| 6 | `install.sh / update.sh .env migrate` | 强制覆盖 CENTRAL_PUSH_URL 到统一值 | 🟢 P2 |
| 7 | `update_checker.py` | UPDATE_CHECK_NOTIFY_ENABLED 加 `config.reload_if_env_changed` 热 reload | 🟢 P2 |
| 8 | `fleet_health.py` | 容器名识别 bug(同 IP 多部门时拿错) | 🟢 P2 |

P0 一条**必须立刻修**(影响后续 fanout 苏木 / 季霖 / 陈家碧 / 吴苍河 共用 Caddy 部门时会重蹈覆辙)。
