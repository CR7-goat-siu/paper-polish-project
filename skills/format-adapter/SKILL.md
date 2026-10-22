---
name: format-adapter
description: Deterministically normalize reference lists — DOI URLs, page ranges on reference lines only. Use on references chunks or reference lists; prose is left untouched.
---

# format-adapter（纯离线）

## 职责边界

- 本地规则归一：DOI → https://doi.org/ 形式；页码区间 → 半字线（仅限参考文献特征行，
  如 [n] 开头 / pp. / vol. / 第 N 页，避免误改正文 "0-100 ms"、"2-4 epochs"）。
- 空白与空行规整。不改文献实质信息。

## run(args) 契约

- `text` / `text_file`；`language` en/cn；`style` keep/ieee/apa/gbt7714（记录目标样式）；`mode` local。
- 返回：`text`、`changes[]`、`target_style`。
