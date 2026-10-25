"""响应-作业宽容匹配器：job_id 纪律缺口的确定性收口。

背景（跨模型对抗验证实测）：异构执行器 17/17 不回显或改写作业身份字段，
且可能把响应落成任意文件名。严格按 responses/<job_id>.txt 取件会让全部
作业 pending 或 job_id_mismatch 拒收——fail-closed 安全，但闭环不可用。

匹配按内容归因强度分级，任何一级产生歧义即放弃该级（fail-closed）：
1. 文件名精确匹配 <job_id>.<ext>（txt / json / md）；
2. 文件名包含 job_id 子串，且子串只命中一个作业；
3. 响应 JSON 回显身份键（如 role+section_id、claim_id+reference_id）与
   唯一一个未匹配作业对应（重复回显视为歧义）；
4. 内容锚定归因：响应里全部逐字引文经 anchor_norm 归一后只可能来自
   一个未匹配作业的锚定底本（最强内容信号）。

边界：润色作业（en-polish / cn-polish）不接入本匹配器——润色响应没有
逐字锚点，错误归因等于把 A 块改文写进 B 块；utils/job_io.fill_responses_
from_dir 对润色保持严格 job_id。本模块只服务自带逐字锚点的评审类作业
（review-sim / cite-intent），错配最坏结果是意见被锚定门禁拒收。

纯标准库；不发网络请求；不读环境变量。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from utils.anchor_norm import normalize_anchor

_RESPONSE_EXTS = (".txt", ".json", ".md")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _parse_payload(raw: str) -> dict[str, Any] | None:
    """宽容解析响应文件为 JSON 对象；失败返回 None（调用方按 unparsable 记录）。"""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def _iter_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """提取响应中的意见条目（兼容 findings 数组与单对象两种形态）。"""
    items = payload.get("findings")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def _identity_key(payload: dict[str, Any], identity_fields: list[str]) -> tuple[str, ...]:
    return tuple(str(payload.get(field) or "").strip() for field in identity_fields)


def _quotes(payload: dict[str, Any], quote_field: str) -> list[str]:
    """提取响应中的逐字引文：findings 数组内的字段 + 顶层同名字段
    （cite-intent 的 verbatim_evidence_sentence 位于顶层）。"""
    values = [str(item.get(quote_field) or "") for item in _iter_findings(payload)]
    values.append(str(payload.get(quote_field) or ""))
    return [normalize_anchor(q) for q in values if normalize_anchor(q)]


def match_files(
    jobs: list[dict[str, Any]],
    responses_dir: str | Path,
    *,
    identity_fields: list[str] | None = None,
    anchor_field: str = "",
    quote_field: str = "",
) -> dict[str, Any]:
    """把响应目录里的文件匹配到作业；返回 (assignments, unmatched_files, warnings)。

    jobs 每项须含 job_id；identity_fields / anchor_field + quote_field 提供时
    启用第 3/4 级匹配。已带 response 的作业跳过（不参与匹配）。
    """
    root = Path(responses_dir)
    assignments: dict[str, str] = {}
    unmatched: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not root.exists():
        return {"assignments": assignments, "unmatched_files": unmatched, "warnings": warnings}

    files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in _RESPONSE_EXTS)
    pending_jobs = [
        job for job in jobs
        if str(job.get("job_id") or "") and not str(job.get("response") or "").strip()
    ]
    job_ids = {str(job.get("job_id") or "") for job in pending_jobs}

    def assign(job_id: str, path: Path, how: str) -> None:
        assignments[job_id] = str(path)
        warnings.append(f"matched {path.name} -> {job_id} ({how})")

    # 第 1 级：文件名精确匹配
    claimed: set[Path] = set()
    for job in pending_jobs:
        job_id = str(job["job_id"])
        for ext in _RESPONSE_EXTS:
            candidate = root / f"{job_id}{ext}"
            if candidate.exists():
                assign(job_id, candidate, "exact_filename")
                claimed.add(candidate)
                break

    remaining_files = [p for p in files if p not in claimed]
    remaining_jobs = [job for job in pending_jobs if str(job["job_id"]) not in assignments]
    if not remaining_files or not remaining_jobs:
        unmatched = [{"file": str(p.name), "reason": "no_matching_job"} for p in remaining_files]
        return {"assignments": assignments, "unmatched_files": unmatched, "warnings": warnings}

    # 第 2 级：文件名包含 job_id 子串（全目录内唯一命中才接受）
    for path in list(remaining_files):
        if path in claimed:
            continue
        hits = [j for j in remaining_jobs if str(j["job_id"]) in path.name]
        if len(hits) == 1:
            assign(str(hits[0]["job_id"]), path, "filename_substring")
            claimed.add(path)
    remaining_files = [p for p in remaining_files if p not in claimed]
    remaining_jobs = [job for job in remaining_jobs if str(job["job_id"]) not in assignments]
    if not remaining_files or not remaining_jobs:
        unmatched = [{"file": str(p.name), "reason": "no_matching_job"} for p in remaining_files]
        return {"assignments": assignments, "unmatched_files": unmatched, "warnings": warnings}

    parsed: dict[Path, dict[str, Any] | None] = {}
    for path in remaining_files:
        try:
            parsed[path] = _parse_payload(path.read_text(encoding="utf-8-sig"))
        except OSError:
            parsed[path] = None

    # 第 3 级：身份键回显唯一对应（重复回显 = 歧义，放弃）
    if identity_fields:
        echo_groups: dict[tuple[str, ...], list[Path]] = {}
        for path, payload in parsed.items():
            if payload is None:
                continue
            key = _identity_key(payload, identity_fields)
            if any(key):
                echo_groups.setdefault(key, []).append(path)
        unambiguous = {key: paths[0] for key, paths in echo_groups.items() if len(paths) == 1}
        for job in list(remaining_jobs):
            key = tuple(str(job.get(field) or "").strip() for field in identity_fields)
            if not any(key) or key not in unambiguous:
                continue
            path = unambiguous[key]
            if path in claimed:
                continue
            assign(str(job["job_id"]), path, "identity_echo")
            claimed.add(path)
        remaining_files = [p for p in remaining_files if p not in claimed]
        remaining_jobs = [job for job in remaining_jobs if str(job["job_id"]) not in assignments]

    # 第 4 级：内容锚定归因——全部引文只可能来自一个未匹配作业
    if anchor_field and quote_field:
        for path in remaining_files:
            if path in claimed:
                continue
            payload = parsed.get(path)
            if payload is None:
                unmatched.append({"file": path.name, "reason": "unparsable_or_not_json"})
                continue
            quotes = _quotes(payload, quote_field)
            if not quotes:
                unmatched.append({"file": path.name, "reason": "no_quotes_to_anchor"})
                continue
            open_jobs = [job for job in remaining_jobs if str(job["job_id"]) not in assignments]
            hits: list[str] = []
            for job in open_jobs:
                haystack = normalize_anchor(str(job.get(anchor_field) or ""))
                if haystack and all(q in haystack for q in quotes):
                    hits.append(str(job["job_id"]))
            if len(hits) == 1:
                assign(hits[0], path, "content_anchor")
                claimed.add(path)
            elif len(hits) > 1:
                unmatched.append({"file": path.name, "reason": "ambiguous_content_anchor"})
            else:
                unmatched.append({"file": path.name, "reason": "no_anchor_match"})
        remaining_files = [p for p in remaining_files if p not in claimed]
    for path in remaining_files:
        if not any(item["file"] == path.name for item in unmatched):
            unmatched.append({"file": path.name, "reason": "no_matching_job"})
    return {"assignments": assignments, "unmatched_files": unmatched, "warnings": warnings}


def identity_matches(
    payload: dict[str, Any],
    job: dict[str, Any],
    identity_fields: list[str],
) -> bool:
    """回显身份键是否与作业一致（宽容 job_id 时的最终身份凭据）。"""
    return _identity_key(payload, identity_fields) == tuple(
        str(job.get(field) or "").strip() for field in identity_fields
    )
