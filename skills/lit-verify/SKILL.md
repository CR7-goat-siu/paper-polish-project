---
name: lit-verify
description: Verification of candidate references against evidence records (DOI, title, authors, year, venue, volume/issue/pages) with a verified/unverified/rejected gate, plus optional online re-verification via Crossref DOI/title lookup. Use before inserting any retrieved reference into a manuscript.
---

# lit-verify（确定性核验门禁 + 可选在线二次核验）

## 职责边界

- 只判定候选条目的真实性，不改写正文。
- 证据来源（任一即可）：
  1. lit-search 落盘的原始数据源记录（条目自带 `evidence`）；
  2. coding harness 回填的数据源响应文件（`evidence_dir` 下的 `*.json`）；
  3. **在线二次核验**（`online`，默认取 config → `verification.online_reverify`）：
     对没有本地证据的候选，按 DOI 直抓 Crossref `works/{doi}`，或按标题
     `query.bibliographic` 检索取 top-1，作为证据再比对。
- **无证据不通过**：`require_evidence=true`（默认）时，找不到可核对证据的候选一律
  `unverified`，这是"禁止编造参考文献"的执行机制。
- 在线核验失败（离线 / 超时 / 未定位到记录）一律按"无证据"降级为 `unverified`——
  核验失败绝不等于通过；只有 verified 条目才允许进入正文。

## run(args) 契约

- `candidates` / `candidates_file`：候选条目（lit-search 的 `lit-candidates.json` 可直接用）；
  也接受 `verified_entries`/`entries` 结构的 JSON。
- `evidence` / `evidence_dir`：证据记录（列表、单个 JSON 或目录）；
  `evidence_source`：给证据打来源标签。
- `online`：无本地证据时是否联网二次核验（默认取配置）；
  `offline`：联网仅读缓存；`online=false` 可强制纯离线。
- `language`；`style`（引用格式，用于生成 `reference` 字段）；
  `require_evidence`（默认取配置，可显式关闭）；`out`：产物目录。

## 判定规则（config/literature.yaml → verification）

- **rejected（error）**：DOI 不成规范 / DOI 前缀命中拒绝列表 / 候选与证据 DOI 不一致 /
  标题相似度低于 `min_title_similarity` / 年份差超过 `max_year_delta` /
  作者姓氏覆盖率低于 `min_author_overlap` / 期刊名相似度低于 `min_venue_similarity` /
  卷期页与证据冲突。
- **unverified（warning）**：找不到证据（含在线核验未命中）/ 候选缺字段（证据中有）/
  年份相差 1 年（在线与正式出版差异）/ 期刊名写法差异。
- **verified**：全部核对项一致，可进入正文。

## 输出

- `results[]`：`title` / `verdict` / `findings[]` / `matched_evidence`（在线命中时带 `online: true`）/
  `entry` / `reference` / `reverify`（mode=doi|title、url、cached、fetched_at、error）
- `verified_entries[]`：可直接作为 cite-manager `insert` 的输入（唯一合法来源）；
  每条都显式带 `verdict: "verified"`，缺少 verdict 的条目会被下游拒绝
- `summary{verified,unverified,rejected}`、`reverified_count`、`online`；
  产物 `lit-verification.json` + `lit-verification.md`
