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

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.anchor_norm import normalize_anchor  # noqa: E402
from utils import response_matching  # noqa: E402
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
) -> dict[str, Any]:
    """按 章节 × 角色（+ 可选跨节视图）渲染评审作业包并落盘。"""
    config = config or load_config()
    language = normalize_language(language)
    sections = split_sections(text, language=language, parsed=parsed)
    if not sections:
        raise ValueError("论文为空，无法生成评审作业包")
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


def apply_responses(
    jobs_or_path: dict[str, Any] | str | Path,
    responses_dir: str | Path | None = None,
    *,
    config: dict[str, Any] | None = None,
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
    config = load_config(args.get("config"))
    language = normalize_language(str(args.get("language") or "en"))

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
        )

    if mode == "apply":
        jobs_source = args.get("jobs") or args.get("jobs_file") or args.get("out")
        if jobs_source is None:
            raise ValueError("review-sim apply 需要 jobs / jobs_file")
        return apply_responses(
            jobs_source,
            args.get("responses_dir"),
            config=config,
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
