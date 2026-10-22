import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import utils.web_fetch as web_fetch
from conftest import skill_module
from utils.config_loader import load_literature_config

ENTRY = {
    "title": "An Approach for EEG Denoising Based on Wasserstein GAN",
    "authors": ["Y. Dong", "X. Li"],
    "year": 2023,
    "venue": "IEEE Transactions on Neural Systems and Rehabilitation Engineering",
    "volume": "31",
    "issue": "9",
    "pages": "3524-3535",
    "doi": "10.1109/TNSRE.2023.3309815",
    "doi_display": "10.1109/TNSRE.2023.3309815",
    "source": "crossref",
    "source_id": "10.1109/TNSRE.2023.3309815",
    "cited_by": 21,
}
CROSSREF_PAYLOAD = {
    "message": {
        "items": [
            {
                "DOI": "10.1109/TNSRE.2023.3309815",
                "title": ["An Approach for EEG Denoising Based on Wasserstein GAN"],
                "author": [{"given": "Y.", "family": "Dong"}, {"given": "X.", "family": "Li"}],
                "container-title": ["IEEE Transactions on Neural Systems and Rehabilitation Engineering"],
                "volume": "31",
                "issue": "9",
                "page": "3524-3535",
                "published": {"date-parts": [[2023, 8]]},
            }
        ]
    }
}
CROSSREF_WORKS_PAYLOAD = {"message": CROSSREF_PAYLOAD["message"]["items"][0]}


class LitVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = skill_module("lit-verify")
        original_loader = self.module.load_literature_config
        isolated = copy.deepcopy(original_loader())
        isolated.setdefault("network", {})["cache_dir"] = str(self.tmp / "litcache")
        self.module.load_literature_config = lambda: copy.deepcopy(isolated)
        self.addCleanup(
            setattr, self.module, "load_literature_config", original_loader
        )
        # 默认在测试中屏蔽真实网络：在线核验抓取失败 → 按无证据处理（unverified）
        web_fetch.set_transport(self._no_network)
        self.addCleanup(web_fetch.set_transport, None)

    def _no_network(self, url, params, headers, timeout):
        raise web_fetch.FetchError(f"test offline: {url}")

    def _candidate(self, **overrides):
        candidate = dict(ENTRY)
        candidate["evidence"] = [dict(ENTRY)]
        candidate.update(overrides)
        return candidate

    def test_inline_evidence_is_verified(self):
        result = self.module.run({"candidates": [self._candidate()], "out": str(self.tmp / "ok")})
        self.assertEqual(result["summary"], {"verified": 1, "unverified": 0, "rejected": 0})
        self.assertEqual(len(result["verified_entries"]), 1)
        self.assertEqual(result["verified_entries"][0]["verdict"], "verified")
        self.assertIn("doi: 10.1109/TNSRE.2023.3309815", result["results"][0]["reference"])
        self.assertTrue(Path(result["files"]["report"]).exists())

    def test_without_evidence_is_unverified(self):
        candidate = dict(ENTRY)
        result = self.module.run({"candidates": [candidate], "out": str(self.tmp / "no-ev")})
        self.assertEqual(result["summary"]["unverified"], 1)
        self.assertEqual(result["results"][0]["findings"][0]["field"], "evidence")
        self.assertEqual(result["verified_entries"], [])

    def test_reject_doi_prefix_even_without_evidence(self):
        candidate = dict(ENTRY, doi="10.9999/fake.2025.0001", doi_display="10.9999/fake.2025.0001")
        result = self.module.run({"candidates": [candidate], "out": str(self.tmp / "fake")})
        self.assertEqual(result["summary"]["rejected"], 1)
        self.assertEqual(result["verdict"], "has_rejected")

    def test_title_mismatch_with_evidence_is_rejected(self):
        candidate = self._candidate(title="A Totally Different Paper")
        result = self.module.run({"candidates": [candidate], "out": str(self.tmp / "mismatch")})
        self.assertEqual(result["summary"]["rejected"], 1)
        self.assertIn("title", {f["field"] for f in result["results"][0]["findings"]})

    def test_evidence_from_directory_of_raw_payload(self):
        evidence_dir = self.tmp / "evidence"
        evidence_dir.mkdir()
        (evidence_dir / "crossref.json").write_text(json.dumps(CROSSREF_PAYLOAD), encoding="utf-8")
        candidate = dict(ENTRY)  # 无内联证据，只能靠外部证据文件
        result = self.module.run(
            {"candidates": [candidate], "evidence_dir": str(evidence_dir), "evidence_source": "crossref", "out": str(self.tmp / "dir")}
        )
        self.assertEqual(result["summary"]["verified"], 1)
        self.assertEqual(result["evidence_count"], 1)

    def test_require_evidence_can_be_relaxed(self):
        candidate = dict(ENTRY)
        candidate.pop("evidence", None)
        strict = self.module.run({"candidates": [candidate], "out": str(self.tmp / "s1")})
        relaxed = self.module.run(
            {"candidates": [candidate], "require_evidence": False, "out": str(self.tmp / "s2")}
        )
        self.assertEqual(strict["summary"]["unverified"], 1)
        self.assertEqual(relaxed["summary"]["unverified"], 1, "无证据时仍应有一条 warning findings")

    def test_candidates_payload_with_verified_entries_key(self):
        payload = {"verified_entries": [dict(ENTRY)], "summary": {"verified": 1}}
        path = self.tmp / "lit-verification.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = self.module.run({"candidates_file": str(path), "out": str(self.tmp / "payload")})
        self.assertEqual(len(result["results"]), 1)


