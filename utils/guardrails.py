"""guardrails：润色结果的确定性消毒器（纯离线、纯标准库、零模型调用）。

设计原则（对应 docs/hallucination-guardrails-plan.md L2）：
- 用集合不变量 / 正则 / 词表替代不可靠的"模型自我评估"；
- CoVe 的 factored 原则：evaluate_pair 只接收 (source, polished) 二元组，
  不接触 prompt、模型、usage 等生成侧信息，杜绝"被流畅文本说服"；
- 所有判定确定性可复现，可直接单测。

十条规则：
  R1 numeric.conservation   数值丢失/新增/单位脱钩（error；新增可配置）
  R2 numeric.polarity       数值邻域与句级极性翻转、主客体互换（error）
  R3 citation.conservation  引用丢失/新增/编号越界（error）
  R4 anchor.conservation    图表公式编号与核心实体丢失（error/警告）
  R5 hedge.flip             对冲语气单向硬化（error）/ 软化（warning）
  R6 scope.shift            样本/条件限定语丢失（warning，strict 升 error）
  R7 hype.lexicon           浮夸词库（委托 claim-guard，error/warning）
  R8 aiflavor.lexicon       AI 腔词库 + 密度硬上限（error）
  R9 structure.inflation    长度比/段落数/过度修正（error）+ 孤儿新增句（warning）
  R10 semantic.drift        句级配对后内容词 Jaccard 漂移（warning）

strict_guardrails=true 时，warning 一并升级为 fail（由调用方决定是否阻断）。
"""

from __future__ import annotations

import heapq
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from utils.citation_check import clean_text  # noqa: F401 - 保留供规则扩展使用
from utils.config_loader import load_rules_config, normalize_language
from utils.differ import diff_stats
from utils.fidelity import (
    CITATION_AUTHOR_YEAR_RE,
    CITATION_BRACKET_RE,
    CITATION_LATEX_RE,
    NUMBER_RE,
    count_paragraphs,
    extract_numbers,
    markup_preserved,
)
from utils.logger import get_logger
from utils.skill_loader import run_skill

logger = get_logger("guardrails")

RULE_NAMES = (
    "numeric.conservation",
    "numeric.polarity",
    "citation.conservation",
    "anchor.conservation",
    "hedge.flip",
    "scope.shift",
    "hype.lexicon",
    "aiflavor.lexicon",
    "structure.inflation",
    "semantic.drift",
)

# --------------------------------------------------------------------------- #
# 词表与阈值常量
# --------------------------------------------------------------------------- #

# 测量单位（单位脱钩检查用；中文量词"个/次/类"等属风格范畴，不纳入）
MEASUREMENT_UNITS = (
    "db", "ms", "min", "hz", "khz", "mhz", "ghz", "kb", "mb", "gb", "tb",
    "ml", "mg", "kg", "μg", "ug", "nm", "mm", "cm", "km", "mv", "μv", "uv",
    "ma", "kw", "mw", "℃", "°c", "mol", "ppm", "ppb", "fps", "bpm",
    "l", "g", "m", "s", "h", "v", "a", "w", "k",
)
_UNIT_ALTERNATION = "|".join(re.escape(unit) for unit in MEASUREMENT_UNITS)
NUMBER_UNIT_RE = re.compile(
    r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(%|" + _UNIT_ALTERNATION + r")(?![A-Za-z])",
    re.IGNORECASE,
)

# 极性词对（正向, 反向）——任一句中正向被反向替换且原词消失即判翻转
POLARITY_PAIRS = (
    ("提升", "降低"), ("提高", "降低"), ("增加", "减少"), ("优于", "劣于"),
    ("高于", "低于"), ("改善", "恶化"), ("上升", "下降"), ("扩大", "缩小"),
    ("增强", "减弱"), ("加快", "减慢"), ("提升", "下降"), ("抑制", "增强"),
    ("接近", "远离"), ("改善", "削弱"),
    ("increase", "decrease"), ("higher", "lower"), ("improve", "worsen"),
    ("better", "worse"), ("rise", "fall"), ("gain", "loss"), ("enhance", "reduce"),
    ("accelerate", "slow"), ("up", "down"), ("over", "under"), ("above", "below"),
    ("outperform", "underperform"), ("suppress", "amplify"), ("degrade", "improve"),
    ("widen", "narrow"), ("raise", "lower"), ("closer", "farther"),
    ("closest", "farthest"), ("shrink", "grow"), ("drop", "rise"),
    ("reduce", "increase"),
)

# 对冲词 → 强化词（反向映射自动生成"软化"警告）
HEDGE_COUNTERPARTS: dict[str, dict[str, list[str]]] = {
    "en": {
        "may": ["prove", "proves", "proven", "demonstrate", "demonstrates", "confirm", "confirms", "establish", "establishes", "certainly", "definitely"],
        "might": ["prove", "proves", "proven", "demonstrate", "demonstrates", "confirm", "confirms", "certainly", "definitely"],
        "could": ["prove", "proves", "demonstrate", "demonstrates", "confirm", "confirms", "certainly", "definitely"],
        "possibly": ["certainly", "definitely", "undoubtedly"],
        "perhaps": ["certainly", "definitely", "undoubtedly"],
        "seem": ["clearly", "obviously", "undoubtedly"],
        "appear": ["clearly", "obviously", "undoubtedly"],
        "suggest": ["prove", "proves", "demonstrate", "demonstrates", "confirm", "confirms", "establish", "establishes"],
        "likely": ["certainly", "definitely", "undoubtedly"],
        "tend to": ["always", "invariably"],
        "to some extent": ["completely", "fully", "entirely"],
        "in part": ["completely", "fully", "entirely"],
        "partially": ["completely", "fully", "entirely"],
        "preliminary": ["exhaustive", "definitive", "conclusive"],
        "hint": ["confirm", "prove", "demonstrate"],
        "some": ["all"],
        "controlled": ["any"],
        "limited": ["unlimited", "arbitrary"],
    },
    "cn": {
        "可能": ["一定", "必然", "肯定", "证明", "证实"],
        "或许": ["一定", "必然", "肯定"],
        "在一定程度上": ["完全", "彻底", "充分"],
        "部分": ["完全", "全部", "彻底"],
        "似乎": ["显然", "确实"],
        "初步": ["充分", "全面"],
        "预计": ["必定"],
        "有望": ["必将"],
        "提示": ["证实", "证明"],
        "受控": ["任意", "任何"],
        "某种程度上": ["完全", "彻底"],
    },
}

# 范围限定语（源句有而配对润句全丢 → scope.shift）：显式清单 + 模式兜底
SCOPE_MARKERS = (
    "在本研究", "在本文", "在本实验", "本实验中", "本文中", "本样本", "该样本",
    "在该数据集", "在此条件下", "在当前设置", "在所构建的",
    "in this study", "in this paper", "in this work", "in our experiment",
    "in this sample", "in the current study", "under these conditions",
    "in the present work", "on this dataset",
)
SCOPE_PHRASE_RE = re.compile(
    r"在[^，。；,!?]{2,24}?(数据集|数据|条件|场景|环境|样本|研究|实验|合成|受控|设置|任务|范畴|范围内)"
    r"|\b(?:in|on|under|within)\s+(?:\w+[\s\-]){0,3}?"
    r"(?:recordings?|datasets?|data|conditions?|settings?|experiments?|scenes?|contexts?|samples?|studies|cohorts?|subsets?|tasks?)",
    re.IGNORECASE,
)
# 范围量化词：源句有限定而润句换成全称 → 范围被扩大
SCOPE_QUANTIFIERS_SRC = ("仅", "只", "限于", "两类", "三种", "上述", "部分")
SCOPE_QUANTIFIERS_POL = ("所有", "全部", "任何", "各类", "普遍")
SCOPE_QUANTIFIERS_SRC_EN = ("only", "limited to", "restricted to", "two", "three", "the above", "specific")
SCOPE_QUANTIFIERS_POL_EN = ("all", "any", "every", "various", "universal", "arbitrary")

