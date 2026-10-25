# AGENTS.md — paper-polish-project 主 Agent 定义

> 本文件面向进入该仓库的 Agent，描述项目角色、标准 SOP 与 Skill 调度方式。

## 1. 项目角色

**项目定位：辅助 coding harness 的论文润色、修改与预审能力层。**

本项目默认不自行启动模型，也不负责代替 coding harness 做任务编排。
coding harness 是执行主体，负责读取 `AGENTS.md` / `SKILL.md`、规划修改、调用模型、使用工具和回填响应；
本项目负责提供领域规则、Prompt、确定性变换、门禁、文档读写与审计结果。
确需项目侧自动执行模型交互时，只能经**免密钥执行桥**（本地推理服务或用户已登录的 CLI，
凭据由该通道自持，项目代码与配置不接触任何凭据），且调用必须写穿同一套作业包与审计文件。

本项目不得宣称、也不得被描述为“无需人工签核的最终发表系统”；
发布前作者/通讯作者人工签核是硬门禁，不是失败，也不得被自动化绕过。“可投稿候选终稿”
仅表示语言、结构、逻辑、术语、引用格式、统计表达、图表标题与投稿格式达到送审候选标准，
不保证录用，不替代作者签核。

本项目是一个**零 API Key**的学术论文预处理、后处理与投稿前预审能力层，负责：

1. 通过 `paper-parser` 解析 LaTeX / Markdown / DOCX 论文，得到可独立加工的章节块（docx 无损读写）。
2. 通过 `en-polish` / `cn-polish` 的 **build 阶段**切分 chunk、渲染结构化 Prompt，产出润色作业包（jobs）。
3. **coding harness 负责实际的 LLM 交互**：读取作业包中的 prompt 调用模型，把响应回填到 `response` 字段或 `responses/<job_id>.txt`；本项目默认不调用任何模型，唯一例外是经用户授权的免密钥执行桥（见第 2 节），其调用仍写穿同一套审计文件。
4. 通过 `en-polish` / `cn-polish` 的 **apply 阶段**清洗响应、执行 `review-gate` 门禁、生成带反馈的重试作业、装配全文与 diff 报告，并回写 docx。
5. 通过 `claim-guard` / `term-check` / `format-adapter` 做客观性守卫、术语校验与格式归一（全部本地确定性规则）。
6. 通过 `evaluator` 用结构化测试集量化润色质量（对抗性客观性 / 术语召回 / GLEU / 保真度）。

## 2. 不可违背的工程约束

- **唯一硬红线——零 API Key**：本仓库不读取任何 Key / Token / Secret，不发送 Authorization 头，不使用任何需要密钥的服务；模型调用若经执行桥完成，凭据必须由该通道自持（本地推理服务 / 用户已登录 CLI），项目代码与配置不得接触凭据。文献检索只允许访问**公开免密钥**学术数据源（Crossref / OpenAlex / Semantic Scholar / arXiv / Europe PMC / PubMed），且必须走 `utils/web_fetch`（超时重试 + 每源限速 + 磁盘缓存 + 离线模式），不得抓取 Google Scholar 等条款不允许的页面。
- **依赖策略**：Python 标准库优先；允许在收益明确时引入第三方依赖（解析、协议、评测等），但任何依赖不得读取、携带或传输密钥，所有网络流量仍必须经 `utils/web_fetch`（直连网络模块禁令是零 Key 红线的执行机制，见 `tests/test_no_api_keys.py`）。
- **模型交互默认走"作业包出、响应回填进"的文件契约**（由 coding harness 完成）；经用户授权的免密钥执行桥是唯一例外——凭据由通道自持，调用必须写穿同一套 jobs / responses / 审计文件，`utils/guardrails` 门禁与 fail-closed 语义不变。
- 中英文润色管线相互独立：语言差异只体现在 `prompts/cn/`、`prompts/en/`、`config/cn-rules.yaml`、`config/en-rules.yaml` 与对应数据中。
- 所有业务能力以 `skills/<skill-name>/` 形式实现，每个 Skill 必须有 `SKILL.md` 与可独立调用的 `run(args) -> dict` 入口。
- Prompt 一律模板化：正文只允许出现在 `prompts/**`，Skill 内禁止硬编码提示词正文；代码只通过 `utils.prompt_renderer.render_template()` 渲染。
- 配置化驱动：模型路由建议、价格、规则强度、术语库、领域包、词库均来自 `config/` 与 `data/`，不允许散落在业务代码中。
- 变更任何 Skill 时保持其输入 / 输出契约不变；变更契约时必须同步更新 `SKILL.md`、调用方与测试。
- 润色结果必须经 `utils/guardrails` 十条规则审查：fail 且重试耗尽强制 Fail-Closed 回退原文；
  疑似杜撰（新增数值/引用/单位）默认判 error（`fabrication_suspects_as_errors`）。
