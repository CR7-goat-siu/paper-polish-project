"""guardrails：十条规则行为测试 + polish_engine Fail-Closed 测试。"""

import unittest

from conftest import skill_module
from utils.guardrails import attribute_feedback, evaluate_pair

CN_CASES = [
    # (说明, 源, 润色, 期望verdict, 期望规则)
    ("合法同义改写", "该模型在测试集上的准确率达到 91.2%，优于基线方法 [1]。",
     "该模型在测试集上取得了 91.2% 的准确率，表现优于基线方法 [1]。", "pass", None),
    ("R1-新增数值", "准确率达到 91.2%。", "准确率达到 95.6%。", "fail", "numeric.conservation"),
    ("R1-单位脱钩", "延迟为 0.8 ms。", "延迟为 0.8。", "fail", "numeric.conservation"),
    ("R1-p值漂移", "组间差异具有统计学意义（p = 0.047）。", "组间差异具有统计学意义（p < 0.05）。", "fail", "numeric.conservation"),
    ("R1-合法精度等价", "SNR 提升了 18.90%。", "SNR 提升了 18.9%。", "pass", None),
    ("R2-极性翻转", "SNR 提升了 3.1 dB。", "SNR 降低了 3.1 dB。", "fail", "numeric.polarity"),
    ("R2-主客体互换", "如表 1 所示，NLGAN 优于 SimpleCNN。", "如表 1 所示，SimpleCNN 优于 NLGAN。", "fail", "numeric.polarity"),
    ("R3-引用丢失", "已有研究 [1] [2] 验证。", "已有研究 [1] 验证。", "fail", "citation.conservation"),
    ("R3-引用越界", "方法有效 [1]。", "方法有效 [1] [25]。", "fail", "citation.conservation"),
    ("R4-图表编号丢失", "如图 3 所示，结构清晰。", "如图所示，结构清晰。", "fail", "anchor.conservation"),
    ("R4-实体篡改", "EEGDenoiseNet 数据集由 Zhang 整理。", "EEGDenoseNet 数据集由 Zhang 整理。", "fail", "anchor.conservation"),
    ("R5-对冲硬化", "该方法可能提升性能。", "该方法证明能提升性能。", "fail", "hedge.flip"),
    ("R6-范围丢失", "在本研究中，所提方法优于基线。", "所提方法优于基线。", "warn", "scope.shift"),
    ("R7-浮夸残留", "方法有效。", "方法是革命性的突破。", "fail", "hype.lexicon"),
    ("R8-AI腔泛化结尾", "本文提出新方法。", "本文提出新方法，具有重要意义。", "fail", "aiflavor.lexicon"),
    ("R8-AI腔密度超标", "本文提出方法并分析结构。", "本文深刻揭示了方法原理，充分说明结构的合理性，该方法不可或缺。", "fail", "aiflavor.lexicon"),
    ("R9-长度膨胀", "本文提出一种方法。", "本文提出一种方法，该方法在设计层面充分考虑了多方面的因素，综合运用了多种先进的技术手段，在理论上具有重要的指导意义，在实验上也进行了充分的验证与分析。", "fail", "structure.inflation"),
    ("R9-孤儿新增句", "方法在两项任务上验证。", "方法在两项任务上验证。据估计，该技术市场规模将在三年内翻倍。", "fail", "structure.inflation"),
    ("R10-语义漂移", "实验采用 8:1:1 的数据划分。", "实验按照一定比例对数据进行了划分。", "warn", "semantic.drift"),
    ("合法拆句不判臆造", "编码器由三个模块堆叠而成，每个模块包含两个卷积层。",
     "编码器由三个模块堆叠而成。每个模块包含两个卷积层。", "pass", None),
]

EN_CASES = [
    ("合法同义改写", "The model reaches an accuracy of 91.2% on the test set [1].",
     "The model achieves an accuracy of 91.2% on the test set [1].", "pass", None),
    ("R1-新增数值", "Accuracy reaches 91.2%.", "Accuracy reaches 95.6%.", "fail", "numeric.conservation"),
    ("R2-极性翻转", "The method reduces RRMSE by 35.33%.", "The method increases RRMSE by 35.33%.", "fail", "numeric.polarity"),
    ("R3-引用丢失", "Prior work [1] [2] studied this.", "Prior work [1] studied this.", "fail", "citation.conservation"),
    ("R4-图表编号丢失", "Results appear in Fig. 3.", "Results appear in the figure.", "fail", "anchor.conservation"),
    ("R5-对冲硬化", "The method may suit real-time use.", "The method proves suitable for real-time use.", "fail", "hedge.flip"),
    ("R7-浮夸残留", "The method is effective.", "The method is an unprecedented breakthrough.", "fail", "hype.lexicon"),
    ("R8-AI腔", "We propose NLGAN.", "We propose NLGAN, which has significant implications for the field.", "fail", "aiflavor.lexicon"),
]


