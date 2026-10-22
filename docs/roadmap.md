# paper-polish-project 迭代路线图

> 优先级：P0 > P1 > P2 > P3。P0 是"可用"的底线，P1 是"可信"，P2 是"好维护"，P3 是"规模化"。

## P0 — 骨架闭环（已完成）

- [x] 目录骨架与 AGENTS / README / Skill 元信息。
- [x] 中英文三档规则配置与术语库。
- [x] paper-parser：LaTeX / Markdown / **DOCX** 解析、章节拆分、无损重建校验、docx 段落索引。
- [x] en-polish / cn-polish 调用链（模板渲染 → 路由 → usage 统计）。
- [x] term-check / format-adapter / batch-processor 离线运行。
- [x] 全量单元测试（无网络、无 Key）。

## P1 — 质量可信（已完成）

- [x] 复核门禁落地：`review-gate` 按档位 `quality_gates` 校验数字 / 引用 / 公式 / 段落 / 长度 / 相似度。
- [x] 带反馈的自动重试：门禁失败生成 retry 作业（含违规明细），二次润色后复检。
- [x] 客观性守卫：`claim-guard` + hype/informal 词库（数据锚定启发式、去浮夸、口语冗余检测）。
- [x] 领域可插拔：`config/domains/` 领域包 + 按术语命中自动识别 + 术语修正对照表注入 Prompt。
- [x] 逐 chunk diff 报告（词级改动 + 统计表 + unified diff）。
- [x] docx 端到端：解析（含无样式编号标题识别）→ 润色 → 保留样式回写；段落不可对齐时回退纯文本。
- [x] Prompt 审计留痕：build 阶段 prompts/ 目录。
- [x] 结构化评测器：objectivity / terminology / grammar(GLEU) / fidelity 四套件 + A/B 盲评生成。
- [x] 实测基线（archived 归档重放，六档全覆盖）：dehype 1.0、recall ≥0.958、interception ≥0.958、
  fix_rate 1.0、overcorrection 0、数字保真 1.0；归档存 data/baseline/（含响应文件，可复现重算）。

## P2 — 纯离线架构（已完成，当前阶段）

- [x] 移除全部网络层（model_client / SDK / Key 读取），`utils/job_io.py` 定义作业契约。
- [x] 润色改为 build（作业包出）/ apply（响应进 + 门禁 + 重试 + 装配）双阶段。
- [x] 守卫类 Skill 全部本地化（claim-guard / term-check / format-adapter / review-gate）。
- [x] evaluator 支持 oracle 校准与外部注入式评测（coding harness 输出可直接套同一套指标）。
- [x] batch-processor 双阶段调度：build → coding harness → apply → retry 循环
  （重试作业带 parent_chunk_id 与全局唯一 job_id，端到端回归测试覆盖）。
- [x] evaluator archived 模式：归档响应离线重放评分（基线证据链可复现）。
- [x] A/B 盲评真随机化（先乱序后贴标签）+ 支持对真实 run 目录出盲评对。
- [x] fidelity 数字比对数值化（区间/科学计数法不误报），引用新增 warning 检查。
- [x] 中英润色实现收敛至 utils/polish_engine.py（消除 460 行复制粘贴）。
- [x] docx 输入的中间产物改名为 .processed.txt（不再产出打不开的假 docx）。
- [x] claim-guard 支持 rules.objectivity.lexicon_whitelist 词条白名单（默认空，不改变默认行为）。
- [x] 数字符号边界：`10 -20` 按区间解析，CJK 后的负数仍正确识别。
- [x] CI：.github/workflows/ci.yml（ubuntu/windows × Python 3.10 全量单测）。
- [ ] 作业包断点续跑粒度细化到 job 级（当前文件级 resume）。
- [ ] review-gate 增加可选的"连贯性"本地启发式（连接词密度、指代距离）。

## P2.5 — 文献支撑（已完成，2026-10-15）

- [x] `utils/citation_check.py`：标题/期刊/作者归一并相似度、DOI 规范校验、多源记录归一、
      元数据逐字段比对（findings）、IEEE / APA / GB-T 7714 / BibTeX 四种格式化。
- [x] `config/literature.yaml`：数据源白名单、网络与缓存参数、缺口信号词、核验阈值、引用风格。
- [x] `utils/web_fetch.py`：标准库 GET + 超时重试 + 每源最小间隔 + 磁盘缓存 + 离线只读；
      密钥请求头硬拒绝，测试用 `set_transport()` 注桩，CI 不发真实请求。
