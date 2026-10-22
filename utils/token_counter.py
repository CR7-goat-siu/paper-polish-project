"""Token / 字符估算与文本切块工具。"""

from __future__ import annotations

import math
import re
from typing import Iterable


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x3000 <= cp <= 0x303F
        or 0xFF00 <= cp <= 0xFFEF
    )


def count_characters(text: str) -> int:
    return len(text)


def count_cjk_characters(text: str) -> int:
    return sum(1 for ch in text if _is_cjk(ch))


def estimate_tokens(text: str, language: str | None = None) -> int:
    """启发式估算 token 数。

    中文字符按约 0.75 token/字估算，其余字符按约 4 字符/token 估算。
    该函数仅用于预算控制；真实用量以 Responses API 返回的 usage 为准。
    """
    if not text:
        return 0
    cjk = count_cjk_characters(text)
    other = len(text) - cjk
    return max(1, int(math.ceil(cjk * 0.75 + other / 4.0)))


# 句子切分：中文句末标点（后可不跟空白）与英文句末标点（后须跟空白或结尾）。
# 设计目标：对任意文本，所有匹配段拼接后与原文逐字符一致（无损）。
_SENTENCE_SPLIT_RE = re.compile(
    r"[^。！？!?；;]*[。！？!?；;]+\s*"  # 中文句末（含省略空白）
    r"|[^。！？!?；;]*[.!?]+(?:\s+|\Z)"  # 英文句末（须跟空白或结尾）
    r"|.+\Z",  # 兜底：无句末标点的剩余内容
    re.DOTALL,
)


def split_sentences(text: str) -> list[str]:
    """把文本切成句子片段，拼接结果与原文逐字符一致。"""
    if not text:
        return []
    return [match.group(0) for match in _SENTENCE_SPLIT_RE.finditer(text)]


def _oversized_paragraph_split(paragraph: str, max_tokens: int) -> list[str]:
    sentences = split_sentences(paragraph)
    current: list[str] = []
    chunks: list[str] = []
    current_len = 0
    for sentence in sentences:
        sentence_len = estimate_tokens(sentence)
        if not current or current_len + sentence_len <= max_tokens:
            current.append(sentence)
            current_len += sentence_len
        else:
            if current:
                chunks.append("".join(current))
            # 极长句直接单独成块（避免死循环）
            current = [sentence]
            current_len = sentence_len
    if current:
        chunks.append("".join(current))
    return chunks


def split_into_chunks(text: str, max_tokens: int, language: str | None = None) -> list[str]:
    """按段落切分文本；单个超长段落再按句子切分。

    切分无损：所有分隔符（空行）保留在相邻块内，块内句子拼接不丢字符。
    中文无空格长句同样能被句子级切分（此前会整段绕过上限）。
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens 必须为正数")
    if estimate_tokens(text) <= max_tokens:
        return [text]
    units = re.split(r"(\n\s*\n)", text)  # 奇数位为空行分隔符
    chunks: list[str] = []
    buffer = ""
    buffer_len = 0
    for index, unit in enumerate(units):
        if index % 2 == 1:
            buffer += unit
            continue
        if not unit:
            continue
        unit_len = estimate_tokens(unit)
        if buffer_len + unit_len <= max_tokens:
            buffer += unit
            buffer_len += unit_len
            continue
        if buffer.strip():
            chunks.append(buffer)
        buffer, buffer_len = "", 0
        if unit_len <= max_tokens:
            buffer, buffer_len = unit, unit_len
        else:
            chunks.extend(_oversized_paragraph_split(unit, max_tokens))
    if buffer.strip():
        chunks.append(buffer)
    return [chunk for chunk in chunks if chunk] or ([text] if text else [])


def total_tokens(texts: Iterable[str]) -> int:
    return sum(estimate_tokens(t) for t in texts)


def format_token_usage(usage: dict) -> str:
    return (
        f"input={usage.get('input_tokens', 0)} "
        f"cached={usage.get('cached_input_tokens', 0)} "
        f"output={usage.get('output_tokens', 0)} "
        f"total={usage.get('total_tokens', 0)}"
    )
