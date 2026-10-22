import unittest

from utils.citation_check import (
    author_overlap,
    author_surnames,
    canonical_entry,
    compare_metadata,
    doi_display,
    doi_is_valid,
    doi_normalize,
    doi_prefix,
    extract_year,
    format_reference,
    normalize_pages,
    normalize_title,
    title_similarity,
    venue_similarity,
    verdict_from_findings,
)


class NormalizationTests(unittest.TestCase):
    def test_normalize_title_strips_punctuation_and_latex(self):
        self.assertEqual(normalize_title("U-Net: Convolutional Networks!"), "u net convolutional networks")
        self.assertEqual(normalize_title("\\textbf{EEG} denoising"), "eeg denoising")

    def test_title_similarity_bounds(self):
        self.assertEqual(title_similarity("EEG denoising", "eeg denoising"), 1.0)
        self.assertEqual(title_similarity("", "eeg denoising"), 0.0)
        self.assertGreater(title_similarity("U-Net for EEG", "U-Net for EEG denoising"), 0.8)
        self.assertLess(title_similarity("EEG denoising", "Quantum chromodynamics"), 0.4)

    def test_venue_similarity(self):
        self.assertEqual(venue_similarity("IEEE TNSRE", "IEEE TNSRE"), 1.0)
        self.assertGreater(
            venue_similarity("IEEE Transactions on Neural Systems", "IEEE Transactions on Neural Systems"), 0.99
        )

    def test_author_surnames_and_overlap(self):
        self.assertEqual(author_surnames(["Olaf Ronneberger", "Philipp Fischer"]), {"ronneberger", "fischer"})
        self.assertEqual(author_surnames("Ronneberger, O.; Fischer, P."), {"ronneberger", "fischer"})
        self.assertEqual(author_surnames([{"family": "Dong", "given": "Y."}]), {"dong"})
        self.assertEqual(author_overlap(["Olaf Ronneberger"], ["Ronneberger, O."]), 1.0)
        self.assertEqual(author_overlap([], ["Ronneberger, O."]), 0.0)


class DoiTests(unittest.TestCase):
    def test_normalize_and_validate(self):
        self.assertEqual(doi_normalize("https://doi.org/10.1109/TNSRE.2023.3309815"), "10.1109/tnsre.2023.3309815")
        self.assertEqual(doi_normalize("doi: 10.1000/xyz123."), "10.1000/xyz123")
        self.assertTrue(doi_is_valid("10.1000/xyz123"))
        self.assertFalse(doi_is_valid("not-a-doi"))
        self.assertFalse(doi_is_valid(""))
        self.assertEqual(doi_prefix("10.1109/TNSRE.2023.3309815"), "10.1109")

    def test_display_keeps_case(self):
        self.assertEqual(doi_display("https://doi.org/10.1109/TNSRE.2023.3309815"), "10.1109/TNSRE.2023.3309815")

    def test_extract_year_variants(self):
        self.assertEqual(extract_year({"published": {"date-parts": [[2015, 5, 1]]}}), 2015)
        self.assertEqual(extract_year({"pubYear": "2001"}), 2001)
        self.assertEqual(extract_year({"publication_year": 2023}), 2023)
        self.assertIsNone(extract_year({"title": ["no year"]}))

    def test_normalize_pages(self):
        self.assertEqual(normalize_pages("234-241"), "234\u2013241")
        self.assertEqual(normalize_pages("234\u2013241"), "234\u2013241")


class CanonicalEntryTests(unittest.TestCase):
    def test_crossref_record(self):
        entry = canonical_entry(
            {
                "DOI": "10.1109/TNSRE.2023.3309815",
                "title": ["An Approach for EEG Denoising Based on Wasserstein GAN"],
                "author": [{"given": "Y.", "family": "Dong"}, {"given": "X.", "family": "Li"}],
                "container-title": ["IEEE TNSRE"],
                "volume": "31",
                "page": "3524-3535",
                "published": {"date-parts": [[2023, 8]]},
                "is-referenced-by-count": 21,
                "type": "journal-article",
            },
            "crossref",
        )
        self.assertEqual(entry["doi"], "10.1109/tnsre.2023.3309815")
        self.assertEqual(entry["doi_display"], "10.1109/TNSRE.2023.3309815")
        self.assertEqual(entry["authors"], ["Y. Dong", "X. Li"])
        self.assertEqual((entry["year"], entry["venue"], entry["cited_by"]), (2023, "IEEE TNSRE", 21))

    def test_openalex_record_uses_display_name(self):
        entry = canonical_entry(
            {
                "id": "https://openalex.org/W1",
                "display_name": "EEG artifact removal with deep learning",
                "publication_year": 2024,
                "doi": "https://doi.org/10.3390/s24010001",
                "authorships": [{"author": {"display_name": "Ann Lee"}}],
                "primary_location": {"source": {"display_name": "Sensors"}},
                "cited_by_count": 3,
            },
            "openalex",
        )
        self.assertEqual(entry["title"], "EEG artifact removal with deep learning")
        self.assertEqual(entry["doi"], "10.3390/s24010001")
        self.assertEqual(entry["authors"], ["Ann Lee"])
        self.assertEqual(entry["year"], 2024)

    def test_arxiv_id_is_extracted_from_source_id(self):
        entry = canonical_entry(
            {
                "id": "https://arxiv.org/abs/1706.03762v7",
                "title": "Attention is All you Need",
                "authors": ["Ashish Vaswani"],
                "published": "2017-06-12T00:00:00Z",
            },
            "arxiv",
        )
        self.assertEqual(entry["arxiv_id"], "1706.03762")
        self.assertEqual(entry["year"], 2017)


class CompareMetadataTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {
            "title": "An Approach for EEG Denoising Based on Wasserstein GAN",
            "authors": ["Y. Dong", "X. Li"],
            "year": 2023,
            "venue": "IEEE TNSRE",
            "volume": "31",
            "pages": "3524-3535",
            "doi": "10.1109/tnsre.2023.3309815",
        }
        self.evidence = dict(self.candidate)

    def test_identical_record_is_verified(self):
        findings = compare_metadata(self.candidate, self.evidence)
        self.assertEqual(findings, [])
        self.assertEqual(verdict_from_findings(findings), "verified")

    def test_title_mismatch_is_rejected(self):
        evidence = dict(self.evidence, title="A Completely Different Paper on Quantum EEG")
        findings = compare_metadata(self.candidate, evidence)
        self.assertEqual(verdict_from_findings(findings), "rejected")
        self.assertIn("title", {item["field"] for item in findings})

    def test_doi_mismatch_is_rejected(self):
        evidence = dict(self.evidence, doi="10.1000/other")
        findings = compare_metadata(self.candidate, evidence)
        self.assertEqual(verdict_from_findings(findings), "rejected")

    def test_year_off_by_one_is_warning(self):
        evidence = dict(self.evidence, year=2024)
        findings = compare_metadata(self.candidate, evidence)
        self.assertEqual(verdict_from_findings(findings), "unverified")
        self.assertEqual([item["field"] for item in findings], ["year"])

    def test_year_beyond_tolerance_is_rejected(self):
        evidence = dict(self.evidence, year=2019)
        findings = compare_metadata(self.candidate, evidence)
        self.assertEqual(verdict_from_findings(findings), "rejected")

    def test_missing_field_on_candidate_is_warning(self):
        candidate = dict(self.candidate)
        candidate.pop("volume")
        findings = compare_metadata(candidate, self.evidence)
        self.assertEqual(verdict_from_findings(findings), "unverified")
        self.assertEqual([item["field"] for item in findings], ["volume"])

    def test_venue_abbreviation_is_warning_not_rejection(self):
        candidate = {
            "title": "An Approach for EEG Denoising",
            "authors": ["Y. Dong"],
            "year": 2023,
            "venue": "IEEE TNSRE",
        }
        evidence = dict(candidate, venue="IEEE Transactions on Neural Systems and Rehabilitation Engineering")
        findings = compare_metadata(candidate, evidence)
        self.assertEqual(verdict_from_findings(findings), "unverified")
        self.assertEqual([item["field"] for item in findings], ["venue"])

    def test_unrelated_venue_is_rejected(self):
        candidate = {
            "title": "An Approach for EEG Denoising",
            "authors": ["Y. Dong"],
            "year": 2023,
            "venue": "Nature Neuroscience",
        }
        evidence = dict(candidate, venue="IEEE Transactions on Neural Systems and Rehabilitation Engineering")
        findings = compare_metadata(candidate, evidence)
        self.assertEqual(verdict_from_findings(findings), "rejected")

    def test_reject_doi_prefix(self):
        candidate = dict(self.candidate, doi="10.9999/fake.2025.0001")
        findings = compare_metadata(candidate, dict(self.evidence, doi="10.9999/fake.2025.0001"), reject_doi_prefixes=["10.9999"])
        self.assertEqual(verdict_from_findings(findings), "rejected")


class FormatReferenceTests(unittest.TestCase):
    def setUp(self):
        self.entry = {
            "title": "An Approach for EEG Denoising Based on Wasserstein GAN",
            "authors": ["Y. Dong", "X. Li"],
            "year": 2023,
            "venue": "IEEE TNSRE",
            "volume": "31",
            "issue": "9",
            "pages": "3524-3535",
            "doi": "10.1109/TNSRE.2023.3309815",
            "doi_display": "10.1109/TNSRE.2023.3309815",
        }

    def test_ieee(self):
        line = format_reference(self.entry, "ieee", 5)
        self.assertTrue(line.startswith('[5] Y. Dong and X. Li, "An Approach'))
        self.assertIn("vol. 31", line)
        self.assertIn("pp. 3524\u20133535", line)
        self.assertIn("doi: 10.1109/TNSRE.2023.3309815", line)

    def test_apa(self):
        line = format_reference(self.entry, "apa")
        self.assertIn("(2023).", line)
        self.assertIn("https://doi.org/10.1109/TNSRE.2023.3309815", line)

    def test_gbt7714(self):
        line = format_reference(self.entry, "gbt7714", 3)
        self.assertTrue(line.startswith("[3] Y. DONG; X. LI"))
        self.assertIn("[J]", line)

    def test_bibtex(self):
        line = format_reference(self.entry, "bibtex")
        self.assertTrue(line.startswith("@article{"))
        self.assertIn("doi = {10.1109/TNSRE.2023.3309815}", line)

    def test_missing_fields_do_not_crash(self):
        self.assertIn("Untitled", format_reference({"title": "Untitled"}, "ieee", 1))


if __name__ == "__main__":
    unittest.main()