- [x] `skills/lit-search/`：gap（离线缺口检测 + 英文检索式草稿）/ search（Crossref · OpenAlex ·
      Semantic Scholar · arXiv · Europe PMC · PubMed 多源检索、去重排序）/ cards（候选文献卡片）/ package
      （`prompts/<lang>/lit-search.md` 作业包）。
- [x] `skills/lit-verify/`：无证据不通过的确定性核验门禁，三档结论 + findings + 报告。
- [x] `cite-manager` 的 insert 模式：只接受 verified 条目，正文标记 + 参考文献条目追加，
      产出 `docx_plan`（token 级）与 `docx_insertions`（条目行），公式段落安全。
- [x] `batch-processor`：`literature=true`（build 出缺口报告）+ apply 阶段 lit-search / lit-verify 步骤。
- [x] AGENTS.md §2 约束由"零网络"改为"零 API Key、零第三方依赖"，SOP 与调度表同步。

## P2.6 — 文献能力升级（已完成，2026-10-15）

针对"主动联网检索与参考文献搜集"短板的四维升级：

- [x] **主动检索触发**：缺口检测新增 `stale_citation`（趋势论断只引旧文献）与
      `incomplete_entry`（参考文献条目缺 year/venue/pages），依赖 `references` 输入；
      lit-search 新增 `auto` 模式（gap→search→cards 一条龙）；batch-processor
      apply 阶段 `literature=true` 自动追加 lit-search(auto)+lit-verify（只产报告，不写正文）；
      build 阶段 `lit_mode=auto` 顺带检索候选。
- [x] **数据源与多轮检索**：新增 PubMed（NCBI E-utilities，esearch→esummary 两步，免密钥）；
      `multi_round` 三档轮次（broad 宽式 → precise 布尔 AND 精确 → fresh_cited 近五年+按源
      被引/提交日期排序），同义词扩展表 `data/literature/synonyms.json`；
      `keywords.highly_cited_min` 可做高引硬过滤。
- [x] **真实性核验与防幻觉**：lit-verify 在线二次核验（`online`，默认取配置）——无本地证据时
      按 DOI 直抓 Crossref `works/{doi}` 或按标题 `query.bibliographic` 取 top-1 作为证据再比对；
      抓取失败/离线一律降级为 unverified（核验失败≠通过）；`reverify` 溯源信息进结果。
- [x] **结构化输出与按需插入**：候选卡片含四种引用格式（IEEE/APA/GB-T 7714/BibTeX）、
      核心结论摘要、推荐理由、证据原句；cite-manager insert 输出 `entry_formats`
      与 `formats_out` Markdown；`utils/citation_check` 新增 `entry_core_summary` /
      `missing_required_fields` / `format_all_styles`。
- [x] 测试：219 项离线单测通过，4 项真实公网用例默认跳过（新增 stale/incomplete 缺口、
      多轮变体、PubMed 适配、auto 一条龙、在线核验、apply 主动触发端到端、多格式输出）。

## P2.7 — 真实公网基准与安全收尾（已完成，2026-10-19）

- [x] `test_live_literature.py`：六源真实连通、10 篇真实文献正样本、5 个伪造/篡改负样本、
      arXiv ID 保留、batch 未验证条目防穿透。
- [x] Semantic Scholar Graph search 与公开 bulk search 同源兜底；429/5xx 指数退避，
      永久性 4xx 不重试。
- [x] `web_fetch` 支持 `Retry-After`，失败响应显式关闭；真实基准无 socket ResourceWarning。
- [x] `lit-verify.verified_entries[]` 显式携带 `verdict=verified`；cite-manager 删除人工绕过入口，
      缺少 verdict、unverified、rejected 一律拒绝。

## P2.8 — 防幻觉/防错审查机制（已完成，2026-10-20）

四层审查架构落地（方案见 docs/hallucination-guardrails-plan.md）：

- [x] L2 `utils/guardrails.py`：十条确定性规则（数值守恒/极性翻转/引用守恒含编号越界/
      锚点守恒含实体篡改/对冲漂移/范围丢失/浮夸词/AI 腔密度/结构膨胀/语义漂移），
      `evaluate_pair()` 只接收 (source, polished) 二元组（CoVe factored 原则）。
- [x] Fail-Closed 修复：polish_engine 门禁 fail 且重试耗尽 → 强制装配原文，
      `fallback_reason` 入 chunk_reports（修复此前润色版仍流入终稿的漏洞）。
