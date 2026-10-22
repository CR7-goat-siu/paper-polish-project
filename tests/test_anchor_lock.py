"""anchor_lock：五类锚点提取与 Prompt 区块渲染测试。"""

import unittest

from utils.anchor_lock import (
    AnchorSet,
    extract_anchors,
    render_anchor_block,
    render_self_check,
)

SAMPLE = (
    "本文提出一种基于噪声学习的生成对抗网络（NLGAN），在 EEGDenoiseNet 数据集上"
    "将 SNR 提升了 18.90%（p = 0.047），延迟为 12 ms [1]。"
    "如图 3 与表 1 所示，该方法可能优于基线，架构详见式 (2)。"
)


class AnchorLockTests(unittest.TestCase):
    def setUp(self):
        self.anchors = extract_anchors(SAMPLE, language="cn", domain="biomedical-eeg")

    def test_numeric_anchors_extracted_and_deduped(self):
        displays = [item["display"] for item in self.anchors.numeric]
        self.assertIn("18.90%", displays)
        self.assertIn("0.047", displays)
        self.assertIn("12", displays)
        self.assertIn("3", displays)
        self.assertIn("1", displays)
        self.assertEqual(len(displays), len(set(displays)), "数值锚点应去重")

    def test_citation_and_figure_anchors(self):
        self.assertIn("[1]", self.anchors.citation)
        figures = " ".join(self.anchors.figure)
        self.assertIn("图 3", figures)
        self.assertIn("表 1", figures)
        self.assertIn("式 (2)", figures)

    def test_entity_anchors_include_terms_and_abbreviations(self):
        self.assertIn("NLGAN", self.anchors.entity)
        self.assertIn("EEGDenoiseNet", self.anchors.entity)
        self.assertIn("SNR", self.anchors.entity)

    def test_hedge_anchors_capture_polarity_and_hedges(self):
        self.assertIn("提升", self.anchors.hedge)
        self.assertIn("优于", self.anchors.hedge)
        self.assertIn("可能", self.anchors.hedge)

    def test_render_anchor_block_contains_all_sections(self):
        block = render_anchor_block(self.anchors, language="cn")
        self.assertIn("必保锚点", block)
        self.assertIn("18.90%", block)
        self.assertIn("[1]", block)
        self.assertIn("NLGAN", block)
        self.assertIn("未通过审查", block)

    def test_render_self_check_has_six_questions(self):
        for language, marker in (("cn", "输出前自检"), ("en", "Pre-output self-check")):
            text = render_self_check(language)
            self.assertIn(marker, text)
            self.assertEqual(len([line for line in text.split("\n") if line[:2].strip().rstrip(".").isdigit()]), 6)

    def test_empty_anchor_set_renders_fallback(self):
        block = render_anchor_block(AnchorSet(), language="cn")
        self.assertIn("未提取到", block)

    def test_english_anchors(self):
        anchors = extract_anchors(
            "The model improves SNR by 18.90% on EEGDenoiseNet [1], as shown in Fig. 3 and Table 1. "
            "It may outperform the baseline; see Eq. (2).",
            language="en",
        )
        block = render_anchor_block(anchors, language="en")
        self.assertIn("Mandatory anchors", block)
        self.assertIn("18.90%", block)
        self.assertIn("EEGDenoiseNet", block)
        self.assertIn("Fig. 3", block)
        self.assertIn("[1]", block)


if __name__ == "__main__":
    unittest.main()
