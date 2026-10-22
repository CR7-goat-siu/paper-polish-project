"""Issue Registry：把各审计 Skill 的 findings 归一为带编号、带优先级的问题清单。

PRD 对应条款：
- §5 阶段二「识别与排序问题」：按严重程度/影响范围排序，问题分 P0-P3；
- §2.2「所有实质性修改必须可追溯」：每个问题分配唯一编号，跨轮次追踪；
- §9.2 Manuscript Auditor 的问题清单部分。

输入（二选一）：
- `sources`：已算好的审计结果列表，每项 `{skill, result, location?}`；
- `text_file` + `language`：由本 Skill 自行调用 claim-guard / term-check / review-gate
  做确定性检查（离线，不调模型）。

输出：统一 registry，含 issue_id、priority（P0-P3）、dimension、指纹与排序。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import issue_model  # noqa: E402
from utils.config_loader import normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from utils.skill_loader import run_skill  # noqa: E402

LOG = get_logger("issue-registry")

SCHEMA_VERSION = 1


def _collect_from_text(text: str, language: str) -> list[dict[str, Any]]:
    """自行跑确定性检查，产出 (skill, result) 对。"""
    sources: list[dict[str, Any]] = []
    for skill in ("claim-guard", "term-check"):
        try:
            result = run_skill(skill, {"text": text, "language": language, "mode": "local"})
        except Exception as exc:  # noqa: BLE001 - 单项检查失败不阻塞登记
            LOG.warning("%s 检查失败: %s", skill, exc)
            continue
        sources.append({"skill": skill, "result": result})
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
    return {
        "schema_version": SCHEMA_VERSION,
        "issue_count": len(deduped),
        "dedup_removed": removed,
        "sources_consulted": [str(s.get("skill") or "unknown") for s in sources],
        "counts_by_priority": issue_model.counts_by_priority(deduped),
        "counts_by_dimension": issue_model.counts_by_dimension(deduped),
        "blocking": [i.issue_id for i in deduped if i.priority == "P0"],
        "issues": [issue.to_dict() for issue in deduped],
    }


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

    output = args.get("out") or args.get("output")
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
