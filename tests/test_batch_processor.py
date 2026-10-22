import json
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils.docx_io import create_minimal_docx, read_paragraphs


class BatchProcessorTests(unittest.TestCase):
    def test_build_then_apply_roundtrip(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "papers"
            paper.mkdir()
            (paper / "a.md").write_text(
                "# Title A\n\n## Abstract\n\nWe reach accuracy of 88.3% on the benchmark [1] with 20 epochs of training.\n",
                encoding="utf-8",
            )
            out = root / "out"
            built = module.run(
                {
                    "inputs": [str(paper / "*.md")],
                    "language": "en",
                    "phase": "build",
                    "strength": "deep",
                    "out": str(out),
                }
            )
            self.assertEqual(built["ok_count"], 1)
            item = built["items"][0]
            self.assertEqual(item["status"], "built")
            jobs_path = Path(item["jobs_file"])
            self.assertTrue(jobs_path.exists())
            self.assertTrue(Path(item["prompts_dir"]).exists())

            # 模拟coding harness：逐作业回填忠实原文的轻改写响应
            jobs = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"]
            responses = Path(item["responses_dir"])
            for job in jobs:
                polished = job["chunk_content"].replace("reach", "achieve")
                (responses / f"{job['job_id']}.txt").write_text(polished, encoding="utf-8")

            applied = module.run(
                {
                    "inputs": [str(paper / "*.md")],
                    "language": "en",
                    "phase": "apply",
                    "strength": "deep",
                    "out": str(out),
                }
            )
            self.assertEqual(applied["ok_count"], 1)
            processed = (out / "a" / "a.processed.md").read_text(encoding="utf-8")
            self.assertIn("achieve", processed)
            self.assertIn("88.3%", processed)  # 数字保真
            self.assertTrue((out / "summary-build.json").exists())
            self.assertTrue((out / "summary-apply.json").exists())

    def test_apply_without_build_errors(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "a.md"
            paper.write_text("# T\n\nBody with 1.5 and [1].\n", encoding="utf-8")
            summary = module.run(
                {
                    "inputs": [str(paper)],
                    "language": "en",
                    "phase": "apply",
                    "out": str(root / "out"),
                }
            )
            self.assertEqual(summary["items"][0]["status"], "error")

    def test_apply_generates_retry_jobs_on_gate_failure(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "a.md"
            paper.write_text(
                "# T\n\n## Results\n\nAccuracy reaches 91.2% on the benchmark [1].\n",
                encoding="utf-8",
            )
            out = root / "out"
            module.run({"inputs": [str(paper)], "language": "en", "phase": "build", "strength": "deep", "out": str(out)})
            jobs = json.loads((out / "a" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
            for job in jobs:
                # 丢失数字 91.2% 的坏响应
                (Path(job_response_dir(out)) / f"{job['job_id']}.txt").write_text(
                    "Accuracy reaches a high level on the benchmark [1].", encoding="utf-8"
                )
            applied = module.run(
                {"inputs": [str(paper)], "language": "en", "phase": "apply", "strength": "deep", "out": str(out)}
            )
            retry_path = out / "a" / "retry-jobs.json"
            self.assertTrue(retry_path.exists())
            retries = json.loads(retry_path.read_text(encoding="utf-8"))["jobs"]
            self.assertGreaterEqual(len(retries), 1)
            self.assertIn("91.2%", retries[0]["feedback"])
            self.assertEqual(applied["items"][0]["status"], "needs_review")

    def test_format_adapter_preserves_polished_body(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper.md"
            paper.write_text(
                "# Test\n\n## Body\n\nWe reach accuracy of 91.2% on the benchmark.\n\n"
                "## References\n\n[1] A. Author. A title. Journal, 2020, 1(2): 3-4.\n",
                encoding="utf-8",
            )
            out = root / "out"
            built = module.run(
                {"inputs": [str(paper)], "language": "en", "phase": "build", "out": str(out)}
            )
            item = built["items"][0]
            jobs = json.loads(Path(item["jobs_file"]).read_text(encoding="utf-8"))["jobs"]
            responses = Path(item["responses_dir"])
            for job in jobs:
                polished = job["chunk_content"].replace("reach", "achieve")
                (responses / f"{job['job_id']}.txt").write_text(polished, encoding="utf-8")
            module.run(
                {"inputs": [str(paper)], "language": "en", "phase": "apply", "out": str(out)}
            )
            processed = (out / "paper" / "paper.processed.md").read_text(encoding="utf-8")
            self.assertIn("achieve accuracy", processed)


def job_response_dir(out: Path) -> Path:
    return out / "a" / "responses"


class BatchDocxTests(unittest.TestCase):
    def test_docx_build_apply_roundtrip(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docx = create_minimal_docx(
                root / "paper.docx",
                [
                    ("Title", "EEG Paper"),
                    ("Heading 1", "Abstract"),
                    ("", "We reach accuracy of 91.2% [1]."),
                    ("Heading 1", "1 Introduction"),
                    ("", "We study electroencephalography signals."),
                    ("Heading 1", "References"),
                    ("", "[1] A. Author. Title. Journal, 2020, pp. 10-20."),
                ],
            )
            out = root / "out"
            built = module.run(
                {"inputs": [str(docx)], "language": "en", "phase": "build", "strength": "deep", "out": str(out)}
            )
            self.assertEqual(built["items"][0]["status"], "built")
            jobs = json.loads((out / "paper" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
            self.assertGreaterEqual(len(jobs), 2)  # 摘要 + 引言两个可润色块
            for job in jobs:
                polished = job["chunk_content"].replace("reach", "achieve")
                (out / "paper" / "responses" / f"{job['job_id']}.txt").write_text(polished, encoding="utf-8")
            applied = module.run(
                {
                    "inputs": [str(docx)],
                    "language": "en",
                    "phase": "apply",
                    "strength": "deep",
                    "pipeline": "en-polish,term-check",
                    "out": str(out),
                }
            )
            self.assertEqual(applied["items"][0]["status"], "ok")
            out_docx = out / "paper" / "paper.polished.docx"
            self.assertTrue(out_docx.exists())
            # 中间产物是纯文本，不得伪装成 .docx
            self.assertTrue((out / "paper" / "paper.processed.txt").exists())
            self.assertFalse((out / "paper" / "paper.processed.docx").exists())
            paragraphs = read_paragraphs(out_docx)
            self.assertIn("achieve", paragraphs[2]["text"])
            self.assertIn("91.2%", paragraphs[2]["text"])
            self.assertEqual(paragraphs[1]["text"], "Abstract")  # 标题块不被润色

    def test_references_only_format_adapter_keeps_prose_ranges(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "a.md"
            paper.write_text(
                "# T\n\nIntro mentions epochs 2-4 and latency 0-100 ms.\n\n"
                "## References\n\n[1] J. Smith. A survey. Journal, 2024, pp. 10-20.\n",
                encoding="utf-8",
            )
            out = root / "out"
            module.run({"inputs": [str(paper)], "language": "en", "phase": "build", "strength": "deep", "out": str(out)})
            # apply 阶段不含 polish，仅 format-adapter + 守卫
            applied = module.run(
                {
                    "inputs": [str(paper)],
                    "language": "en",
                    "phase": "apply",
                    "strength": "deep",
                    "pipeline": "claim-guard,term-check,format-adapter",
                    "out": str(out),
                }
            )
            processed = (out / "a" / "a.processed.md").read_text(encoding="utf-8")
            self.assertIn("10–20", processed)
            self.assertIn("2-4", processed)
            self.assertIn("0-100", processed)
            self.assertEqual(applied["items"][0]["status"], "ok")

    def test_submission_audit_runs_after_docx_export_and_requires_signoff(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docx = create_minimal_docx(
                root / "paper.docx",
                [
                    ("Title", "Preflight Paper"),
                    ("Heading 1", "Results"),
                    ("", "We reach accuracy of 91.2% [1]."),
                    ("Heading 1", "References"),
                    ("", "[1] A. Author. Title. Journal, 2020, pp. 10-20."),
                ],
            )
            out = root / "out"
            module.run(
                {
                    "inputs": [str(docx)],
                    "language": "en",
                    "phase": "build",
                    "strength": "deep",
                    "out": str(out),
                }
            )
            jobs = json.loads((out / "paper" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
            for job in jobs:
                polished = job["chunk_content"].replace("reach", "achieve")
                (out / "paper" / "responses" / f"{job['job_id']}.txt").write_text(
                    polished, encoding="utf-8"
                )
            applied = module.run(
                {
                    "inputs": [str(docx)],
                    "language": "en",
                    "phase": "apply",
                    "strength": "deep",
                    "pipeline": "en-polish,submission-audit",
                    "submission_audit_args": {"render": False},
                    "out": str(out),
                }
            )
            item = applied["items"][0]
            self.assertEqual(item["status"], "needs_review")
            self.assertTrue(Path(item["output_docx_file"]).exists())
            self.assertEqual(
                item["submission_audit"]["human_signoff_status"], "required_not_completed"
            )
            self.assertFalse(item["submission_audit"]["final_publication_allowed"])
            self.assertEqual(item["submission_audit"]["package_build"]["status"], "built")
            self.assertGreaterEqual(
                item["submission_audit"]["package_build"]["evidence_job_count"], 1
            )
            audit_file = Path(item["submission_audit"]["audit_json"])
            self.assertTrue(audit_file.exists())
            audit = json.loads(audit_file.read_text(encoding="utf-8"))
            self.assertFalse(audit["gate"]["automated_pass"])
            self.assertFalse(audit["gate"]["final_publication_allowed"])

    def test_docx_export_uses_per_chunk_text_when_length_changes(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docx = create_minimal_docx(
                root / "paper.docx",
                [
                    ("Title", "Length Change Paper"),
                    ("Heading 1", "Abstract"),
                    (
                        "",
                        "We reach accuracy of 91.2% [1]. The method is very good and totally solves denoising.",
                    ),
                    ("Heading 1", "Results"),
                    ("", "The model reaches 91.2% accuracy [1]. The result show that method is amazing."),
                    ("Heading 1", "References"),
                    ("", "[1] A. Author. Title. Journal, 2020, pp. 10-20."),
                ],
            )
            out = root / "out"
            module.run(
                {
                    "inputs": [str(docx)],
                    "language": "en",
                    "phase": "build",
                    "strength": "deep",
                    "out": str(out),
                }
            )
            jobs = json.loads((out / "paper" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
            for job in jobs:
                heading = job["chunk_content"].split("\n\n", 1)[0].strip()
                body = job["chunk_content"].split("\n\n", 1)[1].strip()
                body = body.replace(" The method is very good and totally solves denoising.", " The method is effective.")
                body = body.replace("The result show that method is amazing.", "The result indicates effectiveness.")
                response = f"{heading}\n\n{body}"
                (out / "paper" / "responses" / f"{job['job_id']}.txt").write_text(
                    response, encoding="utf-8"
                )
            applied = module.run(
                {
                    "inputs": [str(docx)],
                    "language": "en",
                    "phase": "apply",
                    "strength": "deep",
                    "pipeline": "en-polish",
                    "out": str(out),
                }
            )
            item = applied["items"][0]
            self.assertTrue(item["output_docx_file"])
            self.assertTrue(Path(item["output_docx_file"]).exists())
            paragraphs = read_paragraphs(Path(item["output_docx_file"]))
            self.assertEqual(paragraphs[1]["text"], "Abstract")
            self.assertIn("91.2%", paragraphs[2]["text"])
            self.assertNotIn("very good", paragraphs[2]["text"])


if __name__ == "__main__":
    unittest.main()
