---
name: cite-intent
description: Prefills citation intent (supporting/contrasting/background/mentioning) for claim-citation pairs by fetching real cited-paper abstracts from Crossref/OpenAlex (with Semantic Scholar intents as a cross signal), rendering verification job packages, and fail-closed validating that any supporting/contrasting verdict carries a verbatim evidence sentence from the abstract. Use before author sign-off to upgrade manual evidence collection into confirmation.
---

# cite-intent（引用证据强度预填，fail-closed）

## 职责边界

- 只做"引用意图预填"：为正文论断 × 引用编号的每一对，拉取被引文献真实摘要，
  由 coding harness 执行模型分类，并**逐字锚定证据句**；本 Skill 不改写正文、
  不改参考文献、不调用任何模型。
- 摘要来源（全部公开免密钥，走 utils.web_fetch 缓存/限速/离线模式）：
  1. Crossref `works/{doi}`（JATS 摘要剥标签）；
  2. OpenAlex `works/{doi}` 或标题检索 top-1（`abstract_inverted_index` 还原为纯文本，
     标题检索要求相似度 ≥ 0.92 防错配）；
  3. Semantic Scholar `paper/DOI:{doi}?fields=intents` 免费意图字段作交叉信号
     （仅展示，不参与门禁）。
- **Fail-Closed 校验（apply）**：
  - intent 必须在 supporting / contrasting / background / mentioning 白名单内；
  - `job_id` / `claim_id` / `reference_id` 身份必须一致；
  - supporting/contrasting 必须携带 `verbatim_evidence_sentence` 且该句
    空白+大小写归一后**逐字**来自被引摘要；作业本身无摘要、证据句缺失或
    伪造 → 一律降级 `unverified`，原因 `no_abstract_available` /
    `unverified_no_verbatim_evidence`，绝不预填 supporting。

## 红线

- 预填条目状态为 `prefilled_pending_confirmation`：模型预填的分类与证据句只是
  候选，**最终 supporting/contrasting 判定必须由作者确认或覆写**（人工签核红线）；
  降级条目状态为 `manual_evidence_required`。
- 无摘要 ≠ 不通过也不等于通过：无摘要时模型只能判 mentioning，apply 阶段强制降级。

## run(args) 契约

### mode=build（默认）

- `text` / `text_file`：论文全文（用于抽取 claim-citation 对）；
- `references`：参考文献条目列表（含 id/number/doi/title），或 `package` /
  `package_file`（submission-audit 预审包，直接取其 references）；
- `language`；`out`（默认 output/cite-intent）；`offline`（仅读缓存）；
  `force_refresh`。
- 产出：`cite-intent-jobs.json`（每个 job 带 `source_abstract` 锚定底本、
  `abstract_source`、`s2_intents`、`prompt_file`、`response_file`）+
  `prompts/<job_id>.prompt.txt`；拉不到摘要的对进入 `fetch_errors` 并照常出作业
  （prompt 注明必须判 mentioning）。

### mode=apply

- `jobs` / `jobs_file`：build 产物（默认取 out 目录下的 cite-intent-jobs.json）；
- `responses_dir`：coding harness 回填目录（`<job_id>.txt`）。

## 输出

- `prefilled[]`：`proposed_intent` / `final_intent` / `verbatim_evidence_sentence` /
  `abstract_source` / `s2_cross_signal` / `confirmation_status` /
  `fail_reasons`；
- `rejected[]`：JSON 非法 / 身份不符 / intent 非法 / 降级记录（带原因）；
- `pending_jobs`：未回填作业；
- 汇总：`prefilled_count` / `pending_confirmation_count` / `downgraded_count`。
