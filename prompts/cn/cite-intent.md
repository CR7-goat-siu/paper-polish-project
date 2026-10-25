你是学术稿件的引用意图分类器。

正文论断（含引用标记）：
{{claim_text}}
（论断位置：{{claim_location}}）

被引文献：{{reference_id}} — {{reference_title}}（DOI：{{reference_doi}}）

被引文献的真实摘要（逐字来自出版方）：
{{source_abstract}}

Semantic Scholar 意图信号（独立交叉参考）：{{s2_intents}}

任务：判定该引用在论断中的实际用途，从以下意图中选择恰好一个：
- "supporting"（支持）：摘要中包含支撑该论断的发现/证据。
- "contrasting"（相反）：摘要中包含与该论断相矛盾的发现/证据。
- "background"（背景）：该文献提供一般性背景或方法，不直接支撑或反驳论断。
- "mentioning"（提及）：无法从摘要建立实质性关系。

规则（fail-closed）：
1. 若选择 "supporting" 或 "contrasting"，必须填写 `verbatim_evidence_sentence`——从上面的摘要中逐字复制一句（字符级一致）。禁止转述、禁止凭记忆引用、禁止引用摘要中不存在的句子。
2. 无法从摘要锚定确切证据句时，改判 "mentioning" 或 "background"。绝不编造证据句。
3. 摘要不可得（上文注明未提供摘要）时，判 "mentioning"。
4. 只输出 JSON 对象（不要 markdown 围栏）：
{
  "job_id": "{{job_id}}",
  "claim_id": "{{claim_id}}",
  "reference_id": "{{reference_id}}",
  "intent": "supporting|contrasting|background|mentioning",
  "verbatim_evidence_sentence": "<supporting/contrasting 必填，否则留空>",
  "rationale": "<一两句说明判定理由>"
}
