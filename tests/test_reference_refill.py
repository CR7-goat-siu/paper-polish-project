"""文献条目自动补全（lit-search refill → lit-verify → format-adapter）专项测试。

离线：注入假传输层提供 Crossref/OpenAlex 候选；覆盖
正常补全（auto_filled_with_source Token 级计划）、缺字段维度、
同名歧义中止、标题相似度不足不填、年份容差、第一作者姓氏核对、
lit-verify 门禁拒绝，以及 format-adapter refill_plan 的 token diff 正确性。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils.skill_loader import load_skill, run_skill
import utils.web_fetch as web_fetch

# 候选库：Crossref/OpenAlex 假响应（canonical_entry 需要的字段）
_GOOD_RECORD = {
    "DOI": "10.1234/surrogate-eeg",
    "title": ["Surrogate-based artifact removal from single-channel EEG"],
    "author": [{"family": "Sun", "given": "J."}, {"family": "Li", "given": "K."}],
    "container-title": ["IEEE Transactions on Neural Systems and Rehabilitation Engineering"],
    "volume": "28",
    "issue": "4",
    "page": "900-910",
    "published": {"date-parts": [[2019]]},
    "is-referenced-by-count": 120,
}
_AMBIGUOUS_RECORD = {
    "DOI": "10.1234/surrogate-eeg-extended",
    "title": ["Surrogate-based artifact removal from single-channel EEG"],
    "author": [{"family": "Sun", "given": "J."}, {"family": "Wang", "given": "L."}],
    "container-title": ["Journal of Neural Engineering"],
    "volume": "16",
    "page": "046001",
    "published": {"date-parts": [[2019]]},
    "is-referenced-by-count": 60,
}
_UNRELATED_RECORD = {
    "DOI": "10.1234/unrelated",
    "title": ["Deep learning for cooking recipe generation"],
    "author": [{"family": "Wu", "given": "X."}],
    "container-title": ["Something Else"],
    "published": {"date-parts": [[2021]]},
}
_WRONG_AUTHOR_RECORD = {
    "DOI": "10.1234/same-title-other-author",
    "title": ["Surrogate-based artifact removal from single-channel EEG"],
    "author": [{"family": "Zhang", "given": "Y."}],
    "container-title": ["Another Venue"],
    "volume": "5",
    "page": "1-12",
    "published": {"date-parts": [[2019]]},
}


def _transport_factory(records: list[dict], *, crossref: bool = True, openalex: bool = False):
    def transport(url: str, params: dict, headers: dict, timeout: float) -> str:
        if "api.crossref.org/works" in url:
            if not crossref:
                return json.dumps({"message": {"items": []}})
            return json.dumps({"message": {"items": records}})
        if "api.openalex.org" in url:
            if not openalex:
                return json.dumps({"results": []})
            return json.dumps({"results": []})
        raise AssertionError(f"unexpected url: {url}")

    return transport


class FormatAdapterRefillPlanTests(unittest.TestCase):
    def setUp(self):
        self.module = load_skill("format-adapter")

    def test_token_plan_marks_added_fields(self):
        result = self.module.run(
            {
                "mode": "refill_plan",
                "style": "ieee",
                "entries": [
                    {
                        "number": 5,
                        "line": "Sun J, Li K. Surrogate-based artifact removal from single-channel EEG. 2019.",
                        "verified": {
                            "title": "Surrogate-based artifact removal from single-channel EEG",
                            "authors": ["J. Sun", "K. Li"],
                            "year": 2019,
                            "venue": "IEEE Trans. Neural Syst. Rehabil. Eng.",
                            "volume": "28",
                            "issue": "4",
                            "pages": "900-910",
                            "doi": "10.1234/surrogate-eeg",
                            "source": "crossref",
                            "source_id": "10.1234/surrogate-eeg",
                        },
                    }
                ],
            }
        )
        self.assertEqual(result["plan_count"], 1)
        plan = result["plans"][0]
        self.assertEqual(plan["number"], 5)
        self.assertIn("vol.", plan["target_line"])
        self.assertIn("10.1234/surrogate-eeg", plan["target_line"])
        self.assertEqual(
            plan["auto_filled_with_source"]["doi"], "10.1234/surrogate-eeg"
        )
        self.assertEqual(plan["auto_filled_with_source"]["source"], "crossref")
        added = set(plan["fields_added"])
        self.assertIn("volume", added)
        self.assertIn("pages", added)
        self.assertIn("doi", added)
        # token ops 必须覆盖从 current 到 target 的完整差异
        self.assertTrue(any(op["op"] in {"insert", "replace"} for op in plan["token_ops"]))

    def test_missing_verified_metadata_skips(self):
        result = self.module.run(
            {
                "mode": "refill_plan",
                "style": "ieee",
                "entries": [{"number": 1, "line": "incomplete entry", "verified": {}}],
            }
        )
        self.assertEqual(result["status"], "no_plans")
        self.assertEqual(result["skipped"][0]["reason"], "missing_verified_metadata")

    def test_invalid_style_rejected(self):
        with self.assertRaises(ValueError):
            self.module.run(
                {
                    "mode": "refill_plan",
                    "style": "keep",
                    "entries": [{"number": 1, "line": "x", "verified": {"title": "t"}}],
                }
            )


class RefillPipelineTests(unittest.TestCase):
    def setUp(self):
        self.module = load_skill("lit-search")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._original_transport = web_fetch._TRANSPORT

    def tearDown(self):
        web_fetch.set_transport(None)

    def _run_refill(self, entries: list[dict], records: list[dict], **kwargs) -> dict:
        web_fetch.set_transport(_transport_factory(records))
        return self.module.run(
            {
                "mode": "refill",
                "entries": entries,
                "language": "en",
                "style": "ieee",
                # force_refresh 绕过磁盘缓存读：同 query 的测试之间互不串味
                "force_refresh": True,
                "out": str(self.tmp / kwargs.pop("sub", "r")),
                **kwargs,
            }
        )

    def test_complete_entry_fills_with_source(self):
        entry = {
            "number": 5,
            "line": "Sun J, Li K. Surrogate-based artifact removal from single-channel EEG. 2019.",
            "missing_fields": ["venue", "pages", "doi"],
        }
        result = self._run_refill([entry], [_GOOD_RECORD], sub="filled")
        self.assertEqual(result["summary"]["filled"], 1, json.dumps(result["refills"], ensure_ascii=False))
        refill = result["refills"][0]
        self.assertEqual(refill["status"], "filled")
        self.assertEqual(refill["matched"]["doi"], "10.1234/surrogate-eeg")
        self.assertEqual(refill["matched"]["source"], "crossref")
        self.assertGreaterEqual(refill["matched"]["title_similarity"], 0.92)
        self.assertEqual(refill["plan"]["auto_filled_with_source"]["doi"], "10.1234/surrogate-eeg")
        self.assertIn("pages", refill["plan"]["fields_added"])

    def test_ambiguous_candidates_abort(self):
        entry = {
            "number": 3,
            "line": "Sun J. Surrogate-based artifact removal from single-channel EEG. 2019.",
        }
        result = self._run_refill([entry], [_GOOD_RECORD, _AMBIGUOUS_RECORD], sub="ambiguous")
        refill = result["refills"][0]
        self.assertEqual(refill["status"], "auto_fill_aborted_ambiguous")
        self.assertIn("top_candidates", refill)
        self.assertEqual(result["summary"]["aborted_ambiguous"], 1)

    def test_unrelated_candidates_keep_gap(self):
        entry = {
            "number": 4,
            "line": "Sun J, Li K. Surrogate-based artifact removal from single-channel EEG. 2019.",
        }
        result = self._run_refill([entry], [_UNRELATED_RECORD], sub="unrelated")
        refill = result["refills"][0]
        self.assertEqual(refill["status"], "not_found")
        self.assertLess(refill["best_title_similarity"], 0.92)
        self.assertEqual(result["summary"]["filled"], 0)

    def test_first_author_surname_mismatch_rejected(self):
        entry = {
            "number": 6,
            "line": "Sun J. Surrogate-based artifact removal from single-channel EEG. 2019.",
        }
        result = self._run_refill([entry], [_WRONG_AUTHOR_RECORD], sub="surname")
        refill = result["refills"][0]
        # 标题相似度 1.0 仍被拒：第一作者姓氏门禁（Sun ≠ Zhang）
        self.assertEqual(refill["status"], "not_found")
        self.assertEqual(refill["reason"], "no_candidate_passed_strict_match")
        self.assertEqual(refill["best_title_similarity"], 1.0)

    def test_candidate_without_authors_rejected_when_entry_has_surname(self):
        # 候选缺作者时姓氏无法核对 → 一律拒绝（不阻塞判定、不猜测）
        broken = dict(_GOOD_RECORD)
        broken.pop("author")
        entry = {
            "number": 10,
            "line": "Sun J. Surrogate-based artifact removal from single-channel EEG. 2019.",
        }
        result = self._run_refill([entry], [broken], sub="no-authors")
        self.assertEqual(result["refills"][0]["status"], "not_found")
        self.assertEqual(result["summary"]["filled"], 0)

    def test_year_out_of_tolerance_rejected(self):
        entry = {
            "number": 7,
            "line": "Sun J. Surrogate-based artifact removal from single-channel EEG. 2003.",
        }
        result = self._run_refill([entry], [_GOOD_RECORD], sub="year")
        refill = result["refills"][0]
        self.assertEqual(refill["status"], "not_found")

    def test_references_text_mode_detects_missing_and_fills(self):
        references_text = (
            "[5] Sun J, Li K. Surrogate-based artifact removal from single-channel EEG. 2019.\n"
            "[6] Wu X. Quantum phase estimation for lattice QCD. 2021.\n"
        )
        web_fetch.set_transport(_transport_factory([_GOOD_RECORD, _UNRELATED_RECORD]))
        result = self.module.run(
            {
                "mode": "refill",
                "references": references_text,
                "language": "en",
                "style": "ieee",
                "force_refresh": True,
                "out": str(self.tmp / "reftext"),
            }
        )
        by_number = {item["number"]: item for item in result["refills"]}
        # [5] 缺 venue/pages/doi → 补全
        self.assertEqual(by_number[5]["status"], "filled")
        # [6] 缺页码但候选库无相似文献 → 保持缺口
        self.assertEqual(by_number[6]["status"], "not_found")

    def test_offline_empty_cache_degrades_to_not_found(self):
        entry = {
            "number": 8,
            "line": "Sun J. Surrogate-based artifact removal from single-channel EEG. 2019.",
        }
        result = self._run_refill([entry], [], offline=True, sub="offline")
        self.assertEqual(result["refills"][0]["status"], "not_found")
        self.assertEqual(result["summary"]["filled"], 0)

    def test_lit_verify_gate_blocks_doi_mismatch(self):
        # lit-verify 门禁（refill 链的最后一关）：候选 DOI 与证据记录 DOI 不一致 →
        # verdict=rejected，不产出补全计划（refill 内候选与证据同源，故直接测门禁）
        candidate = {
            "title": "Surrogate-based artifact removal from single-channel EEG",
            "authors": ["J. Sun", "K. Li"],
            "year": 2019,
            "venue": "IEEE Transactions on Neural Systems and Rehabilitation Engineering",
            "volume": "28",
            "issue": "4",
            "pages": "900-910",
            "doi": "10.9999/tampered-doi",
            "source": "crossref",
            # 证据记录来自数据源原始响应，DOI 是真实的 10.1234
            "evidence": [dict(_GOOD_RECORD, source="crossref")],
        }
        verification = run_skill(
            "lit-verify",
            {"candidates": [candidate], "language": "en", "style": "ieee", "online": False},
        )
        self.assertEqual(verification["results"][0]["verdict"], "rejected")
        self.assertEqual(verification["summary"]["verified"], 0)


if __name__ == "__main__":
    unittest.main()
