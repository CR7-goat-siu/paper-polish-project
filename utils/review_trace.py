"""Append-only review trace 存储（reviewer 身份与证据链 F5）。

每次 reviewer 调用产生一个不可变 trace：
    output/<run>/review-traces/<trace_id>/
        request.json   完整 prompt、job_id、purpose、artifact paths、hash、scope
        response.md    完整原始响应（不截断、不摘要）
        meta.json      thread_id / 模型身份 / 完成度 / verdict / 时间戳 / 独立性

并维护 append-only 事件账本 `review-events.jsonl`：每轮追加一条摘要事件，
携带 prev_event_sha256 / event_sha256 形成可验证 hash chain；禁止覆盖、
截断或删除历史行。

trace_id 由 run_id / job_id / round / artifact_sha256 / thread_id 五元组
确定性派生（sha1 前 16 位），天然绑定这五个维度——跨 run 复用 trace
（run_id 不同）会被 verify_trace 拒绝。

完整性模型：meta.json 内嵌 request_sha256 / response_sha256；
verify_trace 重算两个文件哈希比对 meta，并校验 meta 自身的身份字段
与调用方期望一致；账本验签（verify_chain）确认事件链未被篡改或截断。
任何不一致 → fail-closed（返回 invalid，由调用方降级 staging）。

纯标准库；无网络。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENTS_FILENAME = "review-events.jsonl"
TRACES_DIRNAME = "review-traces"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """文本哈希（换行归一）：CRLF/CR 统一为 LF 后再哈希。

    Windows 下 read_text/write_text 的换行翻译会让同一份内容的内存形态与
    文件字节哈希不一致；评审证据绑定的是内容而非换行形态，统一归一。"""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def derive_trace_id(
    *, run_id: str, job_id: str, round_index: int, artifact_sha256: str, thread_id: str
) -> str:
    """trace_id 绑定 run / job / 轮次 / artifact 哈希 / thread 五元组。"""
    basis = "|".join(
        str(part)
        for part in (run_id, job_id, round_index, artifact_sha256, thread_id)
    )
    return "trace-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def create_trace(
    run_dir: str | Path,
    *,
    run_id: str,
    job_id: str,
    purpose: str,
    round_index: int,
    artifact_sha256: str,
    thread_id: str,
    request: dict[str, Any],
    response_text: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """写入一个不可变 trace 并追加事件；返回 {trace_id, dir, event}。

    trace 目录已存在（同五元组重复调用）→ 幂等复用原目录，但原始响应文件
    永不覆盖：内容一致则通过，不一致则报 conflict（调用方 fail-closed）。
    """
    root = Path(run_dir)
    trace_id = derive_trace_id(
        run_id=run_id,
        job_id=job_id,
        round_index=round_index,
        artifact_sha256=artifact_sha256,
        thread_id=thread_id,
    )
    trace_dir = root / TRACES_DIRNAME / trace_id
    request_bytes = _canonical(request).encode("utf-8")
    response_bytes = str(response_text or "").encode("utf-8")

    if trace_dir.exists():
        existing_request = (trace_dir / "request.json").read_bytes()
        existing_response = (trace_dir / "response.md").read_bytes()
        if existing_request != request_bytes or existing_response != response_bytes:
            return {
                "trace_id": trace_id,
                "dir": str(trace_dir),
                "status": "conflict",
                "reason": "同 trace_id 的原始内容不一致（疑似篡改或碰撞）",
            }
        status = "exists"
    else:
        trace_dir.mkdir(parents=True, exist_ok=True)
        (trace_dir / "request.json").write_bytes(request_bytes)
        (trace_dir / "response.md").write_bytes(response_bytes)
        meta_full = {
            **meta,
            "trace_id": trace_id,
            "run_id": run_id,
            "job_id": job_id,
            "purpose": purpose,
            "round": round_index,
            "artifact_sha256": artifact_sha256,
            "thread_id": thread_id,
            "request_sha256": _sha256_bytes(request_bytes),
            "response_sha256": _sha256_bytes(response_bytes),
            "timestamp": str(meta.get("timestamp") or _now()),
        }
        (trace_dir / "meta.json").write_text(
            _canonical(meta_full), encoding="utf-8"
        )
        status = "created"

    event = append_event(
        run_dir,
        {
            "kind": "review_call",
            "trace_id": trace_id,
            "job_id": job_id,
            "purpose": purpose,
            "round": round_index,
            "artifact_sha256": artifact_sha256,
            "thread_id": thread_id,
            "meta_sha256": _sha256_bytes((trace_dir / "meta.json").read_bytes()),
            "response_sha256": _sha256_bytes(response_bytes),
            "status": status,
        },
    )
    return {
        "trace_id": trace_id,
        "dir": str(trace_dir),
        "status": status,
        "event": event,
    }


def append_event(run_dir: str | Path, event: dict[str, Any]) -> dict[str, Any]:
    """追加一条事件到 review-events.jsonl，形成 hash chain。

    event_sha256 = sha256(canonical(event 去掉 event_sha256 后) + prev_event_sha256)。
    只追加：任何覆盖/截断/删除历史的尝试都会被 verify_chain 发现。
    """
    root = Path(run_dir)
    events_path = root / EVENTS_FILENAME
    prev = ""
    if events_path.exists():
        lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if lines:
            try:
                prev = str(json.loads(lines[-1]).get("event_sha256") or "")
            except json.JSONDecodeError:
                prev = ""
    body = {**event, "ts": str(event.get("ts") or _now()), "prev_event_sha256": prev}
    digest = _sha256_bytes((_canonical(body) + prev).encode("utf-8"))
    line = _canonical({**body, "event_sha256": digest})
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return {"event_sha256": digest, "prev_event_sha256": prev}


def verify_chain(run_dir: str | Path) -> dict[str, Any]:
    """验签 review-events.jsonl：逐行重算 hash，检测篡改 / 截断 / 删行。"""
    path = Path(run_dir) / EVENTS_FILENAME
    if not path.exists():
        return {"ok": True, "events": 0, "reason": ""}
    prev = ""
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"ok": False, "events": count, "reason": f"第 {count + 1} 行非法 JSON: {exc}"}
        claimed = str(event.get("event_sha256") or "")
        prev_claimed = str(event.get("prev_event_sha256") or "")
        body = {k: v for k, v in event.items() if k != "event_sha256"}
        digest = _sha256_bytes((_canonical(body) + prev).encode("utf-8"))
        if claimed != digest:
            return {"ok": False, "events": count, "reason": f"第 {count + 1} 行 event_sha256 不匹配（链断裂或被篡改）"}
        if prev_claimed != prev:
            return {"ok": False, "events": count, "reason": f"第 {count + 1} 行 prev_event_sha256 不匹配（历史被删改）"}
        prev = claimed
        count += 1
    return {"ok": True, "events": count, "reason": ""}


def verify_trace(
    run_dir: str | Path,
    trace_id: str,
    *,
    run_id: str | None = None,
    expected_job_id: str | None = None,
    expected_artifact_sha256: str | None = None,
    expected_thread_id: str | None = None,
    expected_identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    """验签一个 trace：文件齐全、原始响应未变、身份一致、跨 run 复用拒绝。"""
    trace_dir = Path(run_dir) / TRACES_DIRNAME / str(trace_id or "")
    if not str(trace_id or "").strip():
        return {"ok": False, "reason": "trace_id 缺失"}
    if not trace_dir.is_dir():
        return {"ok": False, "reason": f"trace 目录缺失: {trace_id}"}
    for name in ("request.json", "response.md", "meta.json"):
        if not (trace_dir / name).exists():
            return {"ok": False, "reason": f"trace 文件缺失: {name}"}
    try:
        meta = json.loads((trace_dir / "meta.json").read_text(encoding="utf-8"))
        request = json.loads((trace_dir / "request.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "reason": f"trace JSON 损坏: {exc}"}
    request_bytes = (trace_dir / "request.json").read_bytes()
    response_bytes = (trace_dir / "response.md").read_bytes()
    if meta.get("request_sha256") != _sha256_bytes(request_bytes):
        return {"ok": False, "reason": "request.json 与 meta 记录的哈希不一致（被篡改）"}
    if meta.get("response_sha256") != _sha256_bytes(response_bytes):
        return {"ok": False, "reason": "response.md 与 meta 记录的哈希不一致（原始响应被改动）"}
    if run_id is not None and str(meta.get("run_id") or "") != str(run_id):
        return {"ok": False, "reason": f"trace 属于 run {meta.get('run_id')!r}，跨 run 复用被拒绝"}
    if expected_job_id is not None and str(meta.get("job_id") or "") != str(expected_job_id):
        return {"ok": False, "reason": "trace 的 job_id 与期望不一致（移花接木）"}
    if expected_artifact_sha256 is not None and str(meta.get("artifact_sha256") or "") != str(
        expected_artifact_sha256
    ):
        return {"ok": False, "reason": "trace 的 artifact_sha256 与期望不一致"}
    if expected_thread_id is not None and str(meta.get("thread_id") or "") != str(expected_thread_id):
        return {"ok": False, "reason": "trace 的 thread_id 与期望不一致"}
    for key, value in (expected_identity or {}).items():
        if str(meta.get(key) or "") != str(value):
            return {"ok": False, "reason": f"trace 身份字段 {key} 与期望不一致"}
    chain = verify_chain(trace_dir.parent.parent)
    if not chain.get("ok"):
        return {"ok": False, "reason": f"事件链验签失败: {chain.get('reason')}"}
    return {"ok": True, "reason": "", "meta": meta}
