# 文献检索 / 核验契约（面向 coding harness 与人工复核）

> 适用版本：paper-polish-project 2026-10-15 起（lit-search / lit-verify / cite-manager insert）。
> 红线：**零 API Key**。所有端点必须是公开免密钥接口；发现需要密钥或抓取条款不允许的数据源，
> 一律不得接入。

## 1. 链路总览

```text
正文文本
  └─ lit-search gap        → lit-gaps.json（缺口 + 英文检索式草稿）
       └─ lit-search search → lit-candidates.json + lit-cards.md（候选 + 原始证据）
            └─ lit-verify   → lit-verification.json（verified / unverified / rejected）
                 └─ cite-manager insert → 正文 [n] + 参考文献条目（仅 verified）
```

`lit-search` 的 `auto` 模式等价于 gap → search → cards 一条龙；batch-processor
在 apply 参数中设置 `literature=true` 时会自动追加 lit-search(auto) + lit-verify 两步。

四种角色分工：

| 环节 | 谁来做 | 是否联网 | 是否改正文 |
| --- | --- | --- | --- |
| 缺口检测 | `lit-search` 的 gap 模式（本地确定性规则） | 否 | 否 |
| 候选检索 | `lit-search` 的 search 模式（本地直连免密钥源）或 coding harness（package 作业包） | 是 | 否 |
| 真实性核验 | `lit-verify`（确定性门禁；无本地证据时可选在线二次核验） | 否/可选 | 否 |
| 写入正文 | `cite-manager` 的 insert 模式 | 否 | 是（仅 verified） |

## 2. 免密钥数据源白名单（config/literature.yaml）

