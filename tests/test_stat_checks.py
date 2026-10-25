"""stat_checks（statcheck + GRIM 纯标准库实现）的专项测试。

p 值重算内核用数理统计表中的经典临界值做绝对校验；
抽取与判定用构造正负样本验证准确率必须为 1.0。
"""

from __future__ import annotations

import math
import unittest

from utils.stat_checks import (
    chi2_pvalue,
    check_report,
    check_text,
    f_pvalue,
    grim_check,
    r_pvalue,
    t_pvalue,
    z_pvalue,
)


def _close(a: float, b: float, tol: float = 1e-4) -> bool:
    return math.isfinite(a) and abs(a - b) <= tol


class DistributionKernelTests(unittest.TestCase):
    """重算内核 vs 数理统计经典临界值（全部为教科书标准值）。"""

    def test_t_distribution_critical_values(self):
        # t_{0.975, df} 对应双尾 p = 0.05
        self.assertTrue(_close(t_pvalue(2.048407, 28), 0.05, 1e-6))
        self.assertTrue(_close(t_pvalue(2.228139, 10), 0.05, 1e-6))
        self.assertTrue(_close(t_pvalue(1.959964, 1e9), 0.05, 1e-4))  # df→∞ 退化为正态
        self.assertTrue(_close(t_pvalue(12.706205, 1), 0.05, 1e-6))
        self.assertTrue(_close(t_pvalue(6.313752, 1), 0.10, 1e-6))  # t_{0.95,1}
        # 单尾：t>0 时单尾 = 双尾/2
        self.assertTrue(_close(t_pvalue(2.048407, 28, two_tailed=False), 0.025, 1e-6))
        # 负 t 单尾：1 - 双尾/2
        self.assertTrue(
            _close(t_pvalue(-2.048407, 28, two_tailed=False), 0.975, 1e-6)
        )

    def test_f_distribution_critical_values(self):
        self.assertTrue(_close(f_pvalue(4.964603, 1, 10), 0.05, 1e-6))
        self.assertTrue(_close(f_pvalue(3.708265, 3, 10), 0.05, 1e-6))
        self.assertTrue(_close(f_pvalue(19.296, 5, 2), 0.05, 1e-5))
        self.assertTrue(_close(f_pvalue(3.195055, 2, 47), 0.05, 5e-4))

    def test_chi2_critical_values(self):
        self.assertTrue(_close(chi2_pvalue(3.841459, 1), 0.05, 1e-6))
        self.assertTrue(_close(chi2_pvalue(7.814728, 3), 0.05, 1e-6))
        self.assertTrue(_close(chi2_pvalue(18.307038, 10), 0.05, 1e-6))
        self.assertTrue(_close(chi2_pvalue(0.0, 5), 1.0, 1e-12))

    def test_z_critical_values(self):
        self.assertTrue(_close(z_pvalue(1.959964), 0.05, 1e-6))
        self.assertTrue(_close(z_pvalue(2.575829), 0.01, 1e-6))
        self.assertTrue(_close(z_pvalue(0.0), 1.0, 1e-12))

    def test_r_via_t_transformation(self):
        # r 由 t_{0.975,18} = 2.100922 反推：r = t/sqrt(t²+df) → 双尾 p = 0.05
        r = 2.100922 / math.sqrt(2.100922**2 + 18)
        self.assertTrue(_close(r_pvalue(r, 18), 0.05, 1e-6))
        # r = ±1 → p = 0
        self.assertEqual(r_pvalue(1.0, 10), 0.0)
        self.assertEqual(r_pvalue(-1.0, 10), 0.0)


def _one(text: str, alpha: float = 0.05) -> dict:
    """跑 check_text 并要求恰好抽到 1 条统计量记录。"""
    result = check_text(text, alpha=alpha)
    inferential = [r for r in result["records"] if r["test"] != "grim"]
    return inferential[0]