# 图表/公式引用表达式（支持 图3、图3、4、图1~4、图9(a)、Fig. 2、Table 3、Eq. (5)）
FIGURE_RE = re.compile(
    r"(?:图|表)\s*\d+(?:\s*[~\-–—、,，]\s*\d+)*(?:\(\s*[a-z]\s*\))?"
    r"|Fig(?:ure)?\.?\s*\d+[a-z]?(?:\s*[~\-–—]\s*\d+)?"
    r"|Table\s*\d+[a-z]?(?:\s*[~\-–—]\s*\d+)?"
    r"|Eq(?:uation)?\.?\s*[（(]\s*\d+\s*[)）](?:\s*[~\-–—]\s*\d+)?"
    r"|式\s*[（(]\s*\d+\s*[)）]",
    re.IGNORECASE,
)

# 实体：全大写缩写 / CamelCase / 连字符专名
ABBREV_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")
CAMEL_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b")
HYPHEN_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z]*-[A-Z][A-Za-z]+\b")
UPPER_CAMEL_RE = re.compile(r"\b[A-Z]{2,}[A-Za-z0-9]+\b")
ENTITY_STOPLIST = {
    "DOI", "URL", "PDF", "HTML", "XML", "USA", "UK", "PC", "TV", "CD", "DVD",
    "USB", "FAQ", "ID", "OK", "NaN", "JSON", "HTTP", "HTTPS", "CPU", "GPU",
    "RAM", "ROM", "SQL", "API", "SDK", "CLI", "GUI", "OS", "IO", "AI", "ML",
}

PAIR_THRESHOLD = 0.30  # 句级配对阈值（低于视为无对应句）
MAX_PAIR_CANDIDATES = 24  # 每句参与 Jaccard 的源句候选上限（重叠计数 top-K）
ORPHAN_ERROR_FLOOR = 0.10  # 孤儿句判"臆造"的重叠下限（低于此才升级 error）
POLARITY_WINDOW = 14   # 数值邻域极性检查窗口（字符）
ANCHOR_WINDOW = 30     # 模糊归因的引用豁免窗口（字符）

CN_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；])")
EN_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
CITATION_NUMBER_RE = re.compile(r"\d{1,3}")

EN_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "using", "based",
    "however", "therefore", "these", "those", "such", "which", "have", "has", "are",
    "was", "were", "can", "may", "also", "more", "most", "than", "then", "when",
    "our", "their", "its", "it", "we", "they", "not", "all", "each", "other",
    "between", "during", "after", "before", "while", "both", "under", "over",
    "there", "here", "than", "upon", "within", "without", "across", "per", "via",
}


# --------------------------------------------------------------------------- #
# 输出结构
# --------------------------------------------------------------------------- #


@dataclass
class Violation:
    """单条违例。rule 为规范名；evidence.kind 区分同规则子类（供兼容映射）。"""

    rule: str
    severity: str  # error | warning
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    source_span: tuple[int, int] | None = None
    polished_span: tuple[int, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "evidence": dict(self.evidence),
            "source_span": list(self.source_span) if self.source_span else None,
            "polished_span": list(self.polished_span) if self.polished_span else None,
        }


@dataclass
class GuardReport:
    verdict: str  # pass | warn | fail
    violations: list[Violation]
    attribution: dict[str, list[str]]
    metrics: dict[str, Any]
    retry_feedback: str
    strict: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "violations": [v.as_dict() for v in self.violations],
            "attribution": dict(self.attribution),
            "metrics": dict(self.metrics),
            "retry_feedback": self.retry_feedback,
            "strict": self.strict,
        }


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def _split_sentences(text: str, language: str) -> list[str]:
    text = text or ""
    pattern = CN_SENT_SPLIT_RE if language == "cn" else EN_SENT_SPLIT_RE
    parts = pattern.split(text)
    return [part.strip() for part in parts if part.strip()]


def _content_tokens(sentence: str, language: str) -> set[str]:
    if language == "cn":
        latin = re.findall(r"[A-Za-z][A-Za-z0-9\-\.]{1,}", sentence)
        han = re.findall(r"[\u4e00-\u9fff]", sentence)
        bigrams = {"".join(han[i : i + 2]) for i in range(len(han) - 1)}
        return {token.lower() for token in latin} | bigrams
    words = re.findall(r"[A-Za-z][A-Za-z\-']+", sentence.lower())
    return {word for word in words if word not in EN_STOPWORDS and len(word) > 2}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _pair_sentences(
    source_sentences: list[str], polished_sentences: list[str], language: str
) -> tuple[list[tuple[int, int, float]], list[tuple[int, int, float]], list[int], list[int]]:
    """贪心句级配对（token 集合分组 + 倒排索引预 Filter，避免 O(n²) 全配对）。

    返回 (强配对, 弱配对, 未配对源句下标, 未配对润句下标)：
    - 强配对：相似度 ≥ PAIR_THRESHOLD；
    - 弱配对：润句的最佳匹配低于阈值——scope/hedge/极性检查仍需对照最佳源句，
      否则"润句删掉限定语"恰恰因不相似而逃过检查；
    - 弱配对附带最佳相似度，供语义漂移判定使用。

    源句先按 token 集合分组：重复样板句（如反复出现的同型结果句）在索引与
    Jaccard 计算中只代表一次，组内成员按原始顺序逐个消耗，维持一对一对齐。
    没有这层分组，大量重复句会把每个润句的 top-K 候选全部占满，其余润句
    因候选饥饿而误判为孤儿句，再触发孤儿检查的 O(孤儿数×源句数) 比对。
    """
    source_tokens = [_content_tokens(sentence, language) for sentence in source_sentences]
    polished_tokens = [_content_tokens(sentence, language) for sentence in polished_sentences]
    # 源句分组：token 集合 → 组（成员按原始下标排列，available 为未消耗成员栈）
    groups: dict[frozenset[str], dict[str, Any]] = {}
    group_list: list[dict[str, Any]] = []
    for si, tokens in enumerate(source_tokens):
        key = frozenset(tokens)
        group = groups.get(key)
        if group is None:
            group = {"tokens": tokens, "members": [si], "available": [si]}
            groups[key] = group
            group_list.append(group)
        else:
            group["members"].append(si)
            group["available"].append(si)

    # 倒排索引：token → 组下标（每组只登记一次）
    index: dict[str, list[int]] = {}
    for gi, group in enumerate(group_list):
        for token in group["tokens"]:
            index.setdefault(token, []).append(gi)

    scored: list[tuple[float, int, int]] = []
    for pi, tokens in enumerate(polished_tokens):
        if not tokens:
            continue
        # 重叠计数预过滤：只对重叠最多的 top-K 组算 Jaccard
        overlap: dict[int, int] = {}
        for token in tokens:
            for gi in index.get(token, ()):
                overlap[gi] = overlap.get(gi, 0) + 1
        if not overlap:
            continue
        if len(overlap) > MAX_PAIR_CANDIDATES:
            top = heapq.nlargest(
                MAX_PAIR_CANDIDATES, overlap.items(), key=lambda item: item[1]
            )
            candidate_groups = [gi for gi, _count in top]
        else:
            candidate_groups = list(overlap)
        for gi in candidate_groups:
            group = group_list[gi]
            if not group["available"]:
                continue
            score = _jaccard(tokens, group["tokens"])
            if score > 0:
                scored.append((score, gi, pi))

    scored.sort(reverse=True)
    pairs: list[tuple[int, int, float]] = []
    used_src: set[int] = set()
    used_pol: set[int] = set()
    for score, gi, pi in scored:
        group = group_list[gi]
        if not group["available"] or pi in used_pol or score < PAIR_THRESHOLD:
            continue
        si = group["available"].pop(0)
        pairs.append((si, pi, round(score, 4)))
        used_src.add(si)
        used_pol.add(pi)
    weak: list[tuple[int, int, float]] = []
    claimed: set[int] = set()
    for score, gi, pi in scored:
        if pi in used_pol or pi in claimed:
            continue
        weak.append((group_list[gi]["members"][0], pi, round(score, 4)))
        claimed.add(pi)
    orphan_source = [i for i in range(len(source_sentences)) if i not in used_src]
    orphan_polished = [i for i in range(len(polished_sentences)) if i not in used_pol]
    return pairs, weak, orphan_source, orphan_polished


