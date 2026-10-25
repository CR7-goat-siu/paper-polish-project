# 更新日志

本项目的显著变更记录。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循语义化版本。

## [Unreleased] — v0.17.0 候选（2026-10-26）

### 新增（reviewer 身份契约与评审证据链，F1-F5）

- **F1 reviewer 身份契约**：review-sim job 声明 `identity_contract` 与
  `required_response_fields`；响应必须回填 executor_model / reviewer_model /
  model_family / identity_source（枚举 host_session_event | backend_reported |
  caller_declared | unavailable）；`model_family` 由新工具
  `utils/model_identity.py` 从模型名确定性推导核对（OpenAI / Anthropic /
  Google / DeepSeek / Moonshot / Qwen / Zhipu / MiniMax / xAI / Meta，
  无法唯一识别 → unknown），伪造 family → 响应无效。
- **F2 fresh final review**：review-sim 新增 `purpose=final_acquittal` 与
  `build_final` 模式；全新 reviewer 上下文（context_policy=fresh_only），
  输入白名单 = 当前全文 / 图表清单 / 期刊约束 / artifact hash / 文件路径，
  prompt 模板 `prompts/{cn,en}/review-sim-final.md` 不含 focus_issues /
  审计批注 / 历史意见（测试锁定）；响应必须返回全新 `thread_id`，
  复用 previous_thread_ids 中任一 id → invalid。
- **F3 评审证据绑定最终 artifact**：job 保存 `artifact_sha256`（换行归一
  SHA-256），响应回填 `reviewed_text_sha256` 并声明 scope
  （mode=final_full / sections=["__all__"] / includes_references /
  includes_figures / char_count）；release 重算当前最终稿哈希，三者一致才有效，
  任意字符变化 → 旧 accepted 过期（显式 stale_review 原因）。
- **F4 done / provisional / accepted 三态**：`review_acceptance` 入 run-state
  与 iteration log；状态迁移语义固定（未知身份 / missing identity /
  negative verdict / incomplete → done 并写 gate_reason；同族或
  caller_declared → provisional；跨族 + 可信身份 + 全条件 → accepted）；
  Release Protocol 只消费 accepted，provisional / done 输出 staging；
  老 run-state 迁移为 done，永不升格 accepted。
- **F5 append-only review trace**：每次 reviewer 调用落盘
  `output/<run>/review-traces/<trace_id>/`（request.json / response.md /
  meta.json，内嵌内容哈希）；`review-events.jsonl` 追加式 hash chain
  （prev_event_sha256 / event_sha256）；trace_id 绑定
  run/job/round/artifact hash/thread 五元组；release 验签
  （文件齐全 / 原始响应未变 / 哈希一致 / 身份一致 / 链完整），
  损坏 / 缺失 / 跨 run 复用 → staging。

### 测试

- 新增 tests/test_review_evidence.py（17 项）+ test_release_protocol.py 重写
  扩展（29 项）+ F4 端到端四项（accepted E2E / provisional 拒绝 /
  stale 降级 / 接线级 release 成功路径）；总收集 603 项。
- 六类负路径全部锁定：same-family provisional、unknown identity done、
  stale hash 过期、skipped review 不计、旧 thread 拒绝、损坏 trace staging。

## [Unreleased] — v0.16.0 候选（2026-10-25）

### 修复（架构评审三轮三条证据约束 + 两个漏口）

- **评审证据绑定最终稿版本（第 1 条）**：stage_review 消费完成时记录
  `reviewed_text_sha256`（评审对象文本哈希）；release 校验要求其等于最终稿
  哈希——评审后稿件再被改动而未复审 → 阻断；skipped 周期不再计入最终评审。
- **apply 异常路径先评后交付（第 2 条）**：apply 未产出终稿的停机同样走
  `_final_review_or_deliver`（评审对象是历史最佳），堵住最后一个漏口。
