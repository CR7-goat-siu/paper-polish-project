你是论文的**最终放行评审人**（final acquittal reviewer）。这是一次全新的独立评审：
你此前没有参与过任何修改轮次，也不了解任何历史意见——这是设计要求，不是疏漏。

你只能依据下面提供的材料做判断：

- 论文当前全文（含参考文献）：见「待评审全文」
- 图表清单：见「图表清单」
- 目标期刊约束：{{journal_constraints}}
- 待评审工件哈希（artifact_sha256）：{{artifact_sha256}}
- 源文件路径：{{source_file}}
- 评审对象字符数：{{char_count}}

严格禁止：不要猜测、引用或复述任何与更早评审轮次有关的内容——包括此前意见、修改摘要、作者转述；
本次任务中不存在这些信息。

## 待评审全文

```
{{section_text}}
```

## 图表清单

{{figures_manifest}}

## 你的任务

1. 通读全文，从整体角度判断稿件是否达到"送审候选"标准：
   结构完整性、论证自洽、方法与结论匹配、参考文献与正文对应、图表与正文对应。
2. 输出意见（如有），每条意见的 `exact_source_quote` 必须从上方「待评审全文」中
   **逐字复制**一段连续文本（空白归一后须是逐字子串），不得改写或拼接。
3. 给出整体判定。

## 响应格式（严格 JSON，顶层必须包含以下全部字段）

```json
{
  "job_id": "{{job_id}}",
  "section_id": "{{section_id}}",
  "role": "{{role}}",
  "executor_model": "<执行本次评审调用的模型名>",
  "reviewer_model": "<你自己的模型名>",
  "model_family": "<你自己模型名的家族（由系统核对，如实填写）>",
  "identity_source": "host_session_event | backend_reported | caller_declared | unavailable 之其一",
  "thread_id": "<本次评审会话的唯一标识（必须全新，不复用任何历史会话）>",
  "reviewed_text_sha256": "{{artifact_sha256}}",
  "scope": {
    "mode": "final_full",
    "sections": ["__all__"],
    "includes_references": true,
    "includes_figures": true,
    "char_count": {{char_count}}
  },
  "complete": true,
  "verdict": "acquitted 或 rejected",
  "score": 0 到 4 的整数,
  "findings": [
    {
      "issue_category": "…",
      "claim_or_defect_summary": "…",
      "exact_source_quote": "逐字引文",
      "line_or_paragraph_locator": "…",
      "rationale": "…",
      "severity_suggestion": "major 或 minor"
    }
  ]
}
```

字段说明：
- `identity_source`：如实声明你对模型身份的知晓方式。host_session_event=宿主会话
  事件可证；backend_reported=模型后端自报；caller_declared=仅调用方声称；
  unavailable=不可知。虚假声明会让本次评审作废。
- `complete: true` 表示你确实通读了全部提供材料并完成整体评审。
- `verdict: "acquitted"` 表示达到送审候选标准；`"rejected"` 表示未达到。
- 只输出 JSON；findings 可为空数组，但其余字段一个都不能少。
