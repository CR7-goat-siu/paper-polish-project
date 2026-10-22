"""用 Crossref 精确核验候选学术论文元数据。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.citation_check import canonical_entry, title_similarity  # noqa: E402
from utils.config_loader import load_literature_config  # noqa: E402
import utils.web_fetch as web_fetch  # noqa: E402


def _item_doi(item: dict[str, Any]) -> str:
    return str(item.get("DOI") or item.get("doi") or "").strip()


def resolve_queries(queries_file: Path, output: Path, threshold: float = 0.92) -> dict[str, Any]:
    payload = json.loads(queries_file.read_text(encoding="utf-8"))
    config = load_literature_config()
    rows: list[dict[str, Any]] = []
    for query in payload.get("queries") or []:
        title = str(query.get("title") or "").strip()
        row: dict[str, Any] = {
            "id": query.get("id"),
            "theme": query.get("theme"),
            "query_title": title,
            "status": "unresolved",
        }
        try:
            data, _ = web_fetch.fetch_json(
                "https://api.crossref.org/works",
                {"query.bibliographic": title, "rows": 8},
                config=config,
            )
            items = ((data or {}).get("message") or {}).get("items") or []
            best: tuple[float, dict[str, Any]] | None = None
            for item in items:
                if not isinstance(item, dict) or not _item_doi(item):
                    continue
                candidate_title = (item.get("title") or [""])[0]
                similarity = title_similarity(title, candidate_title)
                if best is None or similarity > best[0]:
                    best = (similarity, item)
            if best and best[0] >= threshold:
                entry = canonical_entry(best[1], "crossref")
                row.update(
                    {
                        "status": "verified",
                        "title_similarity": round(best[0], 4),
                        "metadata": entry,
                        "verification": "title_matched_to_crossref_doi_record",
                        "source": "crossref",
                        "doi": entry.get("doi"),
                        "doi_url": f"https://doi.org/{entry.get('doi')}",
                    }
                )
            elif best:
                row.update(
                    {
                        "status": "rejected_low_similarity",
                        "best_title": (best[1].get("title") or [""])[0],
                        "title_similarity": round(best[0], 4),
                    }
                )
            else:
                row["status"] = "no_crossref_match"
        except Exception as exc:  # noqa: BLE001
            row.update({"status": "error", "error": str(exc)})
        rows.append(row)
    result = {
        "schema_version": "1.0",
        "source": "crossref",
        "threshold": threshold,
        "query_count": len(rows),
        "verified_count": sum(1 for row in rows if row.get("status") == "verified"),
        "rejected_count": sum(1 for row in rows if row.get("status") != "verified"),
        "rows": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
