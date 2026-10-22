import json
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils.config_loader import load_literature_config

PAPER = (
    "# EEG 去噪研究\n\n"
    "## 1 引言\n\n"
    "近年来，基于深度学习的脑电去噪方法研究进展迅速。"
    "该方法在 EEGDenoiseNet 数据集上的表现明显优于现有主流方法，且首次实现了双判别器协同训练[1]。\n"
)
CROSSREF_ITEM = {
    "DOI": "10.1109/TNSRE.2023.3309815",
    "title": ["An Approach for EEG Denoising Based on Wasserstein GAN"],
    "author": [{"given": "Y.", "family": "Dong"}, {"given": "X.", "family": "Li"}],
    "container-title": ["IEEE Transactions on Neural Systems and Rehabilitation Engineering"],
    "volume": "31",
    "issue": "9",
    "page": "3524-3535",
    "published": {"date-parts": [[2023, 8]]},
    "is-referenced-by-count": 21,
    "type": "journal-article",
}


class LiteratureApplyStepTests(unittest.TestCase):
    """lit-search / lit-verify 在 apply 流水线中只产出报告，不改正文。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = skill_module("batch-processor")
        self.paper = self.tmp / "paper.md"
        self.paper.write_text(PAPER, encoding="utf-8")
        self.out = self.tmp / "out"

    def _build_and_fill(self):
        built = self.module.run(
            {"inputs": [str(self.paper)], "language": "cn", "phase": "build", "out": str(self.out)}
        )
        item = built["items"][0]
        jobs = json.loads(Path(item["jobs_file"]).read_text(encoding="utf-8"))["jobs"]
        responses = Path(item["responses_dir"])
        for job in jobs:
            (responses / f"{job['job_id']}.txt").write_text(job["chunk_content"], encoding="utf-8")
        return item

    def test_literature_steps_run_without_changing_text(self):
        item = self._build_and_fill()
        without = self.module.run(
            {"inputs": [str(self.paper)], "language": "cn", "phase": "apply", "out": str(self.out)}
        )
        baseline_text = (self.out / "paper" / "paper.processed.md").read_text(encoding="utf-8")

        applied = self.module.run(
            {
                "inputs": [str(self.paper)],
                "language": "cn",
                "phase": "apply",
                "out": str(self.out),
                "pipeline": "cn-polish,lit-search,lit-verify",
                "lit_args": {"mode": "gap"},
                "lit_verify_args": {},
            }
        )
        self.assertIn(applied["items"][0]["status"], ("ok", "needs_review"))
        self.assertEqual(without["items"][0]["status"], applied["items"][0]["status"])

        lit_dir = Path(item["result_file"]).parent / "step-02-lit-search"
        verify_dir = Path(item["result_file"]).parent / "step-03-lit-verify"
        gap_result = json.loads((lit_dir / "step-result.json").read_text(encoding="utf-8"))
        verify_result = json.loads((verify_dir / "step-result.json").read_text(encoding="utf-8"))
        self.assertEqual(gap_result["mode"], "gap")
        self.assertGreaterEqual(gap_result["gap_count"], 1, "含有绝对化论断的段落应被识别为缺口")
        self.assertEqual(verify_result["summary"], {"verified": 0, "unverified": 0, "rejected": 0})
        self.assertIsNotNone(verify_result["summary"])

        # 文献步骤不得改动正文（含引用标记 [1] 的保真）
        after_text = (self.out / "paper" / "paper.processed.md").read_text(encoding="utf-8")
        self.assertIn("[1]", after_text)
        self.assertIn("EEGDenoiseNet", after_text)
        self.assertEqual(baseline_text.count("\n"), after_text.count("\n"))


class LiteratureAutoTriggerTests(unittest.TestCase):
    """apply 阶段 literature=True 自动追加 lit-search(auto)+lit-verify 步骤。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = skill_module("batch-processor")
        self.paper = self.tmp / "paper.md"
        self.paper.write_text(PAPER, encoding="utf-8")
        self.out = self.tmp / "out"
        lit_module = skill_module("lit-search")
        self.config = json.loads(json.dumps(load_literature_config()))
        self.config["network"]["cache_dir"] = str(self.tmp / "cache")
        for name in ("arxiv", "semantic-scholar", "europepmc", "pubmed"):
            self.config["sources"][name]["enabled"] = False
        original = lit_module.load_literature_config
        lit_module.load_literature_config = lambda: self.config
        self.addCleanup(lambda: setattr(lit_module, "load_literature_config", original))

        import utils.web_fetch as web_fetch

        web_fetch.set_transport(self._transport)
        self.addCleanup(web_fetch.set_transport, None)

    def _transport(self, url, params, headers, timeout):
        if "crossref" in url:
            return json.dumps({"message": {"items": [CROSSREF_ITEM]}})
        return json.dumps({"results": []})

    def _build_and_fill(self):
        built = self.module.run(
            {"inputs": [str(self.paper)], "language": "cn", "phase": "build", "out": str(self.out)}
        )
        item = built["items"][0]
        jobs = json.loads(Path(item["jobs_file"]).read_text(encoding="utf-8"))["jobs"]
        for job in jobs:
            (Path(item["responses_dir"]) / f"{job['job_id']}.txt").write_text(
                job["chunk_content"], encoding="utf-8"
            )
        return item

    def test_literature_flag_appends_lit_steps_and_verifies(self):
        item = self._build_and_fill()
        baseline = self.module.run(
            {"inputs": [str(self.paper)], "language": "cn", "phase": "apply", "out": str(self.out)}
        )
        baseline_text = (self.out / "paper" / "paper.processed.md").read_text(encoding="utf-8")

        applied = self.module.run(
            {
                "inputs": [str(self.paper)],
                "language": "cn",
                "phase": "apply",
                "out": str(self.out),
                "literature": True,
            }
        )
        self.assertEqual(applied["items"][0]["status"], baseline["items"][0]["status"])
        result = json.loads((Path(item["result_file"]).parent / "result.json").read_text(encoding="utf-8"))
        step_names = [step["step"] for step in result["steps"]]
        self.assertIn("lit-search", step_names)
        self.assertIn("lit-verify", step_names)

        search_step = next(step for step in result["steps"] if step["step"] == "lit-search")
        verify_step = next(step for step in result["steps"] if step["step"] == "lit-verify")
        search_result = json.loads(Path(search_step["result_file"]).read_text(encoding="utf-8"))
        verify_result = json.loads(Path(verify_step["result_file"]).read_text(encoding="utf-8"))
        self.assertEqual(search_result["mode"], "auto", "literature=true 触发时应走 auto 一条龙")
        self.assertGreaterEqual(search_result["candidate_count"], 1)
        self.assertTrue(search_result["candidates"][0].get("gap_id"))
        self.assertGreaterEqual(verify_result["summary"]["verified"], 1)

        # 文献链路只产报告，不改正文
        after_text = (self.out / "paper" / "paper.processed.md").read_text(encoding="utf-8")
        self.assertEqual(baseline_text, after_text)
        self.assertIn("[1]", after_text)


if __name__ == "__main__":
    unittest.main()
