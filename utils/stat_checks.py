"""统计报告一致性核查：statcheck + GRIM 的纯标准库实现。

对应能力（对标 Nuijten et al. 的 statcheck 与 GRIM 检验）：
- 从正文抽取 APA 风格检验统计量报告：t / F / χ² / Z / r（含自由度、报告 p 值、单双尾）；
- 用不完全 Beta 函数（Lentz 连分数）与不完全 Gamma 函数确定性重算 p 值，
  零第三方科学计算库（只用 math / fractions）；
- 统计量与报告 p 值双方都按报告精度构造四舍五入区间，区间不相交才判不一致，
  并单独识别"决策不一致"（报告显著但重算必然不显著，反之亦然）；
- GRIM：小样本均值必须在颗粒度上可能——存在整数总和 S 使 round(S/N, d) 等于报告均值
  （Fraction 精确有理数运算）。

设计约束（遵循 AGENTS.md）：
- 纯标准库、纯函数、零网络、零模型；所有阈值可由调用方传入；
- 这是确定性检查：能用数学判定的问题绝不交给 LLM 猜测。
"""

from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Any

# --------------------------------------------------------------------------- #
# 数值内核：正则化不完全 Beta / 不完全 Gamma
# --------------------------------------------------------------------------- #

_TINY = 1e-300
_EPS = 3e-16


