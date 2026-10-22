# 任务
把一条论文主张绑定到被引文献中的可核验原文证据。这是证据审计，不是主题相关性判断。

## 主张
- 主张 ID：{{claim_id}}
- 位置：{{claim_location}}
- 文本：{{claim_text}}

## 引用
- 引用 ID：{{citation_id}}
- 元数据：
{{citation_metadata}}

## 原文材料
{{source_material}}

## 规则
1. 只允许使用上方提供的原文材料；不得根据标题、摘要之外的世界知识或主题相似性推断支持。
2. `verified` 必须同时给出精确原文位置和不少于 12 个字符的逐字证据句。
3. `support_type` 只能是 `direct`、`indirect`、`background`、`opposing`。
4. `support_strength` 只能是 `high`、`medium`、`low`。
5. 原文材料缺失、不完整或不包含该主张时，必须返回 `unverified`，证据句留空并说明缺口。
6. 禁止编造页码、章节、引文、DOI 或作者。
7. 保持不确定性。仅与主题相关不能算作对具体事实主张的核验。

## 输出
只返回严格 JSON，不要代码围栏或 JSON 之外的说明：
{
  "claim_id": "{{claim_id}}",
  "reference_id": "{{citation_id}}",
  "source_location": "页码/章节/段落；无则空",
  "quote": "逐字原文证据句；无则空",
  "support_type": "direct|indirect|background|opposing|unverified",
  "support_strength": "high|medium|low",
  "verification_status": "verified|unverified",
  "reason": "简短的确定性理由",
  "missing_evidence": ["未核验时作者需要补充的材料"]
}
