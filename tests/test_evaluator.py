import tempfile
import unittest
from pathlib import Path

from conftest import skill_module


def oracle_polisher(text: str, meta: dict) -> str:
    """oracle 润色器：直接返回参考答案，用于校准评测器上界。"""
    return str(meta.get("reference") or text)


def identity_polisher(text: str, meta: dict) -> str:
    return text


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("evaluator")

    def test_oracle_scores_near_perfect(self):
        report = self.module.evaluate(
            language="en",
            mode="oracle",
            strength="deep",
            polisher=oracle_polisher,
        )
        overall = report["overall"]
        self.assertGreaterEqual(overall["dehype_rate"], 0.9)
        self.assertGreaterEqual(overall["terminology_recall"], 0.9)
        self.assertGreaterEqual(overall["coined_word_interception"], 0.9)
        self.assertGreaterEqual(overall["gleu_avg"], 0.9)
        self.assertGreaterEqual(overall["number_preservation_rate"], 0.9)

    def test_identity_scores_floor(self):
        report = self.module.evaluate(
            language="en",
            mode="offline",
            strength="deep",
            polisher=identity_polisher,
        )
        overall = report["overall"]
        # 原样输出：去浮夸失败、术语召回为零、过度修正为零
        self.assertLessEqual(overall["dehype_rate"], 0.2)
        self.assertLessEqual(overall["terminology_recall"], 0.2)
        self.assertEqual(overall["overcorrection_rate"], 0.0)

    def test_cn_suites_run(self):
        report = self.module.evaluate(
            language="cn",
            mode="oracle",
            strength="deep",
            polisher=oracle_polisher,
        )
        self.assertIn("objectivity", report["suites"])
        self.assertIn("terminology", report["suites"])
        self.assertIn("grammar", report["suites"])  # grammar-cn.json 已补齐

    def test_run_writes_reports_and_ab(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.module.run(
                {
                    "language": "en",
                    "mode": "oracle",
                    "strength": "deep",
                    "out": str(tmp),
                    "prepare_ab": True,
                    "polisher": oracle_polisher,
                }
            )
            out_dir = Path(report["output"])
            self.assertTrue((out_dir / "report.json").exists())
            self.assertTrue((out_dir / "report.md").exists())
            self.assertTrue((out_dir / "ab" / "answer-key.json").exists())
            pairs = list((out_dir / "ab").glob("pair-*"))
            self.assertGreater(len(pairs), 0)


    def test_prepare_ab_blinds_labels(self):
        """固定种子的确定性盲评断言：一次调用内 A/B 两种指派都要出现。

        原实现无种子随机循环 8 次，存在 1/128 的全同标签偶发失败。
        """
        import json as _json
        import random as _random
        from unittest import mock

        pairs = [
            {"name": f"t{i}", "source": f"original text {i}", "polished": f"polished text {i}"}
            for i in range(6)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            # 种子 1 的 6 次 shuffle 产生 B,B,A,B,A,A（两种指派各半）
            real_random_cls = _random.Random
            with mock.patch("random.Random", lambda: real_random_cls(1)):
                key = self.module.prepare_ab(
                    {}, Path(tmp) / "ab", language="en", pairs=pairs
                )
            data = _json.loads(Path(key).read_text(encoding="utf-8"))
            labels = {entry["polished"] for entry in data}
        self.assertEqual(labels, {"A", "B"}, "A/B 指派必须随机化，不能恒为单侧标签")

    def test_pair_score_prefers_clean_candidate_and_blocks_number_loss(self):
        source = (
            "The proposed method is very good and totally solves the problem. "
            "Accuracy was 91.2% [1]."
        )
        clean = "The proposed method is effective. Accuracy was 91.2% [1]."
        lost_number = "The proposed method is amazing and solves everything."
        clean_score = self.module.run(
            {
                "mode": "pair",
                "source_text": source,
                "polished_text": clean,
                "language": "en",
                "strength": "deep",
            }
        )
        loss_score = self.module.run(
            {
                "mode": "pair",
                "source_text": source,
                "polished_text": lost_number,
                "language": "en",
                "strength": "deep",
            }
        )
        self.assertTrue(clean_score["hard_gate_pass"])
        self.assertFalse(loss_score["hard_gate_pass"])
        self.assertGreater(clean_score["score"], loss_score["score"])
        self.assertEqual(clean_score["metrics"]["numbers_missing"], [])
        self.assertIn("91.2", str(loss_score["metrics"]["numbers_missing"]))


if __name__ == "__main__":
    unittest.main()
