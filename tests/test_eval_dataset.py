import json
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module


ROOT = Path(__file__).resolve().parent.parent
BAD_DIR = ROOT / "data" / "eval" / "simulated-bad-drafts"
REAL_MANIFEST = ROOT / "data" / "eval" / "real-papers" / "manifest.json"


class EvaluationDatasetTests(unittest.TestCase):
    def test_at_least_ten_simulated_bad_drafts_with_disjoint_splits(self):
        manifest = json.loads((BAD_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["not_for_real_performance_claim"])
        self.assertGreaterEqual(manifest["collected_count"], 10)
        entries = manifest["entries"]
        self.assertEqual(len(entries), manifest["collected_count"])
        paths = {item["path"] for item in entries}
        self.assertEqual(len(paths), len(entries))
        for relative in paths:
            self.assertTrue((BAD_DIR / relative).exists(), relative)
        dev = set(manifest["splits"]["dev"])
        test = set(manifest["splits"]["test"])
        self.assertFalse(dev & test)
        self.assertEqual(dev | test, {item["id"] for item in entries})
        flaw_types = {finding for item in entries for finding in item["expected_findings"]}
        self.assertIn("statistics_claim_mismatch", flaw_types)
        self.assertIn("referenced_item_missing", flaw_types)
        self.assertIn("verified_without_evidence", flaw_types)

    def test_real_dataset_manifest_does_not_overclaim_coverage(self):
        manifest = json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["target_count"], 30)
        self.assertFalse(manifest["real_evaluation_ready"])
        self.assertLess(manifest["distinct_real_papers_collected"], manifest["target_count"])
        self.assertEqual(manifest["held_out_test_count"], 0)
        self.assertGreaterEqual(len(manifest["gaps"]), 1)
        self.assertNotEqual(manifest["dataset_kind"], "simulated_bad_draft")
        self.assertIn("public_metadata_candidate_file", manifest)
        candidate_file = REAL_MANIFEST.parent / manifest["public_metadata_candidate_file"]
        candidates = json.loads(candidate_file.read_text(encoding="utf-8"))
        self.assertGreaterEqual(candidates["metadata_candidate_count"], 30)
        self.assertGreaterEqual(candidates["development_count"], 1)
        self.assertGreaterEqual(candidates["held_out_test_count"], 1)
        self.assertFalse(candidates["real_evaluation_ready"])
        self.assertTrue(candidates["not_for_performance_claim"])
        self.assertTrue(
            all(item["evaluation_status"] == "metadata_only" for item in candidates["entries"])
        )

    def test_all_simulated_bad_drafts_build_and_fail_closed(self):
        manifest = json.loads((BAD_DIR / "manifest.json").read_text(encoding="utf-8"))
        module = skill_module("submission-audit")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = []
            for entry in manifest["entries"]:
                out = root / entry["id"]
                built = module.run(
                    {
                        "mode": "package",
                        "input_file": str(BAD_DIR / entry["path"]),
                        "language": "en",
                        "out": str(out),
                    }
                )
                self.assertEqual(built["status"], "built")
                audited = module.run(
                    {
                        "mode": "audit",
                        "package_file": built["package_file"],
                        "out": str(out / "audit"),
                        "render": False,
                    }
                )
                self.assertFalse(audited["gate"]["automated_pass"])
                self.assertFalse(audited["gate"]["final_publication_allowed"])
                self.assertEqual(
                    audited["gate"]["human_signoff_status"], "required_not_completed"
                )
                results.append(audited["status"])
            self.assertEqual(len(results), 10)
            self.assertTrue(all(status == "candidate_with_open_gates" for status in results))


if __name__ == "__main__":
    unittest.main()
