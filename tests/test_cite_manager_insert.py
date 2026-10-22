import unittest

from conftest import skill_module

TEXT = (
    "1 引言\n"
    "近年来，基于深度学习的脑电去噪方法研究进展迅速。本文提出一种新方法，并在EEGDenoiseNet上验证。\n"
    "参考文献\n"
    "[1] A. Author, \"Existing work,\" Journal, 2020.\n"
)
ENTRY = {
    "title": "An Approach for EEG Denoising Based on Wasserstein GAN",
    "authors": ["Y. Dong", "X. Li"],
    "year": 2023,
    "venue": "IEEE Transactions on Neural Systems and Rehabilitation Engineering",
    "volume": "31",
    "pages": "3524-3535",
    "doi": "10.1109/TNSRE.2023.3309815",
    "doi_display": "10.1109/TNSRE.2023.3309815",
    "anchor": "本文提出一种新方法",
    "verdict": "verified",
}


class CiteManagerInsertTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("cite-manager")

    def test_insert_verified_entry(self):
        result = self.module.run(
            {
                "mode": "insert",
                "text": TEXT,
                "language": "cn",
                "entries": [ENTRY],
                "style": "ieee",
                "paragraphs": TEXT.split("\n"),
            }
        )
        self.assertEqual(result["mode"], "insert")
        self.assertIn("本文提出一种新方法[2]", result["text"])
        self.assertIn("[2] Y. Dong and X. Li", result["text"])
        self.assertEqual(result["start_number"], 2)
        self.assertEqual(result["entry_lines"], [line for line in result["entry_lines"]])
        self.assertIn(1, result["docx_plan"])
        self.assertEqual(result["docx_plan"][1]["token_replacements"], [("本文提出一种新方法", "本文提出一种新方法[2]")])
        self.assertIn(3, result["docx_insertions"])

    def test_start_number_override_and_apa_style(self):
        result = self.module.run(
            {
                "mode": "insert",
                "text": TEXT,
                "language": "cn",
                "entries": [ENTRY],
                "style": "apa",
                "start_number": 10,
            }
        )
        self.assertIn("本文提出一种新方法[10]", result["text"])
        self.assertIn("(2023).", result["entry_lines"][0])
        self.assertEqual(result["style"], "apa")

    def test_entry_formats_cover_all_styles(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            formats_out = Path(tmp) / "citations.md"
            result = self.module.run(
                {
                    "mode": "insert",
                    "text": TEXT,
                    "language": "cn",
                    "entries": [ENTRY],
                    "style": "ieee",
                    "formats_out": str(formats_out),
                }
            )
            self.assertEqual(len(result["entry_formats"]), 1)
            formats = result["entry_formats"][0]["formats"]
            self.assertEqual(set(formats), {"ieee", "apa", "gbt7714", "bibtex"})
            self.assertIn("@article{", formats["bibtex"])
            self.assertIn("(2023).", formats["apa"])
            self.assertTrue(formats_out.exists())
            content = formats_out.read_text(encoding="utf-8")
            self.assertIn("gbt7714:", content)
            self.assertIn("ieee:", content)

    def test_unverified_entry_is_refused(self):
        with self.assertRaises(ValueError):
            self.module.run(
                {"mode": "insert", "text": TEXT, "language": "cn", "entries": [dict(ENTRY, verdict="unverified")]}
            )

    def test_unverified_override_is_not_supported(self):
        with self.assertRaises(ValueError):
            self.module.run(
                {
                    "mode": "insert",
                    "text": TEXT,
                    "language": "cn",
                    "entries": [dict(ENTRY, verdict="unverified")],
                    "accept_unverified": True,
                }
            )

    def test_missing_verdict_is_refused(self):
        entry = dict(ENTRY)
        entry.pop("verdict")
        with self.assertRaises(ValueError):
            self.module.run(
                {"mode": "insert", "text": TEXT, "language": "cn", "entries": [entry]}
            )

    def test_ambiguous_anchor_is_refused(self):
        text = TEXT.replace("本文提出一种新方法", "本文提出一种新方法（下称本文提出一种新方法）", 1)
        with self.assertRaises(ValueError):
            self.module.run({"mode": "insert", "text": text, "language": "cn", "entries": [ENTRY]})

    def test_missing_anchor_is_refused(self):
        with self.assertRaises(ValueError):
            self.module.run({"mode": "insert", "text": TEXT, "language": "cn", "entries": [dict(ENTRY, anchor="")]})

    def test_entries_file_from_lit_verification(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        payload = {"verified_entries": [ENTRY]}
        path = tmp / "lit-verification.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = self.module.run(
            {"mode": "insert", "text": TEXT, "language": "cn", "entries_file": str(path)}
        )
        self.assertIn("[2]", result["text"])


if __name__ == "__main__":
    unittest.main()