- 如无明确用户授权，不得用润色结果覆盖原始手稿：默认写 `output/`。
- 用户交付偏好：每轮最终候选 DOCX、渲染 PDF 与交付索引除写入 `output/` 外，
  必须同时复制到项目根目录，便于直接打开；根目录只放最终交付文件，
  完整审计、逐页图像、日志和中间产物仍保留在 `output/`。
- 目录约定：真实手稿历史工作稿与原始输入放 `manuscripts/`；
  历史运行产物归档 `output/archive/`；协作协议见 `docs/protocol.md`，
  PRD 定稿见 `docs/prd.md`。

## 3. 标准润色 SOP（离线双阶段）

1. **解析（paper-parser）**：识别 LaTeX / Markdown / DOCX，抽取标题、章节、参考文献，chunk 无损可重建；docx 同时记录段落索引与批注（`read_comments`）供回写与注入。
2. **领域识别**：按术语命中自动选择 `config/domains/` 领域包（generic / deep-learning / biomedical-eeg，可扩展）。
3. **作业构建（*-polish build）**：逐 chunk 渲染 Prompt（规则 JSON + 术语对照 + 修正对照表 + 章节类型 + 上一块尾部上下文 + 复核反馈 + 按锚定段落挂到本 chunk 的导师批注 `{{comments}}`（最高优先级）+ 文档级简报 `{{brief}}`，均随 job 落盘供 retry 还原），写出 `jobs.json` 与 `prompts/`。
4. **coding harness 执行**：按作业调用模型；响应可保留原始 JSON（自动抽取文本与截断状态）。
5. **响应应用（*-polish apply）**：清洗 → review-gate 门禁（数字 / 引用 / 公式 / 段落 / 长度 / 相似度 / 浮夸残留 / 术语残留）→ 失败自动生成带反馈的重试作业 → 装配全文。
6. **质量提升循环（由 harness 经 optimizer-loop 状态机驱动，协议见 docs/protocol.md）**——decide 接受修订后自动对改动章节发射 review-sim 评审周期（R1 对抗闭环：意见经锚定校验入登记表 → 下一轮以「评审意见」批注引导修订 → reReview 判定收敛；意见保持 requires_human，永不自动改写）；stage=backfill 可经免密钥执行桥（CLI/本地服务，零 Key）自动回填响应（R7）：`stage=init` 建立全源基线审计（issue-registry 文本模式自动跑 claim-guard / term-check / cite-manager audit / lit-search gap / consistency-check / stat-check，结构缺陷如引用越界 / 图表未定义 / 摘要数字不一致也登记为 P0-P3）→ `stage=build` 按修改计划**选择性出作业**（only_chunks 局部补丁 + 审计问题经 `{{comments}}` 通道注入 prompt + 按问题优先级选强度）→ harness 回填响应 → `stage=apply`（门禁 fail 且重试耗尽时句级外科修复优先，整块回退为最后手段）→ `stage=decide` 独立复核（新增 P0/P1 按**指纹**判定拒绝并回滚；按指纹做任务级验证 fixed/still_open）→ 循环至停止条件 → `stage=deliver`（五件套 + 终稿 DOCX + submission-audit 预审 + 双门禁合并状态 + 交付索引，`deliver_to_root` 复制到根目录）。`run-state.json` 支持断点续跑；轮间修正规则/词库后用 `stage=audit` 刷新计划。
7. **守卫与归一（claim-guard / term-check / format-adapter / cite-manager / zotero-crossref）**：客观性 findings、术语 findings、references chunk 的 DOI / 页码归一；引用编号重排 / 合并 / 收敛 / 待补标记走 cite-manager（含 docx_plan）；需要动态文献管理时由 zotero-crossref 保留或重建 Zotero Word 域。
8. **文献支撑（lit-search / lit-verify / cite-manager）**：检测缺少文献支撑的论断（信号词 + 无引用标记，另覆盖陈旧引用与参考文献信息不全）→ 把论点转成英文关键词与布尔检索式（同义词扩展），按多轮策略（宽式 / 精确 / 近五年高引）向公开免密钥数据源检索候选 → 确定性核验（DOI/标题/作者/年份/期刊/卷期页；无证据一律不通过，可选按 DOI/标题联网二次核验）→ 仅把 verified 条目用 `cite-manager` 的 insert 模式写入正文与参考文献表。残缺条目的自动补全走 lit-search `refill`（严格匹配 + 歧义中止 + 计划打标 `auto_filled_with_source`）；引用意图预填走 `cite-intent`（supporting/contrasting 必须逐字锚定被引摘要证据句，否则 fail-closed，结果待作者确认）。
9. **产出**：润色全文、逐 chunk diff 报告、质量循环报告、docx 回写（段落对齐整段替换，或 token_replacements 外科式替换 + 写后强制回读比对）、result.json（pending/needs_review 状态供人工复核）。

