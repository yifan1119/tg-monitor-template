# tg-monitor-template — Claude 专案记忆

## 项目定位

Telegram 私聊监控系统,部署在客户 VPS 上,每个客户部门独立 Docker compose project。

**技术栈**:
- Python 3.11 + Telethon(user client,不是 bot)+ aiogram(bot)+ gspread(Google Sheets)+ SQLite + Flask
- Docker + Caddy(HTTPS)
- 规模:一个客户 200+ 外事号账号

**业务场景**:
- 监听外事号私聊消息,落盘到客户自己的 Google Sheets
- 关键词预警、未回复预警(30 分钟)、删除消息检测
- 每日零点日报、session 健康巡检
- Web 后台账号管理 + 设置精灵

**客户约束**:
- **绝不能让 200+ 已登录账号重新登录**(sessions/ 目录 + docker volume 不能动)
- 每次改动必须向后兼容,老 .env 不改也能跑

## 架构关键文件

| 文件 | 职责 |
|---|---|
| `main.py` | 启动入口,初始化 listener / sheets / bot / scheduler |
| `listener.py` | Telethon 事件处理(on_incoming / on_outgoing),消息入 DB,触发预警 |
| `database.py` | SQLite schema + migration 框架(`_run_migrations` + `_safe_add_column`)+ helpers |
| `sheets.py` | Google Sheets 读写(按账号分桶 flush + 429 退避 per-account) |
| `bot.py` | aiogram bot,预警推送 + callback handler(审核按钮) |
| `tasks.py` | 所有 asyncio loops(`_no_reply_loop` / `_session_health_loop` / `_sheets_backlog_loop` 等) |
| `config.py` | 环境变量加载 + `reload_if_env_changed` 热 reload |
| `web.py` | Flask 后台(登入 / 设置页 / 账号管理 / 驾驶舱) |
| `dashboard_api.py` | 驾驶舱 API |
| `update_checker.py` | 每 6h 查 GitHub 新版 → 推预警群 |
| `upgrader.py` | 软升级逻辑(tarball 覆盖,保留 sessions/ + .env + data/) |
| `release_notes.json` | 每个版本的白话版说明(推送时用) |

## 开发规范(参考吴献忠团队)

### 1. 文档-代码一致性
commit message 里明确标出「影响文档」:
```
影响文档:
- docs/adr/0003-xxx.md (新增)
- README.md#版本 (v3.0.0 条目)
- release_notes.json (v3.0.0 key)
```

### 2. ADR(Architecture Decision Records)
关键决策必须写 ADR 放 `docs/adr/`:
- 格式:`背景 / 决策 / 原因 / 来源 / 后果`
- 文件名:`NNNN-<版本>-<简短描述>.md`
- 来源标注:哪个审计(Codex / 人工)发现的

### 3. 多模型交叉审阅
- 开发:Claude (Opus / Sonnet)
- 审阅:Codex CLI(`codex exec -C . "审阅提示"` + GPT-5.4 高推理)
- 只修 P0 / P1,P2 大多是过度设计忽略

### 4. 暴力测试清单
发布前必过,每个版本更新 `docs/testing/<version>-stress.md`:
- 典型场景:Sheets 429 积压 / Session 冻结 / 并发 callback / 跨部门权限

### 5. 分批发布
大改动拆成多个小版本,每版 feature flag 默认关,独立可回滚。

### 6. Feature flag 默认关
所有新功能必须:
- `.env` 新字段默认值 = 老行为
- Web UI 条件渲染
- 代码分支:`if config.NEW_FEATURE_ENABLED:` 走新路径,否则走老路径

### 7. Worktree 隔离
多分支并行开发时用 git worktree:
- `~/Desktop/claude/tg-monitor-template/` = main(稳定版)
- `~/Desktop/claude/tg-monitor-v3/` = feature/v3.0.0(开发中)

### 8. 🔴 Docker 容器启动 cp 列表(踩过多次的坑,硬性规定)

**问题**:`docker-compose.yml` 容器 command 写的是 `cp -rf /app/repo/*.py /app/` — **只 copy `.py` 文件**,不覆盖:
- `README.md`(Flask `_app_version_string()` 读这个显示登入页版本号)
- `release_notes.json`(升级通知用)
- `templates/*.html`(前端 UI)
- `docs/**`(ADR + testing 文档)

