# 当前版本状态

## 定位

**辅助 coding harness 的论文润色、修改与投稿前预审能力层。**

本项目不得宣称、也不得被描述为“无需人工签核的最终发表系统”。
发布前作者/通讯作者人工签核是硬门禁，不是失败，也不得被自动化绕过。

## 当前版本

- 版本：`v0.17.0` 候选（未发布改动：reviewer 身份契约与评审证据链——identity 契约/final_acquittal 全新上下文评审/哈希绑定 done-provisional-accepted 三态/append-only review trace hash chain；v0.16.0：三轮评审三条证据约束——评审证据绑定最终稿哈希（reviewed_text_sha256）、裁决账本有效性审计（唯一 id/署名/多来源/版本哈希绑定）、waiver 签核工件化（waiver-record.json）；v0.15.0：二轮评审四条——Release Protocol 四条件；v0.14.0：架构评审六条落实——版本决策指纹化、READY 成功路径、交付分层、独立质量评估三层；v0.13.0：R1 对抗闭环——review-sim 进 optimizer-loop decide 轮、R7 免密钥执行桥、job_id 宽容匹配、锚定归一统一；v0.12.0：计划驱动修订、交付闭环、外科回退、五项能力升级、
  语义层真实验证、金标 v3 双口径、YAML 解析器补全，见 docs/roadmap.md）
- 日期：2026-10-25
- 离线回归：603 项（含 PyYAML/scipy 可选对拍与文档同步闸门；数字由 docs-sync 闸门锁定），
  4 项真实公网测试在普通回归中跳过
- 端到端红队 3 连：中文 Markdown（36→20）/ 英文 Markdown（45→22）/ LaTeX 换主题
  （29→8）三轮全部自动收敛并交付，零关键失败（docs/roadmap.md P4）
- optimizer-loop 状态机：init / audit / build / apply / decide / deliver 分步契约 +
  `run-state.json` 断点续跑；每轮按修改计划选择性出作业（局部补丁），
  审计问题注入 prompt 批注通道，decide 按指纹做版本决策与任务级验证
- 交付闭环：终稿 DOCX（docx 回写稿 / Markdown 装配）+ submission-audit 预审 +
  双门禁合并状态 + 交付索引 + 根目录交付（可选真实渲染）
- 端到端真实循环验证：`output/e2e-realloop/`——coding harness 扮演模型，
  埋坑稿 4 轮循环 36 → 20 问题自动收敛并交付（DOCX + 渲染 PDF + 预审）；
  剩余问题全部 requires_human，fail-closed 诚实交付
- 全源审计：issue-registry 文本模式覆盖 claim-guard / term-check /
  cite-manager audit / lit-search gap / consistency-check / stat-check，
  引用越界、图表未定义、摘要数字不一致等结构缺陷已登记为 P0-P3 问题
- 句级外科式 Fail-Closed：门禁重试耗尽时逐句回退违例句，保留已过审修改
- 五项能力升级（2026-10-25）：stat-check 统计一致性重算（statcheck+GRIM 纯标准库，
  面向生物医学类投稿——ML 类稿件推断统计报告密度极低，56 篇金标语料实测 0 命中）/
  review-sim 模拟同行评审（四角色作业包 + 逐字锚定 fail-closed + 双层
  requires_human 硬门禁）/ journal-compliance 期刊合规网关（三档案 + 纯 struct
  图像头 DPI 解析 + 五大声明区；档案阈值未经官方指南核对前一律 WARN 提示）/
  cite-intent 引用证据强度预填（三源摘要链 + 证据句逐字锚定）/ lit-search
  refill 文献条目自动补全（严格匹配 + 歧义中止 + Token 级改写计划）
- 语义层真实验证（docs/validation-report.md）：真实 EEG v7 稿上 review-sim
  锚定通过率 93.8%、cite-intent 摘要覆盖率 77.8%、证据句通过率 100%——
  双特性 validated；验证中修复 3 个真实管线缺陷（GB 著录标题提取、标题兜底
  错配、arXiv DOI 404），并实证语义评审捕获确定性审计盲区（跨表 SNR 数值
  不一致，全源审计 20 问题零命中）
- 真实论文黄金标准评测集 v3：56 篇 arXiv 全文 × 4 领域（biomedical-eeg 32 /
  computer-vision 8 / nlp 8 / time-series 8）——summary 双口径：官方集干净率
  （选择后口径，回归门槛用）+ 全评测 P0 率（含抽取工件的诚实假阳性口径）；
  五类植入缺陷分域召回均 1.0（年份式文献表行级重复检测闭环 56/56）；
  corpus/ 仅官方集、candidates/ 二级缓存 + rejections.json 跨运行淘汰账本；
  暴露并修复区间记号误判与无标题摘要盲区
- 真实公网文献基准：6 个公开免密钥数据源连通，正样本核验与伪造样本拦截通过
- 模拟坏稿：构建、Fail-Closed 和最终发表阻断行为通过
- DOCX smoke：真实 Word 渲染通过，页面非空
- harness 协作 smoke：由 coding harness 执行模型交互，项目负责作业包、门禁、
  DOCX 回写和渲染验证
- 论文质量验收参考文献：公开元数据通过 Crossref DOI 或 arXiv 原始记录核验
- Zotero Word 交叉引用：支持提取、重绑定、审计和刷新准备

## 自动门禁

| 门禁 | 当前结果 |
| --- | --- |
| DOCX 结构与渲染 | PASS |
| 未解决占位符与域指令 | PASS |
| 伪造风险检测 | PASS |
| 引用证据绑定 | 真实样例中仍可能保持开放门禁 |
| 统计一致性 | 真实样例中仍可能保持开放门禁 |
| 图表完整性 | 真实样例中仍可能保持开放门禁 |
| 作者/通讯作者签核 | `required_not_completed` |
| 最终发表许可 | `false` |

模型调用默认由 coding harness 完成；本项目不读取任何模型凭据（唯一红线：零 API Key），
经授权的免密钥执行桥除外。事实证据、统计和图表核验仍需要作者或项目材料支持。

## 真实材料策略

- 未发表 DOCX、渲染 PDF、逐页图像、审稿记录和交付索引不进入公开仓库。
- 公开示例使用模拟稿件、合成数值或公开元数据。
- 真实材料只在本地 `output/` 或用户控制的工作区中处理。
- 任何无法验证的事实、数字、伦理、基金或图表信息都留给作者确认。

## 真实评测集

- 公开 arXiv 元数据候选可用于开发与回归。
- 人工黄金标准、真实全文集和连续多轮红队验收仍在建设中。
- 未完成的人工标准不得被报告为自动通过率。

## 结论

当前已达到“可由 coding harness 组合调用、可回归、可渲染、fail-closed 的论文能力层”，
并已提供润色、门禁重试、质量评分和投稿预审所需的结构化契约。
尚未完成大规模真实论文泛化验证和作者/通讯作者人工签核。
不得宣布最终发表系统或无需人工签核。
