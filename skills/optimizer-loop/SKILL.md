# optimizer-loop — coding harness 可调用的确定性迭代状态机

**职责**：为 coding harness 提供「审计 → 规划 → 计划驱动修改 → 独立复核 →
版本决策 → 任务级验证 → 停止 → 自动交付」的分步状态机与状态记录
（`run-state.json`，可断点续跑）。流程是否继续、何时调用模型由 harness 决定。
对应 PRD §5 全流程、§7 停止条件、§8 交付物；协作协议见 `docs/protocol.md`。

**入口（两种驱动方式）**

```python
# 分步模式（推荐 harness 使用）：一次调用推进一个阶段
run({"stage": "init", "text_file": "<稿件>", "language": "cn",
     "out": "<目录>", "max_rounds": 3})
run({"stage": "audit", "out": "<目录>"})        # 规则/词库轮间修正后重审计最佳版本
run({"stage": "build",  "out": "<目录>"})       # → awaiting_responses + pending_jobs
run({"stage": "apply", "out": "<目录>",
     "responses_dir": "<已回填响应>", "identity": False})
run({"stage": "decide", "out": "<目录>"})       # 复核 + 决策 + 任务级验证
run({"stage": "deliver", "out": "<目录>",
     "render": True, "deliver_to_root": True})  # 交付（预审默认开）
run({"stage": "status", "out": "<目录>"})       # 只读状态视图（断点观测）

# 整体模式（离线验证闭环机制 / 已有 responses_dir 时）
run_loop(text_file, language="cn", strength="deep", out_dir=<目录>,
         max_rounds=3, min_gain=0.2, min_index=3.5, stagnant_limit=2,
         responses_dir=None, identity=False)
```

**状态机阶段**

| stage | 行为 | 产物 / 状态迁移 |
| --- | --- | --- |
| init | 解析 → 全源基线审计（issue-registry × quality-score × revision-planner） | `work/round-000/`、`run-state.json`，next=build |
| audit | 对当前最佳版本重跑全量审计并刷新计划（轮间规则修正后用） | `work/audit-refresh-XXX/`，best.audit_dir 更新 |
| build | 读最佳版本的修改计划：问题按证据锚定到章节（`only_chunks` 选择性出作业；跨界证据按标题切分片段锚定）、审计问题注入 prompt 批注（`focus_issues`）、按问题优先级选强度（`chunk_strength`，规则在 issue-rules.yaml planning 节）；无锚定问题 → 直接交付 | `work/round-XXX/` 作业包；状态=awaiting_responses + `pending_jobs` |
| apply | 回填响应（`responses_dir` / `identity`）→ batch-processor apply（清洗/句级门禁/外科式回退/重试/装配/DOCX 回写） | 候选版本；next=decide |
| decide | 独立复核（重跑全量确定性审计）→ 版本决策（**新 P0/P1 按指纹判定**，序号跨轮漂移不可比）→ **任务级验证**（按指纹判定每个计划任务 fixed / still_open）→ 停止判定 | 接受则更新 best，拒绝则回滚；next=build 或 deliver |
| deliver | PRD §8 五件套 + **终稿 DOCX**（docx 输入取最佳轮次回写稿；md 输入按 Markdown 装配，含 Title/Heading/图表题注样式）+ **submission-audit 预审**（默认开；`render: true` 真实渲染验收）+ **双门禁合并状态** + `delivery-index.md`；`deliver_to_root: true` 按用户偏好复制终稿 DOCX、渲染 PDF、交付索引到根目录 | 状态=delivered |

- 计划无可自动执行任务（全部 requires_human / 缺材料 / 无锚定）时，build 直接转 deliver，不空转。
- requires_human 来源（lit-verify / lit-search / cite-manager / consistency-check）
  不自动改写：补文献走检索→核验→verified-only-insert 管线；
  图表缺失/摘要数字冲突需作者事实裁决（自动改会触发守恒门禁）。
- 每轮输入是**历史最佳版本**（局部补丁原则）；docx 输入优先以回写后的 docx 续跑。
- `identity`：恒等响应，仅用于离线验证闭环机制本身（不产生真实润色）。

**版本决策规则（PRD §5 阶段六）**
- 新增 P0/P1（按指纹）→ **拒绝并回滚**到历史最佳（PRD 测试 G）
- 加权指数下降 → 拒绝并回滚
- 指数上升或问题数下降且无新增 P0/P1 → 接受
- 无新增 P0/P1 但增益低于阈值 → 记为零收益轮

**停止条件（PRD §7.1，满足任一即交付）**
无未处理 P0/P1 / 达到最低质量阈值 / 连续 N 轮无有意义净改善 / 达到最大轮数。
满足停止条件只给投稿准备状态，**不保证发表**。

**交付物（PRD §8）**
`final_manuscript.md`、`final_manuscript.docx`、`quality_report.md`、
`evidence_audit.md`、`iteration_log.json`（含每轮任务级验证）、
`submission_readiness.md`、`delivery-index.md`、`submission-audit/`

**投稿准备状态（PRD §8.5，与预审门禁合并判定）**
- `READY_FOR_AUTHOR_REVIEW`：PRD 三级为 READY 且 submission-audit 自动门禁通过
- `CONDITIONALLY_READY`：有需作者确认事项或预审存在开放门禁
- `BLOCKED`：存在重大科学/事实/证据/完整性问题

**依赖**：paper-parser、issue-registry（全源：claim-guard / term-check /
cite-manager audit / lit-search gap / consistency-check）、quality-score、
revision-planner、batch-processor、submission-audit；纯标准库、离线
