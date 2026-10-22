---
name: cn-polish
description: Chinese paper polishing in two offline stages — build renders structured prompt jobs; apply cleans model responses, runs the review gate, and assembles the polished text. Use for Chinese academic sections, abstracts, or full papers.
---

# cn-polish（纯离线，build / apply 两阶段）

与 en-polish 完全同构（契约一致），仅语言侧资源不同：prompts/cn、config/cn-rules.yaml、data/terms/*-cn.json。

## run(args) 契约

- `text` / `text_file`；`strength` light/deep/rewrite；`mode` build（默认）/apply；
- `domain` auto 或领域包 id；`jobs_file`、`responses_dir`、`retry_jobs_file`、`output`；
- build：`comments`（批注数组）或 `comments_file`（paper-parser 输出 JSON 或批注数组）——批注按
  锚定原文匹配挂到对应 chunk，经模板 `{{comments}}` 注入 prompt，是最高优先级修改要求；
  `brief` / `brief_file` 为文档级简报，经 `{{brief}}` 注入每个 chunk；未锚定批注记入 `warnings`。
  批注与简报随 job 落盘，apply 的 retry 作业自动还原（与 en-polish 契约一致）。

## 返回

- build：`jobs[]`、`domain`、`chunk_count`、`comments`（total / attached_ids / unattached_ids）、`brief`；
  apply：`polished_text`、`chunk_reports[]`、`retry_jobs[]`、
  `usage`、`estimated_cost_usd`、`diff_stats`、`warnings`。
