"""format-adapter Skill：格式与参考文献规范化（纯离线的确定性规则处理）。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.config_loader import normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger("format-adapter")

SUPPORTED_STYLES = ("keep", "ieee", "apa", "gbt7714")
DOI_RE = re.compile(r"(?i)\bdoi\s*[:：]\s*(10\.\d{4,}[^\s,;]+)")
DOI_DOT_RE = re.compile(r"(?i)\bdoi\s*[:：]\s*")
PAGE_RANGE_RE = re.compile(r"\b(\d{1,5})\s*[-–—]\s*(\d{1,5})\b")
SPACE_RE = re.compile(r"[ \t]+")
# 参考文献行特征：[n] 开头 / pp. vol. no. 等标记 / 中文页码。页码区间归一只作用于这类行，
# 避免把正文中的数值区间（如 "0-100 ms"、"2-4 epochs"）误改成 en dash。
REF_LINE_HINT_RE = re.compile(
    r"(?i)^\s*\[\d+\]|\bpp?\.\s|\bvol\.\s|\bno\.\s|\bpages?\b|第\s*\d+\s*[页卷期]"
)


def _normalize_doi(match: re.Match[str]) -> str:
    doi = match.group(1).rstrip(".,;，。；")
    return f"https://doi.org/{doi}"


def _normalize_page_ranges_line_aware(text: str) -> tuple[str, int]:
    changed = 0
    out_lines: list[str] = []
    for line in text.split("\n"):
        if REF_LINE_HINT_RE.search(line):
            line, count = PAGE_RANGE_RE.subn(r"\1–\2", line)
            changed += count
        out_lines.append(line)
    return "\n".join(out_lines), changed


def normalize_locally(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    normalized = text
    if DOI_RE.search(normalized):
        normalized = DOI_RE.sub(_normalize_doi, normalized)
        changes.append("DOI 统一为 https://doi.org/ 形式")
    normalized, page_changes = _normalize_page_ranges_line_aware(normalized)
    if page_changes:
        changes.append("页码范围统一使用半字线（仅限参考文献行）")
    if DOI_DOT_RE.search(normalized) and "https://doi.org/" not in normalized:
        changes.append("移除裸 DOI 前缀标记")
    normalized = SPACE_RE.sub(" ", normalized)
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.rstrip() + ("\n" if normalized.endswith("\n") else ""), changes


def run(args: dict[str, Any]) -> dict[str, Any]:
    language = normalize_language(str(args.get("language", "en")))
    text = args.get("text")
    text_file = args.get("text_file")
    if text is None and text_file:
        text = Path(text_file).read_text(encoding="utf-8-sig")
    if text is None:
        raise ValueError("format-adapter run() 需要 text_file 或 text")
    style = str(args.get("style", "keep")).lower()
    if style not in SUPPORTED_STYLES:
        raise ValueError(f"未知 style: {style!r}，可选 {SUPPORTED_STYLES}")
    mode = str(args.get("mode", "local")).lower()
    if mode not in ("local", "offline"):
        raise ValueError("format-adapter 已离线化：model 模式移除，请由coding harness 处理")
    adapter_text, changes = normalize_locally(text)
    result = {
        "language": language,
        "mode": "local",
        "target_style": style,
        "text": adapter_text,
        "changes": changes,
    }
    output = args.get("output")
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(adapter_text, encoding="utf-8")
        result["output"] = str(output)
    return result
