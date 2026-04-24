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

## 当前状态(2026-04-23)

- main:`v3.0.4`(已发布 — 两段式预警 @ 通知修复:`@username` 走 TG 原生解析,bot inline mention 的反垃圾限制绕开)
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
