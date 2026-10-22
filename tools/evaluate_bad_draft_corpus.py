"""对模拟坏稿语料执行预审包构建与 fail-closed 门禁回归。

该工具只验证“坏稿不会停摆、也不会被误判为已签发候选”。它不把模拟语料
结果当作真实论文性能。真实评测覆盖率由 data/eval/real-papers/manifest.json
单独报告。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.skill_loader import run_skill  # noqa: E402


def evaluate(manifest_path: Path, out_dir: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    corpus_dir = manifest_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for entry in manifest.get("entries") or []:
        case_out = out_dir / entry["id"]
        error = ""
        try:
            built = run_skill(
                "submission-audit",
                {
                    "mode": "package",
                    "input_file": str(corpus_dir / entry["path"]),
                    "language": "en",
                    "out": str(case_out),
                },
            )
            audited = run_skill(
                "submission-audit",
                {
                    "mode": "audit",
                    "package_file": built["package_file"],
                    "out": str(case_out / "audit"),
                    "render": False,
                },
            )
            rows.append(
                {
                    "id": entry["id"],
                    "split": entry["split"],
                    "status": "ok",
                    "built": built.get("status") == "built",
                    "evidence_job_count": built.get("evidence_job_count", 0),
                    "statistics_job_count": built.get("statistics_job_count", 0),
                    "automated_pass": bool((audited.get("gate") or {}).get("automated_pass")),
                    "final_publication_allowed": bool(
                        (audited.get("gate") or {}).get("final_publication_allowed")
                    ),
                    "human_signoff_status": (audited.get("gate") or {}).get(
                        "human_signoff_status"
                    ),
                    "audit_status": audited.get("status"),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 评测需记录坏稿停摆
            error = str(exc)
            rows.append(
                {
                    "id": entry["id"],
                    "split": entry["split"],
                    "status": "error",
                    "error": error,
                    "built": False,
                    "automated_pass": False,
                    "final_publication_allowed": False,
                }
            )
    total = len(rows)
    built = sum(1 for row in rows if row.get("built"))
    fail_closed = sum(
        1
        for row in rows
        if row.get("built")
        and not row.get("automated_pass")
        and not row.get("final_publication_allowed")
        and row.get("human_signoff_status") == "required_not_completed"
    )
    report = {
        "schema_version": "1.0",
        "dataset_kind": manifest.get("dataset_kind"),
        "not_for_real_performance_claim": True,
        "metrics": {
            "total": total,
            "built_count": built,
            "fail_closed_count": fail_closed,
            "auto_pass_count": sum(1 for row in rows if row.get("automated_pass")),
            "run_error_count": sum(1 for row in rows if row.get("status") == "error"),
            "final_publication_claim_count": sum(
                1 for row in rows if row.get("final_publication_allowed")
            ),
            "evidence_job_count": sum(int(row.get("evidence_job_count") or 0) for row in rows),
            "statistics_job_count": sum(
                int(row.get("statistics_job_count") or 0) for row in rows
            ),
            "build_rate": round(built / total, 4) if total else 0.0,
            "fail_closed_rate": round(fail_closed / total, 4) if total else 0.0,
        },
        "rows": rows,
        "limits": [
            "模拟坏稿不用于真实论文通过率。",
            "自动通过数为 0 属预期 fail-closed 基线；后续需增加自动修复与证据回填后复测。",
        ],
    }
    (out_dir / "corpus-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metrics = report["metrics"]
    lines = [
        "# 模拟坏稿预审回归",
        "",
        f"- 总数：{metrics['total']}",
        f"- 成功构建：{metrics['built_count']}",
        f"- fail-closed：{metrics['fail_closed_count']}",
        f"- 自动通过：{metrics['auto_pass_count']}",
        f"- 运行错误：{metrics['run_error_count']}",
        f"- 最终发表误声明：{metrics['final_publication_claim_count']}",
        f"- 证据作业：{metrics['evidence_job_count']}",
        f"- 数字分类作业：{metrics['statistics_job_count']}",
        "",
        "本结果仅为模拟坏稿门禁回归，不代表真实论文评测表现。",
        "",
    ]
    (out_dir / "corpus-report.md").write_text("\n".join(lines), encoding="utf-8")
    return report
