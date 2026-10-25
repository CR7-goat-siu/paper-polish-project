"""Issue Registry：把各审计 Skill 的 findings 归一为带编号、带优先级的问题清单。

PRD 对应条款：
- §5 阶段二「识别与排序问题」：按严重程度/影响范围排序，问题分 P0-P3；
- §2.2「所有实质性修改必须可追溯」：每个问题分配唯一编号，跨轮次追踪；
- §9.2 Manuscript Auditor 的问题清单部分。

输入（二选一）：
- `sources`：已算好的审计结果列表，每项 `{skill, result, location?}`；
- `text_file` + `language`：由本 Skill 自行调用全部确定性审计源
  （claim-guard / term-check / cite-manager audit / lit-search gap / consistency-check，
  均离线，不调模型、不联网），假设输入为完整论文全文。

输出：统一 registry，含 issue_id、priority（P0-P3）、dimension、指纹与排序。
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import consistency, issue_model, stat_checks  # noqa: E402
from utils.config_loader import normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from utils.skill_loader import run_skill  # noqa: E402

LOG = get_logger("issue-registry")

SCHEMA_VERSION = 1

# 与 cite-manager / paper-parser 的参考文献标题特征保持同义的本地副本
# （跨 Skill 禁止 import 实现细节，只允许 run() 契约或公共 utils/）
_REF_HEADING_RE = re.compile(
    r"^\s*#*\s*(references|bibliography|参\s*考\s*文\s*献|文\s*献)\s*[:：]?\s*$",
    re.IGNORECASE,
)


def _references_section(text: str) -> str:
    """截取参考文献章节文本（供 lit-search 的陈旧/信息不全检测）；未找到返回空。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _REF_HEADING_RE.match(line.strip()):
            return "\n".join(lines[index + 1 :])
    return ""


def _cite_audit_findings(audit: dict[str, Any]) -> list[dict[str, Any]]:
    """cite-manager audit 结果 → findings（引用表完整性）。"""
    findings: list[dict[str, Any]] = []
    for num in audit.get("cited_not_listed") or []:
        findings.append(
            {
                "check": "out_of_bounds",
                "found": f"[{num}]",
                "severity": "error",
                "message": f"正文引用了 [{num}]，但参考文献列表中没有该条目",
                "context_snippet": f"cited [{num}] not listed in references",
                "suggestion": "补齐条目、改引既有编号，或走 lit-search → lit-verify → cite-manager insert 流程",
            }
        )
    for num in audit.get("listed_not_cited") or []:
        findings.append(
            {
                "check": "uncited_entry",
                "found": f"[{num}]",
                "severity": "warning",
                "message": f"参考文献条目 [{num}] 未被正文任何位置引用",
                "context_snippet": f"entry [{num}] never cited",
                "suggestion": "核对是否漏引，或经 cite-manager renumber 收敛编号",
            }
        )
    for num in audit.get("duplicated_entries") or []:
        findings.append(
            {
                "check": "duplicated_entry",
                "found": f"[{num}]",
                "severity": "warning",
                "message": f"参考文献条目 [{num}] 重复出现",
                "context_snippet": f"entry [{num}] duplicated",
                "suggestion": "去重并经 cite-manager renumber 重排编号",
            }
        )
    for snippet in audit.get("duplicated_entry_lines") or []:
        findings.append(
            {
                "check": "duplicated_entry_line",
                "found": snippet,
                "severity": "warning",
                "message": f"参考文献存在整行重复条目：{snippet[:60]}",
                "context_snippet": snippet,
                "suggestion": "删除重复条目（年份标注式列表按行内容去重）",
            }
        )
    for marker in audit.get("unparsable_markers") or []:
        findings.append(
            {
                "check": "unparsable_marker",
                "found": str(marker),
                "severity": "warning",
                "message": f"引用标记 {marker} 无法解析（编号格式不合法）",
                "context_snippet": f"unparsable marker {marker}",
                "suggestion": "改为规范的 [n] / [n,m] / [n-m] 格式",
            }
        )
    return findings


_LIT_GAP_META = {
    "missing_citation": ("warning", "论断缺少文献支撑"),
    "stale_citation": ("info", "趋势论断只引旧文献"),
    "incomplete_entry": ("info", "参考文献条目信息不全"),
    "missing_entry": ("error", "正文引用但参考文献缺条目"),
}


