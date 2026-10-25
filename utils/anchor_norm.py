"""锚定文本归一：模型"逐字引用"与原文之间的字符级等价变换。

用途：review-sim / cite-intent 的 fail-closed 锚定校验。真实模型复制原文时
常引入排版级差异（弯引号→直引号、连字 ﬁ→fi、弯划→连字符、不可断空格），
这些是字符级等价，应当放行；而任何措辞改写（换词、调序、增删内容）仍然
必须拒绝——本模块只做等价变换，永不做语义匹配，这条边界是红线。

归一规则（全部为字符级）：
- NFKC Unicode 归一（连字分解、全角→半角、NBSP/全角空格→空格）；
- 弯引号/Prime 记号 → 直引号（“”‘’″′ → " '）；
- 各类短划/长划/数学减号 → ASCII 连字符；
- 省略号 … → ...；
- 连续空白折叠为单空格并去首尾；
- 可选 casefold（两处校验器统一开启：大小写差异属字符级而非语义级）。
"""

from __future__ import annotations

import re
import unicodedata

_CHAR_MAP = {
    # 弯引号 / Prime → 直引号
    "\u201c": '"', "\u201d": '"', "\u201f": '"',
    "\u2018": "'", "\u2019": "'", "\u201b": "'",
    "\u2032": "'", "\u2033": '"',
    # 短划 / 长划 / 数学减号 / 不换行连字符 → ASCII 连字符
    "\u2010": "-", "\u2011": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    # 省略号
    "\u2026": "...",
}
_WS_RE = re.compile(r"\s+")


def normalize_anchor(text: str, *, casefold: bool = True) -> str:
    """把锚定文本归一到可比较形态；casefold 默认开启（大小写视为等价）。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    for source, target in _CHAR_MAP.items():
        value = value.replace(source, target)
    value = _WS_RE.sub(" ", value).strip()
    return value.casefold() if casefold else value
