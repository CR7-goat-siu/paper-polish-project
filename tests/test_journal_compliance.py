"""期刊合规网关（journal-compliance）专项测试。

覆盖：图像文件头解析（PNG pHYs / JPEG JFIF / TIFF 标签，合成字节）、
字数上下限、关键词区间、必需章节与顺序、声明区缺失、参考文献风格，
以及 run_audit 的 journal 参数集成（硬门禁 + 报告落盘）。
"""

from __future__ import annotations

import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils.journal_compliance import (
    check_journal_compliance,
    image_header_info,
    load_journal_profile,
)


def _png_bytes(width: int, height: int, dpi: float | None) -> bytes:
    """合成最小 PNG：签名 + IHDR（+ 可选 pHYs）+ IEND。"""
    payload = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    payload += struct.pack(">I", 13) + b"IHDR" + ihdr
    import zlib

    payload += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    if dpi is not None:
        ppm = int(round(dpi / 0.0254))
        phys = struct.pack(">IIB", ppm, ppm, 1)
        payload += struct.pack(">I", 9) + b"pHYs" + phys
        payload += struct.pack(">I", zlib.crc32(b"pHYs" + phys) & 0xFFFFFFFF)
    payload += struct.pack(">I", 0) + b"IEND"
    payload += struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    return payload


def _jpeg_bytes(width: int, height: int, dpi: float | None) -> bytes:
    """合成最小 JPEG：SOI + APP0(JFIF 含密度) + SOF0 + EOI。"""
    payload = b"\xff\xd8"
    jfif = b"JFIF\x00" + struct.pack(">BHH", 1 if dpi else 0, int(dpi or 1), int(dpi or 1))
    payload += b"\xff\xe0" + struct.pack(">H", len(jfif) + 2) + jfif
    sof = b"\x08" + struct.pack(">HH", height, width) + b"\x01\x11\x00"
    payload += b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    payload += b"\xff\xd9"
    return payload


def _tiff_bytes(width: int, height: int, dpi: float | None) -> bytes:
    """合成最小 TIFF：II 头 + 单 IFD（宽度/高度/分辨率/单位标签）。"""
    entries: list[tuple[int, int, int, int]] = []  # (tag, type, count, value)
    entries.append((256, 4, 1, width))  # ImageWidth LONG
    entries.append((257, 4, 1, height))  # ImageLength LONG
    rational_offset = 8 + 2 + 5 * 12 + 4  # 头 + count + entries + next IFD
    if dpi is not None:
        entries.append((282, 5, 1, rational_offset))  # XResolution
        entries.append((283, 5, 1, rational_offset + 8))  # YResolution
        entries.append((296, 3, 1, 2))  # unit = inch
    else:
        entries.append((296, 3, 1, 2))
    payload = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
    ifd = struct.pack("<H", len(entries))
    for tag, ftype, count, value in entries:
        ifd += struct.pack("<HHI", tag, ftype, count)
        if ftype == 4:
            ifd += struct.pack("<I", value)
        else:
            ifd += struct.pack("<HH", value, 0)
    ifd += struct.pack("<I", 0)  # next IFD = 0
    payload += ifd
    if dpi is not None:
        payload += struct.pack("<II", int(dpi * 100), 100)  # X res
        payload += struct.pack("<II", int(dpi * 100), 100)  # Y res
    return payload


class ImageHeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, name: str, data: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def test_png_with_phys_dpi(self):
        info = image_header_info(_png_bytes(1200, 800, 350.0))
        self.assertEqual(info["format"], "png")
        self.assertEqual(info["width"], 1200)
        self.assertEqual(info["height"], 800)
        self.assertAlmostEqual(info["dpi_x"], 350.0, delta=0.5)
        self.assertEqual(info["dpi_source"], "pHYs")

    def test_png_without_dpi(self):
        info = image_header_info(_png_bytes(600, 400, None))
        self.assertIsNone(info["dpi_x"])
        self.assertEqual(info["dpi_source"], "none")

    def test_jpeg_jfif_dpi(self):
        info = image_header_info(_jpeg_bytes(800, 600, 96))
        self.assertEqual(info["format"], "jpeg")
        self.assertEqual(info["width"], 800)
        self.assertEqual(info["height"], 600)
        self.assertEqual(info["dpi_x"], 96)

    def test_tiff_resolution_tags(self):
        info = image_header_info(_tiff_bytes(1000, 700, 300.0))
        self.assertEqual(info["format"], "tiff")
        self.assertEqual(info["width"], 1000)
        self.assertEqual(info["height"], 700)
        self.assertAlmostEqual(info["dpi_x"], 300.0, delta=0.2)

    def test_unknown_bytes_do_not_crash(self):
        info = image_header_info(b"not an image at all")
        self.assertEqual(info["format"], "unknown")
        self.assertIsNone(info["width"])


