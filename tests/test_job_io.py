import json
import tempfile
import unittest
from pathlib import Path

from utils import job_io


class JobIoTests(unittest.TestCase):
    def test_clean_model_output_strips_fences_and_labels(self):
        raw = "```markdown\n润色后：这是正文。\n```"
        self.assertEqual(job_io.clean_model_output(raw), "这是正文。")
        self.assertEqual(job_io.clean_model_output("Polished text: A b c."), "A b c.")
        self.assertEqual(job_io.clean_model_output("  plain  "), "plain")

    def test_extract_output_text_from_responses_shape(self):
        body = {
            "output": [
                {"type": "reasoning", "content": [{"type": "reasoning", "text": "thinking"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
            ]
        }
        self.assertEqual(job_io.extract_output_text(body), "answer")
        self.assertEqual(job_io.extract_output_text({"output_text": "direct"}), "direct")
        self.assertEqual(job_io.extract_output_text("raw string"), "raw string")

    def test_detect_incomplete_variants(self):
        self.assertEqual(job_io.detect_incomplete({"status": "incomplete"})[0], True)
        self.assertEqual(job_io.detect_incomplete({"stop_reason": "length"})[0], True)
        self.assertEqual(job_io.detect_incomplete({"status": "completed"})[0], False)

    def test_parse_response_field_plain_string(self):
        text, usage, incomplete, reason = job_io.parse_response_field("润色后的文本。")
        self.assertEqual(text, "润色后的文本。")
        self.assertEqual(usage, {})
        self.assertFalse(incomplete)

    def test_parse_response_field_responses_json(self):
        raw = json.dumps(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "部分文本"}]}
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            }
        )
        text, usage, incomplete, reason = job_io.parse_response_field(raw)
        self.assertEqual(text, "部分文本")
        self.assertEqual(usage["total_tokens"], 120)
        self.assertTrue(incomplete)
        self.assertEqual(reason, "max_output_tokens")

    def test_parse_response_field_dict(self):
        text, usage, incomplete, _ = job_io.parse_response_field(
            {"output_text": "text", "usage": {"input_tokens": 5, "output_tokens": 2}}
        )
        self.assertEqual(text, "text")
        self.assertEqual(usage["input_tokens"], 5)

    def test_usage_and_cost_roundtrip(self):
        usage = job_io.normalize_usage({"input_tokens": 1_000_000, "output_tokens": 100_000})
        self.assertEqual(usage["input_tokens"], 1_000_000)
        cost = job_io.estimate_cost_usd(usage, "v4-pro")
        self.assertAlmostEqual(cost, 2.0 + 0.8, places=6)
        self.assertEqual(job_io.estimate_cost_usd(usage, "no-such-model"), 0.0)

    def test_job_roundtrip_and_response_fill(self):
        job = job_io.new_job(
            job_id="c001-a1",
            chunk_id="c001",
            skill="en-polish",
            language="en",
            strength="deep",
            domain="generic",
            section_type="abstract",
            system_prompt="sys",
            prompt="user prompt",
            suggested_model="v4-pro",
            chunk_content="source text",
        )
        self.assertFalse(job_io.job_is_ready(job))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_io.write_jobs(root / "jobs.json", [job], meta={"kind": "build"})
            loaded = job_io.read_jobs(root / "jobs.json")
            self.assertEqual(loaded[0]["job_id"], "c001-a1")
            responses = root / "responses"
            responses.mkdir()
            (responses / "c001-a1.txt").write_text("polished source text", encoding="utf-8")
            filled = job_io.fill_responses_from_dir(loaded, responses)
            self.assertEqual(filled, 1)
            self.assertTrue(job_io.job_is_ready(loaded[0]))
            self.assertEqual(loaded[0]["response"], "polished source text")

    def test_select_best_response_prefers_higher_attempt(self):
        low = {"job_id": "c001-a1", "attempt": 1, "response": "old"}
        high = {"job_id": "c001-a2", "attempt": 2, "response": "new"}
        empty = {"job_id": "c001-a3", "attempt": 3, "response": ""}
        self.assertEqual(job_io.select_best_response([low, high, empty]), high)
        self.assertIsNone(job_io.select_best_response([empty]))

    def test_parse_response_field_non_json_braced_text(self):
        text, _, _, _ = job_io.parse_response_field("{这不是JSON，只是以花括号开头的文本}")
        self.assertIn("这不是JSON", text)


if __name__ == "__main__":
    unittest.main()
