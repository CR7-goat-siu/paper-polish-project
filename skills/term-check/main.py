"""term-check Skill：中英文术语一致性校验（纯离线，基于术语库的确定性检查）。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.config_loader import load_terms, normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger("term-check")

SNIPPET_LEN = 60


def _occurrence_positions(text: str, phrase: str, language: str) -> list[int]:
    """短语在文本中的出现位置（start 下标）。en 侧加词边界防子串误配。"""
    if not phrase:
        return []
    if language == "cn":
        return [m.start() for m in re.finditer(re.escape(phrase), text)]
    pattern = r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])"
    return [m.start() for m in re.finditer(pattern, text, flags=re.IGNORECASE)]


def _all_occurrences(text: str, phrase: str, language: str) -> list[str]:
    return [
        _snip(text, start, len(phrase)) for start in _occurrence_positions(text, phrase, language)
    ][:5]


def _snip(text: str, start: int, length: int) -> str:
    left = max(0, start - SNIPPET_LEN // 2)
    right = min(len(text), start + length + SNIPPET_LEN // 2)
    snippet = text[left:right].replace("\n", " ")
    if left > 0:
        snippet = "…" + snippet
    if right < len(text):
        snippet = snippet + "…"
    return snippet


def _overlaps_any(start: int, length: int, spans: list[tuple[int, int]]) -> bool:
    """判断 [start, start+length) 是否完全落在任一 span 内（包含关系）。"""
    return any(cs <= start and start + length <= ce for cs, ce in spans)


def _strip_references_region(text: str) -> str:
    """去掉参考文献区（References / thebibliography）：条目里的论文标题是
    引用材料的原题，术语一致性检查不适用（与润色跳过 references chunk 同理）。"""
    region = re.search(
        r"(?:^References\s*$|^## References\s*$|\\begin\{thebibliography\}).*?(?:\\end\{thebibliography\}|\Z)",
        text,
        re.M | re.S | re.IGNORECASE,
    )
    if not region:
        return text
    return text[: region.start()] + "\n"


def check_text_local(text: str, language: str, domain: str | None = None) -> dict[str, Any]:
    language = normalize_language(language)
    text = _strip_references_region(text)
    terms_data = load_terms(language, domain)
    findings: list[dict[str, Any]] = []
    entries = terms_data.get("entries", [])
    for entry in entries:
        canonical = str(entry.get("canonical", "")).strip()
        aliases = [str(x) for x in entry.get("aliases", []) if str(x).strip()]
        nonstandard = [str(x) for x in entry.get("nonstandard", []) if str(x).strip()]
        if not canonical:
            continue
        # 包含关系去歧义：别名与规范词互为子串时（如“注意力”⊂“注意力机制”、
        # “泛化”⊂“泛化能力”），落在对方出现范围内的匹配不计为独立用法，
        # 否则每次都会误报 mixed_usage / 假 canonical 命中。
        alias_positions: dict[str, list[int]] = {}
        for alias in aliases:
            alias_positions[alias] = _occurrence_positions(text, alias, language)
        alias_spans = [
            (start, start + len(alias))
            for alias, positions in alias_positions.items()
            for start in positions
        ]
        # 英文形态变体：别名若只是规范词的复数（canonical + s/es），
        # 该处出现计为规范用法而非别名混用（学术文本复数无处不在）。
        morph_canonical_spans: list[tuple[int, int]] = []
        if language == "en":
            for alias, positions in alias_positions.items():
                lowered = alias.lower()
                if lowered in (canonical.lower() + "s", canonical.lower() + "es"):
                    morph_canonical_spans.extend(
                        (start, start + len(alias)) for start in positions
                    )
        canonical_positions = [
            start
            for start in _occurrence_positions(text, canonical, language)
            if not _overlaps_any(start, len(canonical), alias_spans)
        ]
        canonical_spans = [
            (start, start + len(canonical)) for start in canonical_positions
        ] + morph_canonical_spans
        canonical_hits = [
            _snip(text, start, len(canonical)) for start in canonical_positions[:5]
        ]
        # 首用定义模式："规范词 (别名)" 或 "别名 (规范词)" —— 这是定义而非混用。
        defined_alias_spans: list[tuple[int, int]] = []
        if canonical_positions or morph_canonical_spans:
            for alias in aliases:
                for defining, defined in (
                    (canonical, alias),
                    (alias, canonical),
                ):
                    pattern = re.compile(
                        re.escape(defining)
                        + r"\s*\(\s*("
                        + re.escape(defined)
                        + r"(?:s|es)?)\s*\)",
                        re.IGNORECASE if language == "en" else 0,
                    )
                    for match in pattern.finditer(text):
                        defined_alias_spans.append((match.start(1), match.end(1)))
        alias_hits: list[tuple[str, list[str]]] = []
        for alias in aliases:
            hits = [
                _snip(text, start, len(alias))
                for start in alias_positions[alias]
                if not _overlaps_any(start, len(alias), canonical_spans)
                and not _overlaps_any(start, len(alias), defined_alias_spans)
            ][:5]
            if hits:
                alias_hits.append((alias, hits))
        nonstandard_hits: list[tuple[str, list[str]]] = []
        for bad in nonstandard:
            hits = _all_occurrences(text, bad, language)
            if hits:
                nonstandard_hits.append((bad, hits))

        if not canonical_hits and not alias_hits and not nonstandard_hits:
            continue
        if nonstandard_hits:
            for bad, hits in nonstandard_hits:
                findings.append(
                    {
                        "term_id": entry.get("id", canonical),
                        "found": bad,
                        "canonical": canonical,
                        "issue": "nonstandard_use",
                        "severity": "error",
                        "suggestion": f"建议改为规范写法“{canonical}”",
                        "context_snippet": hits[0],
                        "occurrences": len(hits),
                    }
                )
            continue
        if not canonical_hits and alias_hits:
            alias, hits = alias_hits[0]
            findings.append(
                {
                    "term_id": entry.get("id", canonical),
                    "found": alias,
                    "canonical": canonical,
                    "issue": "alias_without_canonical",
                    "severity": "warning",
                    "suggestion": f"首次出现时给出规范写法“{canonical}”，随后保持别名一致",
                    "context_snippet": hits[0],
                    "occurrences": len(hits),
                }
            )
            continue
        if canonical_hits and alias_hits:
            alias, hits = alias_hits[0]
            findings.append(
                {
                    "term_id": entry.get("id", canonical),
                    "found": alias,
                    "canonical": canonical,
                    "issue": "mixed_usage",
                    "severity": "info",
                    "suggestion": "检查别名是否需要首处定义或统一为规范写法",
                    "context_snippet": hits[0],
                    "occurrences": len(hits),
                }
            )
    return _build_report(findings, language, "local")


def _build_report(findings: list[dict[str, Any]], language: str, mode: str) -> dict[str, Any]:
    summary = {"error": 0, "warning": 0, "info": 0, "total": len(findings)}
    for finding in findings:
        severity = str(finding.get("severity", "info")).lower()
        if severity in summary:
            summary[severity] += 1
    return {
        "language": language,
        "mode": mode,
        "findings": findings,
        "summary": summary,
    }


def run(args: dict[str, Any]) -> dict[str, Any]:
    language = normalize_language(str(args.get("language", "en")))
    domain = args.get("domain")
    if domain in ("", None):
        domain = None
    text = args.get("text")
    text_file = args.get("text_file")
    if text is None and text_file:
        text = Path(text_file).read_text(encoding="utf-8-sig")
    if text is None:
        raise ValueError("term-check run() 需要 text_file 或 text")
    mode = str(args.get("mode", "local")).lower()
    if mode not in ("local", "auto", "offline"):
        raise ValueError("term-check 已离线化：model 模式移除，请由coding harness 处理")
    report = check_text_local(text, language, domain)
    output = args.get("output")
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["output"] = str(output)
    return report
