"""评测指标：GLEU（GEC 常用）、术语召回、客观性得分等本地确定性指标。

evaluator Skill 使用本模块为润色输出打分；不依赖模型调用，结果可复现。
GLEU 为 Google-RE 版本的近似实现：奖励同时出现在假设与参考中的 n-gram，
惩罚出现在假设与源句（但不在参考）中的 n-gram，最后取 min(P, R)。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _ngrams(tokens: list[str], order: int) -> Counter:
    return Counter(tuple(tokens[i : i + order]) for i in range(len(tokens) - order + 1))


def gleu(source: str, hypothesis: str, reference: str, max_order: int = 4) -> float:
    """计算 GLEU 分数（0-1）。source 为未润色原句，reference 为人工参考答案。"""
    source_tokens = _tokens(source)
    hyp_tokens = _tokens(hypothesis)
    ref_tokens = _tokens(reference)
    if not hyp_tokens or not ref_tokens:
        return 0.0

    reward = 0
    hyp_total = 0
    ref_total = 0
    for order in range(1, max_order + 1):
        hyp_ngrams = _ngrams(hyp_tokens, order)
        ref_ngrams = _ngrams(ref_tokens, order)
        src_ngrams = _ngrams(source_tokens, order)
        for gram, count in hyp_ngrams.items():
            hyp_total += count
            if gram in ref_ngrams:
                reward += count
            elif gram in src_ngrams:
                reward -= count
        for gram, count in ref_ngrams.items():
            ref_total += count

    precision = max(0, reward) / max(1, hyp_total)
    # 召回方向：假设与参考共现的参考 n-gram 占比（惩罚源句固有 n-gram 的不公平性
    # 在召回侧难以对称定义，参考 Google-RE 实现取共现占比的近似）。
    matched = 0
    for order in range(1, max_order + 1):
        hyp_ngrams = _ngrams(hyp_tokens, order)
        for gram, count in _ngrams(ref_tokens, order).items():
            matched += min(count, hyp_ngrams.get(gram, 0))
    recall = matched / max(1, ref_total)
    return min(precision, recall)


def correction_stats(source: str, hypothesis: str, expected_errors: list[str]) -> dict[str, Any]:
    """按预期错误列表统计修正情况。

    每个预期错误以字符串形式给出，采用词级（token 子序列）匹配：
    假设中不再以完整 token 序列包含该片段即视为已修正，
    避免 "100 epoch" 被 "100 epochs" 误判为未修正。
    """
    hyp_tokens = _tokens(hypothesis)
    fixed: list[str] = []
    remained: list[str] = []
    for error in expected_errors:
        error_tokens = _tokens(error)
        n = len(error_tokens)
        found = False
        if n:
            for i in range(len(hyp_tokens) - n + 1):
                if hyp_tokens[i : i + n] == error_tokens:
                    found = True
                    break
        elif error.lower() in (hypothesis or "").lower():
            found = True
        (remained if found else fixed).append(error)
    total = max(1, len(expected_errors))
    return {
        "fixed": fixed,
        "remained": remained,
        "fix_rate": round(len(fixed) / total, 4),
    }


def term_presence(text: str, terms: list[str], language: str) -> dict[str, int]:
    """统计术语在文本中的出现次数；英文按词边界、中文按子串。"""
    lowered = text or ""
    counts: dict[str, int] = {}
    for term in terms:
        if not term:
            continue
        if language == "cn":
            counts[term] = lowered.count(term)
        else:
            pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
            counts[term] = len(re.findall(pattern, lowered, flags=re.IGNORECASE))
    return counts


def terminology_scores(
    output: str,
    expect_canonical: list[str],
    expect_removed: list[str],
    language: str,
) -> dict[str, Any]:
    """术语评测：canonical 出现即召回，nonstandard 出现即漏拦。"""
    canonical_counts = term_presence(output, expect_canonical, language)
    removed_counts = term_presence(output, expect_removed, language)
    recalled = sum(1 for count in canonical_counts.values() if count > 0)
    leaked = sum(1 for count in removed_counts.values() if count > 0)
    return {
        "canonical_found": [term for term, count in canonical_counts.items() if count > 0],
        "canonical_missing": [term for term, count in canonical_counts.items() if count == 0],
        "recall": round(recalled / max(1, len(expect_canonical)), 4),
        "nonstandard_leaked": [term for term, count in removed_counts.items() if count > 0],
        "interception": round(1 - leaked / max(1, len(expect_removed)), 4),
    }


def objectivity_score(
    source: str,
    output: str,
    hype_to_remove: list[str],
    guard_findings_errors: int,
    numbers_missing: int,
) -> dict[str, Any]:
    """客观性评测：去浮夸 + 数据锚定保持。

    成功标准：输出中不再包含需移除的浮夸词（词级存在性检查）、
    claim-guard 本地扫描无 error 级发现、且没有丢失任何数字。
    """
    lowered = output or ""
    residual = [word for word in hype_to_remove if word and word.lower() in lowered.lower()]
    success = not residual and guard_findings_errors == 0 and numbers_missing == 0
    return {
        "success": success,
        "hype_residual": residual,
        "guard_error_findings": guard_findings_errors,
        "numbers_missing": numbers_missing,
    }