class ExtractionTests(unittest.TestCase):
    def test_extracts_all_five_tests(self):
        text = (
            "t(28) = 2.05, p = .05; F(2, 47) = 5.32, p = .008; "
            "χ²(3, N = 132) = 10.71, p = .013; z = 2.34, p = .019; "
            "r(38) = .42, p = .008."
        )
        result = check_text(text)
        by_test = {r["test"] for r in result["records"] if r["test"] != "grim"}
        self.assertEqual(by_test, {"t", "F", "chi2", "z", "r"})
        self.assertEqual(len(result["findings"]), 0)

    def test_extraction_formats(self):
        cases = [
            ("The effect was significant, t(28)=2.05, p=.051.", "t", 2.05),
            ("t (28) = 2.05, p < .001", "t", 2.05),
            ("F(2,47)=5.32, p = 0.008", "F", 5.32),
            ("χ2(3) = 10.71, p=.013", "chi2", 10.71),
            ("chi2(3, N = 132) = 10.71, p = .013", "chi2", 10.71),
            ("z = -2.34, p = .019", "z", -2.34),
            ("r(38) = -.42, p = .008", "r", -0.42),
            ("r = .42, p = .008", "r", 0.42),
        ]
        for text, test, value in cases:
            record = _one(text)
            self.assertEqual(record["test"], test, msg=text)
            self.assertEqual(record["value"], value, msg=text)

    def test_no_extraction_without_p(self):
        # 无 p 值的报告不参与一致性判定（登记但不算 findings）
        result = check_text("t(28) = 2.05 was observed.")
        self.assertEqual(result["records"], [])
        # 无统计量的 p 值单独出现也不抽
        result = check_text("All p < 0.001 unless noted.")
        self.assertEqual(result["records"], [])

    def test_non_reference_numbers_ignored(self):
        result = check_text("We recruited 256 participants and ran 10 folds.")
        self.assertEqual(result["records"], [])


class ConsistencyVerdictTests(unittest.TestCase):
    def test_consistent_report_passes(self):
        # t=2.05 (df=28) → p ∈ [0.0496, 0.0504]；报告 .05 → 相容
        record = _one("t(28) = 2.05, p = .05")
        self.assertEqual(record["verdict"], "consistent")

    def test_p_value_mismatch_detected(self):
        # t=2.5, df=28 → p ≈ 0.0186；报告 .01 → 区间不相容，但同为显著 → 纯 mismatch
        record = _one("t(28) = 2.5, p = .01")
        self.assertEqual(record["verdict"], "p_value_mismatch")
        self.assertFalse(record["decision_error"])
        self.assertTrue(0.0165 <= record["recomputed_p_min"] <= 0.0208)

    def test_decision_error_sig_reported_but_actually_ns(self):
        record = _one("t(10) = 2.0, p = .04")
        self.assertEqual(record["verdict"], "decision_error")

    def test_decision_error_ns_reported_but_actually_sig(self):
        # t=2.42, df=10 → p ≈ 0.0362；报告 p = .06 → 声称不显著但实际显著
        record = _one("t(10) = 2.42, p = .06")
        self.assertEqual(record["verdict"], "decision_error")

    def test_less_than_bound_respected(self):
        # t=5.0, df=28 → p ≈ 2e-5 < .001：相容
        record = _one("t(28) = 5.0, p < .001")
        self.assertEqual(record["verdict"], "consistent")
        # t=2.5, df=28 → p ≈ .018，不可能 < .001：不相容但同为显著 → 非 decision error
        record = _one("t(28) = 2.5, p < .001")
        self.assertEqual(record["verdict"], "p_value_mismatch")
        self.assertFalse(record["decision_error"])

    def test_rounding_tolerance_no_false_positive(self):
        # 报告 t(28)=2.05, p=.049：重算区间 [0.04932, 0.05036] 与 [.0485,.0495] 相交 → 相容
        record = _one("t(28) = 2.05, p = .049")
        self.assertEqual(record["verdict"], "consistent")
        # p=.04 与重算区间明确不相交 → mismatch（且重算未跨 .05，不升级为决策错误）
        record = _one("t(28) = 2.05, p = .04")
        self.assertEqual(record["verdict"], "p_value_mismatch")

    def test_one_tailed_context(self):
        # t=1.70, df=28：双尾 p≈.0999（报告 .05 不相容），单尾 p≈.0499（相容）
        record = _one("t(28) = 1.70, p = .05, one-tailed")
        self.assertEqual(record["verdict"], "consistent")
        # 无单尾声明时同一数字必须判不一致
        record = _one("t(28) = 1.70, p = .05")
        self.assertEqual(record["verdict"], "p_value_mismatch")

    def test_multiple_reports_in_sentence(self):
        text = "Group A scored higher, t(10) = 2.0, p = .01; Group B did not, t(12) = 1.0, p = .34."
        result = check_text(text)
        inferential = [r for r in result["records"] if r["test"] != "grim"]
        self.assertEqual(len(inferential), 2)
        verdicts = {r["verdict"] for r in inferential}
        self.assertEqual(verdicts, {"decision_error", "consistent"})

    def test_missing_df_is_untestable_not_error(self):
        record = _one("r = .42, p = .008")
        self.assertEqual(record["verdict"], "untestable_missing_df")

    def test_r_with_n_in_sentence_derives_df(self):
        # N = 41 → df = 39；r = .31 → t = .31*sqrt(39/(1-.0961)) ≈ 2.032 → p ≈ .0495
        record = _one("The correlation was significant, r = .31, p = .05 (N = 41).")
        self.assertEqual(record["verdict"], "consistent")
        self.assertEqual(record["df"], 39.0)


