import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from utils.docx_io import create_minimal_docx, normalize_text_colors


class DocxPostprocessTests(unittest.TestCase):
    def test_normalize_text_colors_removes_red_and_highlight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = create_minimal_docx(root / "source.docx", [("Title", "Paper"), ("", "Body")])
            source2 = root / "source2.docx"
            with zipfile.ZipFile(source) as archive, zipfile.ZipFile(source2, "w") as target:
                for item in archive.infolist():
                    data = archive.read(item.filename)
                    if item.filename == "word/document.xml":
                        text = data.decode("utf-8")
                        text = text.replace(
                            '<w:r><w:t xml:space="preserve">Body</w:t></w:r>',
                            '<w:r><w:rPr><w:color w:val="EE0000"/>'
                            '<w:highlight w:val="yellow"/></w:rPr>'
                            '<w:t xml:space="preserve">Body</w:t></w:r>',
                        )
                        data = text.encode("utf-8")
                    target.writestr(item, data)
            target = root / "clean.docx"
            normalize_text_colors(source2, target)
            with zipfile.ZipFile(target) as archive:
                document = archive.read("word/document.xml").decode("utf-8")
            self.assertNotIn("EE0000", document)
            self.assertNotIn("<w:highlight", document)
            self.assertNotIn(":highlight", document)
            root = ET.fromstring(document)
            colors = [
                node.attrib.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")
                for node in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}color")
            ]
            self.assertIn("000000", colors)

    def test_normalize_text_colors_preserves_word_namespace_prefixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "namespaces.docx"
            namespace_doc = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
                'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
                'mc:Ignorable="w14"><w:body><w:p><w:r><w:t>Body</w:t></w:r></w:p></w:body></w:document>'
            )
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", namespace_doc)
            target = root / "clean.docx"
            normalize_text_colors(source, target)
            with zipfile.ZipFile(target) as archive:
                text = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("<w:document", text)
            self.assertIn('xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"', text)
            self.assertIn('mc:Ignorable="w14"', text)


if __name__ == "__main__":
    unittest.main()
