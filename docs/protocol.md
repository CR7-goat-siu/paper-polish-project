# coding harness 协作协议（编辑协议）

> 本文档定义 coding harness 与 paper-polish-project 的完整协作契约：
> harness 是执行主体和 Orchestrator；本项目是领域引擎、编辑协议、确定性工具和质检层。
> 状态机契约见 `skills/optimizer-loop/SKILL.md`，本文是它的顶层视图与端到端时序。

## 1. 角色边界

| 职责 | 归属 |
| --- | --- |
| 理解用户意图、规划任务、决定何时循环/停止 | coding harness |
| 模型交互（读 prompt、调模型、回填响应） | coding harness |
| 论文解析、Prompt 渲染、门禁、装配、审计、交付 | 本项目（确定性，零模型） |
| 发布前人工签核 | 作者/通讯作者（硬门禁，不可自动化绕过） |

## 2. 端到端时序（粗稿 → 投稿候选）

```text
harness                                   本项目（optimizer-loop 状态机）
──────                                   ─────────────────────────────
run stage=init  ────────────────────────▶ 全源基线审计（5 确定性来源）
                                         registry → scorecard → plan
run stage=build ────────────────────────▶ 计划驱动：问题锚定章节 →
                                         选择性作业包（only_chunks +
                                         focus_issues 批注 + chunk_strength）
      ◀── status=awaiting_responses + pending_jobs ──┘
逐 prompt 调模型，写 responses/<job_id>.txt
run stage=apply ────────────────────────▶ 回填 → 清洗 → 句级门禁 →
                                         重试作业（如有）→ 装配 → DOCX 回写
run stage=decide ───────────────────────▶ 独立复核（重跑全量审计）→
                                         版本决策（新 P0/P1 按指纹判定 → 拒绝回滚）
                                         任务级验证（fixed / still_open）
                                         停止条件判定
      （未停止则回到 build；停止则 ▼）
run stage=deliver ──────────────────────▶ 五件套 + 终稿 DOCX（最佳轮次回写稿或
                                         Markdown 装配）+ submission-audit 预审 +
                                         双门禁合并状态 + 交付索引 +（可选）根目录交付
      ◀── delivery-index.md / 交付状态 ──┘
harness 把 requires_human 事项交给作者；作者签核（硬门禁）
```

## 3. 关键契约

### 3.1 响应回填
- 文件：`responses/<job_id>.txt`（纯文本）或 `.json`（Responses 原始返回）。
- job_id 是确定性复合标识（`<章节id>-<子块id>-a<尝试号>`），build 后即稳定，
  harness 可在 build 与 apply 之间任意中断、续跑。
- `run-state.json` 是唯一状态源；`stage=status` 只读返回下一步与待回填清单。

### 3.2 计划驱动修订
- 每轮只对问题锚定成功的章节出作业（局部补丁）；无锚定问题 → 直接交付。
- 审计问题经 `{{comments}}` 批注通道注入 prompt（最高优先级，与导师批注同通道）。
- 强度路由：`config/issue-rules.yaml` planning.priority_strength。

### 3.3 版本决策与安全
- 新增 P0/P1 按**指纹**判定（issue_id 序号在轮间会漂移，不可用于对比）。
- 门禁 fail 且重试耗尽：先句级外科修复（逐句门禁，只回退违例句），
  无法对齐或整块终检仍失败才整块回退原文；外科修复的 chunk 标记需人工目检。
- requires_human 来源（lit-verify / lit-search / cite-manager / consistency-check）
  不自动改写：补文献必须走 检索→核验→verified-only-insert 管线；
  图表缺失/摘要数字冲突需要作者事实裁决。

### 3.4 交付与签核
- 交付状态 = PRD 三级（READY_FOR_AUTHOR_REVIEW / CONDITIONALLY_READY / BLOCKED）
  与 submission-audit 自动门禁**合并**判定。
- `final_publication_allowed` 恒为 false；人工签核状态在预审报告中显式记录。
- `deliver_to_root=true` 按用户交付偏好复制终稿 DOCX、渲染 PDF、交付索引到根目录。

### 3.5 轮间规则变更
- 规则/词库/术语包在轮间被修正后，`stage=audit` 对当前最佳版本重跑全量审计，
  刷新计划，避免消费陈旧问题清单。

## 4. 目录约定

| 路径 | 内容 |
| --- | --- |
| 根目录 | 最终交付物（DOCX/PDF/交付索引）+ 源码与文档 |
| `manuscripts/` | 真实手稿历史工作稿与原始输入（不入 git） |
| `output/<run>/` | 当前运行完整审计与中间产物 |
| `output/archive/` | 历史运行归档 |
| `test-manuscripts/` | 埋坑测试稿（测试引用，勿移动） |