- **裁决账本有效性审计（第 3 条）**：issue_id 重复 → fail-closed；空白
  adjudicator → 无效证据；已裁决来源 < 2 → 拒绝；新增 manuscript_sha256
  列绑定稿件版本（缺失或不一致 → 拒绝）。评审者最小反例（5 条同 id、空白
  署名、precision=1.0 的 TP 行）现被拒绝并指明证据缺陷。
- **waiver 签核工件化（第 4 条）**：豁免必须是 `{check, author, reason}`
  工件（纯字符串不再生效）；每次豁免落盘 `waiver-record.json`
  （作者/原因/时间/被豁免条件/稿件哈希/运行状态）并在交付索引可审计。
- **H5 端到端边界**：新增四条件 release 的 E2E——有效评审哈希 + 有效账本 +
  放行 CONDITIONAL 时，release 仍且仅被 submission-audit 材料门挡住
  （evidence 门要求文献核验与作者材料，离线无法合法变绿是其设计目的）；
  伪造账本 E2E 反例拒绝。四条件全绿 → release 由单元级钉死
  （tests/test_release_protocol.py）。

### 测试

- test_release_protocol.py 重写为 17 项（评审者反例/哈希绑定/账本审计/
  豁免工件/apply 路由）+ E2E 边界 2 项；总收集 570 项。

## [Unreleased] — v0.15.0 候选（2026-10-25）

### 修复（架构评审二轮四条意见）

- **最终评审不可绕过（第 1 条）**：stage_build 的"无任务 / 无锚定 / 轮数超限"
  三个提前交付路径统一走 `_final_review_or_deliver`——评审启用且预算未用尽时
  先对全章节发射最终评审再交付（干净稿 + review_sim=True 此前 review_cycles=0，
  现在必须 ≥1 且测试断言）。
- **正式 release 不可绕过 submission-audit（第 2 条）**：`_release_requirements`
  四条件聚合——①确定性审计 ②最终评审完成 ③预审已执行且 automated_pass
  （**不可豁免**）④人工 precision 门禁；预审未执行的 READY 只能是诊断包；
  交付索引附条件状态表，结果带 `release_requirements`。`release_waivers`
  仅接受 final_review / precision_gate（作者显式接管）。
- **precision 门禁进入代码（第 3 条）**：`utils/adjudication.py` 读取裁决账本
  （TP/FP/UNCLEAR 三值，UNCLEAR 不入分母），已裁决 ≥5 且 precision ≥ 0.8
  才放行；stage_deliver 集成 `adjudication_ledger` 参数（默认探测
  out_dir/adjudication-ledger.csv）。
- **location 漂移豁免收紧（第 4 条）**：同 location 但两侧上下文都有且不相似
  时不再豁免（同段新问题误豁口子堵上）；仅一侧上下文缺失（无凭据可比）时
  允许仅凭 location 豁免，防止误拒。

### 测试

- 新增 tests/test_release_protocol.py（12 项：账本加载/门禁四分支/四条件聚合
  六分支）+ 干净稿评审周期断言 + G4 豁免拆分 + G2 负路径重写；总收集 563 项
  （docs-sync 数字闸门同步）。

## [Unreleased] — v0.14.0 候选（2026-10-25）

### 修复（架构评审 2026-10-25 六条意见的落实）

- **版本决策指纹化修复（评审第 1 条，安全缺口）**：decide() 的新增 P0/P1 判定
  从"种类集合比对"改为**指纹比对 + 显式漂移豁免**——新指纹与已消失的同 kind
  旧指纹构成重锚定（location 一致或上下文 difflib 相似度 ≥ 0.6）才豁免；
  评审者的最小反例（同 kind 异处实例替换）此前会以 accept_no_gain 放行回归，
  现已拒绝并回滚，反例固化为回归测试。
- **测试 G 落实（评审第 2 条）**：新增 DecideFingerprintTests 直测 decide()
  七个分支（新 kind / 同 kind 异处 / 计数膨胀 / 窗口漂移豁免 / 同 location 豁免 /
  指纹未变 / 指数下降）；此前文档声称覆盖却无任何直接测试。
