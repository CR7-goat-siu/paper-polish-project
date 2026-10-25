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
    """读取裁决账本，返回统计结构；文件不存在返回空态（missing=True）。"""
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
        }
    rows = list(csv.DictReader(target.open(encoding="utf-8-sig")))
    tp = fp = unclear = unresolved = 0
    by_source: dict[str, dict[str, int]] = {}
    for row in rows:
        verdict = str(row.get("adjudication") or "").strip().upper()
        source = str(row.get("source") or "").strip() or "unknown"
        if verdict == "TP":
            tp += 1
        elif verdict == "FP":
            fp += 1
        elif verdict == "UNCLEAR":
            unclear += 1
        else:
            unresolved += 1
            continue
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
    }


def precision_gate(
    ledger_path: str | Path,
    *,
    min_adjudicated: int = 5,
    min_precision: float = 0.8,
) -> dict[str, Any]:
    """人工裁决门禁：通过条件 = 已裁决 ≥ min_adjudicated 且 precision 达标。"""
    state = load_ledger(ledger_path)
    if state["missing"]:
        return {
            "pass": False,
            "reason": f"裁决账本不存在: {state['path']}",
            "ledger": state,
            "min_adjudicated": min_adjudicated,
            "min_precision": min_precision,
        }
    if state["adjudicated"] < min_adjudicated:
        return {
            "pass": False,
            "reason": (
                f"已裁决条目 {state['adjudicated']} 不足 {min_adjudicated}"
                f"（未裁决 {state['unresolved']} / UNCLEAR {state['unclear']} 不计）"
            ),
            "ledger": state,
            "min_adjudicated": min_adjudicated,
            "min_precision": min_precision,
        }
    precision = state["precision_overall"]
    if precision is None or precision < min_precision:
        return {
            "pass": False,
            "reason": (
                f"整体 precision {precision} 低于 {min_precision}"
                f"（TP {state['tp']} / FP {state['fp']}）"
            ),
            "ledger": state,
            "min_adjudicated": min_adjudicated,
            "min_precision": min_precision,
        }
    return {
        "pass": True,
        "reason": f"已裁决 {state['adjudicated']} 条，precision {precision} ≥ {min_precision}",
        "ledger": state,
        "min_adjudicated": min_adjudicated,
        "min_precision": min_precision,
    }
