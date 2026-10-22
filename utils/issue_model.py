"""问题对象模型：把各审计 Skill 的 findings 归一为统一 Issue。

PRD §2.2 要求"每一轮修改都需要保存修改的问题编号"，PRD §5 阶段二要求
"每个问题分配唯一编号，方便跨轮次追踪"——这就是那套编号与优先级的实现。

设计约束（遵循 AGENTS.md）：
- 规则全部来自 config/issue-rules.yaml，本模块不含硬编码阈值；
- 纯标准库、纯函数，可被 issue-registry / quality-score / revision-planner 共用；
- 这是全项目第二个强类型数据契约（第一个是 utils/chunk_contract.ChunkRef）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from utils.config_loader import load_yaml_file

RULES_PATH = "config/issue-rules.yaml"

# 优先级顺序：数字越小越紧急
PRIORITY_ORDER = ("P0", "P1", "P2", "P3")
PRIORITY_LABEL = {
    "P0": "阻断级",
    "P1": "重大问题",
    "P2": "一般问题",
    "P3": "细节问题",
}


@dataclass
class Issue:
    """一个可追踪的论文问题。"""

    issue_id: str
    source: str
    priority: str
    dimension: str
    severity: str
    title: str
    detail: str = ""
    location: str = ""
    evidence: str = ""
    confidence: str = "medium"
    suggestion: str = ""
    fingerprint: str = ""
    status: str = "open"  # open / fixed / deferred / needs_human
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (PRIORITY_ORDER.index(self.priority), self.dimension, self.issue_id)


def load_issue_rules(path: str = RULES_PATH) -> dict[str, Any]:
    return load_yaml_file(path)


def _cfg(rules: dict[str, Any], key: str, default: Any = None) -> Any:
    return rules.get(key, default)


def _override_table(rules: dict[str, Any], name: str) -> dict[str, str]:
    """取覆写表并归一化键名。

    项目自带的极简 YAML 解析器会把键上的引号保留进键名
    （实测 ``"claim-guard::absolute"`` 解析成 ``'"claim-guard::absolute"'``），
    因此这里统一剥掉成对引号，避免覆写规则静默失效。
    """
    table = _cfg(rules, name) or {}
    return {str(key).strip().strip('"').strip("'"): str(value) for key, value in table.items()}


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _match_key(rules: dict[str, Any], table: str, source: str, check: str) -> str | None:
    """按 source::check 精确命中覆写表，未命中返回 None。"""
    overrides = _override_table(rules, table)
    for candidate in (f"{source}::{check}", f"{source}::{check.split('.')[0]}"):
        if candidate in overrides:
            return overrides[candidate]
    return None


def resolve_priority(
    rules: dict[str, Any], source: str, check: str, severity: str
) -> str:
    """严重度 + 来源 + check → P0-P3。"""
    override = _match_key(rules, "priority_overrides", source, check)
    if override in PRIORITY_ORDER:
        return override
    mapping = _cfg(rules, "default_severity_to_priority") or {}
    priority = str(mapping.get(str(severity).lower(), "P2"))
    return priority if priority in PRIORITY_ORDER else "P2"


def resolve_dimension(rules: dict[str, Any], source: str, check: str) -> str:
    """来源 + check → 质量维度。"""
    override = _match_key(rules, "dimension_overrides", source, check)
    if override:
        return str(override)
    dimensions = _cfg(rules, "dimensions") or {}
    mapped = str((_cfg(rules, "source_dimension") or {}).get(source, "review_risk"))
    return mapped if mapped in dimensions else "review_risk"


def make_fingerprint(source: str, check: str, found: str, context: str) -> str:
    """稳定指纹：同一问题跨轮次、跨副本都得到同一指纹，用于去重与跨轮追踪。"""
    chars = int((_cfg(load_issue_rules(), "dedup") or {}).get("context_chars", 60))
    payload = "|".join(
        [source, _norm_text(check).lower(), _norm_text(found).lower(), _norm_text(context)[:chars]]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def make_issue_id(fingerprint: str, sequence: int) -> str:
    """人类可读的稳定编号：ISS-<指纹前8>-<序号>。"""
    return f"ISS-{fingerprint[:8]}-{sequence:03d}"


def normalize_finding(
    rules: dict[str, Any],
    source: str,
    raw: dict[str, Any],
    *,
    sequence: int,
    location: str = "",
) -> Issue:
    """把任一来源的原始 finding 归一为 Issue。

    支持四种既有 findings 结构：
    - review-gate:    {check, severity, message, evidence{...}}
    - claim-guard:    {id, found, issue, severity, suggestion, context_snippet}
    - term-check:     {term_id, found, canonical, issue, severity, suggestion, context_snippet}
    - lit-verify:     {level, field, message, detail}（外层带 entry/verdict）
    """
    check = str(
        raw.get("check")
        or raw.get("issue")
        or raw.get("field")
        or raw.get("term_id")
        or raw.get("id")
        or "unknown"
    )
    severity = str(raw.get("severity") or raw.get("level") or "warning").lower()
    found = _norm_text(raw.get("found") or raw.get("canonical") or raw.get("field") or "")
    issue_kind = _norm_text(raw.get("issue") or "")
    context = _norm_text(
        raw.get("context_snippet") or raw.get("detail") or raw.get("message") or ""
    )
    # 标题描述"问题是什么"，suggestion 单独存放"怎么改"
    message = _norm_text(raw.get("message") or "")
    if message:
        title = message
    elif found and issue_kind:
        title = f"{found}（{issue_kind}）"
    elif found:
        title = found
    else:
        title = check
    if len(title) > 120:
        title = title[:117] + "..."

    priority = resolve_priority(rules, source, check, severity)
    dimension = resolve_dimension(rules, source, check)
    fingerprint = make_fingerprint(source, check, found, context)
    return Issue(
        issue_id=make_issue_id(fingerprint, sequence),
        source=source,
        priority=priority,
        dimension=dimension,
        severity=severity,
        title=title,
        detail=_norm_text(raw.get("detail") or "")[:400],
        location=location,
        evidence=context[:300],
        confidence=str(raw.get("confidence") or "medium"),
        suggestion=_norm_text(raw.get("suggestion") or "")[:300],
        fingerprint=fingerprint,
        metadata={
            "check": check,
            "found": found,
            "occurrences": raw.get("occurrences"),
        },
    )


def normalize_findings(
    rules: dict[str, Any],
    source: str,
    findings: Iterable[dict[str, Any]],
    *,
    location: str = "",
    start_sequence: int = 1,
) -> list[Issue]:
    return [
        normalize_finding(rules, source, raw, sequence=start_sequence + index, location=location)
        for index, raw in enumerate(findings)
    ]


def dedupe(issues: list[Issue]) -> tuple[list[Issue], int]:
    """按指纹去重，返回 (去重后列表, 移除条数)。"""
    seen: dict[str, Issue] = {}
    removed = 0
    for issue in issues:
        if issue.fingerprint in seen:
            removed += 1
            # 保留更高优先级的那条
            if PRIORITY_ORDER.index(issue.priority) < PRIORITY_ORDER.index(
                seen[issue.fingerprint].priority
            ):
                seen[issue.fingerprint] = issue
            continue
        seen[issue.fingerprint] = issue
    return sorted(seen.values(), key=lambda i: i.sort_key), removed


def sort_issues(issues: list[Issue]) -> list[Issue]:
    return sorted(issues, key=lambda i: i.sort_key)


def counts_by_priority(issues: list[Issue]) -> dict[str, int]:
    counts = {priority: 0 for priority in PRIORITY_ORDER}
    for issue in issues:
        counts[issue.priority] = counts.get(issue.priority, 0) + 1
    return counts


def counts_by_dimension(issues: list[Issue]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.dimension] = counts.get(issue.dimension, 0) + 1
    return counts


def covered_dimensions(rules: dict[str, Any], sources: Iterable[str]) -> set[str]:
    """本次实际运行的检查所能覆盖的质量维度。

    没有跑过对应检查的维度不能打满分——否则"没问题"会被伪装成"查过没问题"。
    这与 PRD §4.4「防范评价体系被钻空子」、§8.2「评分置信度及证据覆盖情况」对应。
    """
    source_dimension = _cfg(rules, "source_dimension") or {}
    overrides = _override_table(rules, "dimension_overrides")
    covered: set[str] = set()
    for source in sources:
        clean = str(source)
        if clean in source_dimension:
            covered.add(str(source_dimension[clean]))
        prefix = f"{clean}::"
        for key, dimension in overrides.items():
            if key.startswith(prefix):
                covered.add(dimension)
    return covered
