# Architecture Decision Records (ADR)

每个非 trivial 的改动决策都要写一篇 ADR,放在这个目录。

## 格式

文件名:`NNNN-<version>-<short-description>.md`

内容结构:
```markdown
# ADR-NNNN: <简短标题>

## 背景
(为什么要做这个改动?什么问题?)

## 决策
(做了什么?具体改动)

## 原因
(为什么这么改而不是别的方案?权衡了什么?)

## 来源
(这个决策是怎么发现/推动的?Codex 审计 / 人工 / 客户反馈 / ...)

## 后果
(这个决策带来什么影响?副作用?需要后续跟进的?)
```

## 索引

| # | 版本 | 标题 | 来源 | 文件 |
|---|---|---|---|---|
| 0001 | v2.10.23 | Sheets flush 按账号分桶(修表格空白根因) | Codex 审计 + 客户反馈 | [0001](0001-v2.10.23-sheets-per-account-flush.md) |
| 0002 | v2.10.23 | Session health 加 get_me() 真 RPC 探测 | 某账号冻结案例 | [0002](0002-v2.10.23-session-health-get-me-probe.md) |
| 0003 | v2.10.23 | has_alert_today 只认真送达的(修失败不重试) | Codex 审计 Critical A2 | [0003](0003-v2.10.23-has-alert-today-sent-only.md) |
| 0004 | v2.10.23 | upsert_account 不再覆盖业务字段 | Codex 审计 Major A5 | [0004](0004-v2.10.23-upsert-account-no-business-override.md) |
| 0005 | v3.0.0 | 两段式预警用 stage 字段不改 type | Codex 审计 Critical B1/B2/B3 | [0005](0005-v3.0.0-two-stage-use-stage-column.md) |
| 0006 | v2.10.23 | `sync_headers` 单账号异常隔离(跟 0001 同逻辑) | 客户「B2/B3 填了推送空白」反馈 | [0006](0006-v2.10.23-sync-headers-per-account-isolation.md) |
| 0007 | v2.10.24 | `update.sh` orphan cleanup 放宽 + 容器缺失检测 | 客户升级撞 "container name already in use" | [0007](0007-v2.10.24-update-sh-robust-container-recreate.md) |
| 0008 | v2.10.24.1 | Sheets 读 API 配额保护:`sync_headers` + `peer_name_consistency` 间隔独立化 + 紧急开关 + 修 docstring-代码不一致 | 客户(150+ 账号)线上 429 配额爆 | [0008](0008-v2.10.24.1-sheets-read-quota-fix.md) |
| 0009 | v2.10.24.2 | 预警分页历史空白自动回填(启动立刻补 + 每小时巡检;幂等只填空栏;DB 也空的 log 清单) | 客户升级 v2.10.24.1 后发现 sed 止血期间遗留的空白 A/B 栏 | [0009](0009-v2.10.24.2-backfill-alert-history.md) |
| 0010 | v2.10.24.3 | 预警分页整行缺失自动 writeback(alerts.sheet_written + 60s loop 无限重试,保零丢失) | 客户反馈「预警分页缺整行不可接受」(429 > 6 秒写入失败后无补救机制) | [0010](0010-v2.10.24.3-alert-writeback-no-loss.md) |
| 0011 | v2.10.24.4 | `update_checker` 版本号 regex 支持四段(贪婪 `v\d+(?:\.\d+)+`)— 修后台升级弹窗显示错版本说明 | 客户截图反馈:v2.10.24.3 升级弹窗显示的是 v2.10.24 的容器冲突说明 | [0011](0011-v2.10.24.4-update-checker-four-segment-version.md) |
| 0012 | v2.10.24.5 | `release_notes.json` 里 `v2.10.24` key 改为四个 hotfix 累计说明(补旧客户展示路径)— 还没升级的旧客户也能看到正确升级说明 | v2.10.24.4 推完后用户追问「还没升级的客户能看到对的吗」 | [0012](0012-v2.10.24.5-release-notes-v2.10.24-cumulative-backfill.md) |
| 0013 | v2.10.24.6 | `release_notes.json` 文案必须白话原则(禁止文件名 / 函数名 / 技术缩写,改用业务具象词)— 客户看的是业务不是技术 | 用户反馈「这种说明太复杂客户哪看得懂,我都看不懂」 | [0013](0013-v2.10.24.6-release-notes-plain-language.md) |
| 0014 | v2.10.25 | 媒体存储切换:从 Google Drive 改为 TG 档案群(feature flag,默认保留 drive)— 避免违规内容冻结客户 Google 账号 | 客户线上反馈 Google 账号冻结 | [0014](0014-v2.10.25-media-storage-tg-archive.md) |
| 0015 | v3.0.0 | 两段式未回复预警数据层:migration V5(accounts +4 字段 + alerts.stage)+ 5 个 flag + 3 个 TG 装置伪装字段 + demo 错位 DB 兼容修复(Codex C 方案) | 客户对接需求 + Codex 审计 + demo 错位 DB 事故 | [0015](0015-v3.0.0-two-stage-alert-data-layer.md) |
| 0016 | v3.0.0 | 两段式预警推送 + callback + Telethon 真名解析 + 自动升级 loop(templates / bot / tasks / listener 四模块行为实现) | 客户对接需求 + v2.10.26 测试期客户反馈(全域文案 / Sheet 不加末列 / @ 显示真名 / TG 装置伪装) | [0016](0016-v3.0.0-two-stage-alert-push-callback.md) |
