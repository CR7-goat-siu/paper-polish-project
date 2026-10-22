import tempfile
import unittest
from pathlib import Path

from conftest import skill_module


class TermCheckTests(unittest.TestCase):
    def test_english_nonstandard_and_alias(self):
        module = skill_module("term-check")
        report = module.run(
            {
                "text": "Our convolutional neural network uses a conv net. "
                "We compare against a convolution neural network baseline.",
                "language": "en",
                "mode": "local",
            }
        )
        self.assertGreaterEqual(report["summary"]["total"], 1)
        issues = [f["issue"] for f in report["findings"]]
        self.assertIn("nonstandard_use", issues)

    def test_chinese_local_report(self):
        module = skill_module("term-check")
        report = module.run(
            {
                "text": "该模型采用褶积网络提取特征，并使用学习速率 0.001 进行训练。",
                "language": "cn",
                "mode": "local",
            }
        )
        issues = {f.get("issue") for f in report["findings"]}
        self.assertTrue(issues)


if __name__ == "__main__":
    unittest.main()
