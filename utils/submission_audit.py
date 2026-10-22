"""投稿前预审的确定性核心。

该模块不调用模型、不读取密钥、不修改原稿。输入为结构化论文预审包，
输出证据图谱、统计登记、图表清单、DOCX 结构与真实渲染结果，以及
统一的候选终稿门禁。所有无法确认的信息一律进入风险与作者确认清单。
"""

from __future__ import annotations

import json
import hashlib
import re
import struct
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from utils.config_loader import PROJECT_ROOT, load_yaml_file
from utils.zotero_crossref import audit_zotero_docx

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NUMBER_RE = re.compile(r"(?<![\w.])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?%?")
CITATION_RE = re.compile(r"\[(\d{1,3}(?:\s*[,，\-–—]\s*\d{1,3})*)\]")
FIGURE_MENTION_RE = re.compile(
    r"(?i)\b(?:fig(?:ure)?\.?|table|图|表)\s*([0-9]+)\b"
)
MARKDOWN_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)[\s、.．]\s*(.{2,80})$"
)
REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:references?|bibliography|参\s*考\s*文\s*献|文\s*献)\s*[:：]?\s*$",
    re.IGNORECASE,
)
REFERENCE_LINE_RE = re.compile(r"^\s*\[(\d{1,3})\]\s*(.*)$")
CAPTION_LINE_RE = re.compile(
    r"^\s*(?:(?:fig(?:ure)?\.?)|table|图|表)\s*([0-9]+)\s*[.．:：-]\s*(.+)$",
    re.IGNORECASE,
)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
HEADING_STYLE_RE = re.compile(r"(?i)(?:heading|标题)\s*([1-6])")
ALLOWED_SUPPORT_TYPES = {"direct", "indirect", "background", "opposing"}
ALLOWED_SUPPORT_STRENGTH = {"high", "medium", "low"}
VERIFIED_STATES = {"verified", "confirmed", "passed"}


@dataclass
class TextBlock:
    location: str
    text: str
    section: str = ""
    block_id: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: str | Path) -> str:
    target = Path(path)
    if not target.exists() or not target.is_file():
        return ""
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(payload: Any) -> str:
    data = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _load_json_source(source: Any) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    if source is None:
        return {}
    path = Path(str(source))
    if not path.exists():
        raise FileNotFoundError(f"预审包不存在: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("预审包顶层必须是 JSON 对象")
    payload.setdefault("_package_dir", str(path.resolve().parent))
    return payload


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else (PROJECT_ROOT / "config" / "submission.yaml")
    return load_yaml_file(target) if target.exists() else {}


def _blocks_from_manuscript(text: str) -> list[TextBlock]:
    """把纯文本/Markdown 切成带位置的段落，不解析 LaTeX 结构。"""
    blocks: list[TextBlock] = []
    section = "Front matter"
    paragraph_index = 0
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        heading = MARKDOWN_HEADING_RE.match(line)
        numbered = NUMBERED_HEADING_RE.match(line)
        if heading:
            section = heading.group(2).strip()
            paragraph_index = 0
            blocks.append(
                TextBlock(
                    location=f"{section}:heading",
                    text=section,
                    section=section,
                    block_id=f"h{line_number}",
                )
            )
            continue
        if numbered and len(line) <= 100 and not line.endswith((".", "。", "!", "！", "?", "？")):
            section = numbered.group(1).strip()
            paragraph_index = 0
            blocks.append(
                TextBlock(
                    location=f"{section}:heading",
                    text=section,
                    section=section,
                    block_id=f"h{line_number}",
                )
            )
            continue
        paragraph_index += 1
        blocks.append(
            TextBlock(
                location=f"{section} ¶{paragraph_index}",
                text=line,
                section=section,
                block_id=f"l{line_number}",
            )
        )
    return blocks


def _blocks_from_parsed(parsed: dict[str, Any]) -> list[TextBlock]:
    blocks: list[TextBlock] = []
    for index, chunk in enumerate(parsed.get("chunks") or [], start=1):
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("content") or "").strip()
        if not text:
            continue
        heading = str(chunk.get("heading") or chunk.get("kind") or f"Block {index}")
        for paragraph_index, paragraph in enumerate(re.split(r"\n\s*\n", text), start=1):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            blocks.append(
                TextBlock(
                    location=f"{heading} ¶{paragraph_index}",
                    text=paragraph,
                    section=heading,
                    block_id=f"{chunk.get('id') or index}-p{paragraph_index}",
                )
            )
    if blocks:
        return blocks
    return _blocks_from_manuscript(str(parsed.get("full_text") or ""))