def _betacf(a: float, b: float, x: float, *, max_iter: int = 400) -> float:
    """不完全 Beta 函数的 Lentz 连分数展开（Numerical Recipes 标准实现）。"""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _TINY:
        d = _TINY
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def reg_inc_beta(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数 I_x(a, b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    if a <= 0.0 or b <= 0.0:
        return float("nan")
    log_front = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _gamma_p_series(a: float, x: float, *, max_iter: int = 600) -> float:
    """正则化下不完全 Gamma P(a, x) 的级数展开（x < a + 1 时收敛）。"""
    total = 1.0 / a
    delta = total
    ap = a
    for _ in range(max_iter):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * _EPS:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gamma_q_cf(a: float, x: float, *, max_iter: int = 600) -> float:
    """正则化上不完全 Gamma Q(a, x) 的 Lentz 连分数展开（x >= a + 1 时收敛）。"""
    b = x + 1.0 - a
    c = 1.0 / _TINY
    d = 1.0 / b if abs(b) > _TINY else 1.0 / _TINY
    h = d
    for i in range(1, max_iter + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < _TINY:
            d = _TINY
        c = b + an / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def gamma_q(a: float, x: float) -> float:
    """正则化上不完全 Gamma 函数 Q(a, x) = Γ(a, x) / Γ(a)。"""
    if x <= 0.0:
        return 1.0
    if a <= 0.0:
        return float("nan")
    if x < a + 1.0:
        return 1.0 - _gamma_p_series(a, x)
    return _gamma_q_cf(a, x)


# --------------------------------------------------------------------------- #
# 分布 p 值
# --------------------------------------------------------------------------- #


def t_pvalue(t: float, df: float, *, two_tailed: bool = True) -> float:
    """t 分布的 p 值（双尾默认）。t=0 → 1，|t|→∞ → 0。"""
    if df <= 0:
        return float("nan")
    x = df / (df + t * t)
    p_two = reg_inc_beta(df / 2.0, 0.5, x)
    if two_tailed:
        return p_two
    return p_two / 2.0 if t >= 0 else 1.0 - p_two / 2.0


def f_pvalue(f_value: float, df1: float, df2: float) -> float:
    """F 分布右尾 p 值：p = I_x(df2/2, df1/2)，x = df2 / (df2 + df1·F)。"""
    if df1 <= 0 or df2 <= 0 or f_value < 0:
        return float("nan")
    if f_value == 0.0:
        return 1.0
    x = df2 / (df2 + df1 * f_value)
    return reg_inc_beta(df2 / 2.0, df1 / 2.0, x)


def chi2_pvalue(x: float, df: float) -> float:
    """χ² 分布右尾 p 值：p = Q(df/2, x/2)。"""
    if df <= 0 or x < 0:
        return float("nan")
    return gamma_q(df / 2.0, x / 2.0)


def z_pvalue(z: float, *, two_tailed: bool = True) -> float:
    """标准正态 p 值（erfc 实现）。"""
    p_two = math.erfc(abs(z) / math.sqrt(2.0))
    if two_tailed:
        return p_two
    return p_two / 2.0 if z >= 0 else 1.0 - p_two / 2.0


def r_pvalue(r: float, df: float, *, two_tailed: bool = True) -> float:
    """Pearson 相关系数检验：转化为 t = r·sqrt(df/(1-r²)) 后复用 t 分布。"""
    if df <= 0:
        return float("nan")
    if abs(r) >= 1.0:
        return 0.0 if abs(r) == 1.0 else float("nan")
    t = r * math.sqrt(df / (1.0 - r * r))
    return t_pvalue(t, df, two_tailed=two_tailed)


# --------------------------------------------------------------------------- #
# 报告抽取
# --------------------------------------------------------------------------- #

_NUM = r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)"
_P_OP = r"(?:=|＝|<|＜|>|＞|≤|≥)"
_P_TAIL = rf"[,;，；]?\s*p\s*(?:-?value\s*)?{_P_OP}\s*({_NUM})"

STAT_PATTERNS: dict[str, re.Pattern[str]] = {
    # t(28) = 2.05, p = .051
    "t": re.compile(rf"\bt\s*\(\s*({_NUM})\s*\)\s*=\s*({_NUM}){_P_TAIL}"),
    # F(2, 47) = 5.32, p = .008
    "F": re.compile(rf"\bF\s*\(\s*({_NUM})\s*[,，]\s*({_NUM})\s*\)\s*=\s*({_NUM}){_P_TAIL}"),
    # χ²(3, N = 132) = 10.71, p = .013 / chi2(3) = 10.71, p = .013
    "chi2": re.compile(
        rf"(?:χ\s*²|χ\s*2|Χ\s*²|Χ\s*2|chi\s*²|chi\s*2)\s*"
        rf"\(\s*({_NUM})\s*(?:[,，]\s*[Nn]\s*=\s*\d+\s*)?\)\s*=\s*({_NUM}){_P_TAIL}",
        re.IGNORECASE,
    ),
    # z = 2.34, p = .019
    "z": re.compile(rf"\b[zZ]\s*(?:\(\s*∞\s*\))?\s*=\s*({_NUM}){_P_TAIL}"),
    # r(38) = .42, p = .008 / r = .42, p = .008
    "r": re.compile(rf"\br\s*(?:\(\s*({_NUM})\s*\))?\s*=\s*({_NUM}){_P_TAIL}"),
}

ONE_TAILED_RE = re.compile(r"one[\s-]?tail(?:ed)?|directional|单尾", re.IGNORECASE)
SENTENCE_SPLIT_RE = re.compile(r"[^\n。．！？!?；;]+[。．！？!?；;]?")


def _sentences(text: str) -> list[str]:
    return [m.group().strip() for m in SENTENCE_SPLIT_RE.finditer(text or "") if m.group().strip()]

MEAN_RE = re.compile(
    rf"(?<![A-Za-z])(?:M|Mean|mean|均值|平均分?)\s*[:：=]\s*({_NUM})(?![\d%])"
)
SAMPLE_N_RE = re.compile(r"(?<![A-Za-z])[Nn]\s*[:：=]\s*(\d+)")


def _decimals(raw: str) -> int:
    """报告值字符串的小数位数（.051 → 3，2.05 → 2，5 → 0）。"""
    raw = str(raw).strip()
    if "." in raw:
        return len(raw.split(".", 1)[1])
    return 0


def _rounding_interval(value: float, decimals: int) -> tuple[float, float]:
    """四舍五入区间 [v - 0.5ulp, v + 0.5ulp]。"""
    ulp = 10.0 ** (-decimals) if decimals > 0 else 1.0
    return value - ulp / 2.0, value + ulp / 2.0


def _op_normalize(op: str) -> str:
    if op in {"<", "＜", "≤"}:
        return "<=" if op == "≤" else "<"
    if op in {">", "＞", "≥"}:
        return ">=" if op == "≥" else ">"
    return "="


def _p_interval_consistent(
    op: str, p_lo: float, p_hi: float, bound: float, decimals: int
) -> bool:
    """报告 p 值（含其自身的四舍五入区间 / 不等号）与重算区间是否相容。"""
    if op == "=":
        r_lo, r_hi = _rounding_interval(bound, decimals)
        return r_lo <= p_hi and p_lo <= r_hi
    if op == "<":
        return p_lo < bound
    if op == "<=":
        return p_lo <= bound
    if op == ">":
        return p_hi > bound
    return p_hi >= bound  # ">="


def _recompute_p(record: dict[str, Any], value: float, one_tailed: bool) -> float:
    test = record["test"]
    if test == "t":
        return t_pvalue(value, record["df"], two_tailed=not one_tailed)
    if test == "F":
        return f_pvalue(value, record["df1"], record["df2"])
    if test == "chi2":
        return chi2_pvalue(value, record["df"])
    if test == "z":
        return z_pvalue(value, two_tailed=not one_tailed)
    if test == "r":
        return r_pvalue(value, record["df"], two_tailed=not one_tailed)
    return float("nan")


def _scan_stats(text: str) -> list[dict[str, Any]]:
    """抽取全部检验统计量报告（不判定一致性）。"""
    records: list[dict[str, Any]] = []
    for sentence in _sentences(text):
        if not sentence.strip():
            continue
        one_tailed = bool(ONE_TAILED_RE.search(sentence))
        for test, pattern in STAT_PATTERNS.items():
            for match in pattern.finditer(sentence):
                op_match = re.search(rf"p\s*(?:-?value\s*)?({_P_OP})", match.group(0))
                op = _op_normalize(op_match.group(1)) if op_match else "="
                groups = match.groups()
                if test == "t":
                    value_raw = groups[1]
                    record = {"test": "t", "df": float(groups[0]), "value": float(groups[1])}
                elif test == "F":
                    value_raw = groups[2]
                    record = {
                        "test": "F",
                        "df1": float(groups[0]),
                        "df2": float(groups[1]),
                        "value": float(groups[2]),
                    }
                elif test == "chi2":
                    value_raw = groups[1]
                    record = {"test": "chi2", "df": float(groups[0]), "value": float(groups[1])}
                elif test == "z":
                    value_raw = groups[0]
                    record = {"test": "z", "value": float(groups[0])}
                else:  # r
                    value_raw = groups[1]
                    df: float | None = float(groups[0]) if groups[0] else None
                    if df is None:
                        n_match = SAMPLE_N_RE.search(sentence)
                        if n_match and int(n_match.group(1)) >= 3:
                            df = float(int(n_match.group(1)) - 2)
                    record = {"test": "r", "df": df, "value": float(groups[1])}
                record.update(
                    {
                        "reported_p": groups[-1],
                        "p_op": op,
                        "p_decimals": _decimals(groups[-1]),
                        "value_decimals": _decimals(value_raw),
                        "one_tailed_context": one_tailed,
                        "sentence": sentence.strip()[:240],
                        "reported": match.group(0).strip(),
                    }
                )
                records.append(record)
    return records


def check_report(record: dict[str, Any], *, alpha: float = 0.05) -> dict[str, Any]:
    """对单条统计报告做确定性重算与一致性判定（纯函数）。"""
    result = dict(record)
    result["alpha"] = alpha
    result["verdict"] = "consistent"
    result["decision_error"] = False
    value = result.get("value")
    df = result.get("df")
    if result["test"] in {"t", "r"} and (df is None or df <= 0):
        result["verdict"] = "untestable_missing_df"
        return result
    if result["test"] == "r" and abs(value) > 1.0:
        result["verdict"] = "untestable_invalid_value"
        return result
    if result["test"] in {"F", "chi2"} and value < 0:
        result["verdict"] = "untestable_invalid_value"
        return result

    p_decimals = int(result["p_decimals"])
    reported_p = float(result["reported_p"])
    op = result["p_op"]

    # 统计量按其报告精度构造区间，区间两端各重算一次 p，得到重算 p 的包络
    value_lo, value_hi = _rounding_interval(value, int(result["value_decimals"]))
    one_tailed = bool(result.get("one_tailed_context"))
    # 单尾声明时同时接受单尾与双尾口径（口径差异不应被误报为数值错误）
    tail_variants = (True, False) if one_tailed else (False,)
    candidates: list[float] = []
    for variant in tail_variants:
        for scenario_value in (value_lo, value_hi):
            p = _recompute_p(result, scenario_value, one_tailed=variant)
            if not math.isnan(p):
                candidates.append(p)
    if not candidates:
        result["verdict"] = "untestable_invalid_value"
        return result
    p_min, p_max = min(candidates), max(candidates)
    result["recomputed_p_min"] = p_min
    result["recomputed_p_max"] = p_max

    if not _p_interval_consistent(op, p_min, p_max, reported_p, p_decimals):
        result["verdict"] = "p_value_mismatch"

    # 决策一致性（gross inconsistency）：仅当报告值对 α 边界有明确主张时判定；
    # 重算区间跨越 α 时视为无法判定，不报（避免四舍五入假阳性）。
    if op == "=":
        reported_sig: bool | None = reported_p < alpha
    elif op in {"<", "<="}:
        reported_sig = True if reported_p <= alpha else None
    else:  # ">" / ">="
        reported_sig = False if reported_p >= alpha else None
    recomputed_sig = p_max < alpha
    recomputed_ns = p_min >= alpha
    if reported_sig is True and recomputed_ns:
        result["verdict"] = "decision_error"
        result["decision_error"] = True
    elif reported_sig is False and recomputed_sig:
        result["verdict"] = "decision_error"
        result["decision_error"] = True
    return result


def grim_check(mean_raw: str, n: int) -> dict[str, Any]:
    """GRIM 检验：报告均值在小样本整数总和脚下是否可能。

    存在整数 S 使得 S/N 按报告小数位四舍五入等于报告均值 → consistent。
    使用 Fraction 精确有理数运算，避免浮点误差；N > 10^d 时颗粒度不足以判定。
    """
    decimals = _decimals(mean_raw)
    mean = float(mean_raw)
    scaled = round(mean * (10**decimals))
    result: dict[str, Any] = {
        "test": "grim",
        "mean": mean_raw,
        "n": n,
        "decimals": decimals,
    }
    if n <= 0:
        result["verdict"] = "untestable_invalid_n"
        return result
    if decimals == 0:
        result["verdict"] = "consistent"
        return result
    if n > 10**decimals:
        result["verdict"] = "untestable_granularity"
        return result
    tolerance = Fraction(1, 2 * 10**decimals) + Fraction(1, 10**12)
    target = Fraction(scaled, 10**decimals)
    lo = max(0, math.floor(n * float(target - tolerance)) - 1)
    hi = math.ceil(n * float(target + tolerance)) + 1
    for s in range(lo, hi + 1):
        if abs(Fraction(s, n) - target) <= tolerance:
            result["verdict"] = "consistent"
            result["sum_candidate"] = s
            return result
    result["verdict"] = "grim_impossible_mean"
    return result


def _grim_records(text: str) -> list[dict[str, Any]]:
    """扫描 M/N 组合并做 GRIM 判定（句内就近配对）。"""
    records: list[dict[str, Any]] = []
    for sentence in _sentences(text):
        if not sentence.strip():
            continue
        means = list(MEAN_RE.finditer(sentence))
        ns = list(SAMPLE_N_RE.finditer(sentence))
        if not means or not ns:
            continue
        for mean_match in means:
            nearest = min(ns, key=lambda m: abs(m.start() - mean_match.end()))
            if abs(nearest.start() - mean_match.end()) > 120:
                continue
            n = int(nearest.group(1))
            result = grim_check(mean_match.group(1), n)
            result["sentence"] = sentence.strip()[:240]
            result["reported"] = f"M = {result.get('mean')}, N = {n}"
            records.append(result)
    return records


def check_text(
    text: str,
    *,
    alpha: float = 0.05,
    location: str = "",
) -> dict[str, Any]:
    """对全文跑统计一致性核查（统计量重算 + GRIM），返回 {records, findings, summary}。

    findings 采用 issue-registry 可直接归一的 finding 结构
    （check / found / severity / message / context_snippet / suggestion）。
    """
    records: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for record in _scan_stats(text):
        result = check_report(record, alpha=alpha)
        result["location"] = location
        records.append(result)
        if result["verdict"] in {"p_value_mismatch", "decision_error"}:
            findings.append(
                {
                    "check": result["verdict"],
                    "found": result.get("reported", ""),
                    "severity": "error",
                    "message": (
                        f"{result.get('reported', '')} 重算 p 值区间 "
                        f"[{result.get('recomputed_p_min', float('nan')):.4g}, "
                        f"{result.get('recomputed_p_max', float('nan')):.4g}] "
                        + (
                            "与报告 p 值不相容"
                            if result["verdict"] == "p_value_mismatch"
                            else f"与报告显著性判断矛盾（α={alpha:g}）"
                        )
                    ),
                    "context_snippet": result.get("sentence", ""),
                    "suggestion": "核对统计量、自由度、单双尾口径与 p 值的对应关系",
                    "_location": location,
                }
            )
    for record in _grim_records(text):
        record["location"] = location
        records.append(record)
        if record["verdict"] == "grim_impossible_mean":
            findings.append(
                {
                    "check": "grim_impossible_mean",
                    "found": record["reported"],
                    "severity": "error",
                    "message": (
                        f"均值 {record['mean']}（N={record['n']}，"
                        f"{record['decimals']} 位小数）在整数总和颗粒度上不可能"
                    ),
                    "context_snippet": record.get("sentence", ""),
                    "suggestion": "核对均值、样本量与原始数据的颗粒度是否一致",
                    "_location": location,
                }
            )
    summary = {
        "total_reports": len(records),
        "inferential_reports": sum(1 for item in records if item.get("test") != "grim"),
        "grim_reports": sum(1 for item in records if item.get("test") == "grim"),
        "p_value_mismatch": sum(1 for item in records if item.get("verdict") == "p_value_mismatch"),
        "decision_error": sum(1 for item in records if item.get("verdict") == "decision_error"),
        "grim_impossible_mean": sum(
            1 for item in records if item.get("verdict") == "grim_impossible_mean"
        ),
    }
    return {"records": records, "findings": findings, "summary": summary}
