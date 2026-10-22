import unittest

from utils.sections import classify_section


class SectionsTests(unittest.TestCase):
    def test_english_headings(self):
        self.assertEqual(classify_section("Abstract", "en"), "abstract")
        self.assertEqual(classify_section("1 Introduction", "en"), "introduction")
        self.assertEqual(classify_section("3.2 Proposed Method", "en"), "method")
        self.assertEqual(classify_section("IV. Experiments and Results", "en"), "experiment")
        self.assertEqual(classify_section("Related Work", "en"), "related_work")
        self.assertEqual(classify_section("5 Conclusion", "en"), "conclusion")
        self.assertEqual(classify_section("Index Terms", "en"), "keywords")

    def test_chinese_headings(self):
        self.assertEqual(classify_section("摘要", "cn"), "abstract")
        self.assertEqual(classify_section("第1章 绪论", "cn"), "introduction")
        self.assertEqual(classify_section("2 系统模型与预备知识", "cn"), "method")
        self.assertEqual(classify_section("3.2 实验结果与分析", "cn"), "experiment")
        self.assertEqual(classify_section("关键词", "cn"), "keywords")

    def test_unknown_or_empty(self):
        self.assertEqual(classify_section("", "en"), "")
        self.assertEqual(classify_section("Appendix A: Proofs", "en"), "")


if __name__ == "__main__":
    unittest.main()
