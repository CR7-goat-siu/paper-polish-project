# issue-registry — 问题登记器

**职责**：把各审计 Skill 的 findings 归一为带唯一编号、带优先级（P0-P3）、
带质量维度的问题清单。对应 PRD §5 阶段二「识别与排序问题」与 §2.2「修改可追溯」。

**入口**：`run({"text_file": "<稿件>", "language": "cn", "out": "<目录>"})`

**输入**（二选一）
- `text_file` / `text` + `language`：由本 Skill 自行调用**全部确定性审计源**
  （离线、不调模型、不联网），假设输入为完整论文全文：
  - `claim-guard`：浮夸 / 绝对化 / 口语（句级词面）
  - `term-check`：术语一致性
  - `cite-manager`（audit）：引用表完整性（正文引用越界 / 条目未引用 / 重复 / 不可解析标记）
  - `lit-search`（gap）：文献支撑缺口（论断缺引 / 趋势句只引旧文献 / 条目信息不全）
  - `consistency-check`（`utils/consistency`）：跨节一致性（图表引用完整性 /
    摘要百分比与正文数值一致 / 结论实体未出现在实验章节）
  - `stat-check`（`utils/stat_checks`）：统计报告一致性（t/F/χ²/Z/r 重算 p 值
    区间容差判定 / decision_error / GRIM 小样本均值颗粒度）
- `sources`：已算好的审计结果列表，每项 `{skill, result, location?}`；
- `extra_sources`：文本模式下的追加来源（如 review-sim 评审意见），与确定性审计同场登记；
  支持 review-gate（`violations`）、claim-guard / term-check（`findings`）、
  lit-verify（`results[*].findings`）、上述新来源（`findings`）等结构

**输出**
- `issue-registry.json`：`{issue_count, dedup_removed, sources_consulted, counts_by_priority, counts_by_dimension, blocking, issues[]}`
- `issue-registry.md`：人类可读登记表

**质量约束**
- 每个问题有稳定 `issue_id`（`ISS-<指纹>-<序号>`）与 `fingerprint`，跨轮次可追踪
- 优先级判定全部来自 `config/issue-rules.yaml`（含新来源的
  `source_dimension` / `dimension_overrides` / `priority_overrides`），代码不含硬编码阈值
- 去重按指纹；`sources_consulted` 必须如实记录跑了哪些检查（供 quality-score 判断覆盖度）
- 单项来源失败只记告警、不阻塞登记（仍能产出部分 registry）
- 结构类缺陷（引用越界 / 图表未定义 / 摘要数字不一致）会登记为 P0/P1，
  但其修复多需人工或文献管线（revision-planner 按 `requires_human_sources` 分流）
- **review-sim 硬门禁**：`build_registry` 出口的 `enforce_review_sim_guards` 把所有
  `source=review-sim` 的问题硬编码为 `confidence=low` / `status=needs_human` /
  `metadata.requires_human=True`（调用方输入无法绕过），配合 revision-planner 的
  `HUMAN_ONLY_SOURCES` 源级断言，模拟评审意见永不进入自动改写

**依赖**：`utils/issue_model`、`utils/consistency`、`utils/stat_checks`、
claim-guard、term-check、cite-manager、lit-search；纯标准库、离线
