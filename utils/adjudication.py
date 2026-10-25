"""人工裁决账本读取与 precision 门禁（Release Protocol 第 4 条件）。

架构评审（2026-10-25 二轮第 3 条）：裁决协议此前只在文档里，账本没有任何
代码消费。本模块把 docs/adjudication-protocol.md 的判定落成确定性门禁：
- load_ledger：解析 adjudication-ledger.csv（列见协议 §3），统计
  TP / FP / UNCLEAR 与逐来源 precision；
- precision_gate：已裁决条目（TP+FP）≥ min_adjudicated 且
  overall precision ≥ min_precision 才通过；UNCLEAR 不入分母，单独报告。

纯标准库；不访问网络；不做任何语义判断——裁决值完全来自人工填写。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

VALID_ADJUDICATIONS = ("TP", "FP", "UNCLEAR")


def load_ledger(path: str | Path) -> dict[str, Any]:
    """读取裁决账本并做有效性审计（三轮评审第 3 条）。

    除计数外还校验：issue_id 唯一性（重复 → duplicates，门禁 fail-closed）、
    adjudicator 署名非空（空白 → unsigned，该行无效）、来源覆盖
    （distinct_sources）、以及可选 manuscript_sha256 列的版本绑定集合。
    """
    target = Path(path)
    if not target.exists():
        return {
            "path": str(target),
            "missing": True,
            "rows": 0,
            "adjudicated": 0,
            "tp": 0,
            "fp": 0,
            "unclear": 0,
            "precision_overall": None,
            "precision_by_source": {},
            "unresolved": 0,
            "duplicates": 0,
            "unsigned": 0,
            "distinct_sources": 0,
            "manuscript_sha256_set": [],
        }
    rows = list(csv.DictReader(target.open(encoding="utf-8-sig")))
    tp = fp = unclear = unresolved = unsigned = 0
    duplicates = 0
    by_source: dict[str, dict[str, int]] = {}
    seen_ids: set[str] = set()
    sha_set: set[str] = set()
    for row in rows:
        verdict = str(row.get("adjudication") or "").strip().upper()
        issue_id = str(row.get("issue_id") or "").strip()
        adjudicator = str(row.get("adjudicator") or "").strip()
        source = str(row.get("source") or "").strip() or "unknown"
        sha = str(row.get("manuscript_sha256") or "").strip()
        if sha:
            sha_set.add(sha)
        if verdict not in VALID_ADJUDICATIONS:
            unresolved += 1
            continue
        if issue_id and issue_id in seen_ids:
            duplicates += 1  # 同一 issue 的重复裁决 → 唯一性破坏（先于署名检查）
            continue
        if issue_id:
            seen_ids.add(issue_id)
        if not adjudicator:
            unsigned += 1  # 无署名的裁决是无效证据
            continue
        if verdict == "TP":
            tp += 1
        elif verdict == "FP":
            fp += 1
        else:
            unclear += 1
        bucket = by_source.setdefault(source, {"tp": 0, "fp": 0})
        if verdict == "TP":
            bucket["tp"] += 1
        elif verdict == "FP":
            bucket["fp"] += 1
    adjudicated = tp + fp
    precision_by_source = {
        source: round(counts["tp"] / (counts["tp"] + counts["fp"]), 4)
        for source, counts in by_source.items()
        if counts["tp"] + counts["fp"] > 0
    }
    return {
        "path": str(target),
        "missing": False,
        "rows": len(rows),
        "adjudicated": adjudicated,
        "tp": tp,
        "fp": fp,
        "unclear": unclear,
        "precision_overall": round(tp / adjudicated, 4) if adjudicated else None,
        "precision_by_source": precision_by_source,
        "unresolved": unresolved,
        "duplicates": duplicates,
        "unsigned": unsigned,
        "distinct_sources": len(
            {s for s, c in by_source.items() if c["tp"] + c["fp"] > 0}
        ),
        "manuscript_sha256_set": sorted(sha_set),
    }


def precision_gate(
    ledger_path: str | Path,
    *,
    min_adjudicated: int = 5,
    min_precision: float = 0.8,
    min_sources: int = 2,
    expected_manuscript_sha256: str | None = None,
) -> dict[str, Any]:
    """人工裁决门禁（三轮评审第 3 条收紧）。

    通过条件（全部满足）：
    - 账本存在；issue_id 无重复（duplicates == 0）；无空白署名裁决（unsigned == 0）；
    - 已裁决（TP+FP）≥ min_adjudicated 且覆盖 ≥ min_sources 个来源；
    - 整体 precision ≥ min_precision；
    - expected_manuscript_sha256 提供时，账本必须绑定同一稿件版本。
    """
    base = {
        "min_adjudicated": min_adjudicated,
        "min_precision": min_precision,
    }
    state = load_ledger(ledger_path)
    detail = {**base, "min_sources": min_sources, "ledger": state}
    if state["missing"]:
        return {"pass": False, "reason": f"裁决账本不存在: {state['path']}", **detail}
    if state["duplicates"]:
        return {
            "pass": False,
            "reason": f"issue_id 重复 {state['duplicates']} 条——裁决证据不唯一",
            **detail,
        }
    if state["unsigned"]:
        return {
            "pass": False,
            "reason": f"{state['unsigned']} 条裁决缺少 adjudicator 署名——无效证据",
            **detail,
        }
    if expected_manuscript_sha256:
        bound = state["manuscript_sha256_set"]
        if not bound:
            return {
                "pass": False,
                "reason": "账本未绑定稿件版本（缺 manuscript_sha256 列）",
                **detail,
            }
        if expected_manuscript_sha256 not in bound:
            return {
                "pass": False,
                "reason": "账本绑定的稿件哈希与当前版本不一致（裁决对象不是本稿）",
                **detail,
            }
    if state["distinct_sources"] < min_sources:
        return {
            "pass": False,
            "reason": (
                f"裁决仅覆盖 {state['distinct_sources']} 个来源，"
                f"少于多来源要求 {min_sources}"
            ),
            **detail,
        }
    if state["adjudicated"] < min_adjudicated:
        return {
            "pass": False,
            "reason": (
                f"已裁决条目 {state['adjudicated']} 不足 {min_adjudicated}"
                f"（未裁决 {state['unresolved']} / UNCLEAR {state['unclear']} 不计）"
            ),
            **detail,
        }
    precision = state["precision_overall"]
    if precision is None or precision < min_precision:
        return {
            "pass": False,
            "reason": (
                f"整体 precision {precision} 低于 {min_precision}"
                f"（TP {state['tp']} / FP {state['fp']}）"
            ),
            **detail,
        }
    return {
        "pass": True,
        "reason": (
            f"已裁决 {state['adjudicated']} 条 / {state['distinct_sources']} 来源，"
            f"precision {precision} ≥ {min_precision}"
        ),
        **detail,
    }
