# revision-planner — 修改规划器

**职责**：把问题登记表转成可执行的修改任务清单。对应 PRD §5 阶段三「自动制定优化计划」。

**入口**：`run({"registry_file": "<registry.json>", "scorecard_file": "<card.json>", "language": "cn", "out": "<目录>"})`

**输入**
- `registry` / `registry_file`：issue-registry 产出
- `scorecard` / `scorecard_file`：quality-score 产出（用于判断缺材料维度）

**输出**
- `revision-plan.json`：`{task_count, tasks[], requires_human[], blocked_by_materials[], counts_by_priority}`
- `revision-plan.md`：人类可读计划

**每个任务必填字段（PRD §5 阶段三 逐条落实）**
`task_id` / `problem_ids` / `defect` / `target` / `allowed_scope` /
`fact_constraints` / `expected_dimension_impact` / `verification_method` / `risk`

**质量约束**
- 任务按 (来源, 维度) 聚合，减少往返轮次；顺序按 P0→P3 排列
- 规划是确定性的：只读登记表与配置，不调用模型，计划可复现可审计
- 阻断级问题与需人工确认的来源（lit-verify）**不自动改写**，转入 `requires_human`
- 缺材料维度转入 `blocked_by_materials`（PRD §6.2：证据不足时暂停对该事实的实质性改写）
- 事实约束来自 `config/issue-rules.yaml → planning.fact_constraints`

**依赖**：`utils/issue_model`；纯标准库、离线