def _parse_reference_entries(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    in_references = False
    for raw in text.splitlines():
        line = raw.strip()
        heading_match = MARKDOWN_HEADING_RE.match(line)
        heading_text = heading_match.group(2).strip() if heading_match else line
        if REFERENCE_HEADING_RE.match(heading_text):
            in_references = True
            continue
        if in_references and (heading_match or NUMBERED_HEADING_RE.match(line)):
            break
        if not in_references:
            continue
        match = REFERENCE_LINE_RE.match(line)
        if not match:
            continue
        number = int(match.group(1))
        raw_entry = match.group(2).strip()
        doi_match = DOI_RE.search(raw_entry)
        year_match = YEAR_RE.search(raw_entry)
        entry = {
            "id": f"R{number}",
            "number": number,
            "raw": raw_entry,
            "doi": doi_match.group(0) if doi_match else "",
            "year": int(year_match.group(0)) if year_match else "",
            "title": "",
            "authors": [],
            "venue": "",
            "verification_status": "unverified",
            "auto_extracted": True,
            "missing_fields": [
                field
                for field, value in (
                    ("title", ""),
                    ("authors", []),
                    ("year", int(year_match.group(0)) if year_match else ""),
                    ("venue", ""),
                )
                if not value
            ],
        }
        entries.append(entry)
    return entries


def _extract_figure_manifest(blocks: list[TextBlock]) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for block in blocks:
        caption_match = CAPTION_LINE_RE.match(block.text)
        for match in FIGURE_MENTION_RE.finditer(block.text):
            token = match.group(0).lower()
            kind = "table" if token.startswith("table") or token.startswith("表") else "figure"
            number = int(match.group(1))
            key = _figure_key(kind, number)
            item = items.setdefault(
                key,
                {
                    "id": key,
                    "kind": kind,
                    "number": number,
                    "caption": "",
                    "body_reference_location": "",
                    "source_file": "",
                    "format": "",
                    "resolution_dpi": None,
                    "axis_labels": [],
                    "units": [],
                    "legend": False,
                    "significance": "",
                    "error_bars": "",
                    "sample_size_note": "",
                    "license": "",
                    "auto_extracted": True,
                },
            )
            if caption_match and int(caption_match.group(1)) == number:
                item["caption"] = caption_match.group(2).strip()
            elif not item.get("body_reference_location"):
                item["body_reference_location"] = block.location
    return list(items.values())


def build_preflight_package(
    text: str,
    *,
    language: str = "en",
    title: str = "",
    source_file: str = "",
    parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从正文自动生成 fail-closed 预审包。

    自动生成的 claim/reference/statistic/figure 仅作为待核验线索；
    不含任何伪造的原文证据，因此不能直接通过证据门禁。
    """
    blocks = _blocks_from_parsed(parsed) if parsed else _blocks_from_manuscript(text)
    references = _parse_reference_entries(text)
    claims = _auto_claims(blocks, references)
    statistics = _auto_number_registry(
        [block for block in blocks if not REFERENCE_HEADING_RE.match(block.section or "")]
    )
    figures = _extract_figure_manifest(blocks)
    return {
        "schema_version": "1.0",
        "package_status": "draft_unverified",
        "language": language,
        "title": title,
        "source_file": source_file,
        "blocks": [
            {"location": block.location, "section": block.section, "text": block.text}
            for block in blocks
        ],
        "paragraphs": [
            {"location": block.location, "section": block.section, "text": block.text}
            for block in blocks
        ],
        "claims": claims,
        "references": references,
        "statistics": {"registry": statistics, "comparisons": [], "recomputations": []},
        "figures": figures,
        "tables": parsed.get("docx_tables") if parsed else [],
        "unresolved": {
            "claims_without_evidence": len(claims),
            "references_without_verification": len(references),
            "statistics_without_verification": len(statistics),
            "figure_items_without_source": sum(1 for item in figures if not item.get("source_file")),
        },
    }


def build_evidence_jobs(
    package: dict[str, Any],
    out_dir: str | Path,
    *,
    language: str = "en",
) -> dict[str, Any]:
    """为“主张—引用”生成coding harness 原文证据核验作业。"""
    from utils.prompt_renderer import render_template

    target = Path(out_dir)
    prompts_dir = target / "prompts"
    responses_dir = target / "responses"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    references = {
        str(item.get("id")): item
        for item in package.get("references") or []
        if isinstance(item, dict) and item.get("id")
    }
    excerpts = package.get("source_excerpts") or {}
    jobs: list[dict[str, Any]] = []
    for claim_index, claim in enumerate(package.get("claims") or [], start=1):
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("id") or f"C{claim_index}")
        for reference_id in claim.get("citations") or []:
            reference_id = str(reference_id)
            job_id = f"{claim_id}-{reference_id}-evidence"
            source_material = ""
            if isinstance(excerpts, dict):
                source_material = str(excerpts.get(reference_id) or "")
            if not source_material:
                source_material = (
                    "NO SOURCE EXCERPTS PROVIDED. Return verification_status='unverified'. "
                    "Do not claim that the cited source supports the claim."
                )
            prompt = render_template(
                "evidence-bind.md",
                language,
                claim_id=claim_id,
                claim_text=str(claim.get("text") or ""),
                claim_location=str(claim.get("location") or ""),
                citation_id=reference_id,
                citation_metadata=references.get(reference_id, {}),
                source_material=source_material,
            )
            prompt_path = prompts_dir / f"{job_id}.prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            jobs.append(
                {
                    "job_id": job_id,
                    "claim_id": claim_id,
                    "reference_id": reference_id,
                    "prompt_file": str(prompt_path.resolve()),
                    "response_file": str((responses_dir / f"{job_id}.txt").resolve()),
                    "source_material_available": not source_material.startswith("NO SOURCE"),
                }
            )
    payload = {
        "schema_version": "1.0",
        "job_count": len(jobs),
        "jobs": jobs,
        "status": "built" if jobs else "no_citation_jobs",
    }
    (target / "evidence-jobs.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError("证据响应不是可解析 JSON")
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("证据响应顶层必须是 JSON 对象")
    return payload


def apply_evidence_responses(
    package: dict[str, Any],
    responses_dir: str | Path,
    *,
    evidence_dir: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把coding harness 的证据核验响应回填到预审包。

    `verified` 仍受参考元数据核验和“是否真的提供原文材料”约束；仅回填一个
    verified 字符串不能绕过证据门禁。
    """
    updated = json.loads(json.dumps(package, ensure_ascii=False))
    response_root = Path(responses_dir)
    jobs_path = Path(evidence_dir) / "evidence-jobs.json" if evidence_dir else response_root.parent / "evidence-jobs.json"
    job_records = {}
    if jobs_path.exists():
        jobs_payload = json.loads(jobs_path.read_text(encoding="utf-8"))
        job_records = {
            str(job.get("job_id")): job for job in jobs_payload.get("jobs") or [] if isinstance(job, dict)
        }
    references = {
        str(item.get("id")): item
        for item in updated.get("references") or []
        if isinstance(item, dict) and item.get("id")
    }
    rows: list[dict[str, Any]] = []
    pending: list[str] = []
    for claim in updated.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("id") or "")
        evidence_list = [item for item in claim.get("evidence") or [] if isinstance(item, dict)]
        existing = {(str(item.get("reference_id")), str(item.get("evidence_job_id") or "")) for item in evidence_list}
        for reference_id in claim.get("citations") or []:
            reference_id = str(reference_id)
            job_id = f"{claim_id}-{reference_id}-evidence"
            response_file = response_root / f"{job_id}.txt"
            if not response_file.exists():
                pending.append(job_id)
                rows.append({"job_id": job_id, "status": "pending_response"})
                continue
            try:
                payload = _parse_json_object(response_file.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                rows.append({"job_id": job_id, "status": "invalid_response", "error": str(exc)})
                continue
            if str(payload.get("claim_id") or "") != claim_id or str(payload.get("reference_id") or "") != reference_id:
                rows.append(
                    {
                        "job_id": job_id,
                        "status": "identity_mismatch",
                        "error": "响应中的 claim_id/reference_id 与作业不一致",
                    }
                )
                continue
            job = job_records.get(job_id) or {}
            source_available = bool(job.get("source_material_available", False))
            proposed = str(payload.get("verification_status") or "unverified").lower()
            quote = str(payload.get("quote") or "").strip()
            location = str(payload.get("source_location") or "").strip()
            support_type = str(payload.get("support_type") or "unverified").lower()
            support_strength = str(payload.get("support_strength") or "low").lower()
            ref = references.get(reference_id) or {}
            ref_status = str(
                ref.get("verification_status") or ref.get("verdict") or "unverified"
            ).lower()
            reasons: list[str] = []
            if proposed in VERIFIED_STATES:
                if not source_available:
                    reasons.append("未向证据作业提供可核验原文材料")
                if len(quote) < 12:
                    reasons.append("证据句不足 12 个字符")
                if not location:
                    reasons.append("缺少原文证据位置")
                if support_type not in ALLOWED_SUPPORT_TYPES:
                    reasons.append("支持类型无效")
                if ref_status not in VERIFIED_STATES:
                    reasons.append("参考文献元数据尚未 verified")
            final_status = "verified" if proposed in VERIFIED_STATES and not reasons else "unverified"
            evidence = {
                "reference_id": reference_id,
                "source_location": location,
                "quote": quote,
                "support_type": support_type,
                "support_strength": support_strength,
                "verification_status": final_status,
                "evidence_job_id": job_id,
                "response_received": True,
                "response_applied": True,
                "reasons": reasons,
            }
            key = (reference_id, job_id)
            if key in existing:
                evidence_list = [
                    evidence
                    if (
                        str(item.get("reference_id")) == reference_id
                        and str(item.get("evidence_job_id") or "") == job_id
                    )
                    else item
                    for item in evidence_list
                ]
            else:
                evidence_list.append(evidence)
                existing.add(key)
            rows.append(
                {
                    "job_id": job_id,
                    "status": "applied",
                    "proposed_status": proposed,
                    "final_status": final_status,
                    "reasons": reasons,
                }
            )
        claim["evidence"] = evidence_list
    updated["package_status"] = "evidence_responses_applied"
    summary = {
        "schema_version": "1.0",
        "response_dir": str(response_root.resolve()),
        "applied_count": sum(1 for row in rows if row.get("status") == "applied"),
        "pending_count": len(pending),
        "invalid_count": sum(1 for row in rows if row.get("status") == "invalid_response"),
        "identity_mismatch_count": sum(1 for row in rows if row.get("status") == "identity_mismatch"),
        "pending_jobs": pending,
        "rows": rows,
    }
    return updated, summary


def build_statistics_jobs(
    package: dict[str, Any],
    out_dir: str | Path,
    *,
    language: str = "en",
) -> dict[str, Any]:
    """为数字登记生成“分类与比较组”coding harness 作业。"""
    from utils.prompt_renderer import render_template

    target = Path(out_dir)
    prompts_dir = target / "prompts"
    responses_dir = target / "responses"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    registry = [
        item
        for item in (package.get("statistics") or {}).get("registry") or []
        if isinstance(item, dict)
    ]
    job_id = "statistics-classification"
    prompt = render_template(
        "stats-audit.md",
        language,
        registry=registry,
        title=str(package.get("title") or ""),
    )
    prompt_path = prompts_dir / f"{job_id}.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    job = {
        "job_id": job_id,
        "number_count": len(registry),
        "prompt_file": str(prompt_path.resolve()),
        "response_file": str((responses_dir / f"{job_id}.txt").resolve()),
    }
    payload = {
        "schema_version": "1.0",
        "job_count": 1 if registry else 0,
        "jobs": [job] if registry else [],
        "status": "built" if registry else "no_numbers",
    }
    (target / "statistics-jobs.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def apply_statistics_responses(
    package: dict[str, Any],
    responses_dir: str | Path,
    *,
    stats_dir: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """回填数字分类结果；禁止改原数字或把统计标为 verified。"""
    updated = json.loads(json.dumps(package, ensure_ascii=False))
    response_root = Path(responses_dir)
    job_id = "statistics-classification"
    response_file = response_root / f"{job_id}.txt"
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "response_file": str(response_file.resolve()),
        "status": "pending_response",
        "updates_applied": 0,
        "comparisons_added": 0,
        "recomputations_added": 0,
        "rejected_updates": [],
    }
    if not response_file.exists():
        return updated, report
    try:
        payload = _parse_json_object(response_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report["status"] = "invalid_response"
        report["error"] = str(exc)
        return updated, report
    stats = updated.setdefault("statistics", {})
    registry = [item for item in stats.get("registry") or [] if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in registry}
    allowed_fields = {"metric_key", "metric", "unit", "meaning", "critical", "figure_table_id"}
    for update in payload.get("updates") or []:
        if not isinstance(update, dict):
            continue
        number_id = str(update.get("id") or update.get("number_id") or "")
        entry = by_id.get(number_id)
        if entry is None:
            report["rejected_updates"].append({"id": number_id, "reason": "number_id_not_found"})
            continue
        if "value" in update and str(update.get("value")) != str(entry.get("value")):
            report["rejected_updates"].append({"id": number_id, "reason": "value_change_forbidden"})
            continue
        before = dict(entry)
        for field in allowed_fields:
            if field not in update:
                continue
            if field == "critical":
                if bool(entry.get("critical")) and not bool(update.get(field)):
                    continue
                entry[field] = bool(update.get(field))
            else:
                entry[field] = update.get(field)
        entry["statistics_classified"] = True
        entry["classification_source"] = job_id
        if entry != before:
            report["updates_applied"] += 1
    existing_comparisons = [
        item for item in stats.get("comparisons") or [] if isinstance(item, dict)
    ]
    existing_ids = {str(item.get("id")) for item in existing_comparisons}
    for index, comparison in enumerate(payload.get("comparisons") or []):
        if not isinstance(comparison, dict):
            continue
        ids = [str(item) for item in comparison.get("number_ids") or [] if str(item) in by_id]
        if len(ids) < 2:
            continue
        comparison_id = str(comparison.get("id") or f"CLASS-CMP-{index + 1}")
        if comparison_id in existing_ids:
            continue
        existing_comparisons.append(
            {
                "id": comparison_id,
                "number_ids": ids,
                "metric": str(comparison.get("metric") or ""),
                "source": job_id,
            }
        )
        existing_ids.add(comparison_id)
        report["comparisons_added"] += 1
    stats["comparisons"] = existing_comparisons
    existing_recomputations = [
        item for item in stats.get("recomputations") or [] if isinstance(item, dict)
    ]
    for index, item in enumerate(payload.get("recomputations") or []):
        if not isinstance(item, dict):
            continue
        input_ids = [str(value) for value in item.get("input_ids") or []]
        reported_id = str(item.get("reported_id") or item.get("output_id") or "")
        if not input_ids or any(number_id not in by_id for number_id in input_ids) or reported_id not in by_id:
            report["rejected_updates"].append(
                {"recompute": index + 1, "reason": "recompute_id_not_found"}
            )
            continue
        operation = str(item.get("operation") or "").lower()
        if operation not in {"sum", "mean", "difference", "ratio", "percent"}:
            report["rejected_updates"].append(
                {"recompute": index + 1, "reason": "unsupported_operation"}
            )
            continue
        existing_recomputations.append(
            {
                "id": str(item.get("id") or f"CLASS-RC-{index + 1}"),
                "operation": operation,
                "inputs": [by_id[number_id].get("value") for number_id in input_ids],
                "reported": by_id[reported_id].get("value"),
                "reported_id": reported_id,
                "input_ids": input_ids,
                "tolerance": item.get("tolerance"),
                "source": job_id,
            }
        )
        report["recomputations_added"] += 1
    stats["recomputations"] = existing_recomputations
    updated["package_status"] = "statistics_responses_applied"
    report["status"] = "applied"
    return updated, report


def _text_blocks(package: dict[str, Any]) -> list[TextBlock]:
    blocks: list[TextBlock] = []
    for index, item in enumerate(package.get("paragraphs") or []):
        if isinstance(item, str):
            blocks.append(TextBlock(location=f"paragraph:{index + 1}", text=item, block_id=f"p{index + 1}"))
        elif isinstance(item, dict):
            text = str(item.get("text") or "")
            if text:
                location = str(item.get("location") or f"paragraph:{index + 1}")
                blocks.append(
                    TextBlock(
                        location=location,
                        text=text,
                        section=str(item.get("section") or ""),
                        block_id=str(item.get("id") or f"p{index + 1}"),
                    )
                )
    if blocks:
        return blocks
    for section_index, section in enumerate(package.get("sections") or []):
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or section.get("title") or f"Section {section_index + 1}")
        paragraphs = section.get("paragraphs") or []
        if isinstance(paragraphs, str):
            paragraphs = [paragraphs]
        for paragraph_index, paragraph in enumerate(paragraphs):
            text = str(paragraph.get("text") if isinstance(paragraph, dict) else paragraph)
            if not text:
                continue
            blocks.append(
                TextBlock(
                    location=f"{heading}:paragraph:{paragraph_index + 1}",
                    text=text,
                    section=heading,
                    block_id=f"s{section_index + 1}p{paragraph_index + 1}",
                )
            )
    if blocks:
        return blocks
    text = str(package.get("text") or "")
    return [TextBlock(location="document", text=line, block_id=f"line-{i + 1}") for i, line in enumerate(text.splitlines()) if line.strip()]


def _expand_citation_numbers(inner: str) -> list[int]:
    numbers: list[int] = []
    for part in re.split(r"[,，]", inner):
        part = part.strip()
        range_match = re.fullmatch(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if 0 <= end - start <= 200:
                numbers.extend(range(start, end + 1))
        elif part.isdigit():
            numbers.append(int(part))
    return sorted(set(numbers))


def _reference_id(reference: dict[str, Any], index: int) -> str:
    return str(reference.get("id") or reference.get("number") or f"R{index + 1}")


def _reference_number(reference: dict[str, Any], index: int) -> int | None:
    raw = reference.get("number", reference.get("id"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return index + 1 if not str(raw or "").startswith("R") else None


def _auto_claims(blocks: list[TextBlock], references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    number_map: dict[int, str] = {}
    for index, ref in enumerate(references):
        number = _reference_number(ref, index)
        if number is not None:
            number_map[number] = _reference_id(ref, index)
    claims: list[dict[str, Any]] = []
    for block in blocks:
        for sentence_index, sentence in enumerate(re.split(r"(?<=[。！？.!?])\s*", block.text)):
            markers = CITATION_RE.findall(sentence)
            if not markers:
                continue
            numbers: list[int] = []
            for marker in markers:
                numbers.extend(_expand_citation_numbers(marker))
            citations = [number_map.get(number, f"#{number}") for number in numbers]
            claims.append(
                {
                    "id": f"AUTO-{block.block_id}-{sentence_index + 1}",
                    "text": sentence.strip(),
                    "location": block.location,
                    "critical": True,
                    "citations": sorted(set(citations)),
                    "evidence": [],
                    "auto_extracted": True,
                }
            )
    return claims


def analyze_evidence(package: dict[str, Any], blocks: list[TextBlock], config: dict[str, Any]) -> dict[str, Any]:
    thresholds = config.get("thresholds") or {}
    max_citations = int(thresholds.get("max_citations_per_claim", 8))
    min_quote = int(thresholds.get("min_evidence_quote_chars", 12))
    references = [item for item in package.get("references") or [] if isinstance(item, dict)]
    refs: dict[str, dict[str, Any]] = {}
    for index, reference in enumerate(references):
        refs[_reference_id(reference, index)] = dict(reference)
    claims = package.get("claims")
    if not isinstance(claims, list) or not claims:
        claims = _auto_claims(blocks, references)

    bindings: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    cited_ids: set[str] = set()
    critical_claim_count = 0
    bound_critical_claim_count = 0
    for claim_index, raw_claim in enumerate(claims):
        if not isinstance(raw_claim, dict):
            continue
        claim = dict(raw_claim)
        claim_id = str(claim.get("id") or f"C{claim_index + 1}")
        claim["id"] = claim_id
        claim_text = str(claim.get("text") or "")
        if not claim_text:
            findings.append(
                {"severity": "error", "code": "claim_text_missing", "claim_id": claim_id, "message": "关键主张缺少文本"}
            )
        citation_ids = [str(item) for item in claim.get("citations") or []]
        cited_ids.update(citation_ids)
        if len(citation_ids) > max_citations:
            findings.append(
                {
                    "severity": "warning",
                    "code": "citation_stacking",
                    "claim_id": claim_id,
                    "message": f"单条主张引用 {len(citation_ids)} 条，超过阈值 {max_citations}",
                }
            )
        evidence_items = [item for item in claim.get("evidence") or [] if isinstance(item, dict)]
        if not evidence_items:
            evidence_items = [
                {
                    "reference_id": citation_id,
                    "proposed_status": "unverified",
                    "support_type": "unverified",
                    "support_strength": "low",
                    "source_location": "",
                    "quote": "",
                }
                for citation_id in citation_ids
            ]
        has_bound = False
        for evidence_index, evidence in enumerate(evidence_items):
            evidence = dict(evidence)
            reference_id = str(evidence.get("reference_id") or evidence.get("citation_id") or "")
            reference = refs.get(reference_id)
            proposed = str(
                evidence.get("verification_status")
                or evidence.get("status")
                or evidence.get("proposed_status")
                or "unverified"
            ).lower()
            support_type = str(evidence.get("support_type") or "unverified").lower()
            support_strength = str(evidence.get("support_strength") or "low").lower()
            quote = str(evidence.get("quote") or evidence.get("evidence_quote") or "").strip()
            source_location = str(evidence.get("source_location") or evidence.get("evidence_location") or "").strip()
            reasons: list[str] = []
            if reference is None:
                reasons.append("引用不在参考文献表中")
            if reference_id not in citation_ids:
                reasons.append("证据对应的引用未出现在该主张的引用标记中")
            if support_type not in ALLOWED_SUPPORT_TYPES:
                reasons.append("支持类型无效或未核验")
            if support_strength not in ALLOWED_SUPPORT_STRENGTH:
                reasons.append("支持强度无效")
            if proposed in VERIFIED_STATES:
                if not quote:
                    reasons.append("标为 verified 但缺少原文证据句")
                elif len(quote) < min_quote:
                    reasons.append(f"证据句少于 {min_quote} 个字符")
                if not source_location:
                    reasons.append("标为 verified 但缺少原文证据位置")
                ref_status = str(
                    (reference or {}).get("verification_status")
                    or (reference or {}).get("verdict")
                    or "unverified"
                ).lower()
                if ref_status not in VERIFIED_STATES:
                    reasons.append("参考文献元数据尚未 verified")
            status = "verified" if proposed in VERIFIED_STATES and not reasons else "unverified"
            if proposed in VERIFIED_STATES and reasons:
                findings.append(
                    {
                        "severity": "error",
                        "code": "verified_without_evidence",
                        "claim_id": claim_id,
                        "reference_id": reference_id,
                        "message": "；".join(reasons),
                    }
                )
            if status == "verified":
                has_bound = True
            binding = {
                "claim_id": claim_id,
                "claim_text": claim_text,
                "claim_location": str(claim.get("location") or ""),
                "reference_id": reference_id,
                "reference_title": str((reference or {}).get("title") or ""),
                "doi": str((reference or {}).get("doi") or ""),
                "source_location": source_location,
                "quote": quote,
                "support_type": support_type,
                "support_strength": support_strength,
                "verification_status": status,
                "reasons": reasons,
                "binding_index": evidence_index + 1,
            }
            bindings.append(binding)
        is_critical = bool(claim.get("critical", True))
        if is_critical:
            critical_claim_count += 1
            if has_bound:
                bound_critical_claim_count += 1
            else:
                findings.append(
                    {
                        "severity": "error",
                        "code": "critical_claim_unbound",
                        "claim_id": claim_id,
                        "message": "关键主张没有通过核验的原位证据绑定",
                    }
                )
    orphan_refs = sorted(set(refs) - cited_ids)
    missing_refs = sorted(cited_ids - set(refs))
    for ref_id in orphan_refs:
        findings.append(
            {
                "severity": "warning",
                "code": "orphan_reference",
                "reference_id": ref_id,
                "message": "参考文献未在关键主张引用中出现",
            }
        )
    for ref_id in missing_refs:
        findings.append(
            {
                "severity": "error",
                "code": "missing_reference_entry",
                "reference_id": ref_id,
                "message": "正文引用了参考文献表中不存在的条目",
            }
        )
    verified_refs = sum(
        1
        for ref in refs.values()
        if str(ref.get("verification_status") or ref.get("verdict") or "").lower() in VERIFIED_STATES
    )
    binding_rate = bound_critical_claim_count / critical_claim_count if critical_claim_count else 0.0
    reference_rate = verified_refs / len(refs) if refs else 0.0
    minimum_claims = int(thresholds.get("minimum_critical_claim_count", 1))
    if critical_claim_count < minimum_claims:
        findings.append(
            {
                "severity": "error",
                "code": "critical_claim_missing",
                "message": f"关键主张数量 {critical_claim_count} 少于最低要求 {minimum_claims}",
            }
        )
    pass_gate = (
        critical_claim_count >= minimum_claims
        and binding_rate >= float(thresholds.get("evidence_binding_rate", 0.95))
        and reference_rate >= float(thresholds.get("reference_verification_rate", 0.95))
        and not missing_refs
    )
    return {
        "claims": claims,
        "bindings": bindings,
        "references": list(refs.values()),
        "orphan_references": orphan_refs,
        "missing_reference_entries": missing_refs,
        "critical_claim_count": critical_claim_count,
        "bound_critical_claim_count": bound_critical_claim_count,
        "evidence_binding_rate": round(binding_rate, 4),
        "reference_verification_rate": round(reference_rate, 4),
        "findings": findings,
        "pass": pass_gate,
    }


def _number_value(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    match = re.search(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?", str(raw or ""))
    return float(match.group(0)) if match else None


def _auto_number_registry(blocks: list[TextBlock]) -> list[dict[str, Any]]:
    registry: list[dict[str, Any]] = []
    for block in blocks:
        for index, match in enumerate(NUMBER_RE.finditer(block.text)):
            context_start = max(0, match.start() - 24)
            context_end = min(len(block.text), match.end() + 24)
            registry.append(
                {
                    "id": f"AUTO-{block.block_id}-{index + 1}",
                    "metric": "",
                    "value": match.group(0),
                    "unit": "",
                    "location": block.location,
                    "meaning": block.text[context_start:context_end].strip(),
                    "critical": False,
                    "recomputable": False,
                    "verification_status": "unverified",
                    "auto_extracted": True,
                }
            )
    return registry


def analyze_statistics(package: dict[str, Any], blocks: list[TextBlock], config: dict[str, Any]) -> dict[str, Any]:
    thresholds = config.get("thresholds") or {}
    settings = config.get("statistics") or {}
    raw = package.get("statistics") or {}
    if isinstance(raw, list):
        registry = [item for item in raw if isinstance(item, dict)]
        comparisons: list[dict[str, Any]] = []
        recomputations: list[dict[str, Any]] = []
    elif isinstance(raw, dict):
        registry = [item for item in raw.get("registry") or [] if isinstance(item, dict)]
        comparisons = [item for item in raw.get("comparisons") or [] if isinstance(item, dict)]
        recomputations = [item for item in raw.get("recomputations") or [] if isinstance(item, dict)]
    else:
        registry, comparisons, recomputations = [], [], []
    if not registry:
        registry = _auto_number_registry(blocks)
    findings: list[dict[str, Any]] = []
    normalized_registry: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(registry):
        entry = dict(item)
        entry_id = str(entry.get("id") or f"N{index + 1}")
        entry["id"] = entry_id
        value = _number_value(entry.get("value"))
        entry["numeric_value"] = value
        status = str(entry.get("verification_status") or "unverified").lower()
        if value is None:
            findings.append(
                {"severity": "error", "code": "stat_value_invalid", "number_id": entry_id, "message": "数值无法解析"}
            )
        if not entry.get("location"):
            findings.append(
                {"severity": "warning", "code": "stat_location_missing", "number_id": entry_id, "message": "数字缺少位置"}
            )
        if not entry.get("meaning") and not entry.get("metric"):
            findings.append(
                {"severity": "warning", "code": "stat_meaning_missing", "number_id": entry_id, "message": "数字缺少含义"}
            )
        if bool(entry.get("critical")) and status not in VERIFIED_STATES:
            severity = "error" if settings.get("require_critical_verified", True) else "warning"
            findings.append(
                {
                    "severity": severity,
                    "code": "critical_stat_unverified",
                    "number_id": entry_id,
                    "message": "关键统计数字尚未核验",
                }
            )
        by_id[entry_id] = entry
        normalized_registry.append(entry)

    abs_tol = float(settings.get("absolute_tolerance", 1e-6))
    rel_tol = float(settings.get("relative_tolerance", 0.01))
    conflict_count = 0
    comparison_reports: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for comparison in comparisons:
        ids = [str(item) for item in comparison.get("number_ids") or []]
        entries = [by_id[item] for item in ids if item in by_id]
        label = str(comparison.get("id") or comparison.get("metric") or f"CMP{len(comparison_reports) + 1}")
        values = [entry["numeric_value"] for entry in entries if entry.get("numeric_value") is not None]
        consistent = bool(values) and (max(values) - min(values) <= max(abs_tol, abs(sum(values) / len(values)) * rel_tol))
        if not consistent:
            conflict_count += 1
            findings.append(
                {
                    "severity": "error",
                    "code": "stat_cross_location_conflict",
                    "comparison_id": label,
                    "message": f"同一指标在不同位置的数值不一致: {values}",
                    "number_ids": ids,
                }
            )
        comparison_reports.append({"id": label, "number_ids": ids, "values": values, "consistent": consistent})
    for entry in normalized_registry:
        key = str(entry.get("metric_key") or entry.get("metric") or "").strip().lower()
        if key:
            groups.setdefault(key, []).append(entry)
    for key, entries in groups.items():
        if len(entries) < 2:
            continue
        compared_ids = {item for report in comparison_reports for item in report["number_ids"]}
        if any(entry["id"] in compared_ids for entry in entries):
            continue
        values = [entry["numeric_value"] for entry in entries if entry.get("numeric_value") is not None]
        if len(values) < 2:
            continue
        consistent = max(values) - min(values) <= max(abs_tol, abs(sum(values) / len(values)) * rel_tol)
        comparison_reports.append(
            {
                "id": f"AUTO-{key}",
                "number_ids": [entry["id"] for entry in entries],
                "values": values,
                "consistent": consistent,
                "auto_grouped": True,
            }
        )
        if not consistent:
            conflict_count += 1
            findings.append(
                {
                    "severity": "error",
                    "code": "stat_cross_location_conflict",
                    "comparison_id": f"AUTO-{key}",
                    "message": f"指标 {key!r} 在不同位置数值不一致: {values}",
                    "number_ids": [entry["id"] for entry in entries],
                }
            )

    recompute_reports: list[dict[str, Any]] = []
    for index, item in enumerate(recomputations):
        spec = dict(item)
        item_id = str(spec.get("id") or f"RC{index + 1}")
        operation = str(spec.get("operation") or "").lower()
        numbers = [_number_value(value) for value in spec.get("inputs") or []]
        reported = _number_value(spec.get("reported"))
        tolerance = _number_value(spec.get("tolerance"))
        tolerance = abs(tolerance) if tolerance is not None else max(abs_tol, abs(reported or 0) * rel_tol)
        calculated: float | None = None
        error = ""
        if any(value is None for value in numbers) or reported is None:
            error = "输入值或报告值无法解析"
        elif operation == "sum":
            calculated = sum(numbers)  # type: ignore[arg-type]
        elif operation == "mean":
            calculated = sum(numbers) / len(numbers) if numbers else None  # type: ignore[arg-type]
        elif operation == "difference":
            calculated = numbers[0] - sum(numbers[1:]) if numbers else None  # type: ignore[arg-type]
        elif operation == "ratio":
            calculated = numbers[0] / numbers[1] if len(numbers) >= 2 and numbers[1] else None  # type: ignore[operator]
        elif operation == "percent":
            calculated = numbers[0] / numbers[1] * 100 if len(numbers) >= 2 and numbers[1] else None  # type: ignore[operator]
        else:
            error = f"不支持的 operation: {operation!r}"
        consistent = calculated is not None and reported is not None and abs(calculated - reported) <= tolerance
        if not consistent:
            conflict_count += 1
            findings.append(
                {
                    "severity": "error",
                    "code": "stat_recompute_mismatch",
                    "recompute_id": item_id,
                    "message": error or f"重算值 {calculated} 与报告值 {reported} 不一致",
                }
            )
        recompute_reports.append(
            {
                "id": item_id,
                "operation": operation,
                "inputs": spec.get("inputs") or [],
                "reported": reported,
                "calculated": calculated,
                "tolerance": tolerance,
                "consistent": consistent,
                "error": error,
            }
        )
    critical_entries = [entry for entry in normalized_registry if entry.get("critical")]
    critical_verified = sum(
        1
        for entry in critical_entries
        if str(entry.get("verification_status") or "unverified").lower() in VERIFIED_STATES
    )
    pass_gate = conflict_count == 0 and (
        not settings.get("require_critical_verified", True)
        or (bool(critical_entries) and critical_verified == len(critical_entries))
    )
    return {
        "registry": normalized_registry,
        "comparisons": comparison_reports,
        "recomputations": recompute_reports,
        "conflict_count": conflict_count,
        "critical_stat_count": len(critical_entries),
        "critical_verified_count": critical_verified,
        "findings": findings,
        "pass": pass_gate,
    }


def analyze_docx_table_conflicts(tables: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """比较多个结果表中相同“伪影类型+方法”的定量指标。"""
    metric_names = {"SNR", "CC", "RRMSE"}
    observations: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    findings: list[dict[str, Any]] = []
    normalized_tables = 0
    for table in tables:
        table_index = int(table.get("index", 0))
        rows = [list(map(str, row)) for row in table.get("rows") or [] if isinstance(row, list)]
        header_index = None
        metric_columns: dict[str, int] = {}
        method_column = None
        for index, row in enumerate(rows[:5]):
            normalized = [str(cell).replace(" ", "").upper() for cell in row]
            found = {
                cell: column
                for column, cell in enumerate(normalized)
                if cell in metric_names
            }
            if len(found) >= 2:
                header_index = index
                metric_columns = found
                for column, cell in enumerate(row):
                    if str(cell).strip() in {"方法", "Method", "method"}:
                        method_column = column
                        break
                if method_column is None:
                    method_column = 1 if len(row) > 1 else 0
                break
        if header_index is None:
            continue
        normalized_tables += 1
        current_category = ""
        for row_index, row in enumerate(rows[header_index + 1 :], start=header_index + 1):
            if len(row) <= method_column:
                continue
            category = str(row[0]).strip() if row else ""
            if category:
                current_category = category
            method = str(row[method_column]).strip().replace("\n", " ")
            if not method or not current_category:
                continue
            method_key = "ours" if method in {"Ours", "ours", "原方法", "本文方法"} else method.lower()
            for metric, column in metric_columns.items():
                if column >= len(row):
                    continue
                value = _number_value(row[column])
                if value is None:
                    continue
                observations.setdefault(
                    (current_category, method_key, metric),
                    [],
                ).append(
                    {
                        "table_index": table_index,
                        "row_index": row_index,
                        "method": method,
                        "value": value,
                        "raw": row[column],
                    }
                )
    settings = config.get("statistics") or {}
    absolute_tolerance = float(settings.get("absolute_tolerance", 1e-6))
    relative_tolerance = float(settings.get("relative_tolerance", 0.01))
    comparisons: list[dict[str, Any]] = []
    for (category, method, metric), values in observations.items():
        if len(values) < 2:
            continue
        numeric = [item["value"] for item in values]
        baseline = sum(numeric) / len(numeric)
        tolerance = max(absolute_tolerance, abs(baseline) * relative_tolerance)
        consistent = max(numeric) - min(numeric) <= tolerance
        comparison = {
            "category": category,
            "method": method,
            "metric": metric,
            "values": values,
            "tolerance": tolerance,
            "consistent": consistent,
        }
        comparisons.append(comparison)
        if not consistent:
            findings.append(
                {
                    "severity": "error",
                    "code": "docx_table_cross_conflict",
                    "category": category,
                    "method": method,
                    "metric": metric,
                    "message": (
                        f"{category}/{method}/{metric} 在不同表格中不一致: "
                        f"{numeric}，容差 {tolerance:g}"
                    ),
                    "observations": values,
                }
            )
    return {
        "table_count": len(tables),
        "numeric_table_count": normalized_tables,
        "comparisons": comparisons,
        "conflict_count": len(findings),
        "findings": findings,
        "pass": not findings,
    }


def _figure_key(kind: str, number: Any) -> str:
    prefix = "T" if kind.lower() in {"table", "tab", "表"} else "F"
    return f"{prefix}{number}"


def _mention_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for match in FIGURE_MENTION_RE.finditer(text):
        token = match.group(0).lower()
        kind = "table" if token.startswith("table") or token.startswith("表") else "figure"
        keys.add(_figure_key(kind, match.group(1)))
    return keys


def _resolve_source_file(path_value: str, package_dir: Path) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    candidates = [path] if path.is_absolute() else [package_dir / path, PROJECT_ROOT / path]
    return next((candidate.resolve() for candidate in candidates if candidate.exists()), None)


def analyze_figures(package: dict[str, Any], blocks: list[TextBlock], config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("figures") or {}
    package_dir = Path(str(package.get("_package_dir") or PROJECT_ROOT))
    items = [item for item in package.get("figures") or [] if isinstance(item, dict)]
    all_text = "\n".join(block.text for block in blocks)
    mentions = _mention_keys(all_text)
    findings: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(items):
        item = dict(raw)
        kind = str(item.get("kind") or "figure").lower()
        number = item.get("number") or index + 1
        key = str(item.get("id") or _figure_key(kind, number))
        item["id"] = key
        item["number"] = number
        item["kind"] = "table" if kind in {"table", "tab", "表"} else "figure"
        if key in seen:
            findings.append(
                {"severity": "error", "code": "duplicate_figure_id", "item_id": key, "message": "图表编号重复"}
            )
        seen.add(key)
        caption = str(item.get("caption") or item.get("title") or "").strip()
        if not caption:
            findings.append(
                {"severity": "error", "code": "caption_missing", "item_id": key, "message": "图/表标题缺失"}
            )
        body_reference = str(item.get("body_reference_location") or "").strip()
        if not body_reference and key not in mentions:
            findings.append(
                {"severity": "error", "code": "body_reference_missing", "item_id": key, "message": "正文未引用该图/表"}
            )
        source_file = str(item.get("source_file") or item.get("source") or "").strip()
        resolved = _resolve_source_file(source_file, package_dir)
        if settings.get("require_source_file", True) and not resolved:
            findings.append(
                {"severity": "error", "code": "source_file_missing", "item_id": key, "message": "源文件缺失或路径不可解析"}
            )
        item["source_file_resolved"] = str(resolved) if resolved else ""
        fmt = str(item.get("format") or (resolved.suffix.lstrip(".") if resolved else "")).lower()
        supported = {str(value).lower() for value in settings.get("supported_figure_formats") or []}
        if item["kind"] == "figure" and supported and fmt not in supported:
            findings.append(
                {
                    "severity": "error",
                    "code": "unsupported_figure_format",
                    "item_id": key,
                    "message": f"图源格式 {fmt!r} 不在允许列表 {sorted(supported)}",
                }
            )
        dpi = _number_value(item.get("resolution_dpi") or item.get("dpi"))
        minimum_dpi = _number_value(settings.get("minimum_figure_dpi")) or 300
        if item["kind"] == "figure" and dpi is not None and dpi < minimum_dpi:
            findings.append(
                {
                    "severity": "error",
                    "code": "figure_resolution_low",
                    "item_id": key,
                    "message": f"分辨率 {dpi:g} dpi 低于阈值 {minimum_dpi:g}",
                }
            )
        is_chart = bool(item.get("is_chart") or item.get("chart_type"))
        if is_chart and settings.get("require_axis_labels_for_charts", True) and not item.get("axis_labels"):
            findings.append(
                {"severity": "error", "code": "axis_labels_missing", "item_id": key, "message": "定量图表缺少轴标签"}
            )
        if is_chart and settings.get("require_units_for_quantitative_axes", True) and not item.get("units"):
            findings.append(
                {"severity": "error", "code": "axis_units_missing", "item_id": key, "message": "定量图表缺少单位"}
            )
        if item.get("multiseries") and settings.get("require_legend_for_multiseries", True) and not item.get("legend"):
            findings.append(
                {"severity": "error", "code": "legend_missing", "item_id": key, "message": "多系列图缺少图例"}
            )
        if item.get("inferential") and settings.get("require_significance_for_inferential", True) and not item.get("significance"):
            findings.append(
                {"severity": "error", "code": "significance_missing", "item_id": key, "message": "推断统计图缺少显著性标记说明"}
            )
        if item.get("dispersion") and settings.get("require_error_bars_for_dispersion", True) and not item.get("error_bars"):
            findings.append(
                {"severity": "error", "code": "error_bars_missing", "item_id": key, "message": "离散统计图缺少误差线说明"}
            )
        if settings.get("require_sample_size_note", True) and not (item.get("sample_size_note") or item.get("sample_size")):
            findings.append(
                {"severity": "error", "code": "figure_sample_size_missing", "item_id": key, "message": "图注缺少样本量说明"}
            )
        if settings.get("require_license", True) and not item.get("license"):
            findings.append(
                {"severity": "warning", "code": "figure_license_missing", "item_id": key, "message": "图/表许可或版权状态未登记"}
            )
        normalized.append(item)
    manifest_keys = {str(item["id"]) for item in normalized}
    missing_items = sorted(mentions - manifest_keys)
    for key in missing_items:
        findings.append(
            {"severity": "error", "code": "referenced_item_missing", "item_id": key, "message": "正文引用了未登记的图/表"}
        )
    figure_numbers = sorted(int(item["number"]) for item in normalized if item["kind"] == "figure" and str(item["number"]).isdigit())
    table_numbers = sorted(int(item["number"]) for item in normalized if item["kind"] == "table" and str(item["number"]).isdigit())
    gaps: list[str] = []
    for prefix, numbers in (("F", figure_numbers), ("T", table_numbers)):
        expected = set(range(1, max(numbers) + 1)) if numbers else set()
        gaps.extend(f"{prefix}{number}" for number in sorted(expected - set(numbers)))
    for key in gaps:
        findings.append(
            {"severity": "error", "code": "figure_number_gap", "item_id": key, "message": "图/表编号跳号"}
        )
    required_total = len(normalized) + len(missing_items)
    complete_count = sum(
        1
        for item in normalized
        if not any(finding.get("item_id") == item["id"] and finding.get("severity") == "error" for finding in findings)
    )
    completeness = complete_count / required_total if required_total else 0.0
    pass_gate = (
        bool(normalized)
        and not missing_items
        and not gaps
        and completeness >= float((config.get("thresholds") or {}).get("figure_completeness_rate", 1.0))
        and not any(finding.get("severity") == "error" for finding in findings)
    )
    return {
        "items": normalized,
        "body_mentions": sorted(mentions),
        "missing_items": missing_items,
        "number_gaps": gaps,
        "completeness_rate": round(completeness, 4),
        "findings": findings,
        "pass": pass_gate,
    }


def _docx_paragraph_texts(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    texts: list[str] = []
    for paragraph in root.iter(f"{{{WORD_NS}}}p"):
        parts = [node.text or "" for node in paragraph.iter(f"{{{WORD_NS}}}t")]
        value = "".join(parts).strip()
        if value:
            texts.append(value)
    return texts


def inspect_docx(docx_path: str | Path, package: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("docx") or {}
    path = Path(docx_path)
    findings: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "path": str(path.resolve()) if path.exists() else str(path),
        "exists": path.exists(),
        "is_zip": False,
        "paragraph_count": 0,
        "table_count": 0,
        "media_count": 0,
        "heading_style_count": 0,
        "outline_heading_count": 0,
        "header_count": 0,
        "footer_count": 0,
        "page_margin_cm": {},
        "line_numbering": False,
        "findings": findings,
    }
    if not path.exists():
        findings.append({"severity": "error", "code": "docx_missing", "message": "DOCX 文件不存在"})
        report["score"] = 0.0
        report["pass"] = False
        return report
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "word/document.xml"}
            absent = sorted(required - names)
            if absent:
                findings.append(
                    {"severity": "error", "code": "docx_package_invalid", "message": f"DOCX 缺少 {absent}"}
                )
            report["is_zip"] = True
            if "word/document.xml" in names:
                document_xml = archive.read("word/document.xml")
                root = ET.fromstring(document_xml)
                paragraphs = _docx_paragraph_texts(document_xml)
                report["paragraph_count"] = len(paragraphs)
                tables = list(root.iter(f"{{{WORD_NS}}}tbl"))
                report["table_count"] = len(tables)
                report["media_count"] = len([name for name in names if name.startswith("word/media/")])
                header_repeat_count = 0
                no_split_row_count = 0
                table_row_count = 0
                tables_with_rows = 0
                for table in tables:
                    rows = table.findall(f"{{{WORD_NS}}}tr")
                    if not rows:
                        continue
                    tables_with_rows += 1
                    table_row_count += len(rows)
                    first_row_header = rows[0].find(
                        f"{{{WORD_NS}}}trPr/{{{WORD_NS}}}tblHeader"
                    )
                    if first_row_header is not None:
                        header_repeat_count += 1
                    for row in rows:
                        if row.find(
                            f"{{{WORD_NS}}}trPr/{{{WORD_NS}}}cantSplit"
                        ) is not None:
                            no_split_row_count += 1
                report["table_header_repeat_count"] = header_repeat_count
                report["table_row_count"] = table_row_count
                report["table_no_split_row_count"] = no_split_row_count
                if tables_with_rows and header_repeat_count != tables_with_rows:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "table_header_repeat_missing",
                            "message": (
                                f"仅 {header_repeat_count}/{tables_with_rows} 个表格首行"
                                "设置为跨页重复表头"
                            ),
                        }
                    )
                if tables_with_rows and no_split_row_count != table_row_count:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "table_row_split_enabled",
                            "message": (
                                f"仅 {no_split_row_count}/{table_row_count} 个表格行"
                                "设置为禁止跨页拆断"
                            ),
                        }
                    )
                field_artifacts = []
                for instruction in root.iter(f"{{{WORD_NS}}}instrText"):
                    value = (instruction.text or "").strip()
                    if value and (
                        "ZOTERO_" in value.upper()
                        or "CSL_BIBLIOGRAPHY" in value.upper()
                        or "CSL_CITATION" in value.upper()
                    ):
                        field_artifacts.append(value)
                for simple_field in root.iter(f"{{{WORD_NS}}}fldSimple"):
                    value = str(
                        simple_field.attrib.get(f"{{{WORD_NS}}}instr") or ""
                    ).strip()
                    if value and (
                        "ZOTERO_" in value.upper()
                        or "CSL_BIBLIOGRAPHY" in value.upper()
                        or "CSL_CITATION" in value.upper()
                    ):
                        field_artifacts.append(value)
                report["field_artifacts"] = field_artifacts
                if field_artifacts:
                    zotero_audit = audit_zotero_docx(path)
                    report["zotero_crossref"] = zotero_audit
                    if not zotero_audit.get("pass"):
                        findings.append(
                            {
                                "severity": "error",
                                "code": "docx_field_artifact",
                                "message": (
                                    "Zotero 域结构不完整或缺少文档偏好："
                                    f"unclosed={zotero_audit.get('unclosed_field_count')}, "
                                    f"malformed={zotero_audit.get('malformed_field_count')}, "
                                    f"prefs={zotero_audit.get('has_document_preferences')}"
                                ),
                            }
                        )
                else:
                    report["zotero_crossref"] = {
                        "present": False,
                        "citation_field_count": 0,
                        "bibliography_field_count": 0,
                    }
                heading_styles = 0
                outline_headings = 0
                line_numbering = False
                margins: dict[str, float] = {}
                headers = len([name for name in names if re.fullmatch(r"word/header\d+\.xml", name)])
                footers = len([name for name in names if re.fullmatch(r"word/footer\d+\.xml", name)])
                for style_text in (archive.read(name) for name in names if name.startswith("word/heading") or name == "word/styles.xml"):
                    match = HEADING_STYLE_RE.search(style_text.decode("utf-8", errors="ignore"))
                    if match:
                        heading_styles += 1
                for node in root.iter():
                    if node.tag == f"{{{WORD_NS}}}pStyle":
                        value = next(iter(node.attrib.values()), "")
                        if HEADING_STYLE_RE.search(value):
                            heading_styles += 1
                    elif node.tag == f"{{{WORD_NS}}}lnNumType":
                        line_numbering = True
                    elif node.tag == f"{{{WORD_NS}}}outlineLvl":
                        outline_headings += 1
                    elif node.tag == f"{{{WORD_NS}}}pgMar":
                        for side in ("top", "right", "bottom", "left"):
                            raw = node.attrib.get(f"{{{WORD_NS}}}{side}")
                            if raw and raw.lstrip("-").isdigit():
                                margins[side] = round(int(raw) / 1440 * 2.54, 2)
                report["heading_style_count"] = heading_styles
                report["outline_heading_count"] = outline_headings
                report["header_count"] = headers
                report["footer_count"] = footers
                report["page_margin_cm"] = margins
                report["line_numbering"] = line_numbering
                if not paragraphs:
                    findings.append({"severity": "error", "code": "docx_text_empty", "message": "DOCX 无可读正文"})
                if (
                    settings.get("require_heading_styles", True)
                    and heading_styles == 0
                    and outline_headings == 0
                ):
                    findings.append(
                        {
                            "severity": "error",
                            "code": "heading_styles_missing",
                            "message": "未检测到 Word 标题样式或 outline level",
                        }
                    )
                if settings.get("require_headers_or_footers", True) and headers + footers == 0:
                    findings.append(
                        {"severity": "warning", "code": "header_footer_missing", "message": "未检测到页眉或页脚"}
                    )
                if settings.get("require_page_margins", True) and len(margins) < 4:
                    findings.append(
                        {"severity": "error", "code": "page_margins_incomplete", "message": "页边距定义不完整"}
                    )
                expected_margin = _number_value(settings.get("default_page_margin_cm"))
                tolerance = _number_value(settings.get("margin_tolerance_cm")) or 0.25
                if expected_margin is not None and len(margins) == 4:
                    deviations = {side: abs(value - expected_margin) for side, value in margins.items()}
                    if any(value > tolerance for value in deviations.values()):
                        findings.append(
                            {
                                "severity": "warning",
                                "code": "page_margin_template_deviation",
                                "message": f"页边距偏离默认模板 {expected_margin:g}±{tolerance:g} cm: {margins}",
                            }
                        )
                if report["media_count"] == 0 and any(
                    str(item.get("kind")) == "figure" for item in package.get("figures") or []
                ):
                    findings.append(
                        {
                            "severity": "error",
                            "code": "docx_figure_media_missing",
                            "message": "图表清单包含图片，但 DOCX 未包含媒体文件",
                        }
                    )
    except (zipfile.BadZipFile, ET.ParseError, OSError) as exc:
        findings.append(
            {"severity": "error", "code": "docx_unreadable", "message": f"DOCX 结构检查失败: {exc}"}
        )
    errors = sum(1 for item in findings if item["severity"] == "error")
    warnings = sum(1 for item in findings if item["severity"] == "warning")
    score = max(0.0, 5.0 - errors * 1.0 - warnings * 0.25)
    report["score"] = round(score, 2)
    report["pass"] = errors == 0 and score >= float((config.get("thresholds") or {}).get("docx_structural_score", 4.0))
    return report


def _png_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()[:24]
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _png_likely_nonblank(path: Path) -> bool:
    try:
        payload = path.read_bytes()
    except OSError:
        return False
    size = _png_size(path)
    if not size:
        return False
    width, height = size
    return width >= 100 and height >= 100 and len(payload) >= 2_000


def _find_executable(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def render_docx(
    docx_path: str | Path,
    out_dir: str | Path,
    config: dict[str, Any],
    *,
    enabled: bool = False,
) -> dict[str, Any]:
    settings = config.get("docx") or {}
    source = Path(docx_path)
    target_dir = Path(out_dir)
    report: dict[str, Any] = {
        "enabled": bool(enabled),
        "required": bool(settings.get("require_word_or_pdf_render", True)),
        "backend": "",
        "pdf": "",
        "page_images": [],
        "page_count": 0,
        "blank_pages": [],
        "automated_visual_checks": {},
        "limitations": [],
        "pass": False,
    }
    if not enabled:
        report["limitations"].append("未请求真实渲染；不能声称已通过页面级验收")
        return report
    if not source.exists():
        report["limitations"].append("DOCX 不存在")
        return report
    target_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = target_dir / f"{source.stem}.render.pdf"
    backend = ""
    errors: list[str] = []
    if sys.platform == "win32":
        powershell = _find_executable("powershell")
        script = PROJECT_ROOT / "skills" / "submission-audit" / "render_word.ps1"
        if powershell and script.exists():
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    str(source.resolve()),
                    str(pdf_path.resolve()),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
            if completed.returncode == 0 and pdf_path.exists():
                backend = "word_com"
            else:
                detail = (completed.stderr or completed.stdout or "").strip()
                errors.append(f"Word COM: {detail[:500]}")
    if not backend:
        soffice = _find_executable("soffice") or _find_executable("libreoffice")
        if soffice:
            completed = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(target_dir), str(source)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
            if completed.returncode == 0 and pdf_path.exists():
                backend = "libreoffice"
            else:
                errors.append(f"LibreOffice: {(completed.stderr or completed.stdout or '').strip()[:500]}")
    if not backend and pdf_path.exists():
        backend = "existing_pdf"
    report["backend"] = backend
    report["pdf"] = str(pdf_path.resolve()) if pdf_path.exists() else ""
    if not backend:
        report["limitations"].extend(errors or ["Word/LibreOffice 渲染不可用"])
        report["limitations"].append("仅完成结构化 DOCX 检查，未完成真实渲染验收")
        return report
    pdftoppm = _find_executable("pdftoppm")
    if not pdftoppm:
        report["limitations"].append("pdftoppm 不可用；已生成 PDF，但未生成页面图像")
        report["pass"] = bool(settings.get("require_word_or_pdf_render", True) and not settings.get("require_page_images", True))
        return report
    prefix = target_dir / "page"
    for old in target_dir.glob("page-*.png"):
        try:
            old.unlink()
        except OSError:
            pass
    dpi = str(int(_number_value(settings.get("render_dpi")) or 120))
    completed = subprocess.run(
        [pdftoppm, "-png", "-r", dpi, str(pdf_path), str(prefix)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    images = sorted(target_dir.glob("page-*.png"))
    if completed.returncode != 0 or not images:
        report["limitations"].append(f"PDF 页面图生成失败: {(completed.stderr or completed.stdout or '').strip()[:500]}")
        return report
    blank_pages = [str(path.name) for path in images if not _png_likely_nonblank(path)]
    report.update(
        {
            "page_images": [str(path.resolve()) for path in images],
            "page_count": len(images),
            "blank_pages": blank_pages,
            "limitations": report["limitations"]
            + ["自动检查验证页面可渲染且非空；字体、重叠、表格溢出的最终视觉判断仍需作者/通讯作者签核"],
        }
    )
    report["automated_visual_checks"] = {
        "pdf_exists": pdf_path.exists(),
        "all_pages_rendered": len(images) >= 1,
        "all_pages_nonblank": not blank_pages,
        "page_count_positive": len(images) > 0,
    }
    report["score"] = 5.0 if not blank_pages else max(0.0, 5.0 - len(blank_pages))
    report["pass"] = (
        pdf_path.exists()
        and not blank_pages
        and report["score"] >= float((config.get("thresholds") or {}).get("docx_render_score", 4.0))
    )
    return report


def find_placeholders(blocks: list[TextBlock], config: dict[str, Any]) -> list[dict[str, Any]]:
    patterns = [str(item) for item in (config.get("placeholders") or {}).get("patterns") or []]
    findings: list[dict[str, Any]] = []
    for block in blocks:
        for pattern in patterns:
            if pattern and pattern in block.text:
                findings.append(
                    {
                        "severity": "error",
                        "code": "unresolved_placeholder",
                        "location": block.location,
                        "token": pattern,
                        "message": f"正文残留未解决占位符 {pattern!r}",
                    }
                )
    return findings


def quality_scores(
    evidence: dict[str, Any],
    statistics: dict[str, Any],
    figures: dict[str, Any],
    docx: dict[str, Any],
    render: dict[str, Any],
    placeholders: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    thresholds = config.get("thresholds") or {}

    def ratio_score(value: float, target: float) -> float:
        if target <= 0:
            return 5.0 if value >= target else 0.0
        return round(min(5.0, max(0.0, value / target * 4.0 + (1.0 if value >= target else 0.0))), 2)

    evidence_score = ratio_score(evidence.get("evidence_binding_rate", 0.0), float(thresholds.get("evidence_binding_rate", 0.95)))
    if evidence.get("missing_reference_entries"):
        evidence_score = min(evidence_score, 1.0)
    stats_score = 5.0 if statistics.get("pass") and statistics.get("conflict_count") == 0 else max(
        0.0, 3.0 - float(statistics.get("conflict_count", 0))
    )
    figures_score = ratio_score(
        float(figures.get("completeness_rate", 0.0)),
        float(thresholds.get("figure_completeness_rate", 1.0)),
    )
    docx_score = float(docx.get("score", 0.0))
    render_score = float(render.get("score", 0.0)) if render.get("pass") else 0.0
    trace_score = 5.0 if evidence.get("pass") and not placeholders else 1.0 if evidence.get("pass") else 0.0
    package_score = 5.0 if docx.get("pass") and render.get("pass") and figures.get("pass") else min(docx_score, figures_score)
    fabrication_codes = {
        "verified_without_evidence",
        "missing_reference_entry",
        "stat_recompute_mismatch",
        "duplicate_figure_id",
    }
    evidence_codes = {str(item.get("code")) for item in evidence.get("findings") or []}
    stat_codes = {str(item.get("code")) for item in statistics.get("findings") or []}
    figure_codes = {str(item.get("code")) for item in figures.get("findings") or []}
    fabrication_score = 0.0 if (evidence_codes | stat_codes | figure_codes) & fabrication_codes else 4.0
    return {
        "language_accuracy": None,
        "academic_tone": None,
        "structure_logic": None,
        "argument_chain": None,
        "terminology": None,
        "citation_format": None,
        "evidence_traceability": evidence_score,
        "statistics_consistency": round(stats_score, 2),
        "figure_table_integrity": figures_score,
        "docx_render": round(min(docx_score, render_score) if render.get("enabled") else docx_score, 2),
        "journal_format": None,
        "submission_package": round(package_score, 2),
        "placeholder_integrity": 5.0 if not placeholders else 0.0,
        "fabrication_risk": fabrication_score,
        "human_signoff_completeness": 0.0,
        "external_review_required": [
            "language_accuracy",
            "academic_tone",
            "structure_logic",
            "argument_chain",
            "terminology",
            "citation_format",
            "journal_format",
        ],
    }


def build_human_signoff(docx_path: str, config: dict[str, Any]) -> str:
    checks = config.get("human_signoff_checks") or []
    lines = [
        "# 人工签核清单",
        "",
        "本清单是投稿前硬门禁。自动化不得绕过，也不代表最终发表许可。",
        "",
        f"- 候选文件：`{docx_path or '(未提供)'}`",
        "- 签核角色：作者 / 通讯作者",
        "",
    ]
    for item in checks:
        lines.append(f"- [ ] {item}")
    lines.extend(
        [
            "",
            "## 签发",
            "",
            "- 作者签字：________________",
            "- 通讯作者签字：________________",
            "- 签核日期：________________",
            "- 声明：以上签核完成后，本文件才可进入目标期刊投稿流程。",
            "",
        ]
    )
    return "\n".join(lines)


def build_author_confirmation(
    evidence: dict[str, Any],
    statistics: dict[str, Any],
    figures: dict[str, Any],
    placeholders: list[dict[str, Any]],
) -> str:
    items: list[str] = []
    for finding in evidence.get("findings") or []:
        if finding.get("severity") == "error":
            items.append(
                f"引用/证据 `{finding.get('claim_id') or finding.get('reference_id') or '-'}`：{finding.get('message')}"
            )
    for finding in statistics.get("findings") or []:
        if finding.get("severity") == "error":
            items.append(
                f"统计 `{finding.get('number_id') or finding.get('comparison_id') or finding.get('recompute_id') or '-'}`：{finding.get('message')}"
            )
    for finding in figures.get("findings") or []:
        if finding.get("severity") in {"error", "warning"}:
            items.append(f"图表 `{finding.get('item_id') or '-'}`：{finding.get('message')}")
    for finding in placeholders:
        items.append(f"占位符 `{finding.get('location')}`：{finding.get('message')}")
    if not items:
        items.append("未发现需要作者补充的确定性风险；仍需完成人工签核中的真实性确认。")
    lines = [
        "# 作者确认清单",
        "",
        "以下问题不得由自动化编造答案；请作者提供事实、证据或明确授权。",
        "",
    ]
    lines.extend(f"- [ ] {item}" for item in items)
    lines.append("")
    return "\n".join(lines)


def build_risk_register(evidence: dict[str, Any], statistics: dict[str, Any], figures: dict[str, Any], docx: dict[str, Any], render: dict[str, Any], placeholders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    risks = []
    for source, findings in (
        ("evidence", evidence.get("findings") or []),
        ("statistics", statistics.get("findings") or []),
        ("figures", figures.get("findings") or []),
        ("docx", docx.get("findings") or []),
        ("placeholders", placeholders),
    ):
        for finding in findings:
            if finding.get("severity") in {"error", "warning"}:
                risk = dict(finding)
                risk["source"] = source
                risks.append(risk)
    for limitation in render.get("limitations") or []:
        risks.append(
            {
                "source": "render",
                "severity": "warning",
                "code": "render_limitation",
                "message": limitation,
            }
        )
    return risks


def build_markdown(report: dict[str, Any]) -> str:
    evidence = report["evidence"]
    statistics = report["statistics"]
    figures = report["figures"]
    docx = report["docx"]
    render = report["render"]
    gate = report["gate"]
    lines = [
        f"# 投稿前预审报告 {report['version']}",
        "",
        f"- 定位：{report['positioning']['product']}",
        "- 最终发表声明：禁止；必须完成作者/通讯作者人工签核",
        f"- 生成时间：{report['generated_at']}",
        f"- 状态：`{report['status']}`",
        f"- 候选终稿：{'允许进入人工签核' if gate['candidate_ready_for_signoff'] else '暂不可签发'}",
        "",
        "## 硬门禁",
        "",
        "| 门禁 | 结果 | 指标 |",
        "| --- | --- | --- |",
        f"| 引用证据绑定 | {'PASS' if evidence['pass'] else 'FAIL'} | {evidence['evidence_binding_rate']:.2%} |",
        f"| 统计一致性 | {'PASS' if statistics['pass'] else 'FAIL'} | conflicts={statistics['conflict_count']} |",
        f"| 图表完整性 | {'PASS' if figures['pass'] else 'FAIL'} | {figures['completeness_rate']:.2%} |",
        f"| DOCX 结构 | {'PASS' if docx['pass'] else 'FAIL'} | score={docx['score']}/5 |",
        f"| DOCX 真实渲染 | {'PASS' if render['pass'] else 'FAIL' if render['enabled'] else 'NOT RUN'} | pages={render['page_count']} |",
        f"| 占位符 | {'PASS' if not report['placeholders'] else 'FAIL'} | count={len(report['placeholders'])} |",
        f"| 人工签核 | REQUIRED | 未执行，不得自动化替代 |",
        "",
        "## 输出物",
        "",
    ]
    for name, path in report["files"].items():
        lines.append(f"- {name}: `{path}`")
    lines.extend(
        [
            "",
            "## 自动评分",
            "",
        ]
    )
    for name, score in report["scores"].items():
        if isinstance(score, list):
            lines.append(f"- {name}: 外部评审待完成 {', '.join(score)}")
        elif score is None:
            lines.append(f"- {name}: 待外部评审")
        else:
            lines.append(f"- {name}: {score}/5")
    lines.extend(
        [
            "",
            "## 限制",
            "",
            "- 自动检查只证明确定性约束与渲染工件存在；不能替代对研究事实、引用原文和版面美观的人工判断。",
            "- 本报告不构成录用、发表或伦理合规保证。",
            "",
        ]
    )
    return "\n".join(lines)


def run_audit(
    package_source: Any,
    *,
    docx_path: str | Path | None = None,
    out_dir: str | Path = "output/submission-audit",
    render: bool = False,
    config_path: str | Path | None = None,
    version: str = "round-1",
) -> dict[str, Any]:
    package = _load_json_source(package_source)
    config = load_config(config_path)
    blocks = _text_blocks(package)
    evidence = analyze_evidence(package, blocks, config)
    statistics = analyze_statistics(package, blocks, config)
    table_audit = analyze_docx_table_conflicts(package.get("tables") or [], config)
    statistics["table_audit"] = table_audit
    statistics["findings"].extend(table_audit["findings"])
    statistics["conflict_count"] += table_audit["conflict_count"]
    if table_audit["conflict_count"]:
        statistics["pass"] = False
    figures = analyze_figures(package, blocks, config)
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    docx_value = str(docx_path or package.get("docx_path") or "").strip()
    actual_docx = Path(docx_value) if docx_value else None
    docx = inspect_docx(actual_docx, package, config) if actual_docx is not None else {
        "path": "",
        "pass": False,
        "score": 0.0,
        "findings": [{"severity": "error", "code": "docx_path_missing", "message": "未提供 DOCX 路径"}],
    }
    render_report = render_docx(actual_docx, output / "rendered", config, enabled=render) if actual_docx is not None else {
        "enabled": render,
        "pass": False,
        "score": 0.0,
        "page_count": 0,
        "limitations": ["未提供 DOCX 路径"],
    }
    placeholders = find_placeholders(blocks, config)
    scores = quality_scores(evidence, statistics, figures, docx, render_report, placeholders, config)
    thresholds = config.get("thresholds") or {}
    render_required = bool((config.get("docx") or {}).get("require_word_or_pdf_render", True))
    fabrication_codes = {
        "verified_without_evidence",
        "missing_reference_entry",
        "stat_recompute_mismatch",
        "duplicate_figure_id",
    }
    detected_codes = {
        str(item.get("code"))
        for collection in (
            evidence.get("findings") or [],
            statistics.get("findings") or [],
            figures.get("findings") or [],
        )
        for item in collection
    }
    hard_gates = {
        "evidence": bool(evidence.get("pass")),
        "statistics": bool(statistics.get("pass")),
        "figures": bool(figures.get("pass")),
        "docx_structure": bool(docx.get("pass")),
        "docx_render": bool(render_report.get("pass")) if render_required else True,
        "placeholders": len(placeholders) <= int(thresholds.get("unresolved_placeholder_count", 0)),
        "fabrication_risk": not bool(detected_codes & fabrication_codes),
        "human_signoff_required": True,
        "human_signoff_completed": False,
    }
    automated_pass = all(
        value
        for key, value in hard_gates.items()
        if key not in {"human_signoff_required", "human_signoff_completed"}
    )
    candidate_ready = automated_pass and hard_gates["human_signoff_required"]
    status = "candidate_ready_for_author_signoff" if candidate_ready else "candidate_with_open_gates"
    risks = build_risk_register(evidence, statistics, figures, docx, render_report, placeholders)
    files = {
        "evidence_graph": str((output / "evidence-graph.json").resolve()),
        "statistics_registry": str((output / "statistics-registry.json").resolve()),
        "figures_manifest": str((output / "figures-manifest.json").resolve()),
        "docx_report": str((output / "docx-render-report.json").resolve()),
        "risk_register": str((output / "risk-register.json").resolve()),
        "author_confirmation": str((output / "author-confirmation.md").resolve()),
        "human_signoff": str((output / "human-signoff.md").resolve()),
        "quality_report": str((output / "quality-report.md").resolve()),
        "release_manifest": str((output / "release-manifest.json").resolve()),
        "reproducibility": str((output / "reproducibility.json").resolve()),
        "audit_json": str((output / "submission-audit.json").resolve()),
    }
    report = {
        "schema_version": "1.0",
        "version": version,
        "generated_at": _utc_now(),
        "positioning": config.get("positioning") or {},
        "status": status,
        "hard_gates": hard_gates,
        "gate": {
            "automated_pass": automated_pass,
            "candidate_ready_for_signoff": candidate_ready,
            "final_publication_allowed": False,
            "human_signoff_status": "required_not_completed",
        },
        "evidence": evidence,
        "statistics": statistics,
        "figures": figures,
        "docx": docx,
        "render": render_report,
        "placeholders": placeholders,
        "risks": risks,
        "scores": scores,
        "files": files,
    }
    (output / "evidence-graph.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "statistics-registry.json").write_text(json.dumps(statistics, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "figures-manifest.json").write_text(json.dumps(figures, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "docx-render-report.json").write_text(json.dumps({**docx, "render": render_report}, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "risk-register.json").write_text(json.dumps(risks, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "author-confirmation.md").write_text(
        build_author_confirmation(evidence, statistics, figures, placeholders), encoding="utf-8"
    )
    (output / "human-signoff.md").write_text(
        build_human_signoff(str(actual_docx or ""), config), encoding="utf-8"
    )
    (output / "quality-report.md").write_text(build_markdown(report), encoding="utf-8")
    manifest_files = {
        name: _sha256_file(path)
        for name, path in files.items()
        if name not in {"release_manifest", "reproducibility", "audit_json"}
    }
    config_target = (
        Path(config_path) if config_path else (PROJECT_ROOT / "config" / "submission.yaml")
    )
    release_manifest = {
        "schema_version": "1.0",
        "version": version,
        "generated_at": report["generated_at"],
        "positioning": report["positioning"],
        "status": status,
        "automated_pass": automated_pass,
        "candidate_ready_for_signoff": candidate_ready,
        "final_publication_allowed": False,
        "human_signoff_status": "required_not_completed",
        "inputs": {
            "package_sha256": _sha256_json(package),
            "docx_path": (
                str(actual_docx.resolve())
                if actual_docx is not None and actual_docx.exists()
                else ""
            ),
            "docx_sha256": _sha256_file(actual_docx) if actual_docx is not None else "",
            "config": str(config_target.resolve()),
            "config_sha256": _sha256_file(config_target),
            "render_enabled": bool(render),
        },
        "output_hashes": manifest_files,
        "files": files,
    }
    (output / "release-manifest.json").write_text(
        json.dumps(release_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    reproducibility = {
        "schema_version": "1.0",
        "generated_at": report["generated_at"],
        "python": sys.version,
        "platform": sys.platform,
        "word_com_supported": sys.platform == "win32",
        "pdftoppm": _find_executable("pdftoppm") or "",
        "libreoffice": _find_executable("soffice") or _find_executable("libreoffice") or "",
        "render_backend": render_report.get("backend") or "",
        "entrypoint": "submission-audit.run(args)",
        "args": {
            "package_file": "<package.json>",
            "docx_path": "<candidate.docx>",
            "out": str(output.resolve()),
            "version": version,
            "render": render,
        },
        "limitations": render_report.get("limitations") or [],
    }
    (output / "reproducibility.json").write_text(
        json.dumps(reproducibility, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report["files"]["release_manifest_sha256"] = _sha256_file(output / "release-manifest.json")
    report["files"]["reproducibility_sha256"] = _sha256_file(output / "reproducibility.json")
    (output / "submission-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
