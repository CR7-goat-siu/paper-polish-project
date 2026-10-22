import copy
import json
import shutil
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

import utils.web_fetch as web_fetch
from conftest import skill_module
from utils.config_loader import load_literature_config

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
OPENALEX_ITEM = {
    "id": "https://openalex.org/W1",
    "display_name": "EEG artifact removal with deep learning: a review",
    "publication_year": 2024,
    "doi": "https://doi.org/10.3390/s24010001",
    "authorships": [{"author": {"display_name": "Ann Lee"}}],
    "primary_location": {"source": {"display_name": "Sensors"}},
    "cited_by_count": 3,
}
SAMPLE_TEXT = (
    "1 引言\n"
    "近年来，基于深度学习的脑电去噪方法研究进展迅速。\n"
    "该方法在EEGDenoiseNet数据集上的表现明显优于现有主流方法，且首次实现了双判别器协同训练。\n"
    "本文提出一种基于噪声学习的生成对抗网络（NLGAN）。\n"
    "如图3所示，MFM模块由三个并行的1D卷积层构成，已在文献[7]中验证。\n"
)


class LitSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = skill_module("lit-search")
        self.config = copy.deepcopy(load_literature_config())
        self.config["network"]["cache_dir"] = str(self.tmp / "cache")
        for name in ("arxiv", "semantic-scholar", "europepmc"):
            self.config["sources"][name]["enabled"] = False
        self.module.load_literature_config = lambda: self.config
        web_fetch.load_literature_config = lambda: self.config
        self.addCleanup(lambda: setattr(web_fetch, "load_literature_config", load_literature_config))
        web_fetch.reset_stats()
        self.calls: list[str] = []
        web_fetch.set_transport(self._transport)
        self.addCleanup(web_fetch.set_transport, None)

    def _transport(self, url, params, headers, timeout):
        self.calls.append(url)
        if "crossref" in url:
            return json.dumps({"message": {"items": [CROSSREF_ITEM]}})
        return json.dumps({"results": [OPENALEX_ITEM]})

    # ---------------- gap（离线确定性，无网络） ---------------- #

    def test_gap_mode_detects_claim_without_citation(self):
        out_dir = self.tmp / "gap"
        result = self.module.run(
            {"mode": "gap", "text": SAMPLE_TEXT, "language": "cn", "domain": "biomedical-eeg", "out": str(out_dir)}
        )
        self.assertGreaterEqual(result["gap_count"], 2)
        self.assertEqual(self.calls, [], "gap 模式不得发起网络请求")
        quotes = " ".join(gap["quote"] for gap in result["gaps"])
        self.assertIn("明显优于", quotes)
        self.assertIn("首次", quotes)
        self.assertNotIn("本文提出", quotes, "自述句不应计入缺口")
        self.assertNotIn("如图3", quotes, "含引用标记的句子不应计入缺口")
        keywords = result["gaps"][0]["keywords"]
        self.assertTrue(keywords["query"], "应产出英文检索式草稿")
        self.assertTrue(Path(result["files"]["gaps"]).exists())

    def test_gap_mode_reports_missing_reference_entries(self):
        result = self.module.run(
            {
                "mode": "gap",
                "text": "已有研究表明该方法有效[9]。",
                "language": "cn",
                "audit": {"cited_not_listed": [9]},
                "out": str(self.tmp / "gap2"),
            }
        )
        types = [gap["gap_type"] for gap in result["gaps"]]
        self.assertIn("missing_entry", types)
        self.assertEqual(self.calls, [])

    # ---------------- search（桩传输层） ---------------- #

    def test_search_dedupes_sources_and_ranks(self):
        out_dir = self.tmp / "search"
        result = self.module.run(
            {
                "mode": "search",
                "queries": ["EEG denoising GAN", "EEG artifact removal"],
                "language": "cn",
                "out": str(out_dir),
                "top_k": 5,
            }
        )
        self.assertEqual(result["candidate_count"], 2, "同一 DOI 的跨源重复应合并为一条")
        scores = [item["rank_score"] for item in result["candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        titles = " ".join(item["title"] for item in result["candidates"])
        self.assertIn("Wasserstein", titles)
        self.assertIn("artifact removal", titles)
        self.assertTrue(result["candidates"][0]["evidence"], "候选应携带原始证据记录")
        # 多轮检索：2 个查询 × broad/precise/fresh_cited 三轮
        self.assertEqual(result["source_stats"]["crossref"]["hits"], 6)
        round_names = [item["round"] for item in result["round_stats"]]
        self.assertEqual(round_names, ["broad", "precise", "fresh_cited"])
        self.assertTrue(Path(result["files"]["cards"]).exists())

    def test_search_uses_cache_on_second_run(self):
        args = {
            "mode": "search",
            "queries": ["EEG denoising GAN"],
            "language": "cn",
            "out": str(self.tmp / "search2"),
        }
        self.module.run(args)
        first_calls = len(self.calls)
        web_fetch.reset_stats()
        self.module.run(args)
        self.assertEqual(len(self.calls), first_calls, "第二次相同检索应命中缓存，不再发请求")
        self.assertGreater(web_fetch.stats()["cache_hits"], 0)

    def test_offline_without_cache_yields_no_candidates(self):
        result = self.module.run(
            {
                "mode": "search",
                "queries": ["EEG denoising quantum"],
                "language": "cn",
                "offline": True,
                "out": str(self.tmp / "search3"),
            }
        )
        self.assertEqual(result["candidate_count"], 0)
        errors = [err for stats in result["source_stats"].values() for err in stats["errors"]]
        self.assertTrue(any("offline" in err for err in errors), errors)
        self.assertEqual(self.calls, [], "离线模式不得发请求")

    def test_package_mode_renders_external_agent_prompt(self):
        result = self.module.run(
            {
                "mode": "package",
                "text": SAMPLE_TEXT,
                "language": "cn",
                "title": "基于噪声对抗学习的脑电信号去噪研究",
                "out": str(self.tmp / "package"),
            }
        )
        self.assertTrue(result["jobs"])
        prompt_path = Path(result["jobs"][0]["prompt_file"])
        self.assertTrue(prompt_path.exists())
        self.assertIn("gap-", prompt_path.read_text(encoding="utf-8"))


REFERENCES_TEXT = (
    "[1] A. Author. \"An old EEG method,\" Old Journal, vol. 1, pp. 1-10, 2010.\n"
    "[2] B. Author. EEG artifact removal survey[J]. Biomedical Signal Processing, 2025, 45: 100-110.\n"
    "[3] C. Author. A paper without any metadata at all\n"
)


class GapEnhancementTests(unittest.TestCase):
    """stale_citation / incomplete_entry / 同义词扩展。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = skill_module("lit-search")
        self.config = copy.deepcopy(load_literature_config())
        self.config["network"]["cache_dir"] = str(self.tmp / "cache")

    def test_stale_citation_flags_trend_claim_with_only_old_refs(self):
        text = "近年来，基于深度学习的脑电去噪方法取得了长足进步[1]。"
        gaps = self.module.detect_gaps(
            text,
            language="cn",
            domain="biomedical-eeg",
            config=self.config,
            references_text=REFERENCES_TEXT,
        )
        stale = [gap for gap in gaps if gap["gap_type"] == "stale_citation"]
        self.assertEqual(len(stale), 1)
        self.assertIn("2010", stale[0]["signals"]["stale_years"])

    def test_recent_citation_not_flagged_stale(self):
        text = "近年来，基于深度学习的脑电去噪方法取得了长足进步[2]。"
        gaps = self.module.detect_gaps(
            text,
            language="cn",
            domain="biomedical-eeg",
            config=self.config,
            references_text=REFERENCES_TEXT,
        )
        self.assertEqual([gap for gap in gaps if gap["gap_type"] == "stale_citation"], [])

    def test_incomplete_entry_flags_missing_metadata(self):
        gaps = self.module.detect_gaps(
            "本文方法优于现有方法。",
            language="cn",
            config=self.config,
            references_text=REFERENCES_TEXT,
        )
        incomplete = [gap for gap in gaps if gap["gap_type"] == "incomplete_entry"]
        self.assertEqual(len(incomplete), 1, "只有缺元数据的 [3] 应被标记")
        self.assertEqual(incomplete[0]["reference_number"], 3)
        self.assertIn("year", incomplete[0]["missing_fields"])
        self.assertIn("venue", incomplete[0]["missing_fields"])

    def test_build_keywords_expands_with_synonyms(self):
        keywords = self.module.build_keywords(
            "该方法采用 GAN 进行 EEG 去噪。", "cn", "biomedical-eeg", self.config
        )
        self.assertIn("EEG", keywords["terms"])
        expanded = " ".join(keywords["expanded_terms"]).lower()
        self.assertIn("generative adversarial network", expanded)
        self.assertTrue(keywords["query"])
        self.assertIn(" AND ", keywords["boolean"], "precise 布尔式应为 AND 连接")


class MultiRoundAndPubmedTests(unittest.TestCase):
    """多轮检索变体 / PubMed 适配器 / auto 模式 / 富卡片。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = skill_module("lit-search")
        self.config = copy.deepcopy(load_literature_config())
        self.config["network"]["cache_dir"] = str(self.tmp / "cache")
        for name in ("arxiv", "semantic-scholar", "europepmc", "pubmed"):
            self.config["sources"][name]["enabled"] = False
        self.module.load_literature_config = lambda: self.config
        web_fetch.load_literature_config = lambda: self.config
        self.addCleanup(lambda: setattr(web_fetch, "load_literature_config", load_literature_config))
        web_fetch.reset_stats()
        self.calls: list[str] = []
        web_fetch.set_transport(self._transport)
        self.addCleanup(web_fetch.set_transport, None)

    def _transport(self, url, params, headers, timeout):
        self.calls.append(url)
        if "crossref" in url:
            return json.dumps({"message": {"items": [CROSSREF_ITEM]}})
        return json.dumps({"results": [OPENALEX_ITEM]})

    def test_round_variants_broad_precise_fresh(self):
        variants = dict(self.module._round_variants("EEG denoising GAN", None))
        self.assertEqual(variants["broad"], "EEG denoising GAN")
        self.assertEqual(variants["precise"], '"EEG" AND "denoising" AND "GAN"')
        self.assertEqual(variants["fresh_cited"], "EEG denoising GAN")

    def test_round_variants_use_gap_boolean(self):
        keywords = {"query": "EEG denoising", "boolean": '"EEG" AND "denoising"'}
        variants = dict(self.module._round_variants("EEG denoising", keywords))
        self.assertEqual(variants["precise"], '"EEG" AND "denoising"')

    def test_single_round_when_disabled(self):
        self.config["multi_round"]["enabled"] = False
        result = self.module.run(
            {"mode": "search", "queries": ["EEG denoising GAN"], "language": "cn", "out": str(self.tmp / "single")}
        )
        self.assertEqual([item["round"] for item in result["round_stats"]], ["broad"])
        self.assertEqual(result["source_stats"]["crossref"]["hits"], 1)

    def test_cards_include_all_styles_and_summary(self):
        result = self.module.run(
            {"mode": "search", "queries": ["EEG denoising GAN"], "language": "cn", "out": str(self.tmp / "cards")}
        )
        cards = Path(result["files"]["cards"]).read_text(encoding="utf-8")
        self.assertIn("ieee:", cards)
        self.assertIn("apa:", cards)
        self.assertIn("gbt7714:", cards)
        self.assertIn("bibtex:", cards)
        self.assertIn("@article{", cards)


class PubMedAdapterTests(unittest.TestCase):
    """PubMed esearch/esummary 适配器（桩传输层）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = skill_module("lit-search")
        self.config = copy.deepcopy(load_literature_config())
        self.config["network"]["cache_dir"] = str(self.tmp / "cache")
        for name in ("crossref", "openalex", "semantic-scholar", "arxiv", "europepmc"):
            self.config["sources"][name]["enabled"] = False
        self.module.load_literature_config = lambda: self.config
        web_fetch.load_literature_config = lambda: self.config
        self.addCleanup(lambda: setattr(web_fetch, "load_literature_config", load_literature_config))
        web_fetch.reset_stats()
        self.calls: list[str] = []
        web_fetch.set_transport(self._transport)
        self.addCleanup(web_fetch.set_transport, None)

    def _transport(self, url, params, headers, timeout):
        self.calls.append(url)
        if "esearch" in url:
            return json.dumps({"esearchresult": {"count": "1", "idlist": ["37555249"]}})
        return json.dumps(
            {
                "result": {
                    "uids": ["37555249"],
                    "37555249": {
                        "title": "EEG denoising with generative adversarial networks",
                        "authors": [{"name": "Dong Y"}, {"name": "Li X"}],
                        "pubdate": "2023 Aug",
                        "fulljournalname": "IEEE Transactions on Neural Systems and Rehabilitation Engineering",
                        "volume": "31",
                        "issue": "9",
                        "pages": "3524-3535",
                        "elocationid": [{"type": "doi", "value": "10.1109/TNSRE.2023.3309815"}],
                    },
                }
            }
        )

    def test_pubmed_search_two_step_and_canonical(self):
        result = self.module.run(
            {"mode": "search", "queries": ["EEG denoising GAN"], "language": "cn", "out": str(self.tmp / "pm")}
        )
        self.assertEqual(result["candidate_count"], 1)
        entry = result["candidates"][0]
        self.assertEqual(entry["source"], "pubmed")
        self.assertEqual(entry["doi"], "10.1109/tnsre.2023.3309815")
        self.assertEqual(entry["year"], 2023)
        self.assertIn("Dong", entry["authors"][0])
        self.assertIn("IEEE", entry["venue"])
        # 多轮检索：broad + precise 两个不同 esearch；esummary 因 id 相同命中缓存
        esearch_calls = [item for item in self.calls if "esearch" in item]
        esummary_calls = [item for item in self.calls if "esummary" in item]
        self.assertEqual(len(esearch_calls), 2)
        self.assertEqual(len(esummary_calls), 1)
        self.assertIn("esearch", self.calls[0])
        self.assertIn("esummary", self.calls[1])


class SemanticScholarFallbackTests(unittest.TestCase):
    """Graph search 429 时使用同源 bulk/search 端点兜底。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = skill_module("lit-search")
        self.config = copy.deepcopy(load_literature_config())
        self.config["network"]["cache_dir"] = str(self.tmp / "cache")
        self.config["network"]["max_retries"] = 0
        for name in self.config["sources"]:
            self.config["sources"][name]["enabled"] = name == "semantic-scholar"
        self.calls: list[str] = []
        web_fetch.set_transport(self._transport)
        self.addCleanup(web_fetch.set_transport, None)

    def _transport(self, url, params, headers, timeout):
        self.calls.append(url)
        if "/bulk" in url:
            headers = Message()
            headers["Retry-After"] = "0"
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", headers, None)
        return json.dumps(
            {
                "data": [
                    {
                        "paperId": "s2-1",
                        "title": "EEG denoising with graph transformers",
                        "year": 2025,
                        "venue": "IEEE Transactions on Biomedical Engineering",
                        "externalIds": {"DOI": "10.1109/TBME.2025.1234567"},
                        "authors": [{"name": "A. Lee"}],
                    }
                ]
            }
        )

    def test_bulk_429_falls_back_to_graph_search(self):
        result = self.module.search(
            ["EEG denoising"],
            config=self.config,
            sources=["semantic-scholar"],
            rounds=["broad"],
            top_k=3,
            force_refresh=True,
        )
        self.assertEqual(len(self.calls), 2)
        self.assertIn("/bulk", self.calls[0])
        self.assertIn("/paper/search", self.calls[1])
        self.assertEqual(result["source_stats"]["semantic-scholar"]["hits"], 1)
        self.assertEqual(result["candidates"][0]["doi"], "10.1109/tbme.2025.1234567")


class AutoModeTests(unittest.TestCase):
    """auto 模式：gap → search → cards 一条龙。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = skill_module("lit-search")
        self.config = copy.deepcopy(load_literature_config())
        self.config["network"]["cache_dir"] = str(self.tmp / "cache")
        for name in ("arxiv", "semantic-scholar", "europepmc", "pubmed"):
            self.config["sources"][name]["enabled"] = False
        self.module.load_literature_config = lambda: self.config
        web_fetch.load_literature_config = lambda: self.config
        self.addCleanup(lambda: setattr(web_fetch, "load_literature_config", load_literature_config))
        web_fetch.reset_stats()
        web_fetch.set_transport(self._transport)
        self.addCleanup(web_fetch.set_transport, None)

    def _transport(self, url, params, headers, timeout):
        if "crossref" in url:
            return json.dumps({"message": {"items": [CROSSREF_ITEM]}})
        return json.dumps({"results": [OPENALEX_ITEM]})

    def test_auto_mode_chains_gap_search_cards(self):
        result = self.module.run(
            {"mode": "auto", "text": SAMPLE_TEXT, "language": "cn", "domain": "biomedical-eeg", "out": str(self.tmp / "auto")}
        )
        self.assertGreaterEqual(result["gap_count"], 1)
        self.assertGreaterEqual(result["candidate_count"], 1)
        candidate = result["candidates"][0]
        self.assertTrue(candidate.get("gap_id"), "候选应关联缺口")
        self.assertTrue(candidate.get("anchor"), "候选应带论断锚点")
        self.assertTrue(Path(result["files"]["gaps"]).exists())
        self.assertTrue(Path(result["files"]["candidates"]).exists())
        self.assertTrue(Path(result["files"]["cards"]).exists())

    def test_auto_mode_without_gaps_returns_empty_candidates(self):
        result = self.module.run(
            {"mode": "auto", "text": "本文提出一种新方法。如图1所示，结构清晰。", "language": "cn", "out": str(self.tmp / "auto2")}
        )
        self.assertEqual(result["gap_count"], 0)
        self.assertEqual(result["candidate_count"], 0)


if __name__ == "__main__":
    unittest.main()