## 4. Skill 调度表

| Skill | 职责 | harness 契约 | 主要依赖 |
| --- | --- | --- | --- |
| paper-parser | LaTeX / Markdown / DOCX 解析、章节拆分、docx 段落索引 | `run(args)` | 无模型依赖 |
| en-polish | 英文润色 build/apply 两阶段 | `run(args)` | rules, domain pack, terms, lexicon |
| cn-polish | 中文润色 build/apply 两阶段 | `run(args)` | 同上（cn 侧） |
| claim-guard | 客观性 / 语域守卫（hype + informal 词库） | `run(args)` | data/lexicon/* |
| term-check | 术语一致性校验 | `run(args)` | data/terms/* |
| format-adapter | references chunk 的 DOI / 页码归一；`mode=refill_plan` 产出文献补全的 Token 级改写计划（auto_filled_with_source 打标，不改正文） | `run(args)` | 本地规则 |
| cite-manager | 引用编号管理：audit / apply 映射重排 / renumber 收敛 / pending 待补标记；传 paragraphs 时产出 docx_plan 外科式回写计划 | `run(args)` | 本地规则 |
| zotero-crossref | Zotero/Word 域审计、清单提取、纯文本引用重绑定、Word 刷新 | `run(args)` | `utils/zotero_crossref`, Word COM, Zotero 本地 API |
| lit-search | 文献缺口检测（缺引/陈旧/信息不全）+ 免密钥六源多轮检索 + 候选文献卡片；`mode=refill` 把残缺条目升级为"检索→严格匹配（标题相似度/第一作者姓氏/年份±1）→lit-verify→Token 级补全计划"，歧义一律中止 | `run(args)` | config/literature.yaml, utils/web_fetch, utils/citation_check |
| lit-verify | 候选文献真实性核验门禁（无证据不通过；可在线二次核验） | `run(args)` | utils/citation_check |
| review-gate | 润色结果复核门禁（内部切换 utils/guardrails 十条确定性规则） | `run(args)` | utils/guardrails, rules.quality_gates |
| evaluator | 润色质量评测（objectivity / terminology / grammar / fidelity） | `run(args)` | data/eval/* |
| issue-registry | 问题登记：归一各审计 findings 为 P0-P3 清单（PRD §5 阶段二）；文本模式自动跑全源审计（claim-guard / term-check / cite-manager audit / lit-search gap / consistency-check / stat-check）；`enforce_review_sim_guards` 把 review-sim 来源硬编码为 requires_human | `run(args)` | utils/issue_model, utils/consistency, utils/stat_checks, claim-guard, term-check, cite-manager, lit-search |
| quality-score | 多维度质量评分卡 0-4 分 + 科学真实性硬门槛（PRD §4） | `run(args)` | utils/issue_model, config/issue-rules.yaml |
| revision-planner | 修改规划：问题清单 → 可执行任务（PRD §5 阶段三） | `run(args)` | utils/issue_model, config/issue-rules.yaml |
| optimizer-loop | 迭代状态机：init/build/apply/decide/review/backfill/deliver 分步 + run-state.json 断点续跑；每轮按计划选择性出作业（only_chunks + focus_issues 批注注入 + chunk_strength），decide 做版本决策（**按指纹 + 漂移豁免**，同 kind 异处新实例必拒）与任务级验证；**R1 对抗闭环：decide 接受后对改动章节发射 review-sim 评审周期，意见生命周期（new/still_open/resolved）+ 平台期止损（2 轮换角色 / 4 轮升级人工）**；Release Protocol 四条件交付（确定性审计 / final_acquittal accepted / submission-audit 不可豁免 / 人工 precision 门禁），全部停机路径先评后交付；评审证据链（身份契约 / fresh_only / 哈希绑定 / trace 验签）见 docs/protocol.md §3.6；stage=backfill 走免密钥执行桥自动回填（R7）；不主动调用模型 | `run(args)` | 上述 Skill 的 `run()` 契约, config/executor-bridge.yaml, utils/executor_bridge |
| batch-processor | 批量 build / apply 调度；支持计划驱动修订参数（only_chunks / focus_issues / chunk_strength） | `run(args)` | 上述 Skill 的 `run()` 契约 |
| submission-audit | 投稿前预审：证据图谱 / 统计（含 stat-check 重算与 GRIM）/ 图表 / DOCX 渲染 / 期刊合规（`journal=<id>` 逐项 PASS/FAIL）/ 人工签核 | `run(args)` | config/submission.yaml, config/journals/*, utils/stat_checks, utils/journal_compliance |
| review-sim | 模拟同行评审（章节 × methodology/statistics/domain_expert/novelty 作业包；响应逐字锚定原文 fail-closed 校验 + job_id 宽容匹配；意见强制 confidence=low + requires_human，永不自动改写；diff_sections 模式支撑按改动章节评审） | `run(args)` | config/review-sim.yaml, prompts/*/review-sim-*.md, utils/sections, utils/response_matching, issue-registry |
| cite-intent | 引用证据强度预填：Crossref/OpenAlex 拉被引摘要（+S2 intents 交叉信号）→ 作业包 → supporting/contrasting 必须逐字锚定证据句，否则 fail-closed 降级 unverified；结果为 prefilled_pending_confirmation 待作者确认 | `run(args)` | config/literature.yaml, utils/web_fetch, utils/citation_check |

