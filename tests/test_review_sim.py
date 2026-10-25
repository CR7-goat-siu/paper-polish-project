"""review-sim（模拟同行评审）专项测试。

覆盖：
- build：作业包渲染（章节 × 角色、跨节视图、prompt 落盘）；
- apply：合法响应回填、伪造原文引用拦截、字段缺失拦截、身份不符拦截、重复去重；
- register：issue-registry 归一 + review-sim 硬门禁属性；
- 改写管线硬门禁：review-sim 来源的问题在 revision-planner 中永不生成自动任务；
- 端到端：在英文埋坑测试稿上捕获跨节语义缺陷（模拟 harness 回填）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import ROOT, skill_module
from utils.skill_loader import run_skill

DRAFT = ROOT / "test-manuscripts" / "en-eeg-denoising-draft.md"


def _write_response(out_dir: Path, job_id: str, payload: dict) -> None:
    (out_dir / "responses").mkdir(parents=True, exist_ok=True)
    (out_dir / "responses" / f"{job_id}.txt").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class ReviewSimBuildTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("review-sim")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_build_creates_jobs_and_prompts(self):
        result = self.module.run(
            {"mode": "build", "text_file": str(DRAFT), "language": "en", "out": str(self.tmp)}
        )
        self.assertEqual(result["status"], "built")
        self.assertGreater(result["job_count"], 0)
        jobs_file = self.tmp / "review-jobs.json"
        self.assertTrue(jobs_file.exists())
        payload = json.loads(jobs_file.read_text(encoding="utf-8"))
        roles = {job["role"] for job in payload["jobs"]}
        self.assertEqual(roles, {"methodology", "statistics", "domain_expert", "novelty"})
        # 每个 job 的 prompt 已落盘且引用了角色与章节
        for job in payload["jobs"]:
            self.assertTrue(Path(job["prompt_file"]).exists(), job["job_id"])
            self.assertTrue(job["section_text"], job["job_id"])
        # 跨节视图存在
        cross = [job for job in payload["jobs"] if job["section_id"] == "cross"]
        self.assertEqual(len(cross), 4)
        # 章节类型来自 utils.sections 的分类（en 草稿应有 method/experiment 等）
        section_types = {job["section_type"] for job in payload["jobs"]}
        self.assertIn("method", section_types)
        self.assertIn("experiment", section_types)

    def test_build_job_ids_are_stable_and_unique(self):
        result = self.module.run(
            {"mode": "build", "text_file": str(DRAFT), "language": "en", "out": str(self.tmp)}
        )
        job_ids = [job["job_id"] for job in result["jobs"]]
        self.assertEqual(len(job_ids), len(set(job_ids)))


class ReviewSimApplyTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("review-sim")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.build = self.module.run(
            {"mode": "build", "text_file": str(DRAFT), "language": "en", "out": str(self.tmp)}
        )
        self.jobs = json.loads((self.tmp / "review-jobs.json").read_text(encoding="utf-8"))

    def _job(self, section_id: str, role: str | None = None) -> dict:
        for job in self.jobs["jobs"]:
            if job["section_id"] == section_id and (role is None or job["role"] == role):
                return job
        raise AssertionError(f"job not found: {section_id}/{role}")

    def _quote_from_job(self, job: dict) -> str:
        text = job["section_text"]
        # 取一段 40+ 字符的连续原文作锚点
        start = text.find(". ")
        return text[start + 2 : start + 82].strip()

    def test_valid_response_is_accepted(self):
        job = self._job("cross", "domain_expert")
        quote = self._quote_from_job(job)
        _write_response(
            self.tmp,
            job["job_id"],
            {
                "job_id": job["job_id"],
                "section_id": job["section_id"],
                "role": job["role"],
                "findings": [
                    {
                        "issue_category": "unsupported_conclusion",
                        "claim_or_defect_summary": "A conclusion is not backed by experiments.",
                        "exact_source_quote": quote,
                        "line_or_paragraph_locator": "¶2",
                        "rationale": "No experiment in the paper tests this claim.",
                        "severity_suggestion": "major",
                        "suggestion": "Add an experiment or soften the claim.",
                    }
                ],
            },
        )
        result = self.module.run({"mode": "apply", "jobs": self.jobs, "responses_dir": str(self.tmp / "responses")})
        self.assertEqual(result["applied_count"], 1)
        finding = result["findings"][0]
        self.assertEqual(finding["severity"], "warning")
        self.assertEqual(finding["confidence"], "low")
        self.assertTrue(finding["requires_human"])

    def test_fabricated_quote_is_rejected(self):
        job = self._job("cross", "novelty")
        _write_response(
            self.tmp,
            job["job_id"],
            {
                "job_id": job["job_id"],
                "section_id": job["section_id"],
                "role": job["role"],
                "findings": [
                    {
                        "issue_category": "overstated_contribution",
                        "claim_or_defect_summary": "Fabricated anchor should be dropped.",
                        "exact_source_quote": "This sentence does not exist anywhere in the paper at all.",
                        "line_or_paragraph_locator": "¶1",
                        "rationale": "Fake.",
                    }
                ],
            },
        )
        result = self.module.run({"mode": "apply", "jobs": self.jobs, "responses_dir": str(self.tmp / "responses")})
        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(result["rejected"][0]["reason"], "fabricated_or_mismatched_quote")

    def test_missing_required_field_is_rejected(self):
        job = self._job("cross", "methodology")
        quote = self._quote_from_job(job)
        payload = {
            "job_id": job["job_id"],
            "section_id": job["section_id"],
            "role": job["role"],
            "findings": [
                {
                    "issue_category": "missing_ablation",
                    "claim_or_defect_summary": "Missing rationale field entirely.",
                    "exact_source_quote": quote,
                    "line_or_paragraph_locator": "¶1",
                }
            ],
        }
        _write_response(self.tmp, job["job_id"], payload)
        result = self.module.run({"mode": "apply", "jobs": self.jobs, "responses_dir": str(self.tmp / "responses")})
        self.assertEqual(result["applied_count"], 0)
        self.assertIn("missing_fields", result["rejected"][0]["reason"])

    def test_identity_mismatch_is_rejected(self):
        job = self._job("cross", "statistics")
        quote = self._quote_from_job(job)
        _write_response(
            self.tmp,
            job["job_id"],
            {
                "job_id": "revsim-someone-else",
                "section_id": job["section_id"],
                "role": job["role"],
                "findings": [
                    {
                        "issue_category": "x",
                        "claim_or_defect_summary": "s",
                        "exact_source_quote": quote,
                        "line_or_paragraph_locator": "¶1",
                        "rationale": "r",
                    }
                ],
            },
        )
        result = self.module.run({"mode": "apply", "jobs": self.jobs, "responses_dir": str(self.tmp / "responses")})
        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(result["rejected"][0]["reason"], "job_id_mismatch")

    def test_duplicate_findings_dedupe(self):
        job = self._job("cross", "novelty")
        quote = self._quote_from_job(job)
        finding = {
            "issue_category": "overstated_contribution",
            "claim_or_defect_summary": "Same finding twice.",
            "exact_source_quote": quote,
            "line_or_paragraph_locator": "¶1",
            "rationale": "dup",
        }
        _write_response(
            self.tmp,
            job["job_id"],
            {"job_id": job["job_id"], "section_id": job["section_id"], "role": job["role"], "findings": [finding, dict(finding)]},
        )
        result = self.module.run({"mode": "apply", "jobs": self.jobs, "responses_dir": str(self.tmp / "responses")})
        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(result["rejected"][0]["reason"], "duplicate_finding")

    def test_multiline_quote_with_normalized_whitespace_matches(self):
        job = self._job("cross", "statistics")
        text = job["section_text"]
        # 取两行拼接处（换行 → 空格归一后应可匹配）
        if "\n" in text:
            idx = text.index("\n")
            quote = text[max(0, idx - 30) : idx + 40]
            if len(" ".join(quote.split())) >= 12:
                _write_response(
                    self.tmp,
                    job["job_id"],
                    {
                        "job_id": job["job_id"],
                        "section_id": job["section_id"],
                        "role": job["role"],
                        "findings": [
                            {
                                "issue_category": "stats_reporting",
                                "claim_or_defect_summary": "Quote spanning a line break.",
                                "exact_source_quote": quote,
                                "line_or_paragraph_locator": "¶1",
                                "rationale": "r",
                            }
                        ],
                    },
                )
                result = self.module.run({"mode": "apply", "jobs": self.jobs, "responses_dir": str(self.tmp / "responses")})
                self.assertEqual(result["applied_count"], 1)
                self.assertEqual(result["findings"][0]["section_id"], job["section_id"])

    def test_no_response_is_pending(self):
        result = self.module.run({"mode": "apply", "jobs": self.jobs})
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["pending_count"], len(self.jobs["jobs"]))


class ReviewSimRegisterTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("review-sim")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _apply_result(self) -> dict:
        return {
            "findings": [
                {
                    "check": "unsupported_conclusion",
                    "found": "Conclusion claims generalization without cross-dataset test.",
                    "severity": "warning",
                    "message": "Conclusion claims generalization without cross-dataset test.",
                    "context_snippet": "Therefore MBF-GAN generalizes to all kinds of EEG noise.",
                    "suggestion": "Add cross-dataset evaluation.",
                    "confidence": "low",
                    "requires_human": True,
                    "role": "domain_expert",
                    "section_id": "cross",
                    "locator": "¶2",
                    "rationale": "No cross-dataset experiment exists.",
                }
            ]
        }

    def test_register_applies_hard_guards(self):
        registry = self.module.run(
            {
                "mode": "register",
                "apply_result": self._apply_result(),
                "language": "en",
                "registry_out": str(self.tmp),
            }
        )
        review_issues = [i for i in registry["issues"] if i["source"] == "review-sim"]
        self.assertEqual(len(review_issues), 1)
        issue = review_issues[0]
        self.assertEqual(issue["confidence"], "low")
        self.assertEqual(issue["status"], "needs_human")
        self.assertTrue(issue["metadata"]["requires_human"])

    def test_issue_registry_guard_is_idempotent_direct(self):
        registry = run_skill(
            "issue-registry",
            {"sources": [{"skill": "review-sim", "result": {"findings": self._apply_result()["findings"]}}]},
        )
        module = skill_module("issue-registry")
        guarded = module.enforce_review_sim_guards(registry)
        for issue in guarded["issues"]:
            if issue["source"] == "review-sim":
                self.assertEqual(issue["status"], "needs_human")
                self.assertEqual(issue["confidence"], "low")


class RewritePipelineHardGateTests(unittest.TestCase):
    """改写管线硬门禁：review-sim 来源的问题永不生成自动改写任务。"""

    def setUp(self):
        self.planner = skill_module("revision-planner")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _registry(self, requires_human_flag: bool) -> dict:
        finding = {
            "check": "unsupported_conclusion",
            "found": "Overclaim",
            "severity": "warning",
            "message": "Overclaim in conclusion",
            "context_snippet": "quote",
        }
        sources = [{"skill": "review-sim", "result": {"findings": [finding]}}]
        registry = run_skill("issue-registry", {"sources": sources})
        if not requires_human_flag:
            # 模拟配置被误改：手动摘掉守卫属性，验证 planner 仍拦截
            for issue in registry["issues"]:
                issue["status"] = "open"
                issue["metadata"].pop("requires_human", None)
        return registry

    def test_review_sim_never_becomes_auto_task(self):
        for flag in (True, False):
            with self.subTest(requires_human_flag=flag):
                plan = self.planner.run({"registry": self._registry(flag), "language": "en"})
                sources_in_tasks = {task["source"] for task in plan["tasks"]}
                self.assertNotIn("review-sim", sources_in_tasks)
                sources_in_human = {task["source"] for task in plan["requires_human"]}
                self.assertIn("review-sim", sources_in_human)

    def test_config_removal_does_not_bypass_gate(self):
        # requires_human_sources 里删掉 review-sim 也不行：源级断言兜底
        planner_src = ROOT / "skills" / "revision-planner" / "main.py"
        self.assertIn("HUMAN_ONLY_SOURCES", planner_src.read_text(encoding="utf-8"))


class ReviewSimEndToEndTests(unittest.TestCase):
    """端到端：英文埋坑稿上的跨节语义缺陷捕获（模拟 harness 回填）。"""

    def setUp(self):
        self.module = skill_module("review-sim")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.build = self.module.run(
            {"mode": "build", "text_file": str(DRAFT), "language": "en", "out": str(self.tmp)}
        )
        self.jobs = json.loads((self.tmp / "review-jobs.json").read_text(encoding="utf-8"))

    def _job(self, role: str, section_id: str) -> dict:
        for job in self.jobs["jobs"]:
            if job["role"] == role and job["section_id"] == section_id:
                return job
        raise AssertionError(f"missing job {role}/{section_id}")

    def test_seeded_semantic_flaws_are_captured(self):
        text = DRAFT.read_text(encoding="utf-8")
        # 埋坑稿的三类跨节语义缺陷（planted-flaws-checklist）：
        # 1) 多分支结构无理论解释（3.2 多分支特征提取无理论论证）
        # 2) 摘要与结果不符（摘要声称完美去噪，第 5 节实际有条件限制）
        # 3) 结论越权（"undoubtedly the best"）
        scenarios = [
            (
                self._job("domain_expert", "cross"),
                "multi_branch_no_theory",
                "the multibranch feature fusion module",
            ),
            (
                self._job("novelty", "6-conclusion"),
                "overclaim",
                "undoubtedly the best",
            ),
            (
                self._job("statistics", "abstract"),
                "abstract_result_mismatch",
                "perfectly removes all kinds of noise interference",
            ),
        ]
        for job, category, anchor_hint in scenarios:
            with self.subTest(category=category):
                # 从该作业的章节文本里找含锚点提示的真实句子作引用
                sentence = ""
                for candidate in job["section_text"].replace("\n", " ").split(". "):
                    if anchor_hint.lower() in candidate.lower():
                        sentence = candidate.strip()
                        break
                self.assertTrue(sentence, f"anchor {anchor_hint!r} not found in {job['job_id']}")
                _write_response(
                    self.tmp,
                    job["job_id"],
                    {
                        "job_id": job["job_id"],
                        "section_id": job["section_id"],
                        "role": job["role"],
                        "findings": [
                            {
                                "issue_category": category,
                                "claim_or_defect_summary": f"{category} defect captured by simulated review.",
                                "exact_source_quote": sentence[:160],
                                "line_or_paragraph_locator": job["section_heading"],
                                "rationale": "Cross-section semantic inconsistency planted for testing.",
                                "severity_suggestion": "major",
                            }
                        ],
                    },
                )
        apply_result = self.module.run(
            {"mode": "apply", "jobs": self.jobs, "responses_dir": str(self.tmp / "responses")}
        )
        self.assertEqual(apply_result["applied_count"], 3)
        categories = {f["check"] for f in apply_result["findings"]}
        self.assertEqual(
            categories,
            {"multi_branch_no_theory", "overclaim", "abstract_result_mismatch"},
        )
        # 注册进 issue-registry 后全部带 requires_human 且不进自动任务
        registry = self.module.run(
            {"mode": "register", "apply_result": apply_result, "language": "en", "registry_out": str(self.tmp)}
        )
        review_issues = [i for i in registry["issues"] if i["source"] == "review-sim"]
        self.assertEqual(len(review_issues), 3)
        self.assertTrue(all(i["metadata"]["requires_human"] for i in review_issues))
        planner = skill_module("revision-planner")
        plan = planner.run({"registry": registry, "language": "en"})
        self.assertNotIn("review-sim", {t["source"] for t in plan["tasks"]})
        self.assertIn("review-sim", {t["source"] for t in plan["requires_human"]})


if __name__ == "__main__":
    unittest.main()
