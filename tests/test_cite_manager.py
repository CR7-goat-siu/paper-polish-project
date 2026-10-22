"""cite-manager：引用编号管理的确定性测试（audit / apply / renumber / pending / docx_plan）。"""

import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils.docx_io import create_minimal_docx, read_paragraphs, write_polished_docx

TEXT = (
    "Prior work [1] established the baseline. Later studies [2-3] extended it, "
    "and [1，2] both report gains.\n"
    "\n"
    "References\n"
    "[1] A. Author. First paper. Journal, 2020, pp. 1-10.\n"
    "[2] B. Author. Second paper. Journal, 2021, vol. 2, no. 3.\n"
    "[3] C. Author. Third paper. Journal, 2022, pp. 5-9.\n"
)

PARAGRAPHS = [
    "Prior work [1] established the baseline. Later studies [2-3] extended it, "
    "and [1，2] both report gains.",
    "References",
    "[1] A. Author. First paper. Journal, 2020, pp. 1-10.",
    "[2] B. Author. Second paper. Journal, 2021, vol. 2, no. 3.",
    "[3] C. Author. Third paper. Journal, 2022, pp. 5-9.",
]


class CiteManagerAuditTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("cite-manager")

    def test_audit_reports_consistency(self):
        result = self.module.run({"text": TEXT, "mode": "audit"})
        audit = result["audit"]
        self.assertEqual(audit["listed"], [1, 2, 3])
        self.assertEqual(audit["cited_not_listed"], [])
        self.assertEqual(audit["listed_not_cited"], [])
        self.assertEqual(audit["cited"]["1"], 2)  # [1] 与 [1，2] 各一次
        self.assertEqual(audit["cited"]["3"], 1)  # [2-3] 展开计数
        self.assertEqual(audit["ref_entry_count"], 3)

    def test_audit_finds_missing_and_uncited(self):
        text = TEXT + "A new claim cites [9] without an entry.\n"
        result = self.module.run({"text": text, "mode": "audit"})
        audit = result["audit"]
        self.assertEqual(audit["cited_not_listed"], [9])
        # 原文 1/2/3 均被引用，无未引用条目
        self.assertEqual(audit["listed_not_cited"], [])


class CiteManagerApplyTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("cite-manager")

    def test_apply_rewrites_markers_with_dedupe_and_sort(self):
        result = self.module.run(
            {"text": TEXT, "mode": "apply", "mapping": {"2": 1}, "compress_ranges": False}
        )
        text = result["text"]
        self.assertIn("[1, 3]", text)  # [2-3] -> [1, 3]
        self.assertIn("[1]", text)  # [1，2] -> [1]（去重 + 全角逗号归一）
        # 未开启 reorder_list 时列表不动
        self.assertIn("[2] B. Author", text)
        pairs = {old: new for old, new in result["token_pairs"]}
        self.assertEqual(pairs.get("[2-3]"), "[1, 3]")
        self.assertEqual(pairs.get("[1，2]"), "[1]")

    def test_apply_compresses_long_runs(self):
        text = "Methods [1,2,3,4] are combined.\n\nReferences\n[1] A. Journal 2020.\n[2] B. Journal 2021.\n[3] C. Journal 2022.\n[4] D. Journal 2023.\n"
        result = self.module.run({"text": text, "mode": "apply", "mapping": {}})
        self.assertIn("[1-4]", result["text"])

    def test_apply_without_hit_keeps_marker_verbatim(self):
        # 零改动原则：映射未命中的标记不做风格归一（[2-3] 不被展开为 [2, 3]）
        result = self.module.run({"text": TEXT, "mode": "apply", "mapping": {}})
        self.assertIn("[2-3]", result["text"])
        self.assertIn("[1，2]", result["text"])
        self.assertEqual(result["token_pairs"], [])

    def test_apply_with_reorder_list_renumbers_entries(self):
        result = self.module.run(
            {"text": TEXT, "mode": "apply", "mapping": {"3": 4}, "reorder_list": True}
        )
        self.assertIn("[4] C. Author. Third paper.", result["text"])
        # [2-3] 引用集合 {2,3} 映射后为 {2,4}：3 已被重映射，不连续不可压缩
        self.assertIn("[2, 4]", result["text"])

    def test_apply_reorder_conflict_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.module.run(
                {"text": TEXT, "mode": "apply", "mapping": {"3": 2}, "reorder_list": True}
            )
        self.assertIn("重复编号", str(ctx.exception))

    def test_apply_mapping_as_json_string(self):
        result = self.module.run(
            {"text": TEXT, "mode": "apply", "mapping": '{"2": 1}', "compress_ranges": False}
        )
        self.assertIn("[1, 3]", result["text"])


class CiteManagerRenumberTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("cite-manager")
        self.text = (
            "Prior work [1] and [2] are used directly.\n"
            "\n"
            "References\n"
            "[1] A. Author. Journal, 2020, pp. 1-10.\n"
            "[2] B. Author. Journal, 2021, pp. 2-4.\n"
            "[3] C. Author. Journal, 2022, pp. 5-9.\n"
        )

    def test_renumber_removes_entry_and_compacts(self):
        result = self.module.run({"text": self.text, "mode": "renumber", "remove": [3]})
        self.assertNotIn("C. Author", result["text"])
        self.assertEqual(result["mapping_used"], {"1": 1, "2": 2})
        self.assertIn("[1] A. Author", result["text"])
        self.assertIn("[2] B. Author", result["text"])

    def test_renumber_empty_remove_raises(self):
        with self.assertRaises(ValueError):
            self.module.run({"text": self.text, "mode": "renumber", "remove": []})

    def test_renumber_removed_but_still_cited_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.module.run({"text": self.text, "mode": "renumber", "remove": [2]})
        self.assertIn("仍被正文引用", str(ctx.exception))


class CiteManagerPendingTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("cite-manager")
        self.text = (
            "Prior work established the baseline. The adaptation follows common practice.\n"
        )

    def test_mark_inserts_placeholder_after_unique_anchor(self):
        result = self.module.run(
            {
                "text": self.text,
                "mode": "pending",
                "action": "mark",
                "anchors": ["established the baseline"],
                "language": "en",
            }
        )
        self.assertIn("established the baseline [CITE-TBD]", result["text"])
        self.assertEqual(result["inserted"][0]["token"], "[CITE-TBD]")

    def test_mark_cn_placeholder(self):
        result = self.module.run(
            {
                "text": "已有工作奠定了基线。本文沿用常见做法。",
                "mode": "pending",
                "action": "mark",
                "anchors": ["奠定了基线"],
                "language": "cn",
            }
        )
        self.assertIn("奠定了基线[待补文献]", result["text"])

    def test_mark_ambiguous_anchor_raises(self):
        text = "baseline A. baseline B.\n"
        with self.assertRaises(ValueError) as ctx:
            self.module.run(
                {"text": text, "mode": "pending", "action": "mark", "anchors": ["baseline"]}
            )
        self.assertIn("恰好 1 次", str(ctx.exception))

    def test_list_finds_pending_tokens(self):
        text = "One [CITE-TBD] and another [待补文献] spot.\n"
        result = self.module.run({"text": text, "mode": "pending", "action": "list"})
        self.assertEqual(result["pending_count"], 2)
        tokens = {f["token"] for f in result["pending"]}
        self.assertEqual(tokens, {"[CITE-TBD]", "[待补文献]"})


class CiteManagerDocxPlanTests(unittest.TestCase):
    def test_docx_plan_end_to_end_with_surgical_write(self):
        module = skill_module("cite-manager")
        text = "\n\n".join(PARAGRAPHS)
        result = module.run(
            {
                "text": text,
                "mode": "apply",
                "mapping": {"1": 4},
                "reorder_list": True,
                "paragraphs": PARAGRAPHS,
            }
        )
        plan = result["docx_plan"]
        # 段 0 正文：[1] -> [4]，[1，2] 引用集合 {1,2} 映射后为 {2,4}；[2-3] 未命中保持原样
        self.assertEqual(
            plan.get(0),
            {"token_replacements": [("[1]", "[4]"), ("[1，2]", "[2, 4]")]},
        )
        # 段 2 条目 [1] -> [4]；References 标题（段 1）与未变化条目（段 3/4）不在计划内
        self.assertEqual(plan.get(2), {"token_replacements": [("[1]", "[4]")]})
        self.assertNotIn(1, plan)
        self.assertNotIn(3, plan)
        self.assertNotIn(4, plan)
        with tempfile.TemporaryDirectory() as tmp:
            source = create_minimal_docx(
                Path(tmp) / "paper.docx", [("", p) for p in PARAGRAPHS]
            )
            target = Path(tmp) / "renumbered.docx"
            write_polished_docx(source, target, dict(plan))
            paragraphs = read_paragraphs(target)
            self.assertEqual(
                paragraphs[0]["text"],
                "Prior work [4] established the baseline. "
                "Later studies [2-3] extended it, and [2, 4] both report gains.",
            )
            self.assertEqual(
                paragraphs[2]["text"],
                "[4] A. Author. First paper. Journal, 2020, pp. 1-10.",
            )
            # 未列入计划的段落保持原样
            self.assertEqual(paragraphs[4]["text"], PARAGRAPHS[4])


if __name__ == "__main__":
    unittest.main()
