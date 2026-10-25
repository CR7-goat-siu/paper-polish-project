"""Optimizer Loop：自主迭代式论文质量优化主循环（PRD §5 / §7 / §8）。

把「审计 → 规划 → 修改 → 独立复核 → 版本决策 → 停止」组成可断点续跑的
状态机（PRD 五个阶段），并以「计划驱动修订」替代全文重写：每轮只对
问题所在章节出作业（only_chunks），把审计问题作为最高优先级批注注入
prompt（focus_issues），按问题优先级选强度（chunk_strength）。

闭环逻辑（对应 PRD 五个阶段）：
  阶段一 建立基线   init：parse → issue-registry → quality-score → revision-plan
  阶段二/三 规划     由 issue-registry / revision-planner 产出；build 阶段消费计划
  阶段四 执行修改   build（选择性作业包）→ coding harness 回填 → apply（门禁+装配）
  阶段五 独立复核   decide：对新版本重跑全部确定性审计（非模型自评）
  阶段六 版本决策   无新增 P0/P1 且质量不退化才接受，否则拒绝并回滚到历史最佳；
                    同时按指纹做任务级验证（fixed / still_open）
  阶段七 停止条件   无未处理 P0/P1 / 达到质量阈值 / 收益停滞 / 最大轮数

两种驱动方式（模型交互始终由 coding harness 完成，本 Skill 不调模型）：
- 分步模式：run({"stage": "init|build|apply|decide|deliver|status", ...})。
  build 之后状态为 awaiting_responses，harness 读取 pending_jobs 回填
  responses/<job_id>.txt，再调用 stage=apply（可随时中断、按 run-state.json 续跑）。
- 整体模式：不传 stage（或直接调用 run_loop），用 identity 恒等响应离线验证
  闭环机制本身；responses_dir 提供已回填响应时走真实润色。

停止条件满足只给投稿准备状态，**不保证发表**；作者/通讯作者签核仍是硬门禁。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import issue_model, job_io  # noqa: E402
from utils.config_loader import PROJECT_ROOT, load_yaml_file, normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from utils.skill_loader import run_skill  # noqa: E402

LOG = get_logger("optimizer-loop")

SCHEMA_VERSION = 1
STATE_VERSION = 3
VALID_STAGES = ("init", "audit", "build", "apply", "decide", "review", "deliver", "status")

REVIEW_SIM_CONFIG_PATH = PROJECT_ROOT / "config" / "review-sim.yaml"

# 投稿准备状态（PRD §8.5）：只用明确分级，不用模糊绿灯
READY = "READY_FOR_AUTHOR_REVIEW"
CONDITIONAL = "CONDITIONALLY_READY"
BLOCKED = "BLOCKED"

# 与 batch-processor 的 POLISHABLE_KINDS 保持同义的本地副本（跨 Skill 禁止 import 实现）
_POLISHABLE_KINDS = {"body", "abstract", "appendix", "section"}


# --------------------------------------------------------------------------- #
# 基础工具
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


def audit_round(
    text: str,
    *,
    language: str,
    out_dir: Path,
    extra_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """对新版本文本做一次完整确定性审计，返回 registry / scorecard / plan。

    extra_sources：追加的审计来源（对抗闭环注入 review-sim 评审意见），
    与确定性审计同场登记，保证跨轮计数、计划与 readiness 口径一致。
    """
    registry_args: dict[str, Any] = {"text": text, "language": language, "out": str(out_dir)}
    if extra_sources:
        registry_args["extra_sources"] = extra_sources
    registry = run_skill("issue-registry", registry_args)
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


def _load_audit(audit_dir: Path) -> dict[str, Any]:
    """从审计目录恢复 {registry, scorecard, plan}（三个 JSON 均已落盘）。"""
    audit_dir = Path(audit_dir)
    registry = json.loads(
        (audit_dir / "issue-registry.json").read_text(encoding="utf-8")
    )
    scorecard = json.loads(
        (audit_dir / "quality-scorecard.json").read_text(encoding="utf-8")
    )
    plan = json.loads((audit_dir / "revision-plan.json").read_text(encoding="utf-8"))
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


def _round_record(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_count": audit["registry"]["issue_count"],
        "counts_by_priority": _priority_counts(audit),
        "weighted_index": _quality_index(audit),
        "hard_gate_pass": audit["scorecard"]["hard_gate"]["pass"],
    }


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

    "新增"按违规种类（source::check::found，上下文无关）判定：issue_id 序号
    跨轮漂移、指纹上下文窗口会被编辑点附近的改文扫中，二者都不能用于
    跨轮对比；同类残留实例不算新增，交给下一轮计划继续处理。
    """
    if previous is None:
        return {"decision": "accept", "reason": "首轮基线，作为历史最佳版本"}

    def _blocking_kinds(audit: dict[str, Any]) -> set[str]:
        """阻断级问题的"种类"集合：source::check::found（上下文无关）。

        指纹含 ±60 字符上下文窗口，编辑点附近的旧问题会因窗口扫到改文而
        漂移成"新指纹"；版本决策关心的是"是否引入了新的违规种类"，
        同类残留实例交给下一轮计划处理。
        """
        kinds: set[str] = set()
        for issue in audit["registry"].get("issues", []):
            if issue.get("priority") not in ("P0", "P1"):
                continue
            check = str((issue.get("metadata") or {}).get("check") or "")
            found = str((issue.get("metadata") or {}).get("found") or issue.get("issue_id"))
            kinds.add(f"{issue.get('source')}::{check}::{found.lower()}")
        return kinds

    prev_blocking = _blocking_kinds(previous)
    curr_blocking = _blocking_kinds(current)
    new_blocking = sorted(curr_blocking - prev_blocking)

    # 硬约束：新增 P0/P1 种类一律拒绝（PRD 测试 G：修改回归必须被拒绝或回滚）
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


