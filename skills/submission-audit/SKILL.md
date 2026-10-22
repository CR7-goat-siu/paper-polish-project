---
name: submission-audit
description: Deterministic submission preflight for evidence-to-claim binding, statistics consistency, figure/table completeness, real DOCX rendering, release risks, author confirmation, and mandatory human signoff. Use before any manuscript is described as a submission-ready candidate.
---

# submission-audit（投稿前预审与人工签核门禁）

## 定位

本 Skill 服务“辅助 coding harness 的论文润色、修改与投稿前预审能力层”。
它只判断候选终稿能否进入作者/通讯作者人工签核，不宣称“无需人工签核的最终发表系统”，
也不提供任何绕过人工签核的入口。

## 输入契约

支持三种模式：

- `mode=package`：从 `input_file`（DOCX / Markdown / 纯文本）自动生成 fail-closed 预审包；
  同时生成 `evidence-jobs.json` 与逐条证据 Prompt。自动抽取内容一律为 `unverified`。
- `mode=bind-apply`：读取 coding harness 回填的证据响应，写回原文位置、逐字引文和支持类型/强度；
  缺少原文材料、证据位置、引文或参考文献元数据核验时仍强制降级。
- `mode=stats-apply`：回填数字指标分类、比较组和重算建议；只允许引用已有数字 ID，
  禁止修改原值，也不会把任何统计数字标为 `verified`。
- `mode=pipeline`：由 harness 一次调用完成 package → 可选证据/统计响应回填 → audit。
- `mode=audit`（默认）：执行引用证据、统计、图表、DOCX 渲染与人工签核门禁。

`run(args)` 接受：

- `package` / `package_file`：结构化预审包（dict 或 JSON 路径）；
- `docx_path`：候选 DOCX；未提供时可从预审包 `docx_path` 读取；
- `out`：报告目录，默认 `output/submission-audit`；
- `render`：是否执行真实 DOCX → PDF → 页面图验收；
- `config`：可选门禁配置路径，默认 `config/submission.yaml`；
- `version`：报告版本标识。

`mode=package` 额外接受 `input_file` / `language` / `title`；
`mode=bind-apply` 额外接受 `responses_dir` / `evidence_dir` / `package_out`；
`mode=stats-apply` 额外接受 `responses_dir` / `stats_dir` / `package_out`。
`mode=pipeline` 额外接受 `input_file`、`docx_path`、`evidence_responses_dir`、
`stats_responses_dir`、`render`。

预审包核心字段：

```json
{
  "language": "cn",
  "paragraphs": [{"location": "Results ¶1", "text": "..."}],
  "claims": [{
    "id": "C1",
    "text": "...",
    "location": "Results ¶1",
    "critical": true,
    "citations": ["R1"],
    "evidence": [{
      "reference_id": "R1",
      "source_location": "PDF p.5 ¶2",
      "quote": "read source evidence ...",
      "support_type": "direct",
      "support_strength": "high",
      "verification_status": "verified"
    }]
  }],
  "references": [{
    "id": "R1",
    "doi": "10....",
    "title": "...",
    "verification_status": "verified"
  }],
  "statistics": {
    "registry": [{
      "id": "N1",
      "metric": "sample_size",
      "value": 128,
      "unit": "participants",
      "location": "Abstract",
      "meaning": "enrolled participants",
      "critical": true,
      "verification_status": "verified"
    }],
    "comparisons": [{"id": "CMP1", "number_ids": ["N1", "N2"]}],
    "recomputations": [{
      "id": "RC1",
      "operation": "percent",
      "inputs": [45, 100],
      "reported": 45
    }]
  },
  "figures": [{
    "id": "F1",
    "kind": "figure",
    "number": 1,
    "caption": "...",
    "body_reference_location": "Results ¶2",
    "source_file": "figures/fig1.svg",
    "format": "svg",
    "resolution_dpi": 300,
    "is_chart": true,
    "axis_labels": ["Time (s)", "Amplitude (µV)"],
    "units": ["s", "µV"],
    "legend": true,
    "significance": "*** p < 0.001",
    "error_bars": "mean ± SD",
    "sample_size_note": "n = 32",
    "license": "author-owned"
  }],
  "docx_path": "output/final.docx"
}
```

未提供 `claims` 时，只从带 `[n]` 的句子生成待核验主张；未提供统计登记时，
只抽取数字作为 `unverified` 线索。自动抽取不等于证据绑定，未经核验一律失败闭锁。

## 输出

- `evidence-graph.json`：主张、引用、原文证据位置、支持类型、支持强度、核验状态；
- `statistics-registry.json`：数字登记、跨位置比较、重算结果、冲突；
- `figures-manifest.json`：图/表、正文引用、源文件、轴、单位、图例、显著性、误差线、样本量、许可；
- `docx-render-report.json`：DOCX 结构检查、Word/LibreOffice PDF、页面图、空白页与限制；
- `risk-register.json`：所有 error/warning 风险；
- `author-confirmation.md`：作者需提供事实或补件的清单；
- `human-signoff.md`：作者/通讯作者硬签核清单；
- `quality-report.md` / `submission-audit.json`：门禁总分、状态与可复现记录。
- `release-manifest.json` / `reproducibility.json`：输入/输出 SHA-256、版本、渲染后端和调用参数。

状态只允许：

- `candidate_ready_for_author_signoff`：自动门禁通过，但仍未完成人工签核；
- `candidate_with_open_gates`：存在自动门禁缺口或风险。

`final_publication_allowed` 永远为 `false`。

## 硬约束

1. `verified` 证据必须同时具备参考文献条目、DOI/标题元数据、证据位置、原文证据句和有效支持类型/强度；
   缺任何一项即降级为 `unverified`，不得伪造补齐。
2. 统计冲突只登记和标记，不修改原始数字；可重算项必须给出输入、公式、计算值、报告值和容差。
3. 正文提到但清单缺失的图/表一律 error；编号跳号、源文件缺失和关键说明缺失不得静默放过。
4. 真实渲染需要系统 Word COM、LibreOffice 或已有 PDF；无渲染工具时只能给结构化检查并明确记录限制。
5. 自动页面检查只验证 PDF 和页面图非空，不替代作者对字体、重叠、表格溢出和期刊模板的最终目检。
6. 任何人工签核项缺失都阻断“投稿候选已签发”，但不会阻断带风险清单的候选版本产出。
