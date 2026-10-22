import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COLLECTION_JSON = ROOT / "data" / "eval" / "academic-paper-collection.json"
COLLECTION_MD = ROOT / "docs" / "academic-paper-collection.md"


class AcademicCollectionTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(COLLECTION_JSON.read_text(encoding="utf-8"))
        self.entries = self.payload["entries"]

    def test_collection_has_at_least_thirty_verified_papers(self):
        self.assertGreaterEqual(self.payload["entry_count"], 30)
        self.assertEqual(len(self.entries), self.payload["entry_count"])
        self.assertEqual(self.payload["rejected_count"], 0)
        self.assertTrue(
            all(
                entry["verification"]
                in {"verified_crossref_doi_record", "verified_arxiv_record"}
                for entry in self.entries
            )
        )

    def test_each_entry_has_searchable_metadata(self):
        titles = set()
        links = set()
        for entry in self.entries:
            self.assertTrue(entry.get("title"))
            self.assertTrue(entry.get("authors"))
            self.assertTrue(entry.get("year"))
            self.assertTrue(entry.get("venue"))
            self.assertTrue(entry.get("summary"))
            self.assertTrue(entry.get("project_value"))
            link = entry.get("doi_url") or entry.get("url")
            self.assertTrue(link and link.startswith("https://"))
            self.assertNotIn(entry["title"].lower(), titles)
            self.assertNotIn(link, links)
            titles.add(entry["title"].lower())
            links.add(link)

    def test_markdown_contains_every_verified_title(self):
        markdown = COLLECTION_MD.read_text(encoding="utf-8")
        for entry in self.entries:
            self.assertIn(entry["title"], markdown)
        self.assertIn("收录数量：33", markdown)
        self.assertIn("核验状态", markdown)


if __name__ == "__main__":
    unittest.main()
