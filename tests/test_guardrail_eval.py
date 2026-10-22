"""guardrail_eval：evaluator guardrails 模式的 SLA 验收测试。

验收门槛（docs/hallucination-guardrails-plan.md L4）：
- 负样本拦截率 Recall = 1.0（expect_severity=error 必须 100% 判 fail）；
- 正样本误杀率 FPR = 0.0；
- 每条规则均有 ≥10 条负样本覆盖。
"""

import unittest

from conftest import skill_module


class GuardrailEvalSlaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = skill_module("evaluator")

    def _run(self, language):
        return self.module.evaluate(language=language, mode="guardrails", strength="deep")

    def test_chinese_sla(self):
        report = self._run("cn")
        guard = report["suites"]["guardrails"]
        self.assertEqual(guard["recall"], 1.0, f"中文负样本漏拦: {guard['leaked']}")
        self.assertEqual(guard["fpr"], 0.0, f"中文正样本误杀: {guard['false_positives']}")
        self.assertEqual(guard["warning_rule_hit_rate"], 1.0)
        self.assertGreaterEqual(guard["negative_count"], 100)
        self.assertGreaterEqual(guard["positive_count"], 20)

    def test_english_sla(self):
        report = self._run("en")
        guard = report["suites"]["guardrails"]
        self.assertEqual(guard["recall"], 1.0, f"英文负样本漏拦: {guard['leaked']}")
        self.assertEqual(guard["fpr"], 0.0, f"英文正样本误杀: {guard['false_positives']}")
        self.assertEqual(guard["warning_rule_hit_rate"], 1.0)
        self.assertGreaterEqual(guard["negative_count"], 100)
        self.assertGreaterEqual(guard["positive_count"], 20)

    def test_rule_coverage_at_least_ten_per_rule(self):
        import json
        from collections import Counter
        from pathlib import Path

        for language in ("cn", "en"):
            cases = json.loads(
                Path(f"data/eval/guardrails-negative-{language}.json").read_text(encoding="utf-8")
            )["cases"]
            counter = Counter(case["rule"] for case in cases)
            for rule, count in counter.items():
                self.assertGreaterEqual(count, 10, f"{language} 规则 {rule} 负样本仅 {count} 条")

    def test_overall_metrics_exposed(self):
        report = self._run("cn")
        self.assertEqual(report["overall"]["guardrail_recall"], 1.0)
        self.assertEqual(report["overall"]["guardrail_fpr"], 0.0)


if __name__ == "__main__":
    unittest.main()
