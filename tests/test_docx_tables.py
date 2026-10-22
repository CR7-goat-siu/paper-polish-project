import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from utils.docx_io import (
    read_tables,
    repeat_table_headers_and_prevent_row_split,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag):
    return f"{{{W_NS}}}{tag}"


class DocxTableTests(unittest.TestCase):
    def _make_table_docx(self, path):
        document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>Method</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>SNR</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:trPr><w:keepNext/></w:trPr><w:tc><w:p><w:r><w:t>Ours</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>12.345</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
  </w:body>
</w:document>"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", document)

    def test_read_tables_preserves_rows_and_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.docx"
            self._make_table_docx(path)
            tables = read_tables(path)
            self.assertEqual(tables[0]["rows"], [["Method", "SNR"], ["Ours", "12.345"]])
            self.assertEqual(len(tables[0]["paragraph_indexes"]), 4)

    def test_repeat_header_and_prevent_row_split_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "table.docx"
            first = root / "first.docx"
            second = root / "second.docx"
            self._make_table_docx(source)

            report = repeat_table_headers_and_prevent_row_split(source, first)
            repeat_table_headers_and_prevent_row_split(first, second)

            self.assertEqual(report["table_count"], 1)
            self.assertEqual(report["header_count"], 1)
            self.assertEqual(report["row_count"], 2)
            with zipfile.ZipFile(second) as archive:
                doc = ET.fromstring(archive.read("word/document.xml"))
            rows = doc.findall(f".//{_w('tr')}")
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(rows[0].findall(f"{_w('trPr')}/{_w('tblHeader')}")), 1)
            self.assertEqual(len(rows[1].findall(f"{_w('trPr')}/{_w('tblHeader')}")), 0)
            for row in rows:
                self.assertEqual(
                    len(row.findall(f"{_w('trPr')}/{_w('cantSplit')}")), 1
                )
            self.assertEqual(rows[1][0].tag, _w("trPr"))
            self.assertIsNotNone(rows[1][0].find(_w("keepNext")))


if __name__ == "__main__":
    unittest.main()
