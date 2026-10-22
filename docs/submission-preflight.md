# 投稿前预审契约

## 项目定位

本项目是辅助 coding harness 的论文润色、修改与投稿前预审能力层。
它不宣称、也不得被描述为“无需人工签核的最终发表系统”。

coding harness 负责流程编排与模型交互；发布前作者/通讯作者人工签核是硬门禁，不是失败，
也不得被自动化绕过。“可投稿候选终稿”只表示语言、结构、逻辑、术语、引用格式、
统计表达、图表标题与投稿格式达到送审候选标准，不保证录用，不替代作者签核。

## 预审包

`submission-audit` 读取结构化 JSON。字段契约见
`skills/submission-audit/SKILL.md`。预审包是连接解析、润色、文献核验、
统计登记、图表管理和 DOCX 成品的统一证据载体。

最小要求：

- `claims[]` 中的关键主张必须带 `evidence[]`；
- 每条 `verified` 证据必须有参考文献条目、证据位置、原文证据句、支持类型和强度；
- 统计数字必须有位置、含义、核验状态；可重算项必须给公式输入；
- 图表必须有标题、正文引用位置、源文件、格式、分辨率与必要说明；
- 跨页表格必须让首行重复为表头，且每个数据行不得在分页处拆断；
  DOCX 最终化可使用 `repeat_table_headers=true` 自动补齐上述属性；
- 真实渲染不得出现空白尾页；最终化可用 `trim_trailing_empty_paragraphs=true`
  安全移除末尾无内容普通段落，含分页符、公式、图形、编号或节属性的段落不得删除；
- DOCX 不得残留 `ZOTERO_BIBL`、`ZOTERO_ITEM`、`CSL_BIBLIOGRAPHY`、
  `CSL_CITATION` 等文献管理域指令；
  最终化可用 `sanitize_field_artifacts=true` 清除指令并保留后的参考文献结果；
- 如果目标文档要求 Zotero 动态引用，则不得清除有效域；必须验证
  `ZOTERO_PREF_1/2`、begin/separate/end、正文引用域和唯一文献表域完整，
  并在 Zotero 运行且 `refresh_ready=true` 后执行 Refresh；
- 同一概念、缩写和数据集名称必须使用唯一规范写法；最终化可用
  `term_replacements` 做保留公式与表格结构的显式术语归一；
- DOCX 必须是真实成品文件，渲染验收不得只检查文本。

## 状态语义

- `candidate_ready_for_author_signoff`：自动门禁通过，仍需人工签核；
- `candidate_with_open_gates`：存在自动门禁缺口、渲染限制或风险；
- `final_publication_allowed=false`：永久字段，自动化不得改写；
- `human_signoff_status=required_not_completed`：人工签核是发布前独立门禁。

## 失败闭锁

- 未读原文却标 `verified`：判 error 并降级为 `unverified`；
- 统计冲突：保留原数字，判 error，进入作者确认清单；
- 正文引用缺失图/表：判 error，进入作者补件清单；
- 真实渲染未运行或失败：不得声称页面级验收通过；
- 正文未解决占位符：阻断候选签发；
- 人工签核缺失：阻断投稿签发，但允许产出带风险清单的候选版本。

## 预审包装配与证据回填

```python
run({
    "mode": "package",
    "input_file": "paper.docx",
    "language": "en",
    "out": "output/preflight",
})
```

该命令生成 `preflight-package.json`、`evidence/evidence-jobs.json` 和逐条证据 Prompt。
 coding harness 只回填 `evidence/responses/<job_id>.txt`。`bind-apply` 会把响应写回预审包，
但缺少原文材料、证据位置、逐字引文或参考文献元数据核验时，`verified` 一律降级。

数字登记会生成 `statistics/statistics-jobs.json`。coding harness 只能补 `metric_key`、含义、
比较组与重算候选，不能修改原值或把统计标为 `verified`。用 `stats-apply` 模式回填后，
再按比较组执行确定性一致性与重算检查。

## 审计调用

```python
run({
    "mode": "audit",
    "package_file": "output/preflight-package.json",
    "docx_path": "output/candidate.docx",
    "render": True,
    "out": "output/submission-audit",
    "version": "v0.1",
})
```

无 Word/LibreOffice 或 `pdftoppm` 时，报告会显式记录限制并返回未通过真实渲染门禁。
表格分页属性属于版面硬门禁；XML 中存在属性仍必须经真实渲染确认效果。

## 组合预审

```python
run({
    "mode": "pipeline",
    "input_file": "paper.docx",
    "docx_path": "output/candidate.docx",
    "render": True,
    "out": "output/preflight-run",
})
```

该模式自动执行 package、可用响应回填与 audit。没有证据/数字分类响应时会保守失败，
但仍生成风险清单、作者确认清单、人工签核清单和质量报告。