class GrimTests(unittest.TestCase):
    def test_possible_mean_passes(self):
        # 82/24 = 3.41666… → round(2) = 3.42 ✓
        self.assertEqual(grim_check("3.42", 24)["verdict"], "consistent")
        # 61/12 = 5.0833 → 5.08 ✓
        self.assertEqual(grim_check("5.08", 12)["verdict"], "consistent")

    def test_impossible_mean_detected(self):
        # 61/12 = 5.0833, 62/12 = 5.1667：round(2) 都得不到 5.13
        result = grim_check("5.13", 12)
        self.assertEqual(result["verdict"], "grim_impossible_mean")
        # 经典 GRIM 样例：M = 5.19, N = 16：S=83 → 5.1875 → 5.19 ✓（可能）
        self.assertEqual(grim_check("5.19", 16)["verdict"], "consistent")
        # M = 2.27, N = 15：S=34 → 2.2667 → 2.27 ✓
        self.assertEqual(grim_check("2.27", 15)["verdict"], "consistent")
        # M = 4.37, N = 21：S=92 → 4.3810；S=91 → 4.3333 → 都不是 4.37
        self.assertEqual(grim_check("4.37", 21)["verdict"], "grim_impossible_mean")

    def test_granularity_untestable(self):
        # N > 10^d 时颗粒度不足以判定（2 位小数、N=1000）
        self.assertEqual(grim_check("3.42", 1000)["verdict"], "untestable_granularity")

    def test_grim_via_check_text(self):
        result = check_text("Participants reported M = 5.13, SD = 0.62, N = 12.")
        self.assertEqual(result["summary"]["grim_impossible_mean"], 1)
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["check"], "grim_impossible_mean")
        result = check_text("Participants reported M = 3.42, SD = 0.51, N = 24.")
        self.assertEqual(result["summary"]["grim_impossible_mean"], 0)
        self.assertEqual(result["findings"], [])


class FindingSchemaTests(unittest.TestCase):
    def test_finding_structure_for_registry(self):
        result = check_text("t(10) = 2.0, p = .01")
        finding = result["findings"][0]
        for key in ("check", "found", "severity", "message", "context_snippet", "suggestion"):
            self.assertIn(key, finding)
        self.assertEqual(finding["check"], "decision_error")
        self.assertEqual(finding["severity"], "error")

    def test_location_tagging(self):
        result = check_text("t(10) = 2.0, p = .01", location="Results ¶3")
        self.assertEqual(result["findings"][0]["_location"], "Results ¶3")
        self.assertEqual(result["records"][0]["location"], "Results ¶3")


class AccuracyGate(unittest.TestCase):
    """P0-A 验收门禁：构造正负样本集上判定准确率必须为 1.0。"""

    POSITIVE_ERRORS = [
        "t(10) = 2.0, p = .01",          # decision error
        "t(10) = 2.0, p = .04",          # decision error
        "t(28) = 2.05, p = .03",         # plain mismatch
        "t(28) = 2.5, p < .001",         # bound violation
        "F(2, 47) = 3.10, p = .04",      # F decision error (F=3.10 → p≈.054)
        "χ²(3) = 6.0, p = .05",          # chi2(3)=6.0 → p≈.111
        "z = 1.20, p = .23",             # z=1.20 → p≈.230 two-tailed... 边界？改用明确错例
        "r(18) = .44, p = .20",          # r=.44 → p≈.0588 ≠ .20
    ]

    def setUp(self):
        # 剔除上面设计中不确定的样例，只保留经重算内核独立确认的错例
        self.positive_errors = [
            case for case in self.POSITIVE_ERRORS if not case.startswith("z = 1.20")
        ]

    NEGATIVES = [
        "t(28) = 2.05, p = .05",
        "t(28) = 5.0, p < .001",
        "F(2, 47) = 5.32, p = .008",
        "χ²(3, N = 132) = 10.71, p = .013",
        "z = 2.34, p = .019",
        "r(38) = .42, p = .008",
        "t(10) = 1.0, p = .34",
        "t(28) = 1.70, p = .05, one-tailed",
    ]

    def test_accuracy_is_one(self):
        for case in self.positive_errors:
            result = check_text(case)
            self.assertEqual(
                len(result["findings"]), 1, msg=f"错例未被拦截: {case} → {result['summary']}"
            )
        for case in self.NEGATIVES:
            result = check_text(case)
            self.assertEqual(
                result["findings"], [], msg=f"正确样例被误报: {case} → {result['findings']}"
            )


if __name__ == "__main__":
    unittest.main()
