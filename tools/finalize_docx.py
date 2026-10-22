"""按数据化 spec 对 DOCX 做安全最终化：颜色归一与唯一段落替换。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.docx_io import (  # noqa: E402
    compact_reference_paragraphs,
    materialize_mteqn_fields,
    normalize_docx_terms,
    normalize_text_colors,
    preserve_inline_math_paragraphs,
    read_paragraphs,
    remove_trailing_empty_paragraphs,
    repeat_table_headers_and_prevent_row_split,
    sanitize_docx_field_artifacts,
    set_heading_outline_levels,
    write_polished_docx,
)
from utils.zotero_crossref import audit_zotero_docx  # noqa: E402


def finalize(source: Path, target: Path, spec: dict[str, Any]) -> dict[str, Any]:
    paragraphs = read_paragraphs(source)
    replacements: dict[int, str] = {}
    applied: list[dict[str, Any]] = []
    for item in spec.get("replacements") or []:
        prefix = str(item.get("match_prefix") or "")
        replacement = str(item.get("replacement") or "")
        if not prefix:
            raise ValueError("replacement.match_prefix 不能为空")
        matches = [p for p in paragraphs if p["text"].startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(
                f"前缀 {prefix!r} 匹配到 {len(matches)} 个段落，必须唯一"
            )
        para = matches[0]
        replacements[int(para["index"])] = replacement
        applied.append(
            {"index": int(para["index"]), "match_prefix": prefix, "old": para["text"]}
        )
    temporary_paths: list[Path] = []
    intermediate = source
    if replacements:
        replacement_path = target.with_name(target.stem + ".replacements.docx")
        write_polished_docx(intermediate, replacement_path, replacements)
        temporary_paths.append(replacement_path)
        intermediate = replacement_path
    inline_math_report = None
    if spec.get("preserve_inline_math_from"):
        math_path = target.with_name(target.stem + ".inlinemath.docx")
        inline_math_report = preserve_inline_math_paragraphs(
            Path(str(spec["preserve_inline_math_from"])),
            intermediate,
            math_path,
        )
        temporary_paths.append(math_path)
        intermediate = math_path
    field_artifact_report = None
    if spec.get("sanitize_field_artifacts", False):
        zotero_audit = audit_zotero_docx(intermediate)
        if (
            int(zotero_audit.get("citation_field_count") or 0) > 0
            and zotero_audit.get("pass")
        ):
            field_artifact_report = {
                "preserved": True,
                "reason": "valid_zotero_fields",
                "audit": zotero_audit,
            }
        else:
            field_artifact_path = target.with_name(target.stem + ".fields.docx")
            field_artifact_report = sanitize_docx_field_artifacts(
                intermediate, field_artifact_path
            )
            field_artifact_report["preserved"] = False
            temporary_paths.append(field_artifact_path)
            intermediate = field_artifact_path
    term_normalization_report = None
    term_replacements = spec.get("term_replacements") or {}
    if term_replacements:
        term_path = target.with_name(target.stem + ".terms.docx")
        term_normalization_report = normalize_docx_terms(
            intermediate, term_path, term_replacements
        )
        temporary_paths.append(term_path)
        intermediate = term_path
    equation_report = None
    if spec.get("materialize_mteqn_fields", False):
        equation_path = target.with_name(target.stem + ".equations.docx")
        equation_report = materialize_mteqn_fields(intermediate, equation_path)
        temporary_paths.append(equation_path)
        intermediate = equation_path
    heading_report = None
    if spec.get("heading_outline_levels", False):
        heading_path = target.with_name(target.stem + ".headings.docx")
        heading_report = set_heading_outline_levels(intermediate, heading_path)
        temporary_paths.append(heading_path)
        intermediate = heading_path
    reference_report = None
    if spec.get("compact_references", False):
        reference_path = target.with_name(target.stem + ".refs.docx")
        reference_report = compact_reference_paragraphs(intermediate, reference_path)
        temporary_paths.append(reference_path)
        intermediate = reference_path
    trailing_blank_report = None
    if spec.get("trim_trailing_empty_paragraphs", False):
        trailing_blank_path = target.with_name(target.stem + ".trailingblanks.docx")
        trailing_blank_report = remove_trailing_empty_paragraphs(
            intermediate, trailing_blank_path
        )
        temporary_paths.append(trailing_blank_path)
        intermediate = trailing_blank_path
    table_pagination_report = None
    if spec.get("repeat_table_headers", False):
        table_path = target.with_name(target.stem + ".tables.docx")
        table_pagination_report = repeat_table_headers_and_prevent_row_split(
            intermediate, table_path
        )
        temporary_paths.append(table_path)
        intermediate = table_path
    if spec.get("normalize_colors", True):
        normalize_text_colors(
            intermediate,
            target,
            color=str(spec.get("text_color") or "000000"),
            remove_highlight=bool(spec.get("remove_highlight", True)),
        )
    elif intermediate != target:
        target.write_bytes(intermediate.read_bytes())
    for path in temporary_paths:
        path.unlink(missing_ok=True)
    return {
        "source": str(source.resolve()),
        "target": str(target.resolve()),
        "replacement_count": len(replacements),
        "replacements": applied,
        "normalize_colors": bool(spec.get("normalize_colors", True)),
        "heading_outline": heading_report,
        "equation_numbers": equation_report,
        "inline_math": inline_math_report,
        "field_artifacts": field_artifact_report,
        "terms": term_normalization_report,
        "references": reference_report,
        "trailing_blanks": trailing_blank_report,
        "table_pagination": table_pagination_report,
    }
