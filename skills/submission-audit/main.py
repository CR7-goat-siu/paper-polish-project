"""submission-audit Skill 入口（纯离线、确定性、标准库）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.skill_loader import run_skill  # noqa: E402
from utils.submission_audit import (  # noqa: E402
    apply_evidence_responses,
    apply_statistics_responses,
    build_evidence_jobs,
    build_preflight_package,
    build_statistics_jobs,
    run_audit,
)


def _build_package(args: dict[str, Any]) -> dict[str, Any]:
    input_file = args.get("input_file") or args.get("input")
    if not input_file:
        raise ValueError("package 模式需要 input_file")
    path = Path(str(input_file))
    if not path.exists():
        raise FileNotFoundError(f"论文文件不存在: {path}")
    language = str(args.get("language") or "en")
    parsed: dict[str, Any] | None = None
    if path.suffix.lower() == ".docx":
        parsed = run_skill("paper-parser", {"input": str(path), "format": "auto"})
        text = str(parsed.get("full_text") or "")
        title = str(args.get("title") or parsed.get("title") or path.stem)
    else:
        text = path.read_text(encoding="utf-8-sig")
        title = str(args.get("title") or path.stem)
    package = build_preflight_package(
        text,
        language=language,
        title=title,
        source_file=str(path.resolve()),
        parsed=parsed,
    )
    out_dir = Path(str(args.get("out") or "output/submission-audit"))
    out_dir.mkdir(parents=True, exist_ok=True)
    package_file = out_dir / "preflight-package.json"
    package_file.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    jobs = build_evidence_jobs(package, out_dir / "evidence", language=language)
    stats_jobs = build_statistics_jobs(package, out_dir / "statistics", language=language)
    return {
        "mode": "package",
        "status": "built",
        "language": language,
        "title": title,
        "package_file": str(package_file.resolve()),
        "evidence_jobs_file": str((out_dir / "evidence" / "evidence-jobs.json").resolve()),
        "evidence_job_count": jobs.get("job_count", 0),
        "statistics_jobs_file": str((out_dir / "statistics" / "statistics-jobs.json").resolve()),
        "statistics_job_count": stats_jobs.get("job_count", 0),
        "unresolved": package.get("unresolved") or {},
        "warnings": [
            "自动抽取包不含已核验原文证据；所有证据作业必须回填后才能通过证据门禁。"
            if jobs.get("job_count")
            else "未发现带编号引用的关键主张，证据绑定门禁保持失败闭锁。",
            "数字分类作业只补指标含义与比较组，不得修改原数字或把统计标为已验证。",
        ],
    }


def _bind_apply(args: dict[str, Any]) -> dict[str, Any]:
    package = args.get("package")
    package_file = args.get("package_file")
    if package is None:
        if not package_file:
            raise ValueError("bind-apply 模式需要 package 或 package_file")
        package = json.loads(Path(str(package_file)).read_text(encoding="utf-8"))
    if not isinstance(package, dict):
        raise ValueError("预审包顶层必须是 JSON 对象")
    out_dir = Path(str(args.get("out") or "output/submission-audit"))
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = Path(str(args.get("evidence_dir") or (out_dir / "evidence")))
    responses_dir = Path(str(args.get("responses_dir") or (evidence_dir / "responses")))
    updated, summary = apply_evidence_responses(
        package,
        responses_dir,
        evidence_dir=evidence_dir,
    )
    package_out = Path(str(args.get("package_out") or (out_dir / "preflight-package.bound.json")))
    package_out.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = out_dir / "evidence-response-report.json"
    summary["package_out"] = str(package_out.resolve())
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "mode": "bind-apply",
        "status": "applied" if summary.get("applied_count") else "no_responses",
        "package_file": str(package_out.resolve()),
        "response_report": str(report_path.resolve()),
        "package": updated,
        "summary": summary,
        "warnings": [
            "verified 证据仍要求参考文献元数据 verified、原文材料和位置完整；仅回填 JSON 不能绕过。"
        ],
    }


def _stats_apply(args: dict[str, Any]) -> dict[str, Any]:
    package = args.get("package")
    package_file = args.get("package_file")
    if package is None:
        if not package_file:
            raise ValueError("stats-apply 模式需要 package 或 package_file")
        package = json.loads(Path(str(package_file)).read_text(encoding="utf-8"))
    if not isinstance(package, dict):
        raise ValueError("预审包顶层必须是 JSON 对象")
    out_dir = Path(str(args.get("out") or "output/submission-audit"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stats_dir = Path(str(args.get("stats_dir") or (out_dir / "statistics")))
    responses_dir = Path(str(args.get("responses_dir") or (stats_dir / "responses")))
    updated, summary = apply_statistics_responses(
        package,
        responses_dir,
        stats_dir=stats_dir,
    )
    package_out = Path(
        str(args.get("package_out") or (out_dir / "preflight-package.stats.json"))
    )
    package_out.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = out_dir / "statistics-response-report.json"
    summary["package_out"] = str(package_out.resolve())
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "mode": "stats-apply",
        "status": summary.get("status"),
        "package_file": str(package_out.resolve()),
        "response_report": str(report_path.resolve()),
        "package": updated,
        "summary": summary,
        "warnings": [
            "数字分类不会改变原始值或 verification_status；冲突仍需作者确认或证据修复。"
        ],
    }


def _pipeline(args: dict[str, Any]) -> dict[str, Any]:
    """一条命令执行 package → 可选响应回填 → audit。"""
    out_dir = Path(str(args.get("out") or "output/submission-audit"))
    out_dir.mkdir(parents=True, exist_ok=True)
    package_result = _build_package(
        {
            "input_file": args.get("input_file") or args.get("input"),
            "language": args.get("language") or "en",
            "title": args.get("title") or "",
            "out": str(out_dir),
        }
    )
    package = json.loads(Path(package_result["package_file"]).read_text(encoding="utf-8"))
    applied_steps: list[dict[str, Any]] = []
    evidence_responses = args.get("evidence_responses_dir")
    if evidence_responses:
        package, summary = apply_evidence_responses(
            package,
            evidence_responses,
            evidence_dir=args.get("evidence_dir") or (out_dir / "evidence"),
        )
        bound_path = out_dir / "preflight-package.bound.json"
        bound_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        applied_steps.append(
            {
                "step": "bind-apply",
                "status": summary.get("status"),
                "applied_count": summary.get("applied_count", 0),
                "pending_count": summary.get("pending_count", 0),
                "package_file": str(bound_path.resolve()),
            }
        )
    stats_responses = args.get("stats_responses_dir")
    if stats_responses:
        package, summary = apply_statistics_responses(
            package,
            stats_responses,
            stats_dir=args.get("stats_dir") or (out_dir / "statistics"),
        )
        stats_path = out_dir / "preflight-package.stats.json"
        stats_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        applied_steps.append(
            {
                "step": "stats-apply",
                "status": summary.get("status"),
                "updates_applied": summary.get("updates_applied", 0),
                "comparisons_added": summary.get("comparisons_added", 0),
                "package_file": str(stats_path.resolve()),
            }
        )
    final_package = out_dir / "preflight-package.audit.json"
    final_package.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    audit = run_audit(
        package,
        docx_path=args.get("docx_path") or args.get("input_file") or args.get("input"),
        out_dir=out_dir / "audit",
        render=bool(args.get("render", False)),
        config_path=args.get("config"),
        version=str(args.get("version") or "pipeline"),
        journal=args.get("journal"),
    )
    return {
        "mode": "pipeline",
        "status": audit.get("status"),
        "package": package_result,
        "applied_steps": applied_steps,
        "audit": audit,
        "gate": audit.get("gate"),
        "files": audit.get("files"),
        "final_publication_allowed": False,
        "warnings": [
            "流水线只产出可投稿候选与签核材料；人工签核未完成且不得声称最终发表许可。"
        ],
    }


def run(args: dict[str, Any]) -> dict[str, Any]:
    mode = str(args.get("mode") or "audit").lower()
    if mode == "package":
        return _build_package(args)
    if mode == "bind-apply":
        return _bind_apply(args)
    if mode == "stats-apply":
        return _stats_apply(args)
    if mode == "pipeline":
        return _pipeline(args)
    if mode != "audit":
        raise ValueError(
            f"未知 submission-audit mode: {mode!r}，可选 audit / package / bind-apply / stats-apply / pipeline"
        )
    package = args.get("package")
    package_file = args.get("package_file")
    if package is None and package_file is None:
        raise ValueError("submission-audit 需要 package 或 package_file")
    return run_audit(
        package if package is not None else package_file,
        docx_path=args.get("docx_path"),
        out_dir=args.get("out") or "output/submission-audit",
        render=bool(args.get("render", False)),
        config_path=args.get("config"),
        version=str(args.get("version") or "round-1"),
        journal=args.get("journal"),
    )