class GuardrailRuleTests(unittest.TestCase):
    def _run_cases(self, cases, language):
        for name, src, pol, expect_verdict, expect_rule in cases:
            with self.subTest(name=name):
                report = evaluate_pair(src, pol, language=language, strength="deep")
                rules = {item.rule for item in report.violations}
                self.assertIn(
                    report.verdict, (expect_verdict, "fail") if expect_verdict != "pass" else ("pass",),
                    f"{name}: verdict={report.verdict} rules={rules}",
                )
                if expect_rule:
                    self.assertIn(expect_rule, rules, f"{name}: 期望规则 {expect_rule}，实际 {rules}")

    def test_chinese_cases(self):
        self._run_cases(CN_CASES, "cn")

    def test_english_cases(self):
        self._run_cases(EN_CASES, "en")

    def test_strict_mode_escalates_warning_to_fail(self):
        report = evaluate_pair(
            "在本研究中，所提方法优于基线。", "所提方法优于基线。", language="cn", strength="deep", strict=True
        )
        self.assertEqual(report.verdict, "fail")

    def test_empty_polish_fails_closed(self):
        report = evaluate_pair("准确率达到 91.2%。", "", language="cn", strength="deep")
        self.assertEqual(report.verdict, "fail")
        self.assertEqual(report.violations[0].evidence["kind"], "empty_text")

    def test_attribute_feedback_groups_by_rule(self):
        report = evaluate_pair("准确率达到 91.2%。", "准确率达到 95.6%，是革命性的突破。", language="cn", strength="deep")
        feedback = attribute_feedback(report.violations, language="cn")
        self.assertIn("数值守恒", feedback)
        self.assertIn("浮夸表述", feedback)
        self.assertIn("95.6%", feedback)

    def test_citation_index_bounds_with_references(self):
        references = "[1] A. Author. Title. Journal, 2020.\n[2] B. Author. Title. Journal, 2021."
        ok = evaluate_pair(
            "方法有效 [1] [2]。", "该方法被证明有效 [1] [2]。",
            language="cn", strength="deep", references_text=references,
        )
        self.assertNotIn("citation.conservation", {v.rule for v in ok.violations})
        bad = evaluate_pair(
            "方法有效 [1] [2]。", "该方法被证明有效 [1] [2] [9]。",
            language="cn", strength="deep", references_text=references,
        )
        self.assertIn("citation.conservation", {v.rule for v in bad.violations})

    def test_large_text_performance(self):
        import time

        base = "本文提出一种方法，在数据集上将 SNR 提升了 18.90%，较基线降低 RRMSE 达 35.33% [1]。实验在三种噪声强度下完成，结果如表 1 所示，架构如图 3 所示。"
        text = base * 600  # ≈ 10 万字符
        polished = text.replace("提升了", "提高了")
        started = time.perf_counter()
        report = evaluate_pair(text, polished, language="cn", strength="deep")
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertLess(elapsed_ms, 5000, f"10 万字段落耗时 {elapsed_ms:.0f}ms 超过 5s 上限")
        self.assertEqual(report.metrics["paragraphs_source"], report.metrics["paragraphs_polished"])


