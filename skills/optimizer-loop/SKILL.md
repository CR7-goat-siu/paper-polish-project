# optimizer-loop — coding harness 可调用的确定性迭代状态机

**职责**：为 coding harness 提供「审计 → 规划 → 计划驱动修改 → 独立复核 →
版本决策 → 任务级验证 → 停止 → 自动交付」的分步状态机与状态记录
（`run-state.json`，可断点续跑）。流程是否继续、何时调用模型由 harness 决定。
对应 PRD §5 全流程、§7 停止条件、§8 交付物；协作协议见 `docs/protocol.md`。

**入口（两种驱动方式）**

```python
# 分步模式（推荐 harness 使用）：一次调用推进一个阶段
run({"stage": "init", "text_file": "<稿件>", "language": "cn",
     "out": "<目录>", "max_rounds": 3, "review_sim": True})
run({"stage": "audit", "out": "<目录>"})        # 规则/词库轮间修正后重审计最佳版本
run({"stage": "build",  "out": "<目录>"})       # → awaiting_responses + pending_jobs
run({"stage": "apply", "out": "<目录>",
     "responses_dir": "<已回填响应>", "identity": False})
run({"stage": "decide", "out": "<目录>"})       # 复核 + 决策 + 任务级验证；接受且启用评审 → next=review
run({"stage": "review", "out": "<目录>"})       # 消费评审周期（R1 对抗闭环）
run({"stage": "backfill", "out": "<目录>",
     "kind": "review-sim",                      # 可选 polish / review-sim / cite-intent
     "bridge_config": {}})                      # 免密钥执行桥自动回填（R7）
run({"stage": "deliver", "out": "<目录>",
     "render": True, "deliver_to_root": True,
     "allow_conditional_release": False,
     "release_waivers": [                    # 豁免=签核工件（dict 形式，str 不生效）：
        {"check": "final_review",            #   {"check, author, reason}——
         "author": "作者名", "reason": "…"}], #   落盘 waiver-record.json 可审计
     "adjudication_ledger": "<csv>", "root_dir": None})  # 交付（预审默认开）
run({"stage": "status", "out": "<目录>"})       # 只读状态视图（断点观测）

# 整体模式（离线验证闭环机制 / 已有 responses_dir 时）
run_loop(text_file, language="cn", strength="deep", out_dir=<目录>,
         max_rounds=3, min_gain=0.2, min_index=3.5, stagnant_limit=2,
         responses_dir=None, identity=False, review_sim=None, bridge=None)
```

**状态机阶段**

| stage | 行为 | 产物 / 状态迁移 |
| --- | --- | --- |
| init | 解析 → 全源基线审计（issue-registry × quality-score × revision-planner） | `work/round-000/`、`run-state.json`，next=build |
| audit | 对当前最佳版本重跑全量审计并刷新计划（轮间规则修正后用） | `work/audit-refresh-XXX/`，best.audit_dir 更新 |
| build | 读最佳版本的修改计划：问题按证据锚定到章节（`only_chunks` 选择性出作业；锚定统一走 anchor_norm 字符级等价）、审计问题注入 prompt 批注（`focus_issues`）、按问题优先级选强度（`chunk_strength`）；**活跃评审意见以「评审意见」批注注入对应章节**（guidance 驱动，永不直接改写）；无锚定问题 → 直接交付 | `work/round-XXX/` 作业包；状态=awaiting_responses + `pending_jobs` |
| apply | 回填响应（`responses_dir` / `identity`）→ batch-processor apply（清洗/句级门禁/外科式回退/重试/装配/DOCX 回写） | 候选版本；next=decide |
| decide | 独立复核（重跑全量确定性审计 + 活跃评审意见同场登记）→ 版本决策（**新 P0/P1 按指纹判定**）→ 任务级验证 → 停止判定；**接受且启用评审时对改动章节发射评审周期（R1）**；停机路径上最终版本也先评后交付 | 接受则更新 best；next=review / build / deliver |
| review | **R1 对抗闭环消费**：apply 校验评审响应（fail-closed 锚定）→ 意见生命周期（new / still_open / resolved_by_reReview）→ 活跃意见刷新进最佳版本审计 → 平台期止损（连续 2 轮无进展换角色组合，4 轮停止升级人工）；响应未齐保持 awaiting_review_sim 幂等返回 | `work/review-cycle-XXX/`；next=build 或 deliver |
| backfill | **R7 免密钥执行桥**：按状态把待回填作业交给 config/executor-bridge.yaml 配置的通道（CLI argv / 回环 HTTP），响应写 responses/<job_id>.txt，调用写穿 `bridge-log.jsonl`；失败作业保持 pending（fail-closed） | 回填报告 `{attempted, filled, skipped, failed}` |
| deliver | PRD §8 五件套 + **终稿 DOCX** + **submission-audit 预审**（默认开）+ **双门禁合并状态** + `delivery-index.md`；**Release Protocol 四条件（二轮评审第 1/2/3 条）**——release 需全部满足：①确定性审计（READY 或 CONDITIONAL+`allow_conditional_release`）②最终语义评审完成（**所有停机路径先评后交付**，含 build 无任务/无锚定/轮数超限/**apply 异常**；`reviewed_text_sha256` 必须等于最终稿哈希——评审后稿件被改动未复审即阻断，skipped 周期不计；评审未启用的运行不得 release，除非签核豁免）③submission-audit 已执行且 automated_pass（**不可豁免**）④人工 precision 门禁（`utils/adjudication` 读裁决账本：issue_id 唯一、adjudicator 署名非空、≥2 来源、manuscript_sha256 绑定当前稿件、已裁决 ≥5 且 precision ≥ 0.8）。任一未满足 → 诊断包（`*_诊断包.docx` / `*_诊断索引.md`，索引附条件状态表）；`root_dir` 覆盖根目录（测试用）；结果含 `delivery_tier` / `release_requirements` / `prd_readiness` | 状态=delivered |

- 计划无可自动执行任务（全部 requires_human / 缺材料 / 无锚定）时，build 直接转 deliver，不空转。
- requires_human 来源（lit-verify / lit-search / cite-manager / consistency-check / review-sim）
  不自动改写：补文献走检索→核验→verified-only-insert 管线；
  图表缺失/摘要数字冲突需作者事实裁决；**review-sim 意见仅作为批注引导下一轮修订，
  问题保持 needs_human 直到作者确认**（issue-registry 硬门禁 + revision-planner
  HUMAN_ONLY_SOURCES 双层不变）。
- 每轮输入是**历史最佳版本**（局部补丁原则）；docx 输入优先以回写后的 docx 续跑。
- `identity`：恒等响应，仅用于离线验证闭环机制本身（不产生真实润色）。
- `review_sim`：对抗闭环开关（None 读 config/review-sim.yaml `loop.enabled`）；
  `bridge`：执行桥开关（None 读 config/executor-bridge.yaml `enabled`，默认关）。

**版本决策规则（PRD §5 阶段六；架构评审 2026-10-25 第 1 条修复）**
- **新增 P0/P1 按指纹判定**：候选侧指纹与历史最佳做差；新指纹若与一条已消失的
  同 kind 旧指纹构成"重锚定"（location 一致，或归一上下文 difflib 相似度 ≥ 0.6，
  即编辑点扫动上下文窗口的漂移形态）则豁免，其余一律视为真新增 →
  **拒绝并回滚**到历史最佳（PRD 测试 G；纯种类集合比对的旧实现会把"同类实例
  替换"放行，已有最小反例回归测试锁定）
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