| 源 | kind | 端点 | 备注 |
| --- | --- | --- | --- |
| Crossref | `crossref` | `https://api.crossref.org/works` | `query.bibliographic` + `rows` + `select`；DOI 核验走 `works/{doi}` |
| OpenAlex | `openalex` | `https://api.openalex.org/works` | `search` + `per-page`；配置 `contact_email` 可进 polite pool |
| Semantic Scholar | `semanticscholar` | `search` + `search/bulk` 双端点 | 无 key 时限速较严；单端点 429 时自动同源兜底 |
| arXiv | `arxiv` | `https://export.arxiv.org/api/query` | Atom XML；建议 ≥3s/请求 |
| Europe PMC | `europepmc` | `https://www.ebi.ac.uk/europepmc/webservices/rest/search` | 生命科学/医学补充 |
| PubMed | `pubmed` | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` | esearch→esummary 两步；建议 ≥1s/请求 |

**不使用**：Google Scholar 网页抓取（违反其条款且不稳定）、任何需要 API Key / OAuth 的服务。

网络行为统一由 `utils/web_fetch` 负责：超时 20s、重试 2 次（指数退避，支持 `Retry-After`）、
每源最小间隔、响应落盘缓存 `output/.litcache`（默认 30 天 TTL）。
`offline=true` 时只读缓存。永久性 4xx（如 404）不再重试；429/5xx 才按退避策略重试。
Semantic Scholar 的 Graph search 与 bulk search 互为同源 fallback。

## 2.1 多轮检索策略（config/literature.yaml → multi_round）

| 轮次 | 检索式 | 作用 |
| --- | --- | --- |
| `broad` | 关键词宽式（术语库英文 + 拉丁词 + `data/literature/synonyms.json` 同义词扩展） | 高召回 |
| `precise` | 布尔 AND 精确式（`keywords.boolean`） | 高精度 |
| `fresh_cited` | 同 broad，附加按源排序（被引数 / 提交日期）+ 近五年过滤 | 前沿高引 |

`keywords.highly_cited_min > 0` 时 fresh_cited 轮按被引数硬过滤；轮次可用 `rounds` 参数覆盖。

## 3. 缺口记录结构（lit-gaps.json）

```json
{
  "gap_id": "gap-001",
  "gap_type": "missing_citation | missing_entry | stale_citation | incomplete_entry",
  "priority": "high | medium",
  "signals": {"absolute_claim": ["首次"], "comparative": ["优于"]},
  "quote": "……（论断原文，最长 160 字符）",
  "offset": 1234,
  "paragraph_index": 42,
  "keywords": {
    "terms": ["EEG denoising", "generative adversarial network"],
    "expanded_terms": ["electroencephalography"],
    "local_terms": ["脑电去噪"],
    "query": "EEG denoising generative adversarial network electroencephalography",
    "boolean": "\"EEG denoising\" AND \"generative adversarial network\"",
    "suffix": ["EEG denoising"]
  }
}
```

- `stale_citation`：趋势论断的引用全部早于 `gap_detection.stale_citation.recency_window_years`，
  `signals.stale_years` 记录已引文献年份；
- `incomplete_entry`：参考文献条目缺 `gap_detection.incomplete_entry.required_fields`
  （默认 year/venue/pages），附 `reference_number` 与 `missing_fields`；
- 两类新缺口依赖 `references` / `references_file` 输入（references chunk 文本）。

## 4. 候选条目结构（lit-candidates.json）

在 canonical entry 之上附加排序与来源信息：

```json
{
  "title": "An Approach for EEG Denoising Based on Wasserstein GAN",
  "authors": ["Y. Dong", "X. Li"],
  "year": 2023,
  "venue": "IEEE Transactions on Neural Systems and Rehabilitation Engineering",
  "volume": "31", "issue": "9", "pages": "3524-3535",
  "doi": "10.1109/tnsre.2023.3309815",
  "doi_display": "10.1109/TNSRE.2023.3309815",
  "url": "https://doi.org/10.1109/TNSRE.2023.3309815",
  "type": "journal-article",
  "cited_by": 21,
  "source": "crossref",
  "source_id": "10.1109/TNSRE.2023.3309815",
  "abstract": "……",
  "rank_score": 2.8792,
  "query": "EEG denoising GAN",
  "gap_id": "gap-002",
  "anchor": "该论断原句（用于插入标记）",
  "evidence": [{"...": "产生该候选的原始数据源记录（canonical 化）"}]
}
```

coding harness 回填 package 作业包时，每个 `lit-prompts/lit-<gap_id>.prompt.txt`
应回答一个 JSON：`{gap_id, keywords_used, boolean_query, candidates[]}`，字段与上表一致。
把回答保存为 `lit-responses/<gap_id>.json`，即可作为 `lit-verify` 的 `evidence_dir` 输入。

## 5. 核验结论（lit-verification.json）

- `verified_entries[]`：唯一允许写入正文的输入（`cite-manager` 的 insert 模式会再校验 `verdict`）；
  每条都显式携带 `verdict: "verified"`，缺少 verdict 的条目不会被放行。
- `results[].findings[]`：`{field, level(error|warning), message, detail}`。
- `results[].reverify`：在线二次核验溯源（`mode=doi|title`、`url`、`cached`、`fetched_at`、`error`）；
  仅在没有本地证据且 `online` 开启时出现。
- 判定阈值集中在 `config/literature.yaml → verification`：
  `min_title_similarity` / `min_venue_similarity` / `max_year_delta` / `min_author_overlap` /
  `require_evidence` / `reject_doi_prefixes` / `online_reverify` / `crossref_works_endpoint`。

## 6. 写入规则（cite-manager insert）

- 只接受显式 `verdict=verified`；缺少 verdict、`unverified` 或 `rejected` 一律拒绝，无绕过入口。
- 正文锚点必须"恰好出现一次"，否则报错——避免标记插到错误位置。
- 参考文献编号默认续排（现有列表最大编号 + 1），可用 `start_number` 指定。
- `entry_formats` / `formats_out`：每条目的 IEEE / APA / GB/T 7714 / BibTeX 四种格式。
- docx 回写：正文标记走 `docx_plan`（token 级外科替换，含公式段落安全），
  新增条目行走 `docx_insertions`（在文末段落之后插入），任何失败回退纯文本并告警。

## 7. 真实公网基准

`tests/test_live_literature.py` 默认跳过；设置 `RUN_LIVE_LITERATURE=1` 后执行真实联网回归：

```powershell
$env:RUN_LIVE_LITERATURE='1'
py -m unittest discover -s tests -p 'test_live_literature.py' -v
```

覆盖六源连通率、10 篇真实文献正样本、5 个伪造/篡改负样本、arXiv ID 保留和
`batch-processor` 未验证条目防穿透。
