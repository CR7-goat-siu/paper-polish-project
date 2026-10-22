"""claim-guard Skill：客观性与学术语域守卫（纯离线）。

检测（a）浮夸/主观论断（hype）、（b）缺少数据锚点的强化表述、（c）口语化/冗余表达。
基于 data/lexicon/ 词库的确定性扫描，不调用模型、不发起网络请求。
不修改原文，只输出 findings，供复核门禁与人工复核使用。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.config_loader import load_lexicon, normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger("claim-guard")

SNIPPET_LEN = 120
ANCHOR_WINDOW = 60

# 数据锚点：匹配位置附近出现数字 / 百分号 / 倍数 / 表格引用则视为有锚。
_ANCHOR_RE = re.compile(r"\d|%|％|倍|Table|表\s*\d|Figure|图\s*\d|\\cite|\[\d")


def _split_top_level(pattern: str) -> list[str]:
    """按顶层 | 拆分正则交替分支（忽略括号内的 |）。"""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in pattern:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _compile_pattern(pattern: str, language: str) -> re.Pattern[str]:
    if language == "cn":
        return re.compile(pattern)
    # 词边界前缀始终需要；尾随边界按分支添加：仅当该分支以字母数字结尾时加，
    # 否则会破坏以空格结尾的短语（如 "we can see from "）的匹配。
    prefix = r"(?<![A-Za-z0-9])"
    branches = "|".join(
        branch + (r"(?![A-Za-z0-9])" if branch and branch[-1].isalnum() else "")
        for branch in _split_top_level(pattern)
        if branch
    )
    return re.compile(prefix + "(?:" + branches + ")", re.IGNORECASE)


def _has_anchor(text: str, start: int, end: int) -> bool:
    left = max(0, start - ANCHOR_WINDOW)
    right = min(len(text), end + ANCHOR_WINDOW)
    return bool(_ANCHOR_RE.search(text[left:right]))


def _context_snippet(text: str, start: int, end: int) -> str:
    left = max(0, start - SNIPPET_LEN // 2)
    right = min(len(text), end + SNIPPET_LEN // 2)
    snippet = text[left:right].replace("\n", " ")
    if left > 0:
        snippet = "…" + snippet
    if right < len(text):
        snippet = snippet + "…"
    return snippet


def check_text_local(text: str, language: str) -> dict[str, Any]:
    """词库驱动的确定性扫描：返回 findings 报告。"""
    language = normalize_language(language)
    findings: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}

    # 白名单逃生口：config/<lang>-rules.yaml -> objectivity.lexicon_whitelist
    # 列出词库条目 id（如 ["chedi"]），被列入的词条不再报 error/warning，
    # 供原稿正当使用特定表述（如引用原文标题）时按需豁免；默认为空，不改变默认行为。
    whitelist: set[str] = set()
    try:
        from utils.config_loader import load_rules_config

        whitelist = set(
            load_rules_config(language).get("objectivity", {}).get("lexicon_whitelist", []) or []
        )
    except Exception:  # noqa: BLE001 - 规则缺失时白名单为空
        whitelist = set()

    for kind in ("hype", "informal"):
        lexicon = load_lexicon(kind, language)
        for entry in lexicon.get("entries", []):
            if str(entry.get("id", "")) in whitelist:
                continue
            pattern = str(entry.get("pattern", "")).strip()
            if not pattern:
                continue
            try:
                regex = _compile_pattern(pattern, language)
            except re.error:
                logger.warning("无效词库正则，已跳过: %s", pattern)
                continue
            severity = str(entry.get("severity", "info"))
            requires_anchor = bool(entry.get("requires_data_anchor", False))
            matches = list(regex.finditer(text or ""))
            if not matches:
                continue
            unanchored = []
            for match in matches:
                if requires_anchor and not _has_anchor(text, match.start(), match.end()):
                    unanchored.append(match)
            effective_severity = severity
            if unanchored:
                escalate = str(entry.get("unanchored_severity", "") or severity)
                effective_severity = max(severity, escalate, key=_SEVERITY_ORDER.index)
            first = unanchored[0] if unanchored else matches[0]
            key = (str(entry.get("id", pattern)), str(first.group(0)).lower())
            if key in seen:
                seen[key]["occurrences"] += len(matches)
                continue
            finding = {
                "id": str(entry.get("id", pattern)),
                "lexicon": kind,
                "category": str(entry.get("category", kind)),
                "found": first.group(0),
                "issue": "unanchored_claim" if unanchored else str(entry.get("category", kind)),
                "severity": effective_severity,
                "suggestion": str(entry.get("suggestion", "")),
                "context_snippet": _context_snippet(text, first.start(), first.end()),
                "occurrences": len(matches),
                "requires_data_anchor": requires_anchor,
                "source": "local",
            }
            seen[key] = finding
            findings.append(finding)

    return _build_report(findings, language, "local")


_SEVERITY_ORDER = ["info", "warning", "error"]


def _build_report(findings: list[dict[str, Any]], language: str, mode: str) -> dict[str, Any]:
    summary = {"error": 0, "warning": 0, "info": 0, "total": len(findings)}
    for finding in findings:
        severity = str(finding.get("severity", "info")).lower()
        if severity in summary:
            summary[severity] += 1
    return {"language": language, "mode": mode, "findings": findings, "summary": summary}


def run(args: dict[str, Any]) -> dict[str, Any]:
    language = normalize_language(str(args.get("language", "en")))
    text = args.get("text")
    text_file = args.get("text_file")
    if text is None and text_file:
        text = Path(text_file).read_text(encoding="utf-8-sig")
    if text is None:
        raise ValueError("claim-guard run() 需要 text_file 或 text")
    mode = str(args.get("mode", "local")).lower()
    if mode not in ("local", "auto", "offline"):
        raise ValueError(f"claim-guard 为纯本地检查，不支持 mode: {mode!r}")
    report = check_text_local(text, language)
    output = args.get("output")
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["output"] = str(output)
    return report
