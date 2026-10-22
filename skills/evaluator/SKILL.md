---
name: evaluator
description: Offline polish-quality evaluator with four suites plus a deterministic pair scorer for iterative candidate selection. Supports oracle calibration, offline floor, injected polishers, single source/polished scoring, and anonymized A/B files for blind review.
---

# evaluator（纯离线）

## 评测套件

1. objectivity（data/eval/objectivity-<lang>.json）：植入浮夸的对抗用例 → dehype_rate + 守卫检出率。
2. terminology（terminology-<lang>.json）：退化术语 → terminology_recall + coined_word_interception。
3. grammar（grammar-en.json）：GEC 用例 → GLEU / 逐错误修正率（词级匹配）；clean 用例 → 过度修正率。
4. fidelity：全套件数字保真率（硬要求 1.0）。

## polisher 来源（零网络）

- mode=oracle：直接采用参考答案 → 校准指标上界（应≈1.0）。
- mode=offline：原样输出 → 指标地板。
- args["polisher"]：注入 callable(text, meta)->str——coding harness 可把自己模型输出逐用例回填后评测。
- `mode=pair`：对单个 `source_text` / `polished_text` 文本对打分，供自动质量循环选择候选。

## run(args) 契约

- `language` en/cn；`mode` offline/oracle/pair；`strength`；`suites` 列表；`out` 目录；`prepare_ab` 生成匿名 A/B。
- `mode=pair` 额外接收 `source_text`、`polished_text`、`domain`、`references_text`。
- 返回：`suites{}` 逐用例明细（含 output_preview 供审计）、`overall{}` 指标、报告 JSON+Markdown 路径。
- `pair` 返回：`score`（0–100）、`hard_gate_pass`、`feedback`、`metrics`（门禁、数字、长度、相似度、claim-guard）。
