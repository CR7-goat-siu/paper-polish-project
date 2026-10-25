"""anchor 归一化（S2）：字符级等价放行 / 语义差异仍拒的对抗测试。"""

from __future__ import annotations

import unittest

from utils.anchor_norm import normalize_anchor

ORIGINAL = "The proposed “multi-branch” module improves SNR by 3.2 dB—significantly."


class NormalizeAnchorTests(unittest.TestCase):
    def test_curly_quotes_become_straight(self):
        self.assertEqual(
            normalize_anchor("He said “hello” and ‘bye’"),
            normalize_anchor('He said "hello" and \'bye\''),
        )

    def test_dashes_and_ellipsis(self):
        self.assertEqual(normalize_anchor("A–B—C−D"), normalize_anchor("A-B-C-D"))
        self.assertEqual(normalize_anchor("wait…"), normalize_anchor("wait..."))

    def test_ligature_and_fullwidth(self):
        self.assertEqual(normalize_anchor("ﬁle"), normalize_anchor("file"))
        self.assertEqual(normalize_anchor("ＡＢ１２３"), normalize_anchor("AB123"))

    def test_nbsp_and_whitespace_folding(self):
        self.assertEqual(
            normalize_anchor("a\u00a0\u2003b\tc\nd"),
            normalize_anchor("a b c d"),
        )

    def test_casefold_default_on(self):
        self.assertEqual(normalize_anchor("SNR Improvement"), normalize_anchor("snr improvement"))
        self.assertNotEqual(normalize_anchor("A", casefold=False), normalize_anchor("a"))

    def test_wording_change_is_not_equivalent(self):
        # 语义边界：换词 / 调序 / 增删内容必须不等价
        self.assertNotEqual(normalize_anchor("improves SNR"), normalize_anchor("increases SNR"))
        self.assertNotEqual(normalize_anchor("A then B"), normalize_anchor("B then A"))
        self.assertNotEqual(normalize_anchor("improves SNR"), normalize_anchor("improves SNR greatly"))
        self.assertNotEqual(normalize_anchor(ORIGINAL), normalize_anchor(
            "The proposed multi-branch module raises SNR by 3.2 dB—significantly."
        ))


class ValidatorIntegrationTests(unittest.TestCase):
    """review-sim / cite-intent 校验器实际吃到归一化后的边界行为。"""

    def setUp(self):
        from utils.skill_loader import load_skill

        self.review_sim = load_skill("review-sim")
        self.cite_intent = load_skill("cite-intent")

    def _review_jobs(self, quote_in_response: str) -> dict:
        section_text = '方法描述。The proposed “multi-branch” module improves SNR by 3.2 dB. 实验见下节。'
        return {
            "jobs": [
                {
                    "job_id": "revsim-test-x",
                    "role": "methodology",
                    "section_id": "x",
                    "section_text": section_text,
                    "response": __import__("json").dumps(
                        {
                            "job_id": "revsim-test-x",
                            "section_id": "x",
                            "role": "methodology",
                            "findings": [
                                {
                                    "issue_category": "test",
                                    "claim_or_defect_summary": "s",
                                    "exact_source_quote": quote_in_response,
                                    "line_or_paragraph_locator": "¶1",
                                    "rationale": "r",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        }

    def test_review_sim_accepts_curly_quote_typographic_copy(self):
        # 模型把原文弯引号抄成直引号（常见排版归一）→ 必须放行
        jobs = self._review_jobs('"multi-branch" module improves SNR')
        result = self.review_sim.apply_responses(jobs)
        self.assertEqual(result["applied_count"], 1, result["rejected"])

    def test_review_sim_still_rejects_paraphrase(self):
        jobs = self._review_jobs("multi-branch module raises SNR")
        result = self.review_sim.apply_responses(jobs)
        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(result["rejected"][0]["reason"], "fabricated_or_mismatched_quote")

    def test_cite_intent_accepts_case_and_quote_noise(self):
        abstract = 'We report a “significant” improvement (p < 0.01) in denoising quality.'
        jobs = {
            "jobs": [
                {
                    "job_id": "citeintent-C001-R1",
                    "claim_id": "C001",
                    "claim_text": "claim [1]",
                    "reference_id": "R1",
                    "reference_number": 1,
                    "source_abstract": abstract,
                    "abstract_available": True,
                    "s2_mapped_intents": [],
                    "response": __import__("json").dumps(
                        {
                            "job_id": "citeintent-C001-R1",
                            "claim_id": "C001",
                            "reference_id": "R1",
                            "intent": "supporting",
                            "verbatim_evidence_sentence": 'We report a "significant" improvement (P < 0.01) in denoising quality.',
                            "rationale": "r",
                        }
                    ),
                }
            ]
        }
        result = self.cite_intent.apply_intent_responses(jobs)
        self.assertEqual(result["prefilled"][0]["final_intent"], "supporting", result["rejected"])

    def test_cite_intent_rejects_quote_kind_change(self):
        # 换引号种类（双→单）不是排版等价，必须拒绝
        abstract = 'We report a “significant” improvement in denoising quality.'
        jobs = {
            "jobs": [
                {
                    "job_id": "citeintent-C003-R1",
                    "claim_id": "C003",
                    "claim_text": "claim [1]",
                    "reference_id": "R1",
                    "reference_number": 1,
                    "source_abstract": abstract,
                    "abstract_available": True,
                    "s2_mapped_intents": [],
                    "response": __import__("json").dumps(
                        {
                            "job_id": "citeintent-C003-R1",
                            "claim_id": "C003",
                            "reference_id": "R1",
                            "intent": "supporting",
                            "verbatim_evidence_sentence": "We report a 'significant' improvement in denoising quality.",
                            "rationale": "r",
                        }
                    ),
                }
            ]
        }
        result = self.cite_intent.apply_intent_responses(jobs)
        self.assertEqual(result["prefilled"][0]["final_intent"], "unverified")

    def test_cite_intent_still_rejects_fabrication(self):
        abstract = "We report an improvement in denoising quality."
        jobs = {
            "jobs": [
                {
                    "job_id": "citeintent-C002-R1",
                    "claim_id": "C002",
                    "claim_text": "claim [1]",
                    "reference_id": "R1",
                    "reference_number": 1,
                    "source_abstract": abstract,
                    "abstract_available": True,
                    "s2_mapped_intents": [],
                    "response": __import__("json").dumps(
                        {
                            "job_id": "citeintent-C002-R1",
                            "claim_id": "C002",
                            "reference_id": "R1",
                            "intent": "supporting",
                            "verbatim_evidence_sentence": "We report a dramatic breakthrough in denoising.",
                            "rationale": "r",
                        }
                    ),
                }
            ]
        }
        result = self.cite_intent.apply_intent_responses(jobs)
        self.assertEqual(result["prefilled"][0]["final_intent"], "unverified")


if __name__ == "__main__":
    unittest.main()