class OnlineReverifyTests(unittest.TestCase):
    """在线二次核验：DOI 直抓 / 标题检索 / 不一致拒绝 / 网络失败降级。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = skill_module("lit-verify")
        self.config = copy.deepcopy(load_literature_config())
        self.config["network"]["cache_dir"] = str(self.tmp / "cache")
        self.module.load_literature_config = lambda: self.config
        web_fetch.load_literature_config = lambda: self.config
        self.addCleanup(lambda: setattr(web_fetch, "load_literature_config", load_literature_config))
        self.calls: list[str] = []
        web_fetch.set_transport(self._transport)
        self.addCleanup(web_fetch.set_transport, None)

    def _transport(self, url, params, headers, timeout):
        self.calls.append(url)
        if "10.1109/tnsre.2023.3309815" in url.lower():
            return json.dumps(CROSSREF_WORKS_PAYLOAD)
        return json.dumps(CROSSREF_PAYLOAD)

    def test_doi_direct_fetch_verifies_evidence_less_candidate(self):
        candidate = dict(ENTRY)  # 无内联证据
        result = self.module.run(
            {"candidates": [candidate], "online": True, "out": str(self.tmp / "doi")}
        )
        self.assertEqual(result["summary"]["verified"], 1)
        self.assertEqual(result["reverified_count"], 1)
        item = result["results"][0]
        self.assertEqual(item["reverify"]["mode"], "doi")
        self.assertTrue(item["matched_evidence"]["online"])
        self.assertTrue(any("10.1109/tnsre.2023.3309815" in call.lower() for call in self.calls))

    def test_title_search_when_candidate_has_no_doi(self):
        candidate = {key: value for key, value in ENTRY.items() if key not in ("doi", "doi_display")}
        result = self.module.run(
            {"candidates": [candidate], "online": True, "out": str(self.tmp / "title")}
        )
        self.assertEqual(result["summary"]["verified"], 1)
        self.assertEqual(result["results"][0]["reverify"]["mode"], "title")
        self.assertTrue(any("query.bibliographic" in call or "crossref" in call for call in self.calls))

    def test_online_record_mismatch_is_rejected(self):
        candidate = dict(ENTRY, doi="10.1109/other.2020.0001", doi_display="10.1109/other.2020.0001")
        result = self.module.run(
            {"candidates": [candidate], "online": True, "out": str(self.tmp / "mismatch")}
        )
        self.assertEqual(result["summary"]["rejected"], 1)
        self.assertIn("doi", {f["field"] for f in result["results"][0]["findings"]})

    def test_offline_flag_disables_reverify(self):
        candidate = dict(ENTRY)
        result = self.module.run(
            {"candidates": [candidate], "online": False, "out": str(self.tmp / "off")}
        )
        self.assertEqual(result["summary"]["unverified"], 1)
        self.assertFalse(result["online"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