def _citation_number_multiset(text: str) -> Counter:
    """正文方括号引用的编号多重集（区间展开，兼容 [1,2] / [1-3] / 全角逗号）。"""
    counter: Counter = Counter()
    for marker in CITATION_BRACKET_RE.findall(text or ""):
        inner = marker.strip()[1:-1]
        for token in re.split(r"[,，]", inner):
            token = token.strip()
            if not token:
                continue
            range_match = re.fullmatch(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", token)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                if end >= start and end - start <= 200:
                    counter.update(range(start, end + 1))
            elif token.isdigit():
                counter[int(token)] += 1
    return counter


def _latex_cite_keys(text: str) -> Counter:
    keys: Counter = Counter()
    for match in CITATION_LATEX_RE.finditer(text or ""):
        body = match.group(0)
        brace = re.search(r"\{([^}]+)\}", body)
        if brace:
            for key in re.split(r"[,，]", brace.group(1)):
                key = key.strip()
                if key:
                    keys[key] += 1
    return keys


def _reference_numbers(references_text: str) -> set[int]:
    """参考文献条目编号集合（识别 `[n] ...` 行；无此格式时返回空集=跳过越界检查）。"""
    numbers: set[int] = set()
    for line in (references_text or "").split("\n"):
        match = re.match(r"^\s*\[(\d{1,3})\]", line)
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def _figure_numbers(text: str) -> set[str]:
    """图表/公式引用中出现的编号集合（图3、图3、4 均贡献 {3,4}）。"""
    numbers: set[str] = set()
    for match in FIGURE_RE.finditer(text or ""):
        for token in CITATION_NUMBER_RE.findall(match.group(0)):
            numbers.add(str(int(token)))
    return numbers


def _entity_tokens(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    claimed_spans: set[tuple[int, int]] = set()
    for pattern in (ABBREV_RE, CAMEL_RE, HYPHEN_ENTITY_RE, UPPER_CAMEL_RE):
        for match in pattern.finditer(text or ""):
            span = match.span()
            if span in claimed_spans:
                continue
            token = match.group(0)
            if token in ENTITY_STOPLIST or len(token) < 2:
                continue
            claimed_spans.add(span)
            counts[token] = counts.get(token, 0) + 1
    return counts


def _word_variants(word: str) -> set[str]:
    """英文词形变化：amplify→amplifies/amplified/amplifying；reduce→reduces/reduced/reducing。"""
    if len(word) <= 3 or " " in word:
        return {word}
    variants = {word, word + "ing"}
    if word.endswith("y"):
        stem = word[:-1]
        variants.update({stem + "ies", stem + "ied"})
    elif word.endswith("e"):
        variants.update({word + "s", word + "d", word[:-1] + "ing"})
    elif word.endswith(("s", "x", "ch", "sh", "o")):
        variants.update({word + "es", word + "ed"})
    else:
        variants.update({word + "s", word + "ed"})
    return variants


_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-\.]{1,}")
_SWAP_STOPWORDS = {
    "as", "shown", "show", "shows", "in", "the", "a", "an", "of", "is", "are",
    "was", "were", "by", "on", "for", "to", "with", "and", "or", "that", "this",
    "it", "its", "we", "our", "be", "been", "has", "have", "had", "can", "could",
    "table", "fig", "figure", "eq", "equation", "section", "see", "from", "at",
    "into", "than", "then", "when", "while", "both", "each", "such", "these",
}


def _latin_tokens(sentence: str) -> set[str]:
    """拉丁/实体/数字 token（主客体互换比对用，排除功能词与图表词噪声）。"""
    return {
        token.lower().rstrip(".-")
        for token in _LATIN_TOKEN_RE.findall(sentence or "")
        if token.lower().rstrip(".-") not in _SWAP_STOPWORDS
    }


def _word_present(word: str, sentence: str, language: str) -> bool:
    if language == "cn":
        return word in sentence
    lowered = sentence.lower()
    return any(
        re.search(r"(?<![A-Za-z])" + re.escape(variant) + r"(?![A-Za-z])", lowered)
        for variant in _word_variants(word.lower())
    )


_POLARITY_REGEX_CACHE: dict[str, Any] = {}


def _polarity_master(language: str):
    """极性词主正则 + surface→规范词 映射（每种语言编译一次）。"""
    if language in _POLARITY_REGEX_CACHE:
        return _POLARITY_REGEX_CACHE[language]
    words = {word for pair in POLARITY_PAIRS for word in pair}
    if language == "cn":
        surface_map = {word: word for word in words}
        pattern = re.compile("|".join(sorted(surface_map, key=len, reverse=True)))
    else:
        surface_map = {}
        for word in words:
            for variant in _word_variants(word.lower()):
                surface_map[variant] = word
        pattern = re.compile(
            r"(?<![A-Za-z])(" + "|".join(sorted(surface_map, key=len, reverse=True)) + r")(?![A-Za-z])",
            re.IGNORECASE,
        )
    _POLARITY_REGEX_CACHE[language] = (pattern, surface_map)
    return pattern, surface_map


def _sentence_surfaces(sentence: str, language: str) -> tuple[set[str], Counter]:
    """一次扫描返回句中出现的极性规范词集合与计数。"""
    pattern, surface_map = _polarity_master(language)
    counter: Counter = Counter()
    for match in pattern.finditer(sentence or ""):
        surface = match.group(0)
        counter[surface_map.get(surface.lower(), surface)] += 1
    return set(counter), counter


def _matched_surface(word: str, sentence: str, language: str) -> str | None:
    """返回句中实际出现的词形（用于 split 定位）；未出现返回 None。"""
    if language == "cn":
        return word if word in sentence else None
    for variant in sorted(_word_variants(word.lower()), key=len, reverse=True):
        if re.search(r"(?<![A-Za-z])" + re.escape(variant) + r"(?![A-Za-z])", sentence.lower()):
            return variant
    return None


def _word_count(word: str, sentence: str, language: str) -> int:
    if language == "cn":
        return sentence.count(word)
    lowered = sentence.lower()
    return sum(
        len(re.findall(r"(?<![A-Za-z])" + re.escape(variant) + r"(?![A-Za-z])", lowered))
        for variant in _word_variants(word.lower())
    )


def _polarity_in(sentence: str, language: str) -> set[str]:
    """句中出现的极性规范词集合（主正则一次扫描）。"""
    pattern, surface_map = _polarity_master(language)
    return {
        surface_map.get(match.group(0).lower(), match.group(0))
        for match in pattern.finditer(sentence or "")
    }


def _hedge_hits(sentence: str, language: str) -> set[str]:
    lowered = sentence.lower() if language == "en" else sentence
    hits: set[str] = set()
    for hedge in HEDGE_COUNTERPARTS.get(language, {}):
        probe = hedge.lower() if language == "en" else hedge
        if _word_present(probe, lowered, language):
            hits.add(hedge)
    return hits


def _scope_hits(sentence: str, language: str) -> list[str]:
    lowered = sentence.lower() if language == "en" else sentence
    hits = [marker for marker in SCOPE_MARKERS if marker.lower() in lowered]
    if not hits:
        hits = [match.group(0) for match in SCOPE_PHRASE_RE.finditer(sentence)]
    return hits


def _scope_widened(src: str, pol: str, language: str) -> bool:
    """源句有范围限定词、润句换成全称词 → 范围被扩大。"""
    if language == "en":
        lowered_src, lowered_pol = src.lower(), pol.lower()
        if not any(word in lowered_src for word in SCOPE_QUANTIFIERS_SRC_EN):
            return False
        return any(word in lowered_pol for word in SCOPE_QUANTIFIERS_POL_EN)
    if not any(word in src for word in SCOPE_QUANTIFIERS_SRC):
        return False
    return any(word in pol for word in SCOPE_QUANTIFIERS_POL)


def _load_ai_flavor(language: str) -> dict[str, Any]:
    from utils.config_loader import load_lexicon

    return load_lexicon("ai-flavor", language)


def _compile_lexicon_pattern(pattern: str, language: str) -> re.Pattern[str]:
    if language == "cn":
        return re.compile(pattern)
    branches = "|".join(
        part + (r"(?![A-Za-z0-9])" if part and part[-1].isalnum() else "")
        for part in pattern.split("|")
    )
    return re.compile(r"(?<![A-Za-z0-9])" + branches, re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 各规则实现
# --------------------------------------------------------------------------- #


def _check_numeric(source: str, polished: str, gates: dict[str, Any], out: list[Violation], metrics: dict[str, Any]) -> None:
    from utils.fidelity import numbers_preserved

    numbers = numbers_preserved(source, polished)
    metrics["numeric_missing"] = len(numbers["missing"])
    metrics["numeric_added"] = len(numbers["added"])
    if numbers["missing"]:
        out.append(
            Violation(
                rule="numeric.conservation",
                severity="error",
                message=f"丢失 {len(numbers['missing'])} 个数值: {', '.join(numbers['missing'][:8])}",
                evidence={"kind": "missing", "values": numbers["missing"][:12]},
            )
        )
    if numbers["added"]:
        severity = "error" if gates.get("fabrication_suspects_as_errors", True) else "warning"
        out.append(
            Violation(
                rule="numeric.conservation",
                severity=severity,
                message=f"新增 {len(numbers['added'])} 个原文没有的数值，疑似杜撰: {', '.join(numbers['added'][:8])}",
                evidence={"kind": "added", "values": numbers["added"][:12]},
            )
        )

    # 单位脱钩：数值仍在但测量单位丢失（0.8 mL → 0.8）
    def _unit_pairs(text: str) -> Counter:
        pairs: Counter = Counter()
        for match in NUMBER_UNIT_RE.finditer(text or ""):
            unit = match.group(2).lower()
            try:
                pairs[(float(match.group(1).replace(",", "")), unit)] += 1
            except ValueError:
                continue
        return pairs

    src_units, pol_units = _unit_pairs(source), _unit_pairs(polished)
    unit_loss = src_units - pol_units
    unit_gain = pol_units - src_units
    metrics["unit_loss"] = sum(unit_loss.values())
    if unit_loss:
        lost = [f"{value:g} {unit}" for (value, unit), count in list(unit_loss.items())[:6]]
        out.append(
            Violation(
                rule="numeric.conservation",
                severity="error",
                message=f"测量单位脱钩（数值保留但单位丢失）: {', '.join(lost)}",
                evidence={"kind": "unit_loss", "pairs": lost},
            )
        )
    if unit_gain:
        gained = [f"{value:g} {unit}" for (value, unit), count in list(unit_gain.items())[:6]]
        out.append(
            Violation(
                rule="numeric.conservation",
                severity="warning",
                message=f"新增原文没有的测量单位: {', '.join(gained)}",
                evidence={"kind": "unit_gain", "pairs": gained},
            )
        )


def _check_polarity(
    source: str,
    polished: str,
    pairs: list[tuple[int, int, float]],
    source_sentences: list[str],
    polished_sentences: list[str],
    language: str,
    out: list[Violation],
    metrics: dict[str, Any],
    weak: list[tuple[int, int, float]] | None = None,
) -> None:
    flips: list[str] = []
    weak = weak or []

    # 句级预计算：每句的极性规范词集合与计数（主正则一次扫描）
    src_precomputed = [_sentence_surfaces(sentence, language) for sentence in source_sentences]
    pol_precomputed = [_sentence_surfaces(sentence, language) for sentence in polished_sentences]
    src_sets = [item[0] for item in src_precomputed]
    src_counters = [item[1] for item in src_precomputed]
    pol_sets = [item[0] for item in pol_precomputed]
    pol_counters = [item[1] for item in pol_precomputed]

    # 1) 句级极性替换：源句含正向词、配对润句只含其反义词（含弱配对）
    for si, pi, _sim in pairs + weak:
        src_polarity, pol_polarity = src_sets[si], pol_sets[pi]
        for positive, negative in POLARITY_PAIRS:
            if positive in src_polarity and negative in pol_polarity and positive not in pol_polarity:
                flips.append(f"“{positive}”→“{negative}”: {source_sentences[si][:40]}")
            elif negative in src_polarity and positive in pol_polarity and negative not in pol_polarity:
                flips.append(f"“{negative}”→“{positive}”: {source_sentences[si][:40]}")

    # 2) 主客体互换：句中仅一处极性词时，"A 优于 B" 被写成 "B 优于 A"
    #    预过滤：仅当源句与润句都只含同一个极性词时才值得详细比对
    for si, pi, _sim in pairs + weak:
        if len(src_sets[si]) != 1 or src_sets[si] != pol_sets[pi]:
            continue
        src, pol = source_sentences[si], polished_sentences[pi]
        for positive, negative in POLARITY_PAIRS:
            for word in (positive, negative):
                if src_counters[si].get(word, 0) != 1 or pol_counters[pi].get(word, 0) != 1:
                    continue
                src_surface = _matched_surface(word, src, language)
                pol_surface = _matched_surface(word, pol, language)
                if not src_surface or not pol_surface:
                    continue
                src_parts = re.split(
                    r"(?<![A-Za-z])" + re.escape(src_surface) + r"(?![A-Za-z])",
                    src, maxsplit=1, flags=re.IGNORECASE,
                )
                pol_parts = re.split(
                    r"(?<![A-Za-z])" + re.escape(pol_surface) + r"(?![A-Za-z])",
                    pol, maxsplit=1, flags=re.IGNORECASE,
                )
                if len(src_parts) < 2 or len(pol_parts) < 2:
                    continue
                src_before = _latin_tokens(src_parts[0])
                src_after = _latin_tokens(src_parts[1])
                pol_before = _latin_tokens(pol_parts[0])
                pol_after = _latin_tokens(pol_parts[1])
                if (
                    src_before & pol_after
                    and src_after & pol_before
                    and not (src_before & pol_before)
                    and not (src_after & pol_after)
                ):
                    flips.append(f"主客体互换（{word} 前后成分对调）: {src[:40]}")

    # 3) 数值邻域极性翻转：同一数值前后窗口内出现反向极性词
    #    预计算两侧"含极性词"的数字及其上下文极性集，按数值 token 对齐
    def _anchored_numbers(body: str) -> dict[str, list[set[str]]]:
        anchored: dict[str, list[set[str]]] = {}
        for match in NUMBER_RE.finditer(body or ""):
            context = body[max(0, match.start() - POLARITY_WINDOW) : match.end() + POLARITY_WINDOW]
            polarity = _polarity_in(context, language)
            if polarity:
                anchored.setdefault(match.group(0), []).append(polarity)
        return anchored

    src_anchored = _anchored_numbers(source)
    pol_anchored = _anchored_numbers(polished)
    for token, src_polarities in src_anchored.items():
        # 同一数值在重复样板句中会出现大量相同上下文，去重后比对唯一极性集
        src_unique = {frozenset(item) for item in src_polarities}
        pol_unique = {frozenset(item) for item in pol_anchored.get(token, ())}
        for src_pol in src_unique:
            for pol_pol in pol_unique:
                for positive, negative in POLARITY_PAIRS:
                    if positive in src_pol and negative in pol_pol and positive not in pol_pol:
                        flips.append(f"数值 {token} 邻域“{positive}”→“{negative}”")
                    elif negative in src_pol and positive in pol_pol and negative not in pol_pol:
                        flips.append(f"数值 {token} 邻域“{negative}”→“{positive}”")

    metrics["polarity_flips"] = len(flips)
    if flips:
        out.append(
            Violation(
                rule="numeric.polarity",
                severity="error",
                message=f"极性方向被翻转 {len(flips)} 处: {flips[0]}",
                evidence={"kind": "flip", "flips": flips[:6]},
            )
        )


def _check_citations(
    source: str,
    polished: str,
    gates: dict[str, Any],
    references_text: str | None,
    out: list[Violation],
    metrics: dict[str, Any],
) -> None:
    from utils.fidelity import citations_preserved

    src_numbers = _citation_number_multiset(source)
    pol_numbers = _citation_number_multiset(polished)
    missing = src_numbers - pol_numbers
    added = pol_numbers - src_numbers
    metrics["citation_missing"] = sum(missing.values())
    metrics["citation_added"] = sum(added.values())
    if missing:
        markers = ", ".join(f"[{n}]×{c}" if c > 1 else f"[{n}]" for n, c in sorted(missing.items())[:8])
        out.append(
            Violation(
                rule="citation.conservation",
                severity="error",
                message=f"引用标记丢失: {markers}",
                evidence={"kind": "weakened", "numbers": sorted(missing)},
            )
        )
    if added:
        markers = ", ".join(f"[{n}]×{c}" if c > 1 else f"[{n}]" for n, c in sorted(added.items())[:8])
        out.append(
            Violation(
                rule="citation.conservation",
                severity="error",
                message=f"新增引用标记，疑似杜撰: {markers}",
                evidence={"kind": "added", "numbers": sorted(added)},
            )
        )

    # LaTeX \cite key 多重集
    src_keys, pol_keys = _latex_cite_keys(source), _latex_cite_keys(polished)
    if src_keys - pol_keys:
        out.append(
            Violation(
                rule="citation.conservation",
                severity="error",
                message=f"LaTeX 引用 key 丢失: {sorted((src_keys - pol_keys))[:6]}",
                evidence={"kind": "weakened_latex", "keys": sorted((src_keys - pol_keys))[:8]},
            )
        )
    if pol_keys - src_keys:
        out.append(
            Violation(
                rule="citation.conservation",
                severity="error",
                message=f"新增 LaTeX 引用 key: {sorted((pol_keys - src_keys))[:6]}",
                evidence={"kind": "added_latex", "keys": sorted((pol_keys - src_keys))[:8]},
            )
        )

    # 作者-年引用计数兜底（无数可提取，仅比数量）
    for kind, pattern in (("author_year", CITATION_AUTHOR_YEAR_RE),):
        src_count = len(pattern.findall(source or ""))
        pol_count = len(pattern.findall(polished or ""))
        if pol_count < src_count:
            out.append(
                Violation(
                    rule="citation.conservation",
                    severity="error",
                    message=f"{kind} 引用数量被削减: {src_count} → {pol_count}",
                    evidence={"kind": "weakened_author_year", "source": src_count, "polished": pol_count},
                )
            )
        elif pol_count > src_count:
            out.append(
                Violation(
                    rule="citation.conservation",
                    severity="error",
                    message=f"新增 {kind} 引用: {src_count} → {pol_count}",
                    evidence={"kind": "added_author_year", "source": src_count, "polished": pol_count},
                )
            )

    # 引用编号越界：正文编号必须存在于参考文献表
    allowed = _reference_numbers(references_text) if references_text else set()
    if gates.get("citation_index_bounded", True) and allowed:
        out_of_bounds = sorted(n for n in pol_numbers if n not in allowed)
        metrics["citation_out_of_bounds"] = out_of_bounds
        if out_of_bounds:
            out.append(
                Violation(
                    rule="citation.conservation",
                    severity="error",
                    message=f"引用编号越界（不在参考文献表 {min(allowed)}–{max(allowed)} 内）: {out_of_bounds[:8]}",
                    evidence={"kind": "out_of_bounds", "numbers": out_of_bounds[:10], "allowed_max": max(allowed)},
                )
            )
    else:
        metrics["citation_out_of_bounds"] = []


def _check_anchors(
    source: str,
    polished: str,
    gates: dict[str, Any],
    out: list[Violation],
    metrics: dict[str, Any],
) -> None:
    if not gates.get("anchor_conservation", True):
        return
    # 图表/公式编号
    src_figures, pol_figures = _figure_numbers(source), _figure_numbers(polished)
    missing_figures = sorted(src_figures - pol_figures, key=lambda item: (len(item), item))
    metrics["figure_missing"] = missing_figures
    if missing_figures:
        out.append(
            Violation(
                rule="anchor.conservation",
                severity="error",
                message=f"图表/公式引用编号丢失: {missing_figures[:8]}",
                evidence={"kind": "figure", "missing": missing_figures[:10]},
            )
        )

    # 实体（数据集/方法名/缩写）：源文出现 ≥2 次而润后完全消失 → error；仅 1 次 → warning
    src_entities, pol_entities = _entity_tokens(source), _entity_tokens(polished)
    whitelist = {str(item) for item in (gates.get("entity_whitelist") or [])}
    vanished_multi = sorted(
        token for token, count in src_entities.items() if count >= 2 and token not in pol_entities and token not in whitelist
    )
    vanished_once = sorted(
        token for token, count in src_entities.items() if count == 1 and token not in pol_entities and token not in whitelist
    )
    metrics["entity_missing"] = vanished_multi + vanished_once
    if vanished_multi:
        out.append(
            Violation(
                rule="anchor.conservation",
                severity="error",
                message=f"核心实体/缩写丢失（源文多次出现）: {vanished_multi[:8]}",
                evidence={"kind": "entity", "missing": vanished_multi[:10]},
            )
        )
    # 实体被拼错/偷换：润后出现与源实体高度相似的变体 → error（比"丢失"更严重）
    import difflib

    pol_entity_tokens = set(pol_entities)
    tampered: list[str] = []
    for token in vanished_multi + vanished_once:
        for candidate in pol_entity_tokens:
            lowered_token, lowered_candidate = token.lower(), candidate.lower()
            similar = difflib.SequenceMatcher(None, lowered_token, lowered_candidate).ratio() >= 0.8
            expansion = lowered_candidate.startswith(lowered_token) or lowered_candidate.endswith(
                lowered_token
            )
            if similar and not expansion:
                tampered.append(f"{token}→{candidate}")
                break
    if tampered:
        out.append(
            Violation(
                rule="anchor.conservation",
                severity="error",
                message=f"实体/缩写被拼错或篡改: {tampered[:6]}",
                evidence={"kind": "entity_tampered", "pairs": tampered[:8]},
            )
        )
    dropped_once = [token for token in vanished_once if not any(token in item for item in tampered)]
    if dropped_once:
        out.append(
            Violation(
                rule="anchor.conservation",
                severity="warning",
                message=f"实体/缩写丢失（源文仅出现 1 次）: {dropped_once[:8]}",
                evidence={"kind": "entity_once", "missing": dropped_once[:10]},
            )
        )


def _check_hedge_and_scope(
    pairs: list[tuple[int, int, float]],
    source_sentences: list[str],
    polished_sentences: list[str],
    language: str,
    strict: bool,
    out: list[Violation],
    metrics: dict[str, Any],
    weak: list[tuple[int, int, float]] | None = None,
) -> None:
    weak = weak or []
    hardened: list[str] = []
    softened: list[str] = []
    scope_lost: list[str] = []
    for si, pi, _sim in pairs + weak:
        src, pol = source_sentences[si], polished_sentences[pi]
        for hedge, counterparts in HEDGE_COUNTERPARTS.get(language, {}).items():
            if not _word_present(hedge.lower() if language == "en" else hedge, src, language):
                continue
            if _word_present(hedge.lower() if language == "en" else hedge, pol, language):
                continue
            for counterpart in counterparts:
                if _word_present(counterpart, pol, language):
                    hardened.append(f"“{hedge}”→“{counterpart}”: {src[:40]}")
                    break
        # 软化（断言 → 对冲）：警告级
        for hedge, counterparts in HEDGE_COUNTERPARTS.get(language, {}).items():
            for counterpart in counterparts:
                if _word_present(counterpart, src, language) and _word_present(
                    hedge.lower() if language == "en" else hedge, pol, language
                ):
                    softened.append(f"“{counterpart}”→“{hedge}”: {src[:40]}")
        # 范围限定语丢失或被扩大
        if _scope_hits(src, language) and not _scope_hits(pol, language):
            scope_lost.append(src[:40])
        elif _scope_widened(src, pol, language):
            scope_lost.append(f"范围被扩大: {src[:40]}")

    metrics["hedge_hardened"] = len(hardened)
    metrics["hedge_softened"] = len(softened)
    metrics["scope_lost"] = len(scope_lost)
    if hardened:
        out.append(
            Violation(
                rule="hedge.flip",
                severity="error",
                message=f"对冲语气被强化 {len(hardened)} 处: {hardened[0]}",
                evidence={"kind": "hardened", "items": hardened[:6]},
            )
        )
    if softened:
        out.append(
            Violation(
                rule="hedge.flip",
                severity="warning",
                message=f"对冲语气被弱化 {len(softened)} 处: {softened[0]}",
                evidence={"kind": "softened", "items": softened[:6]},
            )
        )
    if scope_lost:
        out.append(
            Violation(
                rule="scope.shift",
                severity="error" if strict else "warning",
                message=f"样本/条件限定语丢失 {len(scope_lost)} 处: {scope_lost[0]}",
                evidence={"kind": "scope_lost", "items": scope_lost[:6]},
            )
        )


def _check_hype(polished: str, language: str, out: list[Violation], metrics: dict[str, Any]) -> None:
    report = run_skill("claim-guard", {"text": polished, "language": language, "mode": "local"})
    findings = report.get("findings") or []
    errors = [f for f in findings if f.get("severity") == "error"]
    warnings = [f for f in findings if f.get("severity") == "warning"]
    metrics["hype_error"] = len(errors)
    metrics["hype_warning"] = len(warnings)
    for finding in errors:
        out.append(
            Violation(
                rule="hype.lexicon",
                severity="error",
                message=f"浮夸/主观表述残留: {finding.get('found')} — {finding.get('suggestion', '')}",
                evidence={"kind": "hype", "finding": finding},
            )
        )
    for finding in warnings:
        out.append(
            Violation(
                rule="hype.lexicon",
                severity="warning",
                message=f"疑似浮夸表述: {finding.get('found')}",
                evidence={"kind": "hype_warning", "finding": finding},
            )
        )


def _check_ai_flavor(polished: str, language: str, out: list[Violation], metrics: dict[str, Any]) -> None:
    lexicon = _load_ai_flavor(language)
    limits = lexicon.get("density_limits") or {}
    paragraphs = [p for p in re.split(r"\n\s*\n", polished or "") if p.strip()] or [polished or ""]

    per_category: Counter = Counter()
    hits_by_category: dict[str, list[str]] = {}
    for entry in lexicon.get("entries") or []:
        category = str(entry.get("category") or "ai_highfreq")
        pattern = _compile_lexicon_pattern(str(entry.get("pattern") or ""), language)
        if not pattern.pattern:
            continue
        for match in pattern.finditer(polished):
            if category == "vague_attribution":
                window = polished[max(0, match.start() - ANCHOR_WINDOW) : match.end() + ANCHOR_WINDOW]
                if CITATION_BRACKET_RE.search(window) or re.search(r"\(\s*[A-Z][A-Za-z\-']+.*\d{4}", window):
                    continue  # 有具体出处支撑的归因是合法的
            per_category[category] += 1
            hits_by_category.setdefault(category, []).append(match.group(0)[:24])

    metrics["aiflavor_hits"] = dict(per_category)
    rules: list[tuple[str, str, str]] = [
        ("ai_highfreq", f"每段 ≤ {limits.get('ai_highfreq_per_paragraph', 2)}", "per_paragraph"),
        ("generic_ending", "全文 0 容忍", "total_zero"),
        ("vague_attribution", "全文 0 容忍", "total_zero"),
        ("paragraph_closer", f"全文 ≤ {limits.get('paragraph_closer_total', 1)}", "doc_limit"),
        ("triple_enumeration", f"每段 ≤ {limits.get('triple_enumeration_per_paragraph', 1)}", "per_paragraph"),
    ]
    for category, limit_text, mode in rules:
        count = per_category.get(category, 0)
        if not count:
            continue
        if mode == "total_zero" and count > 0:
            out.append(
                Violation(
                    rule="aiflavor.lexicon",
                    severity="error",
                    message=f"AI 腔[{category}]命中 {count} 处（{limit_text}）: {hits_by_category[category][:4]}",
                    evidence={"kind": category, "count": count, "hits": hits_by_category[category][:6]},
                )
            )
        elif mode == "per_paragraph":
            limit = int(limits.get(f"{category}_per_paragraph", 2))
            # 逐段计数（段内命中数 > limit 才违规）
            category_re = _compile_lexicon_pattern(_category_pattern(lexicon, category, language), language)
            per_para_counts = [
                sum(1 for _ in category_re.finditer(paragraph)) for paragraph in paragraphs
            ]
            if any(count_in_para > limit for count_in_para in per_para_counts):
                out.append(
                    Violation(
                        rule="aiflavor.lexicon",
                        severity="error",
                        message=f"AI 腔[{category}]密度超标（{limit_text}，实际峰值 {max(per_para_counts)}）: {hits_by_category[category][:4]}",
                        evidence={"kind": category, "per_paragraph": per_para_counts, "hits": hits_by_category[category][:6]},
                    )
                )
        elif mode == "doc_limit":
            limit = int(limits.get(f"{category}_total", 1))
            if count > limit:
                out.append(
                    Violation(
                        rule="aiflavor.lexicon",
                        severity="error",
                        message=f"AI 腔[{category}]密度超标（{limit_text}，实际 {count}）: {hits_by_category[category][:4]}",
                        evidence={"kind": category, "count": count, "hits": hits_by_category[category][:6]},
                    )
                )


def _category_pattern(lexicon: dict[str, Any], category: str, language: str) -> str:
    patterns = [
        str(entry.get("pattern") or "")
        for entry in lexicon.get("entries") or []
        if str(entry.get("category") or "") == category and str(entry.get("pattern") or "")
    ]
    return "|".join(f"(?:{pattern})" for pattern in patterns)


def _fast_similarity(source: str, polished: str, language: str) -> float:
    """词/字元级 Dice 相似度（长文本近似，O(n)）。"""
    left = _content_tokens(source, language)
    right = _content_tokens(polished, language)
    if not left or not right:
        return 0.0
    return round(2 * len(left & right) / (len(left) + len(right)), 4)


def _check_structure(
    source: str,
    polished: str,
    gates: dict[str, Any],
    language: str,
    strength: str,
    pairs: list[tuple[int, int, float]],
    orphan_polished: list[int],
    source_sentences: list[str],
    polished_sentences: list[str],
    out: list[Violation],
    metrics: dict[str, Any],
) -> None:
    source_len = max(1, len(source.strip()))
    polished_len = len(polished.strip())
    length_ratio = polished_len / source_len
    metrics["length_ratio"] = round(length_ratio, 4)
    metrics["paragraphs_source"] = count_paragraphs(source)
    metrics["paragraphs_polished"] = count_paragraphs(polished)

    # 相似度：小文本用精确 SequenceMatcher；大文本（合计 > 4000 字符）用词级
    # Dice 近似——SequenceMatcher 在长文本上为 O(n²)，10 万字段落不可接受
    if source_len + polished_len > 4000:
        similarity = _fast_similarity(source, polished, language)
        stats = {"change_ratio": round(1 - similarity, 4)}
    else:
        stats = diff_stats(source, polished)
        similarity = stats["similarity_ratio"]
    metrics["similarity_ratio"] = similarity
    metrics["change_ratio"] = stats["change_ratio"]

    ratio_min = float(gates.get("length_ratio_min", 0.5))
    ratio_max = float(gates.get("length_ratio_max", 2.0))
    # 长度比是 chunk 级门禁：源文过短（<120 字符）时比例失真，交由其他规则兜底
    if source_len >= 120 and not ratio_min <= length_ratio <= ratio_max:
        out.append(
            Violation(
                rule="structure.inflation",
                severity="error",
                message=f"长度比 {length_ratio:.2f} 超出允许区间 [{ratio_min}, {ratio_max}]",
                evidence={"kind": "length_ratio", "length_ratio": round(length_ratio, 4)},
            )
        )

    unchanged_min = float(gates.get("unchanged_min_ratio", 0.0))
    if similarity < unchanged_min:
        out.append(
            Violation(
                rule="structure.inflation",
                severity="error",
                message=f"相似度 {stats['similarity_ratio']} 低于档位下限 {unchanged_min}，疑似过度修正或改写失真",
                evidence={"kind": "overcorrection", "stats": stats},
            )
        )

    paragraph_mode = str(gates.get("paragraph_mode", "exact"))
    src_paras, out_paras = metrics["paragraphs_source"], metrics["paragraphs_polished"]
    if paragraph_mode == "exact":
        if src_paras != out_paras:
            out.append(
                Violation(
                    rule="structure.inflation",
                    severity="error",
                    message=f"段落数量变化: {src_paras} → {out_paras}（本档位要求逐段对应）",
                    evidence={"kind": "paragraphs", "source": src_paras, "polished": out_paras},
                )
            )
    else:
        tolerance = max(1, round(src_paras * 0.1))
        if abs(src_paras - out_paras) > tolerance:
            out.append(
                Violation(
                    rule="structure.inflation",
                    severity="error",
                    message=f"段落数量变化超出容差: {src_paras} → {out_paras}（容差 {tolerance}）",
                    evidence={"kind": "paragraphs", "source": src_paras, "polished": out_paras},
                )
            )

    if markup_preserved(source, polished):
        changed = markup_preserved(source, polished)
        out.append(
            Violation(
                rule="structure.inflation",
                severity="error",
                message=f"公式/代码块标记数量变化: {changed}",
                evidence={"kind": "markup", "changed": changed},
            )
        )

    # 孤儿新增句（"细节出现"漂移）：润句无任何源句对应，且内容不被任何源句包含
    # （包含 = 合法拆句，如 "A，B。" 拆成 "A。B。"，不判违规）
    # 源句按 token 集合去重后比对：重复样板句只算一次，避免 O(孤儿数×源句数) 重复计算
    source_token_groups: dict[frozenset[str], set[str]] = {}
    for sentence in source_sentences:
        tokens = _content_tokens(sentence, language)
        source_token_groups.setdefault(frozenset(tokens), tokens)
    fabricated: list[str] = []
    split_artifacts = 0
    for index in orphan_polished:
        sentence = polished_sentences[index]
        tokens = _content_tokens(sentence, language)
        if tokens and any(tokens <= token_set for token_set in source_token_groups.values()):
            split_artifacts += 1
            continue
        # 与所有源句的最佳重叠低于下限才判"臆造"；中度重叠交给语义漂移警告
        best = max(
            (_jaccard(tokens, token_set) for token_set in source_token_groups.values()),
            default=0.0,
        )
        if best >= ORPHAN_ERROR_FLOOR:
            continue
        # 与源文共享数字的句子是数值表述的合法改写，不判臆造
        sentence_numbers = set(extract_numbers(sentence))
        source_numbers = set(extract_numbers(source))
        if sentence_numbers and sentence_numbers <= source_numbers:
            continue
        fabricated.append(sentence)
    metrics["orphan_sentences"] = len(fabricated)
    metrics["split_artifacts"] = split_artifacts
    if fabricated:
        out.append(
            Violation(
                rule="structure.inflation",
                severity="error",
                message=f"检出 {len(fabricated)} 句无原文依据的新增句（疑似臆造）: {fabricated[0][:40]}",
                evidence={"kind": "orphan", "sentences": [item[:60] for item in fabricated[:6]]},
            )
        )


def _check_semantic_drift(
    pairs: list[tuple[int, int, float]],
    source_sentences: list[str],
    polished_sentences: list[str],
    language: str,
    threshold: float,
    out: list[Violation],
    metrics: dict[str, Any],
    weak: list[tuple[int, int, float]] | None = None,
) -> None:
    """句级语义漂移：配对句内容词重叠低于阈值（warning）。"""
    weak = weak or []
    drifted = [(si, pi, sim) for si, pi, sim in pairs if sim < threshold]
    weak_drifted = [
        (si, pi, sim) for si, pi, sim in weak if 0.12 <= sim < threshold
    ]
    all_drifted = drifted + weak_drifted
    metrics["drift_pairs"] = len(all_drifted)
    if all_drifted:
        si, pi, sim = all_drifted[0]
        out.append(
            Violation(
                rule="semantic.drift",
                severity="warning",
                message=f"{len(all_drifted)} 个句对内容词重叠度 {sim:.2f} 低于阈值 {threshold}，请人工确认语义未漂移: “{source_sentences[si][:30]}” → “{polished_sentences[pi][:30]}”",
                evidence={"kind": "drift", "count": len(all_drifted)},
            )
        )


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def evaluate_pair(
    source: str,
    polished: str,
    *,
    language: str = "cn",
    strength: str = "deep",
    anchors: Any = None,
    references_text: str | None = None,
    config: dict[str, Any] | None = None,
    strict: bool | None = None,
) -> GuardReport:
    """对 (source, polished) 执行十条确定性规则，返回 GuardReport。

    anchors：anchor_lock.AnchorSet（可选；传入时其数值/引用清单参与守恒检查，
    未传入时自行从 source 提取——两者等价，传入可省一次提取）。
    strict=None 时取 quality_gates.strict_guardrails（默认 false）。
    """
    language = normalize_language(language)
    source = source or ""
    polished = polished or ""
    if not source.strip() or not polished.strip():
        violation = Violation(
            rule="structure.inflation",
            severity="error",
            message="源文或润色文本为空，无法完成守恒审查",
            evidence={"kind": "empty_text", "source_empty": not source.strip(), "polished_empty": not polished.strip()},
        )
        return GuardReport(
            verdict="fail",
            violations=[violation],
            attribution={"structure.inflation": [violation.message]},
            metrics={"empty_text": True},
            retry_feedback=attribute_feedback([violation], language=language),
            strict=bool(strict),
        )
    if config is None:
        config = load_rules_config(language)
    strengths = config.get("strengths") or {}
    if strength not in strengths:
        raise ValueError(f"未知强度: {strength!r}，可选 {sorted(strengths)}")
    gates = strengths[strength].get("quality_gates") or {}
    if strict is None:
        strict = bool(gates.get("strict_guardrails", False))

    violations: list[Violation] = []
    metrics: dict[str, Any] = {}

    _check_numeric(source, polished, gates, violations, metrics)
    _check_citations(source, polished, gates, references_text, violations, metrics)
    _check_anchors(source, polished, gates, violations, metrics)

    source_sentences = _split_sentences(source, language)
    polished_sentences = _split_sentences(polished, language)
    pairs, weak, _orphan_source, orphan_polished = _pair_sentences(source_sentences, polished_sentences, language)

    _check_polarity(
        source, polished, pairs, source_sentences, polished_sentences, language, violations, metrics, weak=weak
    )
    _check_hedge_and_scope(
        pairs, source_sentences, polished_sentences, language, strict, violations, metrics, weak=weak
    )
    _check_hype(polished, language, violations, metrics)
    _check_ai_flavor(polished, language, violations, metrics)
    _check_structure(
        source,
        polished,
        gates,
        language,
        strength,
        pairs,
        orphan_polished,
        source_sentences,
        polished_sentences,
        violations,
        metrics,
    )
    drift_threshold = float(gates.get("semantic_drift_threshold", 0.35))
    if strength == "rewrite":
        drift_threshold = min(drift_threshold, 0.25)
    _check_semantic_drift(
        pairs, source_sentences, polished_sentences, language, drift_threshold, violations, metrics, weak=weak
    )

    error_count = sum(1 for item in violations if item.severity == "error")
    warning_count = sum(1 for item in violations if item.severity == "warning")
    if error_count:
        verdict = "fail"
    elif warning_count and strict:
        verdict = "fail"
    elif warning_count:
        verdict = "warn"
    else:
        verdict = "pass"

    attribution: dict[str, list[str]] = {}
    for item in violations:
        attribution.setdefault(item.rule, []).append(item.message)
    feedback = attribute_feedback(violations, language=language)
    return GuardReport(
        verdict=verdict,
        violations=violations,
        attribution=attribution,
        metrics=metrics,
        retry_feedback=feedback,
        strict=strict,
    )


_RULE_LABELS = {
    "cn": {
        "numeric.conservation": "数值守恒",
        "numeric.polarity": "极性方向",
        "citation.conservation": "引用守恒",
        "anchor.conservation": "锚点守恒",
        "hedge.flip": "对冲语气",
        "scope.shift": "范围限定",
        "hype.lexicon": "浮夸表述",
        "aiflavor.lexicon": "AI 腔",
        "structure.inflation": "结构/长度",
        "semantic.drift": "语义漂移",
    },
    "en": {
        "numeric.conservation": "numeric conservation",
        "numeric.polarity": "polarity",
        "citation.conservation": "citation conservation",
        "anchor.conservation": "anchor conservation",
        "hedge.flip": "hedge strength",
        "scope.shift": "scope qualifier",
        "hype.lexicon": "hype wording",
        "aiflavor.lexicon": "AI flavor",
        "structure.inflation": "structure/length",
        "semantic.drift": "semantic drift",
    },
}


def attribute_feedback(violations: list[Violation], *, language: str = "cn") -> str:
    """把违例按规则归因为结构化重试指令（供 retry prompt 的 {{feedback}}）。"""
    if not violations:
        return "(none)" if language == "en" else "（无）"
    labels = _RULE_LABELS.get(language, _RULE_LABELS["cn"])
    grouped: dict[str, list[Violation]] = {}
    for item in violations:
        grouped.setdefault(item.rule, []).append(item)
    lines: list[str] = []
    if language == "en":
        lines.append("The previous output failed review. Fix ONLY the following, then re-polish:")
    else:
        lines.append("上一轮输出未通过审查。请仅针对以下问题修正后重新润色：")
    for rule, items in grouped.items():
        label = labels.get(rule, rule)
        errors = [item for item in items if item.severity == "error"]
        warnings = [item for item in items if item.severity == "warning"]
        head = f"- [{label}]" if language == "cn" else f"- [{label}]"
        if errors:
            lines.append(f"{head} {len(errors)} error(s):")
            lines.extend(f"    · {item.message}" for item in errors[:4])
        if warnings:
            lines.append(f"{head} {len(warnings)} warning(s) (confirm or fix):")
            lines.extend(f"    · {item.message}" for item in warnings[:2])
    if language == "en":
        lines.append("Rules: never invent numbers, citations, units or sources absent from the source text; keep hedge strength and polarity unchanged; no hype or AI-flavor wording.")
    else:
        lines.append("总则：严禁新增原稿没有的数字、引用、单位与出处；极性方向与对冲强度必须与原文一致；不得使用浮夸或 AI 腔表述。")
    return "\n".join(lines)
