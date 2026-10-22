# paper-polish-project

> **项目定位：辅助 coding harness 的论文润色、修改与投稿前预审能力层。**

版本：`v0.11.0`

本项目Coding harness 是执行主体和 Orchestrator；这个项目是它处理论文时的领域引擎、编辑协议、确定性工具和质检层。能够自主评估论文、制定修改计划、反复修改、独立复核，并在满足严格条件后自动交付的论文质量优化；

## 核心关系

```text
用户粗稿
   │
   ▼
coding harness
   ├─ 读取 AGENTS.md / SKILL.md
   ├─ 规划修改与调用顺序
   ├─ 执行模型交互
   ├─ 通过 run(args) 使用本项目能力
   ├─ 根据门禁结果重试、回滚或继续
   └─ 汇总候选稿、审计结果和待作者确认项
   │
   ▼
paper-polish-project
   ├─ paper-parser
   ├─ en-polish / cn-polish
   ├─ claim-guard / term-check / review-gate
   ├─ cite-manager / lit-search / lit-verify
   ├─ issue-registry / quality-score / revision-planner
   ├─ submission-audit
   └─ DOCX / PDF / JSON / Markdown 工件
```

## 设计约束

- **模型执行在项目之外**：coding harness 读取 `jobs.json` 中的 Prompt，调用其已有模型能力，并把响应写回 `response` 字段或 `responses/<job_id>.txt`。
- **零 API Key、零第三方依赖、零内置模型执行器**：项目不读取模型密钥，不启动 Codex CLI、Ollama 或其它模型命令，Python 代码只使用标准库。
- **唯一能力入口**：每个业务能力位于 `skills/<skill-name>/`，通过 `SKILL.md` 描述契约，通过 `run(args) -> dict` 执行。
- **Prompt 模板化**：Prompt 正文只位于 `prompts/**`，代码通过 `utils.prompt_renderer.render_template()` 渲染。
- **配置化驱动**：语言规则、领域包、术语、词库、文献源和预审阈值均来自 `config/` 与 `data/`。
- **Fail-Closed**：润色结果未通过确定性门禁且重试耗尽时回退原文，不把未过审内容静默写入候选稿。
- **人工签核不可绕过**：`final_publication_allowed` 始终为 `false`，人工签核状态由预审报告显式记录。

## Harness 调用契约

Skill 不提供面向用户的 CLI。coding harness 通过公共装载器或等价工具层执行：

```python
from utils.skill_loader import run_skill

result = run_skill("paper-parser", {
    "input": "paper.docx",
    "format": "auto",
})
```

推荐的 harness 工作顺序：

1. 读取 `AGENTS.md` 和目标任务对应的 `skills/<name>/SKILL.md`。
2. 解析论文，确认标题、章节、批注、引用、数字锚点和 DOCX 结构。
3. 根据任务生成修改计划，并选择 `light`、`deep` 或 `rewrite` 强度。
4. 调用 polish 的 build 阶段生成作业包。
5. harness 执行模型交互，将响应回填到作业包或 `responses/`。
6. 调用 polish 的 apply 阶段，读取门禁结果和 `retry-jobs.json`。
7. 根据反馈继续修改、重试、回滚或接受候选版本。
8. 调用审计、引用、术语和预审能力，整理作者确认项。
9. 向用户交付候选文件、风险清单和人工签核材料。

## 目录结构

```text
paper-polish-project/
├── AGENTS.md
├── README.md
├── config/
│   ├── cn-rules.yaml
│   ├── en-rules.yaml
│   ├── issue-rules.yaml
│   ├── literature.yaml
│   ├── submission.yaml
│   ├── zotero.yaml
│   └── domains/
├── data/
│   ├── terms/
│   ├── lexicon/
│   ├── literature/
│   ├── eval/
│   └── baseline/
├── prompts/
│   ├── cn/
│   └── en/
├── skills/
│   ├── paper-parser/
│   ├── en-polish/
│   ├── cn-polish/
│   ├── claim-guard/
│   ├── term-check/
│   ├── format-adapter/
│   ├── cite-manager/
│   ├── zotero-crossref/
│   ├── lit-search/
│   ├── lit-verify/
│   ├── review-gate/
│   ├── evaluator/
│   ├── issue-registry/
│   ├── quality-score/
│   ├── revision-planner/
│   ├── optimizer-loop/
│   ├── batch-processor/
│   └── submission-audit/
├── utils/
├── tools/
└── tests/
```

## 主要能力

| 能力 | Skill | harness 用途 |
| --- | --- | --- |
| 文档解析 | `paper-parser` | 拆章节、保留 DOCX 段落索引与批注锚点 |
| 中英文润色 | `cn-polish` / `en-polish` | build 作业包、apply 响应、门禁与装配 |
| 修改守卫 | `review-gate` / `claim-guard` / `term-check` | 数字、引用、公式、段落、浮夸与术语检查 |
| 引用管理 | `cite-manager` | 引用审计、重排、收敛、待补标记与 DOCX 回写计划 |
| 文献支撑 | `lit-search` / `lit-verify` | 发现证据缺口、检索候选、核验真实性 |
| 问题与规划 | `issue-registry` / `quality-score` / `revision-planner` | 归一 findings、评分、生成可执行修改任务 |
| 迭代支持 | `optimizer-loop` | 为 harness 提供审计、规划、复核和版本决策工具 |
| 批处理 | `batch-processor` | 由 harness 调用的 build / apply 组合调度 |
| 投稿预审 | `submission-audit` | 证据图谱、统计、图表、DOCX 渲染与人工签核 |
| Zotero | `zotero-crossref` | Word 域审计、重绑定、刷新准备 |

## 质量体系

- 客观中立：hype / informal 词库、`claim-guard`、去浮夸 Prompt 与门禁。
- 术语一致：领域术语包、修正对照表、`term-check`。
- 逻辑连贯：章节级 guidance、上下文传递和 harness 的全文规划。
- 精炼规范：conciseness 规则、informal 词库和过度修正门禁。
- 信息保真：数字、引用、公式、实体和段落锚点守恒，失败回退原文。
- 真实表达：禁止自动添加无来源数值、引用、单位或事实。

评测数据位于 `data/eval/`，归档响应位于 `data/baseline/`。当前指标用于回归监测，
不等于真实论文质量泛化结论，也不替代作者和审稿人的科学判断。

## DOCX 与交付

- 默认写 `output/`，不覆盖原始手稿。
- DOCX 回写支持段落对齐替换、token 级外科式替换和写后强制回读。
- 公式、行内对象、表格和批注采用保守策略，无法安全处理时回退并记录告警。
- 最终候选 DOCX、渲染 PDF 与交付索引由 coding harness 根据用户要求同步到项目根目录。
- 完整审计、逐页图像、日志和中间产物保留在 `output/`。

## 安全边界

- 不读取或保存模型凭据。
- 不调用第三方模型 SDK。
- 文献检索只访问白名单中的公开免密钥端点，并统一走 `utils/web_fetch`。
- `lit-verify` 采用“无证据不通过”，`cite-manager` 的插入模式只接受显式 `verified` 条目。
- 不做最终发表许可声明，不替代作者签核、伦理审查或目标期刊的最终格式确认。

## 测试

测试只验证项目内部的确定性能力和 harness 契约，不包含模型执行器测试：

```powershell
py -m unittest discover -s tests -v
```

真实公网文献基准仍为可选测试：

```powershell
$env:RUN_LIVE_LITERATURE='1'
py -m unittest discover -s tests -p 'test_live_literature.py' -v
```

当前实施状态和开放门禁见 `docs/current-status.md`。
