---
name: paper-parser
description: Parse LaTeX, Markdown, or DOCX academic papers into format-preserving section chunks. Use when a paper needs structural analysis, section-level processing, or before the polish build stage.
---

# paper-parser（纯离线）

## 职责边界

- 输入：LaTeX（.tex）/ Markdown（.md）/ Word（.docx）论文；`text` 直接传入仅支持纯文本格式。
- 输出：结构化 JSON：元信息 + 有序 chunk（content / kind / heading / offsets / est_tokens）。
- 不做语义修改、不调用模型；docx 解析记录段落索引供回写。

## DOCX 支持（无样式依赖）

1. 标准 Heading/Title 样式直接识别；
2. 无样式文档按文本启发式识别编号标题（"1 摘要"、"3.1生成器"）与裸章节词（方法/实验/结论…），
   参考文献区（References/参考文献）单独成块；
3. 结果含 `docx_paragraphs`（index/style/text）供 batch 回写对齐。

## run(args) 契约

- `input`（文件路径）或 `text`；`format` auto/latex/markdown/docx/plain；`output` JSON 路径。
- 返回：`format`、`title`、`language_hint`、`chunks[]`、`integrity.reconstructs`（无损校验）、
  `full_text`；docx 额外返回 `docx_paragraphs` 与 `docx.paragraph_count`。
