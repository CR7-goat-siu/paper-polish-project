"""T1 job_id 宽容匹配测试：utils.response_matching + review-sim / cite-intent 接入。

背景：跨模型对抗验证实测异构执行器 17/17 不回显/改写作业身份字段。
宽容匹配按内容归因强度分级，歧义即放弃（fail-closed）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils.response_matching import match_files

SECTION_A = (
    "The proposed model achieves 23.9 dB SNR on the benchmark dataset "
    "with negligible latency overhead in online deployment."
)
SECTION_B = (
    "Ablation studies confirm that removing the gating module degrades "
    "accuracy by 4.2 percentage points across all evaluation settings."
)


def _review_payload() -> dict:
    return {
        "jobs": [
            {
                "job_id": "revsim-methodology-method",
                "role": "methodology",
                "section_id": "method",
                "section_text": SECTION_A,
                "response": "",
            },
            {
                "job_id": "revsim-novelty-introduction",
                "role": "novelty",
                "section_id": "introduction",
                "section_text": SECTION_B,
                "response": "",
            },
        ]
    }


def _finding(quote: str) -> dict:
    return {
        "issue_category": "unsupported_claim",
        "claim_or_defect_summary": "SNR claim lacks baseline comparison",
        "exact_source_quote": quote,
        "line_or_paragraph_locator": "para 2",
        "rationale": "no comparison reported",
    }


def _review_response(job_id: str, section_id: str, quote: str) -> str:
    return json.dumps(
        {
            "job_id": job_id,
            "role": "methodology",
            "section_id": section_id,
            "findings": [_finding(quote)],
        },
        ensure_ascii=False,
    )


class MatchFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _payload(self, section_text: str, section_id: str, role: str, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "role": role,
            "section_id": section_id,
            "section_text": section_text,
            "response": "",
        }

    def test_exact_filename(self):
        jobs = [self._payload(SECTION_A, "method", "methodology", "rev-1")]
        (self.tmp / "rev-1.txt").write_text(_review_response("rev-1", "method", "achieves 23.9 dB SNR"), encoding="utf-8")
        result = match_files(jobs, self.tmp, identity_fields=["role", "section_id"], anchor_field="section_text", quote_field="exact_source_quote")
        self.assertEqual(result["assignments"], {"rev-1": str(self.tmp / "rev-1.txt")})
        self.assertEqual(result["unmatched_files"], [])

    def test_identity_echo_unique(self):
        jobs = [
            self._payload(SECTION_A, "method", "methodology", "rev-1"),
            self._payload(SECTION_B, "introduction", "novelty", "rev-2"),
        ]
        # 文件名与 job_id 无关，载荷 job_id 回显错误，但 role+section_id 正确
        (self.tmp / "response-alpha.txt").write_text(
            _review_response("totally-wrong-id", "introduction", "degrades accuracy by 4.2"),
            encoding="utf-8",
        )
        result = match_files(jobs, self.tmp, identity_fields=["role", "section_id"], anchor_field="section_text", quote_field="exact_source_quote")
        self.assertIn("rev-2", result["assignments"])
        self.assertEqual(result["unmatched_files"], [])

    def test_identity_echo_ambiguous_then_anchor_decides(self):
        jobs = [self._payload(SECTION_A, "method", "methodology", "rev-1")]
        body = _review_response("wrong", "method", "achieves 23.9 dB SNR")
        (self.tmp / "a.txt").write_text(body, encoding="utf-8")
        (self.tmp / "b.txt").write_text(body, encoding="utf-8")
        result = match_files(jobs, self.tmp, identity_fields=["role", "section_id"], anchor_field="section_text", quote_field="exact_source_quote")
        # 身份回显有歧义（两个文件同回显）→ 第 3 级放弃；第 4 级内容锚定
        # 唯一归属同一作业，取其一（内容相同，归因确定），另一份留 unmatched
        self.assertIn("rev-1", result["assignments"])
        self.assertEqual(len(result["unmatched_files"]), 1)

    def test_content_anchor_unique(self):
        jobs = [
            self._payload(SECTION_A, "method", "methodology", "rev-1"),
            self._payload(SECTION_B, "introduction", "novelty", "rev-2"),
        ]
        (self.tmp / "orphan.txt").write_text(
            _review_response("wrong", "wrong-section", "degrades accuracy by 4.2"),
            encoding="utf-8",
        )
        result = match_files(jobs, self.tmp, identity_fields=["role", "section_id"], anchor_field="section_text", quote_field="exact_source_quote")
        self.assertEqual(result["assignments"].get("rev-2"), str(self.tmp / "orphan.txt"))

    def test_content_anchor_ambiguous(self):
        shared = "Shared verbatim sentence used by both jobs here."
        jobs = [
            self._payload(shared + " A", "method", "methodology", "rev-1"),
            self._payload(shared + " B", "introduction", "novelty", "rev-2"),
        ]
        (self.tmp / "x.txt").write_text(_review_response("w", "w", shared), encoding="utf-8")
        result = match_files(jobs, self.tmp, identity_fields=["role", "section_id"], anchor_field="section_text", quote_field="exact_source_quote")
        self.assertEqual(result["assignments"], {})
        self.assertEqual(result["unmatched_files"][0]["reason"], "ambiguous_content_anchor")

    def test_unparsable_file_reported(self):
        jobs = [self._payload(SECTION_A, "method", "methodology", "rev-1")]
        (self.tmp / "junk.txt").write_text("模型拒绝输出 JSON", encoding="utf-8")
        result = match_files(jobs, self.tmp, identity_fields=["role", "section_id"], anchor_field="section_text", quote_field="exact_source_quote")
        self.assertEqual(result["assignments"], {})
        self.assertEqual(result["unmatched_files"][0]["reason"], "unparsable_or_not_json")

    def test_filename_substring_unique(self):
        jobs = [self._payload(SECTION_A, "method", "methodology", "rev-1")]
        (self.tmp / "rev-1-attempt2.txt").write_text(
            _review_response("rev-1", "method", "achieves 23.9 dB SNR"), encoding="utf-8"
        )
        result = match_files(jobs, self.tmp, identity_fields=["role", "section_id"], anchor_field="section_text", quote_field="exact_source_quote")
        self.assertIn("rev-1", result["assignments"])


class ReviewSimToleranceTests(unittest.TestCase):
    """review-sim apply 接入宽容匹配：身份凭据降级为 role+section_id。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.module = skill_module("review-sim")

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relabel_accepted_with_warning(self):
        payload = _review_payload()
        responses = self.tmp / "responses"
        responses.mkdir()
        (responses / "arbitrary-name.txt").write_text(
            _review_response("executor-relabeled", "method", "achieves 23.9 dB SNR"), encoding="utf-8"
        )
        result = self.module.apply_responses(payload, responses)
        self.assertEqual(result["applied_count"], 1)
        self.assertTrue(any("job_id_relabel_accepted" in w for w in result["warnings"]))

    def test_wrong_section_id_still_rejected(self):
        payload = _review_payload()
        responses = self.tmp / "responses"
        responses.mkdir()
        (responses / "revsim-methodology-method.txt").write_text(
            _review_response("revsim-methodology-method", "introduction", "achieves 23.9 dB SNR"),
            encoding="utf-8",
        )
        result = self.module.apply_responses(payload, responses)
        self.assertEqual(result["applied_count"], 0)
        self.assertTrue(any(r["reason"] == "section_id_mismatch" for r in result["rejected"]))

    def test_exact_path_still_works(self):
        payload = _review_payload()
        responses = self.tmp / "responses"
        responses.mkdir()
        (responses / "revsim-methodology-method.txt").write_text(
            _review_response("revsim-methodology-method", "method", "achieves 23.9 dB SNR"),
            encoding="utf-8",
        )
        result = self.module.apply_responses(payload, responses)
        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(result["unmatched_responses"], [])


