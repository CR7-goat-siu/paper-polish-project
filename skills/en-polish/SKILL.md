---
name: en-polish
description: English paper polishing in two offline stages — build renders structured prompt jobs; apply cleans model responses, runs the review gate, and assembles the polished text. Use for English academic sections, abstracts, or full papers.
---

# en-polish（纯离线，build / apply 两阶段）

## 职责边界

- 本 Skill 不发起任何网络请求。build 产出作业包，LLM 交互由coding harness 完成。
- build：切 chunk → 渲染 Prompt（规则 JSON + 术语库 + 修正对照表 + 章节类型 + 上块尾部上下文 + 复核反馈
  + 锚定批注 + 文档级简报）。
- apply：清洗响应（去围栏/前缀、支持原始 JSON）→ review-gate 门禁 → 失败生成带反馈 retry 作业 → 装配 + diff。

## run(args) 契约

- `text` / `text_file`；`strength` light/deep/rewrite；`mode` build（默认）/apply；
- `domain`：领域包 id 或 auto（按术语命中识别）；`title` / `section_type` / `context`；
- build：`comments`（批注数组）或 `comments_file`（paper-parser 输出 JSON 或批注数组）——批注按
  锚定原文匹配挂到对应 chunk，经模板 `{{comments}}` 注入 prompt，是最高优先级修改要求；
  `brief` / `brief_file` 为文档级简报（风格决定、范围约定），经 `{{brief}}` 注入每个 chunk；
  未锚定批注记入 `warnings` 供人工核对。批注与简报随 job 落盘，apply 的 retry 作业自动还原。
- build：`jobs_file` / `output`（写作业包 JSON）；apply：`jobs`（列表）或 `jobs_file` + `responses_dir`
  （读取 `<job_id>.txt` / `.json`），`retry_jobs_file` 输出重试作业；`output` 写润色文本 + 同名 `.diff.md`。

## 返回

- build：`jobs[]`（含 prompt/system_prompt/suggested_model/chunk_id/comments/brief）、`domain`、
  `chunk_count`、`comments`（total / attached_ids / unattached_ids）、`brief`。
- apply：`polished_text`、`chunk_reports[]`（门禁结果）、`retry_jobs[]`、`usage`、`estimated_cost_usd`、
  `diff_stats`、`warnings`（截断 / 空响应 / 门禁未过均在此）。
