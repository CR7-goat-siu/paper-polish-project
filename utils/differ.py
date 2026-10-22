"""差异计算与 diff 报告渲染。

为润色结果提供词级差异统计与 Markdown 报告，供人工复核、质量门禁与评测使用。
"""

from __future__ import annotations

import difflib
import re
from difflib import SequenceMatcher
from typing import Any

_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def similarity_ratio(source: str, polished: str) -> float:
    """归一化空白后的字符级相似度（0-1），作为过度修正 / 改动幅度度量。"""
    return SequenceMatcher(None, normalize_ws(source), normalize_ws(polished)).ratio()


def diff_stats(source: str, polished: str) -> dict[str, Any]:
    """词级差异统计：新增 / 删除词数、相似度、改动幅度。"""
    source_words = _words(source)
    polished_words = _words(polished)
    matcher = SequenceMatcher(None, source_words, polished_words, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return {
        "source_words": len(source_words),
        "polished_words": len(polished_words),
        "added_words": max(0, len(polished_words) - matched),
        "removed_words": max(0, len(source_words) - matched),
        "similarity_ratio": round(similarity_ratio(source, polished), 4),
        "change_ratio": round(1 - similarity_ratio(source, polished), 4),
    }


def unified_diff(source: str, polished: str) -> str:
    """行级 unified diff（结果不稳定时供日志与报告使用）。"""
    lines = list(
        difflib.unified_diff(
            (source or "").splitlines(),
            (polished or "").splitlines(),
            fromfile="source",
            tofile="polished",
            lineterm="",
        )
    )
    return "\n".join(lines)


def render_markdown_diff(source: str, polished: str, title: str = "") -> str:
    """生成人工复核用的 Markdown diff 报告：统计表 + 词级片段 + unified diff。"""
    stats = diff_stats(source, polished)
    matcher = SequenceMatcher(None, _words(source), _words(polished), autojunk=False)

    fragments: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = " ".join(_words(source)[i1:i2])
        added = " ".join(_words(polished)[j1:j2])
        if removed:
            fragments.append(f"- {removed}" + (f"\n+ {added}" if added else ""))
        elif added:
            fragments.append(f"+ {added}")

    lines: list[str] = []
    if title:
        lines.append(f"## {title}")
        lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 原文词数 | {stats['source_words']} |")
    lines.append(f"| 润色后词数 | {stats['polished_words']} |")
    lines.append(f"| 新增词 | {stats['added_words']} |")
    lines.append(f"| 删除词 | {stats['removed_words']} |")
    lines.append(f"| 相似度 | {stats['similarity_ratio']} |")
    lines.append(f"| 改动幅度 | {stats['change_ratio']} |")
    lines.append("")
    lines.append("### 词级改动片段")
    lines.append("")
    for frag in fragments[:80]:
        removed_part, _, added_part = frag.partition("\n+ ")
        if added_part:
            lines.append(f"- ~~{removed_part}~~ → {added_part}")
        elif removed_part.startswith("+ "):
            lines.append(f"- {removed_part[2:]}（新增）")
        else:
            lines.append(f"- ~~{removed_part}~~（删除）")
    if len(fragments) > 80:
        lines.append(f"- …（其余 {len(fragments) - 80} 处改动省略）")
    lines.append("")
    lines.append("### Unified diff")
    lines.append("")
    lines.append("```diff")
    lines.append(unified_diff(source, polished))
    lines.append("```")
    return "\n".join(lines)