class CiteIntentToleranceTests(unittest.TestCase):
    """cite-intent apply 接入宽容匹配：身份凭据降级为 claim_id+reference_id。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.module = skill_module("cite-intent")

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relabel_accepted(self):
        abstract = "Electroencephalography denoising benefits from adversarial training across benchmarks."
        payload = {
            "jobs": [
                {
                    "job_id": "citeintent-c1-r1",
                    "claim_id": "c1",
                    "reference_id": "r1",
                    "source_abstract": abstract,
                    "response": "",
                }
            ]
        }
        responses = self.tmp / "responses"
        responses.mkdir()
        body = json.dumps(
            {
                "job_id": "relabeled-by-executor",
                "claim_id": "c1",
                "reference_id": "r1",
                "intent": "supporting",
                "verbatim_evidence_sentence": "denoising benefits from adversarial training",
                "rationale": "abstract directly supports the claim",
            },
            ensure_ascii=False,
        )
        (responses / "mislabeled.txt").write_text(body, encoding="utf-8")
        result = self.module.apply_intent_responses(payload, responses)
        self.assertEqual(result["prefilled_count"], 1)
        self.assertEqual(result["prefilled"][0]["confirmation_status"], "prefilled_pending_confirmation")
        self.assertTrue(any("job_id_relabel_accepted" in w for w in result["warnings"]))

    def test_identity_mismatch_still_rejected(self):
        abstract = "Electroencephalography denoising benefits from adversarial training across benchmarks."
        payload = {
            "jobs": [
                {
                    "job_id": "citeintent-c1-r1",
                    "claim_id": "c1",
                    "reference_id": "r1",
                    "source_abstract": abstract,
                    "response": "",
                }
            ]
        }
        responses = self.tmp / "responses"
        responses.mkdir()
        body = json.dumps(
            {
                "job_id": "whatever",
                "claim_id": "c999",
                "reference_id": "r1",
                "intent": "supporting",
                "verbatim_evidence_sentence": "denoising benefits from adversarial training",
                "rationale": "mismatched claim",
            },
            ensure_ascii=False,
        )
        (responses / "mislabeled2.txt").write_text(body, encoding="utf-8")
        result = self.module.apply_intent_responses(payload, responses)
        self.assertEqual(result["prefilled_count"], 0)
        self.assertTrue(any(r["reason"] == "job_id_mismatch" for r in result["rejected"]))


if __name__ == "__main__":
    unittest.main()