- [x] L1 `utils/anchor_lock.py`：五类锚点提取 + 必保清单 + CoVe 六问自检表，
      六个 polish 模板注入 `{{anchors}}`/`{{self_check}}` + anti-addition 硬约束；
      retry feedback 升级为按规则归因（attribute_feedback）。
- [x] L4 基准：`data/eval/guardrails-{negative,positive}-{cn,en}.json`
      （每规则 ≥10 负样本 + 25 正样本/语言）；evaluator 的 guardrails 模式：
      **Recall=1.0 / FPR=0.0 / 警告规则命中率=1.0**（中英双语言）。
- [x] AI 腔词库 `data/lexicon/ai-flavor-{cn,en}.json`（密度硬上限：泛化结尾与
      模糊归因 0 容忍、高频词每段 ≤2、套句全文 ≤1）。
- [x] 性能：倒排索引 + top-K 候选 + 主正则预计算 + Counter 多重集；
      10 万字段落实测 ≈0.5s（纯标准库）。
- [ ] L3 批处理集成与 docx 批注告警（下一阶段）。

## P4 — 投稿前预审与人工签核门禁（进行中，2026-10-21）

- [x] 项目定位写入 README：辅助 coding harness 的论文润色、修改与投稿前预审能力层；
      明确禁止“无需人工签核的最终发表系统”表述。
- [x] `config/submission.yaml`：引用证据绑定率、统计冲突、图表完整性、DOCX 结构/真实渲染、
      占位符与人工签核阈值；附 0–5 评分 rubric。
- [x] `submission-audit` Skill：主张—引用—原文证据图谱、数字登记与重算、图表清单、
      Word COM/LibreOffice → PDF → `pdftoppm` 页面图、风险/作者确认/人工签核报告。
- [x] 自动渲染缺工具时显式降级，不得声称页面级验收通过；自动非空页检查不替代作者目检。
- [x] `submission-audit` 接入 `batch-processor.apply` 的最终产物阶段（DOCX 回写后延迟执行）。
- [x] 预审包自动装配（DOCX/Markdown/纯文本）+ 主张—引用证据作业包 + 响应回填 fail-closed。
- [x] 发布清单与复现清单：输入/输出哈希、环境、渲染后端和复现命令。
- [x] 数字分类与比较组由 coding harness 回填 + fail-closed 检查；禁止改原值或伪装 verified。
- [x] `submission-audit` 的 pipeline 模式组合预审装配、可选响应回填与审计。
- [x] 移除项目内置模型执行器；模型交互统一交由 coding harness。
- [x] 保留 build → harness 响应回填 → apply → 门禁重试 → DOCX → 预审的可组合能力。
- [x] 修复润色文本长度变化时 DOCX chunk 偏移错位问题。
- [x] `evaluator mode=pair` 提供单文本对评分，供 harness 决定逐 chunk 质量爬坡和回滚。
- [x] 质量候选采用硬门禁 + 正增益接受规则，负增益自动回滚到上一最佳版本。
- [x] 建立 33 篇 Crossref/arXiv 双源核验的 EEG 去噪论文质量验收集。
- [x] 采集 37 条 arXiv 公开元数据候选并做 development/test 初分。
- [ ] 真实论文评测集达到 30 篇可评测全文并完成 rubric/黄金标准复核。
- [ ] 连续 3 轮端到端红队与真实渲染零关键失败。

## P3 — 规模化与协作

- [ ] CI 增强：模板渲染回归、Skill 契约检查、评测基线守护（基础单测 CI 已就位）。
- [ ] 多稿版本管理：输出物归档到可对比目录（run id）。
- [ ] 用户反馈回流：人工采纳 / 拒绝样本进入评测集。
- [ ] 多模型 A/B（coding harness 侧）：同一作业包跑两个模型，diff 对比 + 门禁择优。
- [ ] 术语库自动扩充：从 apply 阶段的人工修正记录回流候选条目。

## 维护约定

- 路线图条目按"可验证完成物"书写：完成时打勾并附说明。
- 任何会影响契约的迭代必须先更新 `AGENTS.md`、`SKILL.md` 与测试，再改实现。
- 项目保持**零 API Key、零第三方依赖**：文献检索只允许公开免密钥端点，且必须走 `utils/web_fetch`
  （超时重试 + 每源限速 + 磁盘缓存 + 离线模式）；任何引入密钥 / SDK / 抓取条款不允许页面的提议都违反架构红线。
