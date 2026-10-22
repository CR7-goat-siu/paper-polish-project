import unittest

from utils.eval_metrics import (
    correction_stats,
    gleu,
    objectivity_score,
    terminology_scores,
    term_presence,
)


class EvalMetricsTests(unittest.TestCase):
    def test_gleu_bounds_and_ordering(self):
        source = "He don't like apples."
        reference = "He does not like apples."
        perfect = gleu(source, "He does not like apples.", reference)
        unchanged = gleu(source, source, reference)
        wrong = gleu(source, "She hates pears.", reference)
        self.assertGreater(perfect, unchanged)
        self.assertGreaterEqual(perfect, 0.99)
        self.assertLess(unchanged, perfect)
        self.assertLess(wrong, perfect)
        self.assertLess(wrong, 0.3)

    def test_correction_stats_token_matching(self):
        stats = correction_stats(
            "trained for 100 epoch",
            "trained for 100 epochs",
            ["100 epoch", "on 8 GPU"],
        )
        # "100 epoch" 被 "100 epochs" 修复（词级匹配），"on 8 GPU" 未出现于假设也算修复
        self.assertEqual(stats["fixed"], ["100 epoch", "on 8 GPU"])
        self.assertEqual(stats["fix_rate"], 1.0)

    def test_term_presence_boundaries(self):
        counts = term_presence(
            "The convolution neural network baseline vs convolutional neural network.",
            ["convolutional neural network", "convolution"],
            "en",
        )
        self.assertEqual(counts["convolutional neural network"], 1)
        self.assertEqual(counts["convolution"], 1)  # 不应计入 convolutional 的子串

    def test_terminology_scores(self):
        scores = terminology_scores(
            "The convolutional neural network works.",
            ["convolutional neural network"],
            ["convolution neural network"],
            "en",
        )
        self.assertEqual(scores["recall"], 1.0)
        self.assertEqual(scores["interception"], 1.0)

    def test_objectivity_score(self):
        good = objectivity_score("s", "improves accuracy by 3.1%", ["massive"], 0, 0)
        self.assertTrue(good["success"])
        bad = objectivity_score("s", "massive improvement", ["massive"], 1, 1)
        self.assertFalse(bad["success"])


if __name__ == "__main__":
    unittest.main()
