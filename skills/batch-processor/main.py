"""batch-processor Skill：批量论文流水线调度（纯离线，build / apply 双阶段）。

本项目不发起任何网络请求；LLM 交互由coding harness 完成。

- phase=build：paper-parser 解析 → 按 chunk 生成润色作业（渲染好的 prompt）→
  写出 jobs.json 与 prompts/ 目录。coding harness 逐作业调用模型，把响应写入
  responses/<job_id>.txt（或直接回填 jobs.json 的 response 字段）。
- phase=apply：读取作业与响应 → 逐 chunk 门禁（失败自动生成带反馈的重试作业，
  由coding harness 再次执行后重跑 apply）→ 装配全文 → claim-guard / term-check /
  format-adapter（仅 references chunk）→ diff 报告 → docx 回写 → result.json。
"""

from __future__ import annotations

import glob
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import chunk_contract, job_io  # noqa: E402
from utils.config_loader import normalize_language  # noqa: E402
from utils.docx_io import write_polished_docx  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from utils.sections import classify_section  # noqa: E402
from utils.skill_loader import run_skill  # noqa: E402

logger = get_logger("batch-processor")

KNOWN_SKILLS = {
    "en-polish",
    "cn-polish",
    "term-check",
    "claim-guard",
    "format-adapter",
    "cite-manager",
    "lit-search",
    "lit-verify",
    "submission-audit",
}
POLISH_SKILLS = {"en-polish", "cn-polish"}
POLISHABLE_KINDS = {"body", "abstract", "appendix", "section"}


def _expand_inputs(args: dict[str, Any]) -> list[Path]:
    candidates: list[str] = []
    if args.get("inputs"):
        raw_inputs = args["inputs"]
        if isinstance(raw_inputs, str):
            candidates.append(raw_inputs)
        else:
            candidates.extend(raw_inputs)
    for key in ("input_glob", "input_dir"):
        value = args.get(key)
        if value:
            candidates.append(str(Path(value) / "**/*" if key == "input_dir" else value))
    if args.get("input_file"):
        candidates.append(str(args["input_file"]))
    paths: list[Path] = []
    for pattern in candidates:
        paths.extend(Path(p) for p in glob.glob(str(pattern), recursive=True))
    unique: dict[str, Path] = {}
    for path in paths:
        if path.is_file():
            unique[str(path.resolve())] = path
    return sorted(unique.values(), key=lambda p: str(p))


def _apply_pipeline_steps(raw: Any, language: str) -> list[str]:
    if isinstance(raw, str):
        steps = [step.strip() for step in raw.split(",") if step.strip()]
    elif isinstance(raw, (list, tuple)):
        steps = list(raw)
    else:
        steps = [f"{language}-polish", "claim-guard", "term-check", "format-adapter"]
    for step in steps:
        if step not in KNOWN_SKILLS:
            raise ValueError(f"未知流水线步骤: {step!r}，可用 {sorted(KNOWN_SKILLS)}")
    return steps


