"""PRD §4/§5 新增能力的测试：问题登记、质量评分、修改规划、迭代闭环。

覆盖 PRD 验收标准中可用确定性手段验证的条目：
- 测试 A（事实约束）/ 测试 L（评分可信度）：未覆盖的维度不得打分，
  科学真实性硬门槛独立于加权指数；
- 测试 G（修改回归）：引入新 P0/P1 的版本必须被拒绝；
- 测试 H（无效迭代）：收益停滞必须触发停止；
- §5 阶段二/三：问题有编号、分 P0-P3、计划含全部必填字段。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module

SOURCE = (
    "本文方法取得了巨大的提升，准确率达到 94.2% [1]，"
    "是国际领先的最佳方案，特别牛。训练 20 轮。"
)


class IssueRegistryTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("issue-registry")

    def test_registry_assigns_ids_and_priorities(self):
        registry = self.module.run({"text": SOURCE, "language": "cn"})
        self.assertGreater(registry["issue_count"], 0)
        for issue in registry["issues"]:
            self.assertTrue(issue["issue_id"].startswith("ISS-"))
            self.assertIn(issue["priority"], ("P0", "P1", "P2", "P3"))
            self.assertTrue(issue["dimension"])
        self.assertEqual(
            set(registry["counts_by_priority"]),
            {"P0", "P1", "P2", "P3"},
        )

    def test_registry_dedupes_and_records_sources(self):
        """相同来源的相同发现必须去重；sources_consulted 需记录跑了哪些检查。"""
        guard = skill_module("claim-guard").run({"text": SOURCE, "language": "cn"})
        self.assertTrue(guard.get("findings"), "claim-guard 应产出 findings")
        registry = self.module.run(
            {
                "sources": [
                    {"skill": "claim-guard", "result": guard},
                    {"skill": "claim-guard", "result": guard},
                ],
                "language": "cn",
            }
        )
        self.assertGreater(registry["dedup_removed"], 0, "重复来源应被去重")
        self.assertEqual(registry["sources_consulted"], ["claim-guard", "claim-guard"])

    def test_registry_orders_by_priority(self):
        registry = self.module.run({"text": SOURCE, "language": "cn"})
        priorities = [i["priority"] for i in registry["issues"]]
        self.assertEqual(priorities, sorted(priorities))

    def test_registry_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.module.run(
                {"text": SOURCE, "language": "cn", "out": tmp}
            )
            self.assertTrue((Path(tmp) / "issue-registry.json").exists())
            self.assertTrue((Path(tmp) / "issue-registry.md").exists())
            self.assertTrue(registry["registry_file"])

    def test_lit_verify_rejection_is_blocking(self):
        """测试 B：伪造文献必须判 P0。"""
        registry = self.module.run(
            {
                "sources": [
                    {
                        "skill": "lit-verify",
                        "result": {
                            "results": [
                                {
                                    "entry": {"number": 1, "title": "Fake Paper"},
                                    "verdict": "rejected",
                                    "findings": [
                                        {
                                            "level": "error",
                                            "field": "doi",
                                            "message": "DOI 在 Crossref 无记录",
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                ]
            }
        )
        self.assertEqual(registry["counts_by_priority"]["P0"], 1)
        self.assertTrue(registry["blocking"])


class QualityScoreTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("quality-score")
        self.registry = skill_module("issue-registry").run(
            {"text": SOURCE, "language": "cn"}
        )

    def test_uncovered_dimensions_are_not_scored(self):
        """测试 L：没跑过检查的维度不能打满分。"""
        card = self.module.run({"registry": self.registry, "language": "cn"})
        unscored = card["not_assessed"]
        # 文本全源审计后 literature_novelty 已由 cite-manager / lit-search 覆盖；
        # review_risk 仍只有 submission-audit 能覆盖，材料维度缺材料不给分。
        self.assertIn("review_risk", unscored)
        self.assertIn("method_rigor", unscored)
        self.assertIn("literature_novelty", card["coverage"]["assessed_dimensions"])
        for dimension in unscored:
            self.assertIsNone(card["scores"][dimension]["score"], dimension)
        self.assertLess(card["coverage_ratio"], 1.0)

    def test_material_dimensions_require_materials(self):
        card = self.module.run({"registry": self.registry, "language": "cn"})
        self.assertIn("method_rigor", card["not_assessed"])
        self.assertIn(
            "材料", card["not_assessed_reasons"]["method_rigor"]
        )

    def test_hard_gate_is_independent_of_weighted_index(self):
        """测试 L：科学真实性是硬门槛，独立于加权指数。"""
        card = self.module.run({"registry": self.registry, "language": "cn"})
        self.assertFalse(card["hard_gate"]["pass"])
        self.assertTrue(card["hard_gate"]["blocking_issues"])
        # 硬门槛维度分数低于满分，且其问题不计入加权指数的正面贡献
        self.assertLess(card["scores"]["scientific_truth"]["score"], 4)
        self.assertEqual(
            card["scores"]["scientific_truth"]["dimension"], "scientific_truth"
        )

    def test_every_score_carries_evidence(self):
        """PRD §4.2：每项评分必须附带依据/位置/问题/证据/置信度/建议。"""
        card = self.module.run({"registry": self.registry, "language": "cn"})
        for dimension, entry in card["scores"].items():
            if entry["score"] is None:
                continue
            for item in entry["evidence"]:
                for key in ("issue_id", "location", "problem", "evidence", "confidence", "suggestion"):
                    self.assertIn(key, item, f"{dimension} 的证据缺少 {key}")

    def test_clean_text_scores_full_on_covered_dimensions(self):
        """干净的小论文（有参考文献表、表标题，摘要数字与正文一致）在已覆盖维度应得满分。"""
        clean = (
            "## 摘要\n\n"
            "该方法在公开数据集上的准确率达到 94.2%。\n\n"
            "## 实验结果\n\n"
            "该方法在公开数据集上的准确率达到 94.2%，如表 1 所示 [1]。训练 20 轮。\n\n"
            "表 1：性能对比\n\n"
            "## 参考文献\n\n"
            "[1] Author A. Sample title. Sample Journal, 2024, 12(1): 1-10.\n"
        )
        registry = skill_module("issue-registry").run({"text": clean, "language": "cn"})
        # 允许 P3/info 级启发式噪声（如 venue 抽取不出的 GB/T 条目），不允许 P0-P2
        blocking_noise = [
            i for i in registry["issues"] if i["priority"] in ("P0", "P1", "P2")
        ]
        self.assertEqual(blocking_noise, [], blocking_noise)
        card = self.module.run({"registry": registry, "language": "cn"})
        for dimension in card["coverage"]["assessed_dimensions"]:
            self.assertEqual(card["scores"][dimension]["score"], 4, dimension)


class RevisionPlannerTests(unittest.TestCase):
    def setUp(self):
        self.registry = skill_module("issue-registry").run(
            {"text": SOURCE, "language": "cn"}
        )
        self.module = skill_module("revision-planner")

    def test_plan_has_all_required_fields(self):
        """PRD §5 阶段三：任务必须含全部必填字段。"""
        plan = self.module.run({"registry": self.registry, "language": "cn"})
        self.assertGreater(plan["task_count"], 0)
        required = {
            "task_id",
            "problem_ids",
            "defect",
            "target",
            "allowed_scope",
            "fact_constraints",
            "expected_dimension_impact",
            "verification_method",
            "risk",
        }
        for task in plan["tasks"]:
            self.assertTrue(required.issubset(task.keys()), task["task_id"])
            self.assertTrue(task["problem_ids"])
            self.assertTrue(task["fact_constraints"])

    def test_plan_orders_by_priority(self):
        plan = self.module.run({"registry": self.registry, "language": "cn"})
        priorities = [t["priority"] for t in plan["tasks"]]
        self.assertEqual(priorities, sorted(priorities))

    def test_empty_registry_yields_no_tasks(self):
        plan = self.module.run({"registry": {"issues": []}, "language": "cn"})
        self.assertEqual(plan["task_count"], 0)


class DecideFingerprintTests(unittest.TestCase):
    """测试 G 直测（PRD §5 阶段六）：新增 P0/P1 实例必须拒绝并回滚。

    三个层次：
    - 全新 kind → 拒绝（旧行为保持）；
    - 同 kind 但出现在别处（上下文相似度低）→ 拒绝——纯种类集合比对会放行的
      回归反例（架构评审 2026-10-25 最小反例）；
    - 编辑点附近窗口漂移（同 kind + 上下文高相似 / location 一致）→ 豁免不误拒。
    """

    @staticmethod
    def _issue(fp, *, check="hype", found="显著", context="", priority="P1",
               source="claim-guard", location=""):
        return {
            "issue_id": f"ISS-{fp[:8]}-001",
            "source": source,
            "priority": priority,
            "dimension": "scientific_truth",
            "severity": "warning",
            "title": found,
            "fingerprint": fp,
            "evidence": context,
            "location": location,
            "metadata": {"check": check, "found": found},
        }

    @staticmethod
    def _audit(issues, index=2.0):
        return {
            "registry": {
                "issues": issues,
                "issue_count": len(issues),
                "counts_by_priority": {},
            },
            "scorecard": {"weighted_index": index, "hard_gate": {"pass": True}},
        }

    def setUp(self):
        self.decide = skill_module("optimizer-loop").decide

    def test_new_p1_kind_rejected(self):
        prev = self._audit([], index=2.0)
        curr = self._audit(
            [self._issue("aaaa1111", context="全新上下文出现了显著的提升")], index=2.2
        )
        verdict = self.decide(prev, curr, min_gain=0.2)
        self.assertEqual(verdict["decision"], "reject")

    def test_same_kind_new_location_rejected(self):
        """评审者最小反例：旧指纹 A 消失、同 kind 新指纹 B 出现在别处 → 必须拒绝。"""
        prev = self._audit(
            [self._issue("aaaa1111", context="方法部分甲处上下文显示结果显著优于基线模型很多")]
        )
        curr = self._audit(
            [self._issue("bbbb2222", context="结论部分乙处上下文声称性能显著超越全部对比方法")]
        )
        verdict = self.decide(prev, curr, min_gain=0.2)
        self.assertEqual(verdict["decision"], "reject")
        self.assertTrue(verdict["new_blocking"])

    def test_count_increase_rejected(self):
        """旧实例仍在、又多出一条同 kind 新实例 → 拒绝。"""
        prev = self._audit(
            [self._issue("aaaa1111", context="甲处上下文显示结果显著优于基线模型很多个点")]
        )
        curr = self._audit(
            [
                self._issue("aaaa1111", context="甲处上下文显示结果显著优于基线模型很多个点"),
                self._issue("cccc3333", context="乙处摘要声称准确率显著高于所有已发表的方法效果"),
            ],
            index=2.2,
        )
        self.assertEqual(self.decide(prev, curr, min_gain=0.2)["decision"], "reject")

    def test_drifted_window_exempt(self):
        """编辑点附近改文扫动上下文窗口：同 kind、上下文高相似 → 豁免不误拒。"""
        ctx_old = "实验结果表明该方法在多个公开数据集上取得了显著优于现有方法的表现并且训练开销较低" * 1
        ctx_new = "实验结果表明该方法在多个公开基准数据集上取得了显著优于现有方法的表现并且整体训练开销较低一" * 1
        prev = self._audit([self._issue("aaaa1111", context=ctx_old, location="")])
        curr = self._audit([self._issue("bbbb2222", context=ctx_new, location="")], index=2.2)
        verdict = self.decide(prev, curr, min_gain=0.2)
        self.assertIn(verdict["decision"], ("accept", "accept_no_gain"))

    def test_same_location_dissimilar_context_not_exempt(self):
        """G4 收紧：同 location 但两侧上下文都有且不相似 → 不豁免（同段新问题）。"""
        prev = self._audit(
            [self._issue("aaaa1111", context="上下文窗口完全被改文替换的情形甲种写法表述完全不同", location="p:12")]
        )
        curr = self._audit(
            [self._issue("bbbb2222", context="完全不同的新窗口内容乙与旧窗口毫无重叠且指向另一处缺陷", location="p:12")],
            index=2.2,
        )
        verdict = self.decide(prev, curr, min_gain=0.2)
        self.assertEqual(verdict["decision"], "reject")

    def test_same_location_no_context_exempt(self):
        """location 一致且一侧上下文为空（无凭据可比）→ 豁免（防误拒）。"""
        prev = self._audit(
            [self._issue("aaaa1111", context="", location="p:12")]
        )
        curr = self._audit(
            [self._issue("bbbb2222", context="窗口被改文完全重写无法比对", location="p:12")],
            index=2.2,
        )
        verdict = self.decide(prev, curr, min_gain=0.2)
        self.assertIn(verdict["decision"], ("accept", "accept_no_gain"))

    def test_unchanged_fingerprint_not_new(self):
        issue = self._issue("aaaa1111", context="同样的上下文窗口原样保留")
        prev = self._audit([issue], index=2.0)
        curr = self._audit([dict(issue)], index=2.1)
        verdict = self.decide(prev, curr, min_gain=0.2)
        self.assertIn(verdict["decision"], ("accept", "accept_no_gain"))

    def test_weighted_index_drop_rejected(self):
        issue = self._issue("aaaa1111", context="同样的上下文窗口原样保留")
        prev = self._audit([issue], index=2.0)
        curr = self._audit([dict(issue)], index=1.5)
        self.assertEqual(self.decide(prev, curr, min_gain=0.2)["decision"], "reject")


class CleanManuscriptReadyPathTests(unittest.TestCase):
    """PRD 成功路径验收：干净稿件必须能到达 READY_FOR_AUTHOR_REVIEW。

    架构评审（2026-10-25）第 3 条：此前所有端到端运行都以 BLOCKED 收尾，
    READY 只是代码里的字符串。本测试用无缺陷 fixture（引用成对、数字一致、
    无 hype、GB/T 参考文献齐全）证明该状态真实可达。

    注意：合并交付状态会因 submission-audit 的证据绑定门槛停在
    CONDITIONAL（draft_unverified 需作者提供材料）——这是设计使然；
    本测试断言的是 PRD 三级 readiness（自动检查维度）到达 READY。
    """

    def test_clean_manuscript_reaches_prd_ready(self):
        loop = skill_module("optimizer-loop")
        out_dir = Path(tempfile.mkdtemp(prefix="readypath-"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        result = loop.run(
            {
                "text_file": "test-manuscripts/cn-clean-draft.md",
                "language": "cn",
                "identity": True,
                "review_sim": False,
                "max_rounds": 2,
                "out": str(out_dir),
                "submission_audit": True,
            }
        )
        self.assertEqual(result["prd_readiness"], "READY_FOR_AUTHOR_REVIEW")
        self.assertTrue(result["final"]["hard_gate_pass"])
        self.assertEqual(result["final"]["counts_by_priority"].get("P0", 0), 0)
        self.assertEqual(result["final"]["counts_by_priority"].get("P1", 0), 0)
        self.assertGreaterEqual(result["final"]["weighted_index"], 3.5)
        for name in ("final_manuscript.md", "quality_report.md", "iteration_log.json", "delivery-index.md"):
            self.assertTrue((out_dir / name).exists(), name)

    def test_clean_manuscript_review_cycle_resolves_clean(self):
        """干净稿 + 对抗闭环开启：无任务停机路径也必须先评后交付（G1），
        评审无意见可报，仍达 PRD READY。"""
        loop = skill_module("optimizer-loop")
        out_dir = Path(tempfile.mkdtemp(prefix="readyreview-"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        result = loop.run(
            {
                "text_file": "test-manuscripts/cn-clean-draft.md",
                "language": "cn",
                "identity": True,
                "review_sim": True,
                "max_rounds": 1,
                "out": str(out_dir),
                "submission_audit": False,
            }
        )
        self.assertEqual(result["prd_readiness"], "READY_FOR_AUTHOR_REVIEW")
        log = json.loads((out_dir / "iteration_log.json").read_text(encoding="utf-8"))
        cycles = [c for c in log.get("review", {}).get("cycles", []) if not c.get("skipped")]
        self.assertGreaterEqual(len(cycles), 1, "无任务停机路径必须先评后交付（G1）")
        self.assertTrue(all(c.get("new", 0) == 0 for c in cycles), "干净稿评审不得报锚定缺陷")


class DeliveryTierTests(unittest.TestCase):
    """交付层级拆分（架构评审 2026-10-25 第 4 条）：
    BLOCKED / 未放行 CONDITIONAL → 诊断包命名；READY → 可投稿候选命名。"""

    def _deliver(self, text_file, **extra):
        loop = skill_module("optimizer-loop")
        out_dir = Path(tempfile.mkdtemp(prefix="tier-"))
        root_dir = Path(tempfile.mkdtemp(prefix="tierroot-"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, root_dir, ignore_errors=True)
        payload = {
            "text_file": text_file,
            "language": "cn",
            "identity": True,
            "review_sim": False,
            "max_rounds": 1,
            "out": str(out_dir),
            "submission_audit": False,
            "deliver_to_root": True,
            "root_dir": str(root_dir),
        }
        payload.update(extra)
        result = loop.run(payload)
        return result, out_dir, root_dir

    def test_blocked_delivery_is_staging_tier(self):
        result, out_dir, root_dir = self._deliver(
            "test-manuscripts/cn-eeg-denoising-draft.md"
        )
        if result["status"] in ("BLOCKED", "CONDITIONALLY_READY"):
            self.assertEqual(result["delivery_tier"], "staging")
            self.assertFalse((root_dir / "cn-eeg-denoising-draft_可投稿候选.docx").exists())
            if result.get("final_docx"):
                self.assertTrue((root_dir / "cn-eeg-denoising-draft_诊断包.docx").exists())
                index = (root_dir / "cn-eeg-denoising-draft_诊断索引.md").read_text(encoding="utf-8")
                self.assertIn("诊断包（staging）", index)
                self.assertIn("不构成送审候选正式交付", index)
        else:
            self.assertEqual(result["delivery_tier"], "release")

    def test_ready_without_preflight_cannot_release(self):
        """G2：PRD READY 但预审未执行 → 只能诊断包（release 不许绕过 submission-audit）。"""
        result, out_dir, root_dir = self._deliver(
            "test-manuscripts/cn-clean-draft.md", submission_audit=False
        )
        self.assertEqual(result["prd_readiness"], "READY_FOR_AUTHOR_REVIEW")
        self.assertEqual(result["delivery_tier"], "staging")
        checks = result["release_requirements"]["checks"]
        self.assertTrue(checks["deterministic_audit"])
        self.assertFalse(checks["submission_audit"])
        self.assertTrue((root_dir / "cn-clean-draft_诊断包.docx").exists())
        index = (root_dir / "cn-clean-draft_诊断索引.md").read_text(encoding="utf-8")
        self.assertIn("诊断包（staging）", index)
        self.assertIn("submission_audit", index)

    def test_preflight_not_passed_blocks_release_even_waived(self):
        """G2：预审执行但 automated_pass=False 时，放行 CONDITIONAL 并豁免其余
        两项也仍是诊断包——submission_audit 条件不可豁免。"""
        result, out_dir, root_dir = self._deliver(
            "test-manuscripts/cn-clean-draft.md", submission_audit=True
        )
        # 纯文本稿件的证据绑定门禁使 automated_pass=False（设计使然）
        self.assertEqual(result["status"], "CONDITIONALLY_READY")
        self.assertEqual(result["delivery_tier"], "staging")
        self.assertFalse(result["release_requirements"]["checks"]["submission_audit"])
        result2, _, root_dir2 = self._deliver(
            "test-manuscripts/cn-clean-draft.md",
            submission_audit=True,
            allow_conditional_release=True,
            release_waivers=["final_review", "precision_gate"],
        )
        self.assertEqual(result2["delivery_tier"], "staging")
        checks = result2["release_requirements"]["checks"]
        self.assertTrue(checks["final_review"])
        self.assertTrue(checks["precision_gate"])
        self.assertFalse(checks["submission_audit"], "预审未通过必须挡住 release")


class OptimizerLoopTests(unittest.TestCase):
    """闭环机制测试：用恒等响应隔离"循环逻辑"与"润色效果"。"""

    SOURCE_FILE = "test-manuscripts/cn-eeg-denoising-draft.md"

    def _run_loop(self, **kwargs):
        module = skill_module("optimizer-loop")
        payload = {
            "language": "cn",
            "strength": "light",
            "identity": True,
            "max_rounds": 2,
        }
        payload.update(kwargs)
        # 不能用 TemporaryDirectory：with 块退出即删除，调用方拿不到产物
        out_dir = Path(tempfile.mkdtemp(prefix="optloop-"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        payload["out_dir"] = str(out_dir)
        payload.pop("out", None)
        result = module.run_loop(Path(self.SOURCE_FILE), **payload)
        return result, out_dir

    def test_loop_produces_all_prd_deliverables(self):
        """PRD §8：五件交付物必须齐全。"""
        result, out_dir = self._run_loop()
        for name in (
            "final_manuscript.md",
            "quality_report.md",
            "evidence_audit.md",
            "iteration_log.json",
            "submission_readiness.md",
        ):
            self.assertTrue((out_dir / name).exists(), name)

    def test_readiness_uses_explicit_three_levels(self):
        """PRD §8.5：状态只能是明确分级，且写入交付文件。"""
        result, out_dir = self._run_loop()
        self.assertIn(
            result["status"],
            ("READY_FOR_AUTHOR_REVIEW", "CONDITIONALLY_READY", "BLOCKED"),
        )
        text = (out_dir / "submission_readiness.md").read_text(encoding="utf-8")
        self.assertIn(result["status"], text)
        self.assertIn("不代表期刊一定会录用", text)

    def test_iteration_log_records_every_round(self):
        result, out_dir = self._run_loop()
        log = json.loads((out_dir / "iteration_log.json").read_text(encoding="utf-8"))
        self.assertEqual(log["rounds"][0]["kind"], "baseline")
        self.assertGreaterEqual(len(log["rounds"]), 2)
        self.assertTrue(log["stop_reason"])

    def test_stagnation_stops_the_loop(self):
        """测试 H：连续无收益必须停止，不能无限循环。"""
        result, out_dir = self._run_loop(max_rounds=6, stagnant_limit=2)
        log = json.loads((out_dir / "iteration_log.json").read_text(encoding="utf-8"))
        self.assertIn("收益停滞", log["stop_reason"])
        # 只数润色轮（kind=round）；对抗闭环的评审记录（kind=review）不计入停滞轮数
        polish_rounds = [r for r in log["rounds"] if r.get("kind") == "round"]
        self.assertLessEqual(len(polish_rounds), 4)


if __name__ == "__main__":
    unittest.main()
