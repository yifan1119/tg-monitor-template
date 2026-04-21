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
| 0001 | v2.10.23 | Sheets flush 按账号分桶(修表格空白根因) | Codex 审计 + 客户苏总反馈 | [0001](0001-v2.10.23-sheets-per-account-flush.md) |
| 0002 | v2.10.23 | Session health 加 get_me() 真 RPC 探测 | Hao 账号冻结案例 | [0002](0002-v2.10.23-session-health-get-me-probe.md) |
| 0003 | v2.10.23 | has_alert_today 只认真送达的(修失败不重试) | Codex 审计 Critical A2 | [0003](0003-v2.10.23-has-alert-today-sent-only.md) |
| 0004 | v2.10.23 | upsert_account 不再覆盖业务字段 | Codex 审计 Major A5 | [0004](0004-v2.10.23-upsert-account-no-business-override.md) |
| 0005 | v3.0.0 | 两段式预警用 stage 字段不改 type | Codex 审计 Critical B1/B2/B3 | [0005](0005-v3.0.0-two-stage-use-stage-column.md) |
