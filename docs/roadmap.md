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

四层审查架构落地（L1 锚点锁定 / L2 十条确定性规则 / L3 批处理集成与 docx 批注 / L4 基准评测）：

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

## P5 — 对标能力升级与诚实验证（已完成，2026-10-25）

- [x] **五项能力升级**（对标调研后按架构红线落地）：stat-check 统计一致性
      重算（statcheck+GRIM 纯标准库，接入全源审计与 submission-audit）/
      review-sim 模拟同行评审（四角色作业包 + 逐字锚定 fail-closed + 双层
      requires_human 硬门禁）/ journal-compliance 期刊合规网关（三档案 + 纯
      struct 图像头 DPI + 五大声明区 + run_audit(journal=<id>) 硬门禁）/
      cite-intent 引用证据强度预填（三源摘要链 + 证据句逐字锚定）/
      lit-search refill 文献条目自动补全（严格匹配 + 歧义中止 + Token 级计划）。
- [x] **语义层真实验证**（`docs/validation-report.md`，真实 EEG v7 稿）：
      review-sim 锚定通过率 93.8%（预声明门槛 ≥60%）、cite-intent 摘要覆盖率
      77.8%（门槛 ≥50%）、证据句通过率 100%（18/18）——双特性 validated；
      验证中修复 3 个真实管线缺陷（GB 著录标题提取 / 标题兜底错配 GAN→cGAN /
      arXiv DOI 404），并实证语义评审捕获确定性审计盲区（跨表 SNR 不一致，
      全源审计 20 问题零命中）。
- [x] **诚实验证配套**：utils/anchor_norm 字符级等价归一（排版差异放行、
      措辞改写仍拒）；金标语料 v3 双口径（官方集干净率 + 全评测 P0 率含抽取
      工件）+ rejections.json 跨运行淘汰账本 + candidates 二级缓存；
      cite-manager 行级重复检测补齐年份式文献表盲区（金标 56/56 闭环）。
- [x] **工程纪律**：CI（双 OS 矩阵 + 零 Key 扫描 + scipy/PyYAML oracle 对拍
      job + live 手动触发）；CHANGELOG + 分支规则（feature 分支 + 自审 PR）；
      文档同步闸门 test_docs_sync（调度表全覆盖 / 模板有消费者）；测试缓存
      隔离基建（conftest.isolated_web_cache + skill_loader.loaded_skills 注册表）；
      期刊档案溯源 WARN（verified_against 未填持续提示）；评分机制改三轴口径
      （骨架 / 验证完成度 / 交付诚实度，只有挂接实测数字的轴才能变动）。
- [x] 测试：全量 580+ 项离线回归通过（13 skip = 4 网络 + 8 scipy 对拍 +
      1 PyYAML 对拍，均为条件性跳过）。

## P4.6 — 交付闭环、真实循环验证与句级外科回退（已完成，2026-10-23）

- [x] **交付闭环**：optimizer-loop `stage=deliver` 产出终稿 DOCX（docx 输入取
      最佳轮次回写稿；md 输入经 `utils/docx_io.create_docx_from_markdown` 装配，
      含 Title/Heading/图表题注样式）+ submission-audit 预审（`render` 可选真实渲染）+
      PRD 三级与预审门禁**合并交付状态** + `delivery-index.md` + `deliver_to_root`
      根目录交付；`stage=audit` 支持轮间规则修正后刷新计划。
- [x] **端到端真实循环验证**（coding harness 扮演模型角色，`output/e2e-realloop/`）：
      埋坑稿 4 轮循环 36 → 20 问题（P1 9 → 2），加权指数 0.62 → 2.29，
      收益停滞自动停止，自动交付 DOCX + 渲染 PDF + 预审 + 根目录三件套；
      剩余 20 项全部为 requires_human（[9] 越界、图表缺失、文献缺口）——fail-closed 诚实交付。
