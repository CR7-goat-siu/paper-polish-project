import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from utils.docx_io import (
    _check_readback,
    add_comments,
    create_minimal_docx,
    parse_paragraph_kinds,
    read_paragraphs,
    read_comments,
    write_polished_docx,
)

_MINIMAL_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_MINIMAL_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


class DocxIoTests(unittest.TestCase):
    def _make_docx(self, folder: Path) -> Path:
        return create_minimal_docx(
            folder / "sample.docx",
            [
                ("Title", "A Study on EEG Signals"),
                ("Heading 1", "Abstract"),
                ("", "This paper studies EEG signals with accuracy of 91.2%."),
                ("Heading 1", "1 Introduction"),
                ("", "We compare with a convolution neural network baseline [1]."),
                ("Heading 1", "References"),
                ("", "[1] J. Smith. A survey. Journal, 2024, pp. 10-20."),
            ],
        )

    def test_read_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_docx(Path(tmp))
            paragraphs = read_paragraphs(path)
            self.assertEqual(len(paragraphs), 7)
            self.assertEqual(paragraphs[0]["style"], "Title")
            self.assertIn("EEG", paragraphs[0]["text"])
            self.assertEqual(paragraphs[4]["style"], "")

    def test_parse_paragraph_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_docx(Path(tmp))
            paragraphs = read_paragraphs(path)
            kinds = [kind for kind, _ in parse_paragraph_kinds(paragraphs)]
            self.assertEqual(kinds[0], "title")
            self.assertEqual(kinds[1], "abstract")
            self.assertEqual(kinds[2], "abstract")
            self.assertEqual(kinds[3], "section")
            self.assertEqual(kinds[5], "references")
            self.assertEqual(kinds[6], "references")

    def test_write_polished_docx_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_docx(Path(tmp))
            target = Path(tmp) / "polished.docx"
            ok = write_polished_docx(
                path,
                target,
                {2: "This paper studies EEG signals; accuracy reaches 91.2%."},
            )
            self.assertTrue(ok)
            paragraphs = read_paragraphs(target)
            self.assertIn("accuracy reaches", paragraphs[2]["text"])
            # 未改动的段落保持原样
            self.assertEqual(paragraphs[4]["text"], paragraphs[4]["text"])
            self.assertEqual(paragraphs[1]["text"], "Abstract")
            self.assertEqual(paragraphs[1]["style"], "Heading 1")

    def test_write_preserves_namespace_prefixes(self):
        # ET 默认把前缀改写为 ns0:，使 mc:Ignorable="w14" 的值引用悬空，
        # Word 会报“发现无法读取的内容”（WPS 宽容、Word 严格）。
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "real.docx"
            doc_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
                ' xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
                ' xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
                ' mc:Ignorable="w14">'
                "<w:body><w:p><w:r><w:t>keep me [1]</w:t></w:r></w:p></w:body></w:document>"
            )
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("[Content_Types].xml", _MINIMAL_CONTENT_TYPES)
                archive.writestr("_rels/.rels", _MINIMAL_ROOT_RELS)
                archive.writestr("word/document.xml", doc_xml)
            target = Path(tmp) / "out.docx"
            write_polished_docx(source, target, {0: "changed [1]"})
            with zipfile.ZipFile(target) as archive:
                out = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("<w:document", out)
            self.assertNotIn("ns0:", out)
            self.assertIn('mc:Ignorable="w14"', out)
            self.assertIn('xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"', out)

    def test_write_verify_exact_text_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_docx(Path(tmp))
            target = Path(tmp) / "polished.docx"
            expected = "This paper studies EEG signals; accuracy reaches 91.2%."
            write_polished_docx(path, target, {2: expected})
            # 内建回读校验通过的文件，从磁盘读回应与期望逐字符一致
            self.assertEqual(read_paragraphs(target)[2]["text"], expected)


