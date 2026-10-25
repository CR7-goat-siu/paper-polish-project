# 人工裁决协议（F5 层 3）：启发式告警 precision 的独立估计

> 目的：把"可投稿候选"的判定从模型自评中独立出来。检测器召回（F5 层 1）证明
> 有回归价值；本协议用作者对告警的逐条裁决估计 **precision**，作为放行条件的
> 人工证据轴。

## 1. 裁决对象与抽样

- 对象：optimizer-loop 每轮 `evidence_audit.md` / issue-registry 中 **P1 与 P2**
  级告警（P0 数量少且全部人工处理，不入抽样）。
- 抽样：每轮每来源（claim-guard / consistency-check / review-sim / stat-check /
  cite-manager / lit-search）至少抽 5 条；不足全取。随机种子记录在账本头部。
- 边界：review-sim 意见裁决的是"是否为真实缺陷"（它们本来就不自动改写）。

## 2. 判定标准（三值，不用模糊两值）

| 判定 | 定义 |
| --- | --- |
| TP（真缺陷） | 指向的问题在原文中真实存在，且按告警建议处理会改进稿件 |
| FP（误报） | 原文无此问题，或处理建议会损害原意（规则误触发 / 体裁误配） |
| UNCLEAR | 需要稿件外部材料才能裁决（如实验记录核对）——计入"待材料"而非 precision 分母 |

## 3. 账本格式

`adjudication-ledger.csv`，列：
`issue_id, source, check, priority, evidence_snippet, adjudication(TP/FP/UNCLEAR), note, adjudicator, date`。
首批种子由运行产物自动导出（见各运行目录 `adjudication-ledger.csv`）。

## 4. precision 的计算与用途

- `precision(source) = TP / (TP + FP)`，分母不含 UNCLEAR；UNCLEAR 占比单独报告。
- 用途一（规则调阈）：某 source 的 FP 集中于特定 check 时，优先调整
  `config/issue-rules.yaml` 对应 `priority_overrides` / 阈值，而不是全局放宽。
- 用途二（放行条件）："可投稿候选"的完整定义 = **F5 层 2（跨模型环内评审）
  通过 + 本协议累计 precision ≥ 0.8 且 FP 集中的 check 已修**。两项独立于
  执行模型，作者签核仍是最终门禁。

## 5. 首批种子数据（2026-10-25）

- 来源运行：`output/optimizer-loop-e2e-shiqing/`（第二份真实手稿完整闭环）。
- 噪声画像（F5 层 1，8 篇金标夹具）：召回 35/35；干净文本 P0/P1 误报率 0.75，
  噪声集中于 `cite-manager::uncited_entry`（摘录体裁截断伪影）与
  `lit-search::incomplete_entry`——这两类在**全文运行**中不适用摘录豁免逻辑，
  是首批裁决的重点验证对象。