- [x] **真实循环暴露并修复 6 个缺陷**：decide 按 issue_id 对比新 P0/P1 被序号漂移
      误判（改指纹对比）；“极大极小博弈”（minimax）被 intensifier 词库误伤
      （负向前瞻）；term-check 别名/规范词互为子串时永远误报 mixed_usage
      （对称包含过滤）；跨章节边界证据无法锚定（标题切分片段回退 + 被标记词优先 +
      锚点片段透传）；无可锚定问题时退化为无目标全量构建（改为停止交付）；
      consistency-check 任务自动修改必撞守恒门禁（归 requires_human）。
- [x] **句级外科式 Fail-Closed**（`utils/polish_engine._surgical_repair`）：
      门禁 fail 且重试耗尽时逐句门禁，只回退违例句、保留已过审修改；
      句数无法对齐或整块终检仍失败才整块回退；外科修复 chunk 标记需人工目检
      （docx 批注区分 source/surgical 回退）。
- [x] **目录整理**：根目录只留最终交付与源码；历史工作稿入 `manuscripts/`；
      output/ 历史产物归档 `output/archive/`（保留交付索引引用的 eeg_v7）。
- [x] **协议与 PRD 补档**：`docs/protocol.md`（harness 协作协议与端到端时序）、
      `docs/prd.md`（PRD §2-§9 定稿，全仓库引用有了单一事实源）。
- [x] 测试：新增 `test_surgical_fallback.py` 3 项；全量 358 项离线回归通过。

## P4.5 — 计划驱动修订与全源审计（已完成，2026-10-23）

把 optimizer-loop 从"审计 → 全文统一润色 → 再审计"升级为真正的
「评估 → 计划 → 定向修改 → 独立复核 → 决策」闭环：

- [x] **全源审计**：issue-registry 文本模式接入 cite-manager audit
      （引用越界/未引用/重复，含 `## 参考文献` Markdown 标题识别修复）、
      lit-search gap（缺引/陈旧/条目信息不全）、新增 `utils/consistency`
      跨节一致性（图表引用完整性/摘要百分比 vs 正文数值含小数等价/结论实体
      缺席实验章节）；`config/issue-rules.yaml` 补来源映射与 P0-P3 覆写。
      此前全部漏检的结构缺陷（[9] 越界、图 3 未定义、摘要 100% vs 94.7%、
      数据集 B 缺席实验）现在都成为带编号 issue。
- [x] **计划驱动修订**：batch-processor build 支持 `only_chunks`（选择性出作业，
      局部补丁）、`focus_issues`（审计问题经 `{{comments}}` 最高优先级批注通道
      注入 prompt，锚定失败显式告警）、`chunk_strength`（按问题优先级选强度，
      规则在 issue-rules.yaml planning 节）。
- [x] **optimizer-loop 状态机化**：`stage=init/build/apply/decide/deliver/status`
      分步契约 + `run-state.json` 断点续跑；build 后状态 awaiting_responses 并
      给出确定性 pending_jobs 清单；每轮输入为历史最佳版本而非原稿。
- [x] **任务级验证**：decide 阶段按问题指纹判定每个计划任务 fixed / still_open，
      写入 iteration_log；lit-search / cite-manager 来源任务归 requires_human
      （补文献必须走检索→核验→insert 管线，润色器不得代劳）。
