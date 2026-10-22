import tempfile
import unittest
import zipfile
from pathlib import Path

from utils.docx_io import materialize_mteqn_fields


class EquationMaterializeTests(unittest.TestCase):
    def test_mteqn_field_is_replaced_with_cached_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "eq.docx"
            document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p>
    <w:r><w:fldChar w:fldCharType="begin"/></w:r>
    <w:r><w:instrText> MACROBUTTON MTPlaceRef \\* MERGEFORMAT </w:instrText></w:r>
    <w:r><w:instrText> SEQ MTEqn \\h </w:instrText></w:r>
    <w:r><w:instrText>(1.1)</w:instrText></w:r>
    <w:r><w:fldChar w:fldCharType="end"/></w:r>
  </w:p></w:body>
</w:document>"""
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", document)
            target = root / "target.docx"
            report = materialize_mteqn_fields(source, target)
            with zipfile.ZipFile(target) as archive:
                xml = archive.read("word/document.xml").decode("utf-8")
            self.assertEqual(report["materialized_count"], 1)
            self.assertNotIn("instrText", xml)
            self.assertIn("(1.1)", xml)


if __name__ == "__main__":
    unittest.main()
