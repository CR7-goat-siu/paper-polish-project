---
name: claim-guard
description: Offline objectivity and register guard — flags exaggerated or unanchored claims, absolute statements, and colloquial wording using lexicons. Use to audit a manuscript or to power the objectivity part of the review gate.
---

# claim-guard（纯离线）

## 职责边界

- 词库分两类：`data/lexicon/hype-*`（浮夸/主观）与 `data/lexicon/informal-*`（口语冗余）；
  AI 腔套话（"深刻揭示""综上所述""delve into"）由 `data/lexicon/ai-flavor-*` 承担，
  在 `utils/guardrails` R8 按密度硬上限执行（泛化结尾/模糊归因 0 容忍）。
- `requires_data_anchor=true` 的词条在匹配处无数字/百分比时按 `unanchored_severity` 升级。

- 基于 data/lexicon/hype-<lang>.json 与 informal-<lang>.json 的确定性扫描，不调用模型。
- 检测：浮夸词（空前/revolutionary…）、绝对化表述（完美/completely…）、无数据锚点的强化语
  （附近 ±60 字符无数字/表格/引用则升级 severity）、口语与冗余表达。
- 不修改原文，只输出 findings。

## run(args) 契约

- `text` / `text_file`；`language` en/cn；`mode` 仅 local；`output` JSON 路径。
- 返回：`findings[]`（id/category/found/severity/suggestion/context_snippet/occurrences）、
  `summary{error,warning,info,total}`。