def _lit_gap_findings(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """lit-search gap 检测结果 → findings（文献支撑缺口）。"""
    findings: list[dict[str, Any]] = []
    for gap in gaps or []:
        gap_type = str(gap.get("gap_type") or "missing_citation")
        severity, label = _LIT_GAP_META.get(gap_type, ("warning", gap_type))
        quote = str(gap.get("quote") or "")
        findings.append(
            {
                "check": gap_type,
                "found": str(gap.get("gap_id") or gap_type),
                "severity": severity,
                "message": f"{label}：{quote[:80]}",
                "context_snippet": quote,
                "suggestion": "检索并核验候选文献（lit-search search → lit-verify），仅 verified 条目可插入",
            }
        )
    return findings


def _collect_from_text(text: str, language: str) -> list[dict[str, Any]]:
    """自行跑全部确定性审计源，产出 (skill, result) 对。

    单项来源失败只记告警不阻塞登记（缺一个来源仍能产出部分 registry）。
    """
    sources: list[dict[str, Any]] = []
    for skill in ("claim-guard", "term-check"):
        try:
            result = run_skill(skill, {"text": text, "language": language, "mode": "local"})
        except Exception as exc:  # noqa: BLE001 - 单项检查失败不阻塞登记
            LOG.warning("%s 检查失败: %s", skill, exc)
            continue
        sources.append({"skill": skill, "result": result})

    # 引用表完整性（cited_not_listed / uncited / duplicated / 不可解析标记）
    audit_result: dict[str, Any] | None = None
    try:
        audit_result = run_skill(
            "cite-manager", {"text": text, "language": language, "mode": "audit"}
        )
        sources.append(
            {
                "skill": "cite-manager",
                "result": {"findings": _cite_audit_findings(audit_result.get("audit") or {})},
            }
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("cite-manager audit 失败: %s", exc)

    # 文献支撑缺口（缺引 / 陈旧 / 条目信息不全；纯离线确定性检测，不联网）。
    # 不传 audit：正文引用越界已由 cite-manager 来源覆盖，避免同一缺陷双计。
    try:
        with tempfile.TemporaryDirectory(prefix="litgap-") as tmp:
            lit_result = run_skill(
                "lit-search",
                {
                    "mode": "gap",
                    "text": text,
                    "language": language,
                    "references": _references_section(text),
                    "out": tmp,
                },
            )
        sources.append(
            {
                "skill": "lit-search",
                "result": {"findings": _lit_gap_findings(lit_result.get("gaps") or [])},
            }
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("lit-search gap 检测失败: %s", exc)

    # 跨节一致性（图表引用完整性 / 摘要数字一致 / 结论实体一致）
    try:
        consistency_findings = consistency.check_consistency(text, language=language)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("consistency-check 失败: %s", exc)
        consistency_findings = []
    sources.append(
        {"skill": "consistency-check", "result": {"findings": consistency_findings}}
    )

    # 统计报告一致性（statcheck + GRIM，纯确定性重算，不联网不调模型）
    try:
        stat_result = stat_checks.check_text(text)
        sources.append(
            {"skill": "stat-check", "result": {"findings": stat_result.get("findings") or []}}
        )
    except Exception as exc:  # noqa: BLE001 - 单项检查失败不阻塞登记
        LOG.warning("stat-check 失败: %s", exc)
    return sources


def _extract_findings(skill: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """从各 Skill 的结果里取出 findings 列表，兼容三种键名。"""
    for key in ("findings", "violations"):
        value = result.get(key)
        if isinstance(value, list):
            return value
    # lit-verify：results[*].findings，需带上外层 entry 供定位；
    # 优先级应由外层 verdict（rejected/unverified）决定，而非单条 finding 的 field，
    # 因此把 verdict 提为 check，并据 verdict 设定 severity。
    nested: list[dict[str, Any]] = []
    verdict_severity = {"rejected": "error", "unverified": "warning"}
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        entry = item.get("entry") or {}
        location = str(entry.get("number") or entry.get("title") or "")
        verdict = str(item.get("verdict") or "").lower()
        for finding in item.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            enriched = dict(finding)
            if verdict:
                enriched["check"] = verdict
                enriched.setdefault("severity", verdict_severity.get(verdict, "warning"))
            enriched.setdefault("_location", location)
            nested.append(enriched)
    return nested


def enforce_review_sim_guards(registry: dict[str, Any]) -> dict[str, Any]:
    """review-sim 硬门禁：模拟评审意见永不参与自动改写。

    所有 source=review-sim 的问题强制：
    - confidence="low"（模拟评审只是低置信度建议）；
    - status="needs_human"（人工签核前不进改写管线）；
    - metadata.requires_human=True（revision-planner 据此二次拦截）。
    该属性由本函数硬编码，调用方无法通过输入数据绕过。
    """
    for issue in registry.get("issues") or []:
        if str(issue.get("source")) != "review-sim":
            continue
        issue["confidence"] = "low"
        issue["status"] = "needs_human"
        metadata = issue.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            issue["metadata"] = metadata
        metadata["requires_human"] = True
    return registry


def build_registry(
    sources: list[dict[str, Any]], *, rules: dict[str, Any] | None = None
) -> dict[str, Any]:
    rules = rules or issue_model.load_issue_rules()
    issues: list[issue_model.Issue] = []
    sequence = 1
    for source in sources:
        skill = str(source.get("skill") or "unknown")
        result = source.get("result") or {}
        location = str(source.get("location") or "")
        for raw in _extract_findings(skill, result):
            if not isinstance(raw, dict):
                continue
            issue = issue_model.normalize_finding(
                rules,
                skill,
                raw,
                sequence=sequence,
                location=location or str(raw.get("_location") or ""),
            )
            sequence += 1
            issues.append(issue)

    deduped, removed = issue_model.dedupe(issues)
    registry = {
        "schema_version": SCHEMA_VERSION,
        "issue_count": len(deduped),
        "dedup_removed": removed,
        "sources_consulted": [str(s.get("skill") or "unknown") for s in sources],
        "counts_by_priority": issue_model.counts_by_priority(deduped),
        "counts_by_dimension": issue_model.counts_by_dimension(deduped),
        "blocking": [i.issue_id for i in deduped if i.priority == "P0"],
        "issues": [issue.to_dict() for issue in deduped],
    }
    return enforce_review_sim_guards(registry)


def render_markdown(registry: dict[str, Any]) -> str:
    counts = registry.get("counts_by_priority") or {}
    lines = [
        "# 问题登记表（Issue Registry）",
        "",
        f"- 问题总数：{registry.get('issue_count')}（去重移除 {registry.get('dedup_removed')} 条）",
        f"- 优先级分布："
        + "，".join(f"{p} {counts.get(p, 0)}" for p in issue_model.PRIORITY_ORDER),
        "",
        "| 编号 | 优先级 | 维度 | 来源 | 问题 | 位置 | 状态 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for issue in registry.get("issues") or []:
        lines.append(
            f"| {issue['issue_id']} | {issue['priority']} | {issue['dimension']} "
            f"| {issue['source']} | {issue['title'][:60]} | {issue.get('location') or '-'} "
            f"| {issue.get('status', 'open')} |"
        )
    return "\n".join(lines) + "\n"


def run(args: dict[str, Any]) -> dict[str, Any]:
    language = normalize_language(str(args.get("language", "cn")))
    rules_path = args.get("rules") or issue_model.RULES_PATH
    rules = issue_model.load_issue_rules(str(rules_path))

    sources = args.get("sources")
    output = args.get("out") or args.get("output")
    if sources:
        source_list = [s for s in sources if isinstance(s, dict)]
    else:
        text = args.get("text")
        text_file = args.get("text_file")
        if text is None and text_file:
            text = Path(str(text_file)).read_text(encoding="utf-8-sig")
        if text is None:
            raise ValueError("issue-registry 需要 sources 或 text_file/text")
        source_list = _collect_from_text(str(text), language)

    registry = build_registry(source_list, rules=rules)

    if output:
        out_dir = Path(str(output))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "issue-registry.json").write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "issue-registry.md").write_text(
            render_markdown(registry), encoding="utf-8"
        )
        registry["output_dir"] = str(out_dir)
        registry["registry_file"] = str(out_dir / "issue-registry.json")
    return registry
