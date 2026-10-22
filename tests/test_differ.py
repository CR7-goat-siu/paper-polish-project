import unittest

from utils.differ import diff_stats, render_markdown_diff, similarity_ratio, unified_diff


class DifferTests(unittest.TestCase):
    def test_similarity_identical_and_different(self):
        self.assertEqual(similarity_ratio("same text", "same text"), 1.0)
        ratio = similarity_ratio("one two three", "one 2 three")
        self.assertLess(ratio, 1.0)
        self.assertGreater(ratio, 0.3)

    def test_diff_stats_counts(self):
        stats = diff_stats("We use a big model.", "We use a large model.")
        self.assertEqual(stats["source_words"], stats["polished_words"])
        self.assertGreater(stats["added_words"] + stats["removed_words"], 0)
        self.assertAlmostEqual(stats["similarity_ratio"] + stats["change_ratio"], 1.0, places=3)

    def test_unified_diff_and_markdown(self):
        diff = unified_diff("A.\nB.", "A.\nB changed.")
        self.assertIn("-B.", diff)
        md = render_markdown_diff("A big result.", "A substantial result.", title="t")
        self.assertIn("| 相似度 |", md)
        self.assertIn("### 词级改动片段", md)


if __name__ == "__main__":
    unittest.main()
