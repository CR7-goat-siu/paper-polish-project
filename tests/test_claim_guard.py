import unittest

from conftest import skill_module


class ClaimGuardTests(unittest.TestCase):
    def test_english_hype_detection(self):
        module = skill_module("claim-guard")
        report = module.check_text_local(
            "Our method is a groundbreaking and massive improvement, a true miracle.", "en"
        )
        found = {f["found"].lower() for f in report["findings"]}
        self.assertIn("groundbreaking", found)
        self.assertIn("massive", found)
        errors = [f for f in report["findings"] if f["severity"] == "error"]
        self.assertGreaterEqual(len(errors), 2)

    def test_english_unanchored_significant_escalates(self):
        module = skill_module("claim-guard")
        anchored = module.check_text_local(
            "Accuracy improved significantly, reaching 94.2% on the benchmark table.", "en"
        )
        unanchored = module.check_text_local(
            "Accuracy improved significantly across the evaluation.", "en"
        )
        def has_error(report):
            return any(f["severity"] == "error" for f in report["findings"])
        self.assertFalse(has_error(anchored))
        self.assertTrue(has_error(unanchored))

    def test_english_state_of_the_art_requires_citation_or_number(self):
        module = skill_module("claim-guard")
        report = module.check_text_local(
            "Our approach achieves state-of-the-art performance in all tests.", "en"
        )
        ids = {f["id"] for f in report["findings"]}
        self.assertIn("state-of-the-art", ids)

    def test_chinese_hype_detection(self):
        module = skill_module("claim-guard")
        report = module.check_text_local(
            "本文方法效果空前，具有国际领先水平，完美解决了所有难题。", "cn"
        )
        found = {f["found"] for f in report["findings"]}
        self.assertIn("空前", found)
        self.assertIn("国际领先", found)
        self.assertGreaterEqual(report["summary"]["error"], 2)

    def test_chinese_dafu_unanchored(self):
        module = skill_module("claim-guard")
        report = module.check_text_local("本方法大幅优于基线方法。", "cn")
        self.assertTrue(any(f["severity"] == "error" for f in report["findings"]))

    def test_informal_register(self):
        module = skill_module("claim-guard")
        report = module.check_text_local(
            "We can see from Table 1 that it's a lot of better, due to the fact that tuning helps.", "en"
        )
        found = {f["found"].strip().lower() for f in report["findings"]}
        self.assertIn("we can see from", found)
        self.assertIn("it's", found)
        self.assertIn("a lot of", found)

    def test_clean_text_passes(self):
        module = skill_module("claim-guard")
        report = module.check_text_local(
            "Table 1 shows that the proposed method improves accuracy by 3.2% over the baseline.", "en"
        )
        self.assertEqual(report["summary"]["error"], 0)

    def test_occurrences_grouped(self):
        module = skill_module("claim-guard")
        report = module.check_text_local(
            "This is unprecedented. The result is unprecedented too.", "en"
        )
        match = [f for f in report["findings"] if f["found"].lower() == "unprecedented"]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["occurrences"], 2)


    def test_lexicon_whitelist_escape_hatch(self):
        import unittest.mock

        module = skill_module("claim-guard")
        text = "本方法大幅优于基线方法。"
        baseline = module.check_text_local(text, "cn")
        self.assertTrue(any(f["severity"] == "error" for f in baseline["findings"]))
        with unittest.mock.patch(
            "utils.config_loader.load_rules_config",
            return_value={"objectivity": {"lexicon_whitelist": ["dafu"]}},
        ):
            whitelisted = module.check_text_local(text, "cn")
        self.assertFalse(any(f["severity"] == "error" for f in whitelisted["findings"]))


if __name__ == "__main__":
    unittest.main()
