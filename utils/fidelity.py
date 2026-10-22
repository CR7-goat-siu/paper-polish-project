"""保真度度量工具：数字、引用标记、公式块、段落结构等关键要素抽取与比对。

review-gate（复核门禁）与 evaluator（评测）共用本模块，
用于判断润色前后论文的"硬事实"是否被破坏。
"""

from __future__ import annotations

import re
from typing import Any

# 数字 token：支持科学计数法（1e-5）、千分位、百分号。
# 拆成两个分支：
#   带符号分支——前导符号仅在"前面不是数字/点/逗号"且"前一个非空白字符不是数字"时生效，
#     使 "10-20"、"10 -20" 的连字符按区间分隔处理，而 "温度为 -20 度" 仍识别为负数；
#   无符号分支——只保留原守卫，保证 ", " 之后的数字（如 "3, 4.5 and 6" 中的 4.5）正常参与比对。
NUMBER_RE = re.compile(
    r"(?<![\d.,])(?<!\d\s)[+-]\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?%?"
    r"|(?<![\d.,])\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?%?"
)

# 引用标记的三类常见形式。
CITATION_BRACKET_RE = re.compile(r"\[\d+(?:\s*[,，\-–]\s*\d+)*\]")
CITATION_AUTHOR_YEAR_RE = re.compile(r"\([A-Z][A-Za-z\-']+(?:\s+(?:et al\.?|and|&|、|与)\s*[A-Z][A-Za-z\-']+)*,?\s*\d{4}[a-z]?\)")
CITATION_LATEX_RE = re.compile(r"\\(?:cite|citep|citet|citealp|autocite|ref|eqref|cref)\s*\*?(\[[^\]]*\])?\{[^}]+\}")

# 公式与代码块标记。
CODE_FENCE_RE = re.compile(r"```")


def extract_numbers(text: str) -> list[str]:
    """按出现顺序抽取数字 token（保留原样，用于存在性比对）。"""
    return NUMBER_RE.findall(text or "")


def _canonical(token: str) -> tuple[float, bool] | None:
    """把数字 token 规整为 (数值, 是否百分号)；无法解析返回 None。

    数值化比对使 "1e-5" 与 "0.00001"、"18.90%" 与 "18.9%" 视为同一数字，
    只有真实丢失（数值不存在）才报 missing。
    """
    percent = token.endswith("%")
    raw = token.rstrip("%").replace(",", "")
    try:
        return float(raw), percent
    except ValueError:
        return None


def _number_values(text: str) -> list[tuple[float, bool]]:
    values: list[tuple[float, bool]] = []
    for token in NUMBER_RE.findall(text or ""):
        pair = _canonical(token)
        if pair is not None:
            values.append(pair)
    return values


def _format_value(value: float, percent: bool) -> str:
    text = f"{value:g}"
    return text + "%" if percent else text


def numbers_preserved(source: str, polished: str) -> dict[str, list[str]]:
    """数值级保真比对：源文中每个数字（含区间端点、科学计数法）都必须在
    润色结果中以相同数值出现；格式差异（1e-5 ↔ 0.00001）不算丢失。

    missing：源文有而润色后缺失（硬错误）。added：润色后新增（warning 级，
    由调用方确认是否杜撰）。用 Counter 多重集实现，输出顺序与出现顺序一致。
    """
    from collections import Counter

    source_values = _number_values(source)
    polished_values = _number_values(polished)
    missing_counter = Counter(source_values) - Counter(polished_values)
    added_counter = Counter(polished_values) - Counter(source_values)

    missing: list[str] = []
    emitted: Counter = Counter()
    for value, percent in source_values:
        key = (value, percent)
        if emitted[key] < missing_counter.get(key, 0):
            missing.append(_format_value(value, percent))
            emitted[key] += 1

    added: list[str] = []
    emitted = Counter()
    for value, percent in polished_values:
        key = (value, percent)
        if emitted[key] < added_counter.get(key, 0):
            added.append(_format_value(value, percent))
            emitted[key] += 1
    return {"missing": missing, "added": added}


def citation_counts(text: str) -> dict[str, int]:
    """统计三类引用标记数量，用于"引用标记不得减少"检查。"""
    text = text or ""
    return {
        "bracket": len(CITATION_BRACKET_RE.findall(text)),
        "author_year": len(CITATION_AUTHOR_YEAR_RE.findall(text)),
        "latex": len(CITATION_LATEX_RE.findall(text)),
    }


def citations_preserved(source: str, polished: str) -> list[str]:
    """返回被削弱的引用标记类型列表（任一类数量减少即视为削弱）。"""
    src = citation_counts(source)
    out = citation_counts(polished)
    return [kind for kind, count in src.items() if count > 0 and out.get(kind, 0) < count]


def citations_added(source: str, polished: str) -> list[str]:
    """返回新增的引用标记类型列表（防杜撰引用，warning 级）。"""
    src = citation_counts(source)
    out = citation_counts(polished)
    return [kind for kind, count in out.items() if count > 0 and count > src.get(kind, 0)]


def count_paragraphs(text: str) -> int:
    """非空段落数（以空行分隔）。"""
    return len([p for p in re.split(r"\n\s*\n", text or "") if p.strip()])


def markup_counts(text: str) -> dict[str, int]:
    """统计公式定界符与代码围栏数量，用于结构保真检查。"""
    text = text or ""
    return {
        "double_dollar": text.count("$$") // 2,
        "equation_env": len(re.findall(r"\\begin\{(?:equation|align|gather)\*?\}", text)),
        "code_fences": len(CODE_FENCE_RE.findall(text)) // 2,
    }


def markup_preserved(source: str, polished: str) -> list[str]:
    """返回数量发生变化的标记类别。"""
    src = markup_counts(source)
    out = markup_counts(polished)
    return [kind for kind, count in src.items() if count > 0 and out.get(kind, 0) != count]


def fidelity_report(source: str, polished: str) -> dict[str, Any]:
    """一次性输出全部保真度检查结果（供门禁与评测复用）。"""
    numbers = numbers_preserved(source, polished)
    return {
        "numbers": numbers,
        "numbers_missing_count": len(numbers["missing"]),
        "numbers_added_count": len(numbers["added"]),
        "citations_weakened": citations_preserved(source, polished),
        "citations_added": citations_added(source, polished),
        "markup_changed": markup_preserved(source, polished),
        "paragraphs_source": count_paragraphs(source),
        "paragraphs_polished": count_paragraphs(polished),
    }
