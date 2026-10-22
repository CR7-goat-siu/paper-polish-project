# quality-score — 多维度质量评分卡

**职责**：按 PRD §4 的 10 个一级维度给出 0-4 分评分卡，每项附带评分依据、
论文位置、问题、证据、置信度与改进建议；科学真实性作为独立硬门槛。
对应 PRD §4「建立多维度论文质量评价体系」。

**入口**：`run({"registry_file": "<issue-registry.json>", "language": "cn", "out": "<目录>", "materials_provided": true})`

**输入**
- `registry` / `registry_file`：issue-registry 的产出（必须含 `sources_consulted`）
- `materials_provided`：是否提供了代码/实验数据/训练日志

**输出**
- `quality-scorecard.json`：`{weighted_index, coverage_ratio, hard_gate, scores{}, not_assessed, not_assessed_reasons, coverage, counts_by_priority}`
- `quality-scorecard.md`：人类可读评分卡

**质量约束（对应 PRD §4.4 防钻空子）**
- 评分是**确定性**的：由登记表的 P0-P3 分布推导，不调用模型、不读模型自评
- **未覆盖维度不打分**：本次没跑过能覆盖该维度的检查 → `score=null` + 原因，
  杜绝"查过没问题"被伪装成"没问题"
- **缺材料维度不打分**：method_rigor / experiment_sufficiency / reproducibility 需材料
- **硬门槛独立**：科学真实性不参与加权，存在未处理 P0/P1 即 `hard_gate.pass=false`
- 规则全部来自 `config/issue-rules.yaml`，固定且可配置

**依赖**：`utils/issue_model`；纯标准库、离线