导致升级后:
- 登入页版本号**显示老版**(Flask 读 `/app/README.md` 还是镜像原始版)
- 前端按钮/modal 改动**没生效**(Flask 读 `/app/templates/*.html` 还是镜像原始版)

**硬性规定**:每次 scp 部署改动到 VPS 后,一律走这个命令同步所有文件到容器:

```bash
docker exec <container> sh -c '
  cp -rf /app/repo/*.py /app/
  cp -rf /app/repo/templates/*.html /app/templates/ 2>/dev/null
  cp -rf /app/repo/README.md /app/README.md 2>/dev/null
  cp -rf /app/repo/release_notes.json /app/release_notes.json 2>/dev/null
'
docker restart <container>
```

**长期修法**(还没做):改 `docker-compose.yml` 的 command 覆盖所有非 state 文件,`docker restart` 就自动同步。但要 recreate 容器,留给下个版本(v3.0.2+)一起做。

目前每次部署必须人工保证这 4 类文件都 cp 到 /app/,否则客户看到的跟代码不一致。

### 9. 🔴 Caddyfile 绝不用 sed -i / cp / vim,只能 `>>`(硬性规定)

**问题**:Caddy 容器里 Caddyfile 是 **file bind mount**(不是目录挂载):

```yaml
# docker-compose.yml
- ./Caddyfile:/etc/caddy/Caddyfile:ro
```

Docker file bind mount 按 **inode** 绑定。任何**原子替换**(`sed -i` / `cp` / `mv` / `vim :wq`)都会产生新 inode,容器的 mount 仍指旧 inode(已 unlink 但句柄还在)→ **容器里永远看到旧内容**。

症状:
- `docker exec caddy reload` 说 `config is unchanged`(读的是容器内老文件)
- 新追加的 site block 永远不生效
- 新部门 HTTPS 永远签不下来

**硬性规定**:改 Caddyfile 只能用 **in-place append**:

```bash
# ✅ 对:不改 inode
echo "新内容" >> /root/tg-monitor-demo/Caddyfile

# ❌ 错:sed -i 原子替换,立刻断 inode
sed -i '/old/d' /root/tg-monitor-demo/Caddyfile

# ❌ 错:cp 整体覆盖
cp /tmp/new /root/tg-monitor-demo/Caddyfile

# ❌ 错:vim 保存
vim /root/tg-monitor-demo/Caddyfile    # :wq 会写临时文件再重命名
```

**如果必须改删/整体重写**(不能只追加),两种补救:
1. `docker restart <caddy容器>` 让容器重新 attach 新 inode
2. 用 `cat /tmp/new > /root/.../Caddyfile`(重定向而非 cp,保持同 inode)

**自查工具**:`scripts/caddy-doctor.sh` 会对比 host 和容器内 Caddyfile 的 size/hash。不一致就是断了,跑一次就知道。

**背景**:2026-04-23 线上某 VPS 一台机部署多部门 HTTPS 失败,排查 40 分钟才发现是这个 inode 断裂问题。详见 [ADR-0017](docs/adr/0017-v3.0.2-caddyfile-inode-bind-mount.md)。

## 关键决策历史

全部 ADR 见 [`docs/adr/`](docs/adr/README.md)。

