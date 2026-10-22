import json
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module

GOOD_RESPONSE = (
    "The proposed method achieves an accuracy of 94.2% on the benchmark, "
    "as shown in Table 1 [1]. The training remains stable across 20 epochs."
)


class EnPolishTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("en-polish")

    def test_build_jobs_renders_full_prompt(self):
        result = self.module.build_jobs(
            "Our method is a massive improvement. It reaches accuracy of 94.2% [1] over 20 epochs.",
            strength="deep",
            metadata={"title": "Deep Learning", "section_type": "abstract"},
        )
        self.assertEqual(result["mode"], "build")
        self.assertEqual(result["strength"], "deep")
        self.assertGreaterEqual(result["chunk_count"], 1)
        job = result["jobs"][0]
        self.assertTrue(job["system_prompt"])
        prompt = job["prompt"]
        self.assertIn("Deep Learning", prompt)
        self.assertIn("massive improvement", prompt)  # 待润色文本在内
        self.assertNotIn("{{", prompt)  # 占位符全部渲染
        self.assertEqual(job["suggested_model"], "v4-pro")
        self.assertFalse(job["response"])

    def test_build_domain_auto_detect(self):
        result = self.module.build_jobs(
            "A convolutional neural network with long short-term memory is trained by backpropagation.",
            strength="light",
        )
        self.assertEqual(result["domain"], "deep-learning")

    def test_apply_good_response_passes_gate(self):
        source = (
            "The proposed method achieves an accuracy of 94.2% on the benchmark, "
            "as shown in Table 1 [1]. The training remains stable across 20 epochs."
        )
        built = self.module.build_jobs(source, strength="deep", metadata={"section_type": "experiment"})
        for job in built["jobs"]:
            job["response"] = GOOD_RESPONSE
        applied = self.module.apply_responses(source, built["jobs"], strength="deep")
        self.assertEqual(applied["mode"], "apply")
        self.assertEqual(applied["polished_text"], GOOD_RESPONSE)
        report = applied["chunk_reports"][0]
        self.assertEqual(report["gate_verdict"], "pass")
        self.assertEqual(applied["retry_jobs"], [])

    def test_apply_missing_number_generates_retry_job(self):
        source = (
            "The proposed method achieves an accuracy of 94.2% on the benchmark, "
            "as shown in Table 1 [1]. The training remains stable across 20 epochs."
        )
        built = self.module.build_jobs(source, strength="deep", metadata={"section_type": "experiment"})
        for job in built["jobs"]:
            job["response"] = (
                "The proposed method achieves a high accuracy on the benchmark, "
                "as shown in Table 1 [1]. The training remains stable across 20 epochs."
            )
        applied = self.module.apply_responses(source, built["jobs"], strength="deep")
        self.assertEqual(applied["chunk_reports"][0]["gate_verdict"], "fail")
        self.assertEqual(len(applied["retry_jobs"]), 1)
        retry = applied["retry_jobs"][0]
        self.assertEqual(retry["attempt"], 2)
        self.assertIn("94.2%", retry["feedback"])  # 反馈中包含丢失的数字

    def test_apply_without_response_keeps_source(self):
        source = "A simple source sentence with number 3.2 and citation [1]."
        built = self.module.build_jobs(source, strength="light")
        applied = self.module.apply_responses(source, built["jobs"], strength="light")
        self.assertEqual(applied["polished_text"], source)
        self.assertEqual(applied["chunk_reports"][0]["gate_verdict"], "no_response")

    def test_run_build_writes_jobs_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.module.run(
                {
                    "text": "Our method reaches accuracy of 91.2% on the dataset [1].",
                    "strength": "light",
                    "mode": "build",
                    "jobs_file": str(Path(tmp) / "jobs.json"),
                }
            )
            self.assertTrue(Path(result["jobs_file"]).exists())
            payload = json.loads(Path(result["jobs_file"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["job_count"], len(result["jobs"]))

    def test_run_apply_reads_jobs_and_responses_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = "The method reaches accuracy of 91.2% on the dataset [1]."
            result = self.module.run(
                {
                    "text": source,
                    "strength": "light",
                    "mode": "build",
                    "jobs_file": str(root / "jobs.json"),
                }
            )
            responses = root / "responses"
            responses.mkdir()
            job_id = result["jobs"][0]["job_id"]
            (responses / f"{job_id}.txt").write_text(
                "The method reaches an accuracy of 91.2% on the dataset [1].", encoding="utf-8"
            )
            applied = self.module.run(
                {
                    "text": source,
                    "strength": "light",
                    "mode": "apply",
                    "jobs_file": str(root / "jobs.json"),
                    "responses_dir": str(responses),
                    "output": str(root / "out" / "polished.txt"),
                }
            )
            self.assertEqual(applied["mode"], "apply")
            self.assertTrue(Path(root / "out" / "polished.txt").exists())
            self.assertTrue(Path(str(applied["diff_file"])).exists())

    def test_offline_mode_maps_to_build(self):
        built = self.module.run({"text": "Some text with 1.5 and [1].", "strength": "light", "mode": "offline"})
        self.assertEqual(built["mode"], "build")

    def test_no_network_imports(self):
        source = Path("skills/en-polish/main.py").read_text(encoding="utf-8")
        for forbidden in ("urllib", "requests", "httpx", "openai", "DEEPSEEK_API_KEY", "api_key"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
