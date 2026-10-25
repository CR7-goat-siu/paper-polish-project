"""review-sim Skill：模拟同行评审（章节 × 角色的语义级缺陷评审）。

对标 MARG / AgentReview 的多角色评审思想，但边界更硬：
- build：把论文按章节切分 × 评审角色渲染结构化作业包（prompt 落盘），
  coding harness 用模型执行作业并把响应回填到 responses/<job_id>.txt；
- apply：fail-closed 校验回填响应——JSON 结构、作业身份一致性、
  exact_source_quote 必须逐字（空白归一后）存在于对应章节原文；
  锚点伪造、字段缺失、身份不符的意见一律丢弃并记录校验告警；
- register：把通过校验的评审意见归一为 issue-registry 可消费的 findings，
  硬编码 confidence=low / source=review-sim / requires_human=True。

红线：评审意见永不自动改写。issue-registry.build_registry 的
enforce_review_sim_guards 与 revision-planner 的 requires_human_sources
（config/issue-rules.yaml）+ 源级断言构成双重硬门禁。

本 Skill 不调用任何模型、不发网络请求（纯标准库）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.anchor_norm import normalize_anchor  # noqa: E402
from utils import model_identity, response_matching, review_trace  # noqa: E402
from utils.config_loader import PROJECT_ROOT, load_yaml_file, normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from utils.prompt_renderer import render_template  # noqa: E402
from utils.sections import classify_section  # noqa: E402
from utils.skill_loader import run_skill  # noqa: E402

logger = get_logger("review-sim")

SCHEMA_VERSION = 1
CONFIG_PATH = PROJECT_ROOT / "config" / "review-sim.yaml"

MARKDOWN_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)[\s、.．]\s*(.{2,80})$"
)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

RESPONSE_FINDING_FIELDS = (
    "issue_category",
    "claim_or_defect_summary",
    "exact_source_quote",
    "line_or_paragraph_locator",
    "rationale",
)

# ---- reviewer 身份契约（F1）与最终放行评审（F2/F3/F4）----
# identity_source 枚举固定：只有 host_session_event / backend_reported 可信
# （可参与自动 release）；caller_declared 最多 provisional；unavailable 永不通过。
IDENTITY_SOURCES = (
    "host_session_event",
    "backend_reported",
    "caller_declared",
    "unavailable",
)
TRUSTED_IDENTITY_SOURCES = ("host_session_event", "backend_reported")
FINAL_VERDICTS = ("acquitted", "rejected")

# final_acquittal 响应必填字段（缺任一 → 无效响应 → acceptance=done）
FINAL_RESPONSE_FIELDS = (
    "job_id",
    "section_id",
    "role",
    "executor_model",
    "reviewer_model",
    "model_family",
    "identity_source",
    "thread_id",
    "reviewed_text_sha256",
    "scope",
    "complete",
    "verdict",
    "score",
    "findings",
)


def _figures_manifest(text: str) -> str:
    """从全文提取图表题注清单（final 评审的允许输入之一）。"""
    lines = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if re.match(r"^(图|表|Figure|Table|Fig\.)\s*\d+", stripped):
            lines.append(stripped[:120])
    return "\n".join(lines) if lines else "（未检测到图表题注）"


def build_final_acquittal_job(
    text: str,
    *,
    language: str = "en",
    title: str = "",
    out_dir: str | Path = "output/review-sim",
    artifact_sha256: str = "",
    previous_thread_ids: list[str] | None = None,
    journal_constraints: str = "",
    source_file: str = "",
    run_id: str = "",
) -> tuple[dict[str, Any], str]:
    """构建 final_acquittal 评审作业（F2/F3）。

    输入白名单：当前全文（含参考文献）、图表清单、期刊约束、artifact hash、
    文件路径。context_policy=fresh_only；previous_thread_ids 供响应线程新鲜度
    校验。返回 (job, prompt_text)。
    """
    language = normalize_language(language)
    artifact_sha256 = artifact_sha256 or review_trace.sha256_text(text)
    char_count = len(text or "")
    job_id = "revsim-final-acquittal"
    required_scope = {
        "mode": "final_full",
        "sections": ["__all__"],
        "includes_references": True,
        "includes_figures": True,
        "char_count": char_count,
    }
    prompt = render_template(
        "review-sim-final.md",
        language,
        job_id=job_id,
        role="final_acquittal",
        section_id="final",
        paper_title=title or "(untitled)",
        journal_constraints=journal_constraints or "（未提供）" if language == "cn" else "(none provided)",
        artifact_sha256=artifact_sha256,
        source_file=source_file or "(in-memory)",
        char_count=char_count,
        section_text=text or "",
        figures_manifest=_figures_manifest(text),
    )
    out_root = Path(out_dir)
    prompts_dir = out_root / "prompts"
    responses_dir = out_root / "responses"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompts_dir / f"{job_id}.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    job = {
        "job_id": job_id,
        "purpose": "final_acquittal",
        "role": "final_acquittal",
        "section_id": "final",
        "section_heading": "Final acquittal review",
        "section_type": "final",
        "section_chars": char_count,
        "section_text": text or "",
        "artifact_sha256": artifact_sha256,
        "char_count": char_count,
        "identity_contract": {
            "required_fields": [
                "executor_model",
                "reviewer_model",
                "model_family",
                "identity_source",
                "thread_id",
            ],
            "identity_source_enum": list(IDENTITY_SOURCES),
            "trusted_identity_sources": list(TRUSTED_IDENTITY_SOURCES),
            "family_derivation": "deterministic-from-model-name",
        },
        "required_response_fields": list(FINAL_RESPONSE_FIELDS),
        "context_policy": "fresh_only",
        "context_id": f"final-{artifact_sha256[:12]}",
        "required_scope": required_scope,
        "previous_thread_ids": list(previous_thread_ids or []),
        "prompt_file": str(prompt_path.resolve()),
        "response_file": str((responses_dir / f"{job_id}.txt").resolve()),
        "status": "pending",
        "response": "",
    }
    return job, prompt


def adjudicate_final_response(
    payload: dict[str, Any] | None,
    *,
    job: dict[str, Any],
    response_text: str,
    run_dir: str | Path,
    run_id: str = "",
    round_index: int = 0,
) -> dict[str, Any]:
    """对 final_acquittal 响应做 fail-closed 验证并产出三态验收（F1/F3/F4）。

    状态语义：done=响应有效但未达 acquittal；provisional=身份可信度不足
    （同族 / caller_declared）；accepted=跨族 + 可信身份 + complete + scope
    final_full + 哈希与当前工件一致 + verdict 正面。任何验证失败都写明
    gate_reason，并仍写 trace（评审尝试本身是证据）。
    """
    job_id = str(job.get("job_id") or "")
    artifact_sha256 = str(job.get("artifact_sha256") or "")
    acceptance = {
        "status": "done",
        "gate_reason": "",
        "job_id": job_id,
        "trace_id": "",
        "executor_model": "",
        "reviewer_model": "",
        "model_family": "",
        "identity_source": "",
        "reviewed_text_sha256": "",
        "complete": False,
        "verdict": "",
        "score": None,
    }

    def _fail(reason: str, *, scope_ok: bool = False, complete: bool = False,
              verdict: str = "", score: Any = None, identity: dict[str, Any] | None = None) -> dict[str, Any]:
        acceptance["gate_reason"] = reason
        acceptance["complete"] = complete
        if verdict:
            acceptance["verdict"] = verdict
        if score is not None:
            acceptance["score"] = score
        for key in ("executor_model", "reviewer_model", "model_family", "identity_source", "reviewed_text_sha256"):
            if identity and identity.get(key):
                acceptance[key] = str(identity[key])
        trace = _write_final_trace(
            run_dir=run_dir,
            run_id=run_id,
            job=job,
            round_index=round_index,
            response_text=response_text,
            payload=payload if isinstance(payload, dict) else {},
            review_completed=complete,
            review_scope_complete=scope_ok,
            identity=identity or {},
        )
        acceptance["trace_id"] = trace.get("trace_id", "")
        return acceptance

    if not isinstance(payload, dict):
        return _fail(f"invalid_json: 响应不是 JSON 对象")
    missing = [f for f in FINAL_RESPONSE_FIELDS if not _field_present(payload, f)]
    if missing:
        return _fail(f"missing_fields: {','.join(missing)}")
    identity = {
        "executor_model": str(payload["executor_model"]).strip(),
        "reviewer_model": str(payload["reviewer_model"]).strip(),
        "model_family": str(payload["model_family"]).strip().lower(),
        "identity_source": str(payload["identity_source"]).strip().lower(),
        "reviewed_text_sha256": str(payload["reviewed_text_sha256"]).strip().lower(),
    }
    acceptance.update(
        {
            "executor_model": identity["executor_model"],
            "reviewer_model": identity["reviewer_model"],
            "model_family": identity["model_family"],
            "identity_source": identity["identity_source"],
            "reviewed_text_sha256": identity["reviewed_text_sha256"],
            "complete": bool(payload.get("complete")),
            "verdict": str(payload.get("verdict") or "").strip().lower(),
            "score": payload.get("score"),
        }
    )
    if identity["identity_source"] not in IDENTITY_SOURCES:
        return _fail(f"illegal_identity_source: {identity['identity_source']!r}", identity=identity)
    derived = model_identity.derive_family(identity["reviewer_model"])
    if identity["model_family"] != derived:
        return _fail(
            f"forged_family: 声明 {identity['model_family']!r} 与推导 {derived!r} 不一致",
            identity=identity,
        )
    thread_id = str(payload.get("thread_id") or "").strip()
    if not thread_id:
        return _fail("missing_thread_id", identity=identity)
    if thread_id in (job.get("previous_thread_ids") or []):
        return _fail("thread_reuse: thread_id 出现在 previous_thread_ids（复用旧上下文）", identity=identity)
    if identity["reviewed_text_sha256"] != str(job.get("artifact_sha256") or "").lower():
        return _fail(
            "hash_mismatch: reviewed_text_sha256 与作业 artifact_sha256 不一致",
            identity=identity,
        )
    scope = payload.get("scope")
    scope_ok = False
    if isinstance(scope, dict):
        expected = job.get("required_scope") or {}
        scope_ok = (
            str(scope.get("mode") or "") == "final_full"
            and list(scope.get("sections") or []) == ["__all__"]
            and scope.get("includes_references") is True
            and scope.get("includes_figures") is True
            and str(scope.get("char_count") or "") == str(expected.get("char_count"))
        )
    if not scope_ok:
        return _fail("scope_incomplete: scope 不是 final_full 全文口径", scope_ok=False,
                     complete=acceptance["complete"], verdict=acceptance["verdict"],
                     score=acceptance["score"], identity=identity)
    if not acceptance["complete"]:
        return _fail("incomplete_review", scope_ok=True, identity=identity)
    if acceptance["verdict"] not in FINAL_VERDICTS:
        return _fail(f"invalid_verdict: {acceptance['verdict']!r}", scope_ok=True,
                     complete=True, identity=identity)
    if acceptance["verdict"] == "rejected":
        return _fail("negative_verdict", scope_ok=True, complete=True,
                     verdict=acceptance["verdict"], score=acceptance["score"], identity=identity)

    # 身份可信度分层（F1）：只有 host_session_event / backend_reported 可信；
    # caller_declared 最多 provisional；unknown 家族永不通过。
    reviewer_family = model_identity.derive_family(identity["reviewer_model"])
    executor_family = model_identity.derive_family(identity["executor_model"])
    acceptance["model_family"] = reviewer_family
    if reviewer_family == "unknown" or executor_family == "unknown":
        return _fail("unknown_identity: 模型名无法唯一推导家族", scope_ok=True, complete=True,
                     verdict=acceptance["verdict"], score=acceptance["score"], identity=identity)
    trace = _write_final_trace(
        run_dir=run_dir,
        run_id=run_id,
        job=job,
        round_index=round_index,
        response_text=response_text,
        payload=payload,
        review_completed=True,
        review_scope_complete=True,
        identity=identity,
    )
    acceptance["trace_id"] = trace["trace_id"]
    if identity["identity_source"] == "caller_declared":
        acceptance["status"] = "provisional"
        acceptance["gate_reason"] = "identity_source=caller_declared 不可信，最多 provisional"
        return acceptance
    if identity["identity_source"] == "unavailable":
        acceptance["status"] = "done"
        acceptance["gate_reason"] = "identity_source=unavailable：身份不可知，不能进入 acquittal"
        return acceptance
    if reviewer_family == executor_family:
        acceptance["status"] = "provisional"
        acceptance["gate_reason"] = "same-family: 评审者与执行者同族，存在同源盲区"
        return acceptance
    acceptance["status"] = "accepted"
    acceptance["gate_reason"] = ""
    return acceptance


def _field_present(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field)
    if field in ("complete",):
        return isinstance(value, bool)
    if field == "scope":
        return isinstance(value, dict)
    if field == "score":
        return isinstance(value, (int, float))
    if field == "findings":
        return isinstance(value, list)
    return bool(str(value or "").strip())


def _write_final_trace(
    *,
    run_dir: str | Path,
    run_id: str,
    job: dict[str, Any],
    round_index: int,
    response_text: str,
    payload: dict[str, Any],
    review_completed: bool,
    review_scope_complete: bool,
    identity: dict[str, Any],
) -> dict[str, Any]:
    request = {
        "job_id": job.get("job_id"),
        "purpose": job.get("purpose") or "final_acquittal",
        "prompt_file": job.get("prompt_file"),
        "prompt_text": Path(str(job.get("prompt_file") or "")).read_text(encoding="utf-8")
        if job.get("prompt_file") and Path(str(job.get("prompt_file"))).exists()
        else "",
        "artifact_paths": {
            "response_file": job.get("response_file"),
            "source_file": job.get("section_heading"),
        },
        "artifact_sha256": job.get("artifact_sha256"),
        "char_count": job.get("char_count"),
        "scope": job.get("required_scope"),
        "context_policy": job.get("context_policy"),
        "previous_thread_ids": job.get("previous_thread_ids"),
    }
    thread_id = str(payload.get("thread_id") or "unknown-thread")
    meta = {
        "thread_id": thread_id,
        "executor_model": identity.get("executor_model", ""),
        "reviewer_model": identity.get("reviewer_model", ""),
        "model_family": model_identity.derive_family(identity.get("reviewer_model", "")),
        "identity_source": identity.get("identity_source", ""),
        "review_completed": review_completed,
        "review_scope_complete": review_scope_complete,
        "score": payload.get("score"),
        "verdict": str(payload.get("verdict") or ""),
        "review_independence": {
            "context_policy": job.get("context_policy"),
            "thread_reused": thread_id in (job.get("previous_thread_ids") or []),
            "previous_thread_count": len(job.get("previous_thread_ids") or []),
        },
    }
    return review_trace.create_trace(
        run_dir,
        run_id=run_id or "review-sim",
        job_id=str(job.get("job_id") or ""),
        purpose=str(job.get("purpose") or "final_acquittal"),
        round_index=round_index,
        artifact_sha256=str(job.get("artifact_sha256") or ""),
        thread_id=thread_id,
        request=request,
        response_text=response_text,
        meta=meta,
    )


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else CONFIG_PATH
    return load_yaml_file(target) if target.exists() else {}


# --------------------------------------------------------------------------- #
# 章节切分
# --------------------------------------------------------------------------- #


def _sanitize_section_key(heading: str) -> str:
    key = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "-", heading.strip()).strip("-").lower()
    return key[:48] or "section"


def split_sections(
    text: str, *, language: str = "en", parsed: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """把论文文本切分为带类型标注的章节块（确定性，纯文本启发式）。

    parsed（paper-parser 产物）存在时优先用 chunks 分组；否则按
    Markdown / 数字标题切分。标题行本身计入该章文本首行，供锚点匹配。
    """
    sections: list[dict[str, Any]] = []
    if parsed and isinstance(parsed.get("chunks"), list):
        for chunk in parsed["chunks"]:
            if not isinstance(chunk, dict):
                continue
            content = str(chunk.get("content") or "").strip()
            if not content:
                continue
            heading = str(chunk.get("heading") or "").strip() or "Unheaded section"
            sections.append(
                {
                    "heading": heading,
                    "section_type": classify_section(heading, language),
                    "text": content,
                }
            )
        if sections:
            return _finalize_sections(sections, language)

    current_heading = ""
    buffer: list[str] = []

    def _flush() -> None:
        content = "\n".join(buffer).strip()
        if content:
            heading = current_heading or "Front matter"
            sections.append(
                {
                    "heading": heading,
                    "section_type": classify_section(heading, language),
                    "text": content,
                }
            )
        buffer.clear()

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        heading_match = MARKDOWN_HEADING_RE.match(line)
        numbered = NUMBERED_HEADING_RE.match(line)
        if heading_match:
            _flush()
            current_heading = heading_match.group(2).strip()
            buffer.append(line)
            continue
        if numbered and len(line) <= 100 and not line.endswith((".", "。", "!", "！", "?", "？")):
            _flush()
            current_heading = numbered.group(1).strip()
            buffer.append(line)
            continue
        buffer.append(raw_line)
    _flush()
    return _finalize_sections(sections, language)


def _finalize_sections(sections: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    finalized: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for section in sections:
        heading = str(section["heading"])
        base_key = _sanitize_section_key(heading)
        count = seen.get(base_key, 0)
        seen[base_key] = count + 1
        section_id = base_key if count == 0 else f"{base_key}-{count + 1}"
        section_type = str(section.get("section_type") or classify_section(heading, language))
        finalized.append(
            {
                "section_id": section_id,
                "heading": heading,
                "section_type": section_type or "other",
                "text": str(section["text"]),
            }
        )
    return finalized


def _build_outline(sections: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {section['heading']} ({section['section_type']})" for section in sections)


# --------------------------------------------------------------------------- #
# build：作业包渲染
# --------------------------------------------------------------------------- #


def _sections_for_role(config: dict[str, Any], role: str) -> list[str]:
    roles = config.get("roles") or {}
    role_cfg = roles.get(role) or {}
    raw = role_cfg.get("sections") or []
    if isinstance(raw, str):
        # 兼容极简 YAML 解析器把行内列表解析成字符串的情况
        raw = [item.strip() for item in raw.strip("[]").split(",") if item.strip()]
    return [str(item) for item in raw]


def build_jobs(
    text: str,
    *,
    language: str = "en",
    title: str = "",
    parsed: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    out_dir: str | Path = "output/review-sim",
    only_sections: list[str] | None = None,
) -> dict[str, Any]:
    """按 章节 × 角色（+ 可选跨节视图）渲染评审作业包并落盘。

    only_sections：只给这些 section_id 出作业（对抗闭环按改动章节评审）；
    跨节视图不受其限制（全文交叉比对始终可看全局）。
    """
    config = config or load_config()
    language = normalize_language(language)
    sections = split_sections(text, language=language, parsed=parsed)
    if not sections:
        raise ValueError("论文为空，无法生成评审作业包")
    if only_sections:
        allowed = {str(item) for item in only_sections}
        sections = [section for section in sections if section["section_id"] in allowed]
    outline = _build_outline(sections)
    out_root = Path(out_dir)
    prompts_dir = out_root / "prompts"
    responses_dir = out_root / "responses"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)

    limits = config.get("limits") or {}
    max_findings = int(limits.get("max_findings_per_job", 8))
    cross_cfg = config.get("cross_view") or {}
    cross_enabled = bool(cross_cfg.get("enabled", True))
    cross_cap_section = int(cross_cfg.get("max_chars_per_section", 6000))
    cross_cap_total = int(cross_cfg.get("max_chars_total", 24000))

    jobs: list[dict[str, Any]] = []
    for role in (config.get("roles") or {}):
        template_name = f"review-sim-{role}.md"
        role_sections = _sections_for_role(config, role)
        for section in sections:
            if role_sections and section["section_type"] not in role_sections:
                continue
            job_id = f"revsim-{role}-{section['section_id']}"
            jobs.append(
                _render_job(
                    template_name,
                    job_id=job_id,
                    role=role,
                    section=section,
                    outline=outline,
                    title=title,
                    language=language,
                    max_findings=max_findings,
                    prompts_dir=prompts_dir,
                    responses_dir=responses_dir,
                )
            )
        if cross_enabled:
            cross_text = _assemble_cross_text(sections, cross_cap_section, cross_cap_total)
            section = {
                "section_id": "cross",
                "heading": "Cross-section view" if language == "en" else "全文交叉比对",
                "section_type": "cross",
                "text": cross_text,
            }
            job_id = f"revsim-{role}-cross"
            jobs.append(
                _render_job(
                    template_name,
                    job_id=job_id,
                    role=role,
                    section=section,
                    outline=outline,
                    title=title,
                    language=language,
                    max_findings=max_findings,
                    prompts_dir=prompts_dir,
                    responses_dir=responses_dir,
                )
            )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "language": language,
        "title": title,
        "job_count": len(jobs),
        "jobs": jobs,
        "status": "built" if jobs else "no_jobs",
        "warnings": [
            "评审作业由 coding harness 执行模型；响应回填 responses/<job_id>.txt 后运行 apply 校验。",
            "评审意见仅作低置信度建议登记，永不触发自动改写（requires_human=True）。",
        ],
    }
    (out_root / "review-jobs.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def _render_job(
    template_name: str,
    *,
    job_id: str,
    role: str,
    section: dict[str, Any],
    outline: str,
    title: str,
    language: str,
    max_findings: int,
    prompts_dir: Path,
    responses_dir: Path,
) -> dict[str, Any]:
    prompt = render_template(
        template_name,
        language,
        job_id=job_id,
        role=role,
        section_id=section["section_id"],
        paper_title=title or "(untitled)",
        section_heading=section["heading"],
        section_type=section["section_type"],
        section_text=section["text"],
        outline=outline or ("（无大纲）" if language == "cn" else "(no outline)"),
        max_findings=max_findings,
    )
    prompt_path = prompts_dir / f"{job_id}.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return {
        "job_id": job_id,
        "role": role,
        "section_id": section["section_id"],
        "section_heading": section["heading"],
        "section_type": section["section_type"],
        "section_chars": len(section["text"]),
        # 全文随作业落盘（与润色作业的 chunk_content 同理）：apply 校验
        # exact_source_quote 必须以它为锚定底本
        "section_text": section["text"],
        "prompt_file": str(prompt_path.resolve()),
        "response_file": str((responses_dir / f"{job_id}.txt").resolve()),
        "status": "pending",
        "response": "",
    }


def _assemble_cross_text(
    sections: list[dict[str, Any]], cap_section: int, cap_total: int
) -> str:
    parts: list[str] = []
    total = 0
    for section in sections:
        body = section["text"]
        if len(body) > cap_section:
            body = body[:cap_section] + "…"
        block = f"## {section['heading']}\n{body}"
        if total + len(block) > cap_total:
            remaining = cap_total - total
            if remaining <= 200:
                break
            block = block[:remaining] + "…"
        parts.append(block)
        total += len(block)
        if total >= cap_total:
            break
    return "\n\n".join(parts)


def diff_sections(
    old_text: str, new_text: str, *, language: str = "en"
) -> dict[str, Any]:
    """对比两版文本的章节集合，返回需要评审的 section_id（改动 + 新增）。

    供 optimizer-loop 对抗闭环做「只评审改动章节」的确定性切分；
    section_id 与 build_jobs 完全同源（同一 split_sections），保证可过滤。
    """
    old = {s["section_id"]: s["text"] for s in split_sections(old_text, language=language)}
    new = {s["section_id"]: s["text"] for s in split_sections(new_text, language=language)}
    changed = sorted(sid for sid, text in new.items() if old.get(sid) != text)
    removed = sorted(sid for sid in old if sid not in new)
    return {
        "schema_version": SCHEMA_VERSION,
        "changed": changed,
        "removed": removed,
        "review_sections": changed,
    }


# --------------------------------------------------------------------------- #
# apply：fail-closed 响应校验
# --------------------------------------------------------------------------- #


def normalize_ws(text: str) -> str:
    """空白归一（保持向后兼容的轻量版本；锚定比对统一走 utils.anchor_norm）。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _parse_response_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError("评审响应不是可解析 JSON")
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("评审响应顶层必须是 JSON 对象")
    return payload


