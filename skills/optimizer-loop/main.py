"""Optimizer Loop：自主迭代式论文质量优化主循环（PRD §5 / §7 / §8）。

这是 PRD 里缺的最后一块：把「审计 → 规划 → 修改 → 独立复核 → 版本决策 → 停止」
组成闭环，而不是顺序调用几个提示词。

闭环逻辑（对应 PRD 五个阶段）：
  阶段一 建立基线   parse → issue-registry → quality-score → revision-plan，存初始快照
  阶段二/三 识别排序与规划   由 issue-registry / revision-planner 产出
  阶段四 执行修改   batch-processor build → 回填响应 → apply（局部补丁，不重写全文）
  阶段五 独立复核   对新版本重跑 issue-registry + quality-score（确定性，非模型自评）
  阶段六 版本决策   比较新旧：无新增 P0/P1 且质量不退化才接受，否则拒绝并回滚到历史最佳
  阶段七 下一轮决策 继续 / 转向 / 收尾，由停止条件判定

停止条件（PRD §7.1）：
  - 无未处理的 P0/P1 问题；
  - 达到配置的最低质量阈值；
  - 连续 N 轮无有意义净改善（收益停滞）；
  - 达到最大轮数。
满足任一条即进入交付，不保证发表。

交付物（PRD §8）：quality_report.md / evidence_audit.md / iteration_log.json /
submission_readiness.md（三级状态，不用模糊绿灯）。

响应来源：`responses_dir` 提供已回填响应；`identity` 用恒等响应（用于离线验证
闭环机制本身，不产生真实润色）。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import issue_model  # noqa: E402
from utils.config_loader import normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from utils.skill_loader import run_skill  # noqa: E402

LOG = get_logger("optimizer-loop")

SCHEMA_VERSION = 1

# 投稿准备状态（PRD §8.5）：只用明确分级，不用模糊绿灯
READY = "READY_FOR_AUTHOR_REVIEW"
CONDITIONAL = "CONDITIONALLY_READY"
BLOCKED = "BLOCKED"


# --------------------------------------------------------------------------- #
# 单轮：审计 + 评分 + 规划
# --------------------------------------------------------------------------- #
def _load_source(text_file: Path) -> tuple[str, Path]:
    """返回 (用于审计的全文文本, 传给 batch-processor 的输入路径)。

    docx 是二进制，不能直接 read_text：用 paper-parser 取全文做审计，
    而 build/apply 仍传原始 docx——batch-processor 自己会解析并回写 docx，
    这样终稿才能保留公式、表格与图表。
    """
    if text_file.suffix.lower() in (".docx", ".doc"):
        parsed = run_skill("paper-parser", {"input": str(text_file), "format": "auto"})
        return str(parsed.get("full_text") or ""), text_file
    return text_file.read_text(encoding="utf-8-sig"), text_file


def audit_round(text: str, *, language: str, out_dir: Path) -> dict[str, Any]:
    """对新版本文本做一次完整确定性审计，返回 registry / scorecard / plan。"""
    registry = run_skill(
        "issue-registry", {"text": text, "language": language, "out": str(out_dir)}
    )
    scorecard = run_skill(
        "quality-score",
        {"registry": registry, "language": language, "out": str(out_dir)},
    )
    plan = run_skill(
        "revision-planner",
        {
            "registry": registry,
            "scorecard": scorecard,
            "language": language,
            "out": str(out_dir),
        },
    )
    return {"registry": registry, "scorecard": scorecard, "plan": plan}


def _priority_counts(audit: dict[str, Any]) -> dict[str, int]:
    return audit["registry"].get("counts_by_priority") or {}


def _blocking(audit: dict[str, Any]) -> list[str]:
    """未处理的 P0/P1 问题编号（硬门槛与停止条件都用它）。"""
    return [
        issue["issue_id"]
        for issue in audit["registry"].get("issues", [])
        if issue.get("priority") in ("P0", "P1")
    ]


def _quality_index(audit: dict[str, Any]) -> float:
    value = audit["scorecard"].get("weighted_index")
    return float(value) if value is not None else 0.0


# --------------------------------------------------------------------------- #
# 阶段六：版本决策
# --------------------------------------------------------------------------- #
def decide(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    min_gain: float,
) -> dict[str, Any]:
    """比较新旧版本，给出接受/拒绝结论与理由。

    PRD §5 阶段六：语言更流畅但引入未经支持的主张 → 必须拒绝；
    只在部分指标提升 → 结合问题严重程度与硬约束判断，不简单取分数最高者。
    """
    if previous is None:
        return {"decision": "accept", "reason": "首轮基线，作为历史最佳版本"}

    prev_blocking = set(_blocking(previous))
    curr_blocking = set(_blocking(current))
    new_blocking = sorted(curr_blocking - prev_blocking)

    # 硬约束：新增 P0/P1 一律拒绝（PRD 测试 G：修改回归必须被拒绝或回滚）
    if new_blocking:
        return {
            "decision": "reject",
            "reason": f"引入新的 P0/P1 问题 {new_blocking}，按硬约束拒绝并回滚",
            "new_blocking": new_blocking,
        }

    prev_index = _quality_index(previous)
    curr_index = _quality_index(current)
    gain = curr_index - prev_index
    prev_issues = previous["registry"].get("issue_count", 0)
    curr_issues = current["registry"].get("issue_count", 0)

    if curr_index < prev_index - 1e-9:
        return {
            "decision": "reject",
            "reason": f"加权指数由 {prev_index} 降至 {curr_index}，质量退化，回滚到历史最佳",
            "gain": round(gain, 3),
        }
    if gain >= min_gain or curr_issues < prev_issues:
        return {
            "decision": "accept",
            "reason": f"加权指数 {prev_index} → {curr_index}（+{round(gain, 3)}），"
            f"问题数 {prev_issues} → {curr_issues}，无新增 P0/P1",
            "gain": round(gain, 3),
        }
    return {
        "decision": "accept_no_gain",
        "reason": f"无新增 P0/P1，但净增益 {round(gain, 3)} 低于阈值 {min_gain}，记为零收益轮",
        "gain": round(gain, 3),
    }


# --------------------------------------------------------------------------- #
# 阶段七：停止条件
# --------------------------------------------------------------------------- #
def should_stop(
    *,
    audit: dict[str, Any],
    round_index: int,
    max_rounds: int,
    min_index: float,
    stagnant_rounds: int,
    stagnant_limit: int,
    recent_gains: list[float],
) -> tuple[bool, str]:
    if not _blocking(audit):
        return True, "不存在尚未处理的阻断级（P0/P1）问题"
    if _quality_index(audit) >= min_index:
        return True, f"加权指数达到最低阈值 {min_index}"
    if round_index >= stagnant_limit and stagnant_rounds >= stagnant_limit:
        return True, f"连续 {stagnant_rounds} 轮无有意义净改善，收益停滞"
    if round_index >= max_rounds:
        return True, f"达到最大迭代轮数 {max_rounds}"
    return False, ""


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #
def run_loop(
    text_file: Path,
    *,
    language: str,
    strength: str,
    out_dir: Path,
    max_rounds: int = 3,
    min_gain: float = 0.2,
    min_index: float = 3.5,
    stagnant_limit: int = 2,
    responses_dir: Path | None = None,
    identity: bool = False,
) -> dict[str, Any]:
    language = normalize_language(language)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    current_text, pipeline_input = _load_source(text_file)
    baseline_audit = audit_round(current_text, language=language, out_dir=work / "round-000")
    iteration_log: list[dict[str, Any]] = [
        {
            "round": 0,
            "kind": "baseline",
            "issue_count": baseline_audit["registry"]["issue_count"],
            "counts_by_priority": _priority_counts(baseline_audit),
            "weighted_index": _quality_index(baseline_audit),
            "hard_gate_pass": baseline_audit["scorecard"]["hard_gate"]["pass"],
        }
    ]
    best_text = current_text
    best_audit = baseline_audit
    recent_gains: list[float] = []
    stagnant = 0
    stop_reason = ""

    for round_index in range(1, max_rounds + 1):
        LOG.info("=== 第 %d 轮 ===", round_index)
        round_dir = work / f"round-{round_index:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)

        # 阶段四：执行修改（局部补丁：build → 回填 → apply）
        run_skill(
            "batch-processor",
            {
                "inputs": [str(pipeline_input)],
                "language": language,
                "phase": "build",
                "strength": strength,
                "out": str(round_dir),
            },
        )
        stem = text_file.stem
        run_root = round_dir / stem
        resp = run_root / "responses"
        resp.mkdir(parents=True, exist_ok=True)
        jobs = json.loads((run_root / "jobs.json").read_text(encoding="utf-8"))["jobs"]
        for job in jobs:
            target = resp / f"{job['job_id']}.txt"
            if responses_dir is not None and (responses_dir / f"{job['job_id']}.txt").exists():
                shutil.copy(responses_dir / f"{job['job_id']}.txt", target)
            elif identity:
                target.write_text(job["chunk_content"], encoding="utf-8")
        apply_result = run_skill(
            "batch-processor",
            {
                "inputs": [str(pipeline_input)],
                "language": language,
                "phase": "apply",
                "strength": strength,
                "out": str(round_dir),
            },
        )
        step_file = run_root / f"step-01-{language}-polish" / "step-result.json"
        candidate_text = (
            json.loads(step_file.read_text(encoding="utf-8")).get("polished_text")
            if step_file.exists()
            else None
        )
        if not candidate_text:
            iteration_log.append(
                {"round": round_index, "kind": "round", "status": "error", "reason": "apply 未产出终稿"}
            )
            stop_reason = "apply 未产出终稿，中止迭代"
            break

        # 阶段五：独立复核（确定性，不读模型自评）
        candidate_audit = audit_round(
            candidate_text, language=language, out_dir=round_dir / "audit"
        )
        verdict = decide(best_audit, candidate_audit, min_gain=min_gain)

        record = {
            "round": round_index,
            "kind": "round",
            "issue_count": candidate_audit["registry"]["issue_count"],
            "counts_by_priority": _priority_counts(candidate_audit),
            "weighted_index": _quality_index(candidate_audit),
            "hard_gate_pass": candidate_audit["scorecard"]["hard_gate"]["pass"],
            "decision": verdict["decision"],
            "reason": verdict["reason"],
        }
        iteration_log.append(record)

        if verdict["decision"] == "reject":
            LOG.info("拒绝本轮修改：%s", verdict["reason"])
            stagnant += 1
            recent_gains.append(0.0)
        else:
            best_text = candidate_text
            best_audit = candidate_audit
            current_text = candidate_text
            gain = float(verdict.get("gain") or 0.0)
            recent_gains.append(gain)
            stagnant = stagnant + 1 if abs(gain) < min_gain else 0

        stop, stop_reason = should_stop(
            audit=best_audit,
            round_index=round_index,
            max_rounds=max_rounds,
            min_index=min_index,
            stagnant_rounds=stagnant,
            stagnant_limit=stagnant_limit,
            recent_gains=recent_gains,
        )
        if stop:
            break

    # ---------------- 交付物（PRD §8） ----------------
    final_audit = best_audit
    final_text = best_text
    (out_dir / "final_manuscript.md").write_text(final_text, encoding="utf-8")
    (out_dir / "iteration_log.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "language": language,
                "strength": strength,
                "stop_reason": stop_reason,
                "rounds": iteration_log,
                "final": {
                    "issue_count": final_audit["registry"]["issue_count"],
                    "counts_by_priority": _priority_counts(final_audit),
                    "weighted_index": _quality_index(final_audit),
                    "hard_gate_pass": final_audit["scorecard"]["hard_gate"]["pass"],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    readiness = _readiness(final_audit, stop_reason)
    (out_dir / "submission_readiness.md").write_text(
        _render_readiness(readiness, final_audit, stop_reason, iteration_log), encoding="utf-8"
    )
    (out_dir / "quality_report.md").write_text(
        _render_quality_report(baseline_audit, final_audit, iteration_log, stop_reason),
        encoding="utf-8",
    )
    (out_dir / "evidence_audit.md").write_text(
        _render_evidence_audit(final_audit), encoding="utf-8"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": readiness["status"],
        "stop_reason": stop_reason,
        "final_manuscript": str(out_dir / "final_manuscript.md"),
        "iteration_log": str(out_dir / "iteration_log.json"),
        "deliverables": [
            "final_manuscript.md",
            "quality_report.md",
            "evidence_audit.md",
            "iteration_log.json",
            "submission_readiness.md",
        ],
        "final": {
            "issue_count": final_audit["registry"]["issue_count"],
            "counts_by_priority": _priority_counts(final_audit),
            "weighted_index": _quality_index(final_audit),
            "hard_gate_pass": final_audit["scorecard"]["hard_gate"]["pass"],
        },
    }


def run(args: dict[str, Any]) -> dict[str, Any]:
    """coding harness 统一入口。"""
    raw_input = args.get("text_file") or args.get("input")
    if not raw_input:
        raise ValueError("optimizer-loop 需要 text_file 或 input")
    responses_dir = args.get("responses_dir")
    return run_loop(
        Path(str(raw_input)),
        language=str(args.get("language") or "en"),
        strength=str(args.get("strength") or "deep"),
        out_dir=Path(str(args.get("out") or "output/optimizer-loop")),
        max_rounds=int(args.get("max_rounds") or 3),
        min_gain=float(args.get("min_gain", 0.2)),
        min_index=float(args.get("min_index", 3.5)),
        stagnant_limit=int(args.get("stagnant_limit") or 2),
        responses_dir=Path(str(responses_dir)) if responses_dir else None,
        identity=bool(args.get("identity", False)),
    )


def _readiness(audit: dict[str, Any], stop_reason: str) -> dict[str, Any]:
    """PRD §8.5 三级状态：不用模糊绿灯。"""
    blocking = _blocking(audit)
    counts = _priority_counts(audit)
    p0 = counts.get("P0", 0)
    p1 = counts.get("P1", 0)
    human = audit["plan"].get("requires_human") or []
    if p0 or not audit["scorecard"]["hard_gate"]["pass"]:
        detail = (
            f"存在 {p0} 个阻断级（P0）问题"
            if p0
            else f"科学真实性维度仍有 {p1} 个未处理的重大问题（硬门槛未通过）"
        )
        return {
            "status": BLOCKED,
            "reason": detail,
            "blocking_issues": blocking,
        }
    if p1 or human:
        return {
            "status": CONDITIONAL,
            "reason": f"剩余 {p1} 个重大问题、{len(human)} 项需作者确认，尚不能无条件进入投稿流程",
            "blocking_issues": blocking,
        }
    return {
        "status": READY,
        "reason": "自动检查已完成，未发现阻断级问题，但仍需作者审阅；不代表期刊一定录用",
        "blocking_issues": [],
    }


def _render_readiness(
    readiness: dict[str, Any], audit: dict[str, Any], stop_reason: str, log: list[dict[str, Any]]
) -> str:
    return "\n".join(
        [
            "# 投稿准备状态",
            "",
            f"**状态：{readiness['status']}**",
            "",
            f"- 判定依据：{readiness['reason']}",
            f"- 停止原因：{stop_reason or '未记录'}",
            f"- 迭代轮数：{max((r['round'] for r in log), default=0)}",
            f"- 加权质量指数：{_quality_index(audit)}（仅覆盖已评估维度）",
            f"- 维度覆盖率：{audit['scorecard'].get('coverage_ratio')}",
            "",
            "> READY_FOR_AUTHOR_REVIEW 不代表期刊一定会录用，也不代表系统已证明所有科学主张正确。",
            "",
        ]
    )


def _render_quality_report(
    baseline: dict[str, Any],
    final: dict[str, Any],
    log: list[dict[str, Any]],
    stop_reason: str,
) -> str:
    lines = [
        "# 质量审计报告",
        "",
        f"- 停止原因：{stop_reason or '未记录'}",
        "",
        "## 初稿 vs 终稿",
        "",
        "| 指标 | 初稿 | 终稿 |",
        "| --- | --- | --- |",
        f"| 问题总数 | {baseline['registry']['issue_count']} | {final['registry']['issue_count']} |",
        f"| P0 | {_priority_counts(baseline).get('P0', 0)} | {_priority_counts(final).get('P0', 0)} |",
        f"| P1 | {_priority_counts(baseline).get('P1', 0)} | {_priority_counts(final).get('P1', 0)} |",
        f"| P2 | {_priority_counts(baseline).get('P2', 0)} | {_priority_counts(final).get('P2', 0)} |",
        f"| P3 | {_priority_counts(baseline).get('P3', 0)} | {_priority_counts(final).get('P3', 0)} |",
        f"| 加权指数 | {_quality_index(baseline)} | {_quality_index(final)} |",
        f"| 科学真实性硬门槛 | {'通过' if baseline['scorecard']['hard_gate']['pass'] else '未通过'}"
        f" | {'通过' if final['scorecard']['hard_gate']['pass'] else '未通过'} |",
        "",
        "## 各轮记录",
        "",
        "| 轮次 | 问题数 | P0 | P1 | 加权指数 | 决策 | 理由 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in log:
        counts = record.get("counts_by_priority") or {}
        lines.append(
            f"| {record['round']} | {record.get('issue_count', '-')} "
            f"| {counts.get('P0', '-')} | {counts.get('P1', '-')} "
            f"| {record.get('weighted_index', '-')} | {record.get('decision', record.get('kind', '-'))} "
            f"| {str(record.get('reason', ''))[:50]} |"
        )
    lines += ["", "## 尚未解决的问题", ""]
    remaining = final["registry"].get("issues") or []
    if not remaining:
        lines.append("无。")
    for issue in remaining:
        lines.append(
            f"- [{issue['priority']}] {issue['issue_id']} {issue['title'][:60]}"
            f"（{issue['dimension']}，位置 {issue.get('location') or '-'}）"
        )
    lines += ["", "## 未评估维度（缺证据/缺材料，未给分）", ""]
    for dimension in final["scorecard"].get("not_assessed") or []:
        reason = (final["scorecard"].get("not_assessed_reasons") or {}).get(dimension, "")
        lines.append(f"- {dimension}：{reason}")
    return "\n".join(lines) + "\n"


def _render_evidence_audit(audit: dict[str, Any]) -> str:
    registry = audit["registry"]
    lines = [
        "# 事实与证据审计",
        "",
        f"- 本次运行的检查：{', '.join(registry.get('sources_consulted') or []) or '无'}",
        f"- 科学真实性硬门槛：{'通过' if audit['scorecard']['hard_gate']['pass'] else '未通过'}",
        "",
        "## 按维度汇总",
        "",
        "| 维度 | 分数 | 未解决问题 |",
        "| --- | --- | --- |",
    ]
    for dimension, entry in audit["scorecard"]["scores"].items():
        score = entry.get("score")
        lines.append(
            f"| {dimension} | {'未评估' if score is None else f'{score}/4'} "
            f"| {entry.get('issue_count', 0)} |"
        )
    lines += ["", "## 需作者确认的事项", ""]
    for task in audit["plan"].get("requires_human") or []:
        lines.append(f"- {task['task_id']}：{task['defect'][:90]}（{task.get('reason', '')}）")
    lines += ["", "## 缺材料阻断的事项", ""]
    for task in audit["plan"].get("blocked_by_materials") or []:
        lines.append(f"- {task['task_id']}：{task['defect'][:90]}（{task.get('reason', '')}）")
    if not (audit["plan"].get("requires_human") or audit["plan"].get("blocked_by_materials")):
        lines.append("无。")
    return "\n".join(lines) + "\n"
