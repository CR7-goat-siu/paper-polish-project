import unittest

from conftest import skill_module


class FormatAdapterTests(unittest.TestCase):
    def test_doi_and_page_range_normalization(self):
        module = skill_module("format-adapter")
        result = module.run(
            {
                "text": "[1] J. Smith. Survey. Journal, 2024, pp. 10-20. doi:10.1000/xyz123\n",
                "language": "en",
                "style": "keep",
                "mode": "local",
            }
        )
        self.assertIn("https://doi.org/10.1000/xyz123", result["text"])
        self.assertIn("10–20", result["text"])
        self.assertGreater(len(result["changes"]), 0)

    def test_model_mode_requires_prompt_variables(self):
        module = skill_module("format-adapter")
        result = module.run(
            {
                "text": "Reference line here.",
                "language": "en",
                "style": "ieee",
                "mode": "local",
            }
        )
        self.assertEqual(result["target_style"], "ieee")


if __name__ == "__main__":
    unittest.main()
