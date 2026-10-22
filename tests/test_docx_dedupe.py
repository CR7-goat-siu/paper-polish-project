import tempfile
import unittest
import zipfile
from pathlib import Path

from utils.docx_io import dedupe_docx_members


class DocxDedupeTests(unittest.TestCase):
    def test_duplicate_rels_and_stale_comments_relation_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "duplicate.docx"
            document = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>'
            rels = (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>'
                "</Relationships>"
            )
            content_types = (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>'
                "</Types>"
            )
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("[Content_Types].xml", content_types)
                archive.writestr("word/document.xml", document)
                archive.writestr("word/_rels/document.xml.rels", rels)
                archive.writestr("word/_rels/document.xml.rels", rels)
            target = root / "clean.docx"
            report = dedupe_docx_members(source, target)
            self.assertEqual(report["removed_duplicate_entries"], 1)
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
                rels_out = archive.read("word/_rels/document.xml.rels").decode("utf-8")
                ct_out = archive.read("[Content_Types].xml").decode("utf-8")
            self.assertEqual(len(names), len(set(names)))
            self.assertNotIn("comments.xml", rels_out)
            self.assertNotIn("/word/comments.xml", ct_out)


if __name__ == "__main__":
    unittest.main()
