"""计划驱动修订与全源审计的测试。

覆盖本轮改造的三块新能力：
- utils/consistency：图表引用完整性 / 摘要数字一致 / 结论实体一致（确定性）；
- issue-registry 文本模式接入 cite-manager / lit-search / consistency-check 全源；
- batch-processor 选择性构建（only_chunks）+ 审计问题批注注入（focus_issues）；
- optimizer-loop 状态机：stage 分步、awaiting_responses、断点续跑、任务级验证；
- cite-manager 参考文献标题识别的 Markdown # 前缀回归。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import ROOT, skill_module

MANUSCRIPT = ROOT / "test-manuscripts" / "cn-eeg-denoising-draft.md"


class ConsistencyCheckTests(unittest.TestCase):
    def setUp(self):
        from utils import consistency

        self.module = consistency

    def test_figure_reference_without_caption_is_error(self):
        text = "# 引言\n\n效果如图 3 所示。\n\n图 2：已有图\n"
        findings = self.module.check_consistency(text, language="cn")
        checks = {f["check"]: f for f in findings}
        self.assertIn("figure_reference", checks)
        self.assertEqual(checks["figure_reference"]["found"], "图 3")
        self.assertIn("orphan_caption", checks)  # 图 2 有标题但未被引用

    def test_abstract_number_inconsistency(self):
        text = "# 摘要\n\n达到 100% 去噪。\n\n# 实验\n\n准确率为 94.7%。\n"
        findings = self.module.check_consistency(text, language="cn")
        matches = [f for f in findings if f["check"] == "abstract_number"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["found"], "100%")

    def test_abstract_number_decimal_equivalence_not_flagged(self):
        text = "# 摘要\n\n达到 94.7% 去噪。\n\n# 实验\n\n准确率为 0.947（归一化）。\n"
        findings = self.module.check_consistency(text, language="cn")
        self.assertNotIn("abstract_number", [f["check"] for f in findings])

    def test_conclusion_entity_missing_from_experiments(self):
        text = (
            "# 方法\n\n在数据集 A 与数据集 B 上训练。\n\n"
            "# 实验\n\n数据集 A 准确率 94.7%。\n\n"
            "# 结论\n\n数据集 B 同样有效。\n"
        )
        findings = self.module.check_consistency(text, language="cn")
        matches = [f for f in findings if f["check"] == "entity_consistency"]
        self.assertEqual(len(matches), 1)
        self.assertIn("数据集 B", matches[0]["found"])

    def test_abstract_without_heading_fallback(self):
        """无独立摘要标题（arXiv 渲染：标题后第一段即摘要）也要能做数字一致性检查。"""
        text = (
            "# A Real Paper Title\n\n"
            "The proposed method achieves 99.9% denoising accuracy.\n\n"
            "# Introduction\n\n"
            "We report an accuracy of 94.2% in our experiments.\n"
        )
        findings = self.module.check_consistency(text, language="en")
        matches = [f for f in findings if f["check"] == "abstract_number"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["found"], "99.9%")

    def test_clean_document_produces_no_findings(self):
        text = (
            "# 摘要\n\n准确率达到 94.2%。\n\n"
            "# 实验\n\n准确率达到 94.2%，如图 1 所示。\n\n图 1：结果\n\n"
            "# 结论\n\n方法有效。\n"
        )
        self.assertEqual(self.module.check_consistency(text, language="cn"), [])


class FullSourceRegistryTests(unittest.TestCase):
    """issue-registry 文本模式：结构缺陷必须成为带编号的 P0-P3 问题。"""

    def test_structural_flaws_are_registered(self):
        registry = skill_module("issue-registry").run(
            {"text_file": str(MANUSCRIPT), "language": "cn"}
        )
        sources = set(registry["sources_consulted"])
        self.assertIn("cite-manager", sources)
        self.assertIn("lit-search", sources)
        self.assertIn("consistency-check", sources)
        by_check = {
            (i["source"], i["metadata"].get("check")) for i in registry["issues"]
        }
        # [9] 越界 → P0
        self.assertIn(("cite-manager", "out_of_bounds"), by_check)
        out_of_bounds = [
            i
            for i in registry["issues"]
            if i["metadata"].get("check") == "out_of_bounds"
        ]
        self.assertEqual(len(out_of_bounds), 1)
        self.assertEqual(out_of_bounds[0]["priority"], "P0")
        # 图 3 未定义 → consistency-check figure_reference（P1）
        self.assertIn(("consistency-check", "figure_reference"), by_check)
        # 数据集 B 只出现在设置与结论 → entity_consistency
        self.assertIn(("consistency-check", "entity_consistency"), by_check)
        # 维度覆盖：文献与图表维度都有证据支撑
        dimensions = registry["counts_by_dimension"]
        self.assertIn("literature_novelty", dimensions)
        self.assertIn("figures_format", dimensions)


class CiteManagerMarkdownHeadingTests(unittest.TestCase):
    def test_markdown_references_heading_is_recognized(self):
        """## 参考文献 标题下的 GB/T 条目（无 et al/pp.）必须被识别为条目。"""
        text = (
            "正文引用 [1] 与 [2]。\n\n"
            "## 参考文献\n\n"
            "[1] 张伟, 李娜. 脑电信号处理. 电子学报, 2015, 43(2): 123-130.\n"
            "[2] 刘强. 生成对抗网络. 计算机学报, 2021, 44(5).\n"
        )
        result = skill_module("cite-manager").run(
            {"text": text, "language": "cn", "mode": "audit"}
        )
        audit = result["audit"]
        self.assertEqual(audit["ref_entry_count"], 2)
        self.assertEqual(audit["cited_not_listed"], [])
        self.assertEqual(audit["listed_not_cited"], [])


