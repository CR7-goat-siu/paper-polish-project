"""批注注入与文档级简报（brief）的引擎级测试，覆盖 en / cn 两侧。"""

import json
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module

SOURCE_EN = (
    "We compare against strong baselines [1]. The accuracy reaches 94.2% over 20 epochs."
)
COMMENT = {
    "id": "1",
    "author": "Prof. Li",
    "date": "2026-09-20T10:00:00Z",
    "text": "Please add a comparison with transformer models.",
    "anchor_text": "We compare against strong baselines",
    "paragraph_indexes": [0],
}


class CommentInjectionTests(unittest.TestCase):
    def test_comment_anchored_into_prompt(self):
        module = skill_module("en-polish")
        result = module.build_jobs(SOURCE_EN, strength="deep", comments=[COMMENT])
        job = result["jobs"][0]
        self.assertIn("Prof. Li", job["prompt"])
        self.assertIn("transformer models", job["prompt"])
        self.assertIn("We compare against strong baselines", job["prompt"])
        self.assertNotIn("{{", job["prompt"])  # {{comments}} 已渲染
        self.assertEqual(result["comments"]["attached_ids"], ["1"])
        self.assertEqual(result["comments"]["unattached_ids"], [])
        self.assertEqual(result["chunks"][0]["comments"], ["1"])
        # 批注文本随 job 落盘，apply 生成重试作业时可还原
        self.assertIn("transformer models", job["comments"])

    def test_comment_without_anchor_reports_warning(self):
        module = skill_module("en-polish")
        orphan = dict(COMMENT, id="2", anchor_text="totally absent anchor text")
        result = module.build_jobs(SOURCE_EN, strength="light", comments=[COMMENT, orphan])
        self.assertEqual(result["comments"]["attached_ids"], ["1"])
        self.assertEqual(result["comments"]["unattached_ids"], ["2"])
        self.assertTrue(any("未能锚定" in w for w in result["warnings"]))
        # 未锚定批注不进 prompt
        self.assertNotIn("totally absent anchor text", result["jobs"][0]["prompt"])

    def test_brief_injected_into_every_prompt(self):
        module = skill_module("en-polish")
        result = module.build_jobs(
            SOURCE_EN, strength="deep", brief="Keep British spelling; do not touch acronyms."
        )
        for job in result["jobs"]:
            self.assertIn("British spelling", job["prompt"])
            self.assertIn("British spelling", job["brief"])
        self.assertEqual(result["brief"], "Keep British spelling; do not touch acronyms.")

    def test_no_comments_renders_none_placeholder(self):
        module = skill_module("en-polish")
        result = module.build_jobs(SOURCE_EN, strength="deep")
        self.assertIn("(none)", result["jobs"][0]["prompt"])
        self.assertEqual(result["comments"]["total"], 0)

    def test_cn_comment_format(self):
        module = skill_module("cn-polish")
        comment = {
            "id": "1",
            "author": "王老师",
            "text": "请补充与 Transformer 方法的对比。",
            "anchor_text": "我们与多个强基线进行了对比",
            "paragraph_indexes": [0],
        }
        text = "我们与多个强基线进行了对比 [1]，准确率达到 94.2%。"
        result = module.build_jobs(text, strength="deep", comments=[comment])
        prompt = result["jobs"][0]["prompt"]
        self.assertIn("王老师", prompt)
        self.assertIn("请补充与 Transformer 方法的对比。", prompt)
        self.assertIn("批注（锚定原文", prompt)

    def test_retry_job_keeps_comments_and_brief(self):
        module = skill_module("en-polish")
        built = module.build_jobs(
            SOURCE_EN,
            strength="deep",
            comments=[COMMENT],
            brief="Prefer active voice.",
        )
        for job in built["jobs"]:
            # 丢失数字 94.2% 的响应必然触发门禁失败
            job["response"] = (
                "We compare against strong baselines [1]. The accuracy is high over 20 epochs."
            )
        applied = module.apply_responses(SOURCE_EN, built["jobs"], strength="deep")
        self.assertEqual(len(applied["retry_jobs"]), 1)
        retry = applied["retry_jobs"][0]
        self.assertIn("transformer models", retry["prompt"])  # 批注还原进重试 prompt
        self.assertIn("Prefer active voice.", retry["prompt"])  # 简报还原
        self.assertIn("94.2%", retry["feedback"])

    def test_run_loads_comments_file_and_brief_file(self):
        module = skill_module("en-polish")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parsed_output = {"comments": [COMMENT]}
            (root / "parsed.json").write_text(
                json.dumps(parsed_output, ensure_ascii=False), encoding="utf-8"
            )
            (root / "brief.txt").write_text("Use SI units only.", encoding="utf-8")
            result = module.run(
                {
                    "text": SOURCE_EN,
                    "strength": "deep",
                    "comments_file": str(root / "parsed.json"),
                    "brief_file": str(root / "brief.txt"),
                }
            )
            prompt = result["jobs"][0]["prompt"]
            self.assertIn("transformer models", prompt)
            self.assertIn("SI units", prompt)


if __name__ == "__main__":
    unittest.main()
