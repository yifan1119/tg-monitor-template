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

## 当前状态(2026-04-22)

- main:`v2.10.24.2`(承接 v2.10.24.1:自动补填预警分页历史空白)
- feature/v3.0.0:Day 1 完 + Day 2 WIP(两段式预警开发中)— **需 rebase 到 v2.10.24.2 main 拿 hotfix**
- 某客户(150+ 账号)2026-04-22 15:07 线上遇到 429 配额爆,sed 止血成功(16:25),已发 v2.10.24.1 + v2.10.24.2,待客户 `./update.sh` 升级

## 升级 / 回滚命令(客户侧)

```bash
# 升级
cd /root/tg-monitor-<部门> && ./update.sh

# 回滚到上一版
cd /root/tg-monitor-<部门> && bash rollback.sh
```
