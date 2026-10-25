"""中英文润色 Skill 的共用引擎（build / apply 两阶段，纯离线）。

en-polish 与 cn-polish 的业务逻辑完全同构，仅语言侧常量不同；
按 AGENTS.md 约束，语言差异只允许存在于 prompts/config/data，
因此实现统一收敛到本模块，两个 Skill 目录只保留薄壳与 run(args) 契约。

通过 create_skill(config) 工厂生成彼此独立的实现命名空间：
config: {skill_name, language, term_language, none_feedback, description}
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from utils import chunk_contract, job_io
from utils.config_loader import (
    ConfigError,
    detect_domain,
    load_domain_pack,
    load_rules_config,
    load_terms,
    normalize_domain,
    resolve_model_key,
)
from utils.differ import diff_stats, render_markdown_diff
from utils.logger import get_logger
from utils.prompt_renderer import (
    render_rules_snapshot,
    render_template,
    render_term_corrections,
    render_terms_text,
)
from utils.skill_loader import run_skill
from utils.token_counter import split_into_chunks, split_sentences

CONTEXT_TAIL_CHARS = 160
VALID_STRENGTHS = ("light", "deep", "rewrite")
VALID_MODES = ("build", "apply", "offline")
# 批注锚定匹配：锚文本被 chunk 边界切断时，退化为前缀匹配的最小前缀长度
COMMENT_ANCHOR_PREFIX_CHARS = 24


def _surgical_enabled(strength_cfg: dict[str, Any]) -> bool:
    """句级外科回退开关（quality_gates.surgical_fallback，默认开启）。"""
    return bool((strength_cfg.get("quality_gates") or {}).get("surgical_fallback", True))


def _gate_pair(
    source_text: str,
    polished_text: str,
    *,
    language: str,
    strength: str,
    references_text: str,
) -> dict[str, Any]:
    return run_skill(
        "review-gate",
        {
            "source_text": source_text,
            "polished_text": polished_text,
            "language": language,
            "strength": strength,
            "mode": "local",
            "references_text": references_text,
        },
    )


def _surgical_repair(
    source: str,
    polished: str,
    *,
    language: str,
    strength: str,
    references_text: str,
) -> tuple[str, str, dict[str, Any]] | None:
    """句级外科修复：逐句门禁，只回退违例句，保留块内已过审的修改。

    返回 (修复后文本, 整块终检verdict, 修复统计)；
    句数无法对齐或整块终检仍 fail 时返回 None（调用方整块回退原文）。
    """
    src_sents = split_sentences(source)
    pol_sents = split_sentences(polished)
    if not src_sents or len(src_sents) != len(pol_sents):
        # 句数不一致时逐句 zip 会错位（静默丢句/串句），宁可整块回退
        return None

    repaired: list[str] = []
    restored_sentences: list[str] = []
    kept = 0
    for src_sent, pol_sent in zip(src_sents, pol_sents):
        if src_sent == pol_sent:
            repaired.append(pol_sent)
            kept += 1
            continue
        verdict = _gate_pair(
            src_sent,
            pol_sent,
            language=language,
            strength=strength,
            references_text=references_text,
        )
        if verdict["verdict"] in ("pass", "warn"):
            repaired.append(pol_sent)
            kept += 1
        else:
            repaired.append(src_sent)
            restored_sentences.append(src_sent.strip()[:60])
    candidate = "".join(repaired)
    final_gate = _gate_pair(
        source,
        candidate,
        language=language,
        strength=strength,
        references_text=references_text,
    )
    if final_gate["verdict"] == "fail":
        return None
    stats = {
        "total": len(src_sents),
        "kept": kept,
        "restored": len(restored_sentences),
        "restored_sentences": restored_sentences,
    }
    return candidate, final_gate["verdict"], stats

_SENTENCE_REWORK_PREFIX = {
    "cn": (
        "【局部返工指令】上一轮输出仅以下句子未通过审查。请保持其余句子原样不动，"
        "仅修正所列句子后重新输出完整文本。"
    ),
    "en": (
        "[Selective rework] Only the sentences listed below failed review. "
        "Keep all other sentences unchanged and re-emit the full text with only those fixed."
    ),
}


def _sentence_rework_prefix(language: str) -> str:
    return _SENTENCE_REWORK_PREFIX.get(language, _SENTENCE_REWORK_PREFIX["cn"])


def _normalize_ws(value: Any) -> str:
    return " ".join(str(value or "").split())


def _match_comment_in_text(comment: dict[str, Any], text: str) -> bool:
    """判断批注是否锚定在给定文本上：归一化空白后做包含判断。

    chunk 切分可能把锚文本截断在两块之间，此时用锚的前缀兜底匹配；
    过短（<6 字符）的前缀不做兜底，避免误挂到无关 chunk。
    """
    anchor = _normalize_ws(comment.get("anchor_text"))
    if not anchor:
        return False
    target = _normalize_ws(text)
    if anchor in target:
        return True
    prefix = anchor[:COMMENT_ANCHOR_PREFIX_CHARS]
    return len(prefix) >= 6 and prefix in target


def _format_comments_block(
    matched: list[dict[str, Any]], language: str, none_value: str
) -> str:
    """把锚定到当前 chunk 的批注渲染为 prompt 区块文本。"""
    if not matched:
        return none_value
    lines: list[str] = []
    for comment in matched:
        author = _normalize_ws(comment.get("author")) or (
            "Reviewer" if language == "en" else "导师"
        )
        anchor = _normalize_ws(comment.get("anchor_text"))[:60]
        text = _normalize_ws(comment.get("text"))
        if language == "en":
            lines.append(f'- {author} commented on "{anchor}": {text}')
        else:
            lines.append(f"- {author} 批注（锚定原文：“{anchor}”）：{text}")
    return "\n".join(lines)


def _normalize_comments(comments: list[Any] | None) -> list[dict[str, Any]]:
    """规整批注输入：只保留有正文的条目，缺失 id 时按序补一个稳定 id。"""
    normalized: list[dict[str, Any]] = []
    for position, comment in enumerate(comments or [], start=1):
        if not isinstance(comment, dict):
            continue
        if not _normalize_ws(comment.get("text")):
            continue
        entry = dict(comment)
        if not str(entry.get("id", "") or "").strip():
            entry["id"] = f"k{position:02d}"
        normalized.append(entry)
    return normalized


def create_skill(config: dict[str, Any]) -> SimpleNamespace:
    skill_name = str(config["skill_name"])
    language = str(config["language"])
    term_language = str(config.get("term_language", language))
    none_feedback = str(config.get("none_feedback", "(none)"))
    logger = get_logger(skill_name)

    def _build_system_prompt(rules_cfg: dict[str, Any]) -> str:
        agent = rules_cfg.get("agent", {})
        parts = [agent.get("role", ""), agent.get("language_note", "")]
        for boundary in agent.get("boundaries", []):
            parts.append(f"- {boundary}")
        return "\n".join(part for part in parts if part)

    def _normalize_strength(strength: str) -> str:
        if strength not in VALID_STRENGTHS:
            raise ConfigError(f"不支持的润色强度: {strength!r}，可选 {VALID_STRENGTHS}")
        return strength

    def _render_anchors(chunk: str, ctx: dict[str, Any] | None = None) -> tuple[str, str]:
        """L1 锚点锁定：提取本 chunk 五类锚点并渲染必保清单 + CoVe 自检表。

        提取失败时退化为空清单（不阻断 build；审查侧 guardrails 仍会兜底）。
        """
        from utils.anchor_lock import extract_anchors, render_anchor_block, render_self_check

        domain_key = (ctx or {}).get("domain_key")
        try:
            anchors = extract_anchors(chunk, language=language, domain=domain_key)
        except Exception as exc:  # noqa: BLE001 - 锚点提取失败不阻断作业生成
            logger.warning("锚点提取失败，退化为空清单: %s", exc)
            return "", render_self_check(language)
        return render_anchor_block(anchors, language=language), render_self_check(language)

    def _normalize_mode(mode: str) -> str:
        value = (mode or "build").lower().replace("_", "-")
        if value == "offline":
            return "build"
        if value not in VALID_MODES:
            raise ConfigError(f"未知 mode: {mode!r}，可选 build / apply")
        return value

    def _chunk_limit(strength_cfg: dict[str, Any], override: int | None = None) -> int:
        if override and override > 0:
            return int(override)
        raw = int(strength_cfg.get("max_input_tokens_per_chunk", 4000))
        # 为 Prompt 模板与系统指令预留约 1000 token
        return max(512, raw - 1000)

    def _resolve_domain(domain: str | None, text: str) -> str:
        if domain in (None, "", "auto"):
            return detect_domain(text, language)
        key = normalize_domain(domain)
        load_domain_pack(key)
        return key

    def _render_prompt(
        strength_cfg: dict[str, Any],
        rules_text: str,
        terms_text: str,
        term_corrections: str,
        *,
        chunk: str,
        metadata: dict[str, Any],
        strength: str,
        feedback: str,
        context_value: str,
        comments_value: str = "",
        brief_value: str = "",
        anchors_value: str = "",
        self_check_value: str = "",
    ) -> str:
        # L1 锚点锁定：未显式提供时从本 chunk 现提（retry 路径与 build 路径等价）
        if not anchors_value or not self_check_value:
            computed_anchors, computed_check = _render_anchors(chunk, None)
            anchors_value = anchors_value or computed_anchors
            self_check_value = self_check_value or computed_check
        return render_template(
            strength_cfg["prompt_template"],
            language,
            title=str(metadata.get("title", "")),
            section_type=str(metadata.get("section_type", "")),
            context=context_value,
            strength_label=str(strength_cfg.get("label", strength)),
            feedback=feedback or none_feedback,
            rules=rules_text,
            terms=terms_text,
            term_corrections=term_corrections,
            comments=comments_value or none_feedback,
            brief=brief_value or none_feedback,
            anchors=anchors_value,
            self_check=self_check_value,
            text=chunk,
        )

    def _shared_context(
        text: str,
        strength: str,
        domain: str | None,
        max_chunk_tokens: int | None,
    ) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("待润色文本为空")
        rules_cfg = load_rules_config(language)
        strength_cfg = rules_cfg["strengths"][strength]
        domain_key = _resolve_domain(domain, text)
        domain_pack = load_domain_pack(domain_key)
        suggested_model = resolve_model_key(skill_name, strength)
        terms_data = load_terms(term_language, domain_key)
        return {
            "rules_cfg": rules_cfg,
            "strength_cfg": strength_cfg,
            "domain_key": domain_key,
            "suggested_model": suggested_model,
            "system_prompt": _build_system_prompt(rules_cfg),
            "rules_text": render_rules_snapshot(rules_cfg, strength, domain_pack),
            "terms_text": render_terms_text(terms_data),
            "term_corrections": render_term_corrections(terms_data, language),
            "chunk_limit": _chunk_limit(strength_cfg, max_chunk_tokens),
        }

    def build_jobs(
        text: str,
        *,
        strength: str = "deep",
        metadata: dict[str, Any] | None = None,
        domain: str | None = "auto",
        max_chunk_tokens: int | None = None,
        feedback_by_chunk: dict[str, str] | None = None,
        comments: list[dict[str, Any]] | None = None,
        brief: str = "",
    ) -> dict[str, Any]:
        """生成待coding harness 执行的 prompt 作业包。

        comments：docx 批注列表（paper-parser 的 result["comments"] 结构），
        按锚定原文匹配挂到对应 chunk，经模板 {{comments}} 注入 prompt；
        brief：文档级润色简报（风格决定、范围约定），注入每个 chunk 的 {{brief}}。
        """
        strength = _normalize_strength(strength)
        metadata = metadata or {}
        ctx = _shared_context(text, strength, domain, max_chunk_tokens)
        chunks = split_into_chunks(text, ctx["chunk_limit"])
        feedback_by_chunk = feedback_by_chunk or {}
        brief_value = str(brief or "").strip()
        normalized_comments = _normalize_comments(comments)
        matched_by_chunk = [
            [c for c in normalized_comments if _match_comment_in_text(c, chunk)]
            for chunk in chunks
        ]
        attached_ids = {
            str(c["id"]) for matched in matched_by_chunk for c in matched
        }
        unattached = [
            c for c in normalized_comments if str(c["id"]) not in attached_ids
        ]
        warnings = [
            f"批注 {c['id']}（{_normalize_ws(c.get('text'))[:24]}…）"
            "未能锚定到任何 chunk，未注入 prompt，请人工核对"
            for c in unattached
        ]

        jobs: list[dict[str, Any]] = []
        chunk_records: list[dict[str, Any]] = []
        prev_tail = ""
        for position, chunk in enumerate(chunks, start=1):
            chunk_id = f"c{position:03d}"
            feedback = feedback_by_chunk.get(chunk_id, "")
            comments_value = _format_comments_block(
                matched_by_chunk[position - 1], language, none_feedback
            )
            anchors_block, self_check_block = _render_anchors(chunk, ctx)
            prompt = _render_prompt(
                ctx["strength_cfg"],
                ctx["rules_text"],
                ctx["terms_text"],
                ctx["term_corrections"],
                chunk=chunk,
                metadata=metadata,
                strength=strength,
                feedback=feedback,
                context_value=str(metadata.get("context", "") or prev_tail),
                comments_value=comments_value,
                brief_value=brief_value,
                anchors_value=anchors_block,
                self_check_value=self_check_block,
            )
            jobs.append(
                job_io.new_job(
                    job_id=f"{chunk_id}-a1" if not feedback else f"{chunk_id}-a2",
                    chunk_id=chunk_id,
                    skill=skill_name,
                    language=language,
                    strength=strength,
                    domain=ctx["domain_key"],
                    section_type=str(metadata.get("section_type", "")),
                    system_prompt=ctx["system_prompt"],
                    prompt=prompt,
                    suggested_model=ctx["suggested_model"],
                    chunk_content=chunk,
                    attempt=1 if not feedback else 2,
                    feedback=feedback,
                    comments=comments_value,
                    brief=brief_value or none_feedback,
                )
            )
            chunk_records.append(
                {
                    "chunk_id": chunk_id,
                    "chars": len(chunk),
                    "comments": [str(c["id"]) for c in matched_by_chunk[position - 1]],
                }
            )
            prev_tail = chunk[-CONTEXT_TAIL_CHARS:]

        return {
            "skill": skill_name,
            "language": language,
            "strength": strength,
            "domain": ctx["domain_key"],
            "suggested_model": ctx["suggested_model"],
            "mode": "build",
            "chunk_count": len(chunks),
            "chunks": chunk_records,
            "jobs": jobs,
            "polished_text": text,
            "brief": brief_value,
            "comments": {
                "total": len(normalized_comments),
                "attached_ids": sorted(attached_ids, key=str),
                "unattached_ids": [str(c["id"]) for c in unattached],
            },
            "warnings": warnings,
        }

    def _gate_max_retries(strength_cfg: dict[str, Any]) -> int:
        return int(strength_cfg.get("quality_gates", {}).get("max_gate_retries", 1))

    def apply_responses(
        text: str,
        jobs: list[dict[str, Any]],
        *,
        strength: str = "deep",
        metadata: dict[str, Any] | None = None,
        domain: str | None = "auto",
        max_chunk_tokens: int | None = None,
        references_text: str | None = None,
    ) -> dict[str, Any]:
        """消费已回填响应的作业：清洗 → 门禁 → 重试作业 → 装配。

        references_text：参考文献全文（传入时启用引用编号越界检查）；
        Fail-Closed：门禁 fail 且重试作业已耗尽时，该 chunk 强制装配**原文**，
        未通过审查的润色文本绝不流入终稿（fallback_reason 记入 chunk_reports）。
        """
        strength = _normalize_strength(strength)
        if not jobs:
            raise ValueError("apply 需要 jobs（至少一个作业）")
        ctx = _shared_context(text, strength, domain, max_chunk_tokens)
        max_retries = _gate_max_retries(ctx["strength_cfg"])
        sentence_rework = bool(
            (ctx["strength_cfg"].get("quality_gates") or {}).get("sentence_rework", False)
        )

        # 先按 skill 过滤，再校验：jobs.json 可能混有其它 skill 的作业，
        # 那些不参与本次装配，也不应参与块身份校验（否则会误报缺块/冲突）。
        own_jobs = [job for job in jobs if job.get("skill") in (None, skill_name)]

        # 错误级校验：空块标识、同一身份下内容冲突。这类问题必须中断，
        # 因为继续装配只会静默丢块或重块。
        key_problems = chunk_contract.validate_job_set(own_jobs)
        if key_problems:
            raise ValueError("作业包块身份校验失败：" + "；".join(key_problems))

        warnings: list[str] = []
        by_chunk: dict[str, list[dict[str, Any]]] = {}
        for job in own_jobs:
            by_chunk.setdefault(chunk_contract.normalize_job_key(job), []).append(job)

        # 期望块集合只在能可靠推导时比对：build_jobs 直产的作业包（无 parent_chunk_id）
        # 可按 build 时同一套切分规则推导；带 parent 的作业包由 batch-processor
        # 按解析器章节自行比对，此处不假设，避免误报。
        expected_map: dict[str, str] | None = None
        if not any(job.get("parent_chunk_id") for job in own_jobs):
            expected_map = chunk_contract.expected_chunk_map(text, ctx["chunk_limit"])

        if expected_map is not None:
            # 缺块补回原文并显式告警：文档完整性优先，绝不让某一块无声消失。
            missing, unexpected = chunk_contract.diff_against_expected(
                by_chunk.keys(), expected_map.keys()
            )
            for key in missing:
                by_chunk[key] = [
                    {
                        "job_id": f"{key}-missing",
                        "chunk_id": key,
                        "chunk_content": expected_map[key],
                        "response": "",
                    }
                ]
                warnings.append(
                    f"块 {key} 在作业包中缺失，已保留原文（build 与 apply 的块集合不一致）"
                )
            if unexpected:
                warnings.append(
                    f"作业包含未在预期块集合中的标识 {unexpected}，"
                    "请确认 build 与 apply 使用了相同强度与 max_chunk_tokens"
                )

        chunk_order = sorted(by_chunk, key=chunk_contract.chunk_sort_key)
        polished_parts: list[str] = []
        chunk_reports: list[dict[str, Any]] = []
        retry_jobs: list[dict[str, Any]] = []
        usage: dict[str, int] = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

        for block_key in chunk_order:
            chunk_jobs = by_chunk[block_key]
            # block_key 是 (parent, chunk) 复合键，只用于分组与排序；
            # 作业自身的 chunk_id（子块编号）仍是身份契约——重试 job_id、chunk_reports
            # 必须沿用原字段，否则复合键里的 "/" 会进入 job_id 与文件名，破坏回填路径。
            block_id = str(chunk_jobs[0].get("chunk_id") or block_key)
            source = str(chunk_jobs[0].get("chunk_content") or "")
            best = job_io.select_best_response(chunk_jobs)
            if best is None:
                polished_parts.append(source)
                warnings.append(f"chunk {block_key} 无响应，已保留原文")
                chunk_reports.append(
                    {"chunk_id": block_id, "gate_verdict": "no_response", "attempts": len(chunk_jobs)}
                )
                continue

            response_text, resp_usage, incomplete, reason = job_io.parse_response_field(
                best.get("response")
            )
            polished_part = response_text or source
            for key in usage:
                usage[key] += int(resp_usage.get(key, 0) or 0)
            if incomplete:
                warnings.append(
                    f"chunk {block_key} 响应可能被截断（{reason}），请人工核对该块结尾"
                )
            if not response_text:
                warnings.append(f"chunk {block_key} 响应为空，已保留原文")

            gate = _gate_pair(
                source,
                polished_part,
                language=language,
                strength=strength,
                references_text=references_text or "",
            )
            attempts = len(chunk_jobs)
            fail_closed = False
            if gate["verdict"] == "fail" and attempts <= max_retries:
                feedback = str(gate.get("retry_feedback", ""))
                if sentence_rework:
                    feedback = _sentence_rework_prefix(language) + "\n" + feedback
                prev_tail = polished_parts[-1][-CONTEXT_TAIL_CHARS:] if polished_parts else ""
                # 重试作业沿用 build 时锚定到本 chunk 的批注与文档级简报（随 job 落盘）
                comments_value = str(chunk_jobs[0].get("comments") or "")
                brief_value = str(chunk_jobs[0].get("brief") or "")
                retry_prompt = _render_prompt(
                    ctx["strength_cfg"],
                    ctx["rules_text"],
                    ctx["terms_text"],
                    ctx["term_corrections"],
                    chunk=source,
                    metadata=metadata or {},
                    strength=strength,
                    feedback=feedback,
                    context_value=str((metadata or {}).get("context", "") or prev_tail),
                    comments_value=comments_value,
                    brief_value=brief_value,
                    anchors_value=_render_anchors(source, ctx)[0],
                    self_check_value=_render_anchors(source, ctx)[1],
                )
                retry_jobs.append(
                    job_io.new_job(
                        job_id=f"{block_id}-a{attempts + 1}",
                        chunk_id=block_id,
                        skill=skill_name,
                        language=language,
                        strength=strength,
                        domain=ctx["domain_key"],
                        section_type=str((metadata or {}).get("section_type", "")),
                        system_prompt=ctx["system_prompt"],
                        prompt=retry_prompt,
                        suggested_model=ctx["suggested_model"],
                        chunk_content=source,
                        attempt=attempts + 1,
                        feedback=feedback,
                        comments=comments_value,
                        brief=brief_value,
                    )
                )
                warnings.append(
                    f"chunk {block_key} 门禁未通过，已生成带反馈的重试作业（待coding harness 执行）"
                )
            if gate["verdict"] == "fail":
                warnings.append(f"chunk {block_key} 门禁未通过，需人工复核: {gate['summary']}")
                if attempts > max_retries:
                    # Fail-Closed：重试耗尽。优先句级外科修复——只回退违例句，
                    # 保留块内已通过审查的句子；无法对齐或整块复检仍失败时
                    # 才整块回退原文（未过审文本绝不流入终稿）。
                    fail_closed = True
                    surgical = None
                    if _surgical_enabled(ctx["strength_cfg"]):
                        surgical = _surgical_repair(
                            source,
                            polished_part,
                            language=language,
                            strength=strength,
                            references_text=references_text or "",
                        )
                    if surgical is not None:
                        polished_part, final_verdict, repairs = surgical
                        fallback_mode = "surgical"
                        warnings.append(
                            f"chunk {block_key} 重试已耗尽，已按句级外科修复保留 "
                            f"{repairs['kept']}/{repairs['total']} 句修改、回退 "
                            f"{repairs['restored']} 句违例（终检 {final_verdict}）"
                        )
                    else:
                        fallback_mode = "source"
                        polished_part = source
                        warnings.append(
                            f"chunk {block_key} 重试已耗尽且门禁仍未通过，已按 Fail-Closed 回退为原文"
                        )

            if fail_closed:
                chunk_reports.append(
                    {
                        "chunk_id": block_id,
                        "gate_verdict": final_verdict
                        if fallback_mode == "surgical"
                        else "fail",
                        "gate_summary": gate["summary"],
                        "attempts": attempts,
                        "similarity_ratio": gate["metrics"].get("similarity_ratio"),
                        "fallback": fallback_mode,
                        "fallback_reason": gate.get("retry_feedback", ""),
                        **(
                            {
                                "surgical_repairs": {
                                    "total": repairs["total"],
                                    "kept": repairs["kept"],
                                    "restored": repairs["restored"],
                                    "restored_sentences": repairs["restored_sentences"],
                                }
                            }
                            if fallback_mode == "surgical"
                            else {}
                        ),
                    }
                )
                polished_parts.append(polished_part)
                continue
            polished_parts.append(polished_part)
            chunk_reports.append(
                {
                    "chunk_id": block_id,
                    "gate_verdict": gate["verdict"],
                    "gate_summary": gate["summary"],
                    "attempts": attempts,
                    "similarity_ratio": gate["metrics"].get("similarity_ratio"),
                }
            )

        polished_text = "\n\n".join(polished_parts) if len(polished_parts) > 1 else polished_parts[0]
        assembly_errors, assembly_warnings = chunk_contract.verify_assembly(
            polished_parts,
            chunk_order,
            source_text=text,
            polished_text=polished_text,
        )
        if assembly_errors:
            raise RuntimeError("装配完整性校验失败：" + "；".join(assembly_errors))
        warnings.extend(assembly_warnings)
        model_key = str(jobs[0].get("suggested_model") or ctx["suggested_model"])
        return {
            "skill": skill_name,
            "language": language,
            "strength": strength,
            "domain": ctx["domain_key"],
            "suggested_model": model_key,
            "mode": "apply",
            "chunk_count": len(chunk_order),
            "polished_text": polished_text,
            "chunk_reports": chunk_reports,
            "retry_jobs": retry_jobs,
            "usage": usage,
            "estimated_cost_usd": job_io.estimate_cost_usd(usage, model_key),
            "warnings": warnings,
            "diff_stats": diff_stats(text, polished_text),
        }

    def _write_outputs(
        result: dict[str, Any],
        source_text: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        output = args.get("output")
        if output:
            out_path = Path(output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if str(result.get("mode")) == "build":
                job_io.write_jobs(
                    out_path,
                    result["jobs"],
                    meta={
                        "skill": skill_name,
                        "language": result["language"],
                        "strength": result["strength"],
                        "domain": result["domain"],
                    },
                )
            else:
                out_path.write_text(result["polished_text"], encoding="utf-8")
            result["output"] = str(output)
            if str(result.get("mode")) == "apply":
                diff_path = out_path.with_suffix(out_path.suffix + ".diff.md")
                diff_path.write_text(
                    render_markdown_diff(
                        source_text, result["polished_text"], title=f"diff: {out_path.name}"
                    ),
                    encoding="utf-8",
                )
                result["diff_file"] = str(diff_path)
        jobs_file = args.get("jobs_file")
        if jobs_file and str(result.get("mode")) == "build":
            job_io.write_jobs(
                jobs_file,
                result["jobs"],
                meta={
                    "skill": skill_name,
                    "language": result["language"],
                    "strength": result["strength"],
                    "domain": result["domain"],
                },
            )
            result["jobs_file"] = str(jobs_file)
        retry_file = args.get("retry_jobs_file")
        if retry_file and result.get("retry_jobs"):
            job_io.write_jobs(retry_file, result["retry_jobs"], meta={"kind": "retry"})
            result["retry_jobs_file"] = str(retry_file)
        return result

    def _load_comments_arg(args: dict[str, Any]) -> list[dict[str, Any]] | None:
        """读取批注输入：comments 列表优先，其次 comments_file（批注数组或 parser 输出）。"""
        comments = args.get("comments")
        if comments is not None:
            return comments
        comments_file = args.get("comments_file")
        if not comments_file:
            return None
        data = json.loads(Path(comments_file).read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            data = data.get("comments") or []
        if not isinstance(data, list):
            raise ValueError("comments_file 需为批注 JSON 数组或 paper-parser 输出 JSON")
        return data

    def _load_brief_arg(args: dict[str, Any]) -> str:
        brief = str(args.get("brief") or "")
        if brief.strip():
            return brief
        brief_file = args.get("brief_file")
        if brief_file:
            return Path(brief_file).read_text(encoding="utf-8-sig")
        return ""

    def run(args: dict[str, Any]) -> dict[str, Any]:
        text = args.get("text")
        text_file = args.get("text_file")
        if text is None and text_file:
            text = Path(text_file).read_text(encoding="utf-8-sig")
        if text is None:
            raise ValueError(f"{skill_name} run() 需要 text_file 或 text")
        mode = _normalize_mode(str(args.get("mode", "build")))
        metadata = {
            "title": args.get("title", ""),
            "section_type": args.get("section_type", ""),
            "context": args.get("context", ""),
        }
        if mode == "build":
            result = build_jobs(
                text,
                strength=str(args.get("strength", "deep")),
                metadata=metadata,
                domain=args.get("domain", "auto"),
                max_chunk_tokens=args.get("max_chunk_tokens"),
                comments=_load_comments_arg(args),
                brief=_load_brief_arg(args),
            )
        else:
            jobs = args.get("jobs")
            if jobs is None:
                jobs_file = args.get("jobs_file")
                if not jobs_file:
                    raise ValueError("apply 模式需要 jobs 或 jobs_file")
                jobs = job_io.read_jobs(jobs_file)
            responses_dir = args.get("responses_dir")
            if responses_dir:
                job_io.fill_responses_from_dir(jobs, responses_dir)
            references_text = args.get("references") or args.get("references_text") or ""
            if not references_text and args.get("references_file"):
                references_text = Path(args["references_file"]).read_text(encoding="utf-8-sig")
            result = apply_responses(
                text,
                jobs,
                strength=str(args.get("strength", "deep")),
                metadata=metadata,
                domain=args.get("domain", "auto"),
                max_chunk_tokens=args.get("max_chunk_tokens"),
                references_text=references_text or None,
            )
        return _write_outputs(result, text, args)

    return SimpleNamespace(
        skill_name=skill_name,
        language=language,
        build_system_prompt=_build_system_prompt,
        build_jobs=build_jobs,
        apply_responses=apply_responses,
        run=run,
    )
