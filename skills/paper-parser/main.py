"""paper-parser Skill：LaTeX / Markdown 论文解析、章节拆分、格式保留。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.docx_io import (  # noqa: E402
    parse_paragraph_kinds,
    read_comments,
    read_paragraphs,
    read_tables,
)
from utils.logger import get_logger  # noqa: E402
from utils.token_counter import estimate_tokens  # noqa: E402

logger = get_logger("paper-parser")

LATEX_SECTION_COMMANDS = (
    ("part", "section"),
    ("chapter", "section"),
    ("section", "section"),
)
LATEX_ABSTRACT_BEGIN_RE = re.compile(r"\\begin\s*\{\s*abstract\s*\}")
LATEX_ABSTRACT_END_RE = re.compile(r"\\end\s*\{\s*abstract\s*\}")
LATEX_THEBIB_BEGIN_RE = re.compile(r"\\begin\s*\{\s*thebibliography\s*\}")
LATEX_THEBIB_END_RE = re.compile(r"\\end\s*\{\s*thebibliography\s*\}")
LATEX_BIBLIOGRAPHY_RE = re.compile(r"\\bibliography\s*\{")
LATEX_PRINTBIBLIOGRAPHY_RE = re.compile(r"\\printbibliography")
LATEX_DOC_BEGIN_RE = re.compile(r"\\begin\s*\{\s*document\s*\}")
LATEX_APPENDIX_RE = re.compile(r"\\appendix")
MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
MD_FRONT_MATTER_DELIMITER = "---"
HEADING_REFERENCES_RE = re.compile(r"^\s*(references|bibliography|参\s*考\s*文\s*献|文\s*献)\s*$", re.IGNORECASE)
HEADING_ABSTRACT_RE = re.compile(r"^\s*(abstract|摘\s*要)\s*[:：]?\s*$", re.IGNORECASE)


def _read_braced(text: str, start: int) -> str:
    """从 start（指向 '{'）读取配对花括号内容，支持嵌套。"""
    if start < 0 or start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    out: list[str] = []
    for ch in text[start:]:
        if ch == "{":
            depth += 1
            if depth > 1:
                out.append(ch)
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(out).strip()
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out).strip()


def _dedupe_markers(markers: list[tuple[int, str, str | None]]) -> list[tuple[int, str, str | None]]:
    seen: set[int] = set()
    out: list[tuple[int, str, str | None]] = []
    for position, kind, heading in sorted(markers, key=lambda item: item[0]):
        if position in seen:
            continue
        seen.add(position)
        out.append((position, kind, heading))
    return out


def _latex_markers(text: str) -> list[tuple[int, str, str | None]]:
    markers: list[tuple[int, str, str | None]] = []
    doc_match = LATEX_DOC_BEGIN_RE.search(text)
    if doc_match:
        markers.append((doc_match.start(), "front_matter", None))

    for cmd, kind in LATEX_SECTION_COMMANDS:
        pattern = re.compile(r"\\" + cmd + r"\*?")
        for match in pattern.finditer(text):
            brace = text.find("{", match.end())
            heading = _read_braced(text, brace) if brace >= 0 else None
            markers.append((match.start(), kind, heading))

    for match in LATEX_ABSTRACT_BEGIN_RE.finditer(text):
        markers.append((match.start(), "abstract", "Abstract"))
        end = LATEX_ABSTRACT_END_RE.search(text, match.end())
        if end:
            markers.append((end.end(), "front_matter", None))

    for match in LATEX_THEBIB_BEGIN_RE.finditer(text):
        markers.append((match.start(), "references", "References"))
        end = LATEX_THEBIB_END_RE.search(text, match.end())
        if end:
            markers.append((end.end(), "front_matter", None))

    for regex in (LATEX_BIBLIOGRAPHY_RE, LATEX_PRINTBIBLIOGRAPHY_RE):
        for match in regex.finditer(text):
            markers.append((match.start(), "references", "References"))

    for match in LATEX_APPENDIX_RE.finditer(text):
        markers.append((match.start(), "appendix", "Appendix"))
    return _dedupe_markers(markers)


def _markdown_markers(text: str) -> list[tuple[int, str, str | None]]:
    markers: list[tuple[int, str, str | None]] = []
    lines = text.splitlines(keepends=True)
    offset = 0
    in_fence = False

    if lines and lines[0].strip() == MD_FRONT_MATTER_DELIMITER:
        markers.append((0, "front_matter", "Front Matter"))
        for line in lines[1:]:
            offset += len(line)
            if line.strip() == MD_FRONT_MATTER_DELIMITER:
                markers.append((offset, "front_matter", None))
                break

    offset = 0
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            match = MD_HEADING_RE.match(stripped)
            if match and not stripped.startswith(">"):
                heading_text = match.group(2).strip()
                if HEADING_REFERENCES_RE.match(heading_text):
                    kind = "references"
                elif HEADING_ABSTRACT_RE.match(heading_text):
                    kind = "abstract"
                else:
                    kind = "section"
                markers.append((offset, kind, heading_text))
        offset += len(line)
    return _dedupe_markers(markers)


def _classify_unmarked(content: str, fmt: str, default: str) -> str:
    head = content.lstrip()
    if not head:
        return default
    if fmt == "latex":
        if re.search(r"\\(?:documentclass|usepackage|newcommand|title)", head[:3000]):
            return "preamble"
        if LATEX_ABSTRACT_BEGIN_RE.search(head[:3000]):
            return "abstract"
        if LATEX_THEBIB_BEGIN_RE.search(head[:3000]):
            return "references"
        return "body"
    if fmt == "markdown":
        if head.startswith(MD_FRONT_MATTER_DELIMITER):
            return "front_matter"
        return "body"
    return default


def _slice_chunks(
    text: str, markers: list[tuple[int, str, str | None]], fmt: str, default_kind: str
) -> list[dict[str, Any]]:
    cuts: list[tuple[int, str, str | None]] = []
    all_points = sorted({pos for pos, _, _ in markers} | {0})
    by_position: dict[int, tuple[str, str | None]] = {}
    for position, kind, heading in markers:
        by_position.setdefault(position, (kind, heading))

    kind_of: dict[int, str] = {}
    heading_of: dict[int, str | None] = {}
    for position in all_points:
        kind, heading = by_position.get(position, (None, None))
        kind_of[position] = kind  # type: ignore[assignment]
        heading_of[position] = heading

    chunks: list[dict[str, Any]] = []
    for index, start in enumerate(all_points):
        end = all_points[index + 1] if index + 1 < len(all_points) else len(text)
        if end <= start:
            continue
        content = text[start:end]
        marker_kind = kind_of[start]
        heading = heading_of[start]
        if marker_kind is None:
            kind = _classify_unmarked(content, fmt, default_kind)
        else:
            kind = marker_kind
        chunks.append(
            {
                "id": f"c{index + 1:03d}",
                "kind": kind,
                "heading": heading or "",
                "order": index,
                "content": content,
                "start": start,
                "end": end,
                "chars": end - start,
                "est_tokens": estimate_tokens(content),
            }
        )
    return chunks


def detect_format(source_format: str | None, source_name: str | None = None) -> str:
    if source_format and source_format != "auto":
        norm = source_format.lower()
        if norm in ("md", "markdown"):
            return "markdown"
        if norm in ("tex", "latex"):
            return "latex"
        if norm in ("docx", "doc"):
            return "docx"
        if norm in ("txt", "text", "plain"):
            return "plain"
        raise ValueError(f"未知格式: {source_format}")
    suffix = (source_name or "").lower()
    if suffix.endswith((".tex", ".ltx")):
        return "latex"
    if suffix.endswith((".md", ".markdown", ".mdown")):
        return "markdown"
    if suffix.endswith(".docx"):
        return "docx"
    return "plain"


def _extract_latex_title(text: str) -> str:
    match = re.search(r"\\title\s*\{", text)
    if match:
        return _read_braced(text, text.find("{", match.end()))
    match = re.search(r"\\begin\s*\{\s*abstract\s*\}", text)
    if match:
        return "Untitled"
    return "Untitled"


def _extract_markdown_title(text: str, chunks: list[dict[str, Any]]) -> str:
    for chunk in chunks:
        if chunk["kind"] == "front_matter" and chunk["content"]:
            match = re.search(r"^title\s*[:：]\s*(.+)$", chunk["content"], re.MULTILINE)
            if match:
                return match.group(1).strip().strip('"\'')
        heading = chunk.get("heading") or ""
        if heading:
            return heading
    return "Untitled"


def _guess_language(text: str) -> str:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    total = max(1, len(text))
    return "zh-CN" if cjk / total > 0.15 else "en"


def _docx_markers(
    paragraphs: list[dict[str, Any]], text: str
) -> list[tuple[int, str, str | None]]:
    """按 docx 段落样式与内容生成切分标记。

    约定：段落以 "\n\n" 连接成 text，标记偏移即各段落起始位置。
    """
    markers: list[tuple[int, str, str | None]] = []
    offset = 0
    kinds = parse_paragraph_kinds(paragraphs)
    for index, (kind, heading_text) in enumerate(kinds):
        if kind == "title":
            markers.append((offset, "front_matter", heading_text))
        elif kind == "abstract":
            if heading_text:
                markers.append((offset, "abstract", "Abstract"))
            # 摘要正文段落不重复打标
        elif kind == "section":
            markers.append((offset, "section", heading_text))
        elif kind == "references":
            if heading_text:
                markers.append((offset, "references", heading_text))
        offset += len(paragraphs[index]["text"]) + (2 if index < len(paragraphs) - 1 else 0)
    return _dedupe_markers(markers)


def parse_text(
    text: str,
    *,
    source_format: str | None = None,
    source_name: str | None = None,
    include_offsets: bool = True,
    paragraphs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fmt = detect_format(source_format, source_name)
    if fmt == "latex":
        markers = _latex_markers(text)
    elif fmt == "markdown":
        markers = _markdown_markers(text)
    elif fmt == "docx":
        if paragraphs is None:
            raise ValueError("docx 解析需要 paragraphs 参数（请通过文件输入）")
        markers = _docx_markers(paragraphs, text)
    else:
        markers = []
    chunks = _slice_chunks(text, markers, fmt, "body")
    if not chunks:
        chunks = [
            {
                "id": "c001",
                "kind": "body",
                "heading": "",
                "order": 0,
                "content": text,
                "start": 0,
                "end": len(text),
                "chars": len(text),
                "est_tokens": estimate_tokens(text),
            }
        ]
    if fmt == "docx" and paragraphs:
        for chunk in chunks:
            indexes = [
                p["index"]
                for p in paragraphs
                if chunk["start"] <= _docx_paragraph_offset(paragraphs, p["index"]) < chunk["end"]
            ]
            chunk["paragraph_indexes"] = indexes
    if not include_offsets:
        for chunk in chunks:
            chunk.pop("start", None)
            chunk.pop("end", None)
    if fmt == "latex":
        title = _extract_latex_title(text)
    elif fmt == "docx" and paragraphs:
        title = next(
            (p["text"] for p in paragraphs if p["style"].lower() in ("title", "heading 1")),
            next((p["text"] for p in paragraphs if p["text"].strip()), "Untitled"),
        )
    else:
        title = _extract_markdown_title(text, chunks)
    reconstructed = "".join(chunk["content"] for chunk in chunks)
    result = {
        "file": source_name or "",
        "format": fmt,
        "language_hint": _guess_language(text),
        "title": title,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "integrity": {
            "total_chars": len(text),
            "reconstructed_chars": len(reconstructed),
            "reconstructs": reconstructed == text,
        },
        "full_text": text,
    }
    if fmt == "docx" and paragraphs is not None:
        result["docx_paragraphs"] = paragraphs
        result["docx"] = {"paragraph_count": len(paragraphs)}
    return result


def _docx_paragraph_offset(paragraphs: list[dict[str, Any]], index: int) -> int:
    offset = 0
    for i in range(index):
        offset += len(paragraphs[i]["text"]) + 2
    return offset


def run(args: dict[str, Any]) -> dict[str, Any]:
    source_path = args.get("input")
    text = args.get("text")
    paragraphs = None
    if source_path is None and text is None:
        raise ValueError("paper-parser run() 需要 input 或 text")
    if text is None:
        path = Path(source_path)
        if path.suffix.lower() == ".docx":
            paragraphs = read_paragraphs(path)
            text = "\n\n".join(p["text"] for p in paragraphs)
        else:
            text = path.read_text(encoding="utf-8-sig")
        source_name = str(args.get("source_name") or path.name)
    else:
        source_name = str(args.get("source_name") or "")
    result = parse_text(
        text,
        source_format=args.get("format", "auto"),
        source_name=source_name,
        include_offsets=bool(args.get("include_offsets", True)),
        paragraphs=paragraphs,
    )
    if paragraphs is not None and source_path:
        try:
            result["comments"] = read_comments(Path(source_path), paragraphs)
        except Exception:  # noqa: BLE001 - 批注读取失败不阻塞解析
            result["comments"] = []
        try:
            tables = read_tables(Path(source_path))
            result["docx_tables"] = tables
            result.setdefault("docx", {})["table_count"] = len(tables)
            table_paragraphs = {
                index
                for table in tables
                for index in table.get("paragraph_indexes") or []
            }
            for chunk in result.get("chunks", []):
                indexes = set(chunk.get("paragraph_indexes") or [])
                overlap = indexes & table_paragraphs
                ratio = len(overlap) / max(1, len(indexes))
                chunk["table_paragraph_ratio"] = round(ratio, 4)
                if overlap and ratio >= 0.5:
                    chunk["kind"] = "table"
        except Exception:  # noqa: BLE001 - 表格读取失败不阻塞解析
            result["docx_tables"] = []
    output = args.get("output")
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result["output"] = str(output)
    return result
