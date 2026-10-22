import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "tools" / "collect_arxiv_eval_candidates.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("collect_arxiv_eval_candidates", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <updated>2024-01-01T00:00:00Z</updated>
    <published>2024-01-01T00:00:00Z</published>
    <title>A Public Test Paper</title>
    <summary>An abstract.</summary>
    <author><name>Ada Author</name></author>
    <link href="http://arxiv.org/abs/2401.00001v1" rel="alternate" type="text/html"/>
    <link href="http://arxiv.org/pdf/2401.00001v1" rel="related" title="pdf" type="application/pdf"/>
    <arxiv:primary_category term="cs.LG"/>
    <arxiv:license>http://creativecommons.org/licenses/by/4.0/</arxiv:license>
  </entry>
</feed>
"""


class ArxivCandidateTests(unittest.TestCase):
    def test_parser_keeps_public_metadata_and_marks_not_ready(self):
        module = _load_module()
        entries = module.parse_arxiv_feed(SAMPLE_FEED, category="cs.LG", split="dev")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["arxiv_id"], "2401.00001v1")
        self.assertEqual(entry["title"], "A Public Test Paper")
        self.assertEqual(entry["authors"], ["Ada Author"])
        self.assertEqual(entry["primary_category"], "cs.LG")
        self.assertTrue(entry["pdf_url"].endswith("2401.00001v1"))
        self.assertEqual(entry["download_status"], "not_downloaded")
        self.assertEqual(entry["evaluation_status"], "metadata_only")
        self.assertEqual(entry["privacy_status"], "public")
        self.assertEqual(entry["split"], "dev")


if __name__ == "__main__":
    unittest.main()
