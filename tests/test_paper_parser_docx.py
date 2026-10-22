import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils.docx_io import create_minimal_docx


class PaperParserDocxTests(unittest.TestCase):
    def test_parse_docx_chunks(self):
        module = skill_module("paper-parser")
        with tempfile.TemporaryDirectory() as tmp:
            path = create_minimal_docx(
                Path(tmp) / "paper.docx",
                [
                    ("Title", "EEG Study"),
                    ("Heading 1", "Abstract"),
                    ("", "We study EEG signals with 91.2% accuracy."),
                    ("Heading 1", "1 Introduction"),
                    ("", "We compare against baselines [1] and cite prior work [2]."),
                    ("Heading 1", "Method"),
                    ("", "The proposed model uses gated units."),
                    ("Heading 1", "References"),
                    ("", "[1] A. Author. Title. Journal, 2020."),
                ],
            )
            result = module.run({"input": str(path)})
        self.assertEqual(result["format"], "docx")
        self.assertEqual(result["title"], "EEG Study")
        kinds = [c["kind"] for c in result["chunks"]]
        self.assertIn("front_matter", kinds)
        self.assertIn("abstract", kinds)
        self.assertIn("references", kinds)
        self.assertTrue(result["integrity"]["reconstructs"])
        # 每个块携带段落索引，供 docx 回写对齐
        for chunk in result["chunks"]:
            self.assertTrue(chunk["paragraph_indexes"])
        headings = [c["heading"] for c in result["chunks"] if c["kind"] == "section"]
        self.assertIn("1 Introduction", headings)

    def test_detect_format_docx(self):
        module = skill_module("paper-parser")
        self.assertEqual(module.detect_format(None, "a.docx"), "docx")
        self.assertEqual(module.detect_format("docx", None), "docx")


if __name__ == "__main__":
    unittest.main()