- [x] 测试：`test_plan_driven_revision.py` 11 项新增（consistency / 全源登记 /
      选择构建 / 状态机断点续跑 / Markdown 标题回归）；全量 355 项离线回归通过。

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
- [x] 真实论文评测集达到 30 篇可评测全文并完成 rubric/黄金标准复核
     （2026-10-23 建成，2026-10-25 扩至多领域 v2，
      `tools/build_gold_standard_corpus.py` + `data/eval/gold-standard/`）：
      56 篇 arXiv 原生 HTML 全文 × 4 领域（biomedical-eeg 32 / computer-vision 8 /
      nlp 8 / time-series 8，公开免密钥，全文只在 `output/gold-standard/`）；
      阴性对照（真实论文全源审计）P0 误报 0/56；阳性对照（五类植入缺陷：
      引用越界/摘要假数字/幽灵图表/浮夸句/重复条目）分域召回均 1.0；
      文件管理 v3：`corpus/` 仅存官方集（official=true）、淘汰稿移入
      `candidates/` 且抓取前查二级缓存（原因跨运行稳定）、
      `candidates/rejections.json` 跨运行淘汰账本、`index.json` 全集清单；
      summary 双口径：`clean_rate_official`（选择后，回归门槛用）与
      `p0_rate_all_evaluated`（含抽取工件的诚实假阳性）。
      【2026-10-25 检测缺口闭环】年份标注式参考文献（ACL [2018]）的
      "重复条目"检测缺口已修复：cite-manager audit 新增与编号风格无关的
      行级重复检测（`duplicated_entry_line`），金标恢复对该风格的植入，
      duplicated_reference 召回 56/56（含 3 篇年份式论文）。
      离线回归夹具（真实论文短摘录，每领域 2 篇）入
      `data/eval/gold-standard/fixtures/`（`test_gold_standard.py`）。
      评测暴露并修复两个真实缺陷：数学区间记号 `[0, 1]` 被 cite-manager 误判
      引用越界（引用编号从 1 起，含 0 的方括号组跳过）；无独立摘要标题的
      arXiv 渲染（标题后第一段即摘要）缺数字一致性检查回退。
- [x] 连续 3 轮端到端红队与真实渲染零关键失败（2026-10-24 完成）：
      #1 `output/e2e-realloop/`（中文 Markdown 稿，4 轮 36→20）；
      #2 `output/e2e-rt2-en/`（英文 Markdown 稿，3 轮 45→22）；
      #3 `output/e2e-rt3-tex/`（自造 LaTeX 稿换主题，3 轮 29→8，测 LaTeX 解析路径）。
      三轮全部自动交付（DOCX + 渲染 PDF + 预审），零关键失败（无崩溃、无门禁穿透、
      剩余问题全部诚实归 requires_human）。红队共逼出并修复 11 个缺陷，包括：
      cite-manager 数学区间 `[0,1]` 误判引用越界、不认识 LaTeX `\bibitem`；
      claim-guard `complete(?:ly)?` 前缀误伤过去式 completed、`total` 误伤
      L_total 公式名与 "a total of N"、`the best` 误伤合法对比指称（降级 warning）、
      全文合并去重导致修一处后下一处指纹漂移被误判新增（改逐处上报 + 种子级
      版本决策对比）；term-check 别名/规范词互为子串永远误报、复数形态不算
      规范用法、"规范词 (缩写)" 首用定义误报混用、参考文献区论文标题被扫术语；
      consistency-check 无独立摘要标题时跳过数字一致性。

## P3 — 规模化与协作

- [ ] CI 增强：模板渲染回归、Skill 契约检查、评测基线守护（基础单测 CI 已就位）。
- [ ] 多稿版本管理：输出物归档到可对比目录（run id）。
- [ ] 用户反馈回流：人工采纳 / 拒绝样本进入评测集。
- [ ] 多模型 A/B（coding harness 侧）：同一作业包跑两个模型，diff 对比 + 门禁择优。
- [ ] 术语库自动扩充：从 apply 阶段的人工修正记录回流候选条目。

## 维护约定

- 路线图条目按"可验证完成物"书写：完成时打勾并附说明。
- 任何会影响契约的迭代必须先更新 `AGENTS.md`、`SKILL.md` 与测试，再改实现。
- 项目保持**零 API Key**：文献检索只允许公开免密钥端点，且必须走 `utils/web_fetch`
  （超时重试 + 每源限速 + 磁盘缓存 + 离线模式）；引入密钥或需要密钥的服务的提议都违反架构红线。
  第三方依赖标准库优先、收益明确时引入，依赖不得接触密钥。
