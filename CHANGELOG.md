# 更新日志

本项目的显著变更记录。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循语义化版本。

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
