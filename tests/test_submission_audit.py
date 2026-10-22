import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from conftest import skill_module
from utils.submission_audit import analyze_docx_table_conflicts, load_config


def _write_minimal_docx(path: Path, *, with_media: bool = True) -> None:
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Results</w:t></w:r></w:p>
    <w:p><w:r><w:t>Accuracy was 45% in the test cohort.</w:t></w:r></w:p>
    <w:sectPr>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
      <w:lnNumType w:countBy="1"/>
    </w:sectPr>
  </w:body>
</w:document>"""
    style_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>
</w:styles>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/styles.xml", style_xml)
        archive.writestr(
            "word/header1.xml",
            '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
        )
        archive.writestr(
            "word/footer1.xml",
            '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
        )
        if with_media:
            archive.writestr("word/media/image1.png", b"not-a-real-png")


class SubmissionAuditTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("submission-audit")
        self.core = self.module.run_audit
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.figure_file = self.tmp / "fig1.png"
        self.figure_file.write_bytes(b"placeholder")
        self.docx = self.tmp / "paper.docx"
        _write_minimal_docx(self.docx)

    def _package(self):
        return {
            "language": "en",
            "paragraphs": [
                {
                    "location": "Results ¶1",
                    "text": "Accuracy was 45% in the test cohort, consistent with prior work [1]. See Figure 1.",
                }
            ],
            "claims": [
                {
                    "id": "C1",
                    "text": "Accuracy was 45% in the test cohort.",
                    "location": "Results ¶1",
                    "critical": True,
                    "citations": ["R1"],
                    "evidence": [
                        {
                            "reference_id": "R1",
                            "source_location": "source PDF p.5 ¶2",
                            "quote": "The test cohort achieved an accuracy of 45 percent.",
                            "support_type": "direct",
                            "support_strength": "high",
                            "verification_status": "verified",
                        }
                    ],
                }
            ],
            "references": [
                {
                    "id": "R1",
                    "number": 1,
                    "doi": "10.1000/example",
                    "title": "A real cited study",
                    "verification_status": "verified",
                }
            ],
            "statistics": {
                "registry": [
                    {
                        "id": "N1",
                        "metric": "accuracy",
                        "value": 45,
                        "unit": "%",
                        "location": "Results ¶1",
                        "meaning": "test cohort accuracy",
                        "critical": True,
                        "verification_status": "verified",
                    }
                ],
                "recomputations": [
                    {
                        "id": "RC1",
                        "operation": "percent",
                        "inputs": [45, 100],
                        "reported": 45,
                    }
                ],
            },
            "figures": [
                {
                    "id": "F1",
                    "kind": "figure",
                    "number": 1,
                    "caption": "Accuracy by cohort.",
                    "body_reference_location": "Results ¶1",
                    "source_file": str(self.figure_file),
                    "format": "png",
                    "resolution_dpi": 600,
                    "is_chart": True,
                    "axis_labels": ["Cohort", "Accuracy (%)"],
                    "units": ["%"],
                    "sample_size_note": "n = 100",
                    "license": "author-owned",
                }
            ],
            "docx_path": str(self.docx),
        }

    def test_valid_evidence_and_preflight_artifacts(self):
        out = self.tmp / "audit"
        result = self.module.run(
            {"package": self._package(), "docx_path": str(self.docx), "out": str(out), "render": False}
        )
        self.assertTrue(result["evidence"]["pass"])
        self.assertTrue(result["statistics"]["pass"])
        self.assertTrue(result["figures"]["pass"])
        self.assertTrue(result["docx"]["pass"])
        self.assertFalse(result["gate"]["final_publication_allowed"])
        self.assertEqual(result["gate"]["human_signoff_status"], "required_not_completed")
        for name in (
            "evidence-graph.json",
            "statistics-registry.json",
            "figures-manifest.json",
            "docx-render-report.json",
            "risk-register.json",
            "author-confirmation.md",
            "human-signoff.md",
            "quality-report.md",
            "release-manifest.json",
            "reproducibility.json",
            "submission-audit.json",
        ):
            self.assertTrue((out / name).exists(), name)
        release = json.loads((out / "release-manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(release["final_publication_allowed"])
        self.assertFalse(release["automated_pass"])
        self.assertTrue(release["inputs"]["package_sha256"])
        self.assertTrue(release["inputs"]["docx_sha256"])
        self.assertTrue(release["output_hashes"]["evidence_graph"])
        signoff = (out / "human-signoff.md").read_text(encoding="utf-8")
        self.assertIn("人工签核", signoff)
        self.assertIn("不得绕过", signoff)

    def test_verified_without_quote_is_downgraded_and_blocks(self):
        package = self._package()
        package["claims"][0]["evidence"][0].pop("quote")
        result = self.core(package, docx_path=self.docx, out_dir=self.tmp / "audit2")
        self.assertFalse(result["evidence"]["pass"])
        self.assertTrue(
            any(item["code"] == "verified_without_evidence" for item in result["evidence"]["findings"])
        )

    def test_recompute_conflict_blocks_statistics(self):
        package = self._package()
        package["statistics"]["recomputations"][0]["reported"] = 54
        result = self.core(package, docx_path=self.docx, out_dir=self.tmp / "audit3")
        self.assertFalse(result["statistics"]["pass"])
        self.assertEqual(result["statistics"]["conflict_count"], 1)
        self.assertEqual(result["statistics"]["recomputations"][0]["calculated"], 45.0)

    def test_missing_referenced_figure_blocks(self):
        package = self._package()
        package["figures"] = []
        result = self.core(package, docx_path=self.docx, out_dir=self.tmp / "audit4")
        self.assertFalse(result["figures"]["pass"])
        self.assertEqual(result["figures"]["missing_items"], ["F1"])

    def test_unresolved_placeholder_blocks_candidate(self):
        package = self._package()
        package["paragraphs"][0]["text"] += " [CITE-TBD]"
        result = self.core(package, docx_path=self.docx, out_dir=self.tmp / "audit5")
        self.assertFalse(result["hard_gates"]["placeholders"])
        self.assertTrue(any(item["code"] == "unresolved_placeholder" for item in result["placeholders"]))

    def test_zotero_field_artifact_fails_docx_structure(self):
        artifact_docx = self.tmp / "artifact.docx"
        with zipfile.ZipFile(self.docx) as source:
            payloads = {name: source.read(name) for name in source.namelist()}
        document = payloads["word/document.xml"].decode("utf-8")
        document = document.replace(
            "<w:r><w:t>Accuracy was 45% in the test cohort.</w:t></w:r>",
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText> ADDIN ZOTERO_BIBL {"uncited":[]} CSL_BIBLIOGRAPHY </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            "<w:r><w:t>Accuracy was 45% in the test cohort.</w:t></w:r>",
        )
        payloads["word/document.xml"] = document.encode("utf-8")
        with zipfile.ZipFile(artifact_docx, "w") as target:
            for name, payload in payloads.items():
                target.writestr(name, payload)
        result = self.core(
            self._package(),
            docx_path=artifact_docx,
            out_dir=self.tmp / "audit-zotero",
        )
        self.assertFalse(result["docx"]["pass"])
        self.assertTrue(
            any(item["code"] == "docx_field_artifact" for item in result["docx"]["findings"])
        )

    def test_table_pagination_without_repeat_header_fails_docx_structure(self):
        table_docx = self.tmp / "table-pagination.docx"
        with zipfile.ZipFile(self.docx) as source:
            payloads = {name: source.read(name) for name in source.namelist()}
        document = payloads["word/document.xml"].decode("utf-8")
        document = document.replace(
            "</w:body>",
            "<w:tbl>"
            "<w:tr><w:tc><w:p><w:r><w:t>Method</w:t></w:r></w:p></w:tc></w:tr>"
            "<w:tr><w:tc><w:p><w:r><w:t>Ours</w:t></w:r></w:p></w:tc></w:tr>"
            "</w:tbl></w:body>",
        )
        payloads["word/document.xml"] = document.encode("utf-8")
        with zipfile.ZipFile(table_docx, "w") as target:
            for name, payload in payloads.items():
                target.writestr(name, payload)
        result = self.core(
            self._package(),
            docx_path=table_docx,
            out_dir=self.tmp / "audit-table-pagination",
        )
        self.assertFalse(result["docx"]["pass"])
        codes = {item["code"] for item in result["docx"]["findings"]}
        self.assertIn("table_header_repeat_missing", codes)
        self.assertIn("table_row_split_enabled", codes)
        self.assertEqual(result["docx"]["table_row_count"], 2)

    def test_no_render_cannot_claim_page_acceptance(self):
        result = self.core(self._package(), docx_path=self.docx, out_dir=self.tmp / "audit6", render=False)
        self.assertFalse(result["render"]["pass"])
        self.assertIn("未请求真实渲染", " ".join(result["render"]["limitations"]))
        self.assertFalse(result["gate"]["automated_pass"])

    def test_package_file_contract(self):
        package_file = self.tmp / "package.json"
        package_file.write_text(json.dumps(self._package(), ensure_ascii=False), encoding="utf-8")
        result = self.module.run(
            {"package_file": str(package_file), "out": str(self.tmp / "audit7"), "render": False}
        )
        self.assertEqual(result["version"], "round-1")
        self.assertFalse(result["gate"]["final_publication_allowed"])

    def test_package_mode_builds_unverified_artifacts_and_evidence_jobs(self):
        manuscript = self.tmp / "draft.md"
        manuscript.write_text(
            "# Draft title\n\n"
            "## Introduction\n\n"
            "Prior studies support this mechanism [1]. Figure 1 shows the trend.\n\n"
            "## Methods\n\n"
            "We enrolled 120 participants and trained for 20 epochs.\n\n"
            "## References\n\n"
            "[1] A. Author. Simulated source. Journal, 2024.\n",
            encoding="utf-8",
        )
        out = self.tmp / "package-run"
        result = self.module.run(
            {
                "mode": "package",
                "input_file": str(manuscript),
                "language": "en",
                "out": str(out),
            }
        )
        self.assertEqual(result["status"], "built")
        self.assertGreaterEqual(result["evidence_job_count"], 1)
        self.assertEqual(result["statistics_job_count"], 1)
        package = json.loads(Path(result["package_file"]).read_text(encoding="utf-8"))
        self.assertEqual(package["package_status"], "draft_unverified")
        self.assertGreaterEqual(len(package["claims"]), 1)
        self.assertGreaterEqual(len(package["statistics"]["registry"]), 2)
        self.assertEqual(package["figures"][0]["id"], "F1")
        self.assertEqual(package["references"][0]["verification_status"], "unverified")
        jobs = json.loads(Path(result["evidence_jobs_file"]).read_text(encoding="utf-8"))
        self.assertEqual(jobs["job_count"], result["evidence_job_count"])
        stats_jobs = json.loads(Path(result["statistics_jobs_file"]).read_text(encoding="utf-8"))
        self.assertEqual(stats_jobs["job_count"], 1)
        prompt = Path(jobs["jobs"][0]["prompt_file"]).read_text(encoding="utf-8")
        self.assertIn("NO SOURCE EXCERPTS PROVIDED", prompt)
        audit = self.module.run(
            {
                "mode": "audit",
                "package_file": result["package_file"],
                "out": str(out / "audit"),
                "render": False,
            }
        )
        self.assertFalse(audit["evidence"]["pass"])
        self.assertFalse(audit["gate"]["final_publication_allowed"])

    def test_package_mode_accepts_docx_via_parser_contract(self):
        from utils.docx_io import create_minimal_docx

        docx = create_minimal_docx(
            self.tmp / "input.docx",
            [
                ("Title", "Input Paper"),
                ("Heading 1", "Results"),
                ("", "Accuracy was 91.2% [1]."),
                ("Heading 1", "References"),
                ("", "[1] A. Author. Source. Journal, 2024, pp. 1-2."),
            ],
        )
        result = self.module.run(
            {
                "mode": "package",
                "input_file": str(docx),
                "language": "en",
                "out": str(self.tmp / "docx-package"),
            }
        )
        self.assertEqual(result["status"], "built")
        package = json.loads(Path(result["package_file"]).read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(package["paragraphs"]), 1)
        self.assertGreaterEqual(result["evidence_job_count"], 1)

    def test_bind_apply_cannot_verify_without_source_material(self):
        manuscript = self.tmp / "no-source.md"
        manuscript.write_text(
            "# Draft\n\n## Results\n\nThis claim cites a source [1].\n\n"
            "## References\n\n[1] A. Author. Simulated source. Journal, 2024.\n",
            encoding="utf-8",
        )
        out = self.tmp / "no-source-run"
        built = self.module.run(
            {
                "mode": "package",
                "input_file": str(manuscript),
                "language": "en",
                "out": str(out),
            }
        )
        jobs = json.loads(Path(built["evidence_jobs_file"]).read_text(encoding="utf-8"))
        job = jobs["jobs"][0]
        Path(job["response_file"]).write_text(
            json.dumps(
                {
                    "claim_id": job["claim_id"],
                    "reference_id": job["reference_id"],
                    "source_location": "p.1",
                    "quote": "A fabricated quote that should not be accepted.",
                    "support_type": "direct",
                    "support_strength": "high",
                    "verification_status": "verified",
                }
            ),
            encoding="utf-8",
        )
        applied = self.module.run(
            {
                "mode": "bind-apply",
                "package_file": built["package_file"],
                "out": str(out),
            }
        )
        self.assertEqual(applied["status"], "applied")
        claim = applied["package"]["claims"][0]
        self.assertEqual(claim["evidence"][0]["verification_status"], "unverified")
        self.assertIn("未向证据作业提供可核验原文材料", claim["evidence"][0]["reasons"])

    def test_bind_apply_accepts_verified_source_and_metadata(self):
        package = self._package()
        package["claims"][0]["evidence"] = []
        package["source_excerpts"] = {
            "R1": "The test cohort achieved an accuracy of 45 percent in the held-out evaluation."
        }
        evidence_dir = self.tmp / "evidence"
        jobs = self.module.build_evidence_jobs(package, evidence_dir, language="en")
        job = jobs["jobs"][0]
        Path(job["response_file"]).write_text(
            json.dumps(
                {
                    "claim_id": "C1",
                    "reference_id": "R1",
                    "source_location": "source PDF p.5 ¶2",
                    "quote": "The test cohort achieved an accuracy of 45 percent.",
                    "support_type": "direct",
                    "support_strength": "high",
                    "verification_status": "verified",
                }
            ),
            encoding="utf-8",
        )
        applied = self.module.run(
            {
                "mode": "bind-apply",
                "package": package,
                "out": str(self.tmp / "bound"),
                "evidence_dir": str(evidence_dir),
                "responses_dir": str(evidence_dir / "responses"),
            }
        )
        self.assertEqual(applied["package"]["claims"][0]["evidence"][0]["verification_status"], "verified")
        audit = self.module.run(
            {
                "mode": "audit",
                "package": applied["package"],
                "docx_path": str(self.docx),
                "out": str(self.tmp / "bound-audit"),
                "render": False,
            }
        )
        self.assertTrue(audit["evidence"]["pass"])

    def test_statistics_classification_preserves_values_and_detects_conflict(self):
        package = self._package()
        package["statistics"]["registry"].extend(
            [
                {
                    "id": "N2",
                    "value": 120,
                    "unit": "participants",
                    "location": "Abstract",
                    "meaning": "enrolled participants",
                    "critical": False,
                    "verification_status": "unverified",
                },
                {
                    "id": "N3",
                    "value": 102,
                    "unit": "participants",
                    "location": "Methods",
                    "meaning": "enrolled participants",
                    "critical": False,
                    "verification_status": "unverified",
                },
            ]
        )
        stats_dir = self.tmp / "statistics"
        jobs = self.module.build_statistics_jobs(package, stats_dir, language="en")
        self.assertEqual(jobs["job_count"], 1)
        response_file = Path(jobs["jobs"][0]["response_file"])
        response_file.write_text(
            json.dumps(
                {
                    "updates": [
                        {
                            "id": "N2",
                            "metric_key": "sample_size_enrolled",
                            "metric": "enrolled sample size",
                            "critical": True,
                            "value": 999,
                        },
                        {
                            "id": "N3",
                            "metric_key": "sample_size_enrolled",
                            "metric": "enrolled sample size",
                            "critical": True,
                        },
                    ],
                    "comparisons": [
                        {
                            "id": "CMP-SAMPLE",
                            "metric": "sample_size_enrolled",
                            "number_ids": ["N2", "N3"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        applied = self.module.run(
            {
                "mode": "stats-apply",
                "package": package,
                "out": str(self.tmp / "stats-applied"),
                "stats_dir": str(stats_dir),
                "responses_dir": str(stats_dir / "responses"),
            }
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["package"]["statistics"]["registry"][1]["value"], 120)
        self.assertTrue(
            any(
                item.get("reason") == "value_change_forbidden"
                for item in applied["summary"]["rejected_updates"]
            )
        )
        audit = self.module.run(
            {
                "mode": "audit",
                "package": applied["package"],
                "docx_path": str(self.docx),
                "out": str(self.tmp / "stats-audit"),
                "render": False,
            }
        )
        self.assertEqual(audit["statistics"]["conflict_count"], 1)
        self.assertFalse(audit["statistics"]["pass"])

    def test_cross_table_conflict_detects_ours_vs_original_method(self):
        tables = [
            {
                "index": 0,
                "rows": [
                    ["伪影类型", "方法", "SNR", "CC", "RRMSE"],
                    ["EOG", "Ours", "12.345", "0.900", "0.070"],
                ],
            },
            {
                "index": 1,
                "rows": [
                    ["伪影类型", "方法", "SNR", "CC", "RRMSE"],
                    ["EOG", "原方法", "13.456", "0.900", "0.090"],
                ],
            },
        ]
        report = analyze_docx_table_conflicts(tables, load_config())
        self.assertEqual(report["conflict_count"], 2)
        metrics = {finding["metric"] for finding in report["findings"]}
        self.assertEqual(metrics, {"SNR", "RRMSE"})


if __name__ == "__main__":
    unittest.main()
