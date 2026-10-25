"""统计内核 oracle 对拍（S9）：装有 scipy 的环境自动与 scipy.stats 互验。

与 PyYAML parity 同模式：运行时零依赖红线不破（scipy 仅测试期可选），
但只要有 oracle 在场就必须对拍，消除"数学内核只被自己验证"的双重标准。
"""

from __future__ import annotations

import math
import unittest

from utils.stat_checks import (
    chi2_pvalue,
    f_pvalue,
    gamma_q,
    r_pvalue,
    reg_inc_beta,
    t_pvalue,
    z_pvalue,
)

try:
    from scipy import stats as _sps  # noqa: F401

    HAS_SCIPY = True
except Exception:
    HAS_SCIPY = False


@unittest.skipUnless(HAS_SCIPY, "环境装有 scipy 时对拍（运行时零依赖红线不受影响）")
class StatOracleTests(unittest.TestCase):
    T_GRID = [-4.5, -2.3, -1.0, -0.4, 0.0, 0.7, 1.5, 2.05, 2.8, 4.2, 7.5]
    DF_GRID = [1, 2, 3, 5, 8, 13, 21, 30, 60, 120]

    def test_t_two_tailed_grid(self):
        worst = 0.0
        for df in self.DF_GRID:
            for t in self.T_GRID:
                ours = t_pvalue(t, df)
                ref = 2.0 * _sps.t.sf(abs(t), df)
                worst = max(worst, abs(ours - ref))
        self.assertLess(worst, 1e-9, f"t 分布双尾最大偏差 {worst:.2e}")

    def test_t_one_tailed_positive_and_negative(self):
        for df in (2, 10, 28):
            for t in (-3.2, -1.4, 0.9, 2.4, 5.0):
                ours = t_pvalue(t, df, two_tailed=False)
                ref = _sps.t.sf(t, df)
                self.assertLess(abs(ours - ref), 1e-9, f"t={t}, df={df}")

    def test_f_grid(self):
        worst = 0.0
        for df1 in (1, 2, 3, 5, 10):
            for df2 in (1, 2, 5, 10, 30, 100):
                for f in (0.1, 0.5, 1.0, 2.5, 5.0, 12.0):
                    ours = f_pvalue(f, df1, df2)
                    ref = _sps.f.sf(f, df1, df2)
                    worst = max(worst, abs(ours - ref))
        self.assertLess(worst, 1e-9, f"F 分布最大偏差 {worst:.2e}")

    def test_chi2_grid(self):
        worst = 0.0
        for df in (1, 2, 3, 5, 10, 20, 50):
            for x in (0.05, 0.5, 1.0, 3.0, 7.5, 20.0, 80.0):
                ours = chi2_pvalue(x, df)
                ref = _sps.chi2.sf(x, df)
                worst = max(worst, abs(ours - ref))
        self.assertLess(worst, 1e-9, f"χ² 分布最大偏差 {worst:.2e}")

    def test_z_two_tailed(self):
        for z in (-4.0, -2.575829, -1.959964, 0.0, 1.0, 3.0, 6.0):
            ours = z_pvalue(z)
            ref = 2.0 * _sps.norm.sf(abs(z))
            self.assertLess(abs(ours - ref), 1e-12, f"z={z}")

    def test_r_via_t_equivalence(self):
        for df in (5, 18, 38, 100):
            for r in (-0.95, -0.6, -0.31, 0.0, 0.42, 0.8, 0.99):
                t = r * math.sqrt(df / (1.0 - r * r))
                ours = r_pvalue(r, df)
                ref = 2.0 * _sps.t.sf(abs(t), df)
                self.assertLess(abs(ours - ref), 1e-9, f"r={r}, df={df}")

    def test_reg_inc_beta_grid(self):
        worst = 0.0
        for a in (0.5, 1.0, 2.5, 10.0, 30.0):
            for b in (0.5, 1.0, 3.0, 15.0):
                for x in (0.01, 0.1, 0.35, 0.5, 0.77, 0.95, 0.999):
                    ours = reg_inc_beta(a, b, x)
                    ref = _sps.beta.cdf(x, a, b)
                    worst = max(worst, abs(ours - ref))
        self.assertLess(worst, 1e-9, f"I_x(a,b) 最大偏差 {worst:.2e}")

    def test_gamma_q_grid(self):
        worst = 0.0
        for a in (0.5, 1.0, 2.0, 7.5, 25.0):
            for x in (0.05, 0.5, 2.0, 8.0, 30.0, 120.0):
                ours = gamma_q(a, x)
                ref = _sps.gamma.sf(x, a)
                worst = max(worst, abs(ours - ref))
        self.assertLess(worst, 1e-9, f"Q(a,x) 最大偏差 {worst:.2e}")


if __name__ == "__main__":
    unittest.main()
