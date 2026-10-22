import json
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module

SOURCE_CN = (
    "该方法在基准数据集上的准确率达到 94.2%，如表 1 所示 [1]。"
    "训练过程在 20 轮迭代内保持稳定。"
)
GOOD_RESPONSE = SOURCE_CN


class CnPolishTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("cn-polish")

    def test_build_jobs_renders_full_prompt(self):
        result = self.module.build_jobs(
            "本文方法取得了巨大的提升，准确率达到 94.2% [1]，训练 20 轮。",
            strength="deep",
            metadata={"title": "脑电去噪", "section_type": "abstract"},
        )
        self.assertEqual(result["mode"], "build")
        self.assertEqual(result["strength"], "deep")
        self.assertGreaterEqual(result["chunk_count"], 1)
        job = result["jobs"][0]
        self.assertTrue(job["system_prompt"])
        prompt = job["prompt"]
        self.assertIn("脑电去噪", prompt)
        self.assertIn("巨大的提升", prompt)  # 待润色文本在内
        self.assertNotIn("{{", prompt)  # 占位符全部渲染
        self.assertEqual(job["suggested_model"], "v4-pro")
        self.assertFalse(job["response"])

    def test_build_domain_auto_detect(self):
        result = self.module.build_jobs(
            "本文采用卷积神经网络与批量归一化进行脑电信号特征提取，并通过反向传播训练。",
            strength="light",
        )
        self.assertEqual(result["domain"], "deep-learning")

    def test_apply_good_response_passes_gate(self):
        source = SOURCE_CN
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
        source = SOURCE_CN
        built = self.module.build_jobs(source, strength="deep", metadata={"section_type": "experiment"})
        for job in built["jobs"]:
            job["response"] = (
                "该方法在基准数据集上取得了较好的准确率，如表 1 所示 [1]。"
                "训练过程在 20 轮迭代内保持稳定。"
            )
        applied = self.module.apply_responses(source, built["jobs"], strength="deep")
        self.assertEqual(applied["chunk_reports"][0]["gate_verdict"], "fail")
        self.assertEqual(len(applied["retry_jobs"]), 1)
        retry = applied["retry_jobs"][0]
        self.assertEqual(retry["attempt"], 2)
        self.assertIn("94.2%", retry["feedback"])  # 反馈中包含丢失的数字

    def test_apply_without_response_keeps_source(self):
        source = "一句包含数字 3.2 与引用 [1] 的简单源句。"
        built = self.module.build_jobs(source, strength="light")
        applied = self.module.apply_responses(source, built["jobs"], strength="light")
        self.assertEqual(applied["polished_text"], source)
        self.assertEqual(applied["chunk_reports"][0]["gate_verdict"], "no_response")

    def test_run_build_writes_jobs_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.module.run(
                {
                    "text": "该方法在数据集上的准确率为 91.2% [1]。",
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
            source = "该方法在数据集上的准确率为 91.2% [1]."
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
                "该方法在数据集上达到了 91.2% 的准确率 [1]。", encoding="utf-8"
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
        built = self.module.run({"text": "包含数字 1.5 与引用 [1] 的文本。", "strength": "light", "mode": "offline"})
        self.assertEqual(built["mode"], "build")

    def test_no_network_imports(self):
        source = Path("skills/cn-polish/main.py").read_text(encoding="utf-8")
        for forbidden in ("urllib", "requests", "httpx", "openai", "DEEPSEEK_API_KEY", "api_key"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