def _verify_tasks(
    best_audit: dict[str, Any], candidate_audit: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """任务级验证（PRD §5 阶段五）：按指纹判断每个计划任务的问题是否消失。

    fixed = 该任务全部问题的指纹都不再出现在候选版本登记表中；
    still_open = 至少一个问题仍在（或按数量部分解决，记录解决比例）。
    """
    fp_by_id = {
        str(issue.get("issue_id")): str(issue.get("fingerprint") or "")
        for issue in best_audit["registry"].get("issues", [])
    }
    candidate_fps = {
        str(issue.get("fingerprint") or "")
        for issue in candidate_audit["registry"].get("issues", [])
    }
    statuses: dict[str, dict[str, Any]] = {}
    for task in best_audit["plan"].get("tasks") or []:
        fingerprints = [
            fp_by_id[pid] for pid in task.get("problem_ids", []) if fp_by_id.get(pid)
        ]
        remaining = [f for f in fingerprints if f in candidate_fps]
        statuses[str(task.get("task_id"))] = {
            "status": "fixed" if fingerprints and not remaining else "still_open",
            "resolved": len(fingerprints) - len(remaining),
            "total": len(fingerprints),
        }
    return statuses


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
# 对抗闭环：review-sim 接入 decide 轮（R1）
# --------------------------------------------------------------------------- #
def _load_review_sim_config() -> dict[str, Any]:
    if REVIEW_SIM_CONFIG_PATH.exists():
        return load_yaml_file(REVIEW_SIM_CONFIG_PATH)
    return {}


def _review_settings(enabled: bool | None = None) -> dict[str, Any]:
    """从 config/review-sim.yaml 解析闭环参数；enabled 覆盖文件默认。"""
    data = _load_review_sim_config()
    loop = data.get("loop") or {}
    settings = {
        "enabled": bool(loop.get("enabled", False)),
        "review_changed_only": bool(loop.get("review_changed_only", True)),
        "include_cross": bool(loop.get("include_cross", True)),
        "max_review_cycles": int(loop.get("max_review_cycles", 8)),
        "stagnation_rotate_after": int(loop.get("stagnation_rotate_after", 2)),
        "stagnation_stop_after": int(loop.get("stagnation_stop_after", 4)),
        "alternate_roles": [str(r) for r in (loop.get("alternate_roles") or [])],
        "guidance_injection": bool(loop.get("guidance_injection", True)),
        "roles": data.get("roles") or {},
        "cross_view": data.get("cross_view") or {},
        "config": data,
    }
    if enabled is not None:
        settings["enabled"] = bool(enabled)
    return settings


def _review_finding_key(finding: dict[str, Any]) -> str:
    """跨评审周期的意见去重键：section + 类别 + 引文前缀。

    issue_model 指纹含 found（评审模型复述文本，跨轮会改写）与 ±60 字符
    上下文窗口，都不适合做意见级跨轮同一性；引文锚点 + 类别才是稳定信号。
    """
    section = _norm_ws(finding.get("section_id"))
    category = _norm_ws(finding.get("check")).lower()
    quote = _norm_ws(finding.get("context_snippet"))[:60]
    return f"{section}::{category}::{quote}"


def _review_extra_sources(active: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把活跃评审意见还原为 issue-registry extra_sources 形态。"""
    if not active:
        return []
    return [{"skill": "review-sim", "result": {"findings": [r["finding"] for r in active]}}]


def _review_pseudo_issues(active: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """活跃评审意见 → 锚定伪问题（走 _map_issues_to_chunks 与批注通道）。

    _comment_label/_comment_author 让 batch-processor 把这批批注标为
    「评审意见」（区别于确定性「审计问题」）；意见本身永不直接改写正文，
    只作为最高优先级批注引导下一轮修订，结果仍受全部门禁与 decide 管辖。
    """
    issues: list[dict[str, Any]] = []
    for index, record in enumerate(active, start=1):
        finding = record.get("finding") or {}
        summary = _norm_ws(finding.get("message") or finding.get("found") or "")
        issues.append(
            {
                "issue_id": f"REVIEW-{index:03d}",
                "priority": "P2",
                "dimension": "review_risk",
                "title": summary,
                "evidence": _norm_ws(finding.get("context_snippet") or ""),
                "suggestion": _norm_ws(finding.get("suggestion") or ""),
                "metadata": {"found": _norm_ws(finding.get("found") or "")},
                "_comment_label": "评审意见",
                "_comment_author": "Review-Sim",
            }
        )
    return issues


def _review_stagnation_step(
    stagnant: int, new_count: int, resolved_count: int, review_cfg: dict[str, Any]
) -> tuple[int, str]:
    """评审周期止损（纯函数，ARIS 纪律：2 轮空转换方向，4 轮叫人）。"""
    if new_count or resolved_count:
        return 0, "progress"
    stagnant += 1
    rotate_after = int(review_cfg.get("stagnation_rotate_after", 2))
    stop_after = int(review_cfg.get("stagnation_stop_after", 4))
    if stagnant >= stop_after:
        return stagnant, "stop"
    if stagnant >= rotate_after and (review_cfg.get("alternate_roles") or []) and not review_cfg.get("rotated"):
        return stagnant, "rotate_roles"
    return stagnant, "monitor"


def _emit_review_cycle(
    state: dict[str, Any], out_dir: Path, prev_text_file: str
) -> None:
    """对当前最佳版本发射一轮评审作业（只评改动章节），进入 awaiting_review_sim。"""
    review = state["review_sim"]
    cycle_index = len(state.get("review_history") or []) + 1
    cycle_dir = Path(out_dir) / "work" / f"review-cycle-{cycle_index:03d}"
    new_text = Path(state["best"]["text_file"]).read_text(encoding="utf-8")
    only_sections: list[str] | None = None
    if (
        review.get("review_changed_only", True)
        and prev_text_file
        and Path(prev_text_file).exists()
        and str(prev_text_file) != str(state["best"]["text_file"])
    ):
        diff = run_skill(
            "review-sim",
            {
                "mode": "diff_sections",
                "old_text": Path(prev_text_file).read_text(encoding="utf-8"),
                "new_text": new_text,
                "language": state["language"],
            },
        )
        only_sections = diff.get("review_sections") or None

    build_config = dict(review.get("config") or {})
    roles = build_config.get("roles") or {}
    current = review.get("roles_current") or sorted(roles)
    build_config["roles"] = {key: value for key, value in roles.items() if key in set(current)}
    cross_view = dict(build_config.get("cross_view") or {})
    cross_view["enabled"] = bool(review.get("include_cross", True))
    build_config["cross_view"] = cross_view

    result = run_skill(
        "review-sim",
        {
            "mode": "build",
            "text": new_text,
            "language": state["language"],
            "out": str(cycle_dir),
            "only_sections": only_sections,
            "config": build_config,
        },
    )
    if not int(result.get("job_count") or 0):
        # 改动章节过滤后无作业（如角色-章节矩阵不覆盖改动）：跳过本轮评审，
        # 如实记录，不计停滞
        state["review_history"] = (state.get("review_history") or []) + [
            {
                "cycle": cycle_index,
                "skipped": "no_review_jobs",
                "only_sections": only_sections,
                "strategy": "skipped",
            }
        ]
        state["review_cycle"] = None
        state["next_stage"] = "build"
        return
    state["review_cycle"] = {
        "cycle": cycle_index,
        "dir": str(cycle_dir),
        "jobs_file": str(cycle_dir / "review-jobs.json"),
        "responses_dir": str(cycle_dir / "responses"),
        "job_count": int(result.get("job_count") or 0),
        "only_sections": only_sections,
    }
    state["next_stage"] = "review"
    state["status"] = "awaiting_review_sim"


def stage_review(state: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """对抗闭环消费阶段：回填评审响应 → fail-closed 校验 → 意见生命周期 → 止损。

    响应不齐（pending）时保持 awaiting_review_sim 幂等返回，由 harness
    继续回填（或执行桥 stage=backfill）；全部就绪后 apply + 登记。
    """
    out_dir = Path(out_dir)
    cycle = state.get("review_cycle") or {}
    if not cycle:
        state["next_stage"] = "build"
        _save_state(out_dir, state)
        return state
    cycle_dir = Path(cycle["dir"])
    cycle_n = int(cycle["cycle"])
    apply_result = run_skill(
        "review-sim",
        {
            "mode": "apply",
            "jobs_file": cycle["jobs_file"],
            "responses_dir": cycle["responses_dir"],
        },
    )
    (cycle_dir / "apply-result.json").write_text(
        json.dumps(apply_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pending = apply_result.get("pending_jobs") or []
    if pending:
        state["status"] = "awaiting_review_sim"
        state["review_pending_jobs"] = pending
        state["next_stage"] = "review"
        _save_state(out_dir, state)
        return state
    state["review_pending_jobs"] = []

    jobs_payload = json.loads(Path(cycle["jobs_file"]).read_text(encoding="utf-8"))
    covered = {str(job.get("section_id")) for job in jobs_payload.get("jobs") or []}
    if "cross" in covered:
        covered.add("__all__")

    active = state.get("review_findings") or []
    flagged: dict[str, dict[str, Any]] = {}
    new_count = still_count = 0
    for finding in apply_result.get("findings") or []:
        key = _review_finding_key(finding)
        flagged[key] = finding
        record = next((r for r in active if r["key"] == key), None)
        if record is not None:
            record["last_cycle"] = cycle_n
            record["times_seen"] = int(record.get("times_seen", 1)) + 1
            still_count += 1
        else:
            active.append(
                {
                    "key": key,
                    "finding": finding,
                    "first_cycle": cycle_n,
                    "last_cycle": cycle_n,
                    "times_seen": 1,
                }
            )
            new_count += 1
    resolved: list[dict[str, Any]] = []
    for record in active:
        if record["key"] in flagged:
            continue
        section_id = str((record.get("finding") or {}).get("section_id") or "")
        if section_id in covered or "__all__" in covered:
            resolved.append(record)
    if resolved:
        resolved_keys = {r["key"] for r in resolved}
        active = [r for r in active if r["key"] not in resolved_keys]
    state["review_findings"] = active

    review_cfg = state.get("review_sim") or {}
    stagnant, strategy = _review_stagnation_step(
        int(state.get("review_stagnant", 0)), new_count, len(resolved), review_cfg
    )
    state["review_stagnant"] = stagnant
    history_entry = {
        "cycle": cycle_n,
        "applied": int(apply_result.get("applied_count") or 0),
        "rejected": int(apply_result.get("rejected_count") or 0),
        "new": new_count,
        "still_open": still_count,
        "resolved": len(resolved),
        "unmatched_responses": len(apply_result.get("unmatched_responses") or []),
        "strategy": strategy,
    }
    state["review_history"] = (state.get("review_history") or []) + [history_entry]
    (cycle_dir / "review-findings.json").write_text(
        json.dumps(
            {
                "active": state["review_findings"],
                "resolved_keys": [r["key"] for r in resolved],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if strategy == "rotate_roles":
        review_cfg["roles_current"] = list(review_cfg.get("alternate_roles") or [])
        review_cfg["rotated"] = True
    if strategy == "stop":
        state["stop_reason"] = (
            f"评审平台期止损：连续 {stagnant} 轮评审无新增意见且无收敛，"
            "按对抗循环纪律升级人工复核"
        )
        state["next_stage"] = "deliver"
        state["status"] = "stopped"
        _save_state(out_dir, state)
        return state

    # 刷新最佳版本审计：活跃意见作为 extra_sources 同场登记，
    # 下一轮计划 / readiness / 跨轮计数全部基于合并口径
    best_text = Path(state["best"]["text_file"]).read_text(encoding="utf-8")
    audit_dir = out_dir / "work" / f"audit-review-{cycle_n:03d}"
    refreshed = audit_round(
        best_text,
        language=state["language"],
        out_dir=audit_dir,
        extra_sources=_review_extra_sources(active),
    )
    state["best"]["audit_dir"] = str(audit_dir)
    state["rounds"].append(
        {
            "round": int(state["round"]),
            "kind": "review",
            "cycle": cycle_n,
            **_round_record(refreshed),
            "new_findings": new_count,
            "resolved_findings": len(resolved),
        }
    )
    state["next_stage"] = "build"
    state["status"] = "running"
    state["review_cycle"] = None
    _save_state(out_dir, state)
    return state


def _identity_fill_review(state: dict[str, Any]) -> None:
    """整体模式 identity 离线验证：为评审作业回填空意见响应。"""
    cycle = state.get("review_cycle") or {}
    payload = json.loads(Path(cycle["jobs_file"]).read_text(encoding="utf-8"))
    responses = Path(cycle["responses_dir"])
    responses.mkdir(parents=True, exist_ok=True)
    for job in payload.get("jobs") or []:
        target = responses / f"{job.get('job_id')}.txt"
        if target.exists():
            continue
        target.write_text(
            json.dumps(
                {
                    "job_id": job.get("job_id"),
                    "role": job.get("role"),
                    "section_id": job.get("section_id"),
                    "findings": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def _skip_review_cycle(state: dict[str, Any], out_dir: Path, *, reason: str) -> dict[str, Any]:
    """fail-closed 跳过本轮评审（整体模式无回填通道时）：如实记录，不计停滞。

    评审不可达不等于质量平台期——跳过轮不喂停滞计数器，避免虚假止损。
    """
    cycle = state.get("review_cycle") or {}
    state["review_history"] = (state.get("review_history") or []) + [
        {
            "cycle": int(cycle.get("cycle") or 0),
            "skipped": reason,
            "new": 0,
            "resolved": 0,
            "strategy": "skipped",
        }
    ]
    state["review_cycle"] = None
    state["review_pending_jobs"] = []
    state["next_stage"] = "build"
    state["status"] = "running"
    _save_state(Path(out_dir), state)
    return state


# --------------------------------------------------------------------------- #
# 计划驱动修订：问题 → 章节 映射
# --------------------------------------------------------------------------- #
def _norm_ws(value: Any) -> str:
    return " ".join(str(value or "").split())


def _strip_ellipsis(value: str) -> str:
    """剥掉证据片段两端的窗口省略号（claim-guard 的 context_snippet 边界标记）。"""
    return value.strip("…").strip(".").strip()


def _map_issues_to_chunks(
    issues: list[dict[str, Any]], chunks: list[dict[str, Any]], *, title: str
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """按证据锚定把问题映射到 parsed chunk；返回 (chunk_id -> issues, 未锚定问题)。

    只映射到可润色章节（body/abstract/appendix/section，排除标题与参考文献）；
    未锚定问题由调用方决定策略（计入告警，不静默丢弃）。
    """
    polishable = {
        str(chunk.get("id"))
        for chunk in chunks
        if chunk.get("kind") in _POLISHABLE_KINDS
        and (chunk.get("heading") or "").strip() != (title or "").strip()
    }
    normalized = [
        (str(chunk.get("id")), _norm_ws(chunk.get("content")))
        for chunk in chunks
        if str(chunk.get("id")) in polishable
    ]
    mapping: dict[str, list[dict[str, Any]]] = {}
    unmapped: list[dict[str, Any]] = []
    for issue in issues:
        placed = False
        for probe in (
            _strip_ellipsis(_norm_ws(issue.get("evidence"))),
            _norm_ws((issue.get("metadata") or {}).get("found")),
        ):
            probe = probe.strip("。；;，, ")
            if len(probe) < 8:
                continue
            chunk_id = _locate_probe(probe[:80], normalized)
            anchored_text = probe[:80]
            if chunk_id is None:
                # 跨章节边界的证据：全文审计的上下文窗口可能横跨两个章节
                # （如 "…方法 ## 摘要 脑电信号作为…"），按 Markdown 标题切分后
                # 优先取包含被标记词的片段（问题真正所在），否则取最长片段。
                fragments = [f.strip() for f in re.split(r"\s##\s", probe) if len(f.strip()) >= 8]
                found_term = _norm_ws((issue.get("metadata") or {}).get("found"))
                found_head = found_term[:2] if len(found_term) >= 2 else found_term
                fragments.sort(
                    key=lambda frag: (bool(found_head and found_head in frag), len(frag)),
                    reverse=True,
                )
                for fragment in fragments:
                    chunk_id = _locate_probe(fragment[:80], normalized)
                    if chunk_id is not None:
                        anchored_text = fragment[:80]
                        break
            if chunk_id is not None:
                # 跨界证据降级为章节内片段作为批注锚点，避免 polish 层
                # 前缀兜底把批注挂到相邻章节
                placement = {**issue, "anchor_text": anchored_text}
                mapping.setdefault(chunk_id, []).append(placement)
                placed = True
                break
        if not placed:
            unmapped.append(issue)
    return mapping, unmapped


def _locate_probe(needle: str, normalized: list[tuple[str, str]]) -> str | None:
    """返回包含 needle 的第一个 chunk id；未命中返回 None。"""
    for chunk_id, content in normalized:
        if needle in content:
            return chunk_id
    return None


def _chunk_strength_map(
    mapping: dict[str, list[dict[str, Any]]], default_strength: str
) -> dict[str, str]:
    """按 chunk 内最高优先级问题查 priority_strength 表；未命中用默认强度。"""
    planning = (issue_model.load_issue_rules().get("planning") or {})
    table = {
        str(key): str(value) for key, value in (planning.get("priority_strength") or {}).items()
    }
    fallback = str(planning.get("revision_default_strength") or default_strength)
    result: dict[str, str] = {}
    for chunk_id, issues in mapping.items():
        priorities = [
            str(issue.get("priority"))
            for issue in issues
            if str(issue.get("priority")) in issue_model.PRIORITY_ORDER
        ]
        if not priorities:
            result[chunk_id] = fallback
            continue
        top = min(priorities, key=issue_model.PRIORITY_ORDER.index)
        result[chunk_id] = table.get(top, fallback)
    return result


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
def _state_file(out_dir: Path) -> Path:
    return Path(out_dir) / "run-state.json"


def _save_state(out_dir: Path, state: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _state_file(out_dir).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_state(out_dir: Path) -> dict[str, Any] | None:
    path = _state_file(out_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _state_view(state: dict[str, Any]) -> dict[str, Any]:
    """harness 视角的状态摘要：下一步做什么、等哪些响应。"""
    review = state.get("review_sim") or {}
    view = {
        "stage": state.get("next_stage"),
        "status": state.get("status"),
        "round": state.get("round"),
        "next_stage": state.get("next_stage"),
        "stop_reason": state.get("stop_reason"),
        "pending_jobs": state.get("pending_jobs") or [],
        "selected_chunks": state.get("selected_chunks"),
        "focus_issue_count": state.get("focus_issue_count"),
        "unmapped_issue_count": state.get("unmapped_issue_count"),
        "responses_dir": state.get("responses_dir"),
        "current_input_file": state.get("current_input_file"),
        "best": state.get("best"),
        "rounds": state.get("rounds"),
        "readiness_status": state.get("readiness_status"),
        "state_file": state.get("state_file"),
        "review_enabled": bool(review.get("enabled")),
        "review_pending_jobs": state.get("review_pending_jobs") or [],
        "review_active_findings": len(state.get("review_findings") or []),
        "review_stagnant": state.get("review_stagnant", 0),
        "review_cycles_done": len(state.get("review_history") or []),
        "guidance_chunk_count": state.get("guidance_chunk_count", 0),
    }
    return view


def _migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    """旧 run-state 的向后兼容：补齐对抗闭环字段（旧运行视为未启用）。"""
    review = state.get("review_sim")
    if not isinstance(review, dict) or not review:
        state["review_sim"] = {**_review_settings(enabled=False), "roles_current": [], "rotated": False}
    state.setdefault("review_findings", [])
    state.setdefault("review_history", [])
    state.setdefault("review_stagnant", 0)
    state.setdefault("review_cycle", None)
    state.setdefault("review_pending_jobs", [])
    state.setdefault("guidance_chunk_count", 0)
    return state


def _best_text_suffix(text_file: Path) -> str:
    """最佳版本文本文件后缀：保留 md/tex 结构；其余统一 .txt。"""
    suffix = text_file.suffix.lower()
    return suffix if suffix in (".md", ".markdown", ".tex") else ".txt"


def stage_init(
    text_file: Path,
    *,
    language: str,
    strength: str,
    out_dir: Path,
    max_rounds: int = 3,
    min_gain: float = 0.2,
    min_index: float = 3.5,
    stagnant_limit: int = 2,
    review_sim: bool | None = None,
) -> dict[str, Any]:
    """阶段一：解析 → 基线审计 → 建立历史最佳与运行状态。

    review_sim：对抗闭环开关（None 时读 config/review-sim.yaml loop.enabled）。
    """
    language = normalize_language(language)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    current_text, pipeline_input = _load_source(text_file)
    baseline_audit = audit_round(
        current_text, language=language, out_dir=work / "round-000"
    )
    # 后续轮的最佳版本文本保留原始结构化后缀（md/tex），否则解析退化成单块 plain 文本
    text_suffix = _best_text_suffix(text_file)
    best_text = work / f"best-round-000{text_suffix}"
    best_text.write_text(current_text, encoding="utf-8")
    review = _review_settings(enabled=review_sim)
    review["roles_current"] = sorted(review.get("roles") or {})
    review["rotated"] = False
    state = {
        "schema_version": STATE_VERSION,
        "state_file": str(_state_file(out_dir)),
        "input_file": str(text_file),
        "language": language,
        "strength": strength,
        "text_suffix": text_suffix,
        "max_rounds": max_rounds,
        "min_gain": min_gain,
        "min_index": min_index,
        "stagnant_limit": stagnant_limit,
        "round": 1,
        "next_stage": "build",
        "status": "running",
        "stop_reason": "",
        "stagnant": 0,
        "recent_gains": [],
        "rounds": [
            {"round": 0, "kind": "baseline", **_round_record(baseline_audit)}
        ],
        "baseline_audit_dir": str(work / "round-000"),
        "best": {
            "round": 0,
            "text_file": str(best_text),
            "input_file": str(pipeline_input),
            "audit_dir": str(work / "round-000"),
        },
        "pending_jobs": [],
        "selected_chunks": None,
        "focus_issue_count": 0,
        "unmapped_issue_count": 0,
        "readiness_status": None,
        "review_sim": review,
        "review_findings": [],
        "review_history": [],
        "review_stagnant": 0,
        "review_cycle": None,
        "review_pending_jobs": [],
        "guidance_chunk_count": 0,
    }
    _save_state(out_dir, state)
    return state


def stage_reaudit(state: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """对当前最佳版本重跑全量确定性审计并刷新其计划。

    适用场景：规则 / 词库 / 术语包在轮间被修正后，历史最佳版本上的旧审计
    已过期——重审计让下一轮 build 消费最新计划，而不是缓存里的陈旧问题。
    """
    out_dir = Path(out_dir)
    text = Path(state["best"]["text_file"]).read_text(encoding="utf-8")
    audit_dir = out_dir / "work" / f"audit-refresh-{int(state['round']):03d}"
    audit = audit_round(text, language=state["language"], out_dir=audit_dir)
    state["best"]["audit_dir"] = str(audit_dir)
    state["audit_refreshed"] = True
    _save_state(out_dir, state)
    return state


def stage_build(state: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """阶段四（上）：按修改计划生成选择性作业包，进入 awaiting_responses。"""
    out_dir = Path(out_dir)
    work = out_dir / "work"
    best = state["best"]
    if int(state.get("round", 1)) > int(state["max_rounds"]):
        state["next_stage"] = "deliver"
        _save_state(out_dir, state)
        return state

    best_audit = _load_audit(Path(best["audit_dir"]))
    tasks = best_audit["plan"].get("tasks") or []
    if not tasks:
        state["stop_reason"] = (
            "无可自动执行的修改任务（剩余问题均需人工确认或缺材料），进入交付"
        )
        state["next_stage"] = "deliver"
        state["status"] = "stopped"
        _save_state(out_dir, state)
        return state

    input_path = Path(best["input_file"])
    parsed = run_skill("paper-parser", {"input": str(input_path), "format": "auto"})
    issues_by_id = {
        str(issue.get("issue_id")): issue
        for issue in best_audit["registry"].get("issues", [])
    }
    focus_issues: list[dict[str, Any]] = []
    for task in tasks:
        for problem_id in task.get("problem_ids", []):
            if problem_id in issues_by_id:
                focus_issues.append(issues_by_id[problem_id])

    mapping, unmapped = _map_issues_to_chunks(
        focus_issues, parsed.get("chunks", []), title=str(parsed.get("title", ""))
    )
    # 对抗闭环 guidance 注入：活跃评审意见按锚定并入修订映射——有自动任务
    # 的章节追加批注；只有评审意见的章节产生 guidance 驱动作业。意见本身
    # 永不直接改写正文（requires_human 保持，作者确认后才算解决）。
    guidance_chunks: dict[str, list[dict[str, Any]]] = {}
    review_findings = state.get("review_findings") or []
    if review_findings and (state.get("review_sim") or {}).get("guidance_injection", True):
        review_mapping, review_unmapped = _map_issues_to_chunks(
            _review_pseudo_issues(review_findings),
            parsed.get("chunks", []),
            title=str(parsed.get("title", "")),
        )
        unmapped = unmapped + review_unmapped
        for chunk_id, issues in review_mapping.items():
            mapping.setdefault(chunk_id, []).extend(issues)
            guidance_chunks[chunk_id] = issues
    state["guidance_chunk_count"] = len(guidance_chunks)
    if not mapping:
        # 无一锚定成功：无方向的全文重润违反局部补丁原则，直接进入交付，
        # 未锚定问题计入告警由作者裁定
        state["stop_reason"] = (
            f"修改计划中的 {len(focus_issues)} 个问题均无法锚定到可润色章节，"
            "无自动修订可执行，进入交付"
        )
        state["next_stage"] = "deliver"
        state["status"] = "stopped"
        state["unmapped_issue_count"] = len(unmapped)
        _save_state(out_dir, state)
        return state
    chunk_strength = _chunk_strength_map(mapping, state["strength"])
    only_chunks = sorted(mapping)

    round_index = int(state["round"])
    round_dir = work / f"round-{round_index:03d}"
    round_dir.mkdir(parents=True, exist_ok=True)
    build_result = run_skill(
        "batch-processor",
        {
            "inputs": [str(input_path)],
            "language": state["language"],
            "phase": "build",
            "strength": state["strength"],
            "out": str(round_dir),
            "only_chunks": only_chunks,
            "focus_issues": [issue for cid in only_chunks for issue in mapping[cid]],
            "chunk_strength": chunk_strength,
        },
    )
    item = (build_result.get("items") or [{}])[0]
    if item.get("status") != "built":
        raise RuntimeError(f"round {round_index} 构建失败: {item.get('errors') or item}")

    jobs = job_io.read_jobs(item["jobs_file"])
    if not jobs:
        state["stop_reason"] = "选择性构建未产出任何作业（计划问题均无法锚定到可润色章节），进入交付"
        state["next_stage"] = "deliver"
        state["status"] = "stopped"
        _save_state(out_dir, state)
        return state

    state.update(
        {
            "next_stage": "apply",
            "status": "awaiting_responses",
            "current_round_dir": str(round_dir),
            "current_input_file": str(input_path),
            "jobs_file": str(item["jobs_file"]),
            "responses_dir": str(Path(item["jobs_file"]).parent / "responses"),
            "pending_jobs": [str(job.get("job_id")) for job in jobs],
            "selected_chunks": only_chunks,
            "focus_issue_count": sum(len(v) for v in mapping.values()),
            "unmapped_issue_count": len(unmapped),
        }
    )
    _save_state(out_dir, state)
    return state


def stage_apply(
    state: dict[str, Any],
    out_dir: Path,
    *,
    responses_dir: Path | None = None,
    identity: bool = False,
) -> dict[str, Any]:
    """阶段四（下）：回填响应 → apply（清洗/门禁/装配）→ 产出候选版本。"""
    out_dir = Path(out_dir)
    jobs_file = Path(state["jobs_file"])
    run_root = jobs_file.parent
    responses = Path(state["responses_dir"])
    responses.mkdir(parents=True, exist_ok=True)

    jobs = job_io.read_jobs(jobs_file)
    for job in jobs:
        if job_io.job_is_ready(job):
            continue
        target = responses / f"{job['job_id']}.txt"
        if responses_dir is not None and (Path(responses_dir) / f"{job['job_id']}.txt").exists():
            shutil.copy(Path(responses_dir) / f"{job['job_id']}.txt", target)
        elif identity:
            target.write_text(str(job.get("chunk_content") or ""), encoding="utf-8")

    round_index = int(state["round"])
    apply_result = run_skill(
        "batch-processor",
        {
            "inputs": [state["current_input_file"]],
            "language": state["language"],
            "phase": "apply",
            "strength": state["strength"],
            "out": str(Path(state["current_round_dir"])),
            "only_chunks": state.get("selected_chunks") or None,
        },
    )
    summary = (apply_result.get("items") or [{}])[0]
    step_file = run_root / f"step-01-{state['language']}-polish" / "step-result.json"
    candidate_text = None
    if step_file.exists():
        candidate_text = json.loads(step_file.read_text(encoding="utf-8")).get(
            "polished_text"
        )
    if not candidate_text:
        state["rounds"].append(
            {
                "round": round_index,
                "kind": "round",
                "status": "error",
                "reason": "apply 未产出终稿",
            }
        )
        state["stop_reason"] = "apply 未产出终稿，中止迭代"
        state["next_stage"] = "deliver"
        state["status"] = "stopped"
        _save_state(out_dir, state)
        return state

    candidate_file = (
        out_dir / "work" / f"candidate-round-{round_index:03d}.txt"
    )
    candidate_file.write_text(str(candidate_text), encoding="utf-8")
    state.update(
        {
            "next_stage": "decide",
            "status": "running",
            "candidate_text_file": str(candidate_file),
            "candidate_docx_file": str(summary.get("output_docx_file") or "") or None,
            "candidate_result_file": str(summary.get("result_file") or "") or None,
        }
    )
    _save_state(out_dir, state)
    return state


def stage_decide(state: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """阶段五/六：独立复核（全量确定性审计）→ 版本决策 → 任务级验证 → 停止判定。

    对抗闭环：候选审计同场登记历史活跃评审意见（计数口径一致）；接受且
    未停机时对最佳版本发射评审周期（只评改动章节），进入 awaiting_review_sim。
    """
    out_dir = Path(out_dir)
    work = out_dir / "work"
    round_index = int(state["round"])
    candidate_text = Path(state["candidate_text_file"]).read_text(encoding="utf-8")
    round_dir = Path(state["current_round_dir"])
    candidate_audit = audit_round(
        candidate_text,
        language=state["language"],
        out_dir=round_dir / "audit",
        extra_sources=_review_extra_sources(state.get("review_findings") or []),
    )
    best_audit = _load_audit(Path(state["best"]["audit_dir"]))
    verdict = decide(best_audit, candidate_audit, min_gain=float(state["min_gain"]))
    task_status = _verify_tasks(best_audit, candidate_audit)

    record = {
        "round": round_index,
        "kind": "round",
        **_round_record(candidate_audit),
        "decision": verdict["decision"],
        "reason": verdict["reason"],
        "selected_chunks": state.get("selected_chunks"),
        "job_count": len(state.get("pending_jobs") or []),
        "task_status": task_status,
    }
    state["rounds"].append(record)

    prev_text_file = str((state.get("best") or {}).get("text_file") or "")
    if verdict["decision"] == "reject":
        LOG.info("拒绝本轮修改：%s", verdict["reason"])
        state["stagnant"] = int(state.get("stagnant", 0)) + 1
        state["recent_gains"] = (state.get("recent_gains") or []) + [0.0]
    else:
        candidate_docx = state.get("candidate_docx_file")
        if candidate_docx and Path(candidate_docx).exists():
            next_input = str(candidate_docx)
        else:
            best_file = work / f"best-round-{round_index:03d}{state.get('text_suffix') or '.txt'}"
            best_file.write_text(candidate_text, encoding="utf-8")
            next_input = str(best_file)
        state["best"] = {
            "round": round_index,
            "text_file": state["candidate_text_file"],
            "input_file": next_input,
            "audit_dir": str(round_dir / "audit"),
        }
        gain = float(verdict.get("gain") or 0.0)
        state["recent_gains"] = (state.get("recent_gains") or []) + [gain]
        state["stagnant"] = (
            int(state.get("stagnant", 0)) + 1 if abs(gain) < float(state["min_gain"]) else 0
        )

    best_audit_now = _load_audit(Path(state["best"]["audit_dir"]))
    stop, stop_reason = should_stop(
        audit=best_audit_now,
        round_index=round_index,
        max_rounds=int(state["max_rounds"]),
        min_index=float(state["min_index"]),
        stagnant_rounds=int(state.get("stagnant", 0)),
        stagnant_limit=int(state["stagnant_limit"]),
        recent_gains=state.get("recent_gains") or [],
    )
    if stop:
        state["stop_reason"] = stop_reason
        state["next_stage"] = "deliver"
        state["status"] = "stopped"
    else:
        state["round"] = round_index + 1
        review = state.get("review_sim") or {}
        cycles_done = len(state.get("review_history") or [])
        if (
            review.get("enabled")
            and verdict["decision"] != "reject"
            and cycles_done < int(review.get("max_review_cycles", 8))
        ):
            # 版本被接受才评审：被拒绝的候选已回滚，评它只会制造虚假停滞
            _emit_review_cycle(state, out_dir, prev_text_file)
        else:
            state["next_stage"] = "build"
    _save_state(out_dir, state)
    return state


def _assemble_final_docx(state: dict[str, Any], final_text: str, out_dir: Path) -> dict[str, Any]:
    """交付 DOCX 装配：docx 输入直接采用最佳轮次回写稿；文本输入按 Markdown 装配。

    返回 {docx_path, method, limitations}；装配失败回退纯文本并记录（不阻断交付）。
    """
    best_input = str(state.get("best", {}).get("input_file") or "")
    if best_input.lower().endswith((".docx", ".doc")) and Path(best_input).exists():
        target = out_dir / "final_manuscript.docx"
        shutil.copy(best_input, target)
        return {"docx_path": str(target), "method": "best_round_docx", "limitations": []}
    target = out_dir / "final_manuscript.docx"
    try:
        from utils.docx_io import create_docx_from_markdown  # noqa: PLC0415

        built = create_docx_from_markdown(target, final_text)
        return {
            "docx_path": built["docx_path"],
            "method": "markdown_assembly",
            "limitations": built["limitations"],
        }
    except Exception as exc:  # noqa: BLE001 - 装配失败回退纯文本交付
        LOG.warning("终稿 DOCX 装配失败，回退纯文本交付: %s", exc)
        return {"docx_path": "", "method": "fallback_text", "limitations": [str(exc)]}


def _run_submission_preflight(
    state: dict[str, Any], out_dir: Path, docx_path: str, *, render: bool = False
) -> dict[str, Any]:
    """投稿前预审（submission-audit pipeline）：证据/统计/图表/结构确定性门禁。

    失败不阻断交付，只降级合并状态并记录——预审缺口必须显式可见。
    render=True 时执行真实 DOCX→PDF→页面图验收（需 Word COM / LibreOffice）。
    """
    try:
        result = run_skill(
            "submission-audit",
            {
                "mode": "pipeline",
                "input_file": state["best"]["text_file"],
                "language": state["language"],
                "docx_path": docx_path,
                "render": render,
                "out": str(out_dir / "submission-audit"),
                "version": f"optimizer-loop-round-{state.get('best', {}).get('round', 0)}",
            },
        )
        gate = result.get("gate") or {}
        return {
            "status": "ok",
            "audit_status": result.get("status"),
            "automated_pass": bool(gate.get("automated_pass")),
            "human_signoff_status": gate.get("human_signoff_status"),
            "final_publication_allowed": bool(gate.get("final_publication_allowed")),
            "audit_json": (result.get("files") or {}).get("audit_json"),
            "gate_detail": gate,
        }
    except Exception as exc:  # noqa: BLE001
        LOG.warning("submission-audit 预审失败，交付状态降级为需人工复核: %s", exc)
        return {"status": "error", "automated_pass": False, "error": str(exc)}


def _merge_readiness(
    prd: dict[str, Any], preflight: dict[str, Any] | None
) -> dict[str, Any]:
    """合并 PRD 三级状态与 submission-audit 自动门禁为单一交付状态。"""
    if prd["status"] == BLOCKED:
        return {**prd, "delivery_status": BLOCKED}
    if preflight is None:
        return {**prd, "delivery_status": prd["status"]}
    if preflight.get("status") == "error":
        return {
            **prd,
            "status": CONDITIONAL,
            "delivery_status": CONDITIONAL,
            "reason": f"{prd['reason']}；投稿预审执行失败（{preflight.get('error')}），需人工复核",
        }
    if prd["status"] == READY and preflight.get("automated_pass"):
        return {**prd, "delivery_status": READY}
    reason = prd["reason"]
    if not preflight.get("automated_pass"):
        reason += "；submission-audit 自动门禁未全部通过（证据绑定/统计/图表/结构存在开放项）"
    return {**prd, "status": CONDITIONAL, "delivery_status": CONDITIONAL, "reason": reason}


def _render_delivery_index(
    *,
    stem: str,
    merged: dict[str, Any],
    stop_reason: str,
    docx_info: dict[str, Any],
    preflight: dict[str, Any] | None,
    state: dict[str, Any],
    final_audit: dict[str, Any],
    root_files: list[str],
) -> str:
    counts = _priority_counts(final_audit)
    lines = [
        f"# 交付索引 — {stem}",
        "",
        f"- **交付状态：{merged['delivery_status']}**（PRD 三级状态与投稿预审门禁合并判定）",
        f"- 判定依据：{merged['reason']}",
        f"- 停止原因：{stop_reason or '未记录'}",
        f"- 迭代轮数：{max((r.get('round', 0) for r in state.get('rounds', [])), default=0)}",
        f"- 最终问题数：{final_audit['registry']['issue_count']}"
        f"（P0 {counts.get('P0', 0)} / P1 {counts.get('P1', 0)} / P2 {counts.get('P2', 0)} / P3 {counts.get('P3', 0)}）",
        f"- 科学真实性硬门槛：{'通过' if final_audit['scorecard']['hard_gate']['pass'] else '未通过'}",
        "",
        "## 交付物",
        "",
        "- final_manuscript.md（终稿全文）",
        f"- final_manuscript.docx（{'最佳轮次 DOCX 回写稿' if docx_info.get('method') == 'best_round_docx' else 'Markdown 装配稿'}）"
        if docx_info.get("docx_path")
        else "- 终稿 DOCX：装配失败，见 quality_report.md（纯文本交付）",
        "- quality_report.md / evidence_audit.md / iteration_log.json / submission_readiness.md",
        f"- submission-audit/（投稿预审完整报告{'; 渲染验收见 docx-render-report.json' if preflight and preflight.get('status') == 'ok' else '；本次未执行或执行失败'}）",
    ]
    if docx_info.get("limitations"):
        lines += ["", "## DOCX 装配限制", ""]
        lines += [f"- {item}" for item in docx_info["limitations"]]
    if preflight and preflight.get("status") == "ok":
        lines += [
            "",
            "## 投稿预审门禁",
            "",
            f"- 自动门禁：{'通过' if preflight.get('automated_pass') else '未全部通过'}",
            f"- 人工签核状态：{preflight.get('human_signoff_status') or '-'}",
            f"- 最终发表许可：{preflight.get('final_publication_allowed')}",
            "- 完整证据图谱 / 统计登记 / 图表清单见 submission-audit/ 目录",
        ]
    lines += [
        "",
        "## 人工签核（硬门禁，不可自动化绕过）",
        "",
        "- 作者/通讯作者按 submission-audit/human-signoff.md 逐项确认",
        "- 本候选稿不代表期刊一定录用",
    ]
    if root_files:
        lines += ["", "## 已复制到项目根目录", ""]
        lines += [f"- {Path(p).name}" for p in root_files]
    return "\n".join(lines) + "\n"


def stage_deliver(
    state: dict[str, Any],
    out_dir: Path,
    *,
    submission_audit: bool = True,
    render: bool = False,
    deliver_to_root: bool = False,
) -> dict[str, Any]:
    """阶段八：交付（PRD §8 五件套 + 终稿 DOCX + 投稿预审 + 合并就绪状态 + 交付索引）。

    - submission_audit：是否执行 submission-audit pipeline（确定性预审；默认开）
    - render：是否真实渲染 DOCX→PDF→页面图（需 Word COM / LibreOffice；默认关）
    - deliver_to_root：按用户交付偏好把终稿 DOCX、渲染 PDF 与交付索引复制到项目根目录
    """
    out_dir = Path(out_dir)
    final_audit = _load_audit(Path(state["best"]["audit_dir"]))
    baseline_audit = _load_audit(Path(state["baseline_audit_dir"]))
    final_text = Path(state["best"]["text_file"]).read_text(encoding="utf-8")
    stop_reason = str(state.get("stop_reason") or "")
    (out_dir / "final_manuscript.md").write_text(final_text, encoding="utf-8")
    (out_dir / "iteration_log.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "language": state["language"],
                "strength": state["strength"],
                "stop_reason": stop_reason,
                "rounds": state["rounds"],
                "review": {
                    "cycles": state.get("review_history") or [],
                    "active_findings": state.get("review_findings") or [],
                },
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
    docx_info = _assemble_final_docx(state, final_text, out_dir)
    preflight = (
        _run_submission_preflight(state, out_dir, str(docx_info.get("docx_path") or ""), render=render)
        if submission_audit
        else None
    )
    merged = _merge_readiness(readiness, preflight)

    state["readiness_status"] = merged["delivery_status"]
    state["next_stage"] = "done"
    state["status"] = "delivered"
    _save_state(out_dir, state)
    (out_dir / "submission_readiness.md").write_text(
        _render_readiness(merged, final_audit, stop_reason, state["rounds"]),
        encoding="utf-8",
    )
    (out_dir / "quality_report.md").write_text(
        _render_quality_report(baseline_audit, final_audit, state["rounds"], stop_reason),
        encoding="utf-8",
    )
    (out_dir / "evidence_audit.md").write_text(
        _render_evidence_audit(final_audit), encoding="utf-8"
    )

    # 根目录交付（用户偏好：最终候选 DOCX / 渲染 PDF / 交付索引复制到项目根目录）
    root_files: list[str] = []
    if deliver_to_root:
        stem = Path(str(state.get("input_file") or "manuscript")).stem
        try:
            root = Path(__file__).resolve().parents[2]
            if docx_info.get("docx_path"):
                target = root / f"{stem}_final.docx"
                shutil.copy(docx_info["docx_path"], target)
                root_files.append(str(target))
            for candidate in (out_dir / "submission-audit").rglob("*.pdf"):
                target = root / f"{stem}_final.render.pdf"
                shutil.copy(candidate, target)
                root_files.append(str(target))
                break
        except Exception as exc:  # noqa: BLE001 - 根目录复制失败不改变交付结论
            LOG.warning("根目录交付复制失败: %s", exc)

    stem = Path(str(state.get("input_file") or "manuscript")).stem
    (out_dir / "delivery-index.md").write_text(
        _render_delivery_index(
            stem=stem,
            merged=merged,
            stop_reason=stop_reason,
            docx_info=docx_info,
            preflight=preflight,
            state=state,
            final_audit=final_audit,
            root_files=root_files,
        ),
        encoding="utf-8",
    )
    if deliver_to_root and docx_info.get("docx_path"):
        try:
            root = Path(__file__).resolve().parents[2]
            index_target = root / f"{stem}_交付索引.md"
            index_target.write_text(
                (out_dir / "delivery-index.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            root_files.append(str(index_target))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("交付索引根目录写入失败: %s", exc)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": merged["delivery_status"],
        "stop_reason": stop_reason,
        "final_manuscript": str(out_dir / "final_manuscript.md"),
        "final_docx": str(docx_info.get("docx_path") or "") or None,
        "docx_method": docx_info.get("method"),
        "submission_audit": preflight,
        "delivery_index": str(out_dir / "delivery-index.md"),
        "root_files": root_files,
        "iteration_log": str(out_dir / "iteration_log.json"),
        "deliverables": [
            "final_manuscript.md",
            "final_manuscript.docx",
            "quality_report.md",
            "evidence_audit.md",
            "iteration_log.json",
            "submission_readiness.md",
            "delivery-index.md",
        ],
        "final": {
            "issue_count": final_audit["registry"]["issue_count"],
            "counts_by_priority": _priority_counts(final_audit),
            "weighted_index": _quality_index(final_audit),
            "hard_gate_pass": final_audit["scorecard"]["hard_gate"]["pass"],
        },
    }


# --------------------------------------------------------------------------- #
# 驱动入口
# --------------------------------------------------------------------------- #
def _optional_bool(value: Any) -> bool | None:
    """run 参数的三态布尔：None / 未传 → 配置默认；字符串兼容 true/false。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def run_loop(
    text_file: Path,
    *,
    language: str = "en",
    strength: str = "deep",
    out_dir: Path = Path("output/optimizer-loop"),
    max_rounds: int = 3,
    min_gain: float = 0.2,
    min_index: float = 3.5,
    stagnant_limit: int = 2,
    responses_dir: Path | None = None,
    identity: bool = False,
    review_sim: bool | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """整体模式：内部依次驱动各 stage（identity / responses_dir 自动回填）。

    kwargs 透传交付选项：submission_audit（默认开）/ render / deliver_to_root。
    """
    out_dir = Path(out_dir)
    state = _load_state(out_dir)
    if state is None:
        state = stage_init(
            text_file,
            language=language,
            strength=strength,
            out_dir=out_dir,
            max_rounds=max_rounds,
            min_gain=min_gain,
            min_index=min_index,
            stagnant_limit=stagnant_limit,
            review_sim=review_sim,
        )
    state = _migrate_state(state)
    while state.get("next_stage") in ("build", "apply", "decide", "review"):
        next_stage = state["next_stage"]
        if next_stage == "build":
            state = stage_build(state, out_dir)
        elif next_stage == "apply":
            state = stage_apply(
                state, out_dir, responses_dir=responses_dir, identity=identity
            )
        elif next_stage == "review":
            state = stage_review(state, out_dir)
            if state.get("status") == "awaiting_review_sim":
                if identity:
                    _identity_fill_review(state)
                    state = stage_review(state, out_dir)
                else:
                    # 整体模式无评审回填通道：fail-closed 跳过本轮评审（如实记录）
                    state = _skip_review_cycle(state, out_dir, reason="reviewer_unavailable")
        else:
            state = stage_decide(state, out_dir)
        if state.get("status") in ("stopped", "delivered"):
            break
    return stage_deliver(
        state,
        out_dir,
        submission_audit=bool(kwargs.get("submission_audit", True)),
        render=bool(kwargs.get("render", False)),
        deliver_to_root=bool(kwargs.get("deliver_to_root", False)),
    )


def run(args: dict[str, Any]) -> dict[str, Any]:
    """coding harness 统一入口：stage 分步驱动或整体自动循环。"""
    stage = str(args.get("stage") or "").strip().lower()
    out_dir = Path(str(args.get("out") or args.get("out_dir") or "output/optimizer-loop"))
    if not stage:
        # 整体模式（兼容既有契约）：identity 恒等响应或 responses_dir 已回填响应
        raw_input = args.get("text_file") or args.get("input")
        if not raw_input:
            raise ValueError("optimizer-loop 需要 text_file 或 input")
        responses_dir = args.get("responses_dir")
        return run_loop(
            Path(str(raw_input)),
            language=str(args.get("language") or "en"),
            strength=str(args.get("strength") or "deep"),
            out_dir=out_dir,
            max_rounds=int(args.get("max_rounds") or 3),
            min_gain=float(args.get("min_gain", 0.2)),
            min_index=float(args.get("min_index", 3.5)),
            stagnant_limit=int(args.get("stagnant_limit") or 2),
            responses_dir=Path(str(responses_dir)) if responses_dir else None,
            identity=bool(args.get("identity", False)),
            review_sim=_optional_bool(args.get("review_sim")),
        )

    if stage == "init":
        raw_input = args.get("text_file") or args.get("input")
        if not raw_input:
            raise ValueError("stage=init 需要 text_file 或 input")
        state = stage_init(
            Path(str(raw_input)),
            language=str(args.get("language") or "en"),
            strength=str(args.get("strength") or "deep"),
            out_dir=out_dir,
            max_rounds=int(args.get("max_rounds") or 3),
            min_gain=float(args.get("min_gain", 0.2)),
            min_index=float(args.get("min_index", 3.5)),
            stagnant_limit=int(args.get("stagnant_limit") or 2),
            review_sim=_optional_bool(args.get("review_sim")),
        )
        return _state_view(state)

    state = _migrate_state(_load_state(out_dir) or {})
    if not state:
        raise ValueError(
            f"未找到 {out_dir / 'run-state.json'}：请先执行 stage=init 建立运行状态"
        )
    if stage == "status":
        return _state_view(state)
    if stage == "audit":
        return _state_view(stage_reaudit(state, out_dir))
    if stage == "build":
        return _state_view(stage_build(state, out_dir))
    if stage == "apply":
        responses_dir = args.get("responses_dir")
        return _state_view(
            stage_apply(
                state,
                out_dir,
                responses_dir=Path(str(responses_dir)) if responses_dir else None,
                identity=bool(args.get("identity", False)),
            )
        )
    if stage == "review":
        return _state_view(stage_review(state, out_dir))
    if stage == "decide":
        return _state_view(stage_decide(state, out_dir))
    if stage == "deliver":
        return stage_deliver(
            state,
            out_dir,
            submission_audit=bool(args.get("submission_audit", True)),
            render=bool(args.get("render", False)),
            deliver_to_root=bool(args.get("deliver_to_root", False)),
        )
    raise ValueError(f"未知 stage: {stage!r}，可选 {VALID_STAGES}")


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
