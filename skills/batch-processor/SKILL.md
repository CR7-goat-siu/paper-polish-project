---
name: batch-processor
description: Offline two-phase batch pipeline — build parses papers and emits prompt job packs; apply consumes filled responses, runs gates with feedback retries, assembles text, and writes DOCX. Use for full-paper or multi-file processing.
---

# batch-processor（纯离线，build / apply 双阶段）

## 阶段

**build**：paper-parser（docx 批注经 `read_comments` 一并抽取）→ 对每个可润色 chunk
（body/abstract/appendix/section，跳过标题块）调用 `{language}-polish` build，源文档批注按锚定
段落注入对应 chunk 的 prompt、`brief` / `brief_file` 注入每个 chunk → 合并作业（job 带
`parent_chunk_id` + chunk 内子块 id，job_id 复合唯一）→ 写 `parsed.json`、`jobs.json`、
`prompts/<job_id>.prompt.txt`、空 `responses/` 目录。

**计划驱动修订（build 可选参数，由 optimizer-loop 或 harness 显式传入）**：

- `only_chunks`：只为指定 parent chunk id 出作业（局部补丁）；其余章节在 apply
  阶段保持原文（`no_jobs` 为预期行为，不计入门禁失败）；
- `focus_issues（条目可用 `_comment_label`/`_comment_author` 覆写批注标签，评审意见标「评审意见/Review-Sim」区别于「审计问题/Auto-Audit」）`：审计问题列表（issue-registry 的 issue dict），转换为锚定批注
  注入对应 chunk 的 prompt `{{comments}}` 通道（与导师批注同优先级；
  证据片段两端的窗口省略号会被剥掉后再锚定；未能锚定的问题会在 build 告警中透出）；
- `chunk_strength`：`{chunk_id: strength}` 逐章节覆盖润色强度
  （规则见 `config/issue-rules.yaml` planning.priority_strength）。

**coding harness**：逐 prompt 调用其已有模型能力，响应写入 `responses/<job_id>.txt`
（或回填 jobs.json 的 response 字段）。

**apply**：`{language}-polish` apply（逐 chunk 门禁 + retry 作业）→ claim-guard / term-check（全文）
→ format-adapter（仅 references chunk）→ 可选 cite-manager（pipeline 显式加入，配 `cite_args`；
audit 只核对，apply/renumber 变换文本并产出 docx_plan）→ 装配全文 + 逐块 diff → docx 回写（非空段落对齐整段替换；写库层原生支持
token_replacements 外科式替换与写后强制回读比对，cite-manager 的 docx_plan 即走该通道，
任何回写失败回退纯文本）→ `retry-jobs.json`（若有）+ `result.json`。
apply 也接受 `only_chunks`（与 build 一致，用于选择性构建后的门禁失败判定）。

**文献支撑（主动触发）**：`literature=true` 在 build 阶段产出缺口报告
（`lit_mode=gap` 默认，`auto` 顺带多源检索候选，落 `<out>/<stem>/literature/`）；
在 apply 阶段自动把 `lit-search`（默认 `auto`：gap→search→cards 一条龙）与 `lit-verify`
追加到流水线末尾（只产报告与核验结论，**不改正文**；候选文件自动链接给 lit-verify）。
写入正文仍只由 cite-manager `insert` 完成，且只接受 verified 条目。

## run(args) 契约

- `inputs` / `input_glob` / `input_dir`；`language` en/cn；`phase` build/apply；`strength`；
- apply 专属：`pipeline`（默认 polish,claim-guard,term-check,format-adapter，可加入 cite-manager /
  lit-search / lit-verify / submission-audit）、`responses_dir`、`style`、`resume`、`cite_args`（cite-manager 步骤参数，如
  `{"mode":"apply","mapping":{"23":20},"reorder_list":true}`）、`literature`（apply：自动追加
  lit-search(auto)+lit-verify）、`lit_args` / `lit_verify_args`（文献步骤参数，如
  `{"mode":"gap"}` / `{"candidates_file":"..."}`）、`strict_guardrails`（warning 也判 fail，
  触发重试/Fail-Closed）、`submission_audit_args`（预审包、渲染开关与门禁配置；
  无显式预审包时生成 fail-closed 的最小骨架）；
- build 专属：`literature`（产出缺口报告）、`lit_mode`（gap/auto）、
  `only_chunks` / `focus_issues` / `chunk_strength`（计划驱动修订，见上）；
  通用：`domain`、`title`、`brief` / `brief_file`（文档级润色简报，注入每个 chunk prompt）、
  `out`、`max_workers`。

## 返回

- build：逐文件 `status=built`、`job_count`、`chunk_count` / `total_chunk_count` /
  `selected_chunks`（选择性构建的实际范围）、`focus_issue_count`、`jobs_file`、
  `prompts_dir`、`responses_dir`；
- apply：`status` ok/needs_review/error、`pending_count`（未回填作业数）、`output_text_file`、
  `output_docx_file`、usage、`guardrail_summary`（fallback/fail/warn chunk 数与违例总计）、
  `submission_audit`（自动门禁状态、人工签核状态、报告路径）；
  汇总写 `summary-<phase>.json`。
- `needs_review` 表示候选可交付但必须人工复核；coding harness 根据结果决定停止或继续重试。

## Fail-Closed 与 docx 批注告警（L3）

- 门禁 fail 且重试作业耗尽（`max_gate_retries`）的 chunk **强制装配原文**，
  `chunk_reports` 记录 `fallback=source` 与 `fallback_reason`——未过审文本绝不流入终稿；
- docx 成品对 fallback/fail 段落自动写入 Word 审阅批注（`word/comments.xml` +
  commentRangeStart/End/Reference，含具体拦截原因），人可直接在 Word/WPS 看到；
- 引用编号越界检查：apply 自动把 references chunk 文本透传给 review-gate。

## 投稿前预审（submission-audit）

`submission-audit` 是延迟步骤，始终在最终 DOCX 回写后执行，避免审计旧文件。
调用参数示例：

```python
run({
    "inputs": ["paper.docx"],
    "language": "en",
    "phase": "apply",
    "pipeline": "en-polish,claim-guard,term-check,format-adapter,submission-audit",
    "submission_audit_args": {
        "render": True,
        "package_file": "output/preflight-package.json",
    },
})
```

未提供 `package_file` / `package` 时，只从解析结果生成最小骨架；自动抽取的引用和数字
保持 `unverified`，因此不会伪造通过。自动门禁任一失败都把批处理状态置为
`needs_review`，并始终把发布前人工签核保持为未完成。
