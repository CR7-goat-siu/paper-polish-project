import json
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module


class SubmissionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("submission-audit")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.draft = self.tmp / "draft.md"
        self.draft.write_text(
            "# Pipeline test\n\n"
            "## Results\n\n"
            "The intervention improved the primary outcome [1]. Figure 1 shows the result.\n\n"
            "## References\n\n"
            "[1] A. Author. Simulated source. Journal, 2024.\n",
            encoding="utf-8",
        )

    def test_one_command_pipeline_builds_reports_and_fails_closed(self):
        out = self.tmp / "pipeline"
        result = self.module.run(
            {
                "mode": "pipeline",
                "input_file": str(self.draft),
                "language": "en",
                "out": str(out),
                "render": False,
                "version": "pipeline-test",
            }
        )
        self.assertEqual(result["status"], "candidate_with_open_gates")
        self.assertFalse(result["gate"]["automated_pass"])
        self.assertFalse(result["gate"]["final_publication_allowed"])
        self.assertEqual(result["gate"]["human_signoff_status"], "required_not_completed")
        self.assertEqual(result["package"]["status"], "built")
        self.assertGreaterEqual(result["package"]["evidence_job_count"], 1)
        self.assertEqual(result["package"]["statistics_job_count"], 1)
        for key in (
            "release_manifest",
            "reproducibility",
            "human_signoff",
            "author_confirmation",
            "audit_json",
        ):
            self.assertTrue(Path(result["files"][key]).exists(), key)
        audit = json.loads(Path(result["files"]["audit_json"]).read_text(encoding="utf-8"))
        self.assertFalse(audit["gate"]["final_publication_allowed"])
        self.assertEqual(audit["gate"]["human_signoff_status"], "required_not_completed")

    def test_pipeline_never_reports_final_publication_permission(self):
        result = self.module.run(
            {
                "mode": "pipeline",
                "input_file": str(self.draft),
                "language": "en",
                "out": str(self.tmp / "pipeline2"),
                "render": False,
            }
        )
        self.assertFalse(result["final_publication_allowed"])
        self.assertIs(result["audit"]["gate"]["final_publication_allowed"], False)
        quality = Path(result["files"]["quality_report"]).read_text(encoding="utf-8")
        self.assertIn("不得", quality)
        self.assertNotIn("可以无需人工签核发表", quality)


if __name__ == "__main__":
    unittest.main()
