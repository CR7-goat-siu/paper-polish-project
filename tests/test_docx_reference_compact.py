import tempfile
import unittest
import zipfile
from pathlib import Path

from utils.docx_io import compact_reference_paragraphs


class ReferenceCompactTests(unittest.TestCase):
    def test_reference_spacing_is_compacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.docx"
            document = (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>[1] Reference.</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>Body paragraph.</w:t></w:r></w:p></w:body></w:document>'
            )
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", document)
            target = root / "target.docx"
            report = compact_reference_paragraphs(source, target)
            self.assertEqual(report["compacted_count"], 1)
            with zipfile.ZipFile(target) as archive:
                xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn('w:after="0"', xml)
            self.assertIn('w:line="240"', xml)


if __name__ == "__main__":
    unittest.main()
