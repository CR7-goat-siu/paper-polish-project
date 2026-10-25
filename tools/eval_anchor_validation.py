"""锚点校验对抗评测（离线确定性判定器）。

消费异构模型对抗工作流的产出（{attacks, reviews, cites} JSON），对每个样例
跑与生产一致的锚定判定（utils.anchor_norm + 子串匹配，review-sim / cite-intent
同源逻辑），输出：

- 攻击拦截矩阵：must-reject 族逐族拦截率；任何逃逸（应拒实收）= 校验器缺口；
- 阳性对照接受率：typographic_normalization / verbatim_exact 必须 100% 接受
  （防止对抗加固后把诚实路径也堵死）；
- 跨模型锚定通过率：异构模型诚实执行的 findings 中通过校验的比例；
- cite-intent：证据句通过率与意图分布。

用法：run({"results": "<results.json>", "payload": "<payload.json>", "out": "<dir>"})
（results 由对抗工作流的 return 值落盘而来；编程契约与 build_gold_standard_corpus 一致。）

判定与生产代码的唯一差异：本工具直接调用 normalize_anchor + 子串测试，
等价于 review-sim.apply_responses / cite-intent.apply_intent_responses 的
锚定路径；两处任一变更必须同步（test_anchor_norm 的集成测试兜底）。
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.anchor_norm import normalize_anchor  # noqa: E402

# 攻击族 → 期望判定（分类学出处见 docs/validation-report.md；
# accept = 排版等价/诚实对照，必须放行；reject = 语义差异，必须拦截）
FAMILY_EXPECTATIONS: dict[str, str] = {
    "paraphrase_synonym": "reject",
    "paraphrase_restructure": "reject",
    "translation": "reject",
    "splice_merge": "reject",
    "ellipsis_internal": "reject",
    "numeric_mutation": "reject",
    "fabrication_plausible": "reject",
    "punctuation_injection": "reject",
    "typographic_normalization": "accept",
    "verbatim_exact": "accept",
}

MIN_QUOTE_CHARS = 12  # 与 review-sim limits.min_quote_chars 一致


def section_text_of(job: dict[str, Any], cross_text: str) -> str:
    text = str(job.get("text") or "")
    return cross_text if text == "__CROSS__" else text


def evaluate_attacks(attacks: list[dict[str, Any]], jobs_by_id: dict[str, dict[str, Any]], cross_text: str) -> dict[str, Any]:
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "intercepted": 0, "escaped": 0, "wrong_expectation": 0})
    escapes: list[dict[str, Any]] = []
    positive_failures: list[dict[str, Any]] = []
    for attack in attacks:
        family = str(attack.get("family") or "unknown")
        expectation = FAMILY_EXPECTATIONS.get(family)
        if expectation is None:
            continue
        job = jobs_by_id.get(str(attack.get("job_id") or ""))
        if job is None:
            matrix[family]["total"] += 1
            matrix[family]["wrong_expectation"] += 1
            continue
        haystack = normalize_anchor(section_text_of(job, cross_text))
        quote = normalize_anchor(str(attack.get("exact_source_quote") or ""))
        accepted = bool(quote) and len(quote) >= MIN_QUOTE_CHARS and quote in haystack
        matrix[family]["total"] += 1
        if expectation == "reject":
            if accepted:
                matrix[family]["escaped"] += 1
                escapes.append({**attack, "verdict": "escaped_accepted"})
            else:
                matrix[family]["intercepted"] += 1
        else:  # accept
            if accepted:
                matrix[family]["intercepted"] += 1  # 对照组按预期被接受
            else:
                matrix[family]["escaped"] += 1
                positive_failures.append({**attack, "verdict": "positive_rejected"})
    return {
        "matrix": {family: dict(stats) for family, stats in sorted(matrix.items())},
        "escapes": escapes,
        "positive_failures": positive_failures,
        "rejection_total": sum(
            stats["total"] for family, stats in matrix.items() if FAMILY_EXPECTATIONS.get(family) == "reject"
        ),
        "rejection_intercepted": sum(
            stats["intercepted"] for family, stats in matrix.items() if FAMILY_EXPECTATIONS.get(family) == "reject"
        ),
    }


def evaluate_reviews(reviews: list[dict[str, Any]], jobs_by_id: dict[str, dict[str, Any]], cross_text: str) -> dict[str, Any]:
    total = 0
    accepted = 0
    rejected: list[dict[str, Any]] = []
    per_job: dict[str, dict[str, int]] = {}
    for answer in reviews:
        job_id = str(answer.get("job_id") or "")
        job = jobs_by_id.get(job_id)
        if job is None:
            continue
        haystack = normalize_anchor(section_text_of(job, cross_text))
        stats = per_job.setdefault(job_id, {"total": 0, "accepted": 0})
        for finding in answer.get("findings") or []:
            total += 1
            stats["total"] += 1
            quote = normalize_anchor(str(finding.get("exact_source_quote") or ""))
            ok = bool(quote) and len(quote) >= MIN_QUOTE_CHARS and quote in haystack
            if ok:
                accepted += 1
                stats["accepted"] += 1
            else:
                rejected.append({"job_id": job_id, "quote": quote[:120], "issue_category": finding.get("issue_category")})
    return {
        "findings_total": total,
        "accepted": accepted,
        "anchor_pass_rate": round(accepted / total, 3) if total else None,
        "rejected": rejected,
        "per_job": per_job,
    }


def evaluate_cites(cites: list[dict[str, Any]], pairs_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total = 0
    evidence_required = 0
    evidence_kept = 0
    downgraded = 0
    intents: Counter[str] = Counter()
    for answer in cites:
        pair = pairs_by_id.get(str(answer.get("job_id") or ""))
        if pair is None:
            continue
        total += 1
        intent = str(answer.get("intent") or "").lower()
        intents[intent] += 1
        sentence = str(answer.get("verbatim_evidence_sentence") or "")
        if intent in {"supporting", "contrasting"}:
            evidence_required += 1
            abstract = normalize_anchor(str(pair.get("source_abstract") or ""))
            if sentence and normalize_anchor(sentence) in abstract:
                evidence_kept += 1
            else:
                downgraded += 1
    return {
        "pairs_total": total,
        "intents": dict(intents),
        "evidence_required": evidence_required,
        "evidence_kept": evidence_kept,
        "evidence_pass_rate": round(evidence_kept / evidence_required, 3) if evidence_required else None,
        "downgraded": downgraded,
    }


def run(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = args or {}
    results_path = args.get("results") or "output/validation/cross-model/adversarial-results.json"
    payload_path = args.get("payload") or "output/validation/cross-model/payload.json"
    out_dir = Path(str(args.get("out") or "output/validation/cross-model"))

    results = json.loads(Path(str(results_path)).read_text(encoding="utf-8"))
    payload = json.loads(Path(str(payload_path)).read_text(encoding="utf-8"))
    cross_text = str(payload.get("cross_text") or "")
    jobs_by_id = {str(j.get("job_id")): j for j in payload.get("review_sections") or []}
    pairs_by_id = {str(p.get("job_id")): p for p in payload.get("cite_pairs") or []}

    attack_report = evaluate_attacks(results.get("attacks") or [], jobs_by_id, cross_text)
    review_report = evaluate_reviews(results.get("reviews") or [], jobs_by_id, cross_text)
    cite_report = evaluate_cites(results.get("cites") or [], pairs_by_id)

    summary = {
        "attack_matrix": attack_report["matrix"],
        "attack_escapes": attack_report["escapes"],
        "positive_control_failures": attack_report["positive_failures"],
        "review": review_report,
        "cite": cite_report,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evaluation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 异构模型对抗验证判定", ""]
    lines.append("## 攻击拦截矩阵")
    lines.append("")
    lines.append("| 攻击族 | 期望 | 总数 | 拦截 | 逃逸 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for family, stats in attack_report["matrix"].items():
        expectation = FAMILY_EXPECTATIONS.get(family, "?")
        lines.append(f"| {family} | {expectation} | {stats['total']} | {stats['intercepted']} | {stats['escaped']} |")
    lines.append("")
    if attack_report["escapes"]:
        lines.append("### 逃逸样例（校验器缺口）")
        for item in attack_report["escapes"]:
            lines.append(f"- [{item.get('family')}] {str(item.get('exact_source_quote'))[:100]}")
        lines.append("")
    if attack_report["positive_failures"]:
        lines.append("### 阳性对照失败（诚实路径被堵）")
        for item in attack_report["positive_failures"]:
            lines.append(f"- [{item.get('family')}] {str(item.get('exact_source_quote'))[:100]}")
        lines.append("")
    lines.append("## 跨模型评审锚定")
    lines.append("")
    lines.append(f"- findings 总数：{review_report['findings_total']}；通过：{review_report['accepted']}；通过率：{review_report['anchor_pass_rate']}")
    for item in review_report["rejected"]:
        lines.append(f"- 拒：[{item['job_id']}] {item['issue_category']}：{item['quote']}")
    lines.append("")
    lines.append("## 跨模型引用意图")
    lines.append("")
    lines.append(f"- 意图分布：{cite_report['intents']}")
    lines.append(f"- supporting/contrasting 证据句通过率：{cite_report['evidence_pass_rate']}（{cite_report['evidence_kept']}/{cite_report['evidence_required']}）")
    (out_dir / "evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1)[:3000])
    print(f"\nwritten: {out_dir / 'evaluation.md'}")


if __name__ == "__main__":
    run()
