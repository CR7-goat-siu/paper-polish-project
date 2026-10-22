import unittest

from conftest import skill_module


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("review-gate")

    def test_pass_on_clean_minor_edit(self):
        result = self.module.evaluate(
            "The model reaches an accuracy of 94.2% on the test set [1].",
            "The model achieves an accuracy of 94.2% on the test set [1].",
            language="en",
            strength="deep",
        )
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["summary"]["error"], 0)

    def test_fail_on_missing_number(self):
        result = self.module.evaluate(
            "The model reaches an accuracy of 94.2% on the test set.",
            "The model reaches a high accuracy on the test set.",
            language="en",
            strength="deep",
        )
        checks = {v["check"] for v in result["violations"]}
        self.assertIn("fidelity.numbers", checks)
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("94.2%", result["retry_feedback"])

    def test_fail_on_weakened_citation(self):
        result = self.module.evaluate(
            "Prior work [1] [2] studies this problem.",
            "Prior work [1] studies this problem.",
            language="en",
            strength="deep",
        )
        checks = {v["check"] for v in result["violations"]}
        self.assertIn("fidelity.citations", checks)

    def test_fail_on_paragraph_change_for_deep(self):
        source = "Paragraph one with content.\n\nParagraph two with content."
        polished = "Paragraph one with content."
        result = self.module.evaluate(source, polished, language="en", strength="deep")
        checks = {v["check"] for v in result["violations"]}
        self.assertIn("structure.paragraphs", checks)

    def test_fail_on_hype_residue(self):
        result = self.module.evaluate(
            "The method improves accuracy by 3.1% over the baseline.",
            "The method is a revolutionary breakthrough, improving accuracy by 3.1% over the baseline.",
            language="en",
            strength="deep",
        )
        checks = {v["check"] for v in result["violations"]}
        self.assertIn("objectivity.hype", checks)
        self.assertEqual(result["verdict"], "fail")

    def test_light_overcorrection_guard(self):
        source = " ".join(
            "The proposed approach demonstrates consistent improvements across multiple benchmark datasets with stable training behavior."
        ) * 2
        heavily_rewritten = " ".join(
            "Our technique shows steady gains over many test sets and the training remains stable throughout."
        ) * 2
        result = self.module.evaluate(
            source, heavily_rewritten, language="en", strength="light"
        )
        checks = {v["check"] for v in result["violations"]}
        self.assertIn("structure.overcorrection", checks)

    def test_chinese_number_loss(self):
        result = self.module.evaluate(
            "该模型在测试集上的准确率达到 91.2%，优于基线方法 [1]。",
            "该模型在测试集上取得了较好的准确率，优于基线方法 [1]。",
            language="cn",
            strength="deep",
        )
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("fidelity.numbers", {v["check"] for v in result["violations"]})

    def test_strict_semantic_warning_is_fail(self):
        result = self.module.evaluate(
            "The generator receives noisy input and estimates artifact components.",
            "The generator processes the input and produces estimates.",
            language="en",
            strength="deep",
            strict=True,
        )
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("semantics.drift", {v["check"] for v in result["violations"]})


if __name__ == "__main__":
    unittest.main()
