"""review-gate Skill：润色结果复核门禁（确定性消毒器的 Skill 入口）。

内部逻辑已切换至 `utils/guardrails.evaluate_pair`（十条规则，R1–R10），
本 Skill 保持三层兼容职责：
1. 术语检查（term-check）——不在 guardrails 十条规则内，仍在此执行；
2. 规则名 → 历史 check 名映射（fidelity.numbers / fidelity.citations /
   structure.paragraphs / objectivity.hype …），保证既有调用方与测试契约不变；
3. 汇总 verdict（pass/warn/fail）、metrics 与 retry_feedback（归因式）。

输出 verdict（pass / warn / fail）、violations 与 retry_feedback（供润色 Skill 生成
带反馈的重试作业，由coding harness 执行二次润色）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import guardrails  # noqa: E402
from utils.config_loader import load_rules_config, normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from utils.skill_loader import run_skill  # noqa: E402

logger = get_logger("review-gate")

SKILL_NAME = "review-gate"

# guardrails 规则名（+ evidence.kind）→ 历史 check 名，保持调用方契约不变
_CHECK_BY_KIND = {
    ("numeric.conservation", "missing"): "fidelity.numbers",
    ("numeric.conservation", "added"): "fidelity.numbers_added",
    ("numeric.conservation", "unit_loss"): "fidelity.unit_loss",
    ("numeric.conservation", "unit_gain"): "fidelity.unit_gain",
    ("citation.conservation", "weakened"): "fidelity.citations",
    ("citation.conservation", "added"): "fidelity.citations_added",
    ("citation.conservation", "weakened_latex"): "fidelity.citations",
    ("citation.conservation", "added_latex"): "fidelity.citations_added",
    ("citation.conservation", "weakened_author_year"): "fidelity.citations",
    ("citation.conservation", "added_author_year"): "fidelity.citations_added",
    ("citation.conservation", "out_of_bounds"): "fidelity.citation_bounds",
    ("anchor.conservation", "figure"): "fidelity.anchors",
    ("anchor.conservation", "entity"): "fidelity.entities",
    ("anchor.conservation", "entity_once"): "fidelity.entities",
    ("structure.inflation", "length_ratio"): "structure.length_ratio",
    ("structure.inflation", "paragraphs"): "structure.paragraphs",
    ("structure.inflation", "overcorrection"): "structure.overcorrection",
    ("structure.inflation", "markup"): "fidelity.markup",
    ("structure.inflation", "orphan"): "structure.orphan_sentence",
}
_CHECK_BY_RULE = {
    "numeric.polarity": "fidelity.numeric_polarity",
    "hedge.flip": "semantics.hedge_flip",
    "scope.shift": "semantics.scope_shift",
    "hype.lexicon": "objectivity.hype",
    "aiflavor.lexicon": "objectivity.aiflavor",
    "semantic.drift": "semantics.drift",
}


def _legacy_check(violation: guardrails.Violation) -> str:
    kind = str((violation.evidence or {}).get("kind") or "")
    if (violation.rule, kind) in _CHECK_BY_KIND:
        return _CHECK_BY_KIND[(violation.rule, kind)]
    return _CHECK_BY_RULE.get(violation.rule, violation.rule)


def _terminology_violations(polished: str, language: str, gates: dict[str, Any]) -> list[dict[str, Any]]:
    report = run_skill("term-check", {"text": polished, "language": language, "mode": "local"})
    findings = report.get("findings") or []
    errors = [f for f in findings if f.get("severity") == "error"]
    term_error_max = int(gates.get("term_error_max", 0))
    if len(errors) <= term_error_max:
        return []
    preview = "; ".join(f"{f.get('found')}→{f.get('canonical')}" for f in errors[:5])
    return [
        {
            "check": "terminology.nonstandard",
            "severity": "error",
            "message": f"不规范术语 {len(errors)} 处（上限 {term_error_max}）: {preview}",
            "evidence": errors[:8],
        }
    ]


def evaluate(
    source: str,
    polished: str,
    *,
    language: str,
    strength: str,
    mode: str = "local",
    references_text: str | None = None,
    strict: bool | None = None,
) -> dict[str, Any]:
    language = normalize_language(language)
    rules = load_rules_config(language)
    strengths = rules.get("strengths", {})
    if strength not in strengths:
        raise ValueError(f"未知强度: {strength!r}，可选 {sorted(strengths)}")
    gates = strengths[strength].get("quality_gates", {})

    report = guardrails.evaluate_pair(
        source,
        polished,
        language=language,
        strength=strength,
        references_text=references_text,
        strict=strict,
    )
    violations: list[dict[str, Any]] = [
        {
            "check": _legacy_check(item),
            "severity": item.severity,
            "message": item.message,
            "evidence": item.evidence,
        }
        for item in report.violations
    ]
    violations.extend(_terminology_violations(polished, language, gates))

    error_count = sum(1 for v in violations if v["severity"] == "error")
    warning_count = sum(1 for v in violations if v["severity"] == "warning")
    # guardrails 在 strict 模式下可能把 warning 汇总为 fail，但单条
    # Violation.severity 仍保持 warning；这里必须尊重底层 report.verdict，
    # 否则 review-gate 会把 strict 结果重新降级为 warn。
    if error_count or report.verdict == "fail":
        verdict = "fail"
    elif warning_count or report.verdict == "warn":
        verdict = "warn"
    else:
        verdict = "pass"

    metrics = dict(report.metrics)
    metrics["objectivity_findings"] = {
        "error": metrics.get("hype_error", 0),
        "warning": metrics.get("hype_warning", 0),
        "total": metrics.get("hype_error", 0) + metrics.get("hype_warning", 0),
    }
    metrics["aiflavor_hits"] = metrics.get("aiflavor_hits", {})

    return {
        "skill": SKILL_NAME,
        "language": language,
        "strength": strength,
        "verdict": verdict,
        "violations": violations,
        "metrics": metrics,
        "summary": {
            "error": error_count,
            "warning": warning_count,
            "total": len(violations),
        },
        "retry_feedback": guardrails.attribute_feedback(
            [v for v in report.violations] + _term_violation_objects(polished, language, gates),
            language=language,
        ),
        "mode": "local",
    }


def _term_violation_objects(polished: str, language: str, gates: dict[str, Any]) -> list[guardrails.Violation]:
    report = run_skill("term-check", {"text": polished, "language": language, "mode": "local"})
    errors = [f for f in (report.get("findings") or []) if f.get("severity") == "error"]
    term_error_max = int(gates.get("term_error_max", 0))
    if len(errors) <= term_error_max:
        return []
    preview = "; ".join(f"{f.get('found')}→{f.get('canonical')}" for f in errors[:5])
    return [
        guardrails.Violation(
            rule="terminology.nonstandard",
            severity="error",
            message=f"不规范术语 {len(errors)} 处（上限 {term_error_max}）: {preview}",
            evidence={"kind": "terminology", "findings": errors[:8]},
        )
    ]


def run(args: dict[str, Any]) -> dict[str, Any]:
    source = args.get("source_text")
    polished = args.get("polished_text")
    source_file = args.get("source_file")
    polished_file = args.get("polished_file")
    if source is None and source_file:
        source = Path(source_file).read_text(encoding="utf-8-sig")
    if polished is None and polished_file:
        polished = Path(polished_file).read_text(encoding="utf-8-sig")
    if source is None or polished is None:
        raise ValueError("review-gate run() 需要 source_text 与 polished_text（或对应 _file）")
    mode = str(args.get("mode", "local")).lower()
    if mode not in ("local", "offline"):
        raise ValueError("review-gate 已离线化：model 模式移除")
    result = evaluate(
        source,
        polished,
        language=str(args.get("language", "en")),
        strength=str(args.get("strength", "deep")),
        mode=mode,
        references_text=args.get("references_text"),
        strict=args.get("strict"),
    )
    output = args.get("output")
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["output"] = str(output)
    return result
