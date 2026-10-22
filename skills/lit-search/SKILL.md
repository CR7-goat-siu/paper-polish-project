---
name: lit-search
description: Detect citation gaps offline and retrieve candidate references from key-free public sources (Crossref, OpenAlex, Semantic Scholar, arXiv, Europe PMC, PubMed) with multi-round queries, emitting ranked candidate cards. Use when a paper needs citation support, related-work expansion, or reference metadata completion.
---

# lit-search（检测离线 · 检索免密钥 · 多轮策略）

## 职责边界

- 只做"发现缺口 + 检索候选 + 落盘卡片"，**不改写正文**（写正文由 cite-manager insert 负责）。
- 检索只访问 `config/literature.yaml` 白名单里的公开免密钥端点；不读取任何 API Key。
- 不判定文献真伪（那是 lit-verify 的职责），但会把原始数据源记录一并落盘作为证据。
- 是否采用某条候选由人工/下游门禁决定；本 Skill 不做"自动补引用"。

## run(args) 契约

- `mode`：`gap`（默认）/ `search` / `cards` / `package` / `auto`
  - `auto`：gap → search → cards 一条龙，供润色/审校流程主动触发检索；
- `text` / `text_file`；`language` en/cn；`domain`（如 biomedical-eeg，`auto` 走自动识别）
- `references` / `references_file`：参考文献文本（陈旧引用与信息不全检测）
- `paragraphs`：段落文本列表，用于把缺口定位到段落下标
- `audit`：cite-manager `audit` 结果，把"正文引用但未列出"转成 `missing_entry` 缺口
- `gaps` / `gaps_file`：search / package 的输入缺口；`queries`：直接给检索式
- `sources`：限定数据源；`top_k`（默认 8）；`offline`（只用缓存）；`force_refresh`
- `rounds`：多轮检索轮次（默认取 config → multi_round）
- `style`：引用格式（ieee/apa/gbt7714/bibtex，默认取配置）；`out`：产物目录

## 输出

- `gaps[]`：`gap_id` / `gap_type`(missing_citation·missing_entry·**stale_citation·incomplete_entry**) /
  `priority` / `signals` / `quote` / `paragraph_index` /
  `keywords{terms,expanded_terms,local_terms,query,boolean,suffix}`；
  `stale_citation` 附 `signals.stale_years`，`incomplete_entry` 附 `missing_fields` 与 `reference_number`；
- `candidates[]`：归一条目 + `rank_score` + `query` + `gap_id` + `anchor` + `evidence[]`（原始来源记录）；
- `round_stats[]`：逐轮（broad / precise / fresh_cited）命中数；
- `source_stats`：逐源命中数与错误；`network.stats`：请求数 / 缓存命中 / 失败数；
- 产物：`lit-gaps.json`、`lit-candidates.json`、`lit-cards.md`、`package` 模式的 `lit-jobs.json` + `lit-prompts/`。

## 缺口类型（维度一：主动检索触发）

| gap_type | 触发条件 | 处置建议 |
| --- | --- | --- |
| `missing_citation` | 信号词（绝对化/比较/实证/趋势）+ 无引用标记 | 检索补引 |
| `stale_citation` | 趋势论断的引用全部早于 `stale_citation.recency_window_years` | 补充近年前沿文献 |
| `incomplete_entry` | 参考文献条目缺 `incomplete_entry.required_fields`（默认 year/venue/pages） | 按标题检索补全元数据 |
| `missing_entry` | cite-manager audit 的"正文引用但未列出" | 补条目或删引用 |

## 多轮检索策略（维度二）

1. **broad**：关键词宽式（术语库英文 + 拉丁词 + 同义词扩展，见 `data/literature/synonyms.json`）；
2. **precise**：布尔 AND 精确式（`keywords.boolean`，或对裸检索串按词 AND）；
3. **fresh_cited**：近五年 + 按源排序（Crossref `is-referenced-by-count` / OpenAlex `cited_by:desc` /
   arXiv `submittedDate` / Europe PMC `CITED desc` / PubMed `relevance`），
   `keywords.highly_cited_min > 0` 时按被引数硬过滤。

轮次与同义词表路径均在 `config/literature.yaml → multi_round` 配置，可用 `rounds` 参数覆盖。

## 数据源与红线

- 免密钥端点：Crossref、OpenAlex、Semantic Scholar Graph、arXiv、Europe PMC、**PubMed（NCBI E-utilities，
  esearch→esummary 两步）**；不使用 Google Scholar 网页抓取（违反其条款且不稳定）。
- 所有请求经 `utils/web_fetch`：超时/重试/指数退避 + 每源最小间隔 + 磁盘缓存
  （`output/.litcache`，默认 30 天）；`offline=true` 时只读缓存，保证可复现。
- 请求头只带 User-Agent/Accept；`Authorization` 等密钥头被代码硬拒绝
  （`tests/test_no_api_keys.py` 静态守住）。

## 典型工作流

1. `run({"mode": "gap", "text_file": "paper.txt", "language": "cn", "references_file": "refs.txt", "out": "output/run1"})`
   → 得到缺口与英文检索式草稿（含布尔精确式）；
2. `run({"mode": "search", "gaps_file": "output/run1/lit-gaps.json", "language": "cn"})`
   → 多源多轮检索，得到候选与 `lit-cards.md`（四种引用格式 + 核心结论摘要）；
3. `lit-verify.run({"candidates_file": "output/run1/lit-candidates.json"})` → 真实性核验；
4. `cite-manager.run({"mode": "insert", "entries_file": "output/run1/lit-verification.json"})`
   → 只把 verified 条目写进正文与参考文献表（公式段落安全）。

coding harness 可在 `batch-processor.run()` 的 apply 参数中设置
`literature=true`，自动追加 lit-search(auto) + lit-verify 两步（只产报告，不写正文）。