def _apply_final(
    payload: dict[str, Any],
    responses_dir: str | Path | None,
    *,
    run_dir: str | Path | None,
    run_id: str,
    round_index: int,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """final_acquittal 消费（F2/F3/F4）：身份契约 + 哈希/scope 校验 + 三态验收。"""
    jobs = [job for job in payload.get("jobs") or [] if isinstance(job, dict)]
    if not jobs:
        raise ValueError("final_acquittal 作业包为空")
    job = jobs[0]
    job_id = str(job.get("job_id") or "")
    raw_response = str(job.get("response") or "")
    response_root = Path(responses_dir) if responses_dir else None
    if not raw_response and response_root is not None:
        assigned = response_matching.match_files(
            jobs, response_root,
            identity_fields=["role", "section_id"],
            anchor_field="section_text",
            quote_field="exact_source_quote",
        )["assignments"].get(job_id)
        if assigned:
            raw_response = Path(assigned).read_text(encoding="utf-8-sig")
    payload_response: dict[str, Any] | None = None
    if raw_response.strip():
        try:
            payload_response = _parse_response_json(raw_response)
        except (ValueError, json.JSONDecodeError):
            payload_response = None
    acceptance = adjudicate_final_response(
        payload_response,
        job=job,
        response_text=raw_response,
        run_dir=run_dir or Path(str(job.get("response_file") or "output/review-sim")).parent.parent,
        run_id=run_id,
        round_index=round_index,
    )
    # final 意见同样逐字锚定校验（意见本身不决定 acceptance，但入证据清单）
    findings: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if isinstance(payload_response, dict):
        haystack = normalize_anchor(str(job.get("section_text") or ""))
        for index, item in enumerate(payload_response.get("findings") or []):
            if not isinstance(item, dict):
                rejected.append({"job_id": job_id, "finding_index": index, "reason": "finding_not_object"})
                continue
            quote = normalize_anchor(str(item.get("exact_source_quote") or ""))
            if len(quote) < 12 or quote not in haystack:
                rejected.append({"job_id": job_id, "finding_index": index, "reason": "fabricated_or_mismatched_quote"})
                continue
            findings.append(
                {
                    "check": normalize_ws(str(item.get("issue_category")))[:80],
                    "found": normalize_ws(str(item.get("claim_or_defect_summary")))[:200],
                    "severity": "warning",
                    "message": normalize_ws(str(item.get("claim_or_defect_summary")))[:300],
                    "context_snippet": quote[:300],
                    "confidence": "low",
                    "requires_human": True,
                    "role": str(job.get("role") or "final_acquittal"),
                    "section_id": str(job.get("section_id") or "final"),
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": "final_acquittal",
        "acceptance": acceptance,
        "trace_id": acceptance.get("trace_id", ""),
        "findings": findings,
        "rejected": rejected,
        "thread_ids": ([str(payload_response.get("thread_id"))] if isinstance(payload_response, dict) and payload_response.get("thread_id") else []),
        "status": "adjudicated",
    }


def apply_responses(
    jobs_or_path: dict[str, Any] | str | Path,
    responses_dir: str | Path | None = None,
    *,
    config: dict[str, Any] | None = None,
    run_dir: str | Path | None = None,
    run_id: str = "",
    round_index: int = 0,
) -> dict[str, Any]:
    """校验回填的评审响应，产出归一化 findings 与被拒清单（fail-closed）。

    校验规则：
    - 响应必须是 JSON 对象，role / section_id 与作业一致；job_id 回显不可靠
      时（异构执行器实测失败模式）降级为 role+section_id 身份凭据，
      二者错必拒，锚定门禁兜底（utils.response_matching）；
    - 每条 finding 必填 RESPONSE_FINDING_FIELDS 全部字段；
    - exact_source_quote 空白归一后必须是指定文本的逐字子串，
      且长度 ≥ min_quote_chars；否则整条丢弃并记录 rejected 原因。
    """
    config = config or load_config()
    if isinstance(jobs_or_path, (str, Path)):
        payload = json.loads(Path(jobs_or_path).read_text(encoding="utf-8"))
    else:
        payload = jobs_or_path
    jobs = [job for job in payload.get("jobs") or [] if isinstance(job, dict)]
    validation = config.get("validation") or {}
    normalize = bool(validation.get("normalize_whitespace", True))
    required_fields = [str(item) for item in validation.get("required_fields") or RESPONSE_FINDING_FIELDS]
    severity_map = dict(validation.get("severity_map") or {"major": "warning", "minor": "info"})
    min_quote = int((config.get("limits") or {}).get("min_quote_chars", 12))

    if str(payload.get("purpose") or "") == "final_acquittal" or (
        jobs and str(jobs[0].get("purpose") or "") == "final_acquittal"
    ):
        return _apply_final(payload, responses_dir, run_dir=run_dir, run_id=run_id,
                            round_index=round_index, config=config)

    response_root = Path(responses_dir) if responses_dir else None
    match_result = {"assignments": {}, "unmatched_files": [], "warnings": []}
    if response_root is not None:
        match_result = response_matching.match_files(
            jobs,
            response_root,
            identity_fields=["role", "section_id"],
            anchor_field="section_text",
            quote_field="exact_source_quote",
        )
    warnings: list[str] = list(match_result["warnings"])
    findings: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    applied = 0
    pending: list[str] = []

    for job in jobs:
        job_id = str(job.get("job_id") or "")
        role = str(job.get("role") or "")
        section_id = str(job.get("section_id") or "")
        section_text = str(job.get("section_text") or "")
        if not section_text:
            # 没有锚定底本就无法核验引用真伪 → fail-closed，整作业拒收
            rejected.append(
                {"job_id": job_id, "finding_index": None, "reason": "section_text_missing"}
            )
            continue
        # 锚定比对统一走字符级等价归一（弯引号/连字/空白/大小写等价；
        # 任何措辞差异仍然拒绝）
        haystack = normalize_anchor(section_text) if normalize else section_text

        raw_response = str(job.get("response") or "")
        if not raw_response and response_root is not None:
            assigned = match_result["assignments"].get(job_id)
            if assigned:
                raw_response = Path(assigned).read_text(encoding="utf-8-sig")
        if not raw_response.strip():
            pending.append(job_id)
            continue

        try:
            result = _parse_response_json(raw_response)
        except (ValueError, json.JSONDecodeError) as exc:
            rejected.append({"job_id": job_id, "finding_index": None, "reason": f"invalid_json: {exc}"})
            continue

        echoed_id = str(result.get("job_id") or "")
        if echoed_id != job_id:
            # job_id 回显不可靠是异构执行器实测失败模式；身份凭据降级为
            # role + section_id 回显一致——二者错必拒，锚定门禁兜底。
            if not response_matching.identity_matches(result, job, ["role", "section_id"]):
                rejected.append({"job_id": job_id, "finding_index": None, "reason": "job_id_mismatch"})
                continue
            warnings.append(f"job_id_relabel_accepted: {job_id} (echoed {echoed_id!r})")
        if str(result.get("section_id") or "") != section_id:
            rejected.append({"job_id": job_id, "finding_index": None, "reason": "section_id_mismatch"})
            continue
        if str(result.get("role") or "") != role:
            rejected.append({"job_id": job_id, "finding_index": None, "reason": "role_mismatch"})
            continue

        seen_quotes: set[str] = set()
        for index, item in enumerate(result.get("findings") or []):
            if not isinstance(item, dict):
                rejected.append({"job_id": job_id, "finding_index": index, "reason": "finding_not_object"})
                continue
            missing = [field for field in required_fields if not str(item.get(field) or "").strip()]
            if missing:
                rejected.append(
                    {
                        "job_id": job_id,
                        "finding_index": index,
                        "reason": f"missing_fields: {','.join(missing)}",
                    }
                )
                continue
            quote = str(item["exact_source_quote"])
            quote_norm = normalize_anchor(quote) if normalize else quote
            if len(quote_norm) < min_quote:
                rejected.append(
                    {"job_id": job_id, "finding_index": index, "reason": "quote_too_short"}
                )
                continue
            if quote_norm not in haystack:
                rejected.append(
                    {
                        "job_id": job_id,
                        "finding_index": index,
                        "reason": "fabricated_or_mismatched_quote",
                        "quote": quote_norm[:120],
                    }
                )
                continue
            dedup_key = (normalize_ws(str(item["issue_category"])).lower(), quote_norm)
            if dedup_key in seen_quotes:
                rejected.append(
                    {"job_id": job_id, "finding_index": index, "reason": "duplicate_finding"}
                )
                continue
            seen_quotes.add(dedup_key)
            severity = severity_map.get(
                str(item.get("severity_suggestion") or "minor").strip().lower(), "info"
            )
            findings.append(
                {
                    "check": normalize_ws(str(item["issue_category"]))[:80],
                    "found": normalize_ws(str(item["claim_or_defect_summary"]))[:200],
                    "severity": severity,
                    "message": normalize_ws(str(item["claim_or_defect_summary"]))[:300],
                    "context_snippet": quote_norm[:300],
                    "suggestion": normalize_ws(str(item.get("suggestion") or ""))[:300],
                    "confidence": "low",
                    "requires_human": True,
                    "role": role,
                    "section_id": section_id,
                    "locator": normalize_ws(str(item["line_or_paragraph_locator"]))[:120],
                    "rationale": normalize_ws(str(item["rationale"]))[:400],
                }
            )
            applied += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "applied_count": applied,
        "rejected_count": len(rejected),
        "pending_count": len(pending),
        "pending_jobs": pending,
        "findings": findings,
        "rejected": rejected,
        "warnings": warnings,
        "unmatched_responses": match_result["unmatched_files"],
        "status": "applied" if applied else ("pending" if pending else "no_valid_findings"),
    }


# --------------------------------------------------------------------------- #
# register：归一进 issue-registry
# --------------------------------------------------------------------------- #


def register_findings(
    apply_result: dict[str, Any],
    *,
    language: str = "cn",
    extra_sources: list[dict[str, Any]] | None = None,
    out: str | Path | None = None,
) -> dict[str, Any]:
    """把通过校验的评审意见喂给 issue-registry，并强制执行 review-sim 守卫。

    issue-registry.build_registry 末尾的 enforce_review_sim_guards 会把所有
    source=review-sim 的问题硬编码为 confidence=low / requires_human /
    needs_human；本函数额外在 source 侧预先打标，双保险。
    """
    review_source = {"skill": "review-sim", "result": {"findings": apply_result.get("findings") or []}}
    sources = list(extra_sources or []) + [review_source]
    registry = run_skill("issue-registry", {"sources": sources, "language": language, "out": out or ""})
    return registry


# --------------------------------------------------------------------------- #
# run() 契约
# --------------------------------------------------------------------------- #


def run(args: dict[str, Any]) -> dict[str, Any]:
    mode = str(args.get("mode") or "build").lower()
    raw_config = args.get("config")
    config = raw_config if isinstance(raw_config, dict) else load_config(raw_config)
    language = normalize_language(str(args.get("language") or "en"))

    if mode == "diff_sections":
        old_text = str(args.get("old_text") or "")
        new_text = str(args.get("new_text") or "")
        if not old_text.strip() or not new_text.strip():
            raise ValueError("review-sim diff_sections 需要 old_text 与 new_text")
        return diff_sections(old_text, new_text, language=language)

    if mode == "build":
        input_file = args.get("input_file") or args.get("input") or args.get("text_file")
        parsed: dict[str, Any] | None = None
        if input_file:
            path = Path(str(input_file))
            if not path.exists():
                raise FileNotFoundError(f"论文文件不存在: {path}")
            if path.suffix.lower() == ".docx":
                parsed = run_skill("paper-parser", {"input": str(path), "format": "auto"})
                text = str(parsed.get("full_text") or "")
            else:
                text = path.read_text(encoding="utf-8-sig")
        else:
            text = str(args.get("text") or "")
        if not text.strip():
            raise ValueError("review-sim build 需要 input_file 或 text")
        return build_jobs(
            text,
            language=language,
            title=str(args.get("title") or ""),
            parsed=parsed,
            config=config,
            out_dir=args.get("out") or "output/review-sim",
            only_sections=args.get("only_sections"),
        )

    if mode == "build_final":
        text = str(args.get("text") or "")
        if not text.strip():
            input_file = args.get("input_file") or args.get("text_file")
            if not input_file:
                raise ValueError("review-sim build_final 需要 text 或 input_file")
            path = Path(str(input_file))
            if path.suffix.lower() == ".docx":
                text = str(run_skill("paper-parser", {"input": str(path), "format": "auto"}).get("full_text") or "")
            else:
                text = path.read_text(encoding="utf-8-sig")
        job, _prompt = build_final_acquittal_job(
            text,
            language=language,
            title=str(args.get("title") or ""),
            out_dir=args.get("out") or "output/review-sim",
            artifact_sha256=str(args.get("artifact_sha256") or ""),
            previous_thread_ids=[str(t) for t in (args.get("previous_thread_ids") or [])],
            journal_constraints=str(args.get("journal_constraints") or ""),
            source_file=str(args.get("source_file") or ""),
        )
        out_root = Path(str(args.get("out") or "output/review-sim"))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "purpose": "final_acquittal",
            "language": language,
            "title": str(args.get("title") or ""),
            "job_count": 1,
            "jobs": [job],
            "status": "built",
            "warnings": [
                "final_acquittal 响应必须携带完整身份契约字段；缺失即无效响应（acceptance=done）。"
            ],
        }
        (out_root / "review-jobs.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return payload

    if mode == "apply":
        jobs_source = args.get("jobs") or args.get("jobs_file") or args.get("out")
        if jobs_source is None:
            raise ValueError("review-sim apply 需要 jobs / jobs_file")
        return apply_responses(
            jobs_source,
            args.get("responses_dir"),
            config=config,
            run_dir=args.get("run_dir"),
            run_id=str(args.get("run_id") or ""),
            round_index=int(args.get("round_index") or 0),
        )

    if mode == "register":
        apply_result = args.get("apply_result")
        if apply_result is None:
            apply_file = args.get("apply_result_file")
            if not apply_file:
                raise ValueError("review-sim register 需要 apply_result 或 apply_result_file")
            apply_result = json.loads(Path(str(apply_file)).read_text(encoding="utf-8"))
        return register_findings(
            apply_result,
            language=language,
            extra_sources=args.get("sources"),
            out=args.get("registry_out") or args.get("out"),
        )

    raise ValueError(f"未知 review-sim mode: {mode!r}，可选 build / apply / register")
