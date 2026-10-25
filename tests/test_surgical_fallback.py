"""句级外科式 Fail-Closed 回退的测试。

场景：门禁 fail 且重试耗尽时，只回退违例句（数字被改的句子），
保留块内已通过审查的润色句子；句数无法对齐时退回整块回退。
"""

import unittest

from conftest import skill_module


def _job(job_id: str, chunk_id: str, attempt: int, response: str, content: str) -> dict:
    return {
        "job_id": job_id,
        "chunk_id": chunk_id,
        "skill": "cn-polish",
        "attempt": attempt,
        "response": response,
        "chunk_content": content,
    }


SOURCE = (
    "本方法的去噪准确率达到 94.7%，信噪比提升 12.5 dB [1]。"
    "该模块能够从多个频带当中挖掘有效的时频域特征信息。"
    "实验在自建数据集上完成，采用 10 折交叉验证。"
)


class SurgicalFallbackTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("cn-polish")

    def _apply(self, response: str):
        jobs = [
            _job("c001-a1", "c001", 1, response, SOURCE),
            _job("c001-a2", "c001", 2, response, SOURCE),
        ]
        return self.module.apply_responses(
            SOURCE,
            jobs,
            strength="deep",
            references_text="[1] Zhang et al. Sample. Journal, 2020.",
        )

    def test_violating_sentence_restored_good_sentence_kept(self):
        # 句 1 违例（94.7%→95.1%，[1] 丢失）；句 2 是合法润色；句 3 原样
        response = (
            "本方法的去噪准确率达到 95.1%，信噪比提升 12.5 dB。"
            "该模块能够从多个频带中挖掘有效的时频域特征。"
            "实验在自建数据集上完成，采用 10 折交叉验证。"
        )
        result = self._apply(response)
        polished = result["polished_text"]
        # 违例句回退为原文：数字与引用完整保留
        self.assertIn("94.7%", polished)
        self.assertIn("[1]", polished)
        self.assertNotIn("95.1%", polished)
        # 合法润色句被保留（外科修复的价值所在）
        self.assertIn("从多个频带中挖掘", polished)
        self.assertNotIn("当中", polished)
        # chunk_reports 记录外科修复而非整块回退
        report = result["chunk_reports"][0]
        self.assertEqual(report.get("fallback"), "surgical")
        repairs = report.get("surgical_repairs") or {}
        self.assertEqual(repairs.get("restored"), 1)
        self.assertEqual(repairs.get("total"), 3)

    def test_misaligned_sentence_counts_fall_back_to_source(self):
        # 句数不一致（两句并一句）→ 无法逐句对齐 → 整块回退原文
        response = "本方法的去噪准确率达到 95.1%，信噪比提升 12.5 dB。实验合并了句子。"
        result = self._apply(response)
        self.assertEqual(result["polished_text"], SOURCE)
        report = result["chunk_reports"][0]
        self.assertEqual(report.get("fallback"), "source")

    def test_all_sentences_violating_fall_back_to_source(self):
        # 全部句子违例 → 外科修复后等于原文（或终检失败）→ 不放行任何违例文本
        response = (
            "本方法的去噪准确率达到 95.1%，信噪比提升 13.0 dB。"
            "该模块完全颠覆了传统方法。"
            "实验在自建数据集上完成，采用 12 折交叉验证。"
        )
        result = self._apply(response)
        polished = result["polished_text"]
        self.assertIn("94.7%", polished)
        self.assertIn("10 折", polished)
        self.assertNotIn("95.1%", polished)
        self.assertNotIn("12 折", polished)


if __name__ == "__main__":
    unittest.main()
