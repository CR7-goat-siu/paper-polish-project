---
name: review-sim
description: Simulated multi-role peer review (methodology / statistics / domain expert / novelty) rendered as fail-closed job packages; harness-model responses must anchor every finding to a verbatim source quote, which is validated before the findings are registered as low-confidence, human-only issues. Use for semantic-level review before submission.
---

# review-sim（模拟同行评审：章节 × 角色）

## 职责边界

- 只产出**语义级评审意见**（论证跳跃、方法学缺陷、贡献与内容不匹配等确定性规则
  看不见的问题），不改写正文、不拦截门禁。
- 评审以"作业包出 / 响应进"方式执行：本 Skill 渲染作业包，coding harness
  调用模型并回填 `responses/<job_id>.txt`；本 Skill 不调用任何模型、不发网络请求。
- **Fail-Closed 响应校验**（apply）：
  - 响应必须是 JSON 对象；`role` / `section_id` 必须与作业一致；`job_id` 回显
    不可靠时（异构执行器实测失败模式）身份凭据降级为 role+section_id，
    二者错必拒（`utils.response_matching` 宽容匹配 + relabel 告警）；
  - 每条意见必填 `issue_category` / `claim_or_defect_summary` / `exact_source_quote` /
    `line_or_paragraph_locator` / `rationale`；
  - `exact_source_quote` 经 anchor_norm 字符级等价后必须是对应章节原文的**逐字子串**
    （且 ≥ `limits.min_quote_chars`）——锚点伪造、改写、翻译一律丢弃并记录 rejected 原因；
  - 响应文件名与 job_id 无关时按四级归因匹配（精确文件名 / 文件名子串 /
    身份回显 / 内容锚定唯一归属），无法归属的文件记入 `unmatched_responses`。
- **硬门禁（双层，不可绕过）**：
  1. `issue-registry.build_registry` 末尾的 `enforce_review_sim_guards` 把所有
     `source=review-sim` 的问题硬编码为 `confidence=low`、`status=needs_human`、
     `metadata.requires_human=True`；
  2. `revision-planner` 的 `HUMAN_ONLY_SOURCES` 源级断言：review-sim 来源的问题
     永远落入 `requires_human`，即使配置文件被误改也不生成自动改写任务。
- 角色与章节覆盖配置在 `config/review-sim.yaml`；评审 rubric 正文只在
  `prompts/<lang>/review-sim-<role>.md`（methodology / statistics / domain_expert / novelty）。

## run(args) 契约

### mode=build（默认）

- `input_file` / `text_file` / `text`：论文输入（.docx 走 paper-parser，其余按文本切分）；
- `language`；`title`；`out`：产物目录（默认 `output/review-sim`）；
- `only_sections`：只给这些 section_id 出作业（R1 对抗闭环按改动章节评审）；
- `config`：配置文件路径或 dict（optimizer-loop 传入角色轮换后的子集）。
- 产出：`review-jobs.json`（每个 job 带 `section_text` 锚定底本、`prompt_file`、
  `response_file`）+ `prompts/<job_id>.prompt.txt`。
- 章节切分：Markdown/数字标题 → `utils.sections.classify_section` 类型标注；
  每个角色按配置的 sections 过滤，另加一个 `cross` 跨节比对作业（全文截断装配）。

### mode=diff_sections

- `old_text` / `new_text` / `language`：两版全文（确定性切分对比）；
- 产出：`{changed[], removed[], review_sections[]}`——optimizer-loop 发射
  评审周期时用 `review_sections` 做 `only_sections` 过滤。

### mode=apply

- `jobs` / `jobs_file`：build 产出的作业包；
- `responses_dir`：coding harness 回填目录（`<job_id>.txt`）。
- 产出：`findings[]`（已归一，带 `confidence=low` / `requires_human=True` / `role` /
  `section_id` / `locator` / `rationale`）、`rejected[]`（含原因）、`pending_jobs`。

### mode=register

- `apply_result` / `apply_result_file`：apply 产物；
- `sources`：可选，合并其他审计来源一起登记；`registry_out`：issue-registry 产物目录。
- 调用 issue-registry 写入问题清单（自动执行硬门禁守卫）。

## 输出

- build：`{schema_version, job_count, jobs[], status}`；
- diff_sections：`{schema_version, changed[], removed[], review_sections[]}`；
- apply：`{applied_count, rejected_count, pending_count, findings[], rejected[], warnings[], unmatched_responses[], status}`；
- register：issue-registry registry（`issues[]` 内 review-sim 条目带硬门禁属性）。

## 与 optimizer-loop 的对抗闭环（R1）

- decide 接受修订后对**改动章节**发射评审周期（`diff_sections` 切分 + cross 全文视图）；
- 意见生命周期：new →（guidance 批注驱动修订）→ reReview 未再命中 = resolved_by_reReview；
- 意见跨轮去重键 = section_id + 类别 + 引文前缀（issue_model 指纹含模型复述文本，
  不适合做意见级同一性）；
- 平台期止损（config/review-sim.yaml loop 节）：连续 2 轮无新增无收敛 → 轮换
  `alternate_roles`；连续 4 轮 → 停止循环、升级人工。

## 质量约束

- 评审意见 severity 只允许 warning/info（严重度建议 major→warning、minor→info），
  永不产出 error/P0——语义评审不阻断，交人工判断。
- 空章节、无响应、非法 JSON 都不阻塞其他作业；单作业失败只记 rejected/pending。