## 5. 质量评估体系（对应四大核心特点）

| 特点 | 落地机制 | 评测方法 |
| --- | --- | --- |
| 客观中立 | hype/informal 词库 + claim-guard + prompt 去浮夸指令 + 门禁 hype_residue | 对抗性测试集（data/eval/objectivity-*.json），指标 dehype_rate |
| 术语精准一致 | 领域包术语库 + prompt 修正对照表 + term-check | 退化术语测试集，指标 terminology_recall / coined_word_interception |
| 逻辑连贯 | section_guidance 按章节注入 + coherence 规则 + chunk 上下文 | 人工盲评（evaluator 的 prepare_ab 生成匿名 A/B） |
| 精炼规范 | conciseness 规则 + informal 词库 + 过度修正门禁 | GEC 测试集，指标 GLEU / fix_rate / overcorrection_rate |
| 信息保真（底线） | review-gate 数字/引用/公式/段落保真 + 失败重试 | 全套件 number_preservation_rate = 1.0 为硬要求 |

评测基线（archived 归档重放，2026-09-26，覆盖 light/deep/rewrite × en/cn 六档，
响应与报告存 data/baseline/，可用当前代码离线重算）：
dehype 全档 1.0；terminology_recall 0.958–1.0；coined_word_interception 0.958–1.0；
fix_rate 全档 1.0；overcorrection 全档 0；number_preservation 全档 1.0；
GLEU（近似实现）en 0.64–0.73、cn 0.31–0.70，仅作同档相对比较。

## 6. 开发与变更守则

- 分支纪律：功能开发走 feature 分支 + 自审 PR 合入 main，不直推 main；
  每次合入前跑全量离线套件（CI 也会跑）。
- 变更入 `CHANGELOG.md`（Keep a Changelog 格式）；文档同步由
  `tests/test_docs_sync.py` 闸门强制（调度表覆盖全部 Skill、模板有消费者）。
- 新增三档强度或调整规则时只改 `config/*-rules.yaml`，并同步更新受影响模板与测试。
- 新增领域：在 `config/domains/<id>.yaml` + `data/terms/<id>-{lang}.json` 各加一个文件即可，无需改代码。
- 新增 Skill：先写 `SKILL.md` 与输入输出契约，再实现 `run(args) -> dict`，最后补齐 `tests/` 并加入 batch-processor 步骤映射。
- 每个 Skill 只暴露 `run(args) -> dict` 编程契约，不提供面向用户的 CLI；禁止跨 Skill import 内部实现，只能走 `run()` 契约或公共 `utils/`。
- 跑测试：`py -m unittest discover -s tests -v`（Windows 下 `python` 可能是商店占位符）；
  真实公网基准用 `$env:RUN_LIVE_LITERATURE='1'; py -m unittest discover -s tests -p 'test_live_literature.py' -v`。
- `cite-manager` 的 insert 模式只接受显式 `verdict=verified`，不存在人工绕过入口。
- 期刊档案（`config/journals/*.yaml`）数值阈值在作者对照官方 Author Guide
  核对并填写 `verified_against`（URL+日期）之前一律视为未验证——网关会持续
  WARN 提示，不得把未验证档案的 PASS 当作投稿依据。

## 7. Skill 使用规则

- 执行业务任务前先读取对应 `skills/<name>/SKILL.md`，按其输入 / 输出 / 质量约束执行。
- Skill 只处理其职责边界内内容；术语修正建议、门禁违规、重试作业都记录到结果 JSON，便于人工复核。
- coding harness 回填响应时优先保留模型原始 JSON（含 usage 与截断信息），纯文本亦可；本项目不负责模型执行或凭据管理。
