"""参考文献元数据工具（纯离线、只用标准库）。

供 lit-search / lit-verify / cite-manager 共用，是"防幻觉核验"的判定底座：
- 标题 / 期刊名 / 作者姓氏集合的归一与相似度；
- DOI 规范校验与前缀提取；
- 多源记录（Crossref / OpenAlex / Semantic Scholar / arXiv / Europe PMC）
  统一为 canonical entry；
- 候选条目与证据记录的逐字段比对（findings）；
- IEEE / APA 7 / GB/T 7714 / BibTeX 四种引用格式化。

约束：本模块不发任何网络请求，也不读取任何 Key。
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Iterable

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?")
_BRACE_RE = re.compile(r"[{}]")
_PUNCT_RE = re.compile(r"[^\w\s\u4e00-\u9fff]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]{1,80}>")
_YEAR_RE = re.compile(r"(1[89]\d{2}|20\d{2})")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_ARXIV_ID_RE = re.compile(
    r"(?i)(?:arxiv:)?(?:https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/)?"
    r"(\d{4}\.\d{4,5})(?:v\d+)?"
)

_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #


def clean_text(value: Any) -> str:
    """去掉 HTML/XML 标签、LaTeX 命令与花括号，压缩空白。"""
    text = str(value or "")
    text = _TAG_RE.sub(" ", text)
    text = _LATEX_CMD_RE.sub(" ", text)
    text = _BRACE_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def normalize_title(title: Any) -> str:
    """标题归一：小写、去标点、压缩空白，便于跨源比对。"""
    text = clean_text(title).lower()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def likely_venue_abbreviation(short: Any, long: Any) -> bool:
    """判断 short 是否像是 long 的缩写（如 "IEEE TNSRE" vs 全称）。

    缩写无法用字符串相似度对齐，命中时把 venue 不一致降级为 warning（待人工确认），
    而不是直接判为伪造；完全无关的期刊名仍然判 error。
    """
    a, b = normalize_venue(short), normalize_venue(long)
    if not a or not b:
        return False
    a_tokens, b_tokens = a.split(), b.split()
    if not a_tokens or len(a_tokens) >= len(b_tokens):
        return False
    matched = 0
    for token in a_tokens:
        for other in b_tokens:
            if token == other or (len(token) >= 4 and other.startswith(token)) or (
                len(other) >= 4 and token.startswith(other)
            ):
                matched += 1
                break
    return matched >= 1 and len(a_tokens) <= 5


def normalize_venue(venue: Any) -> str:
    text = clean_text(venue).lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    for noise in ("the ", "proceedings of the ", "proceedings of ", "journal of "):
        if text.startswith(noise):
            text = text[len(noise) :]
    return text.strip()


def _tokens(text: str) -> list[str]:
    return [token for token in text.split(" ") if token]


def title_similarity(left: Any, right: Any) -> float:
    """标题相似度 0..1：Jaccard 与序列相似度取较大值（对词序变化更稳健）。"""
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    set_a, set_b = set(_tokens(a)), set(_tokens(b))
    union = set_a | set_b
    jaccard = len(set_a & set_b) / len(union) if union else 0.0
    sequence = difflib.SequenceMatcher(None, a, b).ratio()
    containment = 0.0
    if set_a and set_b and (set_a <= set_b or set_b <= set_a):
        containment = len(set_a & set_b) / min(len(set_a), len(set_b)) * 0.95
    return round(max(jaccard, sequence, containment), 4)


def venue_similarity(left: Any, right: Any) -> float:
    a, b = normalize_venue(left), normalize_venue(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)


def doi_normalize(doi: Any) -> str:
    """提取并规范 DOI：去 URL 前缀、去尾标点、统一小写。"""
    text = str(doi or "").strip()
    if not text:
        return ""
    text = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"(?i)^doi:\s*", "", text)
    text = text.strip().strip(".,;)]}\"'")
    return text.lower()


def doi_display(doi: Any) -> str:
    """按发布方原始大小写保留 DOI（用于输出），仅去掉 URL 前缀与尾标点。"""
    text = str(doi or "").strip()
    text = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"(?i)^doi:\s*", "", text)
    return text.strip().strip(".,;)]}\"'")


def doi_is_valid(doi: Any) -> bool:
    return bool(DOI_RE.match(doi_normalize(doi)))


def doi_prefix(doi: Any) -> str:
    normalized = doi_normalize(doi)
    if "/" not in normalized:
        return ""
    return normalized.split("/", 1)[0]


def extract_arxiv_id(value: Any) -> str:
    """从 arXiv URL / `arXiv:xxxx.xxxxx` / 裸 ID 中提取稳定 ID（不含版本号）。"""
    match = _ARXIV_ID_RE.search(str(value or ""))
    return match.group(1) if match else ""


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def extract_year(record: Any) -> int | None:
    """从 dict / list / 字符串中抽取 plausible 年份（深度受限的深度优先）。"""
    if isinstance(record, bool):
        return None
    if isinstance(record, int):
        return record if 1800 < record < 2200 else None
    if isinstance(record, str):
        match = _YEAR_RE.search(record)
        return int(match.group(1)) if match else None
    if isinstance(record, dict):
        for key in (
            "year",
            "publication_year",
            "published_year",
            "pubYear",
            "publicationYear",
            "published",
            "issued",
            "date",
            "publication_date",
            "published-print",
            "published-online",
            "date-parts",
        ):
            if key in record:
                year = extract_year(record[key])
                if year:
                    return year
        for value in record.values():
            year = extract_year(value)
            if year:
                return year
        return None
    if isinstance(record, (list, tuple)):
        for item in record:
            year = extract_year(item)
            if year:
                return year
        return None
    return None


def _split_author(author: Any) -> str:
    if isinstance(author, dict):
        family = author.get("family") or author.get("lastName") or author.get("familyName")
        if family:
            return str(family)
        literal = author.get("literal") or author.get("name") or author.get("display_name")
        author = literal or ""
    text = clean_text(author)
    if not text:
        return ""
    if "," in text:
        return text.split(",", 1)[0].strip()
    parts = [part for part in text.split(" ") if part]
    while parts and parts[-1].lower() in _SUFFIXES:
        parts.pop()
    return parts[-1] if parts else ""


def author_surnames(authors: Any) -> set[str]:
    """作者姓氏集合（小写）。支持字符串列表、dict 列表、逗号/分号分隔的整串。"""
    if not authors:
        return set()
    if isinstance(authors, str):
        if re.search(r"[;；]", authors):
            raw = re.split(r"[;；]", authors)
        else:
            raw = authors.split(",")
        items: list[Any] = [part for part in raw if part.strip()]
    elif isinstance(authors, (list, tuple)):
        items = list(authors)
    else:
        items = [authors]
    surnames = set()
    for item in items:
        surname = _split_author(item).lower()
        surname = _PUNCT_RE.sub("", surname)
        if len(surname) < 2:  # 过滤 "O." / "Y." 这类缩写首字母噪声
            continue
        surnames.add(surname)
    return surnames


def author_overlap(left: Any, right: Any) -> float:
    """姓氏集合覆盖率：交集 / 较小集合大小（任一方为空返回 0）。"""
    set_a, set_b = author_surnames(left), author_surnames(right)
    if not set_a or not set_b:
        return 0.0
    return round(len(set_a & set_b) / min(len(set_a), len(set_b)), 4)


# --------------------------------------------------------------------------- #
# 多源记录 → canonical entry
# --------------------------------------------------------------------------- #


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value


def _pages(first_page: Any, last_page: Any) -> str:
    first = clean_text(_first(first_page))
    last = clean_text(_first(last_page))
    if first and last and first != last:
        return f"{first}\u2013{last}"
    return first or last


def canonical_entry(record: dict[str, Any], source: str = "") -> dict[str, Any]:
    """把任意来源的记录整理成统一结构（缺字段留空字符串，不抛错）。"""
    data = dict(record or {})
    doi_raw = data.get("doi") or data.get("DOI") or ""
    if not doi_raw:
        # PubMed esummary：elocationid / articleids 列表里的 doi 项
        for key in ("elocationid", "articleids"):
            items = data.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and str(item.get("idtype") or item.get("type") or "").lower() == "doi":
                        doi_raw = item.get("value") or ""
                        break
                    if isinstance(item, str) and item.strip().lower().startswith("10."):
                        doi_raw = item
                        break
            if doi_raw:
                break
    doi = doi_normalize(doi_raw)
    title = clean_text(
        _first(data.get("title"))
        or data.get("name")
        or data.get("display_name")
        or data.get("title_display")
        or ""
    )
    venue_value = (
        _first(data.get("container-title"))
        or _first(data.get("container_title"))
        or data.get("venue")
        or data.get("journal")
        or data.get("publicationVenue")
        or data.get("journal_name")
        or data.get("fulljournalname")
        or data.get("primary_location_venue")
        or ""
    )
    if isinstance(venue_value, dict):
        venue_value = venue_value.get("name") or venue_value.get("display_name") or ""
    external_ids = data.get("externalIds") if isinstance(data.get("externalIds"), dict) else {}
    arxiv_raw = (
        data.get("arxiv_id")
        or data.get("arxivId")
        or external_ids.get("ArXiv")
        or external_ids.get("arXiv")
        or ""
    )
    if not arxiv_raw and str(data.get("source") or source).lower() == "arxiv":
        arxiv_raw = data.get("source_id") or data.get("id") or data.get("url") or ""
    arxiv_id = extract_arxiv_id(arxiv_raw)
    authors_raw = data.get("authors") or data.get("author") or data.get("authorships") or []
    if data.get("authorships") and isinstance(data["authorships"], list):
        authors_raw = [
            (item.get("author") or {}) if isinstance(item, dict) else item
            for item in data["authorships"]
        ]
    authors: list[str] = []
    for item in authors_raw if isinstance(authors_raw, (list, tuple)) else [authors_raw]:
        name = ""
        if isinstance(item, dict):
            family = item.get("family") or item.get("lastName") or item.get("familyName")
            given = item.get("given") or item.get("firstName") or ""
            if family:
                name = f"{given} {family}".strip() if given else str(family)
            else:
                name = clean_text(item.get("name") or item.get("display_name") or item.get("literal"))
        else:
            name = clean_text(item)
        name = clean_text(name)
        if name:
            authors.append(name)
    volume = clean_text(_first(data.get("volume")))
    issue = clean_text(_first(data.get("issue")) or _first(data.get("number")))
    pages = clean_text(data.get("page") or data.get("pages") or "")
    if not pages:
        pages = _pages(data.get("first_page"), data.get("last_page"))
    entry = {
        "title": title,
        "authors": authors,
        "year": extract_year(data),
        "venue": clean_text(venue_value),
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "doi_display": doi_display(doi_raw),
        "arxiv_id": arxiv_id,
        "url": clean_text(data.get("url") or data.get("URL") or data.get("link") or ""),
        "type": clean_text(data.get("type") or data.get("publication_type") or ""),
        "cited_by": _as_int(
            data.get("cited_by_count")
            or data.get("citationCount")
            or data.get("is-referenced-by-count")
            or 0
        ),
        "abstract": clean_text(data.get("abstract") or data.get("abstractText") or "")[:1200],
        "source": source or clean_text(data.get("source") or ""),
        "source_id": clean_text(data.get("source_id") or data.get("id") or data.get("paperId") or ""),
    }
    return entry


# --------------------------------------------------------------------------- #
# 元数据比对（防幻觉核验的判定依据）
# --------------------------------------------------------------------------- #


def _finding(field: str, level: str, message: str, detail: Any = None) -> dict[str, Any]:
    item: dict[str, Any] = {"field": field, "level": level, "message": message}
    if detail is not None:
        item["detail"] = detail
    return item


def compare_metadata(
    candidate: dict[str, Any],
    evidence: dict[str, Any],
    *,
    min_title_similarity: float = 0.92,
    min_venue_similarity: float = 0.75,
    max_year_delta: int = 1,
    min_author_overlap: float = 0.5,
    reject_doi_prefixes: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """候选条目 vs 证据记录逐字段比对，返回 findings（level: error / warning）。"""
    findings: list[dict[str, Any]] = []
    reject_prefixes = {str(item).lower() for item in (reject_doi_prefixes or [])}

    cand_doi = doi_normalize(candidate.get("doi"))
    ev_doi = doi_normalize(evidence.get("doi"))
    if cand_doi:
        if not doi_is_valid(cand_doi):
            findings.append(_finding("doi", "error", "DOI 不符合 10.xxxx/yyy 规范", cand_doi))
        if doi_prefix(cand_doi) in reject_prefixes:
            findings.append(_finding("doi", "error", "DOI 前缀命中拒绝列表，疑似占位/伪造", cand_doi))
    if cand_doi and ev_doi and cand_doi != ev_doi:
        findings.append(_finding("doi", "error", "候选 DOI 与证据 DOI 不一致", {"candidate": cand_doi, "evidence": ev_doi}))

    similarity = title_similarity(candidate.get("title"), evidence.get("title"))
    if similarity < min_title_similarity:
        findings.append(
            _finding("title", "error", "标题与证据记录不匹配", {"similarity": similarity})
        )

    cand_year, ev_year = candidate.get("year"), evidence.get("year")
    if cand_year and ev_year and abs(int(cand_year) - int(ev_year)) > max_year_delta:
        findings.append(
            _finding("year", "error", "年份与证据记录差异超出容差", {"candidate": cand_year, "evidence": ev_year})
        )
    elif cand_year and ev_year and cand_year != ev_year:
        findings.append(
            _finding("year", "warning", "年份与证据记录相差 1 年（在线/正式出版差异）", {"candidate": cand_year, "evidence": ev_year})
        )

    cand_authors = author_surnames(candidate.get("authors"))
    ev_authors = author_surnames(evidence.get("authors"))
    if cand_authors and ev_authors:
        overlap = author_overlap(candidate.get("authors"), evidence.get("authors"))
        if overlap < min_author_overlap:
            findings.append(
                _finding("authors", "error", "作者姓氏集合与证据记录不一致", {"overlap": overlap})
            )
    elif cand_authors or ev_authors:
        findings.append(_finding("authors", "warning", "候选或证据缺少作者信息，无法核对"))

    cand_venue = clean_text(candidate.get("venue"))
    ev_venue = clean_text(evidence.get("venue"))
    if cand_venue and ev_venue:
        venue_score = venue_similarity(cand_venue, ev_venue)
        if venue_score < min_venue_similarity:
            level = "warning" if likely_venue_abbreviation(cand_venue, ev_venue) or likely_venue_abbreviation(
                ev_venue, cand_venue
            ) else "error"
            findings.append(
                _finding(
                    "venue",
                    level,
                    "期刊/会议名与证据记录不一致（疑似缩写/全称写法差异）"
                    if level == "warning"
                    else "期刊/会议名与证据记录不一致",
                    {"similarity": venue_score},
                )
            )
        elif venue_score < 1.0:
            findings.append(
                _finding("venue", "warning", "期刊/会议名与证据记录存在写法差异", {"similarity": venue_score})
            )
    elif cand_venue or ev_venue:
        findings.append(_finding("venue", "warning", "候选或证据缺少期刊/会议名，无法核对"))

    for field in ("volume", "issue", "pages"):
        cand_value = clean_text(candidate.get(field))
        ev_value = clean_text(evidence.get(field))
        if cand_value and ev_value:
            if cand_value != ev_value:
                findings.append(
                    _finding(
                        field,
                        "error",
                        f"{field} 与证据记录不一致",
                        {"candidate": cand_value, "evidence": ev_value},
                    )
                )
        elif ev_value and not cand_value:
            findings.append(_finding(field, "warning", f"候选缺少 {field} 字段（证据记录中存在）"))
    return findings


def verdict_from_findings(findings: list[dict[str, Any]]) -> str:
    """findings → verified / unverified / rejected。"""
    if any(item["level"] == "error" for item in findings):
        return "rejected"
    if any(item["level"] == "warning" for item in findings):
        return "unverified"
    return "verified"


_PAGE_RANGE_RE = re.compile(r"(\d+)\s*[-–—]\s*(\d+)")


def normalize_pages(pages: Any) -> str:
    """页码区间统一为半字线（IEEE / APA / GB/T 7714 通用写法）。"""
    text = clean_text(pages)
    return _PAGE_RANGE_RE.sub(lambda m: f"{m.group(1)}\u2013{m.group(2)}", text)


# --------------------------------------------------------------------------- #
# 引用格式化
# --------------------------------------------------------------------------- #

_GB_TYPE = {"journal-article": "J", "proceedings-article": "C", "posted-content": "J", "book": "M"}


def _style_check(style: str) -> str:
    normalized = str(style or "").strip().lower()
    return normalized or "ieee"


def bibtex_key(entry: dict[str, Any]) -> str:
    surnames = sorted(author_surnames(entry.get("authors")))
    first = surnames[0] if surnames else "anon"
    year = entry.get("year") or "n.d."
    word = ""
    for token in normalize_title(entry.get("title")).split(" "):
        if len(token) > 3:
            word = token
            break
    return f"{first}{year}{word}".lower()


def bibtex_entry(entry: dict[str, Any]) -> str:
    fields = [("title", clean_text(entry.get("title")))]
    authors = entry.get("authors") or []
    if authors:
        fields.append(("author", " and ".join(str(item) for item in authors)))
    if entry.get("venue"):
        fields.append(("journal", clean_text(entry["venue"])))
    if entry.get("year"):
        fields.append(("year", str(entry["year"])))
    if entry.get("volume"):
        fields.append(("volume", clean_text(entry["volume"])))
    if entry.get("issue"):
        fields.append(("number", clean_text(entry["issue"])))
    if entry.get("pages"):
        fields.append(("pages", clean_text(entry["pages"]).replace("\u2013", "--")))
    if entry.get("doi"):
        fields.append(("doi", clean_text(entry.get("doi_display")) or doi_normalize(entry["doi"])))
    if entry.get("url"):
        fields.append(("url", clean_text(entry["url"])))
    body = ",\n  ".join(f"{key} = {{{value}}}" for key, value in fields if value)
    return "@article{%s,\n  %s\n}" % (bibtex_key(entry), body)


def _ieee_authors(authors: list[str]) -> str:
    formatted = []
    for name in authors:
        parts = clean_text(name).split(" ")
        if len(parts) >= 2:
            initials = " ".join(f"{part[0]}." for part in parts[:-1] if part)
            formatted.append(f"{initials} {parts[-1]}".strip())
        else:
            formatted.append(clean_text(name))
    if len(formatted) == 2:
        return f"{formatted[0]} and {formatted[1]}"
    if len(formatted) > 2:
        return ", ".join(formatted[:-1]) + ", and " + formatted[-1]
    return formatted[0] if formatted else ""


def _apa_authors(authors: list[str]) -> str:
    formatted = []
    for name in authors:
        parts = clean_text(name).split(" ")
        if len(parts) >= 2:
            initials = " ".join(f"{part[0]}." for part in parts[:-1] if part)
            formatted.append(f"{parts[-1]}, {initials}".strip())
        else:
            formatted.append(clean_text(name))
    if len(formatted) > 1:
        return ", ".join(formatted[:-1]) + ", & " + formatted[-1]
    return formatted[0] if formatted else ""


def format_reference(entry: dict[str, Any], style: str = "ieee", index: int | None = None) -> str:
    """按指定规范格式化一条参考文献（字段缺失时自动省略，不抛错）。"""
    style = _style_check(style)
    title = clean_text(entry.get("title"))
    venue = clean_text(entry.get("venue"))
    volume = clean_text(entry.get("volume"))
    issue = clean_text(entry.get("issue"))
    pages = normalize_pages(entry.get("pages"))
    year = entry.get("year") or ""
    doi = clean_text(entry.get("doi_display")) or doi_normalize(entry.get("doi"))
    url = clean_text(entry.get("url"))
    authors = list(entry.get("authors") or [])

    if style == "bibtex":
        return bibtex_entry(entry)

    if style == "apa":
        parts = []
        if authors:
            parts.append(_apa_authors(authors))
        if year:
            parts.append(f"({year}).")
        if title:
            parts.append(f"{title}.")
        tail = venue
        if volume:
            tail += f", {volume}"
        if issue:
            tail += f"({issue})"
        if pages:
            tail += f", {pages}"
        if tail:
            parts.append(tail.strip() + ".")
        if doi:
            parts.append(f"https://doi.org/{doi}")
        elif url:
            parts.append(url)
        return " ".join(part for part in parts if part).strip()

    if style == "gbt7714":
        pieces = []
        if authors:
            pieces.append("; ".join(clean_text(name).upper() for name in authors))
        pieces.append(title)
        type_code = _GB_TYPE.get(clean_text(entry.get("type")).lower(), "J")
        head = ". ".join(piece for piece in pieces if piece)
        head = f"{head}[{type_code}]" if head else f"[{type_code}]"
        tail = venue
        if year:
            tail += f", {year}"
        if volume:
            tail += f", {volume}"
        if issue:
            tail += f"({issue})"
        if pages:
            tail += f": {pages}"
        prefix = f"[{index}] " if index else ""
        result = f"{prefix}{head}. {tail}.".strip()
        if doi:
            result += f" DOI: {doi}."
        return result

    # 默认 ieee
    prefix = f"[{index}] " if index else ""
    segments = []
    if authors:
        segments.append(_ieee_authors(authors) + ",")
    if title:
        segments.append(f'"{title},"')
    if venue:
        segments.append(venue + ",")
    if volume:
        segments.append(f"vol. {volume},")
    if issue:
        segments.append(f"no. {issue},")
    if pages:
        segments.append(f"pp. {pages},")
    if year:
        segments.append(f"{year}.")
    result = prefix + " ".join(segments)
    if doi:
        result += f" doi: {doi}."
    elif url:
        result += f" {url}"
    return result.strip()


# --------------------------------------------------------------------------- #
# 结构化输出辅助（卡片 / 按需插入）
# --------------------------------------------------------------------------- #

ALL_STYLES = ("ieee", "apa", "gbt7714", "bibtex")


def entry_core_summary(entry: dict[str, Any], max_chars: int = 240) -> str:
    """从摘要抽取 1-2 句作为"核心结论摘要"（无摘要返回空串）。"""
    abstract = clean_text(entry.get("abstract"))
    if not abstract:
        return ""
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(abstract) if part.strip()]
    if not sentences:
        return abstract[:max_chars]
    summary = sentences[0]
    if len(summary) < 80 and len(sentences) > 1:
        candidate = f"{summary} {sentences[1]}"
        if len(candidate) <= max_chars:
            summary = candidate
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return summary


def missing_required_fields(entry: dict[str, Any], fields: list[str] | tuple[str, ...] = ("doi", "venue", "year")) -> list[str]:
    """列出条目缺失的关键元数据字段（供 incomplete_entry 缺口检测复用）。"""
    missing: list[str] = []
    for field in fields:
        value = entry.get(field)
        if field == "doi":
            present = bool(doi_normalize(value))
        else:
            present = bool(clean_text(value))
        if not present:
            missing.append(str(field))
    return missing


def format_all_styles(entry: dict[str, Any]) -> dict[str, str]:
    """一条目的四种规范引用格式（ieee / apa / gbt7714 / bibtex）。"""
    return {style: format_reference(entry, style) for style in ALL_STYLES}
