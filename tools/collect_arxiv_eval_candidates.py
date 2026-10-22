"""采集公开 arXiv 论文元数据，建立真实论文评测候选池。

只访问 config/literature.yaml 白名单中的 arXiv，统一走 utils.web_fetch。
元数据候选不等同于可评测全文；评测就绪状态只有在正文、rubric 和人工复核
完成后才能置为 true。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.config_loader import load_literature_config  # noqa: E402
import utils.web_fetch as web_fetch  # noqa: E402

ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"

DEFAULT_CATEGORIES = [
    "cs.LG",
    "q-bio.NC",
    "stat.ME",
    "econ.EM",
    "physics.soc-ph",
    "q-fin.ST",
    "cs.CL",
    "eess.SP",
    "q-bio.QM",
    "stat.AP",
]


def _text(node: ET.Element | None, default: str = "") -> str:
    return (node.text or "").strip() if node is not None else default


def parse_arxiv_feed(xml_text: str, *, category: str, split: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    entries: list[dict[str, Any]] = []
    for entry in root.findall(f"{{{ATOM}}}entry"):
        id_url = _text(entry.find(f"{{{ATOM}}}id"))
        if not id_url:
            continue
        arxiv_id = id_url.rsplit("/", 1)[-1]
        pdf_url = ""
        abs_url = id_url.replace("http://", "https://")
        for link in entry.findall(f"{{{ATOM}}}link"):
            href = str(link.attrib.get("href") or "")
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = href.replace("http://", "https://")
            if link.attrib.get("rel") == "alternate":
                abs_url = href.replace("http://", "https://")
        authors = [
            _text(author.find(f"{{{ATOM}}}name"))
            for author in entry.findall(f"{{{ATOM}}}author")
        ]
        authors = [author for author in authors if author]
        primary = entry.find(f"{{{ARXIV}}}primary_category")
        license_node = entry.find(f"{{{ARXIV}}}license")
        entries.append(
            {
                "id": f"arxiv-{arxiv_id.replace('/', '-')}",
                "arxiv_id": arxiv_id,
                "title": " ".join(_text(entry.find(f"{{{ATOM}}}title")).split()),
                "authors": authors,
                "published": _text(entry.find(f"{{{ATOM}}}published")),
                "updated": _text(entry.find(f"{{{ATOM}}}updated")),
                "summary": " ".join(_text(entry.find(f"{{{ATOM}}}summary")).split()),
                "primary_category": (
                    str(primary.attrib.get("term") or category) if primary is not None else category
                ),
                "query_category": category,
                "license": _text(license_node, "arxiv-terms-unverified"),
                "abs_url": abs_url,
                "pdf_url": pdf_url,
                "source": "arxiv",
                "source_type": "public_open_metadata",
                "privacy_status": "public",
                "split": split,
                "download_status": "not_downloaded",
                "evaluation_status": "metadata_only",
                "rubric": "config/submission.yaml#quality_rubric",
                "gold_standard": "pending_human_review",
            }
        )
    return entries


def collect(
    *,
    per_category: int = 4,
    output: Path,
    categories: list[str] | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    categories = categories or DEFAULT_CATEGORIES
    config = load_literature_config()
    seen: dict[str, dict[str, Any]] = {}
    source_stats: list[dict[str, Any]] = []
    test_categories = set(categories[-2:])
    for category in categories:
        split = "test" if category in test_categories else "dev"
        query = f"cat:{category}"
        try:
            result = web_fetch.fetch_text(
                "https://export.arxiv.org/api/query",
                {
                    "search_query": query,
                    "start": 0,
                    "max_results": per_category,
                    "sortBy": "relevance",
                    "sortOrder": "descending",
                },
                config=config,
                offline=offline,
                accept="application/atom+xml",
            )
            xml_text = result.body
            entries = parse_arxiv_feed(xml_text, category=category, split=split)
            for entry in entries:
                seen.setdefault(entry["arxiv_id"], entry)
            source_stats.append(
                {
                    "category": category,
                    "status": "ok",
                    "count": len(entries),
                    "cached": result.cached,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单类失败不阻断整池采集
            source_stats.append(
                {"category": category, "status": "error", "error": str(exc), "count": 0}
            )
    entries = sorted(seen.values(), key=lambda item: (item["split"], item["primary_category"], item["arxiv_id"]))
    dev_count = sum(1 for item in entries if item["split"] == "dev")
    test_count = sum(1 for item in entries if item["split"] == "test")
    manifest = {
        "schema_version": "1.0",
        "dataset_kind": "real_public_arxiv_metadata_candidates",
        "target_count": 30,
        "metadata_candidate_count": len(entries),
        "development_count": dev_count,
        "held_out_test_count": test_count,
        "full_text_downloaded_count": 0,
        "gold_standard_count": 0,
        "real_evaluation_ready": False,
        "not_for_performance_claim": True,
        "selection": {
            "categories": categories,
            "per_category": per_category,
            "test_categories": sorted(test_categories),
            "dedupe_key": "arxiv_id",
        },
        "source_stats": source_stats,
        "entries": entries,
        "gaps": [
            "当前只采集公开元数据，尚未下载全文或生成 DOCX。",
            "尚无人工确认黄金标准或盲评 rubric 结果。",
            "元数据候选不得计入真实论文硬门禁通过率。",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