- **READY 成功路径验收（评审第 3 条）**：新增无缺陷 fixture
  `test-manuscripts/cn-clean-draft.md`（GB/T 参考文献齐全、引用成对、数字一致），
  端到端断言 PRD 三级 readiness 真实到达 **READY_FOR_AUTHOR_REVIEW**
  （加权指数 4.0、硬门槛通过）；stage_deliver 结果新增 `prd_readiness`
  暴露合并前状态。submission-audit 证据绑定门槛使合并状态停在 CONDITIONAL
  属设计使然（纯文本稿件无法自证材料）。
- **交付分层（评审第 4 条）**：BLOCKED / 未放行 CONDITIONAL → 诊断包
  （根目录 `*_诊断包.docx` / `*_诊断索引.md`，索引明示不构成送审候选正式交付）；
  READY 或显式 `allow_conditional_release` → release（`*_可投稿候选.docx`）；
  run() 整体模式此前不透传交付选项的缺陷一并修复。
- **文档数字闸门（评审第 6 条）**：current-status.md 的 570+ 项（虚高）改为实测
  550 项，docs-sync 新增闸门断言文档数字 == unittest 实际收集数，数字漂移从此被测试拦截。

### 新增

- **F5 独立质量评估三层（评审第 5 条）**：
  - 层 1 检测器量化报告（`tools/eval_gold_detectors.py`）：8 篇金标夹具，
    植入召回 35/35=1.0；干净文本 P0/P1 误报率 0.75，噪声画像定位到
    `cite-manager::uncited_entry` 与 `lit-search::incomplete_entry`
    （摘录体裁截断伪影，全文运行不适用）——规则调阈有了可测量依据；
  - 层 2 跨模型环内评审：GLM-5.3（非 Flash）评审员对真实手稿终稿出评审
    响应，经宽容匹配进环消费（区别于执行模型 Flash，"独立复核"的跨模型读法）；
  - 层 3 人工裁决协议（`docs/adjudication-protocol.md`）：P1/P2 告警三值裁决
    （TP/FP/UNCLEAR）+ precision 门槛 ≥ 0.8 作为"可投稿候选"的人工证据轴；
    首批 17 条种子账本已从真实稿运行导出。
- 干净稿夹具与对抗闭环/交付分层/指纹决策共 12 个新测试（总收集 550 项）。

## [Unreleased] — v0.13.0 候选（2026-10-25）

### 新增

- **R1 对抗闭环：review-sim 接入 optimizer-loop decide 轮**——decide 接受修订
  后对改动章节（`diff_sections` 确定性切分 + cross 全文视图）自动发射评审周期；
  意见生命周期 new / still_open / resolved_by_reReview（跨轮去重键 = section +
  类别 + 引文前缀）；活跃意见刷新进最佳版本审计（issue-registry `extra_sources`
  合并通道），下一轮 build 以「评审意见」批注注入对应章节（guidance 驱动，
  requires_human 红线不变）；停机路径上最终版本先评后交付；平台期止损照抄
  ARIS 纪律（连续 2 轮无进展换角色组合、4 轮停止升级人工）。
  **真实稿验证（第二份手稿 EEG 世清 docx，2 轮 + 2 评审周期）**：基线 29 问题
  （P1×13）→ 终稿 23（P1×11），加权指数 1.88 → 2.07；cycle-1 的 5 条意见全部
  经 guidance 修订并在 reReview 判定收敛（resolved 5/5）；cycle-2 挖出 4 条更
  深层问题（含表1/表2 EOG SNR 23.939 vs 24.349 跨表矛盾——确定性审计实测漏检
  的语义类）。交付状态 BLOCKED（P1 待作者裁决）为诚实结果。
- **R7 免密钥执行桥**（`utils/executor_bridge.py` + `config/executor-bridge.yaml`
  + optimizer-loop `stage=backfill`）：CLI 通道（argv 列表无 shell，命令只来自
  配置）与回环 HTTP 通道（`utils/web_fetch.post_text_loopback`，仅允许
  localhost/127.0.0.1/::1，请求头免密钥检查、不落缓存）；每次调用写穿
  `bridge-log.jsonl` 审计；失败/超时作业保持 pending（fail-closed）；
  默认 enabled=false，凭据由通道自持（用户已登录 CLI / 本地推理服务）。
