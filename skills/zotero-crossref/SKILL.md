---
name: zotero-crossref
description: Audit, extract, rebind, and refresh Zotero Word citation and bibliography fields. Use when a manuscript must support dynamic citation numbering, bibliography insertion/deletion, and Word/Zotero refresh instead of static bracket text.
---

# zotero-crossref

## 职责

- 识别 DOCX 中的 `ADDIN ZOTERO_ITEM CSL_CITATION` 与
  `ADDIN ZOTERO_BIBL ... CSL_BIBLIOGRAPHY` 域；
- 从已有 Zotero 文献库域提取编号、item key、CSL itemData 清单；
- 把目标 DOCX 的数字方括号引用重绑定为 Zotero Word 域；
- 把参考文献列表包裹为可刷新文献表域；
- 检查 `ZOTERO_PREF_1/2` 文档偏好、begin/separate/end 完整性和未闭合域；
- 通过 Word COM 调用 `Zotero.ZoteroRefresh`，但要求 Zotero 正在运行。

## 输入与模式

- `mode=probe`：检查 Zotero 本地连接器和 Word Zotero 插件；
- `mode=audit`：`input` / `docx`；
- `mode=manifest`：`source` / `input`，可选 `out`；
- `mode=rebind`：`source`（含有效 Zotero 域的 DOCX 或清单 JSON）、
  `target` / `input`（待绑定 DOCX）、`output` / `out`；
- `mode=refresh`：`input` / `docx`、`output` / `out`。

## 标准工作流

1. `run({"mode": "manifest", "source": "source-with-zotero.docx", "out": "zotero-manifest.json"})`
   从已有 Zotero 域文档提取稳定清单。
2. `run({"mode": "rebind", "source": "source-with-zotero.docx", "target": "polished.docx", "output": "polished-zotero.docx"})`
   将润色后文本重新绑定为 Zotero 域。
3. `run({"mode": "refresh", "input": "polished-zotero.docx", "output": "polished-zotero.refreshed.docx"})`
   在 Zotero 已运行时从 Word 内刷新引用和文献表。

## 硬约束

- 不在缺少编号映射时猜测条目；
- 不伪造 Zotero item key、URI 或 CSL itemData；
- 不把无 `ZOTERO_PREF_1/2` 的文档标记为可刷新；
- 引用域必须含 begin / instrText / separate / result / end；
- 未启动 Zotero 时不得声称已执行 `Zotero.ZoteroRefresh`。
