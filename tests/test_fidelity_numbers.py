import unittest

from conftest import skill_module
from utils.fidelity import numbers_preserved


class FidelityNumberTests(unittest.TestCase):
    """P1 回归：区间与科学计数法不得误报为数字丢失。"""

    def test_range_rewritten_as_words(self):
        result = numbers_preserved("The band is 10-20 Hz wide.", "The band is 10 to 20 Hz wide.")
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["added"], [])

    def test_scientific_notation_vs_decimal(self):
        result = numbers_preserved("The effect is significant (p < 1e-5).", "The effect is significant (p < 0.00001).")
        self.assertEqual(result["missing"], [])

    def test_hardware_and_doi_tokens(self):
        source = "Intel i7-12700 CPU; doi 10.1088/1741-2560/3/4/011; ISBN 978-3-319."
        polished = "Intel i7-12700 CPU; doi 10.1088/1741-2560/3/4/011; ISBN 978-3-319."
        self.assertEqual(numbers_preserved(source, polished)["missing"], [])

    def test_percent_formatting_difference(self):
        result = numbers_preserved("SNR 提升 18.90%", "SNR 提升 18.9%")
        self.assertEqual(result["missing"], [])

    def test_real_loss_still_detected(self):
        result = numbers_preserved("Accuracy reaches 91.2% and 88.5% overall.", "Accuracy reaches 88.5% overall.")
        self.assertEqual(result["missing"], ["91.2%"])

    def test_range_endpoint_loss_detected(self):
        result = numbers_preserved("The band is 10-20 Hz wide.", "The band is 10-15 Hz wide.")
        self.assertIn("20", result["missing"])

    def test_sign_after_digit_space_is_range_not_negative(self):
        result = numbers_preserved("values 10 -20 Hz are used.", "values 10 to 20 Hz are used.")
        self.assertEqual(result["missing"], [])

    def test_real_negative_after_cjk_still_matched(self):
        result = numbers_preserved("温度为 -20 度时性能稳定。", "温度为 -20 度时性能保持稳定。")
        self.assertEqual(result["missing"], [])

    def test_numbers_after_comma_survive(self):
        from utils.fidelity import extract_numbers

        tokens = extract_numbers("values were 3, 4.5 and 6 dB")
        self.assertIn("4.5", tokens)
        result = numbers_preserved(
            "We used 91.2% and 88.5% accuracy.",
            "We used 91.2%, 88.5% accuracy.",
        )
        self.assertEqual(result["missing"], [])

    def test_negative_sign_tamper_detected(self):
        result = numbers_preserved(
            "coefficients -0.12, -0.34 hold across runs.",
            "coefficients -0.12, 0.34 hold across runs.",
        )
        self.assertEqual(result["missing"], ["-0.34"])

    def test_review_gate_accepts_rewritten_range(self):
        module = skill_module("review-gate")
        result = module.evaluate(
            "The filter passes 10-20 Hz and significance holds at p < 1e-5 [1].",
            "The filter passes 10 to 20 Hz, and significance holds at p < 0.00001 [1].",
            language="en",
            strength="deep",
        )
        self.assertEqual(result["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
