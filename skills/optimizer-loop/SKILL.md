# optimizer-loop — coding harness 可调用的确定性迭代工具集

**职责**：为 coding harness 提供「审计 → 规划 → 修改 → 独立复核 → 版本决策 → 停止」
所需的确定性步骤和状态记录。流程是否继续、何时调用模型由 harness 决定。
对应 PRD §5 全流程、§7 停止条件、§8 交付物。

**入口**
```python
run({
    "text_file": "<稿件>",
    "language": "cn",
    "out": "<目录>",
    "max_rounds": 3,
    "min_gain": 0.2,
    "min_index": 3.5,
    "stagnant_limit": 2,
    "responses_dir": "<已回填响应>",
})
```

**闭环（PRD 五阶段）**
1. **建立基线**：parse → issue-registry → quality-score → revision-plan，存初始快照
2. **识别排序与规划**：由 issue-registry / revision-planner 产出
3. **执行修改**：batch-processor build → harness 回填响应 → apply（局部补丁，不重写全文）
4. **独立复核**：对新版本重跑 issue-registry + quality-score（确定性，不读模型自评）
5. **版本决策**：比较新旧版本

**版本决策规则（PRD §5 阶段六）**
- 引入新的 P0/P1 → **拒绝并回滚**到历史最佳（PRD 测试 G）
- 加权指数下降 → 拒绝并回滚
- 指数上升或问题数下降且无新增 P0/P1 → 接受
- 无新增 P0/P1 但增益低于阈值 → 记为零收益轮

**停止条件（PRD §7.1，满足任一即交付）**
无未处理 P0/P1 / 达到最低质量阈值 / 连续 N 轮无有意义净改善 / 达到最大轮数。
满足停止条件只给投稿准备状态，**不保证发表**。

**交付物（PRD §8）**
`final_manuscript.md`、`quality_report.md`、`evidence_audit.md`、
`iteration_log.json`、`submission_readiness.md`

**投稿准备状态（PRD §8.5，只用明确分级）**
- `READY_FOR_AUTHOR_REVIEW`：自动检查完成，无阻断级问题，仍需作者审阅
- `CONDITIONALLY_READY`：有需作者确认事项，尚不能无条件进入投稿流程
- `BLOCKED`：存在重大科学/事实/证据/完整性问题

**响应来源**
- `responses_dir`：使用 harness 已回填的响应（真实润色场景）
- `identity`：恒等响应，用于离线验证闭环机制本身（不产生真实润色）

**依赖**：paper-parser、issue-registry、quality-score、revision-planner、
batch-processor；纯标准库、离线
