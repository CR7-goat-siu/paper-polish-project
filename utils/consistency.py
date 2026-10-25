"""跨节一致性检查（确定性、纯标准库、离线）。

弥补 claim-guard / term-check（句级词面检查）覆盖不到的文档级结构缺陷：
- 图/表引用完整性：正文提到的图表必须有对应标题行，孤立标题行告警；
- 摘要数字一致性：摘要中的百分比在正文（含小数等价形式）找不到对应值则告警；
- 结论实体一致性：结论提及的数据集实体从未出现在实验/结果类章节则告警。

findings 结构与 issue-registry 的归一输入兼容（check / found / message /
context_snippet / suggestion / severity），来源名固定为 consistency-check。
所有告警只定位不修改：修数字本身属于作者责任区。
"""

from __future__ import annotations

import re
from typing import Any

from utils.sections import classify_section

# 图表标题行：行首（允许缩进/井号）+ 图/表 + 编号 + 冒号或句点。
# 图注常见写法：图 3：去噪结果 / Figure 3: Results / 表 1. 性能对比
_CAPTION_PATTERNS = {
    "figure": re.compile(r"^\s*#*\s*(?:图|Figure|Fig\.)\s*(\d+)\s*[:：.．]", re.IGNORECASE),
    "table": re.compile(r"^\s*#*\s*表\s*(\d+)\s*[:：.．]"),
}
# 正文提及（含标题行自身，按行首规则区分角色）
_MENTION_PATTERNS = {
    "figure": re.compile(r"(?:图|Figure|Fig\.)\s*(\d+)", re.IGNORECASE),
    "table": re.compile(r"表\s*(\d+)"),
}
_ABSTRACT_HEADING_RE = re.compile(r"^\s*#*\s*(?:摘要|Abstract)\s*[:：]?\s*$", re.IGNORECASE)
_NEXT_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+\S|\d+(?:\.\d+)*\s+\S|(?:关键词|Keywords?)\b)", re.IGNORECASE
)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# 数据集实体：数据集 B / 数据集B / Dataset B / dataset-B（实体名限定大写字母/数字/中文序数）
_ENTITY_RE = re.compile(r"(?:数据集|Dataset)\s*[- ]?([A-Z](?:[A-Za-z0-9_-]*)|\d+|[一二三四五六七八九十])")
_KIND_LABEL = {"figure": "图", "table": "表"}


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _split_sections(text: str, language: str) -> list[dict[str, Any]]:
    """按 Markdown/编号标题行切成 [{heading, section_type, lines}]，供实体一致性用。"""
    sections: list[dict[str, Any]] = [{"heading": "", "section_type": "", "lines": []}]
    for line in text.splitlines():
        stripped = line.strip()
        heading_match = re.match(r"^#{1,6}\s+(.+)$", stripped) or re.match(
            r"^\d+(?:\.\d+)*\s+(.+)$", stripped
        )
        if heading_match:
            sections.append(
                {
                    "heading": heading_match.group(1).strip(),
                    "section_type": classify_section(heading_match.group(1).strip(), language),
                    "lines": [],
                }
            )
        else:
            sections[-1]["lines"].append(line)
    return sections


def check_figure_table_refs(text: str) -> list[dict[str, Any]]:
    """图表引用完整性：正文引用必须有标题，标题未被引用则告警。"""
    findings: list[dict[str, Any]] = []
    lines = text.splitlines()
    captions: dict[str, set[int]] = {"figure": set(), "table": set()}
    mentions: dict[str, dict[int, int]] = {"figure": {}, "table": {}}  # num -> line
    mention_labels: dict[tuple[str, int], str] = {}
    offset = 0
    for line in lines:
        caption_nums: dict[str, int | None] = {"figure": None, "table": None}
        for kind, pattern in _CAPTION_PATTERNS.items():
            match = pattern.match(line)
            if match:
                caption_nums[kind] = int(match.group(1))
        for kind, pattern in _MENTION_PATTERNS.items():
            for match in pattern.finditer(line):
                num = int(match.group(1))
                if num == caption_nums.get(kind):
                    continue  # 标题行自身计为 caption，不计入正文引用
                mentions[kind].setdefault(num, _line_of(text, offset + match.start()))
                mention_labels.setdefault((kind, num), re.sub(r"\s+", " ", match.group(0)))
        for kind, num in caption_nums.items():
            if num is not None:
                captions[kind].add(num)
        offset += len(line) + 1

    for kind in ("figure", "table"):
        label = _KIND_LABEL[kind]
        for num in sorted(set(mentions[kind]) - captions[kind]):
            line_no = mentions[kind][num]
            found = mention_labels.get((kind, num), f"{label} {num}")
            findings.append(
                {
                    "check": f"{kind}_reference",
                    "found": found,
                    "severity": "error",
                    "message": f"正文第 {line_no} 行引用了 {found}，但全文没有对应标题行",
                    "context_snippet": f"line {line_no}: {found} 未定义",
                    "suggestion": f"补齐 {found} 的标题与内容，或改引已有编号",
                }
            )
        for num in sorted(captions[kind] - set(mentions[kind])):
            findings.append(
                {
                    "check": "orphan_caption",
                    "found": f"{label} {num}",
                    "severity": "warning",
                    "message": f"{label} {num} 有标题行但正文从未引用",
                    "context_snippet": f"{label} {num} caption not referenced",
                    "suggestion": "在正文相应位置补充引用，或删除孤立标题",
                }
            )
    return findings


