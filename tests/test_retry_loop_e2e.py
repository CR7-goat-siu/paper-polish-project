import json
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module

GOOD = "Accuracy reaches 91.2% on the benchmark [1]."
BAD = "Accuracy reaches a high level on the benchmark [1]."


class RetryLoopEndToEndTests(unittest.TestCase):
    """P0 回归：重试作业必须真正改变批量链路的最终产物。"""

    def _build(self, out: Path, module) -> Path:
        paper = out / "a.md"
        paper.write_text(
            "# T\n\n## Results\n\n" + GOOD + "\n\n## Discussion\n\nLatency drops to 0.5 ms with SNR of 20.5 dB.\n",
            encoding="utf-8",
        )
        module.run({"inputs": [str(paper)], "language": "en", "phase": "build", "strength": "deep", "out": str(out)})
        return paper

    def _fill_all(self, out: Path, text_map):
        jobs = json.loads((out / "a" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
        for job in jobs:
            (out / "a" / "responses" / f"{job['job_id']}.txt").write_text(text_map(job), encoding="utf-8")

    def test_retry_response_changes_final_output(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            paper = self._build(out, module)
            # 第一轮：所有作业回填"丢失数字"的坏响应（保留结构，仅丢数字）
            self._fill_all(out, lambda job: job["chunk_content"].replace("91.2%", "a high level"))
            applied1 = module.run(
                {"inputs": [str(paper)], "language": "en", "phase": "apply", "strength": "deep", "out": str(out)}
            )
            self.assertEqual(applied1["items"][0]["status"], "needs_review")
            retry_path = out / "a" / "retry-jobs.json"
            self.assertTrue(retry_path.exists())
            retries = json.loads(retry_path.read_text(encoding="utf-8"))["jobs"]
            self.assertGreaterEqual(len(retries), 1)
            for retry in retries:
                self.assertTrue(retry.get("parent_chunk_id"), "重试作业必须携带 parent_chunk_id")
            self.assertEqual(
                len({r["job_id"] for r in retries}), len(retries), "job_id 必须全局唯一"
            )

            # 第二轮：按新 job_id 回填正确响应（改动措辞、保住数字），重跑 apply
            for retry in retries:
                fixed = retry["chunk_content"].replace("reach", "achieve")
                (out / "a" / "responses" / f"{retry['job_id']}.txt").write_text(fixed, encoding="utf-8")
            applied2 = module.run(
                {"inputs": [str(paper)], "language": "en", "phase": "apply", "strength": "deep", "out": str(out)}
            )
            processed = (out / "a" / "a.processed.md").read_text(encoding="utf-8")
            self.assertIn("91.2%", processed, "重试响应必须进入最终产物")
            self.assertEqual(applied2["items"][0]["status"], "ok")

    def test_retry_ids_unique_across_failing_chunks(self):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            paper = out / "a.md"
            paper.write_text(
                "# T\n\n## A\n\nValue is 91.2% here.\n\n## B\n\nValue is 88.5% there.\n",
                encoding="utf-8",
            )
            module.run({"inputs": [str(paper)], "language": "en", "phase": "build", "strength": "deep", "out": str(out)})
            jobs = json.loads((out / "a" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
            for job in jobs:
                (out / "a" / "responses" / f"{job['job_id']}.txt").write_text(
                    "Value is high." if "%" in job["chunk_content"] else job["chunk_content"],
                    encoding="utf-8",
                )
            module.run({"inputs": [str(paper)], "language": "en", "phase": "apply", "strength": "deep", "out": str(out)})
            retries = json.loads((out / "a" / "retry-jobs.json").read_text(encoding="utf-8"))["jobs"]
            self.assertGreaterEqual(len(retries), 2, "两个失败 chunk 各应产生重试作业")
            self.assertEqual(len({r["job_id"] for r in retries}), len(retries))


if __name__ == "__main__":
    unittest.main()
