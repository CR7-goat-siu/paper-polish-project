import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from utils.zotero_crossref import (
    audit_zotero_docx,
    extract_reference_manifest,
    rebind_zotero_fields,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"


def _citation_instruction(number: int, title: str) -> str:
    payload = {
        "citationID": f"TEST{number}",
        "properties": {
            "formattedCitation": f"[{number}]",
            "plainCitation": f"[{number}]",
            "noteIndex": 0,
        },
        "citationItems": [
            {
                "id": number,
                "uris": [f"http://zotero.org/users/1/items/KEY{number}"],
                "itemData": {
                    "id": number,
                    "type": "article-journal",
                    "title": title,
                    "author": [{"family": "Author", "given": "A."}],
                    "issued": {"date-parts": [["2024"]]},
                },
            }
        ],
        "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
    }
    return " ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


def _field_runs(instruction: str, display: str) -> str:
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve">{instruction}</w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f"<w:r><w:t>{display}</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def _write_source(path: Path) -> None:
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W_NS}"><w:body>
<w:p><w:r><w:t>Intro </w:t></w:r>{_field_runs(_citation_instruction(1, "First"), "[1]")}</w:p>
<w:p><w:r><w:t>More </w:t></w:r>{_field_runs(_citation_instruction(2, "Second"), "[2]")}</w:p>
<w:p><w:r><w:t>参考文献</w:t></w:r></w:p>
<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> ADDIN ZOTERO_BIBL {{"uncited":[],"omitted":[],"custom":[]}} CSL_BIBLIOGRAPHY </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>[1] First reference.</w:t></w:r></w:p>
<w:p><w:r><w:t>[2] Second reference.</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>
<w:sectPr/></w:body></w:document>"""
    custom = f"""<?xml version="1.0" encoding="UTF-8"?>
<Properties xmlns="{CUSTOM_NS}" xmlns:vt="{VT_NS}">
<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="2" name="ZOTERO_PREF_1"><vt:lpwstr>&lt;data&gt;&lt;style id="http://www.zotero.org/styles/ieee" locale="zh-CN" hasBibliography="1"/&gt;&lt;prefs&gt;&lt;pref name="fieldType" value="Field"/&gt;&lt;/prefs&gt;&lt;/data&gt;</vt:lpwstr></property>
<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="3" name="ZOTERO_PREF_2"><vt:lpwstr>continuation</vt:lpwstr></property>
</Properties>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
        archive.writestr("docProps/custom.xml", custom)


def _write_target(path: Path) -> None:
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W_NS}"><w:body>
<w:p><w:r><w:t>Polished intro [1]. Later [2].</w:t></w:r></w:p>
<w:p><w:r><w:t>参考文献</w:t></w:r></w:p>
<w:p><w:r><w:t>[1] First reference.</w:t></w:r></w:p>
<w:p><w:r><w:t>[2] Second reference.</w:t></w:r></w:p>
<w:sectPr/></w:body></w:document>"""
    custom = f"""<?xml version="1.0" encoding="UTF-8"?>
<Properties xmlns="{CUSTOM_NS}" xmlns:vt="{VT_NS}"/>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
        archive.writestr("docProps/custom.xml", custom)


class ZoteroCrossrefTests(unittest.TestCase):
    def test_manifest_and_rebind_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.docx"
            target = root / "target.docx"
            output = root / "output.docx"
            _write_source(source)
            _write_target(target)

            manifest = extract_reference_manifest(source)
            self.assertEqual([item["number"] for item in manifest["items"]], [1, 2])
            self.assertEqual(manifest["style"]["style_id"], "http://www.zotero.org/styles/ieee")

            report = rebind_zotero_fields(source, target, output)
            self.assertTrue(report["pass"])
            self.assertEqual(report["rewritten_citation_count"], 2)
            audit = audit_zotero_docx(output)
            self.assertTrue(audit["pass"])
            self.assertEqual(audit["citation_field_count"], 2)
            self.assertEqual(audit["bibliography_field_count"], 1)
            self.assertTrue(audit["has_document_preferences"])
            self.assertTrue(report["refresh_ready"])

    def test_missing_number_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.docx"
            target = root / "target.docx"
            _write_source(source)
            _write_target(target)
            with zipfile.ZipFile(target) as archive:
                payloads = {name: archive.read(name) for name in archive.namelist()}
            payloads["word/document.xml"] = payloads["word/document.xml"].replace(
                b"[1]. Later [2]", b"[1]. Later [3]"
            )
            with zipfile.ZipFile(target, "w") as archive:
                for name, payload in payloads.items():
                    archive.writestr(name, payload)
            with self.assertRaises(ValueError):
                rebind_zotero_fields(source, target, root / "output.docx")


if __name__ == "__main__":
    unittest.main()
