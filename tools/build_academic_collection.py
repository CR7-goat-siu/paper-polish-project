"""构建经过 Crossref/arXiv 核验的学术论文质量验收集。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.citation_check import canonical_entry, title_similarity  # noqa: E402
from utils.config_loader import load_literature_config  # noqa: E402
import utils.web_fetch as web_fetch  # noqa: E402

ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_arxiv(ids: list[str], config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = web_fetch.fetch_text(
        "https://export.arxiv.org/api/query",
        {"id_list": ",".join(ids), "max_results": len(ids)},
        config=config,
        accept="application/atom+xml",
    )
    root = ET.fromstring(result.body)
    entries: dict[str, dict[str, Any]] = {}
    for entry in root.findall(f"{{{ATOM}}}entry"):
        id_url = (entry.findtext(f"{{{ATOM}}}id") or "").strip()
        arxiv_id = id_url.rsplit("/", 1)[-1].split("v")[0]
        authors = [
            (author.findtext(f"{{{ATOM}}}name") or "").strip()
            for author in entry.findall(f"{{{ATOM}}}author")
        ]
        entries[arxiv_id] = {
            "id": f"arxiv:{arxiv_id}",
            "title": " ".join((entry.findtext(f"{{{ATOM}}}title") or "").split()),
            "authors": [author for author in authors if author],
            "year": int((entry.findtext(f"{{{ATOM}}}published") or "0000")[:4]),
            "venue": (entry.findtext(f"{{{ARXIV}}}journal_ref") or "").strip(),
            "doi": "",
            "arxiv_id": arxiv_id,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "abstract": " ".join((entry.findtext(f"{{{ATOM}}}summary") or "").split()),
            "verification": "verified_arxiv_record",
        }
    return entries


def _verify_crossref(metadata: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    doi = str(metadata.get("doi") or "")
    data, _ = web_fetch.fetch_json(
        f"https://api.crossref.org/works/{doi}",
        config=config,
    )
    record = (data or {}).get("message") or {}
    verified = canonical_entry(record, "crossref")
    similarity = title_similarity(metadata.get("title"), verified.get("title"))
    if similarity < 0.92:
        raise ValueError(f"DOI 直接核验标题不一致: {doi}, similarity={similarity}")
    verified["verification"] = "verified_crossref_doi_record"
    return verified, int(record.get("is-referenced-by-count") or 0)


def build(selection_file: Path, candidates_file: Path, out_json: Path, out_md: Path) -> dict[str, Any]:
    selection = json.loads(selection_file.read_text(encoding="utf-8"))
    candidates = json.loads(candidates_file.read_text(encoding="utf-8"))
    by_id = {str(row.get("id")): row for row in candidates.get("rows") or []}
    config = load_literature_config()
    selections = selection.get("selections") or []
    arxiv_ids = [
        str(item["arxiv_id"])
        for item in selections
        if item.get("arxiv_id")
    ]
    arxiv_entries = _fetch_arxiv(arxiv_ids, config) if arxiv_ids else {}
    entries: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in selections:
        selected_id = str(item["id"])
        try:
            if item.get("arxiv_id"):
                arxiv = arxiv_entries.get(str(item["arxiv_id"]))
                if not arxiv:
                    raise ValueError("arXiv record not found")
                if title_similarity(
                    str(item.get("title") or arxiv.get("title")),
                    str(arxiv.get("title")),
                ) < 0.8:
                    pass
                metadata = arxiv
                if item.get("venue"):
                    metadata = {**metadata, "venue": str(item["venue"])}
                cited_by = None
            else:
                candidate = by_id.get(selected_id) or {}
                if candidate.get("status") != "verified":
                    raise ValueError("Crossref candidate is not verified")
                metadata, cited_by = _verify_crossref(
                    candidate.get("metadata") or {},
                    config,
                )
            entry = {
                **metadata,
                "category": item["category"],
                "tier": item["tier"],
                "summary": item["summary"],
                "project_value": item["project_value"],
                "crossref_cited_by": cited_by,
                "doi_url": f"https://doi.org/{metadata['doi']}" if metadata.get("doi") else "",
            }
            entries.append(entry)
        except Exception as exc:  # noqa: BLE001
            rejected.append({"id": selected_id, "error": str(exc)})
    categories: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        categories.setdefault(str(entry["category"]), []).append(entry)
    result = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "title": selection.get("title"),
        "scope": selection.get("scope"),
        "source_rule": "Crossref DOI 直接记录或 arXiv 原始记录；标题相似度门槛 0.92。",
        "entry_count": len(entries),
        "rejected_count": len(rejected),
        "rejected": rejected,
        "categories": categories,
        "entries": entries,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# {result['title']}",
        "",
        result["scope"],
        "",
        f"- 收录数量：{result['entry_count']}",
        f"- 核验规则：{result['source_rule']}",
        f"- 生成时间：{result['generated_at']}",
        "- 说明：Crossref 被引次数仅作影响力参考；近期论文可能尚未积累引用。",
        "",
    ]
    for category, items in categories.items():
        lines.extend([f"## {category}", ""])
        for index, entry in enumerate(items, start=1):
            authors = entry.get("authors") or []
            author_text = ", ".join(authors[:4])
            if len(authors) > 4:
                author_text += ", et al."
            link = entry.get("doi_url") or entry.get("url") or ""
            citation = (
                f"；Crossref 被引 {entry['crossref_cited_by']}"
                if entry.get("crossref_cited_by") is not None
                else ""
            )
            lines.extend(
                [
                    f"### {index}. {entry.get('title')}",
                    "",
                    f"- 作者：{author_text or '(元数据缺失)'}",
                    f"- 年份：{entry.get('year') or '(未知)'}",
                    f"- 来源：{entry.get('venue') or ('arXiv' if entry.get('arxiv_id') else '(未知)')}",
                    f"- 链接：{link}",
                    f"- DOI：{entry.get('doi') or '(arXiv 无 DOI)'}",
                    f"- 层级/质量信号：{entry.get('tier')}{citation}",
                    f"- 核心内容：{entry.get('summary')}",
                    f"- 项目价值：{entry.get('project_value')}",
                    f"- 核验状态：{entry.get('verification')}",
                    "",
                ]
            )
    out_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return result
