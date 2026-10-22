"""装配层数据契约：块身份、归组键、差集校验与装配完整性。

背景：项目里存在两套 build 实现，对"块身份"使用了不同字段——
- ``utils/polish_engine.build_jobs``：``chunk_id`` 即块身份（c001..cNNN），无 ``parent_chunk_id``；
- ``skills/batch-processor._build_one``：``chunk_id`` 为 chunk 内子块编号，
  真实章节身份在 ``parent_chunk_id``（该处 L180-184 注释已说明"双层标识"的设计意图）。

由此产生两类历史静默事故：
1. ``apply_responses`` 早期只按 ``chunk_id`` 归组，batch-processor 作业包
   （``chunk_id`` 恒为 c001）被压成 1 块、15 块内容静默消失；
2. 归组键改为只取 ``parent_chunk_id`` 后，同一章节的多个子块又会被合并成一组、
   只取一个响应，其余子块润色结果丢失（长章节超出一个 chunk 上限时必然触发）。

因此装配单元必须是 **(parent_chunk_id, chunk_id) 复合身份**：
- 无 parent（build_jobs 直产）→ 键为 ``chunk_id``；
- 有 parent（batch-processor）→ 键为 ``parent/chunk``，既区分章节又保留子块。
同一键下的多个作业视为同一块的多次尝试（attempt），由 select_best_response 择优。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from utils.token_counter import split_into_chunks

_CHUNK_KEY_RE = re.compile(r"^c(\d+)$", re.IGNORECASE)
_UNPARSEABLE = 10**9


def normalize_job_key(job: dict[str, Any]) -> str:
    """作业所属装配单元的键：``(parent_chunk_id, chunk_id)`` 复合身份。

    复合而非二选一，是为了同时满足两种作业包格式且不合并同章节的多个子块。
    空键返回空字符串，由调用方判定为错误。
    """
    parent = str(job.get("parent_chunk_id") or "").strip()
    sub = str(job.get("chunk_id") or "").strip()
    if parent and sub:
        return f"{parent}/{sub}"
    return parent or sub


def chunk_sort_key(key: str) -> tuple[int, ...]:
    """块排序键：按各段数字后缀排序。

    直接 ``sorted(keys)`` 是字符串排序，``c10`` 会排在 ``c2`` 之前；
    编号位数不一致（如 ``c9`` 与 ``c010`` 混用）时同样会错序。
    复合键 ``c002/c010`` 按 (章, 子块) 两级数值排序。
    """
    parts: list[int] = []
    for segment in str(key).split("/"):
        match = _CHUNK_KEY_RE.match(segment.strip())
        parts.append(int(match.group(1)) if match else _UNPARSEABLE)
    return tuple(parts) if parts else (_UNPARSEABLE,)


def expected_chunk_map(text: str, chunk_limit: int) -> dict[str, str]:
    """按与 ``build_jobs`` 完全相同的切分规则，推导应有的块标识 → 块内容。

    仅对 ``build_jobs`` 直产的作业包（无 ``parent_chunk_id``）适用；
    带 parent 的作业包其期望集合由解析器章节决定，调用方应自行传入。
    """
    chunks = split_into_chunks(text, chunk_limit)
    return {f"c{i:03d}": chunk for i, chunk in enumerate(chunks, start=1)}


@dataclass(frozen=True)
class ChunkRef:
    """一个装配单元的最小描述：章节身份、子块身份与内容。

    这是全项目第一个跨 Skill 的强类型数据契约——此前 chunk 结构全靠裸 dict
    键名约定（``dict[str, Any]`` 全项目 414 处），改键名无静态报错。
    """

    parent_chunk_id: str
    chunk_id: str
    content: str

    @property
    def key(self) -> str:
        if self.parent_chunk_id and self.chunk_id:
            return f"{self.parent_chunk_id}/{self.chunk_id}"
        return self.parent_chunk_id or self.chunk_id

    @property
    def sort_key(self) -> tuple[int, ...]:
        return chunk_sort_key(self.key)


def to_chunk_refs(jobs: Iterable[dict[str, Any]]) -> list[ChunkRef]:
    """把作业列表转换为装配单元描述（保持输入顺序）。"""
    return [
        ChunkRef(
            parent_chunk_id=str(job.get("parent_chunk_id") or "").strip(),
            chunk_id=str(job.get("chunk_id") or "").strip(),
            content=str(job.get("chunk_content") or ""),
        )
        for job in jobs
    ]


def validate_job_set(jobs: Sequence[dict[str, Any]]) -> list[str]:
    """作业包内部一致性校验，返回**错误级**问题列表（空列表 = 通过）。

    只依据作业包自身，不引入外部假设，因此对两种作业包格式都不会误报：
    - 块标识为空；
    - 同一键下 chunk_content 不一致（不同内容被压到同一身份，历史静默丢块的症状）。
    """
    problems: list[str] = []
    contents: dict[str, str] = {}
    counts: dict[str, int] = {}
    for job in jobs:
        key = normalize_job_key(job)
        if not key:
            problems.append(
                f"作业 {job.get('job_id', '?')} 缺少块标识（parent_chunk_id 与 chunk_id 均为空）"
            )
            continue
        counts[key] = counts.get(key, 0) + 1
        content = str(job.get("chunk_content") or "")
        if key in contents and contents[key] != content:
            problems.append(
                f"块 {key} 被 {counts[key]} 个作业引用，但 chunk_content 不一致，"
                "疑似不同内容被压到同一块身份（历史静默丢块缺陷的症状）"
            )
        contents.setdefault(key, content)
    return problems


def diff_against_expected(
    actual_keys: Iterable[str],
    expected_keys: Iterable[str],
) -> tuple[list[str], list[str]]:
    """作业包块集合与期望块集合的差集，返回 ``(missing, unexpected)``。

    两者都按块排序键有序返回，便于稳定地补原文与生成告警文案。
    """
    actual = set(actual_keys)
    expected = list(expected_keys)
    missing = [key for key in expected if key not in actual]
    unexpected = sorted(actual - set(expected), key=chunk_sort_key)
    return missing, unexpected


def verify_assembly(
    parts: list[str],
    keys: Sequence[str],
    *,
    source_text: str,
    polished_text: str,
) -> tuple[list[str], list[str]]:
    """装配后完整性校验，返回 ``(errors, warnings)``。

    errors 为必须中断的硬失败；warnings 为需要人工留意的信号。
    """
    errors: list[str] = []
    warnings: list[str] = []

    if len(parts) != len(keys):
        errors.append(f"装配段数（{len(parts)}）与块数（{len(keys)}）不一致")
    if len(set(keys)) != len(keys):
        errors.append("块标识存在重复，装配结果可能丢块或重块")
    empty = [index for index, part in enumerate(parts) if not part.strip()]
    if empty:
        errors.append(f"以下段装配结果为空: {empty}")

    source_len = len(source_text.strip())
    output_len = len(polished_text.strip())
    if source_len and output_len < source_len * 0.5:
        warnings.append(
            f"终稿长度（{output_len}）不足原文（{source_len}）的一半，疑似装配截断，请人工核对"
        )
    return errors, warnings