class FailClosedTests(unittest.TestCase):
    """polish_engine：门禁 fail 且重试耗尽 → 强制装配原文。"""

    def setUp(self):
        self.module = skill_module("cn-polish")

    def test_failed_chunk_falls_back_to_source_when_retries_exhausted(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            jobs_file = Path(tmp) / "jobs.json"
            built = self.module.run(
                {"mode": "build", "text": "准确率达到 91.2%。", "strength": "deep", "out": str(jobs_file)}
            )
            job = built["jobs"][0]
            # 第一次 apply：坏响应（丢失数字）→ 生成重试作业
            first = self.module.run(
                {
                    "mode": "apply",
                    "text": "准确率达到 91.2%。",
                    "jobs": [dict(job, response="准确率达到较高水平。")],
                    "strength": "deep",
                }
            )
            self.assertTrue(first["retry_jobs"], "坏响应应生成重试作业")
            # 第二次 apply：重试响应仍然坏 → 重试耗尽 → Fail-Closed 回退原文
            retry_job = dict(first["retry_jobs"][0], response="准确率很高。")
            second = self.module.run(
                {
                    "mode": "apply",
                    "text": "准确率达到 91.2%。",
                    "jobs": [job, retry_job],
                    "strength": "deep",
                }
            )
            self.assertIn("91.2%", second["polished_text"], "Fail-Closed 后终稿必须回退为原文")
            fallback = [r for r in second["chunk_reports"] if r.get("fallback") == "source"]
            self.assertTrue(fallback, "chunk_reports 应记录 fallback=source")
            self.assertIn("91.2%", fallback[0]["fallback_reason"])


if __name__ == "__main__":
    unittest.main()


class FailClosedDocxLoopTests(unittest.TestCase):
    """SLA 第 3 项：门禁 fail 且重试耗尽 → 终稿 100% 回退原文 + docx 含批注告警。"""

    def test_batch_fail_closed_with_docx_comments(self):
        import json
        import tempfile
        from pathlib import Path

        from conftest import skill_module
        from utils.docx_io import create_minimal_docx, read_comments

        batch = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docx = create_minimal_docx(
                root / "paper.docx",
                [
                    ("Title", "EEG Paper"),
                    ("Heading 1", "1 Introduction"),
                    ("", "The model reaches an accuracy of 91.2% on EEGDenoiseNet [1], as shown in Fig. 3."),
                ],
            )
            out = root / "out"
            built = batch.run(
                {"inputs": [str(docx)], "language": "cn", "phase": "build", "out": str(out)}
            )
            item = built["items"][0]
            result_dir = Path(item["result_file"]).parent
            responses = Path(item["responses_dir"])
            jobs = json.loads((result_dir / "jobs.json").read_text(encoding="utf-8"))["jobs"]
            self.assertTrue(jobs)
            for job in jobs:
                (responses / f"{job['job_id']}.txt").write_text(
                    job["chunk_content"].replace("91.2%", "较高水平").replace("Fig. 3", "图"),
                    encoding="utf-8",
                )
            first = batch.run(
                {"inputs": [str(docx)], "language": "cn", "phase": "apply", "out": str(out)}
            )
            self.assertEqual(first["items"][0]["status"], "needs_review")
            retry_path = result_dir / "retry-jobs.json"
            self.assertTrue(retry_path.exists(), "坏响应必须生成重试作业")
            retries = json.loads(retry_path.read_text(encoding="utf-8"))["jobs"]
            for job in retries:
                (responses / f"{job['job_id']}.txt").write_text(
                    job["chunk_content"].replace("91.2%", "很高的准确率"),
                    encoding="utf-8",
                )
            batch.run({"inputs": [str(docx)], "language": "cn", "phase": "apply", "out": str(out)})
            final_text = (result_dir / "paper.processed.txt").read_text(encoding="utf-8")
            self.assertIn("91.2%", final_text, "Fail-Closed 后终稿必须回退为原文")
            self.assertIn("Fig. 3", final_text)
            result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(result["guardrail_summary"]["fallback_chunks"], 1)
            docx_target = result_dir / "paper.polished.docx"
            self.assertTrue(docx_target.exists())
            comments = read_comments(docx_target)
            self.assertTrue(comments, "Fail-Closed 段落必须在 docx 中打批注告警")
            self.assertEqual(len(comments), 1, "一个失败 chunk 只应生成一条汇总批注")
            self.assertTrue(any("回退" in c["text"] for c in comments))


class StrictBatchGuardrailTests(unittest.TestCase):
    """batch-processor strict_guardrails 必须把命中的 warning 走重试并最终回退原文。"""

    def test_strict_warning_is_retried_then_fail_closed(self):
        import json
        import tempfile
        from pathlib import Path

        batch = skill_module("batch-processor")
        source = "The generator receives noisy input and estimates artifact components."
        bad = "The generator processes the input and produces estimates."
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper.md"
            paper.write_text("# Test\n\n## Method\n\n" + source + "\n", encoding="utf-8")
            out = root / "out"
            built = batch.run(
                {"inputs": [str(paper)], "language": "en", "phase": "build", "out": str(out)}
            )
            item = built["items"][0]
            result_dir = Path(item["result_file"]).parent
            responses = Path(item["responses_dir"])
            jobs = json.loads((result_dir / "jobs.json").read_text(encoding="utf-8"))["jobs"]
            for job in jobs:
                (responses / f"{job['job_id']}.txt").write_text(bad, encoding="utf-8")

            first = batch.run(
                {
                    "inputs": [str(paper)],
                    "language": "en",
                    "phase": "apply",
                    "out": str(out),
                    "strict_guardrails": True,
                }
            )
            self.assertEqual(first["items"][0]["status"], "needs_review")
            retry_path = result_dir / "retry-jobs.json"
            self.assertTrue(retry_path.exists())
            retries = json.loads(retry_path.read_text(encoding="utf-8"))["jobs"]
            for job in retries:
                (responses / f"{job['job_id']}.txt").write_text(bad, encoding="utf-8")

            second = batch.run(
                {
                    "inputs": [str(paper)],
                    "language": "en",
                    "phase": "apply",
                    "out": str(out),
                    "strict_guardrails": True,
                }
            )
            final_text = (result_dir / "paper.processed.md").read_text(encoding="utf-8")
            result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(second["items"][0]["status"], "needs_review")
            self.assertIn(source, final_text)
            self.assertNotIn(bad, final_text)
            self.assertGreaterEqual(result["guardrail_summary"]["fallback_chunks"], 1)