_COMPLIANT_TEXT = """# IEEE-style Paper

## Abstract
This study proposes a method and validates it on public data with significant gains reported across benchmarks.

## Keywords
deep learning; signal processing; denoising; evaluation

## 1 Introduction
Prior work has studied this problem extensively and the gap remains [1].

## 2 Method
We describe the architecture and its components in detail here.

## 3 Experiments
We evaluate on three datasets with statistical tests and ablations.

## 4 Conclusion
The method is effective within the tested scope.

## Conflict of Interest
The authors declare no conflict of interest.

## Ethics Approval
This study was approved by the Institutional Review Board (IRB approval).

## Data Availability
Data availability: all datasets are publicly available.

## Author Contributions
Author contributions: conceptualization by A.B.; CRediT taxonomy applied.

## Acknowledgments
Funding: this work was supported by grant No. 12345.

## References
[1] Author A. Title of the first reference. 2020.
[2] Author B. Title of the second reference. 2021.
"""


class ComplianceChecksTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_journal_profile("ieee-trans")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_compliant_text_passes(self):
        result = check_journal_compliance(_COMPLIANT_TEXT, self.profile)
        self.assertTrue(result["pass"], json.dumps(result["items"], ensure_ascii=False))
        self.assertEqual(result["fail_count"], 0)

    def test_abstract_over_limit_fails(self):
        long_abstract = _COMPLIANT_TEXT.replace(
            "This study proposes a method and validates it on public data with significant gains reported across benchmarks.",
            " ".join(["word"] * 300),
        )
        result = check_journal_compliance(long_abstract, self.profile)
        item = next(item for item in result["items"] if item["check"] == "word_limit_abstract")
        self.assertEqual(item["status"], "FAIL")
        self.assertFalse(result["pass"])

    def test_keywords_out_of_range_fail(self):
        result = check_journal_compliance(
            _COMPLIANT_TEXT.replace(
                "deep learning; signal processing; denoising; evaluation",
                "deep learning",
            ),
            self.profile,
        )
        item = next(item for item in result["items"] if item["check"] == "keywords_range")
        self.assertEqual(item["status"], "FAIL")
        self.assertEqual(item["actual"], 1)

    def test_missing_required_section_fails(self):
        result = check_journal_compliance(
            _COMPLIANT_TEXT.replace("## 3 Experiments\nWe evaluate on three datasets with statistical tests and ablations.\n\n", ""),
            self.profile,
        )
        checks = {item["check"]: item["status"] for item in result["items"]}
        self.assertEqual(checks.get("sections_missing::experiment"), "FAIL")
        self.assertFalse(result["pass"])

    def test_missing_declaration_fails_with_registry_finding(self):
        result = check_journal_compliance(
            _COMPLIANT_TEXT.replace("## Data Availability\nData availability: all datasets are publicly available.\n\n", ""),
            self.profile,
        )
        item = next(item for item in result["items"] if item["check"] == "declaration_missing::data_availability")
        self.assertEqual(item["status"], "FAIL")
        finding = next(f for f in result["findings"] if f["check"] == "declaration_missing::data_availability")
        self.assertEqual(finding["severity"], "error")

    def test_low_dpi_image_fails(self):
        fig = self.tmp / "fig1.png"
        fig.write_bytes(_png_bytes(600, 400, 150.0))
        result = check_journal_compliance(_COMPLIANT_TEXT, self.profile, figures_dir=str(self.tmp))
        item = next(item for item in result["items"] if item["check"] == "figure_dpi")
        self.assertEqual(item["status"], "FAIL")
        self.assertIn("150", item["detail"])

    def test_high_dpi_image_passes(self):
        fig = self.tmp / "fig1.png"
        fig.write_bytes(_png_bytes(2000, 1200, 350.0))
        result = check_journal_compliance(_COMPLIANT_TEXT, self.profile, figures_dir=str(self.tmp))
        item = next(item for item in result["items"] if item["check"] == "figure_dpi")
        self.assertEqual(item["status"], "PASS")

    def test_image_without_dpi_info_warns(self):
        fig = self.tmp / "fig1.png"
        fig.write_bytes(_png_bytes(2000, 1200, None))
        result = check_journal_compliance(_COMPLIANT_TEXT, self.profile, figures_dir=str(self.tmp))
        item = next(item for item in result["items"] if item["check"] == "figure_dpi")
        self.assertEqual(item["status"], "WARN")

    def test_disallowed_extension_fails(self):
        fig = self.tmp / "fig1.bmp"
        fig.write_bytes(b"BM\x00\x00 fake bitmap")
        result = check_journal_compliance(_COMPLIANT_TEXT, self.profile, figures_dir=str(self.tmp))
        item = next(item for item in result["items"] if item["check"] == "figure_format")
        self.assertEqual(item["status"], "FAIL")

    def test_section_order_enforced_for_elsevier_profile(self):
        profile = load_journal_profile("elsevier-bspc")
        reordered = _COMPLIANT_TEXT.replace(
            "## 1 Introduction\nPrior work has studied this problem extensively and the gap remains [1].\n\n## 2 Method\nWe describe the architecture and its components in detail here.\n\n## 3 Experiments\nWe evaluate on three datasets with statistical tests and ablations.",
            "## 1 Experiments\nWe evaluate on three datasets with statistical tests and ablations.\n\n## 2 Method\nWe describe the architecture and its components in detail here.\n\n## 3 Introduction\nPrior work has studied this problem extensively and the gap remains [1].",
        )
        result = check_journal_compliance(reordered, profile)
        item = next(item for item in result["items"] if item["check"] == "sections_order")
        self.assertEqual(item["status"], "FAIL")
        # 正常顺序的同一份文本应 PASS
        baseline = check_journal_compliance(_COMPLIANT_TEXT, profile)
        order_item = next(item for item in baseline["items"] if item["check"] == "sections_order")
        self.assertEqual(order_item["status"], "PASS")

    def test_author_year_style_check(self):
        profile = dict(self.profile)
        profile["reference_style"] = "author-year"
        author_year_refs = _COMPLIANT_TEXT.replace(
            "[1] Author A. Title of the first reference. 2020.\n[2] Author B. Title of the second reference. 2021.",
            "Author, A. (2020). Title of the first reference.\nAuthor, B. (2021). Title of the second reference.",
        )
        result = check_journal_compliance(author_year_refs, profile)
        item = next(item for item in result["items"] if item["check"] == "reference_style")
        self.assertEqual(item["status"], "PASS")
        # 顺序编号文本配作者-年份要求 → FAIL
        result2 = check_journal_compliance(_COMPLIANT_TEXT, profile)
        item2 = next(item for item in result2["items"] if item["check"] == "reference_style")
        self.assertEqual(item2["status"], "FAIL")

    def test_unknown_journal_id_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_journal_profile("no-such-journal")


