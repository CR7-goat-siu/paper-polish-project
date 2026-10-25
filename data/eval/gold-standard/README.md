# 真实论文黄金标准评测集（gold-standard）

由 `tools/build_gold_standard_corpus.py` 采集与派生（arXiv 原生 HTML，公开免密钥）。
重建：`py tools/build_gold_standard_corpus.py`（全部输入走磁盘缓存，已采集论文不重抓）。

## 目录布局

| 位置 | 内容 | 是否入 git |
| --- | --- | --- |
| `output/gold-standard/corpus/` | 官方评测集全文（**仅** gold.json 收录的论文） | 否（真实论文全文不进版本库） |
| `output/gold-standard/candidates/` | 已抓取未入选的候选全文（带淘汰原因，可作缓存复用） | 否 |
| `output/gold-standard/gold.json` | 逐篇金标判定（阴性对照 + 五类阳性对照） | 否 |
| `output/gold-standard/index.json` | 全集清单：论文×领域×文件 + 候选×淘汰原因 | 否 |
| `output/gold-standard/report.md` | 评测报告（分领域 / 分缺陷召回） | 否 |
| `data/eval/gold-standard/gold-summary.json` | 汇总指标（提交进库，供离线查阅与回归对比） | 是 |
| `data/eval/gold-standard/fixtures/` | 真实论文短摘录夹具（每领域 2 篇，离线回归用） | 是 |

## 评测设计

- **阴性对照**：真实发表论文的完整抽取文本跑全源审计（issue-registry 文本模式），
  期望 0 个 P0（测量审计器在真实学术文本上的误报率）；阴性对照出现 P0 的论文
  视为抽取工件（HTML→Markdown 引用错位等），淘汰入 `candidates/` 并记录原因。
- **阳性对照**：对每篇论文植入 5 类确定性缺陷（引用越界 / 摘要假数字 /
  幽灵图表引用 / 浮夸句 / 重复文献条目），期望对应检查逐项命中（测量召回率）。
- **金标适配**：年份标注式参考文献（ACL 风格 `[2018]`，同年多条合法）不适用
  "重复编号"语义——cite-manager 另有与编号风格无关的行级重复检测
  （`duplicated_entry_line`）覆盖该风格，2026-10-25 起该风格同样植入并检出。

## 当前规模（2026-10-25，v2）

56 篇 × 4 领域：biomedical-eeg 32 / computer-vision 8 / nlp 8 / time-series 8。
阴性对照干净率 1.0（分域均 1.0）；五类植入缺陷分域召回均 1.0。
