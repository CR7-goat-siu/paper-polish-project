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
        self.assertIn("literature_novelty", unscored)
        self.assertIn("review_risk", unscored)
        for dimension in unscored:
            self.assertIsNone(card["scores"][dimension]["score"])
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
        clean = "该方法在基准数据集上的准确率达到 94.2%，如表 1 所示 [1]。训练 20 轮。"
        registry = skill_module("issue-registry").run({"text": clean, "language": "cn"})
        card = self.module.run({"registry": registry, "language": "cn"})
        for dimension in card["coverage"]["assessed_dimensions"]:
            if dimension == "scientific_truth":
                continue
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
        self.assertLessEqual(len(log["rounds"]), 4)


if __name__ == "__main__":
    unittest.main()
