"""纯离线的润色作业（job）读写与模型输出后处理工具。

本项目不发起任何网络请求：LLM 交互由coding harness 完成。
本模块负责两端的纯本地处理：
- build 端：prompt 作业包的构造与落盘格式；
- apply 端：coding harness 返回文本的清洗（去代码围栏/前缀）、
  Responses 风格原始 JSON 的文本抽取、截断检测、usage 归一与成本估算。

所有函数均为纯函数，不接触文件系统以外的任何 I/O。
"""

from __future__ import annotations

import json
import re
from typing import Any

from utils.config_loader import get_model_config

JOB_SCHEMA_VERSION = "1.0"

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?|```\s*$", re.MULTILINE)
_LEADING_LABEL_RE = re.compile(r"^\s*(?:润色后|改写后|Polished(?: text)?| Revised(?: text)?| Output)\s*[:：]\s*", re.IGNORECASE)


def new_job(
    *,
    job_id: str,
    chunk_id: str,
    skill: str,
    language: str,
    strength: str,
    domain: str,
    section_type: str,
    system_prompt: str,
    prompt: str,
    suggested_model: str,
    chunk_content: str,
    attempt: int = 1,
    feedback: str = "",
    comments: str = "",
    brief: str = "",
) -> dict[str, Any]:
    """构造一个待coding harness 执行的 prompt 作业。

    comments / brief 为渲染后的批注区块与文档级简报文本；
    随作业落盘，apply 阶段生成重试作业时据此还原同款上下文。
    """
    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "chunk_id": chunk_id,
        "attempt": attempt,
        "feedback": feedback,
        "comments": comments,
        "brief": brief,
        "skill": skill,
        "language": language,
        "strength": strength,
        "domain": domain,
        "section_type": section_type,
        "suggested_model": suggested_model,
        "system_prompt": system_prompt,
        "prompt": prompt,
        "chunk_content": chunk_content,
        "status": "pending",
        "response": "",
        "usage": {},
        "incomplete": False,
        "incomplete_reason": "",
        "warnings": [],
    }


def job_is_ready(job: dict[str, Any]) -> bool:
    return bool(str(job.get("response", "")).strip())


def clean_model_output(raw: str) -> str:
    """清洗coding harness 回填的响应文本：去 Markdown 围栏与“润色后：”式前缀。"""
    if not raw:
        return ""
    text = raw.strip()
    text = _FENCE_RE.sub("", text)
    text = _LEADING_LABEL_RE.sub("", text, count=1)
    # 常见的多余结尾围栏已由 _FENCE_RE 处理；再收紧首尾空白
    return text.strip()


def extract_output_text(body: Any) -> str:
    """从 Responses 风格的原始 JSON 中抽取文本（coding harness 可直接粘贴原始返回）。

    跳过 type == "reasoning" 的输出项，避免思考内容污染润色正文。
    """
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    for key in ("output_text", "text"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    parts: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            parts.append(value)

    for item in body.get("output") or []:
        if isinstance(item, str):
            add(item)
            continue
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).lower() == "reasoning":
            continue
        add(item.get("text"))
        add(item.get("output_text"))
        for content in item.get("content") or []:
            if isinstance(content, str):
                add(content)
            elif isinstance(content, dict):
                if str(content.get("type", "")).lower() == "reasoning":
                    continue
                add(content.get("text"))
                add(content.get("output_text"))
                add(content.get("transcript"))
    return "\n".join(parts).strip()


def detect_incomplete(body: dict[str, Any]) -> tuple[bool, str]:
    """检测 Responses 原始返回是否因 token 上限等原因被截断。"""
    status = str(body.get("status", "") or "").lower()
    details = body.get("incomplete_details") or {}
    reason = ""
    if isinstance(details, dict):
        reason = str(details.get("reason", "") or "")
    if status == "incomplete" or reason:
        return True, reason or status or "incomplete"
    stop = str(body.get("stop_reason", "") or "").lower()
    if stop in ("length", "max_output_tokens", "max_tokens"):
        return True, stop
    return False, ""


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    cached = int(
        usage.get("cached_input_tokens")
        or usage.get("cache_hit_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or 0
    )
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    return {
        "input_tokens": max(0, input_tokens),
        "cached_input_tokens": cached,
        "output_tokens": output_tokens,
        "total_tokens": total,
    }


def estimate_cost_usd(usage: dict[str, int], model_key: str) -> float:
    """按 1M token 单价估算成本（仅当coding harness 回填了 usage 时有意义）。"""
    try:
        model_cfg = get_model_config(model_key)
    except Exception:  # noqa: BLE001 - 未知模型别名时成本置零，不阻塞后处理
        return 0.0
    pricing = model_cfg.get("pricing", {})
    input_price = float(pricing.get("input_per_1m", 0))
    cache_price = float(pricing.get("cache_hit_per_1m", input_price))
    output_price = float(pricing.get("output_per_1m", 0))
    cached = usage.get("cached_input_tokens", 0)
    uncached = max(0, usage.get("input_tokens", 0) - cached)
    cost = uncached * input_price / 1_000_000
    cost += cached * cache_price / 1_000_000
    cost += usage.get("output_tokens", 0) * output_price / 1_000_000
    return round(cost, 6)


def parse_response_field(raw_response: Any) -> tuple[str, dict[str, Any], bool, str]:
    """把作业中被回填的 response 字段规整为 (text, usage, incomplete, reason)。

    支持三种回填形式：
    - 纯字符串：视为已抽取的润色文本；
    - Responses 原始 JSON 字符串：抽取文本与截断状态；
    - dict：同上。
    """
    if raw_response is None:
        return "", {}, False, ""
    if isinstance(raw_response, str):
        stripped = raw_response.strip()
        if stripped.startswith("{"):
            try:
                body = json.loads(stripped)
            except json.JSONDecodeError:
                return clean_model_output(raw_response), {}, False, ""
            if isinstance(body, dict) and ("output" in body or "output_text" in body):
                incomplete, reason = detect_incomplete(body)
                return (
                    clean_model_output(extract_output_text(body)),
                    normalize_usage(body.get("usage")),
                    incomplete,
                    reason,
                )
        return clean_model_output(raw_response), {}, False, ""
    if isinstance(raw_response, dict):
        incomplete, reason = detect_incomplete(raw_response)
        return (
            clean_model_output(extract_output_text(raw_response)),
            normalize_usage(raw_response.get("usage")),
            incomplete,
            reason,
        )
    return str(raw_response), {}, False, ""


def read_jobs(path) -> list[dict[str, Any]]:
    """读取作业文件（JSON 数组或 {"jobs": [...]} 包装）。"""
    from pathlib import Path

    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise ValueError(f"作业文件格式不正确: {path}")
    return jobs


def write_jobs(path, jobs: list[dict[str, Any]], *, meta: dict[str, Any] | None = None) -> None:
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": JOB_SCHEMA_VERSION, "job_count": len(jobs)}
    if meta:
        payload.update(meta)
    payload["jobs"] = jobs
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fill_responses_from_dir(jobs: list[dict[str, Any]], responses_dir) -> int:
    """从目录回填响应：每个 <job_id>.txt / <job_id>.json 为该作业的响应。

    返回成功回填的作业数。txt 视为纯文本，json 视为 Responses 原始返回。
    """
    from pathlib import Path

    responses_dir = Path(responses_dir)
    filled = 0
    for job in jobs:
        job_id = str(job.get("job_id", ""))
        if not job_id or job_is_ready(job):
            continue
        for suffix in (".txt", ".json"):
            candidate = responses_dir / f"{job_id}{suffix}"
            if candidate.exists():
                raw = candidate.read_text(encoding="utf-8-sig")
                job["response"] = raw if suffix == ".json" else raw
                job["status"] = "done"
                filled += 1
                break
    return filled


def select_best_response(jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """同一 chunk 多次尝试时，选最高 attempt 且有响应的作业。"""
    candidates = [job for job in jobs if job_is_ready(job)]
    if not candidates:
        return None
    return max(candidates, key=lambda job: int(job.get("attempt", 1)))
