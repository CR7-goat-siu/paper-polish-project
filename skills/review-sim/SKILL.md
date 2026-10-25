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
  - 响应必须是 JSON 对象，`job_id` / `section_id` / `role` 与作业一致；
  - 每条意见必填 `issue_category` / `claim_or_defect_summary` / `exact_source_quote` /
    `line_or_paragraph_locator` / `rationale`；
  - `exact_source_quote` 空白归一后必须是对应章节原文的**逐字子串**（且 ≥
    `limits.min_quote_chars`）——锚点伪造、改写、翻译一律丢弃并记录 rejected 原因；
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
- `language`；`title`；`out`：产物目录（默认 `output/review-sim`）。
- 产出：`review-jobs.json`（每个 job 带 `section_text` 锚定底本、`prompt_file`、
  `response_file`）+ `prompts/<job_id>.prompt.txt`。
- 章节切分：Markdown/数字标题 → `utils.sections.classify_section` 类型标注；
  每个角色按配置的 sections 过滤，另加一个 `cross` 跨节比对作业（全文截断装配）。

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
- apply：`{applied_count, rejected_count, pending_count, findings[], rejected[], status}`；
- register：issue-registry registry（`issues[]` 内 review-sim 条目带硬门禁属性）。

## 质量约束

- 评审意见 severity 只允许 warning/info（严重度建议 major→warning、minor→info），
  永不产出 error/P0——语义评审不阻断，交人工判断。
- 空章节、无响应、非法 JSON 都不阻塞其他作业；单作业失败只记 rejected/pending。
