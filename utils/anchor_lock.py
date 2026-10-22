"""anchor_lock：润色 Prompt 的锚点锁定层（L1 提示词契约，纯离线确定性）。

职责（对应 docs/hallucination-guardrails-plan.md L1）：
- 从待润色 chunk 提取五类"必保锚点"：数值 / 引用标记 / 图表公式编号 / 专有名词缩写 /
  对冲与极性词；
- render_anchor_block()：渲染为 prompt 中的锁定清单（模型必须逐项保留）；
- render_self_check()：渲染 CoVe 式六问自检表（输出前逐项核查，任一为否则先修正）。

与 guardrails 的分工：anchor_lock 管"生成前把约束讲清楚"，guardrails 管"生成后
确定性验回去"。两侧共用同一套词表来源（guardrails 的极性/对冲表、fidelity 的
数值/引用正则），避免提示词与审查器标准不一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from utils.config_loader import load_terms
from utils.fidelity import (
    CITATION_BRACKET_RE,
    _canonical,
    extract_numbers,
)
from utils.guardrails import HEDGE_COUNTERPARTS, POLARITY_PAIRS

NUMERIC_CONTEXT_WINDOW = 14
MAX_NUMERIC_ITEMS = 40
MAX_OTHER_ITEMS = 20

FIGURE_RE = re.compile(
    r"(?:图|表)\s*\d+(?:\s*[~\-–—、,，]\s*\d+)*(?:\(\s*[a-z]\s*\))?"
    r"|Fig(?:ure)?\.?\s*\d+[a-z]?(?:\s*[~\-–—]\s*\d+)?"
    r"|Table\s*\d+[a-z]?(?:\s*[~\-–—]\s*\d+)?"
    r"|Eq(?:uation)?\.?\s*[（(]\s*\d+\s*[)）](?:\s*[~\-–—]\s*\d+)?"
    r"|式\s*[（(]\s*\d+\s*[)）]",
    re.IGNORECASE,
)
ABBREV_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")
CAMEL_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b")
HYPHEN_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z]*-[A-Z][A-Za-z]+\b")
UPPER_CAMEL_RE = re.compile(r"\b[A-Z]{2,}[A-Za-z0-9]+\b")
ENTITY_STOPLIST = {
    "DOI", "URL", "PDF", "HTML", "XML", "USA", "UK", "PC", "TV", "CD", "DVD",
    "USB", "FAQ", "ID", "OK", "NaN", "JSON", "HTTP", "HTTPS", "CPU", "GPU",
    "RAM", "ROM", "SQL", "API", "SDK", "CLI", "GUI", "OS", "IO", "AI", "ML",
}

_ANCHOR_HEADER = {
    "cn": "## 本段必保锚点（锁定清单，输出中须逐一原样保留）",
    "en": "## Mandatory anchors for this paragraph (preserve each exactly as written)",
}
_SELF_CHECK_HEADER = {
    "cn": "## 输出前自检（CoVe 式逐项核查；任一项为“否”，先修正再输出）",
    "en": "## Pre-output self-check (answer each; if any is “no”, fix before emitting)",
}
_SELF_CHECK_ITEMS = {
    "cn": [
        "输出中的每个数值、百分比与单位，是否都能在原文中找到完全一致的对应？",
        "是否新增了任何原文没有的数字、百分比、机构名、数据集名、研究或出处？（必须为“否”）",
        "所有引用标记是否原样保留——未删除、未削弱、未新增、未改号？",
        "是否引入了浮夸、空洞或 AI 常用套话（如“深刻揭示”“综上所述”“首先/其次”）？（必须为“否”）",
        "对冲与限定语气（“可能/表明/在一定程度上”）的强度、极性词的方向，是否与原文一致？",
        "段落数量是否与原文一致？",
    ],
    "en": [
        "Does every number, percentage and unit in your output have an exact counterpart in the source?",
        "Have you added any number, percentage, organization, dataset, study or source absent from the source? (must be “no”)",
        "Are all citation markers preserved exactly — none dropped, weakened, added or renumbered?",
        "Have you introduced hype, empty wording or AI-slop phrases (e.g. “delve into”, “In conclusion”, “Firstly/Secondly”)? (must be “no”)",
        "Are hedge strength (may/suggest) and polarity direction identical to the source?",
        "Is the paragraph count identical to the source?",
    ],
}
_ANCHOR_TAIL = {
    "cn": "若输出缺失上述任何一项，或出现清单之外的新数值/新引用/新出处，一律视为未通过审查。",
    "en": "If any anchor is missing, or any number/citation/source outside this list appears, the output fails review.",
}


@dataclass
class AnchorSet:
    """五类必保锚点。numeric 元素: {"value", "display", "context"}。"""

    numeric: list[dict[str, Any]] = field(default_factory=list)
    citation: list[str] = field(default_factory=list)
    figure: list[str] = field(default_factory=list)
    entity: list[str] = field(default_factory=list)
    hedge: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.numeric or self.citation or self.figure or self.entity or self.hedge)


def _numeric_anchors(text: str) -> list[dict[str, Any]]:
    seen: set[tuple[float, bool]] = set()
    anchors: list[dict[str, Any]] = []
    for token in extract_numbers(text):
        pair = _canonical(token)
        if pair is None:
            continue
        value, percent = pair
        if (value, percent) in seen:
            continue
        seen.add((value, percent))
        position = text.find(token)
        context = text[max(0, position - NUMERIC_CONTEXT_WINDOW) : position + len(token) + NUMERIC_CONTEXT_WINDOW]
        anchors.append({"value": value, "display": token, "context": re.sub(r"\s+", " ", context).strip()})
        if len(anchors) >= MAX_NUMERIC_ITEMS:
            break
    return anchors


def _dedupe(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
        if len(ordered) >= limit:
            break
    return ordered


def _entity_anchors(text: str, language: str, domain: str | None) -> list[str]:
    entities: list[str] = []
    try:
        terms = load_terms(language, domain)
        for entry in terms.get("entries", []):
            canonical = str(entry.get("canonical") or "").strip()
            if canonical and canonical in text:
                entities.append(canonical)
    except Exception:  # noqa: BLE001 - 术语库缺失时退化为模式匹配
        pass
    for pattern in (ABBREV_RE, CAMEL_RE, HYPHEN_ENTITY_RE, UPPER_CAMEL_RE):
        entities.extend(match.group(0) for match in pattern.finditer(text))
    return _dedupe([item for item in entities if item not in ENTITY_STOPLIST and len(item) >= 2], MAX_OTHER_ITEMS)


def _hedge_anchors(text: str, language: str) -> list[str]:
    lowered = text.lower() if language == "en" else text
    words: list[str] = list(HEDGE_COUNTERPARTS.get(language, {}))
    for positive, negative in POLARITY_PAIRS:
        words.extend((positive, negative))
    found: list[str] = []
    for word in words:
        probe = word.lower() if language == "en" else word
        if language == "cn":
            if probe in lowered:
                found.append(word)
        elif re.search(r"(?<![A-Za-z])" + re.escape(probe) + r"(?![A-Za-z])", lowered):
            found.append(word)
    return _dedupe(found, MAX_OTHER_ITEMS)


def extract_anchors(
    text: str,
    *,
    language: str = "cn",
    domain: str | None = None,
    references_text: str | None = None,
) -> AnchorSet:
    """提取五类必保锚点。references_text 暂留作扩展（引用越界检查在 guardrails 侧）。"""
    text = text or ""
    return AnchorSet(
        numeric=_numeric_anchors(text),
        citation=_dedupe(CITATION_BRACKET_RE.findall(text), MAX_OTHER_ITEMS),
        figure=_dedupe((match.group(0) for match in FIGURE_RE.finditer(text)), MAX_OTHER_ITEMS),
        entity=_entity_anchors(text, language, domain),
        hedge=_hedge_anchors(text, language),
    )


def _section(title: str, items: list[str], language: str) -> list[str]:
    if not items:
        return []
    joined = " / ".join(items)
    return [f"- {title}: {joined}"]


def render_anchor_block(anchors: AnchorSet, *, language: str = "cn", max_items: int = MAX_NUMERIC_ITEMS) -> str:
    """渲染锚点锁定清单（ Markdown 区块，注入 prompt 的 {{anchors}}）。"""
    language = "cn" if language not in ("cn", "en") else language
    if anchors.is_empty():
        return (
            "（本段未提取到必保锚点；仍须遵守守恒总则。）"
            if language == "cn"
            else "(No anchors extracted for this paragraph; the conservation rules still apply.)"
        )
    numeric_displays = [str(item["display"]) for item in anchors.numeric][:max_items]
    if language == "cn":
        lines = [_ANCHOR_HEADER["cn"], ""]
        lines.extend(
            _section(
                f"数值（{len(numeric_displays)} 个，逐一保留，含精度与单位）",
                numeric_displays,
                language,
            )
        )
        lines.extend(_section("引用标记（原样保留，不得增删改号）", anchors.citation, language))
        lines.extend(_section("图表/公式编号（原样保留）", anchors.figure, language))
        lines.extend(_section("专有名词与缩写（原样保留）", anchors.entity, language))
        lines.extend(_section("对冲/极性词（强度与方向保持一致）", anchors.hedge, language))
        lines.extend(["", _ANCHOR_TAIL["cn"]])
    else:
        lines = [_ANCHOR_HEADER["en"], ""]
        lines.extend(
            _section(
                f"Numbers ({len(numeric_displays)}, keep precision and units)",
                numeric_displays,
                language,
            )
        )
        lines.extend(_section("Citation markers (keep exactly; no add/drop/renumber)", anchors.citation, language))
        lines.extend(_section("Figure/table/equation references (keep exactly)", anchors.figure, language))
        lines.extend(_section("Proper nouns and abbreviations (keep exactly)", anchors.entity, language))
        lines.extend(_section("Hedge/polarity words (keep strength and direction)", anchors.hedge, language))
        lines.extend(["", _ANCHOR_TAIL["en"]])
    return "\n".join(lines)


def render_self_check(language: str = "cn") -> str:
    """渲染 CoVe 式六问自检表（注入 prompt 的 {{self_check}}）。"""
    language = "cn" if language not in ("cn", "en") else language
    lines = [_SELF_CHECK_HEADER[language], ""]
    for index, question in enumerate(_SELF_CHECK_ITEMS[language], start=1):
        lines.append(f"{index}. {question}")
    return "\n".join(lines)