def _extract_abstract(text: str) -> tuple[str, int, int] | None:
    """返回 (摘要文本, 正文起始行下标0基, 结束行下标0基不含)；找不到返回 None。

    返回原始行范围（含摘要段内的空行），供调用方精确剔除摘要段。
    无独立摘要标题时回退：文档以单个 `# 标题` 起始且紧跟正文段，
    视第一段为摘要（arXiv/LaTeXML 渲染与多数投稿模板的常见形态）。
    """
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if _ABSTRACT_HEADING_RE.match(line):
            start = index + 1
            break
    if start is None:
        # 回退：# 标题 后的第一段视为摘要（要求标题在首行、段落非空）
        if lines and lines[0].startswith("# ") and not lines[0].startswith("## "):
            candidate = 1
            while candidate < len(lines) and not lines[candidate].strip():
                candidate += 1
            if candidate < len(lines) and lines[candidate].strip():
                start = candidate
        if start is None:
            return None
    body: list[str] = []
    if start is not None and start < len(lines) and _ABSTRACT_HEADING_RE.match(lines[start - 1] if start else ""):
        for line in lines[start:]:
            if _NEXT_SECTION_HEADING_RE.match(line.strip()) or _ABSTRACT_HEADING_RE.match(line):
                break
            body.append(line)
    else:
        # 回退路径：摘要段 = 标题后的第一段（到第一个空行或标题行为止）
        for line in lines[start:]:
            if not line.strip() or line.lstrip().startswith("#"):
                break
            body.append(line)
    abstract = "\n".join(body).strip()
    if not abstract:
        return None
    return abstract, start, start + len(body)


def check_abstract_numbers(text: str) -> list[dict[str, Any]]:
    """摘要百分比一致性：摘要中的每个百分比都应能在正文找到（含 0.xx 小数等价）。"""
    extracted = _extract_abstract(text)
    if extracted is None:
        return []
    abstract, abstract_start, abstract_end = extracted
    lines = text.splitlines()
    # 正文 = 全文精确剔除摘要段（含段内空行），避免摘要数字泄漏进正文比对
    rest_lines = lines[:abstract_start] + lines[abstract_end:]
    body = "\n".join(rest_lines)
    body_percents = {float(m.group(1)) for m in _PERCENT_RE.finditer(body)}
    body_numbers = [float(m.group()) for m in _NUMBER_RE.finditer(body)]

    findings: list[dict[str, Any]] = []
    seen: set[float] = set()
    for match in _PERCENT_RE.finditer(abstract):
        value = float(match.group(1))
        if value in seen:
            continue
        seen.add(value)
        if value in body_percents:
            continue
        # 小数等价：94.7% ↔ 0.947
        equivalent = any(
            0 < num < 1 and abs(num * 100 - value) <= 0.05 for num in body_numbers
        )
        if equivalent:
            continue
        findings.append(
            {
                "check": "abstract_number",
                "found": f"{match.group(1)}%",
                "severity": "warning",
                "message": f"摘要声称 {match.group(1)}%，正文找不到对应数值（含小数等价形式）",
                "context_snippet": abstract[:160],
                "suggestion": "核对摘要与结果章节的数值是否一致；摘要数字必须来自实验结果",
            }
        )
    return findings


def check_conclusion_entities(text: str, *, language: str = "cn") -> list[dict[str, Any]]:
    """结论实体一致性：结论提到的数据集实体若从未出现在实验/结果类章节则告警。

    只对"在结论之外已出现过、却缺席实验章节"的实体告警——
    这是"数据集只出现在设置与结论、结果里从未使用"这类断裂的确定性信号；
    结论首次提及的泛指实体不告警，避免误报。
    """
    sections = _split_sections(text, language)
    conclusion = [s for s in sections if s["section_type"] == "conclusion"]
    if not conclusion:
        return []
    experiment_text = "\n".join(
        line
        for s in sections
        if s["section_type"] in ("experiment", "discussion")
        for line in ([s["heading"]] if s["heading"] else []) + s["lines"]
    )
    non_conclusion_text = "\n".join(
        line
        for s in sections
        if s["section_type"] != "conclusion"
        for line in ([s["heading"]] if s["heading"] else []) + s["lines"]
    )
    experiments_by_name = _entity_index(experiment_text)
    established_names = _entity_index(non_conclusion_text)
    findings: list[dict[str, Any]] = []
    for section in conclusion:
        conclusion_index = _entity_index("\n".join(section["lines"]))
        for name, quote in conclusion_index.items():
            if name in experiments_by_name or name not in established_names:
                continue
            findings.append(
                {
                    "check": "entity_consistency",
                    "found": f"数据集 {name}",
                    "severity": "warning",
                    "message": (
                        f"结论提到“数据集 {name}”，但该实体从未出现在实验/结果章节"
                        f"（引文：{quote[:60]}）"
                    ),
                    "context_snippet": quote[:160],
                    "suggestion": "核对实验设置：结论引用的每个数据集都应有对应实验结果",
                }
            )
    return findings


def _entity_index(text: str) -> dict[str, str]:
    index: dict[str, str] = {}
    for match in _ENTITY_RE.finditer(text):
        name = match.group(1)
        index.setdefault(name, match.group(0))
    return index


def check_consistency(text: str, *, language: str = "cn") -> list[dict[str, Any]]:
    """运行全部跨节一致性检查，返回 findings 列表（无网络、无文件写入）。"""
    if not text or not text.strip():
        return []
    findings: list[dict[str, Any]] = []
    findings.extend(check_figure_table_refs(text))
    findings.extend(check_abstract_numbers(text))
    findings.extend(check_conclusion_entities(text, language=language))
    return findings