def _load_parsed(result_dir: Path) -> dict[str, Any] | None:
    candidates = [
        result_dir / "step-01-paper-parser" / "parsed.json",
        result_dir / "parsed.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def _polishable_chunks(parsed: dict[str, Any], title: str) -> list[dict[str, Any]]:
    return [
        chunk
        for chunk in parsed.get("chunks", [])
        if chunk.get("kind") in POLISHABLE_KINDS
        and (chunk.get("heading") or "").strip() != title.strip()
    ]


def _references_text(parsed: dict[str, Any]) -> str:
    """references chunk 文本（供文献缺口检测：陈旧引用 / 信息不全）。"""
    return "\n".join(
        str(chunk.get("content") or "") for chunk in parsed.get("chunks", []) if chunk.get("kind") == "references"
    )


def _focus_issue_comments(focus_issues: list[Any] | None) -> list[dict[str, Any]]:
    """把审计问题转成锚定批注（走 polish 模板的 {{comments}} 最高优先级通道）。

    这是「计划驱动修订」的注入点：issue-registry 的问题按证据锚定到
    对应 chunk 后，以审阅人批注形式进入 prompt，与导师批注同通道、
    同优先级（模板无需变更，锚定匹配与重试持久化自动复用）。
    """
    comments: list[dict[str, Any]] = []
    for position, issue in enumerate(focus_issues or [], start=1):
        if not isinstance(issue, dict):
            continue
        # 锚点优先级：optimizer-loop 片段锚定提供的 anchor_text（跨界证据已
        # 降级为章节内片段）> 原始证据；两端的窗口省略号剥掉后再锚定
        anchor = " ".join(
            str(
                issue.get("anchor_text")
                or issue.get("evidence")
                or issue.get("location")
                or ""
            ).split()
        ).strip("…").strip()
        if not anchor:
            continue
        priority = str(issue.get("priority") or "P2")
        dimension = str(issue.get("dimension") or "")
        title = " ".join(str(issue.get("title") or "").split())
        suggestion = " ".join(str(issue.get("suggestion") or "").split())
        text = (
            f"[审计问题 {issue.get('issue_id') or position}][{priority}]"
            f"{f'[{dimension}]' if dimension else ''} {title}"
            + (f"；修改建议：{suggestion}" if suggestion else "")
        )
        comments.append(
            {
                "id": str(issue.get("issue_id") or f"issue-{position:03d}"),
                "author": "Auto-Audit",
                "anchor_text": anchor,
                "text": text,
            }
        )
    return comments


def _splice_polished(original: str, polished: str) -> str:
    """把润色文本接回原 chunk 位置，保留原文首尾空白，避免 chunk 间段落粘连。"""
    if not original.strip():
        return original
    lead = original[: len(original) - len(original.lstrip())]
    trail = original[len(original.rstrip()):]
    return lead + polished.strip() + trail


# --------------------------------------------------------------------------- #
# build 阶段
# --------------------------------------------------------------------------- #


def _build_one(
    file_path: Path,
    *,
    language: str,
    strength: str,
    out_root: Path,
    extra: dict[str, Any],
) -> dict[str, Any]:
    started = time.time()
    result_dir = out_root / file_path.stem
    result_dir.mkdir(parents=True, exist_ok=True)
    is_docx = file_path.suffix.lower() == ".docx"

    text = "" if is_docx else file_path.read_text(encoding="utf-8-sig")
    parsed = run_skill("paper-parser", {"input": str(file_path), "format": "auto"})
    if not parsed.get("integrity", {}).get("reconstructs", True):
        raise ValueError("chunk 重建校验失败")
    text = parsed["full_text"]
    title = str(extra.get("title") or parsed.get("title", ""))
    (result_dir / "parsed.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    chunks = _polishable_chunks(parsed, title)
    doc_comments = parsed.get("comments") or []
    brief = str(extra.get("brief", "") or "")
    jobs: list[dict[str, Any]] = []
    warnings: list[str] = []
    # 计划驱动修订：只对指定章节出作业（局部补丁），并把审计问题作为
    # 最高优先级批注注入对应 chunk 的 prompt。
    only_chunks = {str(c) for c in (extra.get("only_chunks") or [])}
    focus_comments = _focus_issue_comments(extra.get("focus_issues"))
    chunk_strength_map = {
        str(key): str(value)
        for key, value in (extra.get("chunk_strength") or {}).items()
    }
    if focus_comments:
        warnings.append(
            f"计划驱动修订：{len(focus_comments)} 条审计问题将按锚定段落注入 prompt"
        )
    if only_chunks:
        selected = [c for c in chunks if str(c.get("id")) in only_chunks]
        skipped = [str(c.get("id")) for c in chunks if str(c.get("id")) not in only_chunks]
        warnings.append(
            f"选择性构建：仅 {len(selected)}/{len(chunks)} 个章节生成作业"
            f"（依据修改计划，跳过 {skipped}）"
        )
        chunks_for_jobs = selected
    else:
        chunks_for_jobs = chunks
    if doc_comments:
        warnings.append(
            f"源文档含 {len(doc_comments)} 条批注，已按锚定段落注入对应 chunk 的 prompt"
        )
    prev_tail = ""
    for chunk in chunks_for_jobs:
        section_type = classify_section(chunk.get("heading", ""), language)
        build_result = run_skill(
            f"{language}-polish",
            {
                "mode": "build",
                "text": chunk["content"],
                "strength": chunk_strength_map.get(str(chunk.get("id")), strength),
                "domain": extra.get("domain", "auto"),
                "title": title,
                "section_type": section_type,
                "context": prev_tail,
                "comments": doc_comments + focus_comments,
                "brief": brief,
            },
        )
        for job in build_result.get("jobs", []):
            # 双层标识：parent_chunk_id 对齐 parsed chunk；chunk_id 为 chunk 内子块编号。
            # 作业唯一 id 由两者复合，保证跨 chunk 不冲突。
            sub_id = str(job.get("chunk_id", "c001"))
            attempt = int(job.get("attempt", 1))
            job["parent_chunk_id"] = chunk["id"]
            job["job_id"] = f"{chunk['id']}-{sub_id}-a{attempt}"
            jobs.append(job)
        # polish build 的告警（如批注/审计问题未能锚定到本 chunk）必须透出，不能静默丢弃
        for warning in build_result.get("warnings", []) or []:
            warnings.append(f"chunk {chunk['id']}: {warning}")
        prev_tail = chunk["content"][-160:]

    jobs_path = result_dir / "jobs.json"
    job_io.write_jobs(
        jobs_path,
        jobs,
        meta={
            "file": str(file_path),
            "language": language,
            "strength": strength,
            "title": title,
        },
    )
    prompts_dir = result_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    responses_dir = result_dir / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    for job in jobs:
        (prompts_dir / f"{job['job_id']}.prompt.txt").write_text(
            f"=== system ===\n{job['system_prompt']}\n\n=== prompt ===\n{job['prompt']}",
            encoding="utf-8",
        )
    literature_info: dict[str, Any] = {}
    if extra.get("literature"):
        lit_args = dict(extra.get("lit_args") or {})
        lit_args.update(
            {
                "mode": lit_args.get("mode") or extra.get("lit_mode") or "gap",
                "text": text,
                "language": language,
                "domain": extra.get("domain", "auto"),
                "title": title,
                "references": _references_text(parsed),
                "paragraphs": [
                    item.get("text", "") for item in (parsed.get("docx_paragraphs") or [])
                ]
                or None,
                "out": str(result_dir / "literature"),
            }
        )
        try:
            lit_result = run_skill("lit-search", lit_args)
            literature_info = {
                "gap_count": lit_result.get("gap_count", len(lit_result.get("gaps") or [])),
                "gaps_file": (lit_result.get("files") or {}).get("gaps"),
                "candidates_file": (lit_result.get("files") or {}).get("candidates"),
                "candidate_count": lit_result.get("candidate_count"),
                "source_stats": lit_result.get("source_stats"),
            }
            if literature_info["gap_count"]:
                warnings.append(
                    f"检测到 {literature_info['gap_count']} 处缺少文献支撑的论断，"
                    "见 literature/lit-gaps.json（可用 lit-search search 检索候选）"
                )
            if literature_info.get("candidate_count"):
                warnings.append(
                    f"自动检索到 {literature_info['candidate_count']} 条候选文献，"
                    "见 literature/lit-candidates.json 与 lit-cards.md（需 lit-verify 核验后方可插入）"
                )
        except Exception as exc:  # noqa: BLE001 - 文献阶段失败不应阻断润色作业
            logger.exception("文献缺口检测失败 file=%s", file_path.name)
            literature_info = {"error": str(exc)}
            warnings.append(f"文献缺口检测失败: {exc}")

    return {
        "file": str(file_path),
        "status": "built",
        "job_count": len(jobs),
        "chunk_count": len(chunks_for_jobs),
        "total_chunk_count": len(chunks),
        "selected_chunks": [str(c.get("id")) for c in chunks_for_jobs],
        "focus_issue_count": len(focus_comments),
        "literature": literature_info,
        "jobs_file": str(jobs_path),
        "prompts_dir": str(prompts_dir),
        "responses_dir": str(responses_dir),
        "warnings": warnings,
        "elapsed_seconds": round(time.time() - started, 3),
        "result_file": str(result_dir / "build.json"),
    }

def _collect_jobs(result_dir: Path, responses_dir: Path | None) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for name in ("jobs.json", "retry-jobs.json"):
        path = result_dir / name
        if path.exists():
            jobs.extend(job_io.read_jobs(path))
    if responses_dir is not None:
        job_io.fill_responses_from_dir(jobs, responses_dir)
    return jobs


def _run_polish_apply(
    polish_skill: str,
    *,
    parsed: dict[str, Any],
    jobs: list[dict[str, Any]],
    language: str,
    strength: str,
    out_dir: Path,
    extra: dict[str, Any],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """references_text 透传给 review-gate/guardrails：启用引用编号越界检查（防杜撰角标）。"""
    title = str(extra.get("title") or parsed.get("title", ""))
    chunks = _polishable_chunks(parsed, title)

    # 契约校验：本路径按 parent_chunk_id 归组。缺 parent 的作业会被并到同一个空键下，
    # 只有第一个章节生效、其余章节静默丢失；指向不存在章节的作业同样会被无声忽略。
    job_parents = [str(job.get("parent_chunk_id") or "").strip() for job in jobs]
    keyless = sum(1 for parent in job_parents if not parent)
    if keyless:
        raise ValueError(
            f"{keyless} 个作业缺少 parent_chunk_id，无法按章节归组"
            "（请使用 batch-processor build 阶段产出的作业包，"
            "不要直接喂 build_jobs 的作业）"
        )
    expected_ids = [str(chunk.get("id", "")) for chunk in chunks]
    _, unexpected = chunk_contract.diff_against_expected(job_parents, expected_ids)
    if unexpected:
        raise ValueError(
            f"作业指向了不存在的章节 {unexpected}，当前可润色章节为 {expected_ids}"
        )

    by_parent: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        by_parent.setdefault(str(job.get("parent_chunk_id", "")), []).append(job)

    polished_map: dict[str, str] = {}
    chunk_reports: list[dict[str, Any]] = []
    retry_jobs: list[dict[str, Any]] = []
    warnings: list[str] = []
    usage: dict[str, int] = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    prev_tail = ""
    for chunk in chunks:
        chunk_id = str(chunk.get("id", ""))
        section_type = classify_section(chunk.get("heading", ""), language)
        chunk_jobs = by_parent.get(chunk_id, [])
        if not chunk_jobs:
            # build 之后新增的 chunk 或无作业：保留原文并记录
            polished_map[chunk_id] = chunk["content"]
            chunk_reports.append(
                {
                    "chunk": chunk_id,
                    "kind": chunk.get("kind", ""),
                    "heading": chunk.get("heading", ""),
                    "section_type": section_type,
                    "gate_verdict": "no_jobs",
                    "gate_summary": {},
                    "attempts": 0,
                    "similarity_ratio": None,
                }
            )
            warnings.append(f"chunk {chunk_id} 无润色作业，保留原文")
            prev_tail = chunk["content"][-160:]
            continue
        result = run_skill(
            polish_skill,
            {
                "mode": "apply",
                "text": chunk["content"],
                "jobs": chunk_jobs,
                "strength": strength,
                "domain": extra.get("domain", "auto"),
                "title": title,
                "section_type": section_type,
                "context": prev_tail,
                "references": _references_text(parsed),
                "strict": bool(extra.get("strict_guardrails")) or None,
            },
        )
        polished_map[chunk_id] = _splice_polished(
            chunk["content"], str(result.get("polished_text", chunk["content"]))
        )
        for retry in result.get("retry_jobs", []):
            # 重试作业由 polish 子层生成，只有 chunk 内子块 id；此处补齐双层标识，
            # 使其能被下一轮 apply 的 by_parent 归组，且 job_id 全局唯一可回填。
            attempt = int(retry.get("attempt", 2))
            sub_id = str(retry.get("chunk_id", "c001"))
            retry["parent_chunk_id"] = chunk_id
            retry["job_id"] = f"{chunk_id}-{sub_id}-a{attempt}"
            retry_jobs.append(retry)
        for key in usage:
            usage[key] += int((result.get("usage") or {}).get(key, 0) or 0)
        warnings.extend(result.get("warnings", []) or [])
        sub_reports = result.get("chunk_reports", [])
        if sub_reports:
            severity = {"pass": 0, "warn": 1, "no_response": 2, "fail": 3}
            worst = max(
                (str(r.get("gate_verdict", "no_response")) for r in sub_reports),
                key=lambda v: severity.get(v, 3),
            )
            chunk_reports.append(
                {
                    "chunk": chunk_id,
                    "kind": chunk.get("kind", ""),
                    "heading": chunk.get("heading", ""),
                    "section_type": section_type,
                    "gate_verdict": worst,
                    "gate_summary": {
                        "sub_chunks": len(sub_reports),
                        "fails": sum(
                            1 for r in sub_reports if r.get("gate_verdict") == "fail"
                        ),
                    },
                    "attempts": max(int(r.get("attempts", 0) or 0) for r in sub_reports),
                    "similarity_ratio": next(
                        (
                            r.get("similarity_ratio")
                            for r in sub_reports
                            if r.get("similarity_ratio") is not None
                        ),
                        None,
                    ),
                    "sub_reports": sub_reports,
                }
            )
        prev_tail = chunk["content"][-160:]

        diff_path = out_dir / f"{chunk_id}.diff.md"
        try:
            from utils.differ import render_markdown_diff

            diff_path.write_text(
                render_markdown_diff(
                    chunk["content"],
                    polished_map[chunk_id],
                    title=f"chunk {chunk_id} ({chunk.get('heading') or chunk.get('kind', '')})",
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - diff 报告失败不阻塞主流程
            pass

    assembled = "".join(
        polished_map.get(str(chunk.get("id", "")), chunk["content"])
        for chunk in parsed.get("chunks", [])
    )
    # 装配后完整性校验：段数、空段、以及"终稿不足原文一半"的截断告警。
    assembly_errors, assembly_warnings = chunk_contract.verify_assembly(
        [polished_map.get(str(chunk.get("id", "")), "") for chunk in chunks],
        [str(chunk.get("id", "")) for chunk in chunks],
        source_text=str(parsed.get("full_text") or ""),
        polished_text=assembled,
    )
    if assembly_errors:
        raise RuntimeError("batch-processor 装配完整性校验失败：" + "；".join(assembly_errors))
    warnings.extend(assembly_warnings)
    # 选择性构建时，计划外章节的 no_jobs 是预期行为，不计入门禁失败
    only_chunks = {str(c) for c in (extra.get("only_chunks") or [])}

    def _needs_attention(report: dict[str, Any]) -> bool:
        if not only_chunks or str(report.get("chunk")) in only_chunks:
            if report.get("gate_verdict") in ("fail", "no_response", "no_jobs"):
                return True
        # 外科修复的 chunk：终检虽过，但过程曾被门禁拦截，需人工目检保留句
        subs = report.get("sub_reports") or [report]
        return any(str(s.get("fallback") or "") == "surgical" for s in subs if isinstance(s, dict))

    step_result = {
        "skill": polish_skill,
        "mode": "apply",
        "chunk_count": len(chunks),
        "selected_chunks": sorted(only_chunks) if only_chunks else None,
        "polished_text": assembled,
        "chunk_texts": polished_map,
        "chunk_reports": chunk_reports,
        "usage": usage,
        "warnings": warnings,
        "gate_failed_chunks": [r["chunk"] for r in chunk_reports if _needs_attention(r)],
    }
    return assembled, step_result, retry_jobs


def _run_format_adapter_step(
    *,
    text: str,
    parsed: dict[str, Any],
    language: str,
    out_dir: Path,
    extra: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """format-adapter：仅作用于 references chunk（纯本地规则）。"""
    ref_chunks = [c for c in parsed.get("chunks", []) if c.get("kind") == "references"]
    if not ref_chunks:
        return text, {
            "skill": "format-adapter",
            "mode": "local",
            "target_style": extra.get("style", "keep"),
            "text": text,
            "changes": [],
            "warnings": ["未找到 references chunk，format-adapter 跳过"],
        }
    updated_text = text
    polished_map: dict[str, str] = {}
    warnings: list[str] = []
    for chunk in ref_chunks:
        result = run_skill(
            "format-adapter",
            {
                "text": chunk["content"],
                "language": language,
                "style": extra.get("style", "keep"),
                "mode": "local",
            },
        )
        original = str(chunk.get("content") or "")
        formatted = _splice_polished(original, str(result.get("text", original)))
        if formatted != original:
            if updated_text.count(original) == 1:
                updated_text = updated_text.replace(original, formatted, 1)
                polished_map[chunk["id"]] = formatted
            else:
                warnings.append(
                    f"references chunk {chunk['id']} 在当前正文中出现 "
                    f"{updated_text.count(original)} 次，format-adapter 跳过替换"
                )
        warnings.extend(result.get("warnings", []) or [])
    return updated_text, {
        "skill": "format-adapter",
        "mode": "local",
        "target_style": extra.get("style", "keep"),
        "text": updated_text,
        "changes": [f"references chunk {cid} 已按目标样式归一" for cid in polished_map],
        "warnings": warnings,
    }


def _export_docx(
    file_path: Path,
    parsed: dict[str, Any],
    polished_text: str,
    out_dir: Path,
    comments_by_index: dict[int, list[str]] | None = None,
    chunk_texts: dict[str, str] | None = None,
) -> tuple[Path | None, list[str]]:
    """docx 回写：按 chunk 的 paragraph_indexes 对齐润色段落。

    对齐按"非空段落"进行：模型丢弃空段落（纯空行）不影响回写；
    非空段落数不一致时放弃回写，回退纯文本输出。
    comments_by_index：{段落下标: [批注文本]}，写入 Word 审阅批注（L3 HITL 告警）。
    """
    warnings: list[str] = []
    paragraphs = parsed.get("docx_paragraphs") or []
    if not paragraphs:
        return None, ["docx 段落信息缺失，回写跳过"]
    paragraph_text = {p["index"]: p["text"] for p in paragraphs}
    replacements: dict[int, str] = {}
    offset = 0
    for chunk in parsed.get("chunks", []):
        content = chunk["content"]
        if chunk_texts is not None:
            polished_chunk = chunk_texts.get(str(chunk.get("id")), content)
        else:
            polished_chunk = polished_text[offset : offset + len(content)]
        offset += len(content)
        indexes = chunk.get("paragraph_indexes") or []
        if not indexes or chunk.get("kind") not in POLISHABLE_KINDS:
            continue
        pieces = polished_chunk.split("\n\n")
        while pieces and not pieces[-1].strip():
            pieces.pop()
        nonempty_indexes = [i for i in indexes if paragraph_text.get(i, "").strip()]
        # 容忍模型丢弃空段落：非空片段数与 chunk 内非空原文段落数一致即可对齐
        nonempty_pieces = [p for p in pieces if p.strip()]
        if len(nonempty_pieces) == len(nonempty_indexes) and nonempty_pieces:
            for para_index, piece in zip(nonempty_indexes, nonempty_pieces):
                replacements[para_index] = piece
            continue
        if len(pieces) == len(indexes):
            for para_index, piece in zip(indexes, pieces):
                replacements[para_index] = piece
            continue
        warnings.append(
            f"chunk {chunk['id']} 段落数 {len(pieces)} 与原文 {len(indexes)} 不一致，docx 回写放弃，输出纯文本"
        )
        return None, warnings
    target = out_dir / f"{file_path.stem}.polished.docx"
    try:
        write_polished_docx(file_path, target, replacements)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"docx 回写失败（{exc}），已输出纯文本")
        return None, warnings
    if comments_by_index:
        try:
            from utils.docx_io import add_comments

            add_comments(target, comments_by_index)
        except Exception as exc:  # noqa: BLE001 - 批注失败不影响主产物
            warnings.append(f"docx 批注写入失败（{exc}），请查看 result.json 中的告警")
    return target, warnings


def _collect_guardrail_feedback(
    parsed: dict[str, Any], steps: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[int, list[str]]]:
    """从各步骤结果汇总门禁违例，并按段落下标生成 docx 批注。

    返回 (guardrail_summary, comments_by_index)：
    - guardrail_summary：{fallback_chunks, failed_chunks, warned_chunks, total_errors, total_warnings}
    - comments_by_index：{段落下标: [批注文本]}——fallback 与 fail 的 chunk 落批注
    """
    chunk_paragraphs = {
        str(chunk.get("id")): list(chunk.get("paragraph_indexes") or [])
        for chunk in parsed.get("chunks", [])
    }
    summary = {
        "fallback_chunks": 0,
        "failed_chunks": 0,
        "warned_chunks": 0,
        "total_errors": 0,
        "total_warnings": 0,
    }
    comments: dict[int, list[str]] = {}
    for step in steps:
        if step.get("status") != "ok" or step.get("step") not in POLISH_SKILLS:
            continue
        try:
            payload = json.loads(Path(step["result_file"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
        for report in payload.get("chunk_reports") or []:
            # batch 层 chunk_reports 为聚合视图；fallback/违例明细在 sub_reports 中
            parent_chunk = str(report.get("chunk"))
            sub_reports = report.get("sub_reports") or [report]
            for sub in sub_reports:
                gate_summary = sub.get("gate_summary") or {}
                summary["total_errors"] += int(gate_summary.get("error") or 0)
                summary["total_warnings"] += int(gate_summary.get("warning") or 0)
                verdict = str(sub.get("gate_verdict"))
                if sub.get("fallback") == "source":
                    summary["fallback_chunks"] += 1
                    message = (
                        "[guardrails] 门禁未通过且重试已耗尽，本段已回退为原文。原因："
                        f"{str(sub.get('fallback_reason') or '')[:200]}"
                    )
                elif sub.get("fallback") == "surgical":
                    summary["fallback_chunks"] += 1
                    repairs = sub.get("surgical_repairs") or {}
                    message = (
                        "[guardrails] 门禁未通过且重试已耗尽，本段已按句级外科修复："
                        f"保留 {repairs.get('kept', '?')}/{repairs.get('total', '?')} 句修改，"
                        f"回退 {repairs.get('restored', '?')} 句违例"
                        f"（{'; '.join((repairs.get('restored_sentences') or [])[:3])}）。"
                        "请人工复核被回退的句子。"
                    )
                elif verdict == "fail":
                    summary["failed_chunks"] += 1
                    message = (
                        f"[guardrails] 门禁未通过（错误 {gate_summary.get('error')} 处），"
                        "请人工复核后重跑 apply。"
                    )
                elif verdict == "warn":
                    summary["warned_chunks"] += 1
                    continue  # 警告级不打断阅读，仅计入统计
                else:
                    continue
                indexes = chunk_paragraphs.get(parent_chunk, [])
                if indexes:
                    # 一个失败 chunk 只锚定首段，避免整段/整节数十个段落重复同一告警。
                    heading = next(
                        (
                            str(chunk.get("heading") or "")
                            for chunk in parsed.get("chunks", [])
                            if str(chunk.get("id")) == parent_chunk
                        ),
                        "",
                    )
                    comments.setdefault(indexes[0], []).append(
                        f"[chunk {parent_chunk}{(' ' + heading) if heading else ''}] {message}"
                    )
    return summary, comments


def _apply_one(
    file_path: Path,
    *,
    language: str,
    pipeline: list[str],
    strength: str,
    out_root: Path,
    extra: dict[str, Any],
) -> dict[str, Any]:
    started = time.time()
    result_dir = out_root / file_path.stem
    result_path = result_dir / "result.json"
    if extra.get("resume") and result_path.exists():
        return {"file": str(file_path), "status": "skipped", "result_file": str(result_path)}
    result_dir.mkdir(parents=True, exist_ok=True)

    parsed = _load_parsed(result_dir)
    if parsed is None:
        raise ValueError("缺少 parsed.json：请先运行 build 阶段")
    parsed_file = str(parsed.get("file", ""))
    if parsed_file and Path(parsed_file).name != file_path.name:
        logger.warning(
            "parsed.json 记录的源文件为 %s，与当前输入 %s 不一致，继续按当前输入处理",
            parsed_file,
            file_path.name,
        )

    responses_dir = Path(extra.get("responses_dir") or (result_dir / "responses"))
    jobs = _collect_jobs(result_dir, responses_dir)
    pending = [job for job in jobs if not job_io.job_is_ready(job)]

    # 主动触发：literature=true 时自动把文献检测/检索/核验步骤追加进流水线
    # （gap → search → cards → verify；写入正文仍只由 cite-manager insert 完成）
    pipeline = list(pipeline)
    if extra.get("literature"):
        for step in ("lit-search", "lit-verify"):
            if step not in pipeline:
                pipeline.append(step)

    steps: list[dict[str, Any]] = []
    current_text = str(parsed.get("full_text") or "")
    warnings: list[str] = []
    usage_summary: dict[str, int] = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    errors: list[dict[str, str]] = []
    gate_failed = False
    retry_jobs: list[dict[str, Any]] = []
    lit_candidates_file: str | None = None
    export_chunk_texts: dict[str, str] = {}

    # submission-audit 必须看到最终 DOCX，因此从普通前序步骤中拆出，
    # 在 docx 回写完成后单独执行。
    preflight_requested = "submission-audit" in pipeline
    pipeline = [step for step in pipeline if step != "submission-audit"]

    for index, skill_name in enumerate(pipeline):
        step_out_dir = result_dir / f"step-{index + 1:02d}-{skill_name}"
        step_out_dir.mkdir(parents=True, exist_ok=True)
        step_record: dict[str, Any] = {"step": skill_name, "status": "pending"}
        try:
            if skill_name in POLISH_SKILLS:
                current_text, step_result, step_retries = _run_polish_apply(
                    skill_name,
                    parsed=parsed,
                    jobs=jobs,
                    language=language,
                    strength=strength,
                    out_dir=step_out_dir,
                    extra=extra,
                )
                export_chunk_texts = {
                    str(key): str(value)
                    for key, value in (step_result.get("chunk_texts") or {}).items()
                }
                retry_jobs.extend(step_retries)
                if step_result.get("gate_failed_chunks"):
                    gate_failed = True
            elif skill_name == "format-adapter":
                current_text, step_result = _run_format_adapter_step(
                    text=current_text,
                    parsed=parsed,
                    language=language,
                    out_dir=step_out_dir,
                    extra=extra,
                )
            elif skill_name in ("lit-search", "lit-verify"):
                # 文献步骤：只产出报告 / 候选 / 核验结果，不修改当前正文。
                # 真正写入正文由 cite-manager 的 insert 模式完成（只接受 verified 条目）。
                step_args = (
                    dict(extra.get("lit_args") or {})
                    if skill_name == "lit-search"
                    else dict(extra.get("lit_verify_args") or {})
                )
                step_args.setdefault("language", language)
                step_args.setdefault("out", str(step_out_dir))
                if skill_name == "lit-search":
                    # literature 主动触发时默认 auto（gap→search→cards 一条龙）；
                    # 显式传入 mode 时以调用方为准（gap 只出报告不检索）
                    step_args.setdefault("mode", "auto" if extra.get("literature") else "gap")
                    lit_mode = str(step_args.get("mode"))
                    if lit_mode == "auto" or not any(
                        step_args.get(key) for key in ("text", "gaps", "gaps_file", "queries")
                    ):
                        step_args["text"] = current_text
                    if not step_args.get("references"):
                        step_args["references"] = _references_text(parsed)
                    if not step_args.get("paragraphs") and parsed.get("docx_paragraphs"):
                        step_args["paragraphs"] = [
                            item.get("text", "") for item in parsed.get("docx_paragraphs") or []
                        ] or None
                    step_args.setdefault("domain", extra.get("domain", "auto"))
                    step_args.setdefault("title", extra.get("title", ""))
                else:
                    step_args.setdefault(
                        "candidates_file",
                        lit_candidates_file or str(result_dir / "literature" / "lit-candidates.json"),
                    )
                step_result = run_skill(skill_name, step_args)
                if skill_name == "lit-search":
                    candidates_file = (step_result.get("files") or {}).get("candidates")
                    if candidates_file:
                        lit_candidates_file = candidates_file
            elif skill_name == "cite-manager":
                # 引用编号管理：默认 audit 只核对不改文本；通过 cite_args 传
                # mode/mapping/remove 等参数才会变换文本（参数见 cite-manager SKILL.md）
                cite_args = dict(extra.get("cite_args") or {})
                cite_args.setdefault("mode", "audit")
                cite_args.update(
                    {"text": current_text, "language": language}
                )
                step_result = run_skill("cite-manager", cite_args)
                if str(step_result.get("mode")) != "audit":
                    current_text = str(step_result.get("text", current_text))
                    step_result["output"] = str(step_out_dir / "cites.txt")
                    (step_out_dir / "cites.txt").write_text(current_text, encoding="utf-8")
            else:  # claim-guard / term-check：作用于当前全文
                args = {
                    "text": current_text,
                    "language": language,
                    "domain": extra.get("domain", "auto"),
                    "mode": "local",
                }
                if skill_name == "term-check":
                    args["output"] = str(step_out_dir / "term-check.json")
                if skill_name == "claim-guard":
                    args["output"] = str(step_out_dir / "claim-guard.json")
                step_result = run_skill(skill_name, args)

            step_record.update(
                {
                    "status": "ok",
                    "mode": step_result.get("mode", "local"),
                    "warnings": step_result.get("warnings", []),
                    "result_file": str(step_out_dir / "step-result.json"),
                }
            )
            (step_out_dir / "step-result.json").write_text(
                json.dumps(step_result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            step_usage = step_result.get("usage") or {}
            for key in usage_summary:
                usage_summary[key] += int(step_usage.get(key, 0) or 0)
            warnings.extend(step_result.get("warnings", []) or [])
        except Exception as exc:  # noqa: BLE001
            logger.exception("step failed file=%s step=%s", file_path.name, skill_name)
            step_record.update({"status": "error", "error": str(exc)})
            errors.append({"step": skill_name, "error": str(exc)})
        steps.append(step_record)

    if retry_jobs:
        retry_path = result_dir / "retry-jobs.json"
        existing_retry = []
        if retry_path.exists():
            existing_retry = [
                job
                for job in job_io.read_jobs(retry_path)
                if str(job.get("job_id", "")) not in {j["job_id"] for j in retry_jobs}
            ]
        job_io.write_jobs(
            retry_path, existing_retry + retry_jobs, meta={"kind": "retry"}
        )
        warnings.append(
            f"已生成 {len(retry_jobs)} 个带反馈的重试作业（{retry_path}）；"
            "coding harness 执行后重跑 apply 即可合并"
        )

    # docx 输入的中间产物是纯文本，不能伪装成 .docx（Word 打不开会误导用户）；
    # 真正的 docx 成品由 _export_docx 产出（<stem>.polished.docx）。
    is_docx = file_path.suffix.lower() == ".docx"
    output_suffix = ".txt" if is_docx else (file_path.suffix or ".txt")
    output_text_path = result_dir / f"{file_path.stem}.processed{output_suffix}"
    output_text_path.write_text(current_text, encoding="utf-8")

    # L3 汇总：门禁违例统计（供人工复核）+ docx 批注（fallback/needs_review 段落高亮告警）
    guardrail_summary, comments_by_index = _collect_guardrail_feedback(parsed, steps)
    if comments_by_index:
        warnings.append(
            f"{len(comments_by_index)} 个段落在 docx 中打了批注告警（门禁未通过已回退原文或需人工复核）"
        )

    docx_path: Path | None = None
    if file_path.suffix.lower() == ".docx":
        docx_path, docx_warnings = _export_docx(
            file_path,
            parsed,
            current_text,
            result_dir,
            comments_by_index=comments_by_index,
            chunk_texts=export_chunk_texts or None,
        )
        warnings.extend(docx_warnings)

    preflight_summary: dict[str, Any] = {}
    if preflight_requested:
        preflight_index = len(steps) + 1
        preflight_dir = result_dir / f"step-{preflight_index:02d}-submission-audit"
        preflight_dir.mkdir(parents=True, exist_ok=True)
        preflight_step: dict[str, Any] = {"step": "submission-audit", "status": "pending"}
        try:
            preflight_args = dict(extra.get("submission_audit_args") or {})
            if not preflight_args.get("package") and not preflight_args.get("package_file"):
                package_build = run_skill(
                    "submission-audit",
                    {
                        "mode": "package",
                        "input_file": str(file_path),
                        "language": language,
                        "title": str(extra.get("title") or parsed.get("title") or ""),
                        "out": str(preflight_dir),
                    },
                )
                preflight_args["package_file"] = package_build["package_file"]
                preflight_summary["package_build"] = {
                    "status": package_build.get("status"),
                    "package_file": package_build.get("package_file"),
                    "evidence_job_count": package_build.get("evidence_job_count", 0),
                    "unresolved": package_build.get("unresolved") or {},
                }
            preflight_args.setdefault(
                "docx_path",
                str(docx_path) if docx_path else "",
            )
            preflight_args.setdefault(
                "out",
                str(preflight_dir),
            )
            preflight_args.setdefault(
                "version",
                f"batch-{file_path.stem}",
            )
            preflight_result = run_skill("submission-audit", preflight_args)
            preflight_summary = {
                **preflight_summary,
                "status": preflight_result.get("status"),
                "audit_json": (preflight_result.get("files") or {}).get("audit_json"),
                "automated_pass": bool((preflight_result.get("gate") or {}).get("automated_pass")),
                "human_signoff_status": (preflight_result.get("gate") or {}).get("human_signoff_status"),
                "final_publication_allowed": bool(
                    (preflight_result.get("gate") or {}).get("final_publication_allowed")
                ),
            }
            preflight_step.update(
                {
                    "status": "ok",
                    "mode": "preflight",
                    "result_file": str(preflight_dir / "step-result.json"),
                    "warnings": [],
                }
            )
            (preflight_dir / "step-result.json").write_text(
                json.dumps(preflight_result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if not preflight_summary["automated_pass"]:
                gate_failed = True
                warnings.append("submission-audit 自动门禁未全部通过，候选版本需作者确认/人工签核")
        except Exception as exc:  # noqa: BLE001
            logger.exception("submission preflight failed file=%s", file_path.name)
            preflight_step.update({"status": "error", "error": str(exc)})
            errors.append({"step": "submission-audit", "error": str(exc)})
        steps.append(preflight_step)

    status = "error" if errors else ("needs_review" if gate_failed or pending else "ok")
    summary = {
        "file": str(file_path),
        "source_chars": len(str(parsed.get("full_text") or "")),
        "output_chars": len(current_text),
        "status": status,
        "steps": steps,
        "warnings": warnings,
        "errors": errors,
        "pending_jobs": [job["job_id"] for job in pending],
        "pending_count": len(pending),
        "usage": usage_summary,
        "guardrail_summary": guardrail_summary,
        "submission_audit": preflight_summary,
        "output_text_file": str(output_text_path),
        "output_docx_file": str(docx_path) if docx_path else "",
        "elapsed_seconds": round(time.time() - started, 3),
        "result_file": str(result_path),
    }
    result_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


# --------------------------------------------------------------------------- #
# 调度入口
# --------------------------------------------------------------------------- #


def _load_brief(args: dict[str, Any]) -> str:
    """文档级润色简报：brief 直接给文本，brief_file 优先读文件。"""
    brief_file = args.get("brief_file")
    if brief_file:
        return Path(str(brief_file)).read_text(encoding="utf-8")
    return str(args.get("brief") or "")


def run(args: dict[str, Any]) -> dict[str, Any]:
    language = normalize_language(str(args.get("language", "en")))
    phase = str(args.get("phase", "build")).lower()
    if phase not in ("build", "apply"):
        raise ValueError(f"未知 phase: {phase!r}，可选 build / apply")
    pipeline = _apply_pipeline_steps(args.get("pipeline"), language)
    strength = str(args.get("strength", "deep"))
    files = _expand_inputs(args)
    if not files:
        return {"status": "no_input_files", "files": []}
    out_root = Path(args.get("out") or ROOT / "output" / "batch")
    out_root.mkdir(parents=True, exist_ok=True)
    max_workers = max(1, int(args.get("max_workers", 1)))

    def execute(path: Path) -> dict[str, Any]:
        if phase == "build":
            return _build_one(
                path,
                language=language,
                strength=strength,
                out_root=out_root,
                extra={
                    "title": args.get("title", ""),
                    "domain": args.get("domain", "auto"),
                    "brief": _load_brief(args),
                    "literature": bool(args.get("literature", False)),
                    "lit_mode": args.get("lit_mode") or "gap",
                    "lit_args": args.get("lit_args"),
                    # 计划驱动修订（optimizer-loop / harness 显式传入）：
                    # only_chunks 限定出作业的章节；focus_issues 注入审计问题批注；
                    # chunk_strength 按问题优先级逐章节覆盖润色强度。
                    "only_chunks": args.get("only_chunks"),
                    "focus_issues": args.get("focus_issues"),
                    "chunk_strength": args.get("chunk_strength"),
                },
            )
        return _apply_one(
            path,
            language=language,
            pipeline=pipeline,
            strength=strength,
            out_root=out_root,
            extra={
                "resume": bool(args.get("resume", False)),
                "style": args.get("style", "keep"),
                "title": args.get("title", ""),
                "domain": args.get("domain", "auto"),
                "responses_dir": args.get("responses_dir"),
                "cite_args": args.get("cite_args"),
                "lit_args": args.get("lit_args"),
                "lit_verify_args": args.get("lit_verify_args"),
                "submission_audit_args": args.get("submission_audit_args"),
                "literature": bool(args.get("literature", False)),
                "strict_guardrails": bool(args.get("strict_guardrails", False)),
                # 与 build 阶段一致：选择性构建时计划外章节的 no_jobs 不算门禁失败
                "only_chunks": args.get("only_chunks"),
            },
        )

    results: list[dict[str, Any]] = []
    if max_workers <= 1:
        for path in files:
            try:
                results.append(execute(path))
            except Exception as exc:  # noqa: BLE001
                logger.exception("file failed: %s", path)
                results.append({"file": str(path), "status": "error", "errors": [{"step": phase, "error": str(exc)}]})
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {pool.submit(execute, path): path for path in files}
            for future in as_completed(future_map):
                path = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    logger.exception("file failed: %s", path)
                    results.append({"file": str(path), "status": "error", "errors": [{"step": phase, "error": str(exc)}]})
    usage_summary: dict[str, int] = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    for result in results:
        for key in usage_summary:
            usage_summary[key] += int(result.get("usage", {}).get(key, 0) or 0)
    summary = {
        "out_dir": str(out_root),
        "phase": phase,
        "language": language,
        "strength": strength,
        "input_count": len(files),
        "ok_count": sum(
            1 for r in results if r.get("status") in ("ok", "built")
        ),
        "needs_review_count": sum(1 for r in results if r.get("status") == "needs_review"),
        "skipped_count": sum(1 for r in results if r.get("status") == "skipped"),
        "error_count": sum(1 for r in results if r.get("status") == "error"),
        "usage": usage_summary,
        "items": results,
    }
    if phase == "build":
        summary["next_step"] = (
            "coding harness：读取各文件目录下 prompts/*.prompt.txt 生成响应，"
            "写入 responses/<job_id>.txt 后调用 apply 阶段"
        )
    summary_path = out_root / f"summary-{phase}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
