"""Revision Planner：把问题登记表转成可执行的修改计划（PRD §5 阶段三）。

PRD 要求每项任务包含：问题编号、当前缺陷、预期修改目标、允许修改的范围、
需遵守的事实约束、预期影响的评价维度、完成后的验证方法、可能引入的风险。
本 Skill 全部产出这些字段，且顺序按优先级与依赖关系排列。

原则：
- 局部、可追踪的补丁，而不是每轮重写全文（PRD §5 阶段四）；
- 证据不足的任务不自动改写，转入 requires_human / blocked_by_materials；
- 规划是确定性的：只读登记表与配置，不调用模型，因此计划本身可复现、可审计。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import issue_model  # noqa: E402
from utils.config_loader import normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402

LOG = get_logger("revision-planner")

SCHEMA_VERSION = 1

# review-sim 硬门禁：模拟评审意见永不自动改写（即使配置误删也不受影响）。
HUMAN_ONLY_SOURCES = frozenset({"review-sim"})


def _planning_cfg(rules: dict[str, Any]) -> dict[str, Any]:
    return rules.get("planning") or {}


def _group_key(issue: issue_model.Issue) -> tuple[str, str]:
    """同一 (来源, 维度) 的问题合并成一个任务，减少往返轮次。"""
    return (issue.source, issue.dimension)


def build_plan(
    registry: dict[str, Any],
    scorecard: dict[str, Any] | None = None,
    *,
    rules: dict[str, Any] | None = None,
    language: str = "cn",
) -> dict[str, Any]:
    rules = rules or issue_model.load_issue_rules()
    planning = _planning_cfg(rules)
    fact_constraints = list(planning.get("fact_constraints") or [])
    human_sources = set(planning.get("requires_human_sources") or [])
    material_blocked = set(planning.get("material_blocked_dimensions") or [])

    issues = [
        issue_model.Issue(**raw) if isinstance(raw, dict) else raw
        for raw in registry.get("issues", [])
    ]
    if not issues:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_count": 0,
            "tasks": [],
            "requires_human": [],
            "blocked_by_materials": [],
            "note": "问题登记表为空，无需生成修改计划",
        }

    # 按 (来源, 维度) 聚合，再按优先级排序
    groups: dict[tuple[str, str], list[issue_model.Issue]] = {}
    for issue in issues:
        groups.setdefault(_group_key(issue), []).append(issue)

    ordered: list[tuple[str, str, list[issue_model.Issue]]] = []
    for key, members in groups.items():
        members_sorted = issue_model.sort_issues(members)
        ordered.append((key[0], key[1], members_sorted))
    ordered.sort(key=lambda item: issue_model.sort_key(item[2][0]).__iter__().__next__() if False else (
        issue_model.PRIORITY_ORDER.index(item[2][0].priority),
        item[1],
        item[0],
    ))

    tasks: list[dict[str, Any]] = []
    requires_human: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for index, (source, dimension, members) in enumerate(ordered, start=1):
        problem_ids = [m.issue_id for m in members]
        priority = members[0].priority
        defect = "；".join(dict.fromkeys(m.title for m in members))[:300]
        target = _target_for(dimension, source, members)
        scope = _scope_for(source)
        verification = (
            f"重跑 issue-registry，确认 {', '.join(problem_ids[:5])}"
            f"{' 等' if len(problem_ids) > 5 else ''} 已消失，且 {dimension} 维度无新增 P0/P1"
        )
        risk = _risk_for(source, dimension)
        task = {
            "task_id": f"T{index:03d}",
            "priority": priority,
            "dimension": dimension,
            "source": source,
            "problem_ids": problem_ids,
            "defect": defect,
            "target": target,
            "allowed_scope": scope,
            "fact_constraints": fact_constraints,
            "expected_dimension_impact": [dimension],
            "verification_method": verification,
            "risk": risk,
            "status": "pending",
        }

        if (
            source in human_sources
            or source in HUMAN_ONLY_SOURCES
            or any(m.priority == "P0" for m in members)
            or any((m.metadata or {}).get("requires_human") for m in members)
        ):
            # 文献核验未通过 / 阻断级问题 / 模拟评审意见：不自动改写，交作者裁定
            task["status"] = "requires_human"
            task["reason"] = (
                "阻断级问题、需人工确认的来源或 requires_human 标记，按 PRD §6.2 暂停实质性改写并继续其他任务"
            )
            requires_human.append(task)
            continue
        if dimension in material_blocked and not (scorecard or {}).get(
            "coverage", {}
        ).get("materials_provided"):
            task["status"] = "blocked_by_materials"
            task["reason"] = "缺少代码/实验数据/训练日志，无法在现有证据内完成修改"
            blocked.append(task)
            continue
        tasks.append(task)

    return {
        "schema_version": SCHEMA_VERSION,
        "language": language,
        "task_count": len(tasks),
        "tasks": tasks,
        "requires_human": requires_human,
        "blocked_by_materials": blocked,
        "counts_by_priority": issue_model.counts_by_priority(issues),
    }


def _target_for(dimension: str, source: str, members: list[issue_model.Issue]) -> str:
    suggestions = [m.suggestion for m in members if m.suggestion]
    if suggestions:
        return "；".join(dict.fromkeys(suggestions))[:300]
    return f"消除 {dimension} 维度下 {len(members)} 个已登记问题"


def _scope_for(source: str) -> str:
    return {
        "claim-guard": "仅限含浮夸/绝对化表述的句子，保留其数值与引用",
        "term-check": "仅限术语写法统一，首处定义除外",
        "review-gate": "仅限门禁指出的违例位置",
        "lit-verify": "仅限参考文献条目与正文引用标记",
        "submission-audit": "仅限审查报告指出的章节",
    }.get(source, "仅限问题所在句子或段落")


def _risk_for(source: str, dimension: str) -> str:
    if source == "claim-guard":
        return "削弱结论后可能与摘要/结论表述不一致，需同步检查"
    if source == "term-check":
        return "统一术语可能与既有图表标签冲突"
    if source == "lit-verify":
        return "改动引用编号会牵动全文引用标记，须走 cite-manager"
    if dimension == "scientific_truth":
        return "可能触及事实表述，改后必须重跑保真门禁"
    return "可能引入与其它章节的不一致"


def render_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# 修改计划（Revision Plan）",
        "",
        f"- 可执行任务：{plan['task_count']} 个",
        f"- 需人工确认：{len(plan.get('requires_human') or [])} 个",
        f"- 缺材料阻断：{len(plan.get('blocked_by_materials') or [])} 个",
        "",
    ]
    for task in plan.get("tasks") or []:
        lines += [
            f"## {task['task_id']} [{task['priority']}] {task['dimension']}",
            f"- 问题编号：{', '.join(task['problem_ids'])}",
            f"- 当前缺陷：{task['defect']}",
            f"- 预期目标：{task['target']}",
            f"- 允许范围：{task['allowed_scope']}",
            f"- 事实约束：{'；'.join(task['fact_constraints'])}",
            f"- 预期影响维度：{', '.join(task['expected_dimension_impact'])}",
            f"- 验证方法：{task['verification_method']}",
            f"- 可能风险：{task['risk']}",
            "",
        ]
    for task in plan.get("requires_human") or []:
        lines.append(f"- 需人工确认 {task['task_id']}：{task['defect'][:80]}（{task.get('reason', '')}）")
    for task in plan.get("blocked_by_materials") or []:
        lines.append(f"- 缺材料阻断 {task['task_id']}：{task['defect'][:80]}（{task.get('reason', '')}）")
    return "\n".join(lines) + "\n"


def run(args: dict[str, Any]) -> dict[str, Any]:
    language = normalize_language(str(args.get("language", "cn")))
    rules_path = args.get("rules") or issue_model.RULES_PATH
    rules = issue_model.load_issue_rules(str(rules_path))

    registry = args.get("registry")
    if registry is None:
        registry_file = args.get("registry_file")
        if not registry_file:
            raise ValueError("revision-planner 需要 registry 或 registry_file")
        registry = json.loads(Path(str(registry_file)).read_text(encoding="utf-8"))

    scorecard = args.get("scorecard")
    if scorecard is None and args.get("scorecard_file"):
        scorecard = json.loads(Path(str(args["scorecard_file"])).read_text(encoding="utf-8"))

    plan = build_plan(registry, scorecard, rules=rules, language=language)

    output = args.get("out") or args.get("output")
    if output:
        out_dir = Path(str(output))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "revision-plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "revision-plan.md").write_text(render_markdown(plan), encoding="utf-8")
        plan["output_dir"] = str(out_dir)
        plan["plan_file"] = str(out_dir / "revision-plan.json")
    return plan
