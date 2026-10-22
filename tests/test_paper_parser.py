import tempfile
import unittest
from pathlib import Path

from conftest import skill_module


class PaperParserTests(unittest.TestCase):
    def _write(self, folder: Path, name: str, text: str) -> Path:
        path = folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_parse_markdown_sections(self):
        module = skill_module("paper-parser")
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                "paper.md",
                "# Attention for Security\n\nIntro text.\n\n## Method\n\nWe use attention.\n\n## Experiments\n\nResults.\n",
            )
            result = module.run({"input": str(path), "format": "markdown"})
        self.assertEqual(result["format"], "markdown")
        self.assertTrue(result["integrity"]["reconstructs"])
        kinds = [chunk["kind"] for chunk in result["chunks"]]
        self.assertIn("section", kinds)
        headings = [chunk.get("heading") for chunk in result["chunks"]]
        self.assertIn("Method", headings)

    def test_parse_latex_preserves_format(self):
        module = skill_module("paper-parser")
        tex = (
            "\\documentclass{article}\n"
            "\\title{Deep Learning for Intrusion Detection}\n"
            "\\begin{document}\n"
            "\\maketitle\n"
            "\\begin{abstract}\n"
            "We study deep learning for security.\n"
            "\\end{abstract}\n"
            "\\section{Introduction}\n"
            "Modern attacks require better detectors.\n"
            "\\begin{thebibliography}{9}\n"
            "\\bibitem{key1} A. Author. Title. Journal, 2024.\n"
            "\\end{thebibliography}\n"
            "\\end{document}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "paper.tex", tex)
            result = module.run({"input": str(path), "format": "latex"})
        self.assertTrue(result["integrity"]["reconstructs"])
        self.assertEqual(result["integrity"]["reconstructed_chars"], len(tex))
        kinds = [chunk["kind"] for chunk in result["chunks"]]
        self.assertIn("preamble", kinds)
        self.assertIn("front_matter", kinds)
        self.assertIn("abstract", kinds)
        self.assertIn("references", kinds)
        abstract = next(chunk for chunk in result["chunks"] if chunk["kind"] == "abstract")
        self.assertIn("\\begin{abstract}", abstract["content"])
        self.assertIn("\\end{abstract}", abstract["content"])

    def test_markdown_heading_inside_code_block_ignored(self):
        module = skill_module("paper-parser")
        text = "# Real\n\n```python\n# Fake heading\nprint('ok')\n```\n\n## Real Two\n"
        result = module.parse_text(text, source_format="markdown")
        self.assertTrue(result["integrity"]["reconstructs"])
        headings = [chunk.get("heading") for chunk in result["chunks"]]
        self.assertNotIn("Fake heading", headings)


if __name__ == "__main__":
    unittest.main()
