---
name: review-gate
description: Deterministic polish review gate backed by utils/guardrails (ten invariant rules: numeric conservation, polarity, citations, anchors, hedges, scope, hype, AI-flavor, structure, semantic drift). Emits verdict pass/warn/fail, structured violations and attributed retry feedback.
---

# review-gate（确定性消毒器的 Skill 入口）

## 职责边界

- 内部逻辑在 `utils/guardrails.evaluate_pair()`：对 (source, polished) 二元组执行十条
  确定性规则，**不接触 prompt/模型/usage 等生成侧信息**（CoVe factored 原则）。
- 本 Skill 保持三层兼容职责：术语检查（term-check）、规则名 → 历史 check 名映射
  （fidelity.numbers / fidelity.citations / structure.paragraphs / objectivity.hype …）、
  汇总 verdict 与归因式 retry_feedback。
- 纯离线、零模型调用；所有判定可由单测复现。

## run(args) 契约

- `source_text` / `polished_text`（或 `source_file` / `polished_file`）
- `language` en/cn；`strength` light/deep/rewrite；`mode` 仅 local（model 模式已移除）
- `references_text`：参考文献全文——启用 R3 引用编号越界检查（防杜撰角标）
- `strict`：warning 也判 fail（默认取 `quality_gates.strict_guardrails`）
- `output`：结果 JSON 路径

## 十条规则（config/<lang>-rules.yaml → quality_gates 可调）

| 规则 | 检查 | 级别 |
| --- | --- | --- |
| numeric.conservation | 数值丢失/新增/单位脱钩 | error（新增可配置） |
| numeric.polarity | 极性翻转、主客体互换 | error |
| citation.conservation | 引用丢失/新增/编号越界 | error |
| anchor.conservation | 图表公式编号、实体拼错/消失 | error/警告 |
| hedge.flip | 对冲语气单向硬化 | error |
| scope.shift | 范围限定语丢失/扩大 | 警告（strict 升 error） |
| hype.lexicon | 浮夸词库（claim-guard） | error/警告 |
| aiflavor.lexicon | AI 腔词库 + 密度硬上限 | error |
| structure.inflation | 长度比/段落/过度修正/孤儿新增句 | error |
| semantic.drift | 句级内容词重叠漂移 | 警告 |

## 输出

`verdict`（pass/warn/fail）、`violations[]`（check/severity/message/evidence）、
`metrics`、`summary{error,warning,total}`、`retry_feedback`（按规则归因，供重试 prompt）。
