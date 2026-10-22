import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.finalize_docx import finalize
from utils.docx_io import (
    create_minimal_docx,
    normalize_docx_terms,
    read_paragraphs,
    remove_trailing_empty_paragraphs,
    sanitize_docx_field_artifacts,
)


class FinalizeDocxTests(unittest.TestCase):
    def test_unique_prefix_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = create_minimal_docx(
                root / "source.docx",
                [("Title", "Paper"), ("", "[13] old reference"), ("", "Keep")],
            )
            target = root / "target.docx"
            report = finalize(
                source,
                target,
                {
                    "normalize_colors": True,
                    "replacements": [
                        {"match_prefix": "[13]", "replacement": "[13] verified reference"}
                    ],
                },
            )
            self.assertEqual(report["replacement_count"], 1)
            texts = [item["text"] for item in read_paragraphs(target)]
            self.assertIn("[13] verified reference", texts)

    def test_ambiguous_prefix_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = create_minimal_docx(
                root / "source.docx",
                [("Title", "Paper"), ("", "[13] first"), ("", "[13] second")],
            )
            with self.assertRaises(ValueError):
                finalize(
                    source,
                    root / "target.docx",
                    {"replacements": [{"match_prefix": "[13]", "replacement": "x"}]},
                )

    def test_trailing_empty_paragraphs_are_removed_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = create_minimal_docx(
                root / "source.docx",
                [("Title", "Paper"), ("", "Final paragraph"), ("", ""), ("", "")],
            )
            target = root / "target.docx"
            report = remove_trailing_empty_paragraphs(source, target)
            self.assertEqual(report["removed_count"], 2)
            self.assertEqual(
                [item["text"] for item in read_paragraphs(target)],
                ["Paper", "Final paragraph"],
            )

    def test_zotero_bibliography_field_artifacts_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "field.docx"
            target = root / "target.docx"
            document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText> ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>[1] Archive reference.</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText> ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"x"} </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>[2] In-text citation.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", document)
            report = sanitize_docx_field_artifacts(source, target)
            self.assertEqual(report["removed_instruction_count"], 2)
            self.assertEqual(report["removed_control_run_count"], 4)
            with zipfile.ZipFile(target) as archive:
                xml = archive.read("word/document.xml").decode("utf-8")
            self.assertNotIn("ZOTERO_BIBL", xml)
            self.assertNotIn("CSL_BIBLIOGRAPHY", xml)
            self.assertNotIn("ZOTERO_ITEM", xml)
            self.assertNotIn("CSL_CITATION", xml)
            self.assertNotIn('w:fldCharType="begin"', xml)
            self.assertNotIn('w:fldCharType="separate"', xml)
            self.assertIn("[1] Archive reference.", xml)
            self.assertIn("[2] In-text citation.", xml)

    def test_docx_terms_are_normalized_without_rebuilding_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = create_minimal_docx(
                root / "source.docx",
                [
                    ("Title", "EEGDenoiseNet 伪影去除"),
                    ("", "混合伪影与伪影判别器"),
                ],
            )
            target = root / "target.docx"
            report = normalize_docx_terms(
                source,
                target,
                {"伪影": "伪迹", "EEGDenoiseNet": "EEGdenoiseNet"},
            )
            texts = [item["text"] for item in read_paragraphs(target)]
            self.assertEqual(report["replacement_count"], 4)
            self.assertIn("EEGdenoiseNet 伪迹去除", texts)
            self.assertIn("混合伪迹与伪迹判别器", texts)


if __name__ == "__main__":
    unittest.main()
