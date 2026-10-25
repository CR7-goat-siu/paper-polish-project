"""真实论文黄金标准评测集的离线回归测试。

夹具来自真实 arXiv 论文的短摘录（data/eval/gold-standard/fixtures/，
由 tools/build_gold_standard_corpus.py 采集派生，全文不入 git）；
测试本身确定性离线：对摘录植入五类缺陷必须全部检出，干净摘录必须零 P0。
完整 30+ 篇评测（含网络采集）由 tools/build_gold_standard_corpus.py 按需重跑。
"""

import importlib.util
import json
import unittest
from pathlib import Path

from conftest import ROOT
from utils.skill_loader import run_skill

FIXTURE_DIR = ROOT / "data" / "eval" / "gold-standard" / "fixtures"

_TOOL_PATH = ROOT / "tools" / "build_gold_standard_corpus.py"


def _plant_variants(text: str):
    spec = importlib.util.spec_from_file_location("gold_corpus_tool", _TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.plant_variants(text)


class GoldStandardFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = sorted(FIXTURE_DIR.glob("*.md"))
        if not cls.fixtures:
            raise unittest.SkipTest(
                "金标夹具未生成：先运行 tools/build_gold_standard_corpus.py"
            )

    def test_intact_excerpts_have_no_p0(self):
        """阴性对照：真实论文摘录的完整审计不得出现 P0（阻断级误报）。"""
        for fixture in self.fixtures:
            text = fixture.read_text(encoding="utf-8")
            registry = run_skill("issue-registry", {"text": text, "language": "en"})
            p0 = [i for i in registry["issues"] if i["priority"] == "P0"]
            self.assertEqual(
                p0,
                [],
                f"{fixture.name} 阴性对照出现 P0 误报: "
                f"{[i['title'][:40] for i in p0]}",
            )

    def test_planted_defects_are_all_detected(self):
        """阳性对照：五类植入缺陷在每份真实摘录上全部检出。"""
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
        for fixture in self.fixtures:
            text = fixture.read_text(encoding="utf-8")
            expected_names = manifest[fixture.name]["expected_variants"]
            variants = _plant_variants(text)
            by_name = {v["name"]: v for v in variants}
            for name in expected_names:
                self.assertIn(name, by_name, f"{fixture.name} 应可植入 {name}")
                registry = run_skill(
                    "issue-registry", {"text": by_name[name]["text"], "language": "en"}
                )
                planted_terms = ("revolutionary", "undoubtedly", "state-of-the-art", "perfectly")
                hit = False
                for issue in registry["issues"]:
                    if issue["source"] != by_name[name]["expected_source"]:
                        continue
                    check = (issue.get("metadata") or {}).get("check")
                    found = str((issue.get("metadata") or {}).get("found") or "")
                    if by_name[name]["expected_check"] is not None:
                        hit = hit or check == by_name[name]["expected_check"]
                    else:
                        hit = hit or any(
                            term in found.lower() or term in issue["title"].lower()
                            for term in planted_terms
                        )
                self.assertTrue(
                    hit, f"{fixture.name} 的 {name} 未被检出（召回失败）"
                )


if __name__ == "__main__":
    unittest.main()
