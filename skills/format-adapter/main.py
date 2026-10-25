"""format-adapter Skill：格式与参考文献规范化（纯离线的确定性规则处理）。

两种模式：
- local（默认）：DOI / 页码范围 / 空白归一（行为与历史版本完全一致）；
- refill_plan：文献条目补全的 Token 级改写计划——输入原始条目行与
  verified 元数据（lit-search refill → lit-verify 链条的产物），用
  difflib.SequenceMatcher 产出逐 token 的 replace/insert/delete 计划，
  每条计划显式打标 auto_filled_with_source（注明 DOI / 数据源证据）。
  计划不落正文：是否应用由人工审阅 / cite-manager 决定。
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.citation_check import format_reference  # noqa: E402
from utils.config_loader import normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger("format-adapter")

SUPPORTED_STYLES = ("keep", "ieee", "apa", "gbt7714")
REFILL_STYLES = ("ieee", "apa", "gbt7714")
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


# --------------------------------------------------------------------------- #
# refill_plan：文献条目补全的 Token 级改写计划
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(r"\S+")
# 记录每个 token 在原行中的偏移，供下游做外科式替换
_TOKEN_SPAN_RE = re.compile(r"\S+")


def _tokens_with_spans(line: str) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    for match in _TOKEN_SPAN_RE.finditer(line):
        tokens.append({"text": match.group(0), "start": match.start(), "end": match.end()})
    return tokens


def token_rewrite_plan(current: str, target: str) -> list[dict[str, Any]]:
    """Token 级 diff：difflib.SequenceMatcher 产出的有序操作列表。

    op ∈ {equal, replace, insert, delete}；source_tokens/current_span 对应原行，
    target_tokens 对应补全后的目标行。确定性（同输入同输出）。
    """
    source = _tokens_with_spans(current)
    target_tokens = [match.group(0) for match in _TOKEN_RE.finditer(target)]
    matcher = difflib.SequenceMatcher(
        a=[token["text"] for token in source], b=target_tokens, autojunk=False
    )
    ops: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        op: dict[str, Any] = {"op": tag}
        if i2 > i1:
            op["source_tokens"] = [token["text"] for token in source[i1:i2]]
            op["current_span"] = [source[i1]["start"], source[i2 - 1]["end"]]
        else:
            op["source_tokens"] = []
        if j2 > j1:
            op["target_tokens"] = target_tokens[j1:j2]
        else:
            op["target_tokens"] = []
        ops.append(op)
    return ops


def _fields_added(verified: dict[str, Any]) -> list[str]:
    return [field for field in ("year", "venue", "volume", "issue", "pages", "doi") if verified.get(field)]


def build_refill_plans(entries: list[dict[str, Any]], style: str) -> dict[str, Any]:
    """为 (原始行, verified 元数据) 对生成改写计划；verified 缺失的条目跳过。"""
    plans: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number", entry.get("id"))
        line = str(entry.get("line") or "").strip()
        verified = entry.get("verified") or {}
        if not line or not isinstance(verified, dict) or not verified.get("title"):
            skipped.append({"number": number, "reason": "missing_verified_metadata"})
            continue
        target = format_reference(verified, style).strip()
        plans.append(
            {
                "number": number,
                "current_line": line,
                "target_line": target,
                "token_ops": token_rewrite_plan(line, target),
                "fields_added": _fields_added(verified),
                "auto_filled_with_source": {
                    "doi": str(verified.get("doi") or ""),
                    "source": str(verified.get("source") or ""),
                    "source_id": str(verified.get("source_id") or ""),
                    "title": str(verified.get("title") or ""),
                    "year": verified.get("year"),
                    "venue": str(verified.get("venue") or ""),
                    "volume": str(verified.get("volume") or ""),
                    "issue": str(verified.get("issue") or ""),
                    "pages": str(verified.get("pages") or ""),
                },
            }
        )
    return {
        "mode": "refill_plan",
        "style": style,
        "plan_count": len(plans),
        "plans": plans,
        "skipped": skipped,
        "status": "built" if plans else "no_plans",
        "warnings": [
            "改写计划仅供审阅或 cite-manager 应用；本 Skill 不直接改写正文。",
            "每条计划带 auto_filled_with_source 证据（DOI/数据源/来源 ID），人工审阅时逐条核对。",
        ],
    }


def run(args: dict[str, Any]) -> dict[str, Any]:
    language = normalize_language(str(args.get("language", "en")))
    style = str(args.get("style", "keep")).lower()
    mode = str(args.get("mode", "local")).lower()
    if mode == "refill_plan":
        if style not in REFILL_STYLES:
            raise ValueError(f"refill_plan 需要 {REFILL_STYLES} 之一的目标 style，当前: {style!r}")
        entries = args.get("entries")
        if entries is None:
            entries_file = args.get("entries_file")
            if not entries_file:
                raise ValueError("refill_plan 需要 entries 或 entries_file")
            entries = json.loads(Path(str(entries_file)).read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("entries 必须是条目列表")
        return build_refill_plans(entries, style)
    if mode not in ("local", "offline"):
        raise ValueError("format-adapter 已离线化：model 模式移除，请由coding harness 处理")
    text = args.get("text")
    text_file = args.get("text_file")
    if text is None and text_file:
        text = Path(text_file).read_text(encoding="utf-8-sig")
    if text is None:
        raise ValueError("format-adapter run() 需要 text_file 或 text")
    if style not in SUPPORTED_STYLES:
        raise ValueError(f"未知 style: {style!r}，可选 {SUPPORTED_STYLES}")
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
