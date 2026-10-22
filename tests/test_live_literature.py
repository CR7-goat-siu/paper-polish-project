"""真实公网文献链路基准测试。

默认跳过，避免普通离线回归依赖外网。设置 RUN_LIVE_LITERATURE=1 后运行：

    $env:RUN_LIVE_LITERATURE='1'
    py -m unittest discover -s tests -p 'test_live_literature.py' -v

覆盖指标：
- A：Crossref / OpenAlex / Semantic Scholar / arXiv / Europe PMC / PubMed 六源连通；
- B：10 篇真实文献正样本 + 5 个伪造/篡改负样本；
- C：batch-processor 只允许 verified 条目进入正文；
- D：本文件与全量离线测试共同回归。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils.citation_check import canonical_entry, missing_required_fields
from utils.config_loader import load_literature_config
import utils.web_fetch as web_fetch

LIVE = os.environ.get("RUN_LIVE_LITERATURE") == "1"

POSITIVE_DOIS = [
    "10.1109/CVPR.2016.90",
    "10.18653/v1/N19-1423",
    "10.1088/1741-2552/aace8c",
    "10.1007/978-3-319-24574-4_28",
    "10.1145/3065386",
    "10.1023/A:1010933404324",
    "10.1038/nature14539",
    "10.1038/nature16961",
    "10.1038/s41586-021-03819-2",
    "10.1056/NEJMoa2034577",
]

NEGATIVE_CANDIDATES = [
    {
        "title": "Neural Gravitational Wave Denoising via Holographic Transformer Networks",
        "authors": ["A. Fake"],
        "year": 2025,
        "venue": "Journal of Fabricated Results",
        "doi": "10.9999/fake.2025.0001",
    },
    {
        "title": "Quantum-Resistant Blockchain Authentication for Wearable EEG Sensors",
        "authors": ["A. Fake"],
        "year": 2025,
        "venue": "Nature",
        "doi": "10.1109/CVPR.2016.90",
    },
    {
        "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
        "authors": ["J. Devlin", "M. Chang", "K. Lee", "K. Toutanova"],
        "year": 2019,
        "venue": "NAACL",
        "doi": "10.5555/bert-fake.2024.001",
    },
    {
        "title": "Random Forests for Quantum Entanglement Classification",
        "authors": ["L. Breiman"],
        "year": 1990,
        "venue": "Machine Learning",
        "doi": "10.1023/A:1010933404324",
    },
    {
        "title": "Neural Gravitational Wave Denoising via Holographic Transformer Networks",
        "authors": ["A. Fake"],
        "year": 2025,
        "venue": "Journal of Fabricated Results",
    },
]


@unittest.skipUnless(LIVE, "真实公网测试需设置 RUN_LIVE_LITERATURE=1")
class LiveLiteratureBenchmarkTests(unittest.TestCase):
    """真实联网闭环；只使用公开免密钥端点。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.metrics: dict[str, object] = {}
        cls.config = copy.deepcopy(load_literature_config())
        cls.config["network"].update(
            {
                "cache_dir": str(cls.tmp / "cache"),
                "timeout_seconds": 30,
                "max_retries": 4,
                "backoff_seconds": 0.75,
            }
        )
        cls.search_module = skill_module("lit-search")
        cls.verify_module = skill_module("lit-verify")
        cls.batch_module = skill_module("batch-processor")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        print("\nLIVE_LITERATURE_METRICS=" + json.dumps(cls.metrics, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _source_query(source: str) -> str:
        # arXiv 的 all:"短语" 查询对短词更稳定；其余源使用医学/信号处理主题。
        return "deep learning" if source == "arxiv" else "EEG denoising"

    def _crossref_entry(self, doi: str) -> dict[str, object]:
        data, result = web_fetch.fetch_json(
            f"https://api.crossref.org/works/{doi}",
            {},
            config=self.config,
            min_interval=0.5,
            force_refresh=True,
        )
        self.assertFalse(result.cached)
        entry = canonical_entry((data or {}).get("message") or {}, "crossref")
        self.assertEqual(entry["doi"], doi.lower())
        return entry

    def test_01_six_sources_are_connected_and_parseable(self):
        sources = [
            "crossref",
            "openalex",
            "semantic-scholar",
            "arxiv",
            "europepmc",
            "pubmed",
        ]
        outcomes: dict[str, dict[str, object]] = {}
        for source in sources:
            web_fetch.reset_stats()
            result = self.search_module.search(
                [self._source_query(source)],
                config=self.config,
                sources=[source],
                rounds=["broad"],
                top_k=3,
                force_refresh=True,
            )
            stats = result["source_stats"].get(source) or {}
            outcomes[source] = {
                "queries": stats.get("queries", 0),
                "hits": stats.get("hits", 0),
                "errors": list(stats.get("errors") or []),
            }
            self.assertGreater(stats.get("queries", 0), 0, f"{source} 未完成解析")
            self.assertGreater(stats.get("hits", 0), 0, f"{source} 解析成功但无命中")
            self.assertEqual(stats.get("errors"), [], f"{source} 网络错误: {stats.get('errors')}")
        self.metrics["sources"] = outcomes
        self.metrics["source_success_rate"] = 1.0

    def test_02_real_positive_and_fabricated_negative_verification(self):
        positive_candidates = []
        for doi in POSITIVE_DOIS:
            entry = self._crossref_entry(doi)
            entry.pop("evidence", None)
            positive_candidates.append(entry)

        positive = self.verify_module.verify_candidates(
            positive_candidates,
            config=self.config,
            online=True,
            offline=False,
        )
        verified_entries = positive["verified_entries"]
        complete = []
        for entry in verified_entries:
            missing = missing_required_fields(entry, ("title", "authors", "year"))
            has_id = bool(entry.get("doi") or entry.get("arxiv_id"))
            complete.append(not missing and has_id)

        tpr = len(verified_entries) / len(positive_candidates)
        completeness = sum(complete) / len(complete) if complete else 0.0
        self.assertGreaterEqual(tpr, 0.9, positive["results"])
        self.assertEqual(completeness, 1.0, verified_entries)

        negative = self.verify_module.verify_candidates(
            NEGATIVE_CANDIDATES,
            config=self.config,
            online=True,
            offline=False,
        )
        blocked = [item for item in negative["results"] if item["verdict"] != "verified"]
        tnr = len(blocked) / len(NEGATIVE_CANDIDATES)
        self.assertEqual(tnr, 1.0, negative["results"])
        self.assertEqual(negative["summary"]["verified"], 0)

        self.metrics["positive"] = {
            "count": len(positive_candidates),
            "verified": len(verified_entries),
            "tpr": round(tpr, 4),
            "metadata_completeness": round(completeness, 4),
        }
        self.metrics["negative"] = {
            "count": len(NEGATIVE_CANDIDATES),
            "blocked": len(blocked),
            "tnr": round(tnr, 4),
            "verified": negative["summary"]["verified"],
        }

    def test_03_arxiv_id_is_preserved_and_verifiable(self):
        result = self.search_module.search(
            ["deep learning"],
            config=self.config,
            sources=["arxiv"],
            rounds=["broad"],
            top_k=3,
            force_refresh=True,
        )
        self.assertTrue(result["candidates"], result["source_stats"])
        candidate = next((item for item in result["candidates"] if item.get("arxiv_id")), None)
        self.assertIsNotNone(candidate, result["candidates"])
        verified = self.verify_module.verify_candidates(
            [candidate],
            config=self.config,
            online=False,
            offline=False,
        )
        self.assertEqual(verified["summary"]["verified"], 1, verified["results"])
        entry = verified["verified_entries"][0]
        self.assertTrue(entry.get("arxiv_id"))
        self.assertEqual(entry["verdict"], "verified")
        self.metrics["arxiv"] = {
            "arxiv_id": entry["arxiv_id"],
            "title": entry.get("title"),
            "verified": True,
        }

    def test_04_batch_pipeline_never_inserts_unverified_entries(self):
        paper = self.tmp / "batch-paper.md"
        paper.write_text(
            "# EEG 去噪研究\n\n"
            "## 1 引言\n\n"
            "该方法在 EEGDenoiseNet 数据集上的表现明显优于现有主流方法。\n",
            encoding="utf-8",
        )
        out = self.tmp / "batch-out"
        built = self.batch_module.run(
            {
                "inputs": [str(paper)],
                "language": "cn",
                "phase": "build",
                "out": str(out),
            }
        )
        item = built["items"][0]
        jobs = json.loads(Path(item["jobs_file"]).read_text(encoding="utf-8"))["jobs"]
        responses = Path(item["responses_dir"])
        for job in jobs:
            (responses / f"{job['job_id']}.txt").write_text(
                job["chunk_content"], encoding="utf-8"
            )

        anchor = "该方法在 EEGDenoiseNet 数据集上的表现明显优于现有主流方法"
        verified_entry = self._crossref_entry("10.1088/1741-2552/aace8c")
        verified_entry.update({"anchor": anchor, "verdict": "verified"})
        fake_entry = dict(verified_entry)
        fake_entry.update(
            {
                "title": "Fabricated Neural Denoising Result",
                "doi": "10.9999/fabricated.2025.0001",
                "doi_display": "10.9999/fabricated.2025.0001",
                "verdict": "unverified",
            }
        )

        good_file = self.tmp / "verified-entries.json"
        good_file.write_text(
            json.dumps({"verified_entries": [verified_entry]}, ensure_ascii=False),
            encoding="utf-8",
        )
        good = self.batch_module.run(
            {
                "inputs": [str(paper)],
                "language": "cn",
                "phase": "apply",
                "out": str(out),
                "pipeline": "cn-polish,cite-manager",
                "cite_args": {"mode": "insert", "entries_file": str(good_file)},
            }
        )
        self.assertEqual(good["items"][0]["status"], "ok", good["items"][0])
        good_text = (out / "batch-paper" / "batch-paper.processed.md").read_text(encoding="utf-8")
        self.assertIn(anchor + "[1]", good_text)
        self.assertIn("doi: 10.1088/1741-2552/aace8c", good_text.lower())

        bad_file = self.tmp / "mixed-entries.json"
        bad_file.write_text(
            json.dumps({"verified_entries": [verified_entry, fake_entry]}, ensure_ascii=False),
            encoding="utf-8",
        )
        bad = self.batch_module.run(
            {
                "inputs": [str(paper)],
                "language": "cn",
                "phase": "apply",
                "out": str(out),
                "pipeline": "cn-polish,cite-manager",
                "cite_args": {"mode": "insert", "entries_file": str(bad_file)},
            }
        )
        self.assertEqual(bad["items"][0]["status"], "error", bad["items"][0])
        self.assertTrue(
            any(error.get("step") == "cite-manager" for error in bad["items"][0]["errors"]),
            bad["items"][0],
        )
        bad_text = (out / "batch-paper" / "batch-paper.processed.md").read_text(encoding="utf-8")
        self.assertNotIn("10.9999/fabricated", bad_text.lower())
        self.assertNotIn(anchor + "[1]", bad_text)
        self.metrics["batch_safety"] = {
            "verified_inserted": True,
            "unverified_inserted": 0,
            "mixed_payload_rejected_atomically": True,
        }


if __name__ == "__main__":
    unittest.main()
