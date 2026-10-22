import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from utils.docx_io import create_minimal_docx, set_heading_outline_levels


class DocxHeadingTests(unittest.TestCase):
    def test_outline_levels_are_added_without_style_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = create_minimal_docx(
                root / "source.docx",
                [
                    ("Title", "Paper"),
                    ("", "1 Introduction"),
                    ("", "1.1 Background"),
                    ("", "1.1.1 Detail"),
                    ("", "16.471"),
                    ("", "参考文献"),
                ],
            )
            target = root / "target.docx"
            report = set_heading_outline_levels(source, target)
            self.assertEqual(report["level_1"], 2)
            self.assertEqual(report["level_2"], 1)
            self.assertEqual(report["level_3"], 1)
            with zipfile.ZipFile(target) as archive:
                xml = archive.read("word/document.xml")
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            values = [node.attrib.get(ns + "val") for node in ET.fromstring(xml).iter(ns + "outlineLvl")]
            self.assertEqual(values, ["0", "1", "2", "0"])

    def test_heading_numbering_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "numbered.docx"
            document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>3 方法</w:t></w:r></w:p>
  </w:body>
</w:document>"""
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", document)
            target = root / "target.docx"
            set_heading_outline_levels(source, target)
            with zipfile.ZipFile(target) as archive:
                xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("numPr", xml)
            self.assertIn("outlineLvl", xml)
            self.assertNotIn(">3 方法<", xml)


if __name__ == "__main__":
    unittest.main()
