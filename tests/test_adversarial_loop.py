"""T2 R1 对抗闭环测试：review-sim 接入 optimizer-loop 的 decide 轮。

覆盖：评审周期发射与消费、意见生命周期（new / still_open / resolved）、
平台期止损（ARIS 纪律 2 轮换向 / 4 轮叫人）、fail-closed 可见性、
整体模式终止性（无死循环）、guidance 批注注入与 requires_human 红线。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import ROOT, skill_module
from utils.skill_loader import run_skill

MANUSCRIPT = ROOT / "test-manuscripts" / "cn-eeg-denoising-draft.md"


def _empty_response(job: dict) -> str:
    return json.dumps(
        {
            "job_id": job.get("job_id"),
            "role": job.get("role"),
            "section_id": job.get("section_id"),
            "findings": [],
        },
        ensure_ascii=False,
    )


class ReviewStagnationStepTests(unittest.TestCase):
    """止损纯函数：progress 清零 / 2 轮换向 / 4 轮叫人。"""

    def setUp(self):
        self.step = skill_module("optimizer-loop")._review_stagnation_step
        self.cfg = {
            "stagnation_rotate_after": 2,
            "stagnation_stop_after": 4,
            "alternate_roles": ["methodology", "statistics"],
            "rotated": False,
        }

    def test_progress_resets(self):
        self.assertEqual(self.step(3, 1, 0, self.cfg), (0, "progress"))
        self.assertEqual(self.step(3, 0, 2, self.cfg), (0, "progress"))

    def test_rotate_at_threshold(self):
        self.assertEqual(self.step(0, 0, 0, self.cfg), (1, "monitor"))
        self.assertEqual(self.step(1, 0, 0, self.cfg), (2, "rotate_roles"))

    def test_stop_at_threshold(self):
        self.assertEqual(self.step(3, 0, 0, self.cfg), (4, "stop"))

    def test_rotate_only_once(self):
        rotated = dict(self.cfg, rotated=True)
        self.assertEqual(self.step(1, 0, 0, rotated), (2, "monitor"))

    def test_no_alternate_roles_means_monitor(self):
        bare = dict(self.cfg, alternate_roles=[])
        self.assertEqual(self.step(1, 0, 0, bare), (2, "monitor"))


class DiffSectionsTests(unittest.TestCase):
    def test_changed_detection(self):
        old = "# 摘要\n\n准确率 94.2%。\n\n# 实验\n\n如图 1 所示。\n"
        new = "# 摘要\n\n准确率 95.1%。\n\n# 实验\n\n如图 1 所示。\n"
        result = run_skill(
            "review-sim",
            {"mode": "diff_sections", "old_text": old, "new_text": new, "language": "cn"},
        )
        self.assertEqual(result["changed"], ["摘要"])
        self.assertEqual(result["review_sections"], ["摘要"])
        result_all = run_skill(
            "review-sim",
            {"mode": "diff_sections", "old_text": old, "new_text": old, "language": "cn"},
        )
        self.assertEqual(result_all["changed"], [])


class AdversarialLoopFlowTests(unittest.TestCase):
    """分步流程：decide 发射评审周期 → 回填 → 意见入登记表 → guidance 注入。"""

    def setUp(self):
        self.loop = skill_module("optimizer-loop")
        self.out_dir = Path(tempfile.mkdtemp(prefix="advloop-"))
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)

    def _stage(self, stage: str, **extra):
        payload = {"stage": stage, "out": str(self.out_dir), "language": "cn", "identity": True}
        payload.update(extra)
        return self.loop.run(payload)

    def _backfill_review(self, *, relabel: bool = True, with_finding: bool = True) -> int:
        """为当前评审周期回填响应；可选携带一条锚定意见，其余为空意见。"""
        state = json.loads((self.out_dir / "run-state.json").read_text(encoding="utf-8"))
        cycle = state["review_cycle"]
        payload = json.loads(Path(cycle["jobs_file"]).read_text(encoding="utf-8"))
        jobs = payload["jobs"]
        responses = Path(cycle["responses_dir"])
        responses.mkdir(parents=True, exist_ok=True)
        target = max(jobs, key=lambda job: len(str(job.get("section_text") or "")))
        section_text = str(target.get("section_text") or "")
        quote = " ".join(section_text.split())[20:70]
        self.assertGreaterEqual(len(quote), 12, "锚定引文过短")
        finding = {
            "issue_category": "unsupported_claim",
            "claim_or_defect_summary": "该句缺少与基线的直接对比支撑",
            "exact_source_quote": quote,
            "line_or_paragraph_locator": "para 1",
            "rationale": "跨节评审发现该声明无对照数据",
        }
        for job in jobs:
            if job is target and with_finding:
                body = {
                    "job_id": "executor-relabeled-id" if relabel else job["job_id"],
                    "role": job["role"],
                    "section_id": job["section_id"],
                    "findings": [finding],
                }
                name = "arbitrary-executor-name.txt" if relabel else f"{job['job_id']}.txt"
            else:
                body = {
                    "job_id": job["job_id"],
                    "role": job["role"],
                    "section_id": job["section_id"],
                    "findings": [],
                }
                name = f"{job['job_id']}.txt"
            (responses / name).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        return len(jobs)

    def test_decide_emits_review_and_consumes_backfill(self):
        self._stage("init", text_file=str(MANUSCRIPT), max_rounds=3, review_sim=True)
        self._stage("build")
        self._stage("apply")
        view = self._stage("decide")
        self.assertEqual(view["next_stage"], "review")
        self.assertEqual(view["status"], "awaiting_review_sim")
        state = json.loads((self.out_dir / "run-state.json").read_text(encoding="utf-8"))
        cycle = state["review_cycle"]
        self.assertTrue(Path(cycle["jobs_file"]).exists())
        self.assertGreater(int(cycle["job_count"]), 0)
        self.assertTrue(state["review_sim"]["enabled"])

        job_count = self._backfill_review()
        self.assertGreater(job_count, 0)
        view = self._stage("review")
        self.assertEqual(view["next_stage"], "build")
        self.assertEqual(view["review_active_findings"], 1)
        self.assertEqual(view["review_stagnant"], 0)

        # 意见以 review-sim 来源入登记表，且强制 requires_human（红线）
        state = json.loads((self.out_dir / "run-state.json").read_text(encoding="utf-8"))
        audit = json.loads(
            (Path(state["best"]["audit_dir"]) / "issue-registry.json").read_text(encoding="utf-8")
        )
        review_issues = [i for i in audit["issues"] if i.get("source") == "review-sim"]
        self.assertTrue(review_issues, "评审意见必须入登记表")
        self.assertTrue(all(i.get("status") == "needs_human" for i in review_issues))

        # 下一轮 build 的 prompt 包含「评审意见」批注（guidance 通道）
        self._stage("build")
        state = json.loads((self.out_dir / "run-state.json").read_text(encoding="utf-8"))
        jobs = json.loads(Path(state["jobs_file"]).read_text(encoding="utf-8"))["jobs"]
        prompts = "\n".join(job.get("prompt") or "" for job in jobs)
        self.assertIn("评审意见 REVIEW-001", prompts)
        self.assertGreaterEqual(state.get("guidance_chunk_count", 0), 0)

    def test_re_review_resolves_finding(self):
        # stagnant_limit 放宽：identity 模式零收益轮会触发确定性停滞停止，
        # 本测试只关心评审意见生命周期，不让旧停止条件抢先
        self._stage("init", text_file=str(MANUSCRIPT), max_rounds=3, review_sim=True, stagnant_limit=5)
        self._stage("build")
        self._stage("apply")
        self._stage("decide")
        self._backfill_review()
        self._stage("review")
        # 第二轮：identity 修订后 decide 再次发射评审（无差异 → 全章节覆盖）
        self._stage("build")
        self._stage("apply")
        self._stage("decide")
        self._backfill_review(with_finding=False)
        view = self._stage("review")
        self.assertEqual(view["review_active_findings"], 0)
        state = json.loads((self.out_dir / "run-state.json").read_text(encoding="utf-8"))
        history = state["review_history"]
        self.assertEqual(history[0]["resolved"], 0)
        self.assertEqual(history[1]["resolved"], 1)

    def test_review_pending_is_visible_fail_closed(self):
        self._stage("init", text_file=str(MANUSCRIPT), max_rounds=3, review_sim=True)
        self._stage("build")
        self._stage("apply")
        self._stage("decide")
        view = self._stage("review")
        self.assertEqual(view["status"], "awaiting_review_sim")
        self.assertEqual(view["next_stage"], "review")
        state = json.loads((self.out_dir / "run-state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["review_pending_jobs"], "pending 作业必须对 harness 可见")
        # 幂等：再次调用不崩、状态不变
        again = self._stage("review")
        self.assertEqual(again["status"], "awaiting_review_sim")

    def test_whole_mode_identity_terminates(self):
        result = self.loop.run(
            {
                "text_file": str(MANUSCRIPT),
                "language": "cn",
                "identity": True,
                "review_sim": True,
                "max_rounds": 1,
                "out": str(self.out_dir),
            }
        )
        self.assertIn(
            result["status"],
            ("READY_FOR_AUTHOR_REVIEW", "CONDITIONALLY_READY", "BLOCKED"),
        )
        log = json.loads((self.out_dir / "iteration_log.json").read_text(encoding="utf-8"))
        self.assertTrue(log.get("review"), "迭代日志必须记录评审周期")
        for cycle in log["review"]["cycles"]:
            self.assertNotIn("skipped", cycle)


class ReviewDisabledTests(unittest.TestCase):
    """review_sim=False 显式关闭：decide 行为与旧契约一致。"""

    def setUp(self):
        self.loop = skill_module("optimizer-loop")
        self.out_dir = Path(tempfile.mkdtemp(prefix="advoff-"))
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)

    def _stage(self, stage: str, **extra):
        payload = {"stage": stage, "out": str(self.out_dir), "language": "cn", "identity": True}
        payload.update(extra)
        return self.loop.run(payload)

    def test_disabled_goes_straight_to_build(self):
        self._stage("init", text_file=str(MANUSCRIPT), max_rounds=2, review_sim=False)
        self._stage("build")
        self._stage("apply")
        view = self._stage("decide")
        self.assertIn(view["next_stage"], ("build", "deliver"))
        self.assertEqual(view["review_cycles_done"], 0)


if __name__ == "__main__":
    unittest.main()
