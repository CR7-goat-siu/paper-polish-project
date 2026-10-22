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


def _all_occurrences(text: str, phrase: str, language: str) -> list[str]:
    if not phrase:
        return []
    if language == "cn":
        indexes = [m.start() for m in re.finditer(re.escape(phrase), text)]
    else:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])"
        indexes = [m.start() for m in re.finditer(pattern, text, flags=re.IGNORECASE)]
    snippets = []
    for start in indexes:
        left = max(0, start - SNIPPET_LEN // 2)
        right = min(len(text), start + len(phrase) + SNIPPET_LEN // 2)
        snippet = text[left:right].replace("\n", " ")
        if left > 0:
            snippet = "…" + snippet
        if right < len(text):
            snippet = snippet + "…"
        snippets.append(snippet)
    return snippets[:5]


def check_text_local(text: str, language: str, domain: str | None = None) -> dict[str, Any]:
    language = normalize_language(language)
    terms_data = load_terms(language, domain)
    findings: list[dict[str, Any]] = []
    entries = terms_data.get("entries", [])
    for entry in entries:
        canonical = str(entry.get("canonical", "")).strip()
        aliases = [str(x) for x in entry.get("aliases", []) if str(x).strip()]
        nonstandard = [str(x) for x in entry.get("nonstandard", []) if str(x).strip()]
        if not canonical:
            continue
        canonical_hits = _all_occurrences(text, canonical, language)
        alias_hits: list[tuple[str, list[str]]] = []
        nonstandard_hits: list[tuple[str, list[str]]] = []
        for alias in aliases:
            hits = _all_occurrences(text, alias, language)
            if hits:
                alias_hits.append((alias, hits))
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
