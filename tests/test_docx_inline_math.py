import tempfile
import unittest
import zipfile
from pathlib import Path

from utils.docx_io import preserve_inline_math_paragraphs


class InlineMathPreserveTests(unittest.TestCase):
    def _docx(self, path: Path, body: str):
        document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
<w:body>{body}</w:body></w:document>"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", document)

    def test_inline_math_paragraph_restores_original_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.docx"
            candidate = root / "candidate.docx"
            self._docx(
                original,
                '<w:p><w:r><w:t>Noise discriminator </w:t></w:r>'
                '<m:oMath><m:r><m:t>D_noise</m:t></m:r></m:oMath></w:p>'
                '<w:p><w:r><w:t>Plain paragraph.</w:t></w:r></w:p>',
            )
            self._docx(
                candidate,
                '<w:p><w:r><w:t>Noise discriminator ().</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>Polished plain paragraph.</w:t></w:r></w:p>',
            )
            target = root / "target.docx"
            report = preserve_inline_math_paragraphs(original, candidate, target)
            self.assertEqual(report["preserved_count"], 1)
            with zipfile.ZipFile(target) as archive:
                xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("D_noise", xml)
            self.assertIn("Polished plain paragraph.", xml)


if __name__ == "__main__":
    unittest.main()
