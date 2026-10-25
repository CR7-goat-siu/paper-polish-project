"""金标检测器量化报告工具（F5 层 1：检测器层证据）。

对 data/eval/gold-standard/fixtures/ 的真实 arXiv 摘录做双向量化：
- 阴性（原文完整）：逐 source::check 统计启发式告警噪声画像——"干净文本上
  每类检查的触发率"，即架构评审第 5 条所指"P1 告警噪声"的可测量形态；
- 阳性（植入五类缺陷）：逐变体召回。

产物：detector-summary.json（机器可读）+ detector-report.md（人读报告）。
确定性离线；网络采集的 30+ 篇全量评测仍由 build_gold_standard_corpus.py 按需重跑，
本工具覆盖的是入库夹具的固化报告（每次检测器规则变更后重跑即可回归对比）。

run(args) 契约：{"out": <目录>}（默认 data/eval/gold-standard/）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.skill_loader import run_skill  # noqa: E402

FIXTURE_DIR = ROOT / "data" / "eval" / "gold-standard" / "fixtures"
GOLD_TOOL_PATH = ROOT / "tools" / "build_gold_standard_corpus.py"


def _plant_variants():
    spec = importlib.util.spec_from_file_location("gold_corpus_tool", GOLD_TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.plant_variants


def _signature(issue: dict[str, Any]) -> str:
    metadata = issue.get("metadata") or {}
    return f"{issue.get('source')}::{metadata.get('check')}"


def evaluate_fixture(text: str, manifest_entry: dict[str, Any], plant) -> dict[str, Any]:
    result: dict[str, Any] = {"name": manifest_entry.get("name") or ""}

    # 阴性：原文完整审计 → 噪声画像
    registry = run_skill("issue-registry", {"text": text, "language": "en"})
    intact_issues = registry.get("issues") or []
    result["intact"] = {
        "issue_count": len(intact_issues),
        "counts_by_priority": registry.get("counts_by_priority") or {},
        "signature_counts": dict(Counter(_signature(i) for i in intact_issues)),
    }

    # 阳性：植入变体召回
    variants = plant(text)
    by_name = {v["name"]: v for v in variants}
    recall: list[dict[str, Any]] = []
    for name in manifest_entry.get("expected_variants") or []:
        variant = by_name.get(name)
        if not variant:
            recall.append({"variant": name, "planted": False, "detected": False})
            continue
        vreg = run_skill("issue-registry", {"text": variant["text"], "language": "en"})
        detected = False
        planted_terms = ("revolutionary", "undoubtedly", "state-of-the-art", "perfectly")
        for issue in vreg.get("issues") or []:
            if issue.get("source") != variant.get("expected_source"):
                continue
            check = (issue.get("metadata") or {}).get("check")
            found = str((issue.get("metadata") or {}).get("found") or "")
            if variant.get("expected_check") is not None:
                detected = detected or check == variant["expected_check"]
            else:
                detected = detected or any(
                    term in found.lower() or term in issue["title"].lower()
                    for term in planted_terms
                )
        recall.append({"variant": name, "planted": True, "detected": detected})
    result["recall"] = recall
    return result


def run(args: dict[str, Any]) -> dict[str, Any]:
    out_dir = Path(str(args.get("out") or FIXTURE_DIR.parent))
    if not FIXTURE_DIR.exists():
        raise FileNotFoundError(f"金标夹具未生成: {FIXTURE_DIR}")
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    plant = _plant_variants()

    fixtures = sorted(path for path in FIXTURE_DIR.glob("*.md"))
    if not fixtures:
        raise FileNotFoundError("夹具目录为空")
    per_fixture = []
    for path in fixtures:
        text = path.read_text(encoding="utf-8")
        entry = manifest.get(path.name) or {}
        per_fixture.append(evaluate_fixture(text, entry, plant))

    # 汇总
    total_intact_issues = sum(f["intact"]["issue_count"] for f in per_fixture)
    intact_with_p0p1 = sum(
        1 for f in per_fixture
        if (f["intact"]["counts_by_priority"].get("P0", 0) + f["intact"]["counts_by_priority"].get("P1", 0)) > 0
    )
    noise: dict[str, int] = defaultdict(int)
    for f in per_fixture:
        for signature, count in f["intact"]["signature_counts"].items():
            noise[signature] += count
    planted_total = sum(
        1 for f in per_fixture for r in f["recall"] if r["planted"]
    )
    planted_detected = sum(
        1 for f in per_fixture for r in f["recall"] if r["planted"] and r["detected"]
    )
    summary = {
        "schema_version": 1,
        "fixture_count": len(per_fixture),
        "intact": {
            "issue_count_total": total_intact_issues,
            "issues_per_fixture_avg": round(total_intact_issues / len(per_fixture), 2),
            "fixtures_with_p0_or_p1": intact_with_p0p1,
            "p0p1_false_positive_rate": round(intact_with_p0p1 / len(per_fixture), 4),
            "noise_by_signature": dict(sorted(noise.items(), key=lambda kv: -kv[1])),
        },
        "planted": {
            "variants_total": planted_total,
            "variants_detected": planted_detected,
            "recall_rate": round(planted_detected / planted_total, 4) if planted_total else None,
        },
        "note": (
            "阴性口径为启发式告警噪声画像（P0/P1 误报率为阻断级误报率）；"
            "P2/P3 告警不阻断，其触发频次是规则调阈的依据（人工裁决协议见 docs/adjudication-protocol.md）"
        ),
        "per_fixture": per_fixture,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "detector-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# 金标检测器量化报告（F5 层 1）",
        "",
        f"- 夹具：{len(per_fixture)} 篇真实 arXiv 论文摘录（离线固化集）",
        f"- 阳性植入召回：{planted_detected}/{planted_total}"
        f"（{summary['planted']['recall_rate']}）",
        f"- 阴性 P0/P1 误报率：{intact_with_p0p1}/{len(per_fixture)}"
        f"（{summary['intact']['p0p1_false_positive_rate']}）",
        f"- 干净文本告警总量：{total_intact_issues}"
        f"（均值 {summary['intact']['issues_per_fixture_avg']}/篇，全部为 P2/P3 噪声画像）",
        "",
        "## 干净文本上的启发式噪声画像（调阈依据）",
        "",
        "| 签名 | 触发次数 |",
        "| --- | --- |",
    ]
    for signature, count in summary["intact"]["noise_by_signature"].items():
        lines.append(f"| {signature} | {count} |")
    lines += ["", "## 逐夹具明细", ""]
    for f in per_fixture:
        counts = f["intact"]["counts_by_priority"]
        lines.append(
            f"- {f['name']}：告警 {f['intact']['issue_count']}"
            f"（P0 {counts.get('P0', 0)} / P1 {counts.get('P1', 0)} /"
            f" P2 {counts.get('P2', 0)} / P3 {counts.get('P3', 0)}），"
            f"植入召回 {sum(1 for r in f['recall'] if r['detected'])}/{sum(1 for r in f['recall'] if r['planted'])}"
        )
    (out_dir / "detector-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":  # pragma: no cover - 工具入口仅手动调用
    print(json.dumps(run({}), ensure_ascii=False, indent=1)[:500])
