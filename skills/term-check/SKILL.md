---
name: term-check
description: Check terminology consistency in Chinese or English text against the domain glossary. Use when verifying canonical terms, nonstandard forms, or mixed usage in academic papers.
---

# term-check（纯离线）

## 职责边界

- 对照 data/terms/<domain>-<lang>.json（或历史 data/<lang>-terms.json）做确定性检查。
- issue 类型：nonstandard_use（error）/ alias_without_canonical（warning）/ mixed_usage（info）。

## run(args) 契约

- `text` / `text_file`；`language` en/cn；`domain` 领域包 id 或 auto；`mode` local；`output` JSON 路径。
- 返回：`findings[]`、`summary`。
