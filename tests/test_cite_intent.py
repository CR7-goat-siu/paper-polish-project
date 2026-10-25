"""cite-intent（引用证据强度预填）专项测试。

离线确定性：OpenAlex 倒排摘要还原、claim-citation 对抽取、
fail-closed 回填校验（正常证据句 / 伪造证据句 / 无摘要 / 身份不符 / 非法意图），
以及注入假传输层的 build 端到端（不产生真实网络请求）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils.skill_loader import run_skill
import utils.web_fetch as web_fetch

ABSTRACT = (
    "We propose a transformer-based denoising network for single-channel EEG. "
    "The method improves SNR by 3.2 dB over the best baseline on two public datasets. "
    "However, performance degrades on real-world artifacts collected outside the lab."
)


def _fake_transport_factory(crossref_abstract: str | None = ABSTRACT, openalex_abstract: str | None = ABSTRACT):
    def transport(url: str, params: dict, headers: dict, timeout: float) -> str:
        if "api.crossref.org/works" in url:
            if crossref_abstract is None:
                return json.dumps({"status": "ok", "message": {}})
            return json.dumps({"message": {"abstract": f"<jats:p>{crossref_abstract}</jats:p>"}})
        if "api.openalex.org/works" in url:
            source = openalex_abstract or ""
            if not source:
                return json.dumps({"results": []})
            inverted = {
                word: [index]
                for index, word in enumerate(source.replace(".", "").split())
            }
            return json.dumps({"id": "https://openalex.org/W123", "abstract_inverted_index": inverted})
        if "api.semanticscholar.org" in url:
            return json.dumps({"intents": ["result", "methodology"]})
        raise AssertionError(f"unexpected url in test transport: {url}")

    return transport


class OpenAlexAbstractTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("cite-intent")

    def test_inverted_index_reconstruction(self):
        record = {
            "abstract_inverted_index": {
                "EEG": [4, 9],
                "We": [0],
                "study": [1],
                "signals.": [10],
                "clean": [5],
            }
        }
        text = self.module.openalex_abstract_text(record)
        self.assertEqual(text, "We study EEG clean EEG signals.")

    def test_missing_or_malformed_index(self):
        self.assertEqual(self.module.openalex_abstract_text({}), "")
        self.assertEqual(self.module.openalex_abstract_text({"abstract_inverted_index": "junk"}), "")


class PairExtractionTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("cite-intent")

    def test_pairs_with_ranges_and_repeated_citations(self):
        text = "Deep models help a lot [1,2]. They also work offline [3-5]. No citation here."
        references = [{"id": f"R{i}", "number": i, "title": f"t{i}", "doi": f"10.1/x{i}"} for i in range(1, 6)]
        pairs = self.module.extract_claim_citation_pairs(text, references)
        by_claim = {}
        for pair in pairs:
            by_claim.setdefault(pair["claim_id"], []).append(pair["reference_number"])
        self.assertEqual(by_claim["C001"], [1, 2])
        self.assertEqual(by_claim["C002"], [3, 4, 5])
        self.assertEqual(len(by_claim), 2)

    def test_unknown_reference_number_still_pairs(self):
        text = "A claim about something [9]."
        pairs = self.module.extract_claim_citation_pairs(text, [{"id": "R1", "number": 1}])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["reference_number"], 9)
        self.assertEqual(pairs[0]["reference_id"], "R9")


class ApplyValidationTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("cite-intent")
        self.jobs = {
            "jobs": [
                {
                    "job_id": "citeintent-C001-R1",
                    "claim_id": "C001",
                    "claim_text": "Transformers denoise EEG well [1].",
                    "reference_id": "R1",
                    "reference_number": 1,
                    "reference_title": "Transformer EEG denoising",
                    "reference_doi": "10.1/abc",
                    "source_abstract": ABSTRACT,
                    "abstract_source": "crossref",
                    "abstract_available": True,
                    "s2_mapped_intents": ["supporting"],
                },
                {
                    "job_id": "citeintent-C002-R2",
                    "claim_id": "C002",
                    "claim_text": "Prior work ignored real noise [2].",
                    "reference_id": "R2",
                    "reference_number": 2,
                    "reference_title": "No abstract paper",
                    "reference_doi": "10.1/def",
                    "source_abstract": "",
                    "abstract_source": "",
                    "abstract_available": False,
                    "s2_mapped_intents": [],
                },
            ]
        }

    def _respond(self, job_id: str, payload: dict) -> dict:
        response = dict(payload)
        return response

    def test_supporting_with_verbatim_evidence_prefills(self):
        jobs = json.loads(json.dumps(self.jobs))
        jobs["jobs"][0]["response"] = json.dumps(
            {
                "job_id": "citeintent-C001-R1",
                "claim_id": "C001",
                "reference_id": "R1",
                "intent": "supporting",
                "verbatim_evidence_sentence": "The method improves SNR by 3.2 dB over the best baseline on two public datasets.",
                "rationale": "Direct evidence.",
            }
        )
        result = self.module.apply_intent_responses(jobs)
        self.assertEqual(result["status"], "applied")
        entry = result["prefilled"][0]
        self.assertEqual(entry["final_intent"], "supporting")
        self.assertEqual(entry["confirmation_status"], "prefilled_pending_confirmation")
        self.assertEqual(entry["fail_reasons"], [])
        self.assertEqual(entry["s2_cross_signal"], ["supporting"])

    def test_evidence_sentence_matches_with_case_and_whitespace_noise(self):
        jobs = json.loads(json.dumps(self.jobs))
        jobs["jobs"][0]["response"] = json.dumps(
            {
                "job_id": "citeintent-C001-R1",
                "claim_id": "C001",
                "reference_id": "R1",
                "intent": "contrasting",
                "verbatim_evidence_sentence": "  However,   PERFORMANCE degrades on\nreal-world artifacts collected outside the lab. ",
                "rationale": "r",
            }
        )
        result = self.module.apply_intent_responses(jobs)
        self.assertEqual(result["prefilled"][0]["final_intent"], "contrasting")

    def test_fabricated_evidence_is_fail_closed(self):
        jobs = json.loads(json.dumps(self.jobs))
        jobs["jobs"][0]["response"] = json.dumps(
            {
                "job_id": "citeintent-C001-R1",
                "claim_id": "C001",
                "reference_id": "R1",
                "intent": "supporting",
                "verbatim_evidence_sentence": "This sentence is invented by the model and not in the abstract.",
                "rationale": "fabricated",
            }
        )
        result = self.module.apply_intent_responses(jobs)
        entry = result["prefilled"][0]
        self.assertEqual(entry["final_intent"], "unverified")
        self.assertEqual(entry["confirmation_status"], "manual_evidence_required")
        self.assertIn("unverified_no_verbatim_evidence", entry["fail_reasons"])
        self.assertEqual(result["downgraded_count"], 1)

    def test_missing_evidence_sentence_is_fail_closed(self):
        jobs = json.loads(json.dumps(self.jobs))
        jobs["jobs"][0]["response"] = json.dumps(
            {
                "job_id": "citeintent-C001-R1",
                "claim_id": "C001",
                "reference_id": "R1",
                "intent": "supporting",
                "verbatim_evidence_sentence": "",
                "rationale": "forgot",
            }
        )
        result = self.module.apply_intent_responses(jobs)
        self.assertEqual(result["prefilled"][0]["final_intent"], "unverified")
        self.assertIn("unverified_no_verbatim_evidence", result["prefilled"][0]["fail_reasons"])

    def test_no_abstract_supporting_is_fail_closed(self):
        jobs = json.loads(json.dumps(self.jobs))
        jobs["jobs"][1]["response"] = json.dumps(
            {
                "job_id": "citeintent-C002-R2",
                "claim_id": "C002",
                "reference_id": "R2",
                "intent": "supporting",
                "verbatim_evidence_sentence": "any sentence",
                "rationale": "r",
            }
        )
        result = self.module.apply_intent_responses(jobs)
        entry = result["prefilled"][0]
        self.assertEqual(entry["final_intent"], "unverified")
        self.assertIn("no_abstract_available", entry["fail_reasons"])

    def test_background_needs_no_evidence(self):
        jobs = json.loads(json.dumps(self.jobs))
        jobs["jobs"][0]["response"] = json.dumps(
            {
                "job_id": "citeintent-C001-R1",
                "claim_id": "C001",
                "reference_id": "R1",
                "intent": "background",
                "verbatim_evidence_sentence": "",
                "rationale": "context only",
            }
        )
        result = self.module.apply_intent_responses(jobs)
        entry = result["prefilled"][0]
        self.assertEqual(entry["final_intent"], "background")
        self.assertEqual(entry["confirmation_status"], "prefilled_pending_confirmation")
        self.assertEqual(entry["verbatim_evidence_sentence"], "")

    def test_identity_mismatch_and_invalid_intent_rejected(self):
        jobs = json.loads(json.dumps(self.jobs))
        jobs["jobs"][0]["response"] = json.dumps(
            {
                "job_id": "other-job",
                "claim_id": "C001",
                "reference_id": "R1",
                "intent": "supporting",
                "verbatim_evidence_sentence": ABSTRACT,
            }
        )
        jobs["jobs"][1]["response"] = json.dumps(
            {
                "job_id": "citeintent-C002-R2",
                "claim_id": "C002",
                "reference_id": "R2",
                "intent": "definitely-supporting",
            }
        )
        result = self.module.apply_intent_responses(jobs)
        reasons = {item["reason"] for item in result["rejected"]}
        self.assertIn("job_id_mismatch", reasons)
        self.assertTrue(any(reason.startswith("invalid_intent") for reason in reasons))

    def test_no_responses_pending(self):
        result = self.module.apply_intent_responses(json.loads(json.dumps(self.jobs)))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["pending_count"], 2)


class BuildEndToEndTests(unittest.TestCase):
    """注入假传输层的 build 端到端：验证作业包渲染与摘要注入。"""

    def setUp(self):
        self.module = skill_module("cite-intent")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._original_transport = web_fetch._TRANSPORT
        web_fetch.set_transport(_fake_transport_factory())
        self.addCleanup(web_fetch.set_transport, None)

    def test_build_creates_jobs_with_abstracts_and_s2_signals(self):
        text = (
            "# Paper\n\n"
            "Transformer models denoise EEG well [1].\n"
            "## References\n"
            "[1] Someone. Transformer EEG denoising. 2023. https://doi.org/10.1/abc\n"
        )
        payload = self.module.run(
            {
                "mode": "build",
                "text": text,
                "references": [{"id": "R1", "number": 1, "title": "Transformer EEG denoising", "doi": "10.1/abc"}],
                "language": "en",
                "out": str(self.tmp),
            }
        )
        self.assertEqual(payload["job_count"], 1)
        job = payload["jobs"][0]
        self.assertTrue(job["abstract_available"])
        self.assertIn("transformer-based denoising network", job["source_abstract"])
        self.assertEqual(job["abstract_source"], "crossref")
        self.assertEqual(job["s2_mapped_intents"], ["background", "supporting"])
        prompt = Path(job["prompt_file"]).read_text(encoding="utf-8")
        self.assertIn("citeintent-C001-R1", prompt)
        self.assertIn("3.2 dB", prompt)
        self.assertTrue((self.tmp / "cite-intent-jobs.json").exists())

    def test_build_without_abstract_still_makes_job(self):
        web_fetch.set_transport(_fake_transport_factory(crossref_abstract=None, openalex_abstract=None))
        # 用独立 DOI 避开上一个用例写入磁盘缓存的响应
        text = "A claim [1].\n## References\n[1] A. B. Title. 2020. https://doi.org/10.1/noabs-xyz\n"
        payload = self.module.run(
            {
                "mode": "build",
                "text": text,
                "references": [{"id": "R1", "number": 1, "title": "Title", "doi": "10.1/noabs-xyz"}],
                "language": "en",
                "out": str(self.tmp / "second"),
            }
        )
        job = payload["jobs"][0]
        self.assertFalse(job["abstract_available"])
        self.assertIn("NO ABSTRACT AVAILABLE", Path(job["prompt_file"]).read_text(encoding="utf-8"))


class ArxivFallbackTests(unittest.TestCase):
    """S3：arXiv 摘要兜底（arXiv DOI 在 Crossref 常为 404）。"""

    def setUp(self):
        self.module = skill_module("cite-intent")
        import utils.web_fetch as web_fetch

        self.web_fetch = web_fetch
        self._original = web_fetch._TRANSPORT
        self.addCleanup(web_fetch.set_transport, None)

    def test_arxiv_doi_regex(self):
        match = self.module._ARXIV_DOI_RE.match("10.48550/arxiv.1312.6199")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "1312.6199")
        self.assertIsNone(self.module._ARXIV_DOI_RE.match("10.1109/tsp.2013.2288675"))

    def test_arxiv_id_lookup(self):
        atom = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            "<title>Generative Adversarial Nets</title>"
            "<summary> We propose a new framework via advers process. </summary>"
            "</entry></feed>"
        )
        self.web_fetch.set_transport(lambda url, params, headers, timeout: atom)
        abstract = self.module._arxiv_abstract(arxiv_id="1406.2661", title="", config={"sources": {"arxiv": {"enabled": True, "min_interval_seconds": 0}}}, offline=False)
        self.assertEqual(abstract, "We propose a new framework via advers process.")

    def test_arxiv_title_gate_rejects_mismatch(self):
        atom = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            "<title>A Totally Different Paper About Cooking</title>"
            "<summary>Unrelated summary text that should be rejected.</summary>"
            "</entry></feed>"
        )
        self.web_fetch.set_transport(lambda url, params, headers, timeout: atom)
        abstract = self.module._arxiv_abstract(
            arxiv_id="", title="Generative Adversarial Nets",
            config={"sources": {"arxiv": {"enabled": True, "min_interval_seconds": 0}}}, offline=False,
        )
        self.assertEqual(abstract, "")

    def test_full_chain_arxiv_doi_hit(self):
        def transport(url: str, params: dict, headers: dict, timeout: float) -> str:
            if "api.crossref.org/works" in url:
                raise RuntimeError("HTTP 404")
            if "api.openalex.org" in url:
                return json.dumps({"abstract_inverted_index": None})
            if "export.arxiv.org" in url:
                return (
                    '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                    "<title>Adam: A Method for Stochastic Optimization</title>"
                    "<summary>We introduce Adam, an algorithm for first-order gradient-based optimization.</summary>"
                    "</entry></feed>"
                )
            raise AssertionError(f"unexpected url: {url}")

        self.web_fetch.set_transport(transport)
        result = self.module.fetch_reference_abstract(
            {"doi": "10.48550/arxiv.1412.6980", "title": "Adam: A Method for Stochastic Optimization"},
            config={
                "sources": {
                    "crossref": {"min_interval_seconds": 0},
                    "openalex": {"enabled": True, "min_interval_seconds": 0},
                    "arxiv": {"enabled": True, "min_interval_seconds": 0},
                }
            },
        )
        self.assertEqual(result["source"], "arxiv-doi")
        self.assertIn("Adam", result["abstract"])


class OfflineNoNetworkTests(unittest.TestCase):
    def test_offline_build_with_empty_cache_degrades_to_no_abstract(self):
        module = skill_module("cite-intent")
        text = "A claim [1].\n## References\n[1] A. B. Title. 2020. https://doi.org/10.1/off\n"
        with tempfile.TemporaryDirectory() as tmp:
            payload = module.run(
                {
                    "mode": "build",
                    "text": text,
                    "references": [{"id": "R1", "number": 1, "title": "Title", "doi": "10.1/off"}],
                    "language": "en",
                    "out": str(Path(tmp) / "off"),
                    "offline": True,
                }
            )
            self.assertEqual(payload["job_count"], 1)
            self.assertFalse(payload["jobs"][0]["abstract_available"])


if __name__ == "__main__":
    unittest.main()