class RunAuditJournalIntegrationTests(unittest.TestCase):
    """run_audit(journal=...) 的集成：硬门禁、报告与产物文件。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _package(self, text: str) -> dict:
        return {
            "language": "en",
            "paragraphs": [
                {"location": f"sec ¶{i + 1}", "text": line}
                for i, line in enumerate(text.splitlines())
                if line.strip()
            ],
            "claims": [],
            "references": [],
            "statistics": {"registry": [], "comparisons": [], "recomputations": []},
            "figures": [],
        }

    def test_journal_gate_added_and_report_written(self):
        module = skill_module("submission-audit")
        text = _COMPLIANT_TEXT.replace(
            "## Conflict of Interest\nThe authors declare no conflict of interest.\n\n", ""
        )
        audit = module.run(
            {
                "package": self._package(text),
                "out": str(self.tmp),
                "version": "journal-test",
                "journal": "ieee-trans",
            }
        )
        report = audit["audit"] if "audit" in audit else audit
        jc = report["journal_compliance"]
        self.assertIsNotNone(jc)
        self.assertFalse(jc["pass"])
        self.assertIn("journal_compliance", report["hard_gates"])
        self.assertFalse(report["hard_gates"]["journal_compliance"])
        self.assertTrue((self.tmp / "journal-compliance.json").exists())
        self.assertIn("journal_compliance", report["files"])
        self.assertIsNotNone(report["scores"]["journal_format"])

    def test_no_journal_keeps_report_backward_compatible(self):
        module = skill_module("submission-audit")
        audit = module.run(
            {"package": self._package(_COMPLIANT_TEXT), "out": str(self.tmp), "version": "plain"}
        )
        report = audit["audit"] if "audit" in audit else audit
        self.assertIsNone(report.get("journal_compliance"))
        self.assertNotIn("journal_compliance", report["hard_gates"])


if __name__ == "__main__":
    unittest.main()
