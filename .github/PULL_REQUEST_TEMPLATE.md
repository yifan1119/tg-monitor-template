## 改动说明
<!-- 1-2 句话说改了啥、为啥改 -->

## 影响文档
<!-- 列出本次改动波及的 docs/ADR/README/release_notes 路径 — 没动写「无」 -->
- [ ] docs/adr/NNNN-xxx.md(本次决策 ADR)
- [ ] release_notes.json(版本说明)
- [ ] README.md(版本表)
- [ ] CLAUDE.md(关键决策历史 / 当前状态)

## 验证
<!-- 三块都填,按 CLAUDE.md 验证规范 -->

### 静态自查
- [ ] `python3 -m py_compile <改动文件>` 通过
- [ ] `grep` 关键改动点确认行数对得上
- [ ] `./scripts/check-secrets.sh` 没敏感词输出

### 集成 / 单元测试
- [ ] dev 容器跑通主路径
- [ ] mock 异常路径(429 / OAuth 失效 / 容器名错配 / 等)

### 回归
- [ ] 老版本现有功能不变
- [ ] 老 `.env` 不改也能跑(向后兼容)

## Codex Review
<!-- 按 CLAUDE.md 硬性规定:必须 Agent + Codex 审过 P0/P1 才合并 -->
- [ ] Codex review 完成,P0/P1 全修
- [ ] Codex 审阅 log 链接 / 摘要 ↓

```
<贴 codex 输出 P0/P1 部分>
```

## 相关 ADR
<!-- 链接到本次决策的 ADR -->
- docs/adr/NNNN-xxx.md
