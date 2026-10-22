---
name: cite-manager
description: Deterministic citation-numbering management for numeric-bracket styles (IEEE-like [n]). Audits in-text markers against the reference list, applies renumbering mappings, compacts numbering after deletions, and inserts grep-able "citation needed" placeholders. Use when revising papers whose citations must be renumbered, merged, or marked as pending.
---

# cite-manager（纯离线，确定性规则）

## 职责边界

- 只处理数字方括号引用体系（`[1]`、`[1,2]`、`[1-3]`、全角逗号/连接号均支持）。
- 不虚构引用：新增引用一律用待补占位标记表达，由人工补齐真实条目。
- 文本变换为主；传入 `paragraphs` 时额外产出 docx 外科式回写计划（token 级）。
- 条目的物理增删在纯文本模式下支持（renumber 清空被删行）；docx 侧物理删除
  无法用 token 表达，新增条目请配合 `write_polished_docx` 的 insertions。

## run(args) 契约

- `text` / `text_file`；`language` en/cn（决定待补占位标记默认值）；
- `mode`：
  - `audit`（默认）：只读核对，不改文本；
  - `apply`：`mapping`（或 `mapping_file`）= {旧编号: 新编号}，重写正文标记，
    `reorder_list: true` 时列表条目同步原位重编号；`compress_ranges`（默认 true）
    把组内 ≥3 个连续编号压缩为 `a-b`；
- **零改动原则**：映射未命中且无可压缩跑的标记原样保留（不做逗号/空格/全半角风格归一），
  保证 docx_plan 只包含必须变化的 token，外科式回写不产生计划外文本漂移；
  - `renumber`：`remove` = 要删除的编号列表；幸存条目按原顺序重排 1..N，
    正文与列表同步，返回 `mapping_used`；
  - `pending`：`action=list` 列出全部待补标记；`action=mark` 需 `anchors`
    （锚点文本列表，每个须在文中恰好出现一次），在其后插入
    `placeholder`（默认 `[CITE-TBD]` / `[待补文献]`）；
  - `insert`：把**已核验**文献写入正文与参考文献表。`entries` / `entries_file`
    （lit-verify 的 `verified_entries`）逐条带 `anchor`（论断原句，须恰好出现一次）；
    编号默认续排（`start_number` 可指定）；`style` 选择 ieee/apa/gbt7714/bibtex；
    **只接受显式 verdict=verified**；缺少 verdict、unverified 或 rejected 一律拒绝，无绕过入口；
    `formats_out` 额外产出四种规范格式的 Markdown（`entry_formats` 同步进结果 JSON）。
- `paragraphs`：可选，段落文本列表（如 `read_paragraphs` 的 text 序列）。

## 返回

- 通用：`text`（变换后全文）、`changes`（人读变更清单）、`token_pairs`
  （实际发生的 (旧标记, 新标记)，含条目编号重写）；
- `paragraphs` 传入时：`docx_plan` = {段落下标: {"token_replacements": [...]}}，
  可直接组装为 `write_polished_docx` 的 `replacements`（外科式，含公式段落安全）；
- `audit` 模式：`cited` / `listed` / `cited_not_listed` / `listed_not_cited` /
  `duplicated_entries` / `unparsable_markers`；
- `renumber` 模式：`mapping_used` / `removed`；
- `insert` 模式：`inserted`（锚点/标记/编号/偏移）、`entry_lines`、`entry_formats`
  （每条目的 ieee/apa/gbt7714/bibtex 四种格式）、`formats_file`、`start_number`；
  传 `paragraphs` 时另给 `docx_plan`（正文标记外科替换）与 `docx_insertions`
  （条目行插入到文末段落之后）。

## 硬约束（失败即抛错）

- 映射或 remove 指向不存在的编号 → 报错；
- 被删除的编号仍被正文引用 → 报错（提示先 apply 映射到幸存条目）；
- `reorder_list` 造成列表编号重复 → 报错（提示改用 remove + renumber 收敛）；
- pending mark 的锚点出现次数 ≠ 1 → 报错。

## 典型工作流（docx 引用重排）

1. `paper-parser` 解析得到 `full_text` 与 `docx_paragraphs`；
2. `cite-manager` 的 `audit` 模式核对引用一致性；
3. `cite-manager` 的 `apply` 模式配合 `mapping={"23": 20}` 与 `reorder_list=true` 得到
   新文本、`token_pairs` 与 `docx_plan`（传 paragraphs）；
4. 合并/删除后用 `renumber` 模式与 `remove=[5, 9]` 收敛编号；
5. `write_polished_docx(source, target, docx_plan)` 外科式回写——公式段落安全。
6. 如目标文献使用 Zotero/Word 动态交叉引用，再运行
   `zotero-crossref` 的 `rebind` 模式，不得把 Zotero 域结果改成不可更新的纯文本。
