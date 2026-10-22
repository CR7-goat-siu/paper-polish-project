"""lit-search Skill：文献缺口检测 + 公开免密钥数据源检索 + 候选文献卡片。

能力（五种模式）
- gap：纯离线确定性检测"缺少文献支撑的论断"（信号词 + 无引用标记 + 排除自述句），
  另支持 stale_citation（趋势论断只引旧文献）与 incomplete_entry（参考文献条目缺
  关键元数据），产出 gap 列表与英文检索式草稿；
- search：对每个 gap / query 执行**多轮检索**（broad 宽式 → precise 布尔精确 →
  fresh_cited 近五年+高引排序），调用公开免密钥学术数据源（Crossref / OpenAlex /
  Semantic Scholar / arXiv / Europe PMC / PubMed），归一去重、排序，产出候选条目与文献卡片；
- cards：把候选条目渲染成中文/英文文献卡片（Markdown，含四种引用格式）；
- package：把 gap 渲染成coding harness 作业包（prompts/<lang>/lit-search.md 模板）；
- auto：gap → search → cards 一条龙（润色/审校流程中"主动触发检索"的入口）。

红线：零 API Key。只访问 config/literature.yaml 白名单端点，代码不读取任何密钥；
所有请求经 utils.web_fetch（缓存 + 限速 + 离线模式），原始响应即核验证据。
文献真伪判定不在本 Skill（lit-verify 的职责），但候选条目自带证据记录。
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.citation_check import (  # noqa: E402
    canonical_entry,
    clean_text,
    doi_normalize,
    entry_core_summary,
    format_all_styles,
    format_reference,
    missing_required_fields,
    normalize_title,
    title_similarity,
)
from utils.config_loader import PROJECT_ROOT, load_literature_config, load_terms, normalize_language  # noqa: E402
from utils.fidelity import CITATION_BRACKET_RE  # noqa: E402
from utils.logger import get_logger  # noqa: E402
import utils.web_fetch as web_fetch  # noqa: E402

logger = get_logger("lit-search")

MODES = ("gap", "search", "cards", "package", "auto")
SENTENCE_SPLIT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?", re.UNICODE)
LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-\+\.]{1,}")
EN_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "using", "based",
    "method", "methods", "result", "results", "study", "studies", "paper", "proposed",
    "however", "therefore", "these", "those", "such", "which", "have", "has", "are",
    "was", "were", "can", "may", "also", "more", "most", "than", "then", "when",
}
FIELD_SELECT_CROSSREF = (
    "DOI,title,author,container-title,volume,issue,page,published,type,URL,abstract,is-referenced-by-count"
)
S2_FIELDS = "title,year,venue,externalIds,authors,abstract,citationCount,publicationTypes,journal,url"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
DOI_IN_TEXT_RE = re.compile(r"10\.\d{4,9}/\S+")
YEAR_IN_TEXT_RE = re.compile(r"(1[89]\d{2}|20\d{2})")
PAGES_IN_TEXT_RE = re.compile(r"\bpp?\.\s*\d+|页\s*\d+|\d+\s*[-–—～~]\s*\d+")
REF_ENTRY_LINE_RE = re.compile(r"^\s*\[(\d{1,3})\]\s*(.+)$")
GB_VENUE_MARKER_RE = re.compile(r"\[[JCDMDSRP]\]")
QUOTED_TITLE_RE = re.compile(r"[“\"]([^”\"]{4,})[”\"]|《([^》]{4,})》")
PUBLISHER_RE = re.compile(r"[A-Z][A-Za-z\.]+:\s*[A-Z]")
CITATION_NUMBER_RE = re.compile(r"\d{1,3}")
ROUND_NAMES = ("broad", "precise", "fresh_cited")


# --------------------------------------------------------------------------- #
# 缺口检测（离线确定性）
# --------------------------------------------------------------------------- #


def _sentences(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group().strip()) for m in SENTENCE_SPLIT_RE.finditer(text or "") if m.group().strip()]


def _contains_any(text: str, needles: list[str]) -> list[str]:
    lowered = text.lower()
    hits = []
    for needle in needles:
        needle = str(needle)
        if not needle:
            continue
        if needle.lower() in lowered:
            hits.append(needle)
    return hits


def _latin_terms(sentence: str, limit: int) -> list[str]:
    found: list[str] = []
    for match in LATIN_TOKEN_RE.finditer(sentence):
        token = match.group().strip(".-+")
        if len(token) < 2:
            continue
        if token.lower() in EN_STOPWORDS:
            continue
        if token not in found:
            found.append(token)
        if len(found) >= limit:
            break
    return found


def _terms_index(language: str, domain: str | None) -> list[tuple[str, list[str], str]]:
    """[(中文/英文匹配词, 全部写法, 英文关键词)] —— 供检索式构造。"""
    try:
        data = load_terms(language, domain)
    except Exception as exc:  # noqa: BLE001 - 术语库缺失不应阻断检测
        logger.warning("术语库加载失败，检索式退化为拉丁词：%s", exc)
        return []
    index: list[tuple[str, list[str], str]] = []
    for entry in data.get("entries", []):
        canonical = str(entry.get("canonical") or "").strip()
        if not canonical:
            continue
        variants = [canonical] + [str(x) for x in entry.get("aliases", []) if str(x).strip()]
        english = str(entry.get("en") or "").strip()
        if language == "cn":
            english = english or canonical
        else:
            english = canonical
        index.append((canonical, variants, english))
    return index


_SYNONYMS_CACHE: dict[str, dict[str, list[str]]] = {}


def load_synonyms(config: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """同义词组索引：{小写形式: [同组其他形式]}（文件缺失/损坏时返回空表）。"""
    cfg = config or load_literature_config()
    raw = str((cfg.get("multi_round") or {}).get("synonyms_file") or "data/literature/synonyms.json")
    if raw in _SYNONYMS_CACHE:
        return _SYNONYMS_CACHE[raw]
    index: dict[str, list[str]] = {}
    path = Path(raw)
    path = path if path.is_absolute() else PROJECT_ROOT / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("同义词表加载失败（%s），查询扩展退化为空", exc)
        _SYNONYMS_CACHE[raw] = index
        return index
    for group in data.get("groups") or []:
        if not isinstance(group, dict):
            continue
        forms = [str(group.get("canonical") or "").strip()] + [
            str(item).strip() for item in (group.get("synonyms") or []) if str(item).strip()
        ]
        forms = [form for form in forms if form]
        if len(forms) < 2:
            continue
        for form in forms:
            key = form.lower()
            bucket = index.setdefault(key, [])
            for other in forms:
                if other.lower() != key and other not in bucket:
                    bucket.append(other)
    _SYNONYMS_CACHE[raw] = index
    return index


def expand_terms(terms: list[str], config: dict[str, Any], limit: int | None = None) -> list[str]:
    """用同义词组扩展检索词（保持原序，扩展词附后，总量受 limit 控制）。"""
    synonyms = load_synonyms(config)
    if not synonyms:
        return []
    cap = int(limit if limit is not None else (config.get("multi_round") or {}).get("max_expanded_terms", 8))
    expanded: list[str] = []
    for term in terms:
        for variant in synonyms.get(str(term).lower(), []):
            if variant.lower() in {str(item).lower() for item in terms}:
                continue
            if variant not in expanded:
                expanded.append(variant)
            if len(expanded) >= cap:
                return expanded
    return expanded


def build_keywords(
    sentence: str,
    language: str,
    domain: str | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """从句子构造英文检索式草稿（术语库映射 + 拉丁词 + 同义词扩展 + 领域后缀）。

    产出：
    - terms：基础检索词（术语库英文 + 拉丁词）；
    - expanded_terms：同义词扩展词；
    - query：broad 宽式检索串（基础词 + 扩展词）；
    - boolean：precise 精确式（基础词 AND 连接）。
    """
    keyword_cfg = config.get("keywords") or {}
    limit = int(keyword_cfg.get("max_terms_per_query", 6))
    min_chars = int(keyword_cfg.get("min_term_chars", 2))
    suffix = [str(item) for item in (keyword_cfg.get("domain_suffix") or [])]

    matched_en: list[str] = []
    matched_local: list[str] = []
    for canonical, variants, english in _terms_index(language, domain):
        if any(variant and variant in sentence for variant in variants):
            matched_local.append(canonical)
            if english and english not in matched_en:
                matched_en.append(english)

    latin = _latin_terms(sentence, limit)
    terms: list[str] = []
    for item in matched_en + latin:
        if len(item) < min_chars:
            continue
        if item not in terms:
            terms.append(item)

    base_terms = terms[:limit]
    expanded = expand_terms(base_terms, config)

    query_terms = list(base_terms)
    for extra in expanded:
        if len(query_terms) >= limit + len(expanded):
            break
        if extra not in query_terms:
            query_terms.append(extra)
    for extra in suffix:
        if len(query_terms) >= 2:
            break
        if extra not in query_terms:
            query_terms.append(extra)
    if not query_terms and suffix:
        query_terms = suffix[:1]
    primary = suffix[:1]

    if len(base_terms) >= 2:
        boolean = " AND ".join(f'"{item}"' for item in base_terms)
    elif base_terms:
        boolean = f'"{base_terms[0]}"'
    elif primary:
        boolean = f'"{primary[0]}"'
    else:
        boolean = ""
    if primary and primary[0] not in base_terms and base_terms:
        boolean = f'{boolean} AND "{primary[0]}"' if boolean else f'"{primary[0]}"'
    return {
        "terms": base_terms,
        "expanded_terms": expanded,
        "local_terms": matched_local,
        "query": " ".join(query_terms),
        "boolean": boolean,
        "suffix": primary,
    }


def _parse_citation_numbers(sentence: str) -> list[int]:
    """提取句子中的引用编号（支持 [1]、[1,2]、[1-3]）。"""
    numbers: set[int] = set()
    for marker in CITATION_BRACKET_RE.findall(sentence or ""):
        inner = marker.strip()[1:-1] if marker.startswith("[") else marker
        for token in re.split(r"[,，]", inner):
            token = token.strip()
            if not token:
                continue
            range_match = re.fullmatch(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", token)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                if end >= start and end - start <= 200:
                    numbers.update(range(start, end + 1))
            elif token.isdigit():
                numbers.add(int(token))
    return sorted(numbers)


def parse_reference_entries(references_text: str) -> list[dict[str, Any]]:
    """解析参考文献文本为条目记录（供陈旧引用 / 信息不全检测）。

    识别 `[n] ...` 行；抽取年份 / DOI / 页码；期刊名按 GB/T 标记（[J]/[C]/[M]）、
    《》或引号标题后的片段启发式判断；"出版地: 出版社"视为书籍（不要求页码）。
    不追求完整著录，只服务缺口检测。
    """
    entries: list[dict[str, Any]] = []
    for line in (references_text or "").split("\n"):
        match = REF_ENTRY_LINE_RE.match(line)
        if not match:
            continue
        number = int(match.group(1))
        body = match.group(2).strip()
        if not body:
            continue
        year_match = YEAR_IN_TEXT_RE.search(body)
        doi_match = DOI_IN_TEXT_RE.search(body)
        has_pages = bool(PAGES_IN_TEXT_RE.search(body))
        book_like = bool(GB_VENUE_MARKER_RE.search(body) and "[M]" in body) or bool(PUBLISHER_RE.search(body))
        venue = ""
        gb_match = GB_VENUE_MARKER_RE.search(body)
        if gb_match:
            tail = body[gb_match.end():]
            venue = tail.split(",", 1)[0].split("，", 1)[0].split("。", 1)[0].strip(" .:，。")
        else:
            quoted = QUOTED_TITLE_RE.search(body)
            if quoted:
                tail = body[quoted.end():].lstrip(" ,，.。;；:：")
                segment = re.split(r"[,，]", tail, maxsplit=1)[0]
                if re.search(r"[A-Za-z]", segment) and len(segment) >= 3:
                    venue = segment.strip(" .:，。")
            elif book_like:
                publisher = PUBLISHER_RE.search(body)
                if publisher:
                    venue = body[publisher.start():].split(",", 1)[0].split("，", 1)[0].strip(" .:，。")
        entries.append(
            {
                "number": number,
                "line": body,
                "year": int(year_match.group(1)) if year_match else None,
                "doi": doi_normalize(doi_match.group(0)) if doi_match else "",
                "pages": has_pages,
                "venue": venue,
                "book_like": book_like,
            }
        )
    return entries


def detect_gaps(
    text: str,
    *,
    language: str = "cn",
    domain: str | None = None,
    config: dict[str, Any] | None = None,
    paragraphs: list[str] | None = None,
    audit: dict[str, Any] | None = None,
    references_text: str | None = None,
) -> list[dict[str, Any]]:
    """检测缺少文献支撑的论断 + 引用表缺口 + 陈旧引用 + 信息不全条目（确定性，无网络）。"""
    config = config or load_literature_config()
    language = normalize_language(language)
    gap_cfg = config.get("gap_detection") or {}
    signals_cfg = gap_cfg.get("signals") or {}
    no_citation = [str(item) for item in (gap_cfg.get("no_citation_needed") or [])]
    min_chars = int(gap_cfg.get("min_sentence_chars", 24))
    max_gaps = int(gap_cfg.get("max_gaps", 40))
    max_quote = int(gap_cfg.get("max_quote_chars", 160))
    stale_cfg = gap_cfg.get("stale_citation") or {}
    incomplete_cfg = gap_cfg.get("incomplete_entry") or {}
    current_year = datetime.now(timezone.utc).year

    ref_entries = parse_reference_entries(references_text) if references_text else []
    ref_year_by_number = {item["number"]: item["year"] for item in ref_entries if item.get("year")}

    def _paragraph_index(offset: int) -> int | None:
        if not paragraphs:
            return None
        cursor = 0
        for index, paragraph in enumerate(paragraphs):
            if offset < cursor + len(paragraph) + 1:
                return index
            cursor += len(paragraph) + 1
        return None

    def _append(gap: dict[str, Any]) -> bool:
        gaps.append(gap)
        return len(gaps) >= max_gaps

    gaps: list[dict[str, Any]] = []
    for start, _end, sentence in _sentences(text or ""):
        if len(sentence) < min_chars:
            continue
        if _contains_any(sentence, no_citation):
            continue
        citations = CITATION_BRACKET_RE.findall(sentence)
        matched: dict[str, list[str]] = {}
        for category, words in signals_cfg.items():
            hits = _contains_any(sentence, [str(item) for item in (words or [])])
            if hits:
                matched[category] = hits
        if not matched:
            continue
        keywords = build_keywords(sentence, language, domain, config)
        quote = sentence[:max_quote] + ("…" if len(sentence) > max_quote else "")
        if not citations:
            if _append(
                {
                    "gap_id": f"gap-{len(gaps) + 1:03d}",
                    "gap_type": "missing_citation",
                    "priority": "high" if "absolute_claim" in matched else "medium",
                    "signals": matched,
                    "quote": quote,
                    "offset": start,
                    "paragraph_index": _paragraph_index(start),
                    "keywords": keywords,
                }
            ):
                break
            continue
        # 有引用但全是旧文献的趋势句 → 建议补充前沿研究背景
        if stale_cfg.get("enabled", True) and "trend" in matched and ref_year_by_number:
            window = int(stale_cfg.get("recency_window_years", 5))
            min_needed = int(stale_cfg.get("min_citations", 1))
            cited_numbers = _parse_citation_numbers(sentence)
            known_years = [ref_year_by_number[n] for n in cited_numbers if n in ref_year_by_number]
            if len(known_years) >= min_needed and all(
                year < current_year - window for year in known_years
            ):
                if _append(
                    {
                        "gap_id": f"gap-{len(gaps) + 1:03d}",
                        "gap_type": "stale_citation",
                        "priority": "medium",
                        "signals": {**matched, "stale_years": [str(year) for year in known_years]},
                        "quote": quote,
                        "offset": start,
                        "paragraph_index": _paragraph_index(start),
                        "keywords": keywords,
                    }
                ):
                    break

    # 参考文献条目信息不全：缺年份 / 期刊名 / 页码
    # 规则：有 DOI 的条目不因缺 venue/pages 被标记（DOI 可精确定位）；
    # 书籍（出版地: 出版社 / [M]）不要求页码。
    if incomplete_cfg.get("enabled", True) and ref_entries:
        required = [str(item) for item in (incomplete_cfg.get("required_fields") or ["year", "venue", "pages"])]
        for item in ref_entries:
            pseudo = {
                "doi": item.get("doi") or "",
                "year": item.get("year") or "",
                "pages": "x" if item.get("pages") else "",
                "venue": item.get("venue") or "",
            }
            missing = missing_required_fields(pseudo, required)
            if pseudo["doi"]:
                missing = [field for field in missing if field not in ("venue", "pages")]
            if item.get("book_like") and "pages" in missing:
                missing.remove("pages")
            if not missing:
                continue
            if _append(
                {
                    "gap_id": f"gap-{len(gaps) + 1:03d}",
                    "gap_type": "incomplete_entry",
                    "priority": "medium",
                    "signals": {"missing_fields": missing},
                    "quote": f"[{item['number']}] {item['line'][:max_quote]}",
                    "offset": None,
                    "paragraph_index": None,
                    "keywords": build_keywords(item["line"], language, domain, config),
                    "reference_number": item["number"],
                    "missing_fields": missing,
                }
            ):
                break

    # 引用表缺口：cite-manager audit 的"正文引用但未列出"直接转成待补条目
    for ref_id in (audit or {}).get("cited_not_listed", []) or []:
        if _append(
            {
                "gap_id": f"gap-{len(gaps) + 1:03d}",
                "gap_type": "missing_entry",
                "priority": "high",
                "signals": {"audit": [f"[{ref_id}]"]},
                "quote": f"正文引用了 [{ref_id}]，但参考文献列表中不存在该条目",
                "offset": None,
                "paragraph_index": None,
                "keywords": {"terms": [], "expanded_terms": [], "local_terms": [], "query": "", "boolean": "", "suffix": []},
            }
        ):
            return gaps
    return gaps


# --------------------------------------------------------------------------- #
# 数据源适配（全部为公开免密钥端点）
# --------------------------------------------------------------------------- #


def _recency_filter(recency_years: int | None) -> int | None:
    if not recency_years:
        return None
    return datetime.now(timezone.utc).year - int(recency_years)


def _search_crossref(query: str, *, source_cfg: dict[str, Any], config: dict[str, Any], offline: bool | None, recency_years: int | None, sort: str | None = None) -> list[dict[str, Any]]:
    network_cfg = config.get("network") or {}
    params: dict[str, Any] = {
        "query.bibliographic": query,
        "rows": int(source_cfg.get("rows", 8)),
        "select": FIELD_SELECT_CROSSREF,
    }
    since = _recency_filter(recency_years)
    if since:
        params["filter"] = f"from-pub-date:{since}-01-01"
    if sort:
        params["sort"] = sort
    if network_cfg.get("contact_email"):
        params["mailto"] = network_cfg["contact_email"]
    data, _ = web_fetch.fetch_json(
        str(source_cfg["endpoint"]),
        params,
        config=config,
        min_interval=float(source_cfg.get("min_interval_seconds", 1.0)),
        offline=offline,
    )
    items = ((data or {}).get("message") or {}).get("items") or []
    return [canonical_entry(item, "crossref") for item in items]


def _search_openalex(query: str, *, source_cfg: dict[str, Any], config: dict[str, Any], offline: bool | None, recency_years: int | None, sort: str | None = None) -> list[dict[str, Any]]:
    network_cfg = config.get("network") or {}
    params: dict[str, Any] = {
        "search": query,
        "per-page": int(source_cfg.get("rows", 8)),
    }
    since = _recency_filter(recency_years)
    if since:
        params["filter"] = f"from_publication_date:{since}-01-01"
    if sort:
        params["sort"] = sort
    if network_cfg.get("contact_email"):
        params["mailto"] = network_cfg["contact_email"]
    data, _ = web_fetch.fetch_json(
        str(source_cfg["endpoint"]),
        params,
        config=config,
        min_interval=float(source_cfg.get("min_interval_seconds", 1.0)),
        offline=offline,
    )
    items = (data or {}).get("results") or []
    return [canonical_entry(item, "openalex") for item in items]


def _search_semanticscholar(query: str, *, source_cfg: dict[str, Any], config: dict[str, Any], offline: bool | None, recency_years: int | None, sort: str | None = None) -> list[dict[str, Any]]:
    network_cfg = config.get("network") or {}
    params: dict[str, Any] = {
        "query": query,
        "limit": int(source_cfg.get("rows", 6)),
        "fields": S2_FIELDS,
    }
    since = _recency_filter(recency_years)
    if since:
        params["year"] = f"{since}-"
    # 共享出口 IP 下 Graph search 经常 429；优先使用同一公开服务的 bulk
    # 端点，失败时再回退到普通 search 端点。两者都不需要任何 Key。
    endpoints = [
        str(source_cfg.get("bulk_endpoint") or ""),
        str(source_cfg.get("endpoint") or ""),
    ]
    data: dict[str, Any] = {}
    last_error: Exception | None = None
    for endpoint in [item for item in endpoints if item]:
        try:
            data, _ = web_fetch.fetch_json(
                endpoint,
                params,
                config=config,
                min_interval=float(source_cfg.get("min_interval_seconds", 1.5)),
                offline=offline,
            )
            break
        except Exception as exc:  # noqa: BLE001 - 同源端点互相兜底
            last_error = exc
            logger.warning("Semantic Scholar 端点失败，尝试同源兜底: %s (%s)", endpoint, exc)
    if not data:
        if last_error is not None:
            raise last_error
        return []
    results = []
    items = (data or {}).get("data") or (data or {}).get("results") or []
    for item in items[: int(source_cfg.get("rows", 6))]:
        record = dict(item)
        external = record.get("externalIds") or {}
        if external.get("DOI"):
            record["doi"] = external["DOI"]
        journal = record.get("journal") or {}
        if isinstance(journal, dict) and not record.get("venue"):
            record["venue"] = journal.get("name") or ""
        results.append(canonical_entry(record, "semanticscholar"))
    return results


def _search_arxiv(query: str, *, source_cfg: dict[str, Any], config: dict[str, Any], offline: bool | None, recency_years: int | None, sort: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "search_query": f'all:"{query}"',
        "start": 0,
        "max_results": int(source_cfg.get("rows", 6)),
        "sortBy": sort or "relevance",
    }
    result = web_fetch.fetch_text(
        str(source_cfg["endpoint"]),
        params,
        config=config,
        min_interval=float(source_cfg.get("min_interval_seconds", 3.0)),
        offline=offline,
        accept="application/atom+xml",
    )
    try:
        from xml.etree import ElementTree as ET

        root = ET.fromstring(result.body or "<feed/>")
    except Exception as exc:  # noqa: BLE001 - 上游返回异常时降级为空结果
        logger.warning("arXiv 响应解析失败: %s", exc)
        return []
    entries = []
    for node in root.findall(f"{ATOM_NS}entry"):
        entries.append(
            canonical_entry(
                {
                    "title": (node.findtext(f"{ATOM_NS}title") or "").strip(),
                    "authors": [
                        name.text.strip()
                        for name in node.findall(f"{ATOM_NS}author/{ATOM_NS}name")
                        if name.text
                    ],
                    "published": node.findtext(f"{ATOM_NS}published") or "",
                    "id": node.findtext(f"{ATOM_NS}id") or "",
                    "url": node.findtext(f"{ATOM_NS}id") or "",
                    "doi": (node.findtext("{http://arxiv.org/schemas/atom}doi") or "").strip(),
                    "venue": (node.findtext("{http://arxiv.org/schemas/atom}journal_ref") or "").strip(),
                },
                "arxiv",
            )
        )
    return entries


def _search_europepmc(query: str, *, source_cfg: dict[str, Any], config: dict[str, Any], offline: bool | None, recency_years: int | None, sort: str | None = None) -> list[dict[str, Any]]:
    since = _recency_filter(recency_years)
    final_query = f"({query}) AND (FIRST_PDATE:[{since}-01-01 TO 2100-01-01])" if since else query
    params: dict[str, Any] = {
        "query": final_query,
        "format": "json",
        "pageSize": int(source_cfg.get("rows", 6)),
        "resultType": "core",
    }
    if sort:
        params["sort"] = sort
    data, _ = web_fetch.fetch_json(
        str(source_cfg["endpoint"]),
        params,
        config=config,
        min_interval=float(source_cfg.get("min_interval_seconds", 0.5)),
        offline=offline,
    )
    results = []
    for item in ((data or {}).get("resultList") or {}).get("result") or []:
        journal = ((item.get("journalInfo") or {}).get("journal") or {}) if isinstance(item.get("journalInfo"), dict) else {}
        results.append(
            canonical_entry(
                {
                    "title": item.get("title"),
                    "authors": item.get("authorString"),
                    "pubYear": item.get("pubYear"),
                    "venue": journal.get("title") if isinstance(journal, dict) else "",
                    "journal": journal.get("title") if isinstance(journal, dict) else "",
                    "volume": ((item.get("journalInfo") or {}).get("volume") or ""),
                    "issue": ((item.get("journalInfo") or {}).get("issue") or ""),
                    "page": item.get("pageInfo"),
                    "doi": item.get("doi"),
                    "id": item.get("id") or item.get("pmid"),
                    "cited_by_count": item.get("citedByCount"),
                    "abstract": item.get("abstractText"),
                },
                "europepmc",
            )
        )
    return results


def _search_pubmed(query: str, *, source_cfg: dict[str, Any], config: dict[str, Any], offline: bool | None, recency_years: int | None, sort: str | None = None) -> list[dict[str, Any]]:
    """PubMed（NCBI E-utilities，免密钥）：esearch 取 id → esummary 取元数据。"""
    rows = int(source_cfg.get("rows", 6))
    since = _recency_filter(recency_years)
    search_params: dict[str, Any] = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": rows,
        "sort": sort or "relevance",
    }
    if since:
        search_params.update({"mindate": f"{since}/01/01", "maxdate": "3000/01/01", "datetype": "pdat"})
    data, _ = web_fetch.fetch_json(
        str(source_cfg["endpoint"]),
        search_params,
        config=config,
        min_interval=float(source_cfg.get("min_interval_seconds", 1.0)),
        offline=offline,
    )
    ids = [str(item) for item in ((data or {}).get("esearchresult") or {}).get("idlist") or []]
    if not ids:
        return []
    summary, _ = web_fetch.fetch_json(
        str(source_cfg.get("summary_endpoint") or "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"),
        {"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
        config=config,
        min_interval=float(source_cfg.get("min_interval_seconds", 1.0)),
        offline=offline,
    )
    result = (summary or {}).get("result") or {}
    entries = []
    for uid in result.get("uids") or []:
        item = result.get(str(uid)) or {}
        if not isinstance(item, dict) or not item.get("title"):
            continue
        authors: list[dict[str, str]] = []
        for author in item.get("authors") or []:
            name = str((author or {}).get("name") if isinstance(author, dict) else author or "").strip()
            parts = name.split()
            if len(parts) >= 2:
                authors.append({"family": parts[0], "given": " ".join(parts[1:])})
            elif parts:
                authors.append({"family": parts[0]})
        entries.append(
            canonical_entry(
                {
                    "title": item.get("title"),
                    "authors": authors,
                    "pubdate": item.get("pubdate") or item.get("epubdate") or "",
                    "fulljournalname": item.get("fulljournalname") or item.get("source") or "",
                    "volume": item.get("volume") or "",
                    "issue": item.get("issue") or "",
                    "pages": item.get("pages") or "",
                    "elocationid": item.get("elocationid") or [],
                    "articleids": item.get("articleids") or [],
                    "uid": uid,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                },
                "pubmed",
            )
        )
    return entries


SEARCHERS = {
    "crossref": _search_crossref,
    "openalex": _search_openalex,
    "semanticscholar": _search_semanticscholar,
    "arxiv": _search_arxiv,
    "europepmc": _search_europepmc,
    "pubmed": _search_pubmed,
}


def _enabled_sources(config: dict[str, Any], only: list[str] | None = None) -> list[tuple[str, dict[str, Any]]]:
    sources = config.get("sources") or {}
    selected = []
    for name, source_cfg in sources.items():
        if not isinstance(source_cfg, dict) or not source_cfg.get("enabled", True):
            continue
        kind = str(source_cfg.get("kind") or name)
        if kind not in SEARCHERS:
            logger.warning("未知数据源 kind=%s（%s），已跳过", kind, name)
            continue
        if only and name not in only and kind not in only:
            continue
        selected.append((name, source_cfg))
    return selected


def _round_variants(query: str, keywords: dict[str, Any] | None) -> list[tuple[str, str]]:
    """单条 query 的多轮检索式变体：[(round_name, query_string)]。

    - broad：宽式（关键词直接拼接 / 检索式原串）；
    - precise：布尔 AND 精确式（来自 keywords.boolean，或对裸检索串按词 AND）；
    - fresh_cited：与 broad 同串，由调用方附加排序参数走"近五年 + 高引"。
    """
    variants: list[tuple[str, str]] = []
    query = str(query or "").strip()
    if not query:
        return variants
    variants.append(("broad", query))
    precise = ""
    if isinstance(keywords, dict):
        precise = str(keywords.get("boolean") or "").strip()
    if not precise:
        tokens = [token for token in re.split(r"\s+", query) if token]
        if len(tokens) >= 2:
            precise = " AND ".join(f'"{token}"' for token in tokens)
        elif tokens:
            precise = f'"{tokens[0]}"'
    if precise and precise != query:
        variants.append(("precise", precise))
    variants.append(("fresh_cited", query))
    return variants


def _rounds_from_config(config: dict[str, Any]) -> list[str]:
    cfg = config.get("multi_round") or {}
    if not cfg.get("enabled", True):
        return ["broad"]
    rounds: list[str] = []
    for key, name in (("round_broad", "broad"), ("round_precise", "precise"), ("round_ranked", "fresh_cited")):
        value = str(cfg.get(key) or name).strip()
        if value and value != "off" and value not in rounds:
            rounds.append(value)
    max_rounds = int(cfg.get("max_rounds", 3))
    return rounds[:max_rounds] or ["broad"]


# --------------------------------------------------------------------------- #
# 排序 / 去重 / 卡片
# --------------------------------------------------------------------------- #


def _merge_entries(pairs: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """把 (检索式, 条目) 对合并为候选条目：按 DOI / 标题去重，聚合证据与命中检索式。"""
    merged: dict[str, dict[str, Any]] = {}
    for query, entry in pairs:
        if not isinstance(entry, dict):
            continue
        key = entry.get("doi") or normalize_title(entry.get("title"))
        if not key:
            continue
        record = merged.get(key)
        if record is None:
            record = dict(entry)
            record["evidence"] = [entry]
            record["queries"] = [query] if query else []
            merged[key] = record
            continue
        seen = {(item.get("source"), item.get("source_id")) for item in record["evidence"]}
        if (entry.get("source"), entry.get("source_id")) not in seen:
            record["evidence"].append(entry)
        if query and query not in record["queries"]:
            record["queries"].append(query)
        for field in ("abstract", "url", "venue", "volume", "issue", "pages", "type"):
            if not record.get(field) and entry.get(field):
                record[field] = entry[field]
        if not record.get("year") and entry.get("year"):
            record["year"] = entry["year"]
        record["cited_by"] = max(int(record.get("cited_by") or 0), int(entry.get("cited_by") or 0))
    return list(merged.values())


def _score(entry: dict[str, Any], query: str, config: dict[str, Any]) -> float:
    ranking = config.get("ranking") or {}
    weights = ranking.get("weights") or {}
    window = int(ranking.get("recency_window_years", 5))
    sources = config.get("sources") or {}
    source_cfg = sources.get(str(entry.get("source")) or "", {}) or {}
    source_score = float(source_cfg.get("weight", 0.8))
    match = title_similarity(query, entry.get("title"))
    year = entry.get("year")
    current = datetime.now(timezone.utc).year
    recency = 0.0
    if isinstance(year, int) and year > 1800:
        recency = max(0.0, min(1.0, 1.0 - (current - year) / max(window, 1)))
    cited = int(entry.get("cited_by") or 0)
    cited_score = min(1.0, math.log1p(max(cited, 0)) / math.log1p(1000))
    return round(
        float(weights.get("source_weight", 1.0)) * source_score
        + float(weights.get("title_match", 1.5)) * match
        + float(weights.get("recency", 0.8)) * recency
        + float(weights.get("cited_by", 0.3)) * cited_score,
        4,
    )


def search(
    queries: list[str],
    *,
    language: str = "cn",
    domain: str | None = None,
    config: dict[str, Any] | None = None,
    sources: list[str] | None = None,
    top_k: int = 8,
    offline: bool | None = None,
    force_refresh: bool = False,
    keywords_by_query: dict[str, dict[str, Any]] | None = None,
    rounds: list[str] | None = None,
) -> dict[str, Any]:
    """多轮多源检索 + 跨检索式去重排序，返回 {candidates, source_stats, round_stats, network}。

    rounds 默认取 config/literature.yaml → multi_round（broad / precise / fresh_cited）；
    fresh_cited 轮按源附加排序参数（被引数 / 提交日期），实现"近五年 + 高引"筛选。
    """
    config = config or load_literature_config()
    recency_years = int((config.get("keywords") or {}).get("recency_filter_years", 0)) or None
    highly_cited_min = int((config.get("keywords") or {}).get("highly_cited_min", 0) or 0)
    ranked_sort = (config.get("multi_round") or {}).get("ranked_sort") or {}
    effective_rounds = rounds or _rounds_from_config(config)
    source_stats: dict[str, dict[str, Any]] = {}
    round_stats: list[dict[str, Any]] = []
    pairs: list[tuple[str, dict[str, Any]]] = []
    effective_queries = [str(item).strip() for item in queries if str(item).strip()]
    keywords_by_query = keywords_by_query or {}

    for round_name in effective_rounds:
        round_hits = 0
        round_pairs: list[tuple[str, dict[str, Any]]] = []
        for query in effective_queries:
            variants = dict(_round_variants(query, keywords_by_query.get(query)))
            if round_name not in variants:
                continue
            round_query = variants[round_name]
            for name, source_cfg in _enabled_sources(config, sources):
                kind = str(source_cfg.get("kind") or name)
                stats = source_stats.setdefault(name, {"kind": kind, "queries": 0, "hits": 0, "errors": []})
                sort = str(ranked_sort.get(name) or ranked_sort.get(kind) or "") if round_name == "fresh_cited" else ""
                try:
                    found = SEARCHERS[kind](
                        round_query,
                        source_cfg=source_cfg,
                        config=config,
                        offline=offline,
                        recency_years=recency_years,
                        sort=sort or None,
                    )
                except web_fetch.NetworkDisabledError as exc:
                    stats["errors"].append(f"offline: {exc}")
                    continue
                except Exception as exc:  # noqa: BLE001 - 单源失败不影响整体检索
                    stats["errors"].append(str(exc))
                    logger.warning("数据源 %s 第 %s 轮检索失败 query=%r err=%s", name, round_name, round_query, exc)
                    continue
                if round_name == "fresh_cited" and highly_cited_min > 0:
                    found = [item for item in found if int(item.get("cited_by") or 0) >= highly_cited_min]
                stats["queries"] += 1
                stats["hits"] += len(found)
                round_hits += len(found)
                round_pairs.extend((round_query, item) for item in found)
        round_stats.append({"round": round_name, "hits": round_hits, "queries": len(effective_queries)})
        pairs.extend(round_pairs)

    candidates: list[dict[str, Any]] = []
    for record in _merge_entries(pairs):
        record_queries = list(record.get("queries") or [""])
        best_query = max(record_queries, key=lambda item: _score(record, item, config))
        record = dict(record)
        record["query"] = best_query
        record["rank_score"] = _score(record, best_query, config)
        candidates.append(record)

    candidates.sort(key=lambda item: (-float(item.get("rank_score") or 0), -(item.get("year") or 0)))
    limit = max(top_k, 1) * max(len(effective_queries), 1)
    return {
        "candidates": candidates[:limit],
        "source_stats": source_stats,
        "round_stats": round_stats,
        "network": {"stats": web_fetch.stats(), "cache": web_fetch.cache_stats(config)},
    }


def render_cards(candidates: list[dict[str, Any]], *, language: str = "cn", style: str = "ieee") -> str:
    """候选文献卡片：元数据 + 核心结论摘要 + 推荐理由 + 四种引用格式。"""
    language = normalize_language(language)
    title = "候选文献卡片" if language == "cn" else "Candidate reference cards"
    lines = [f"# {title}", ""]
    if not candidates:
        lines.append("（无候选文献）" if language == "cn" else "(no candidates)")
        lines.append("")
        return "\n".join(lines)
    for index, item in enumerate(candidates, start=1):
        reference = format_reference(item, style, index)
        styles = format_all_styles(item)
        lines.append(f"## {index}. {clean_text(item.get('title')) or '(untitled)'}")
        lines.append("")
        lines.append(f"- 引用格式（{style}）：{reference}")
        lines.append(f"- 年份：{item.get('year') or '未知'}　期刊/会议：{clean_text(item.get('venue')) or '未知'}")
        lines.append(f"- DOI：{item.get('doi_display') or item.get('doi') or '无'}　来源：{item.get('source') or '-'}")
        lines.append(f"- 排序得分：{item.get('rank_score')}　被引：{item.get('cited_by') or 0}")
        summary = entry_core_summary(item)
        if summary:
            lines.append(f"- 核心结论摘要：{summary}")
        if item.get("why_relevant"):
            lines.append(f"- 推荐引用理由：{clean_text(item['why_relevant'])}")
        if item.get("evidence_quote"):
            lines.append(f"- 证据原句：{clean_text(item['evidence_quote'])}")
        if item.get("query"):
            lines.append(f"- 命中检索式：{item.get('query')}")
        lines.append("- 四种规范格式：")
        for name, text in styles.items():
            lines.append(f"  - {name}: {text}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# run(args) 契约
# --------------------------------------------------------------------------- #


def _read_text_arg(args: dict[str, Any]) -> str:
    if args.get("text"):
        return str(args["text"])
    if args.get("text_file"):
        return Path(str(args["text_file"])).read_text(encoding="utf-8")
    return ""


def _load_json(path: Any, default: Any) -> Any:
    if not path:
        return default
    target = Path(str(path))
    if not target.exists():
        return default
    return json.loads(target.read_text(encoding="utf-8"))


def _write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _references_text(args: dict[str, Any]) -> str:
    if args.get("references"):
        return str(args["references"])
    if args.get("references_file"):
        return Path(str(args["references_file"])).read_text(encoding="utf-8")
    return ""


def _queries_from_gaps(gaps: list[dict[str, Any]]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """从缺口取 broad 检索式与关键词（兼容旧调用方）。"""
    queries: list[str] = []
    query_map: dict[str, dict[str, Any]] = {}
    for gap in gaps or []:
        keywords = gap.get("keywords") or {}
        query = str(keywords.get("query") or "").strip()
        if query:
            queries.append(query)
            query_map.setdefault(query, keywords)
    return queries, query_map


def _gap_query_variants(gaps: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """[(检索式变体, gap, keywords)] —— broad 与 precise 变体都登记到所属缺口。

    多轮检索下候选的命中检索式可能是精确式，只有同时登记两个变体，
    才能把候选正确关联回缺口（gap_id / anchor）。
    """
    variants: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for gap in gaps or []:
        keywords = gap.get("keywords") or {}
        broad = str(keywords.get("query") or "").strip()
        precise = str(keywords.get("boolean") or "").strip()
        if broad:
            variants.append((broad, gap, keywords))
        if precise and precise != broad:
            variants.append((precise, gap, keywords))
    return variants


def run(args: dict[str, Any]) -> dict[str, Any]:
    """lit-search 入口。

    参数
    - mode：gap / search / cards / package / auto（默认 gap）
    - text / text_file；language（cn/en）；domain（auto 可）
    - paragraphs：段落文本列表（gap 用于定位段落下标）
    - references / references_file：参考文献文本（陈旧引用与信息不全检测）
    - audit：cite-manager audit 结果（把"正文引用但未列出"转成待补条目）
    - gaps / gaps_file：search / package 的输入缺口；queries：直接给检索式
    - sources：限定数据源名列表；top_k；offline：强制离线（只用缓存）
    - rounds：多轮检索轮次（默认取 config multi_round）
    - style：引用格式（ieee/apa/gbt7714/bibtex）；out：产物目录前缀
    """
    mode = str(args.get("mode") or "gap").strip().lower()
    if mode not in MODES:
        raise ValueError(f"未知 mode: {mode!r}，可用 {list(MODES)}")
    language = normalize_language(args.get("language") or "cn")
    domain = args.get("domain")
    if domain in ("auto", ""):
        domain = None
    config = load_literature_config()
    style = str(args.get("style") or (config.get("styles") or {}).get("default") or "ieee")
    out_dir = Path(str(args.get("out") or "output/lit-search"))
    offline = args.get("offline")
    offline = None if offline is None else bool(offline)
    force_refresh = bool(args.get("force_refresh"))
    top_k = int(args.get("top_k") or 8)
    web_fetch.reset_stats()

    result: dict[str, Any] = {
        "skill": "lit-search",
        "mode": mode,
        "language": language,
        "style": style,
        "files": {},
        "warnings": [],
    }

    if mode in ("gap", "package", "auto"):
        gaps = args.get("gaps") or _load_json(args.get("gaps_file"), [])
        if not gaps:
            text = _read_text_arg(args)
            gaps = detect_gaps(
                text,
                language=language,
                domain=domain,
                config=config,
                paragraphs=args.get("paragraphs"),
                audit=args.get("audit"),
                references_text=_references_text(args),
            )
        result["gaps"] = gaps
        result["gap_count"] = len(gaps)
        result["files"]["gaps"] = _write(out_dir / "lit-gaps.json", json.dumps(gaps, ensure_ascii=False, indent=2))
        if mode == "package":
            from utils.prompt_renderer import render_template

            prompts = []
            for gap in gaps:
                prompt = render_template(
                    "lit-search.md",
                    language,
                    gap=gap,
                    title=str(args.get("title") or ""),
                    sources=sorted((config.get("sources") or {}).keys()),
                    style=style,
                )
                job_id = f"lit-{gap.get('gap_id', 'gap')}"
                _write(out_dir / "lit-prompts" / f"{job_id}.prompt.txt", prompt)
                prompts.append({"job_id": job_id, "gap_id": gap.get("gap_id"), "prompt_file": str(out_dir / "lit-prompts" / f"{job_id}.prompt.txt")})
            result["jobs"] = prompts
            result["files"]["jobs"] = _write(out_dir / "lit-jobs.json", json.dumps(prompts, ensure_ascii=False, indent=2))
            return result
        if mode == "gap":
            return result

    if mode == "cards":
        candidates = args.get("candidates") or _load_json(args.get("candidates_file"), [])
        markdown = render_cards(candidates, language=language, style=style)
        result["markdown"] = markdown
        result["files"]["cards"] = _write(out_dir / "lit-cards.md", markdown)
        return result

    # search / auto：多源多轮检索
    gaps = args.get("gaps") or _load_json(args.get("gaps_file"), [])
    if not gaps and mode == "auto":
        gaps = result.get("gaps") or []
    queries: list[str] = []
    query_map: dict[str, dict[str, Any]] = {}
    variant_to_gap: dict[str, dict[str, Any]] = {}
    for query in args.get("queries") or []:
        query = str(query).strip()
        if query:
            queries.append(query)
    for variant, gap, keywords in _gap_query_variants(gaps):
        queries.append(variant)
        query_map.setdefault(variant, keywords)
        variant_to_gap[variant] = gap
    if args.get("text") or args.get("text_file"):
        text = _read_text_arg(args)
        detected = detect_gaps(
            text,
            language=language,
            domain=domain,
            config=config,
            paragraphs=args.get("paragraphs"),
            audit=args.get("audit"),
            references_text=_references_text(args),
        )
        if mode == "auto":
            result["gaps"] = detected
            result["gap_count"] = len(detected)
        for variant, gap, keywords in _gap_query_variants(detected):
            queries.append(variant)
            query_map.setdefault(variant, keywords)
            variant_to_gap[variant] = gap
    if not queries:
        if mode == "auto":
            result["candidates"] = []
            result["candidate_count"] = 0
            result["source_stats"] = {}
            result["files"]["cards"] = _write(
                out_dir / "lit-cards.md", render_cards([], language=language, style=style)
            )
            return result
        raise ValueError("search 模式需要 queries / gaps / text 之一，且需能构造出英文检索式")

    seen: set[str] = set()
    unique_queries = [item for item in queries if not (item in seen or seen.add(item))]
    outcome = search(
        unique_queries,
        language=language,
        domain=domain,
        config=config,
        sources=args.get("sources"),
        top_k=top_k,
        offline=offline,
        force_refresh=force_refresh,
        keywords_by_query=query_map,
        rounds=args.get("rounds"),
    )
    candidates = []
    for item in outcome["candidates"]:
        record = dict(item)
        keywords = query_map.get(str(record.get("query")))
        gap = variant_to_gap.get(str(record.get("query")))
        if gap:
            record["gap_id"] = gap.get("gap_id")
            record["anchor"] = gap.get("quote")
        elif keywords is not None:
            record["keywords_used"] = keywords
        candidates.append(record)
    result.update(
        {
            "queries": unique_queries,
            "candidates": candidates,
            "candidate_count": len(candidates),
            "source_stats": outcome["source_stats"],
            "round_stats": outcome.get("round_stats") or [],
            "network": outcome["network"],
        }
    )
    result["files"]["candidates"] = _write(out_dir / "lit-candidates.json", json.dumps(candidates, ensure_ascii=False, indent=2))
    if args.get("cards", True):
        result["files"]["cards"] = _write(out_dir / "lit-cards.md", render_cards(candidates, language=language, style=style))
    return result
