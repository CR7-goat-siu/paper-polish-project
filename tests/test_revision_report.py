import tempfile
import unittest
from pathlib import Path

from tools.build_revision_report import build_revision_report
from utils.docx_io import create_minimal_docx


class RevisionReportTests(unittest.TestCase):
    def test_revision_report_lists_changed_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = create_minimal_docx(
                root / "before.docx",
                [("Title", "Paper"), ("", "Old sentence."), ("", "Unchanged.")],
            )
            after = create_minimal_docx(
                root / "after.docx",
                [("Title", "Paper"), ("", "New academic sentence."), ("", "Unchanged.")],
            )
            report = build_revision_report(
                before,
                after,
                root / "diff.md",
                root / "diff.json",
                title="Test diff",
            )
            self.assertEqual(report["changed_paragraph_count"], 1)
            markdown = (root / "diff.md").read_text(encoding="utf-8")
            self.assertIn("Old sentence.", markdown)
            self.assertIn("New academic sentence.", markdown)

    def test_revision_report_supports_inserted_and_deleted_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = create_minimal_docx(
                root / "before.docx",
                [("Title", "Paper"), ("", "Keep."), ("", "Remove me.")],
            )
            after = create_minimal_docx(
                root / "after.docx",
                [("Title", "Paper"), ("", "Keep."), ("", "Inserted."), ("", "Tail.")],
            )
            report = build_revision_report(
                before,
                after,
                root / "diff.md",
                root / "diff.json",
                title="Structural diff",
            )
            self.assertEqual(report["source_paragraph_count"], 3)
            self.assertEqual(report["target_paragraph_count"], 4)
            self.assertTrue(report["changes"])
            types = {item["change_type"] for item in report["changes"]}
            self.assertIn("insert", types)
            self.assertIn("replace", types)
            markdown = (root / "diff.md").read_text(encoding="utf-8")
            self.assertIn("Remove me.", markdown)
            self.assertIn("Inserted.", markdown)

            deleted_target = create_minimal_docx(
                root / "deleted.docx",
                [("Title", "Paper"), ("", "Keep.")],
            )
            deleted_report = build_revision_report(
                before,
                deleted_target,
                root / "deleted-diff.md",
                root / "deleted-diff.json",
                title="Deletion diff",
            )
            self.assertIn(
                "delete", {item["change_type"] for item in deleted_report["changes"]}
            )


if __name__ == "__main__":
    unittest.main()
