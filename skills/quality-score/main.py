"""Quality Score：多维度论文质量评分卡（PRD §4）。

设计要点（严格对应 PRD）：
- 不用单一模糊分数，而是 10 个相互独立的一级维度，每项 0-4 分；
- 每项评分必须附带：评分依据、论文位置、发现的问题、证据、置信度、改进建议；
- 科学真实性是**独立硬门槛**，不参与加权——综合分再高，有未处理重大事实错误也不放行；
- 防钻空子：评分规则来自 config/issue-rules.yaml，固定且可配置；
  输入只有"问题登记表 + 确定性审计信号"，不读模型自评，因此修改者无法自评提分。

评分是确定性的：由 issue-registry 的 P0-P3 分布推导，而非语言模型主观打分。
这满足 PRD §4.4「应当区分生成式评价与确定性验证」。
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

LOG = get_logger("quality-score")

SCHEMA_VERSION = 1

# 无法从确定性信号推断的维度：标记为 not_assessed，绝不虚构分数
ASSESSABLE_FROM_ISSUES = {
    "scientific_truth",
    "contribution",
    "literature_novelty",
    "academic_expression",
    "argument_structure",
    "figures_format",
    "review_risk",
}
# 需要代码/实验材料才能评估的维度（PRD §3.3：材料缺失必须明说）
REQUIRES_MATERIALS = {"method_rigor", "experiment_sufficiency", "reproducibility"}

SCORE_LABELS = {
    0: "存在严重缺陷，无法满足基本要求",
    1: "缺陷较多，需要实质性修改",
    2: "基本合格，但仍有明显问题",
    3: "质量较好，仅有有限的可改进之处",
    4: "已达到当前评价标准下的高质量水平",
}


def _scoring_cfg(rules: dict[str, Any]) -> dict[str, Any]:
    return rules.get("scoring") or {}


def score_dimension(
    rules: dict[str, Any], dimension: str, issues: list[issue_model.Issue]
) -> dict[str, Any]:
    """按未解决问题加权扣分，得出 0-4 分与逐条证据。"""
    cfg = _scoring_cfg(rules)
    base = float(cfg.get("base_score", 4.0))
    cap = float(cfg.get("dimension_penalty_cap", 6.0))
    penalty_of = cfg.get("priority_penalty") or {}

    dimension_issues = [i for i in issues if i.dimension == dimension]
    penalty = 0.0
    for issue in dimension_issues:
        penalty += float(penalty_of.get(issue.priority, 1.0))
    penalty = min(penalty, cap)

    raw = base - penalty
    score = int(max(0, min(4, round(raw))))
    return {
        "dimension": dimension,
        "score": score,
        "raw_score": round(raw, 2),
        "penalty": round(penalty, 2),
        "label": SCORE_LABELS[score],
        "issue_count": len(dimension_issues),
        "issues": [i.issue_id for i in dimension_issues],
        "evidence": [
            {
                "issue_id": i.issue_id,
                "priority": i.priority,
                "location": i.location or "-",
                "problem": i.title,
                "evidence": i.evidence or "-",
                "confidence": i.confidence,
                "suggestion": i.suggestion or "-",
            }
            for i in dimension_issues
        ],
    }


def build_scorecard(
    registry: dict[str, Any], *, rules: dict[str, Any] | None = None
) -> dict[str, Any]:
    rules = rules or issue_model.load_issue_rules()
    issues = [
        issue_model.Issue(**raw) if isinstance(raw, dict) else raw
        for raw in registry.get("issues", [])
    ]
    dimensions = rules.get("dimensions") or {}

    scores: dict[str, Any] = {}
    not_assessed: list[str] = []
    reasons: dict[str, str] = {}
    covered = issue_model.covered_dimensions(rules, registry.get("sources_consulted") or [])
    for dimension, spec in dimensions.items():
        if dimension in REQUIRES_MATERIALS and not registry.get("materials_provided"):
            not_assessed.append(dimension)
            reasons[dimension] = "缺少代码/实验数据/训练日志等材料（PRD §3.3 要求明说）"
            scores[dimension] = {
                "dimension": dimension,
                "score": None,
                "label": "未评估",
                "issue_count": 0,
                "issues": [],
                "evidence": [],
            }
            continue
        if covered and dimension not in covered:
            # 本次没跑过能覆盖该维度的检查：不能打满分，否则"查过没问题"会被伪装
            not_assessed.append(dimension)
            reasons[dimension] = "本次未运行覆盖该维度的检查（无证据，不评分）"
            scores[dimension] = {
                "dimension": dimension,
                "score": None,
                "label": "未评估",
                "issue_count": 0,
                "issues": [],
                "evidence": [],
            }
            continue
        scores[dimension] = score_dimension(rules, dimension, issues)

    # 加权指数（科学真实性不参与加权）
    total_weight = 0.0
    weighted = 0.0
    for dimension, spec in dimensions.items():
        entry = scores.get(dimension) or {}
        if entry.get("score") is None:
            continue
        if spec.get("hard_gate"):
            continue
        weight = float(spec.get("weight", 1.0))
        total_weight += weight
        weighted += weight * float(entry["score"])

    # 硬门槛：科学真实性维度存在 P0/P1 未解决问题即不放行
    truth = scores.get("scientific_truth") or {}
    truth_blocking = [
        i.issue_id for i in issues if i.dimension == "scientific_truth" and i.priority in ("P0", "P1")
    ]
    hard_gate_pass = not truth_blocking

    return {
        "schema_version": SCHEMA_VERSION,
        "weighted_index": round(weighted / total_weight, 2) if total_weight else None,
        "weighted_index_note": "0-4 制加权均值，仅覆盖已评估维度，仅用于观察趋势，不解释为录用概率",
        "coverage_ratio": round(len(covered) / max(1, len(dimensions)), 2),
        "hard_gate": {
            "dimension": "scientific_truth",
            "pass": hard_gate_pass,
            "blocking_issues": truth_blocking,
            "rule": "科学真实性为独立硬门槛：存在未处理的 P0/P1 问题即阻断放行（PRD §4.3）",
        },
        "scores": scores,
        "not_assessed": not_assessed,
        "not_assessed_reasons": reasons,
        "coverage": {
            "sources_consulted": registry.get("sources_consulted") or [],
            "assessed_dimensions": sorted(covered),
            "materials_provided": bool(registry.get("materials_provided")),
        },
        "counts_by_priority": issue_model.counts_by_priority(issues),
    }


def render_markdown(card: dict[str, Any], rules: dict[str, Any]) -> str:
    dimensions = rules.get("dimensions") or {}
    lines = [
        "# 论文质量评分卡",
        "",
        f"- 加权质量指数：{card.get('weighted_index')}（0-4 制，仅覆盖已评估维度）",
        f"- 维度覆盖率：{card.get('coverage_ratio')}"
        f"（{len((card.get('coverage') or {}).get('assessed_dimensions') or [])}/{len(dimensions)} 个维度有证据）",
        f"- 科学真实性硬门槛：**{'通过' if card['hard_gate']['pass'] else '未通过'}**"
        + (
            f"（阻断问题 {', '.join(card['hard_gate']['blocking_issues'])}）"
            if card["hard_gate"]["blocking_issues"]
            else ""
        ),
        "",
        "| 维度 | 分数 | 未解决问题 | 主要依据 |",
        "| --- | --- | --- | --- |",
    ]
    for dimension, spec in dimensions.items():
        entry = card["scores"].get(dimension) or {}
        label = spec.get("label", dimension)
        if entry.get("score") is None:
            reason = (card.get("not_assessed_reasons") or {}).get(dimension, "未评估")
            lines.append(f"| {label} | 未评估 | - | {reason} |")
            continue
        top = (entry.get("evidence") or [{}])[0]
        basis = top.get("problem", "-") if top else "-"
        lines.append(
            f"| {label} | {entry['score']}/4 | {entry.get('issue_count', 0)} | {str(basis)[:40]} |"
        )
    if card.get("not_assessed"):
        lines += ["", "未评估维度（缺证据/缺材料，未给分）："]
        for dimension in card["not_assessed"]:
            lines.append(
                f"- {dimension}：{(card.get('not_assessed_reasons') or {}).get(dimension, '')}"
            )
    return "\n".join(lines) + "\n"


def run(args: dict[str, Any]) -> dict[str, Any]:
    language = normalize_language(str(args.get("language", "cn")))
    rules_path = args.get("rules") or issue_model.RULES_PATH
    rules = issue_model.load_issue_rules(str(rules_path))

    registry = args.get("registry")
    if registry is None:
        registry_file = args.get("registry_file")
        if not registry_file:
            raise ValueError("quality-score 需要 registry 或 registry_file")
        registry = json.loads(Path(str(registry_file)).read_text(encoding="utf-8"))
    if args.get("materials_provided") is not None:
        registry = dict(registry)
        registry["materials_provided"] = bool(args.get("materials_provided"))

    card = build_scorecard(registry, rules=rules)

    output = args.get("out") or args.get("output")
    if output:
        out_dir = Path(str(output))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "quality-scorecard.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "quality-scorecard.md").write_text(
            render_markdown(card, rules), encoding="utf-8"
        )
        card["output_dir"] = str(out_dir)
        card["scorecard_file"] = str(out_dir / "quality-scorecard.json")
    return card