- **job_id 宽容匹配**（`utils/response_matching.py`）：异构执行器实测 17/17
  不回显作业身份——文件级四级归因（精确文件名 / 文件名子串 / 身份键回显 /
  逐字锚点内容唯一归属），歧义即放弃；review-sim / cite-intent apply 接入，
  job_id 回显错误但身份键一致时接受并记 relabel 告警；润色作业无逐字锚点，
  保持严格 job_id（防跨块换文）。

### 修复

- 锚定归一三处口径分裂：optimizer-loop 问题→章节映射、批注→段落锚定
  （utils/polish_engine）统一改走 `utils.anchor_norm` 字符级等价（评审意见的
  context_snippet 本就是 anchor_norm 形态，原空白折叠匹配必漏）；批注锚文本
  改存原文形态。修复后真实稿 5 条评审意见全部锚定成功（修复前 0/5）。

## [Unreleased] — v0.12.0 候选（2026-10-25）

### 新增

- **stat-check 统计报告一致性核查**（`utils/stat_checks.py`）：t/F/χ²/Z/r 正则
  抽取 + 不完全 Beta/Gamma（Lentz 连分数）纯标准库重算 p 值 + 四舍五入区间
  容差 + decision_error 判定 + GRIM 小样本均值颗粒度（Fraction 精确整数）；
  接入 submission-audit `inferential_checks` 与 issue-registry 全源审计。
  定位说明：面向生物医学类投稿（推断统计报告密集）；ML 类稿件密度极低，
  56 篇金标语料实测 0 命中。
- **review-sim 模拟同行评审**（新 Skill）：章节 × 四角色（methodology /
  statistics / domain_expert / novelty）作业包 + 跨节视图；响应必须逐字锚定
  原文（`exact_source_quote`），fail-closed 校验；意见强制 confidence=low +
  requires_human（issue-registry 出口守卫 + revision-planner 源级断言双层硬
  门禁，永不自动改写）。**真实稿验证：锚定通过率 93.8%**。
- **journal-compliance 期刊合规网关**（`utils/journal_compliance.py` +
  `config/journals/` 三档案）：纯 struct 解析 PNG pHYs / JPEG JFIF / TIFF
  分辨率标签取真实 DPI；五大声明区 regex；`run_audit(journal=<id>)` 逐项
  PASS/FAIL 入硬门禁。档案阈值默认未经官方指南核对（WARN 提示），核对后
  填 `verified_against` 字段解除。
- **cite-intent 引用证据强度预填**（新 Skill）：Crossref → OpenAlex（倒排
  索引还原）→ arXiv 三源摘要链 + Semantic Scholar intents 交叉信号；
  supporting/contrasting 必须逐字锚定被引摘要证据句否则降级 unverified；
  结果 prefilled_pending_confirmation 待作者确认。**真实稿验证：摘要覆盖率
  77.8%、证据句通过率 100%**。
- **lit-search refill 文献条目自动补全**：残缺条目 → 严格匹配（标题相似度
  ≥0.92 + 第一作者姓氏 + 年份±1，前两名 epsilon 内歧义中止）→ lit-verify
  门禁 → format-adapter `refill_plan` Token 级改写计划
  （`auto_filled_with_source` 打标，不落正文）。
- **utils/anchor_norm.py**：锚定文本字符级等价归一（NFKC + 弯引号/连字/空白/
  大小写），review-sim 与 cite-intent 共用；边界红线：只做等价变换，永不做
  语义匹配。
- 轻量 YAML 回退解析器补全：行内流式列表/映射、列表内联映射、引号键剥引号、
  冒号+空格键值切分规范；`tests/test_yaml_parser.py` 24 项语法回归 + 全仓库
  配置冒烟 + PyYAML parity（可选对拍）。PyYAML 仍为可选加速项，零依赖红线不变。