class SelectiveBuildTests(unittest.TestCase):
    def test_only_chunks_and_focus_issues(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory(prefix="selbuild-") as tmp:
            result = module.run(
                {
                    "inputs": [str(MANUSCRIPT)],
                    "language": "cn",
                    "phase": "build",
                    "strength": "light",
                    "out": tmp,
                    "only_chunks": ["c016"],
                    "focus_issues": [
                        {
                            "issue_id": "ISS-test-001",
                            "priority": "P1",
                            "dimension": "scientific_truth",
                            "title": "浮夸表述",
                            "evidence": "特别牛，显著优于所有现有方法。",
                            "suggestion": "删除口语化表述",
                        }
                    ],
                    "chunk_strength": {"c016": "deep"},
                }
            )
            item = result["items"][0]
            self.assertEqual(item["status"], "built")
            self.assertEqual(item["selected_chunks"], ["c016"])
            self.assertEqual(item["focus_issue_count"], 1)
            jobs = json.loads(Path(item["jobs_file"]).read_text(encoding="utf-8"))["jobs"]
            self.assertTrue(jobs)
            # 只为 c016 出作业，且双层标识正确
            self.assertTrue(all(j["parent_chunk_id"] == "c016" for j in jobs))
            # 强度按 chunk 覆盖为 deep
            self.assertTrue(all(j["strength"] == "deep" for j in jobs))
            # 审计问题进入 prompt 的批注通道（最高优先级）
            prompt = jobs[0]["prompt"]
            self.assertIn("审计问题 ISS-test-001", prompt)


class OptimizerLoopStateMachineTests(unittest.TestCase):
    """分步状态机：harness 视角的 init → build → awaiting → apply → decide → deliver。"""

    def _stage(self, stage: str, out_dir: Path, **extra):
        payload = {"stage": stage, "out": str(out_dir), "language": "cn", "identity": True}
        payload.update(extra)
        return skill_module("optimizer-loop").run(payload)

    def test_stepwise_stages_with_resume(self):
        out_dir = Path(tempfile.mkdtemp(prefix="optsm-"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)

        view = self._stage("init", out_dir, text_file=str(MANUSCRIPT), max_rounds=2)
        self.assertEqual(view["next_stage"], "build")
        self.assertEqual(view["round"], 1)
        self.assertTrue((out_dir / "run-state.json").exists())

        view = self._stage("build", out_dir)
        self.assertEqual(view["status"], "awaiting_responses")
        self.assertEqual(view["next_stage"], "apply")
        self.assertTrue(view["pending_jobs"], "build 后必须给出待回填 job 清单")
        self.assertTrue(view["selected_chunks"], "计划驱动修订应选择性出作业")
        # harness 可以只读 status 就拿到待回填清单（断点续跑的观测点）
        status = self._stage("status", out_dir)
        self.assertEqual(status["pending_jobs"], view["pending_jobs"])

        view = self._stage("apply", out_dir)
        self.assertEqual(view["next_stage"], "decide")
        self.assertEqual(view["status"], "running")

        view = self._stage("decide", out_dir)
        # 对抗闭环（R1）启用时 decide 接受后进入评审周期；未启用/被拒则回 build
        self.assertIn(view["next_stage"], ("build", "review", "deliver"))
        log = json.loads((out_dir / "run-state.json").read_text(encoding="utf-8"))
        round_records = [r for r in log["rounds"] if r.get("kind") == "round"]
        self.assertTrue(round_records[-1].get("task_status"), "decide 必须产出任务级验证")

        result = self._stage("deliver", out_dir)
        self.assertIn(
            result["status"],
            ("READY_FOR_AUTHOR_REVIEW", "CONDITIONALLY_READY", "BLOCKED"),
        )
        for name in (
            "final_manuscript.md",
            "quality_report.md",
            "evidence_audit.md",
            "iteration_log.json",
            "submission_readiness.md",
        ):
            self.assertTrue((out_dir / name).exists(), name)

    def test_selective_round_builds_fewer_jobs_than_full(self):
        """计划驱动修订：首轮选择性作业数必须少于全章节作业数。"""
        out_dir = Path(tempfile.mkdtemp(prefix="optsel-"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        self._stage("init", out_dir, text_file=str(MANUSCRIPT), max_rounds=2)
        view = self._stage("build", out_dir)
        # 参考文献章节（c018）与标题 chunk 永不进润色作业；问题未覆盖的章节也跳过
        parsed = skill_module("paper-parser").run(
            {"input": str(MANUSCRIPT), "format": "auto"}
        )
        all_polishable = {
            c["id"]
            for c in parsed["chunks"]
            if c["kind"] in ("body", "abstract", "appendix", "section")
        }
        self.assertLess(
            len(view["selected_chunks"]),
            len(all_polishable),
            "至少有一个无问题章节被选择性跳过",
        )
        self.assertNotIn("c018", view["selected_chunks"])

    def test_issue_to_chunk_mapping(self):
        optimizer = skill_module("optimizer-loop")

        parsed = skill_module("paper-parser").run(
            {"input": str(MANUSCRIPT), "format": "auto"}
        )
        issues = [
            {
                "issue_id": "ISS-1",
                "evidence": "与最优基线方法相比提升了 3.2 个百分点，差异具有统计学意义",
                "metadata": {"found": "特别牛"},
            },
            {"issue_id": "ISS-2", "evidence": "短"},  # 无法锚定
        ]
        mapping, unmapped = optimizer._map_issues_to_chunks(
            issues, parsed["chunks"], title=str(parsed.get("title", ""))
        )
        self.assertIn("c016", mapping)
        self.assertEqual([i["issue_id"] for i in mapping["c016"]], ["ISS-1"])
        self.assertEqual([i["issue_id"] for i in unmapped], ["ISS-2"])


if __name__ == "__main__":
    unittest.main()
