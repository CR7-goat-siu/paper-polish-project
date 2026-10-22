# issue-registry — 问题登记器

**职责**：把各审计 Skill 的 findings 归一为带唯一编号、带优先级（P0-P3）、
带质量维度的问题清单。对应 PRD §5 阶段二「识别与排序问题」与 §2.2「修改可追溯」。

**入口**：`run({"text_file": "<稿件>", "language": "cn", "out": "<目录>"})`

**输入**（二选一）
- `text_file` / `text` + `language`：由本 Skill 自行调用 claim-guard / term-check 做确定性检查（离线）
- `sources`：已算好的审计结果列表，每项 `{skill, result, location?}`；
  支持 review-gate（`violations`）、claim-guard / term-check（`findings`）、
  lit-verify（`results[*].findings`）三种结构

**输出**
- `issue-registry.json`：`{issue_count, dedup_removed, sources_consulted, counts_by_priority, counts_by_dimension, blocking, issues[]}`
- `issue-registry.md`：人类可读登记表

**质量约束**
- 每个问题有稳定 `issue_id`（`ISS-<指纹>-<序号>`）与 `fingerprint`，跨轮次可追踪
- 优先级判定全部来自 `config/issue-rules.yaml`，代码不含硬编码阈值
- 去重按指纹；`sources_consulted` 必须如实记录跑了哪些检查（供 quality-score 判断覆盖度）

**依赖**：`utils/issue_model`、claim-guard、term-check；纯标准库、离线