- 金标语料 v3：56 篇 × 4 领域（biomedical-eeg / computer-vision / nlp /
  time-series）；summary 双口径（官方集干净率 + 全评测 P0 率含抽取工件）；
  `candidates/rejections.json` 跨运行淘汰账本；candidates/ 二级缓存防重抓。
- CI 工作流（`.github/workflows/ci.yml`）：ubuntu + Python 3.10 全量离线套件
  + 零 Key 静态扫描；live 网络套件仅手动触发。
- 文档同步闸门（`tests/test_docs_sync.py`）：AGENTS 调度表必须覆盖全部
  Skill、prompt 模板必须有消费者、CHANGELOG / 验证报告存在性。

### 修复

- guardrails 句级配对在重复样板句下候选饥饿 → O(n²) 孤儿检查（10 万字
  6.5s 超 5s 门禁）——源句 token 集合分组 + 极性邻域去重，6.5s → 0.22s。
- lit-search 六个 searcher 未透传 `force_refresh`（签名 / 调用点 / fetch 三层）。
- test_lit_search 模块级配置补丁泄漏污染同进程后续测试。
- cite-intent 标题检索兜底对"严格扩展"标题错配（GAN → Conditional GAN，
  序列相似度 0.95 放过）——改为归一化精确相等双门禁。
- cite-intent GB/T 著录《》标题未提取，标题检索链路整体失效。
- arXiv DOI（10.48550/*）在 Crossref 404 —— arXiv Atom 摘要兜底。
- cite-manager 对年份标注式文献表（ACL `[2018]`）完全失明（REF_ENTRY_RE
  仅认 1-3 位编号）——audit 新增与编号风格无关的行级重复检测
  （`duplicated_entry_line`）；金标恢复该风格植入，duplicated_reference
  召回 56/56。
- `load_skill` 按路径加载不注册 sys.modules，测试工具无法枚举 Skill 模块
  —— 新增 `loaded_skills()` 注册表。

### 变更

- **宪章收窄：唯一硬红线 = 零 API Key**（用户 2026-10-24 指令）——
  "零第三方依赖、零内置模型执行器"从红线降级为默认纪律：标准库仍优先，
  允许在收益明确时引入第三方依赖（不得接触密钥，网络流量仍必须走
  `utils/web_fetch`）；模型交互默认仍走作业包契约，新增**可选免密钥执行桥**
  形态（本地推理服务 / 用户已登录 CLI，凭据由通道自持、调用写穿同一套
  jobs / responses / 审计文件）。人工签核、fail-closed、默认写 `output/`
  等纪律不变。AGENTS.md / README / docs/prd / docs/current-status /
  docs/roadmap 同步修订；`test_no_api_keys` 重新表述为红线执行机制
  （禁直连网络模块与密钥型 SDK），守护范围不变。

### 工程纪律

- 分支规则：功能开发走 feature 分支 + 自审 PR，不再直推 main（AGENTS.md §6）。
- 测试缓存隔离基建（`tests/conftest.isolated_web_cache`）：web_fetch 磁盘
  缓存指向独立目录并接管全部相关模块的配置加载器，消除测试间缓存串味。
- 评分机制：停止单数字自评漂移，改为三轴口径（骨架 / 验证完成度 / 交付
  诚实度），只有挂接实测数字的轴才能变动。

## [v0.11.0] — 2026-10-23

- 计划驱动修订（optimizer-loop init/audit/build/apply/decide/deliver 状态机
  + run-state 断点续跑 + focus_issues 批注通道）。
- E2E 交付闭环（终稿 DOCX / 渲染 PDF / submission-audit 预审 / 双门禁 /
  交付索引 / 根目录交付）。
- 句级外科式 Fail-Closed 回退。
- 中 / 英 / LaTeX 三形态红队全部自动收敛，零关键失败（11 个真 bug 修复）。
- 32 篇真实 arXiv 金标语料（EEG 方向）+ 离线回归夹具。
- utils/consistency 跨节一致性检查；docs/prd.md 与 docs/protocol.md 定稿。
