import unittest

from utils.prompt_renderer import PromptTemplateError, list_templates, render_template


class PromptRendererTests(unittest.TestCase):
    def test_render_english_deep_template(self):
        text = render_template(
            "polish-deep.md",
            "en",
            title="Test",
            section_type="Method",
            context="",
            strength_label="Deep polish",
            feedback="(none)",
            rules="{}",
            terms="{}",
            term_corrections="(none)",
            comments="(none)",
            brief="(none)",
            anchors="- Numbers: 94.2%",
            self_check="1. Numbers match?",
            text="We propose a method.",
        )
        self.assertIn("We propose a method.", text)
        self.assertNotIn("{{", text)

    def test_render_chinese_light_template(self):
        text = render_template(
            "polish-light.md",
            "cn",
            title="测试",
            section_type="方法",
            context="",
            strength_label="轻度润色",
            feedback="（无）",
            rules="{}",
            terms="{}",
            term_corrections="（无）",
            comments="（无）",
            brief="（无）",
            anchors="- 数值: 91.2%",
            self_check="1. 数值一致？",
            text="本文提出一种方法。",
        )
        self.assertIn("本文提出一种方法。", text)

    def test_missing_variable_raises(self):
        with self.assertRaises(PromptTemplateError):
            render_template("polish-light.md", "en", text="x")

    def test_anchor_and_self_check_blocks_render(self):
        text = render_template(
            "polish-light.md",
            "cn",
            title="测试",
            section_type="方法",
            context="",
            strength_label="轻度润色",
            feedback="（无）",
            rules="{}",
            terms="{}",
            term_corrections="（无）",
            comments="（无）",
            brief="（无）",
            anchors="- 数值: 91.2% / 0.103",
            self_check="1. 数值一致？\n2. 无新增？",
            text="本文提出一种方法。",
        )
        self.assertIn("必保锚点", text)
        self.assertIn("1. 数值一致？", text)
        self.assertIn("严禁新增", text)

    def test_template_lists_are_discoverable(self):
        self.assertIn("polish-deep.md", list_templates("en"))
        self.assertIn("polish-deep.md", list_templates("cn"))


if __name__ == "__main__":
    unittest.main()