| 版本 | 关键决策 | ADR |
|---|---|---|
| v2.10.23 | Sheets flush 按账号分桶 + 单账号失败隔离 + 429 per-account 退避 | [0001](docs/adr/0001-v2.10.23-sheets-per-account-flush.md) |
| v2.10.23 | Session health 加 `get_me()` 真 RPC 探测(修冻结账号绿灯 bug) | [0002](docs/adr/0002-v2.10.23-session-health-get-me-probe.md) |
| v2.10.23 | `has_alert_today` 只认真送达的(修失败不重试 bug) | [0003](docs/adr/0003-v2.10.23-has-alert-today-sent-only.md) |
| v2.10.23 | `upsert_account` 不再覆盖业务字段(改成 ON CONFLICT 只更新 TG 身份) | [0004](docs/adr/0004-v2.10.23-upsert-account-no-business-override.md) |
| v3.0.0 | 两段式预警用 `alerts.stage` 字段而不是改 `type`(向后兼容回滚) | [0005](docs/adr/0005-v3.0.0-two-stage-use-stage-column.md) |
| v2.10.23 | `sync_headers` 单账号异常隔离(同 flush_pending 逻辑,修 B2/B3 推送空白) | [0006](docs/adr/0006-v2.10.23-sync-headers-per-account-isolation.md) |
| v2.10.24 | `update.sh` orphan cleanup 放宽 + 容器缺失检测(修升级撞冲突) | [0007](docs/adr/0007-v2.10.24-update-sh-robust-container-recreate.md) |
| v2.10.24.1 | Sheets 读 API 配额保护:`sync_headers` + `peer_name_consistency` 间隔独立化(默认 60s → 600s)+ 紧急开关 + 修 docstring-代码不一致 | [0008](docs/adr/0008-v2.10.24.1-sheets-read-quota-fix.md) |
| v2.10.24.2 | 预警分页历史空白自动回填(承接 v2.10.24.1):启动立刻补 + 每小时巡检;幂等只填空栏;DB 也空的 log 清单 | [0009](docs/adr/0009-v2.10.24.2-backfill-alert-history.md) |
| v2.10.24.3 | 预警分页整行缺失自动 writeback(alerts.sheet_written + 60s loop 无限重试,保零丢失;升级后新预警不再因 429/短暂抖动丢行) | [0010](docs/adr/0010-v2.10.24.3-alert-writeback-no-loss.md) |
| v2.10.24.4 | `update_checker` 版本号 regex 支持四段(贪婪 `v\d+(?:\.\d+)+`)— 修后台升级弹窗一直显示旧版本说明 | [0011](docs/adr/0011-v2.10.24.4-update-checker-four-segment-version.md) |
| v2.10.24.5 | `release_notes.json` `v2.10.24` key 改累计说明 — 补旧客户(还没升 v2.10.24.4)也能看到正确内容 | [0012](docs/adr/0012-v2.10.24.5-release-notes-v2.10.24-cumulative-backfill.md) |
| v2.10.24.6 | `release_notes.json` 文案白话化原则(禁用文件名 / 函数名 / 技术缩写,改业务具象词)| [0013](docs/adr/0013-v2.10.24.6-release-notes-plain-language.md) |
| v2.10.25 | 媒体存储切换:`MEDIA_STORAGE_MODE=drive/tg_archive/off`(默认 drive,tg_archive 用 Bot 转发到独立 TG 群规避 Google 账号冻结)| [0014](docs/adr/0014-v2.10.25-media-storage-tg-archive.md) |
| v3.0.0 | 两段式未回复预警 schema(migration V5 + accounts +4 字段 + `alerts.stage` + demo 错位 DB 兼容修复)| [0015](docs/adr/0015-v3.0.0-two-stage-alert-data-layer.md) |
| v3.0.0 | 两段式预警推送 + callback + Telethon 真名解析 + 自动升级 loop(事件驱动 + poll 兜底) | [0016](docs/adr/0016-v3.0.0-two-stage-alert-push-callback.md) |
| v3.0.2 | Caddyfile 热更新的 Docker file bind mount inode 断裂 — enable_https.sh 加 inode 自愈 + fail-loud + 新增 `scripts/caddy-doctor.sh` 自查工具(shared caddy 模式一台 VPS 部多部门 HTTPS 终于稳定)| [0017](docs/adr/0017-v3.0.2-caddyfile-inode-bind-mount.md) |
| v3.0.3 | `update.sh` 升级时自动 Caddy 体检 + 自愈(只动本部门相关的 Caddy,保护 VPS 上其他项目) — 承接 v3.0.2,把故障检测从"客户自己跑诊断工具"升到"升级自动自愈" | [0017](docs/adr/0017-v3.0.2-caddyfile-inode-bind-mount.md) |
| v3.0.4 | 两段式预警 @ 通知修复:`@username` 格式不再强转 inline mention,改用 TG 原生 @ 解析(bot 的 inline mention 受反垃圾规则限制,没 /start 过 bot 的人收不到通知;用 `@text` 文本能稳稳触发) — `bot.py:_build_tg_mention` 优先级调整 | [0018](docs/adr/0018-v3.0.4-tg-mention-notification-fix.md) |
| v3.0.5 | 删除消息预警跟 stage2 审批体验对齐:账号配了 `owner_tg_id` → @负责人 + 登记违规/取消按钮 (数据驱动,没配的账号保持老通过/拒绝路径向后兼容);新增 `REMIND_DELETE_TEXT` 配置 | [0019](docs/adr/0019-v3.0.5-delete-alert-owner-mention.md) |
| v3.0.6 | 驾驶舱三件套运维自助化:(1) 后台日志查看面板(容器白名单防越权 + 注入防御正则)(2) Sheet 写入堵塞自动诊断(扫 tg-monitor log 识别 OAuth 失效/429/无权限 + 修复按钮) (3) 补齐 v3.0.5 的 `REMIND_DELETE_TEXT` UI 输入框 | [0020](docs/adr/0020-v3.0.6-dashboard-self-service-ops.md) |
| v3.0.7 | OAuth 重新授权后 Sheets 自愈 — 闭合 v3.0.6 诊断—修复链路:`SheetsWriter` 加 `reload_credentials()` + `_write_lock` 改 RLock(防递归死锁);`flush_pending` 加三层 OAuth 自愈 catch(`RefreshError` 主路径 + `APIError` 关键词兜底 + bare Exception 兜底,**OAuth 检查在 429 检查之前**);`OAUTH_FAIL_MARKERS` + `is_oauth_failure()` helper 抽到 `oauth_helper.py` 单一来源,跟 `dashboard_api.py` 共用。**Codex 抓出的关键约束**:`tg-monitor` / `tg-web` 是独立容器跨进程不能共享 in-memory singleton — 走文件 IPC(`data/google_oauth_token.json` 共享 docker volume),flush_pending 自愈时读最新文件 | [0021](docs/adr/0021-v3.0.7-oauth-reauth-hot-reload.md) |
| v3.0.8 | Sheets 写入架构治本 + 卡死深度诊断 + 自助修复:`write_messages` 改用 `values.append` 替代 `update + col_values read`(每个 peer 2 → 1 API call,client 改/插/删行 append 自动跟随末尾不被覆盖); `_rate_limit` 加 60s 滑动窗口令牌桶(默认 50/min,Google 配额 60/min/user 不超); 驾驶舱「立刻深度诊断」modal 后台跑 SQL 显示孤儿消息(peer FK 失效)/ `col_group=NULL` / 缺 sheet_tab 明细; 「一键修复」按钮 `/api/diag/sheets-fix-stuck` POST `@admin_required` 自动放弃孤儿 / 分配 NULL col_group; 「立刻重启监听器」按钮整合 v3.0.7.1; 设置页加 `SHEETS_FLUSH_INTERVAL` / `SHEETS_RATE_LIMIT_PER_MIN` 输入框; 诊断关键词收紧不再 false positive 把 Drive 上传 404 误判成 Sheet 不存在。**关键设计 pivot**:用户审阅抓到原计划的"DB 缓存 row counter"会被客户改表破坏 → pivot 到 append API 自动跟随末尾 | [0022](docs/adr/0022-v3.0.8-sheets-quota-fix-and-deep-diag.md) |
| v3.1 | Sheet 后台扫描 + 客户删旧消息自动回填空位:解决 v3.0.8 `values.append + table_range` 被 Google 自动检测整张表 boundary 推高行号(实测 38 peer 各自 col_group 独立但 max_row 全部齐头并进 280-421)+ 客户手删旧消息无回填的痛点。`peers` migration V6 加 `next_sheet_row` + `next_sheet_row_resynced_at`;`write_messages` 双轨决策(update 命中 next_row / NULL fallback append);`_sheet_position_resync_loop` 每 15 分钟 `ws.get_all_values()` 整 worksheet 一次性扫(1 API/ws 无视 peer 数),`_scan_first_empty` 纯函数找首空行;tg-web → tg-monitor IPC 走 `data/.sheet_resync_request` 文件标志(跟 ADR-0021 OAuth 同模式);3 层 race 防御(updatedRange 校验 / acell verify 强保护开关 / append fallback);feature flag `SHEET_RESYNC_ENABLED` 默认 ON 关掉退回 v3.0.9;0 重登 0 数据迁移 | [0027](docs/adr/0027-v3.1-sheet-resync-auto-refill.md) |
| v3.0.9 | 中央台数据接口扩展 — 把 DB 业务字段全量暴露给 metrics token:`accounts_matrix()` SELECT 加 tg_id/business_tg_id/owner_tg_id/remind_*_text;`alerts_recent()` SELECT 加 status/stage/keyword/reviewed_at/sheet_written/claimed_at/last_write_error + account_id/peer_id/msg_id;新增 4 个 /api/v1/* endpoint(violations 违规登记 / alerts 通用查 + 分页 / peers 全监控聊天 / messages 消息明细强制 account_id+peer_id 必填防整表扫)沿用 metrics token 鉴权。**0 新表 0 新字段 0 数据迁移**,纯只读扩 SELECT + 加路由,不动 listener/sheets/bot/sessions,200+ TG 账号不重登;status/type/stage 白名单校验 + limit 硬上限(alerts/messages 1000、peers 5000)+ `_clamp_int()` 防滥用 | [0026](docs/adr/0026-v3.0.9-central-data-api-expansion.md) |
| v3.0.8.3 | 修「立刻重启监听器」404 找不到容器:`/api/restart` (web.py:2229) 直接 `client.containers.get('tg-monitor-' + COMPANY_NAME)` 找不到就 throw, **没复用** `_start_tg_monitor()` (web.py:450) 早就有的 fallback (找本机任何 tg-monitor-*)。客户案例 `.env COMPANY_NAME=dingfenggs1` 但实际容器=`tg-monitor-dingfenggs2` (部署遗留 inconsistency, 设置页锁定 COMPANY_NAME 不能改)→ 重启按钮 404 客户卡死。**修法**: `/api/restart` 改成 `_start_tg_monitor()` 0 新代码; `dashboard_api._diagnose_sheets_stuck` (line 671) 加同样 fallback | [0025](docs/adr/0025-v3.0.8.3-restart-container-fallback.md) |
| v3.0.8.2 | 升级提示去 SSH 包装 + 复制按钮 HTTP/HTTPS 三层兜底 + 深度诊断永远可见入口:`upgrader.build_upgrade_cmd` 不再 wrap `ssh root@<IP>` (客户没 root 凭据且命令本来就要在 VPS 跑); 3 个 templates 复制按钮加 `copyTextFallback` (`navigator.clipboard` → `execCommand('copy')` → `prompt()` 三层); 驾驶舱日志面板上方加 `{% if is_admin %}` 区块「Sheet 写入诊断 ▸ 立刻深度诊断」永远可见按钮 (复用 v3.0.8 modal); 升级 modal 文案 SSH → 升级命令 | [0024](docs/adr/0024-v3.0.8.2-remove-ssh-wrap-and-always-visible-deep-diag.md) |
| v3.0.8.1 | docker cp 漏同步根治 + 普通用户隐藏 admin 按钮:`docker-compose.yml::tg-web` command 从 `cp -rf templates 目录复制`(嵌套 bug 让 Flask 读旧版,v3.0.8 客户升级看不到按钮就是这个)改成 `templates/*.html` 文件级 glob;tg-web + tg-monitor command 都加 `cp README.md` + `cp release_notes.json`(`_app_version_string` / update_checker 用最新文案);**CLAUDE.md 硬规定 #8 长期修法终于落地**。`web.py::dashboard_page` 传 `is_admin` 给 template,`dashboard.html` 加 `IS_ADMIN` 全局 JS 标志,3 个 admin-only 按钮(深度诊断 / 一键修复 / 立刻重启)前端隐藏给普通成员看「请联系管理员」文字提示。后端 `/api/restart` `@login_required` 不动(保账号管理页历史按钮兼容)| [0023](docs/adr/0023-v3.0.8.1-docker-cp-rule-fix-and-admin-button-gate.md) |

## 发布流程

1. feature 分支开发 + commit
2. Codex 审阅(`codex exec -C ~/Desktop/tg-monitor-v3 ...`)
3. 修 P0/P1,写 ADR
4. 合并到 main
5. `git tag vX.Y.Z && git push --tags`
6. 每 6h 客户预警群自动收到升级通知(update_checker 机制)
7. 观察 24-48h 再推下一版

## 客户部门信息

**真实客户 / 部门列表 / 联系人 / VPS 地址** 放在 `.claude/private-notes.md`
(gitignored,不进 GitHub)。需要时本地查。

## 当前状态(2026-04-29)

- main:`v3.1`(开发中 — Sheet 后台扫描 + 客户删旧消息自动回填空位 — 解决 v3.0.8 append 被 Google 自动检测全表推高行号 + 客户手删无回填的痛点;`peers` migration V6,`write_messages` 双轨,`_sheet_position_resync_loop` 每 15 min `ws.get_all_values` 整张扫;feature flag 默认 ON 可关退 v3.0.9;0 重登 0 数据迁移)
- 之前:`v3.0.9`(已发布 — 中央台数据接口扩展:23 个 DB 业务字段全暴露给 metrics token,新增 4 个 /api/v1/* 只读 endpoint。**0 新表 0 新字段 0 数据迁移**,200+ 账号不重登)
- 之前:`v3.0.8.3`(已发布 — 修「立刻重启监听器」404 找不到容器,`/api/restart` 复用 `_start_tg_monitor()` 现成 fallback,部署遗留 COMPANY_NAME 跟实际容器名对不齐的部门也能用;诊断卡片同样加 fallback)
- 之前:`v3.0.8.2`(升级提示去 SSH 包装 + 复制按钮 HTTP/HTTPS 三层兜底 + 深度诊断永远可见入口)
- 之前:`v3.0.8.1`(docker cp 漏同步根治 + 普通用户隐藏 admin 按钮:修 v3.0.8 升级看不到按钮的根因 (`cp -rf templates 目录复制` 嵌套),CLAUDE.md 硬规定 #8 终于落地;`is_admin` flag 传 dashboard.html,3 个 admin-only 按钮前端隐藏)
- 之前:`v3.0.8`(Sheets 写入架构治本 + 卡死深度诊断 + 自助修复;`append` 替代 `update` 砍 quota 用量一半;驾驶舱「立刻深度诊断」+「一键修复」+「立刻重启监听器」三个按钮纯 web 自助;诊断关键词收紧)
- 之前:`v3.0.7.1`(救命补丁,驾驶舱诊断卡片加「立刻重启监听器」按钮 — **整合到 v3.0.8 一起发**,无独立 tag)
- 之前:`v3.0.7`(OAuth 重新授权后 Sheets 自愈,闭合 v3.0.6 的诊断—修复链路;客户在驾驶舱点「去重新授权」走完 OAuth,Sheets 写入 5-30 秒内自动恢复,无需 SSH `docker restart`)
- 之前:`v3.0.6`(驾驶舱三件套运维自助化:日志面板 + Sheet 堵塞诊断 + REMIND_DELETE_TEXT UI)
- 之前:`v3.0.5`(删除消息预警对齐 stage2 审批体验)
- 之前:`v3.0.4`(两段式预警 @ 通知修复:`@username` 走 TG 原生解析)
- 之前:`v3.0.3`(update.sh 升级时自动 Caddy 体检 + 自愈)
- 之前:`v3.0.2`(Caddy inode 自愈 + caddy-doctor.sh 自查工具,shared caddy 多部门 HTTPS 稳定)
- 之前:`v3.0.1`(两段式预警数据驱动 + 驾驶舱版本号修正 + 硬规定 #8 Docker cp 列表)
- 之前:`v2.10.25`(已发布 — 媒体存储切换 `MEDIA_STORAGE_MODE=drive|tg_archive|off`,默认 drive)
- **feature/v3.0.0 → `integration/v3.0.0-on-main` 分支集成完成**:两段式未回复预警(30min @ 商务 + 40min @ 负责人 + 违规/取消按钮 + Telethon 真名解析 + 自动升级 loop + TG 装置伪装)。从 `origin/main@32e5029` 起,已完成 7 个核心代码 commit(database / config / templates / bot / listener / tasks / main)+ 文档整合 ADR-0015 / ADR-0016
- **待做**:测试 + Codex round2 审阅 + merge main + `git tag v3.0.0` + push
- 某客户(150+ 账号)2026-04-22 15:07 线上遇到 429 配额爆,sed 止血成功(16:25),已发 v2.10.24.1 + v2.10.24.2,待客户 `./update.sh` 升级

## 升级 / 回滚命令(客户侧)

```bash
# 升级
cd /root/tg-monitor-<部门> && ./update.sh

# 回滚到上一版
cd /root/tg-monitor-<部门> && bash rollback.sh
```
