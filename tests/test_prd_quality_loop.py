"""PRD §4/§5 新增能力的测试：问题登记、质量评分、修改规划、迭代闭环。

覆盖 PRD 验收标准中可用确定性手段验证的条目：
- 测试 A（事实约束）/ 测试 L（评分可信度）：未覆盖的维度不得打分，
  科学真实性硬门槛独立于加权指数；
- 测试 G（修改回归）：引入新 P0/P1 的版本必须被拒绝；
- 测试 H（无效迭代）：收益停滞必须触发停止；
- §5 阶段二/三：问题有编号、分 P0-P3、计划含全部必填字段。
"""

import hashlib
from unittest.mock import patch
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
            release_waivers=[
                {"check": "final_review", "author": "测试作者", "reason": "单元测试豁免"},
                {"check": "precision_gate", "author": "测试作者", "reason": "单元测试豁免"},
            ],
        )
        self.assertEqual(result2["delivery_tier"], "staging")
        checks = result2["release_requirements"]["checks"]
        self.assertTrue(checks["final_review"])
        self.assertTrue(checks["precision_gate"])
        self.assertFalse(checks["submission_audit"], "预审未通过必须挡住 release")


class ReleaseBoundaryE2ETests(unittest.TestCase):
    """H5：四条件 release 的端到端边界（三轮评审第 5 条）。

    诚实边界：submission-audit 的 evidence 门要求参考文献核验与作者材料
    （draft_unverified），离线无法合法变绿——这正是该门禁的设计目的。
    因此端到端能证明的是：其余三个条件全绿时，release 仍被材料轴挡住；
    以及伪造账本必须被拒绝。四条件全绿 → release 的判定由单元级钉死。
    """

    @staticmethod
    def _signed_waiver(check):
        return {"check": check, "author": "测试作者", "reason": "E2E 边界测试"}

    def _full_run(self, ledger_rows, **extra):
        import hashlib

        loop = skill_module("optimizer-loop")
        out_dir = Path(tempfile.mkdtemp(prefix="relboundary-"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        fixture = Path("test-manuscripts/cn-clean-draft.md")
        # 干净稿无任务 → best 恒为 round-000（与输入同内容），哈希可预知
        # （与 release 同口径：换行归一后的内容哈希）
        expected_sha = hashlib.sha256(
            fixture.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        header = [
            "issue_id", "source", "check", "priority", "evidence_snippet",
            "adjudication", "note", "adjudicator", "date", "manuscript_sha256",
        ]
        ledger = out_dir / "adjudication-ledger.csv"
        import csv as _csv

        with ledger.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = _csv.writer(handle)
            writer.writerow(header)
            for row in ledger_rows(expected_sha):
                writer.writerow(row)
        payload = {
            "text_file": str(fixture),
            "language": "cn",
            "identity": True,
            "review_sim": True,
            "max_rounds": 1,
            "out": str(out_dir),
            "submission_audit": True,
            "deliver_to_root": False,
            "adjudication_ledger": str(ledger),
        }
        payload.update(extra)
        return loop.run(payload), out_dir

    def test_valid_evidence_blocked_only_by_materials(self):
        """有效评审哈希 + 有效账本 + 放行 CONDITIONAL：release 仍被
        submission-audit 的材料门挡住（且仅被它挡住）。"""
        def rows(sha):
            data = [
                ("i1", "claim-guard", "TP", "作者甲"),
                ("i2", "consistency-check", "TP", "作者甲"),
                ("i3", "claim-guard", "TP", "作者乙"),
                ("i4", "lit-search", "TP", "作者乙"),
                ("i5", "consistency-check", "FP", "作者甲"),
            ]
            return [
                [iid, src, "hype", "P1", "证据", verdict, "", author, "2026-10-25", sha]
                for iid, src, verdict, author in data
            ]

        result, out_dir = self._full_run(
            rows,
            allow_conditional_release=True,
            release_waivers=[self._signed_waiver("final_review")],
        )
        checks = result["release_requirements"]["checks"]
        self.assertTrue(checks["deterministic_audit"])
        self.assertTrue(checks["precision_gate"], "有效账本（哈希绑定/多来源/署名）必须通过")
        self.assertTrue(checks["final_review"], "评审哈希绑定后必须通过（或被有效豁免）")
        self.assertFalse(checks["submission_audit"], "材料门必须挡住 release")
        self.assertEqual(result["delivery_tier"], "staging")
        index = (out_dir / "delivery-index.md").read_text(encoding="utf-8")
        self.assertIn("submission_audit", index)
        # 豁免签核工件落盘
        waiver = json.loads((out_dir / "waiver-record.json").read_text(encoding="utf-8"))
        self.assertEqual(waiver[-1]["waived_checks"], ["final_review"])
        self.assertEqual(waiver[-1]["entries"][0]["author"], "测试作者")

    def test_forged_ledger_rejected_e2e(self):
        """评审者反例端到端：伪造账本（重复 id + 空署名）→ precision_gate 拒绝。"""

        def rows(sha):
            return [
                ["same-id", "claim-guard", "hype", "P1", "证据", "TP", "", "", "2026-10-25", sha]
            ] * 5

        result, out_dir = self._full_run(rows)
        checks = result["release_requirements"]["checks"]
        self.assertFalse(checks["precision_gate"])
        reason = result["release_requirements"]["reasons"].get("precision_gate", "")
        self.assertTrue("不唯一" in reason or "署名" in reason, reason)
        self.assertEqual(result["delivery_tier"], "staging")


class ReviewAcceptanceE2ETests(unittest.TestCase):
    """F4 三态端到端：final_acquittal → review_acceptance → release 消费。

    - done → accepted：跨族可信评审响应回填后验收升格；
    - accepted 后 artifact 变化 → done（stale_review 显式原因）；
    - provisional（同族）不可 release；
    - 成功路径（接线级）：accepted + 预审通过 → release 命名可投稿候选。
    """

    def _run_to_final(self, *, reviewer_model, model_family, identity_source):
        """跑干净稿完整闭环，停机路径发射 final_acquittal，回填指定身份的
        评审响应，消费后返回 (result, out_dir, state)。"""
        import hashlib

        loop = skill_module("optimizer-loop")
        out_dir = Path(tempfile.mkdtemp(prefix="acce2e-"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        payload = {
            "stage": "init",
            "text_file": "test-manuscripts/cn-clean-draft.md",
            "language": "cn",
            "identity": True,
            "review_sim": True,
            "max_rounds": 1,
            "out": str(out_dir),
        }
        loop.run(payload)
        view = loop.run({"stage": "build", "out": str(out_dir)})
        if view["next_stage"] == "apply":
            loop.run({"stage": "apply", "out": str(out_dir), "identity": True})
            view = loop.run({"stage": "decide", "out": str(out_dir)})
        # 干净稿无任务 → build 直接先评后交付；有任务 → apply/decide 后停机发射
        assert view["next_stage"] == "review", view
        state = json.loads((out_dir / "run-state.json").read_text(encoding="utf-8"))
        assert state["review_cycle"]["purpose"] == "final_acquittal", state["review_cycle"]
        # 回填符合身份契约的 final 响应
        jobs = json.loads(Path(state["review_cycle"]["jobs_file"]).read_text(encoding="utf-8"))
        job = jobs["jobs"][0]
        best_text = Path(state["best"]["text_file"]).read_text(encoding="utf-8")
        response = {
            "job_id": job["job_id"],
            "section_id": "final",
            "role": "final_acquittal",
            "executor_model": "GLM-5.3-Flash",
            "reviewer_model": reviewer_model,
            "model_family": model_family,
            "identity_source": identity_source,
            "thread_id": "thread-final-e2e-001",
            "reviewed_text_sha256": job["artifact_sha256"],
            "scope": {
                "mode": "final_full",
                "sections": ["__all__"],
                "includes_references": True,
                "includes_figures": True,
                "char_count": job["char_count"],
            },
            "complete": True,
            "verdict": "acquitted",
            "score": 4,
            "findings": [],
        }
        responses = Path(state["review_cycle"]["responses_dir"])
        responses.mkdir(parents=True, exist_ok=True)
        (responses / f"{job['job_id']}.txt").write_text(
            json.dumps(response, ensure_ascii=False), encoding="utf-8"
        )
        view = loop.run({"stage": "review", "out": str(out_dir)})
        state = json.loads((out_dir / "run-state.json").read_text(encoding="utf-8"))
        return loop, out_dir, state, best_text

    def test_done_to_accepted_then_staging_honestly(self):
        loop, out_dir, state, best_text = self._run_to_final(
            reviewer_model="claude-sonnet-4",
            model_family="anthropic",
            identity_source="host_session_event",
        )
        self.assertEqual(state["review_acceptance"]["status"], "accepted")
        self.assertEqual(
            state["review_acceptance"]["reviewed_text_sha256"],
            hashlib.sha256(best_text.encode("utf-8")).hexdigest(),
        )
        # trace 落盘
        trace_dir = out_dir / "review-traces" / state["review_acceptance"]["trace_id"]
        self.assertTrue((trace_dir / "response.md").exists())
        self.assertTrue((trace_dir / "meta.json").exists())
        # 交付（诚实边界）：材料门（submission-audit）离线不可合法变绿 → staging，
        # final_review 条件因 accepted 而通过
        result = loop.run({"stage": "deliver", "out": str(out_dir), "submission_audit": True})
        self.assertEqual(result["status"], "CONDITIONALLY_READY")
        self.assertEqual(result["delivery_tier"], "staging")
        checks = result["release_requirements"]["checks"]
        self.assertTrue(checks["final_review"], "accepted 评审必须被 release 消费")
        self.assertFalse(checks["submission_audit"], "材料门必须挡住 release")
        # iteration log 记录 review_acceptance
        log = json.loads((out_dir / "iteration_log.json").read_text(encoding="utf-8"))
        self.assertEqual(log["review"]["review_acceptance"]["status"], "accepted")

    def test_provisional_cannot_release_e2e(self):
        loop, out_dir, state, _ = self._run_to_final(
            reviewer_model="GLM-5.3",
            model_family="zhipu",
            identity_source="host_session_event",
        )
        self.assertEqual(state["review_acceptance"]["status"], "provisional")
        result = loop.run({"stage": "deliver", "out": str(out_dir), "submission_audit": True})
        self.assertEqual(result["delivery_tier"], "staging")
        self.assertFalse(result["release_requirements"]["checks"]["final_review"])
        self.assertIn("不是 accepted", result["release_requirements"]["reasons"]["final_review"])

    def test_accepted_then_artifact_change_goes_done(self):
        loop, out_dir, state, best_text = self._run_to_final(
            reviewer_model="claude-sonnet-4",
            model_family="anthropic",
            identity_source="host_session_event",
        )
        self.assertEqual(state["review_acceptance"]["status"], "accepted")
        # 评审后最终稿被改动一个字符 → 旧 accepted 立即过期（F4/F3）
        best_file = Path(state["best"]["text_file"])
        best_file.write_text(best_text + " ", encoding="utf-8")
        result = loop.run({"stage": "deliver", "out": str(out_dir), "submission_audit": True})
        state = json.loads((out_dir / "run-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["review_acceptance"]["status"], "done")
        self.assertIn("stale_review", state["review_acceptance"]["gate_reason"])
        self.assertIn("stale_review", result["release_requirements"]["reasons"]["final_review"])

    def test_full_release_success_path_wiring(self):
        """交付验收 3（接线级）：accepted + 预审通过 → release 命名可投稿候选。

        submission-audit 的材料门离线不可合法变绿（evidence 门要求文献核验与
        作者材料——这是该门禁的设计目的），本测试用桩预审验证 release 接线；
        材料门的真实阻断由 test_valid_evidence_blocked_only_by_materials 证明。
        """
        loop, out_dir, state, _ = self._run_to_final(
            reviewer_model="claude-sonnet-4",
            model_family="anthropic",
            identity_source="host_session_event",
        )
        self.assertEqual(state["review_acceptance"]["status"], "accepted")
        # 精确账本（作者裁决证据）：绑定本稿哈希、双来源、署名、precision 0.8
        best_text = Path(state["best"]["text_file"]).read_text(encoding="utf-8")
        import csv as _csv

        sha = hashlib.sha256(best_text.encode("utf-8")).hexdigest()
        ledger = out_dir / "adjudication-ledger.csv"
        with ledger.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = _csv.writer(handle)
            writer.writerow([
                "issue_id", "source", "check", "priority", "evidence_snippet",
                "adjudication", "note", "adjudicator", "date", "manuscript_sha256",
            ])
            for i in range(1, 5):
                writer.writerow([
                    f"iss-{i}", "claim-guard" if i % 2 else "consistency-check",
                    "hype", "P1", "证据", "TP", "", "作者甲", "2026-10-26", sha,
                ])
            writer.writerow([
                "iss-5", "consistency-check", "hype", "P2", "证据", "FP", "",
                "作者乙", "2026-10-26", sha,
            ])
        (out_dir / "root").mkdir(parents=True, exist_ok=True)
        with patch.object(
            loop,
            "_run_submission_preflight",
            return_value={
                "status": "ok",
                "automated_pass": True,
                "human_signoff_status": "required_not_completed",
                "final_publication_allowed": False,
            },
        ):
            result = loop.run(
                {
                    "stage": "deliver",
                    "out": str(out_dir),
                    "submission_audit": True,
                    "deliver_to_root": True,
                    "root_dir": str(out_dir / "root"),
                }
            )
        self.assertEqual(result["status"], "READY_FOR_AUTHOR_REVIEW")
        self.assertEqual(result["delivery_tier"], "release")
        self.assertTrue(all(result["release_requirements"]["checks"].values()))
        self.assertTrue((out_dir / "root" / "cn-clean-draft_可投稿候选.docx").exists())
        index = (out_dir / "root" / "cn-clean-draft_交付索引.md").read_text(encoding="utf-8")
        self.assertIn("正式交付（release）", index)
        self.assertIn("仍需作者签核", index)
        # final_publication_allowed 恒 false（签核只属于最终发表环节）
        self.assertFalse(result["submission_audit"]["final_publication_allowed"])


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