class MathDocxTests(unittest.TestCase):
    """外科式 token 替换：含公式段落只改 w:t 文本，结构/公式原样保留。"""

    M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

    def _make_math_docx(self, folder: Path) -> Path:
        path = folder / "math.docx"
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            f'xmlns:m="{self.M_NS}"><w:body>'
            '<w:p><w:r><w:t xml:space="preserve">Intro cites [20] here.</w:t></w:r></w:p>'
            "<w:p>"
            '<w:r><w:t xml:space="preserve">The loss combines terms, </w:t></w:r>'
            "<m:oMath><m:r><m:t>L=Lrec+aLsup</m:t></m:r></m:oMath>"
            '<w:r><w:t xml:space="preserve"> following </w:t></w:r>'
            '<w:r><w:t xml:space="preserve">[2</w:t></w:r>'
            '<w:r><w:t xml:space="preserve">3]</w:t></w:r>'
            '<w:r><w:t xml:space="preserve"> with adaptations [24].</w:t></w:r>'
            "</w:p>"
            '<w:p><w:r><w:t xml:space="preserve">[23] Huang, W. (1998). A study. Journal, pp. 1-10.</w:t></w:r></w:p>'
            "<w:sectPr/></w:body></w:document>"
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES_STUB)
            archive.writestr("_rels/.rels", _ROOT_RELS_STUB)
            archive.writestr("word/document.xml", document)
        return path

    def _paragraphs_xml(self, path: Path) -> list[ET.Element]:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        return list(root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"))

    def test_surgical_replacement_preserves_math(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_math_docx(Path(tmp))
            target = Path(tmp) / "out.docx"
            write_polished_docx(
                path,
                target,
                {1: {"token_replacements": [("[23]", "[20]"), ("[24]", "[21]")]}},
            )
            paragraphs = read_paragraphs(target)
            # "[23]" 跨两个 run（"[2" + "3]"）也能整体替换
            self.assertEqual(
                paragraphs[1]["text"], "The loss combines terms,  following [20] with adaptations [21]."
            )
            # 其他段落不受影响
            self.assertEqual(paragraphs[0]["text"], "Intro cites [20] here.")
            self.assertIn("[23] Huang", paragraphs[2]["text"])
            # 公式原样保留：oMath 仍在，m:t 未被触碰
            xml_paragraphs = self._paragraphs_xml(target)
            math_nodes = [
                node for node in xml_paragraphs[1].iter() if node.tag == f"{{{self.M_NS}}}oMath"
            ]
            self.assertEqual(len(math_nodes), 1)
            math_text = "".join(
                node.text or ""
                for node in math_nodes[0].iter()
                if node.tag == f"{{{self.M_NS}}}t"
            )
            self.assertEqual(math_text, "L=Lrec+aLsup")

    def test_surgical_replacement_all_occurrences(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_math_docx(Path(tmp))
            target = Path(tmp) / "out.docx"
            # "[20]" 在段 0 出现一次；替换值含旧 token 也不应死循环
            write_polished_docx(
                path, target, {0: {"token_replacements": [("[20]", "[20,21]")]}}
            )
            paragraphs = read_paragraphs(target)
            self.assertEqual(paragraphs[0]["text"], "Intro cites [20,21] here.")

    def test_surgical_missing_token_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_math_docx(Path(tmp))
            with self.assertRaises(ValueError) as ctx:
                write_polished_docx(
                    path,
                    Path(tmp) / "out.docx",
                    {1: {"token_replacements": [("[99]", "[1]")]}},
                )
            self.assertIn("未找到待替换 token", str(ctx.exception))

    def test_empty_token_pairs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_math_docx(Path(tmp))
            with self.assertRaises(ValueError):
                write_polished_docx(path, Path(tmp) / "out.docx", {1: {"token_replacements": []}})

    def test_replacement_plus_insertion_passes_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_math_docx(Path(tmp))
            target = Path(tmp) / "out.docx"
            write_polished_docx(
                path,
                target,
                replacements={0: "Intro cites [19] here."},
                insertions={0: ["Inserted right after intro."]},
            )
            paragraphs = read_paragraphs(target)
            self.assertEqual(paragraphs[0]["text"], "Intro cites [19] here.")
            self.assertEqual(paragraphs[1]["text"], "Inserted right after intro.")
            # 插入后原段 1（公式段）顺移为段 2，内容不变
            self.assertIn("The loss combines terms", paragraphs[2]["text"])

    def test_check_readback_detects_garble(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_math_docx(Path(tmp))
            paragraphs = self._paragraphs_xml(path)
            # 期望与实际不一致（模拟乱串/重复拼接）必须在库层面报错
            with self.assertRaises(ValueError) as ctx:
                _check_readback(paragraphs, {0: "Intro cites [21] here."}, "test")
            self.assertIn("回读校验", str(ctx.exception))
            # 一致时静默通过
            _check_readback(paragraphs, {0: "Intro cites [20] here."}, "test")


_CONTENT_TYPES_STUB = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_ROOT_RELS_STUB = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


if __name__ == "__main__":
    unittest.main()


class DocxCommentTests(unittest.TestCase):
    """add_comments：OOXML 批注写入 + 回读校验（ECMA-376 最小四要素）。"""

    def _docx(self, tmp):
        return create_minimal_docx(
            Path(tmp) / "paper.docx",
            [
                ("Title", "EEG Paper"),
                ("Heading 1", "1 Introduction"),
                ("", "The model reaches an accuracy of 91.2% [1]."),
                ("", "As shown in Fig. 3, the encoder stacks three blocks."),
            ],
        )

    def test_comments_roundtrip_with_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            docx = self._docx(tmp)
            self.assertTrue(
                add_comments(docx, {2: ["门禁未通过已回退原文：新增数值 95.6%", "引用 [25] 不在文献表"]})
            )
            comments = read_comments(docx)
            self.assertEqual(len(comments), 2)
            self.assertEqual({c["paragraph_indexes"][0] for c in comments}, {2})
            self.assertIn("95.6%", comments[0]["text"])
            self.assertEqual(comments[0]["author"], "paper-polish guardrails")
            # 段落文本与数量不受影响
            paragraphs = read_paragraphs(docx)
            self.assertEqual(len(paragraphs), 4)
            self.assertIn("91.2%", paragraphs[2]["text"])

    def test_comments_xml_structure_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            docx = self._docx(tmp)
            add_comments(docx, {2: ["结构化校验"]})
            with zipfile.ZipFile(docx) as archive:
                names = archive.namelist()
                self.assertIn("word/comments.xml", names)
                self.assertIn("word/_rels/document.xml.rels", names)
                for name in names:
                    if name.endswith((".xml", ".rels")):
                        ET.fromstring(archive.read(name))
                rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
                content_types = archive.read("[Content_Types].xml").decode("utf-8")
                document = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("relationships/comments", rels)
            self.assertIn('Target="comments.xml"', rels)
            self.assertIn("/word/comments.xml", content_types)
            self.assertEqual(document.count("commentRangeStart"), 1)
            self.assertEqual(document.count("commentRangeEnd"), 1)
            self.assertEqual(document.count("commentReference"), 1)

    def test_comments_append_to_existing_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            docx = self._docx(tmp)
            add_comments(docx, {2: ["第一条"]})
            add_comments(docx, {3: ["第二条"]})
            comments = read_comments(docx)
            self.assertEqual(len(comments), 2)
            ids = sorted(int(c["id"]) for c in comments)
            self.assertEqual(len(set(ids)), 2, "批注 id 必须唯一")

    def test_empty_comments_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            docx = self._docx(tmp)
            self.assertFalse(add_comments(docx, {}))
            self.assertEqual(read_comments(docx), [])

    def test_replace_retries_permission_error(self):
        from unittest import mock
        from utils.docx_io import _replace_with_retry

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            target = Path(tmp) / "target"
            source.write_text("x", encoding="utf-8")
            target.write_text("y", encoding="utf-8")
            with mock.patch(
                "utils.docx_io.os.replace",
                side_effect=[PermissionError("busy"), PermissionError("busy"), None],
            ) as replace:
                _replace_with_retry(source, target, attempts=3)
            self.assertEqual(replace.call_count, 3)

    def test_add_comments_does_not_duplicate_zip_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            docx = self._docx(tmp)
            add_comments(docx, {2: ["check"]})
            with zipfile.ZipFile(docx) as archive:
                names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
