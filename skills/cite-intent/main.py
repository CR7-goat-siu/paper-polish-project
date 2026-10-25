"""cite-intent Skill：引用证据强度预填（对标 scite.ai 的 supporting/contrasting 分类）。

能力（两种模式）
- build：为每个 claim-citation 对拉取被引文献的真实摘要（Crossref works/{doi} 与
  OpenAlex works/{doi}（abstract_inverted_index 还原），Semantic Scholar intents
  作交叉信号），渲染证据核验作业包；coding harness 执行模型并回填响应；
- apply：fail-closed 校验回填——intent 必须在白名单内；supporting/contrasting
  必须携带 verbatim_evidence_sentence 且该句逐字（空白/大小写归一后）来自被引
  摘要；无摘要 / 证据句伪造 / 身份不符一律降级为 unverified 并记录原因，
  绝不允许凭空预填 supporting。

红线：零 API Key（全部走 utils.web_fetch 的公开免密钥端点 + 缓存 + 限速）；
预填结果状态为 prefilled_pending_confirmation，最终 supporting/contrasting
判定必须由作者确认或覆写（人工签核红线）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.anchor_norm import normalize_anchor  # noqa: E402
from utils.citation_check import (  # noqa: E402
    clean_text,
    doi_normalize,
    normalize_title,
    title_similarity,
)
from utils.config_loader import load_literature_config, normalize_language  # noqa: E402
from utils.fidelity import CITATION_BRACKET_RE  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from utils.prompt_renderer import render_template  # noqa: E402
import utils.web_fetch as web_fetch  # noqa: E402

logger = get_logger("cite-intent")

SCHEMA_VERSION = 1
ALLOWED_INTENTS = ("supporting", "contrasting", "background", "mentioning")
EVIDENCE_INTENTS = {"supporting", "contrasting"}
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
# 句子边界：中文句末标点（可带尾随空白）或英文句号+空白+新句起始（大写/数字/引用标记）
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[。．！？!?；;])\s*|(?<=\.)\s+(?=[A-Z0-9\[(\"'])"
)
REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:references?|bibliography|参\s*考\s*文\s*献|文\s*献)\s*[:：]?\s*$",
    re.IGNORECASE,
)

# Semantic Scholar intents → 本项目 intent 空间的粗映射（仅作交叉信号展示，不做门禁）
S2_INTENT_MAP = {
    "result": "supporting",
    "methodology": "background",
    "background": "background",
    "citation": "background",
}


# --------------------------------------------------------------------------- #
# 摘要获取与还原（全部免密钥端点）
# --------------------------------------------------------------------------- #


def openalex_abstract_text(record: dict[str, Any]) -> str:
    """把 OpenAlex 的 abstract_inverted_index 还原为纯文本摘要。"""
    inverted = record.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            positions.append((int(index), str(word)))
    if not positions:
        return ""
    positions.sort()
    return " ".join(word for _index, word in positions)


def _strip_jats(abstract: str) -> str:
    """Crossref 的摘要常为 JATS XML：剥掉标签并归一空白。"""
    text = re.sub(r"<[^>]+>", " ", abstract or "")
    return re.sub(r"\s+", " ", text).strip()


def _arxiv_abstract(
    *,
    arxiv_id: str = "",
    title: str = "",
    config: dict[str, Any],
    offline: bool | None,
) -> str:
    """arXiv Atom API 摘要兜底：按 arXiv ID 直查，或按标题检索取 top-1。

    arXiv DOI（10.48550/arxiv.*）在 Crossref 常为 404，本函数是这类文献的
    主要摘要来源；标题检索复用与 OpenAlex 相同的 0.92 相似度门禁防错配。
    """
    sources = config.get("sources") or {}
    arxiv_cfg = sources.get("arxiv") or {}
    if not arxiv_cfg.get("enabled", True):
        return ""
    endpoint = str(arxiv_cfg.get("endpoint") or "https://export.arxiv.org/api/query")
    params: dict[str, Any] = {"max_results": "1"}
    if arxiv_id:
        params["id_list"] = arxiv_id
    elif title:
        params["search_query"] = f'ti:"{title}"'
        params["sortBy"] = "relevance"
    else:
        return ""
    try:
        result = web_fetch.fetch_text(
            endpoint,
            params,
            config=config,
            min_interval=float(arxiv_cfg.get("min_interval_seconds", 3.0)),
            offline=offline,
        )
        from xml.etree import ElementTree as ET

        root = ET.fromstring(result.body or "<feed/>")
        atom = "{http://www.w3.org/2005/Atom}"
        for entry in root.findall(f"{atom}entry"):
            summary = " ".join((entry.findtext(f"{atom}summary") or "").split())
            if not summary:
                continue
            if title and not arxiv_id:
                entry_title = " ".join((entry.findtext(f"{atom}title") or "").split())
                if not _title_match_is_exact(title, entry_title):
                    return ""
            return summary
    except Exception as exc:  # noqa: BLE001 - 兜底失败按无摘要处理
        logger.warning("arXiv 摘要兜底失败 id=%r title=%r err=%s", arxiv_id, title[:50], exc)
    return ""


_ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5})(v\d+)?$", re.IGNORECASE)


_GB_TITLE_RE = re.compile(r"[《“]([^》”]{6,200})[》”]")


def _reference_title(reference: dict[str, Any]) -> str:
    """标题：显式 title 字段优先；缺失时从原始著录行提取《》/引号内标题。

    GB/T 著录（submission-audit 参考文献解析）不带 title 字段，只有 raw 行；
    不提取则标题检索链路（OpenAlex / arXiv 兜底）整体失效。
    """
    explicit = clean_text(reference.get("title"))
    if explicit:
        return explicit
    raw = str(reference.get("raw") or reference.get("line") or "")
    match = _GB_TITLE_RE.search(raw)
    return match.group(1).strip() if match else ""


def _title_match_is_exact(query: str, candidate: str) -> bool:
    """标题检索兜底的精确门禁：归一化标题完全相等。

    序列相似度对"严格扩展"标题过松（实测 "Generative adversarial nets" 与
    "Conditional Generative Adversarial Nets" 相似度 0.95 仍错配）；标题兜底
    没有 DOI 锚点，必须要求归一化精确相等才允许采信摘要。
    """
    left = normalize_title(query)
    right = normalize_title(candidate)
    return bool(left) and left == right


def fetch_reference_abstract(
    reference: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    offline: bool | None = None,
) -> dict[str, Any]:
    """拉取被引文献摘要：Crossref DOI → OpenAlex DOI → OpenAlex 标题检索 → arXiv 兜底。

    返回 {abstract, source, source_id, doi, fetched_url}；全部失败时 abstract=""。
    网络失败按无摘要处理（fail-closed），绝不抛出到调用方。
    """
    config = config or load_literature_config()
    sources = config.get("sources") or {}
    doi = doi_normalize(reference.get("doi"))
    title = _reference_title(reference)
    result: dict[str, Any] = {"abstract": "", "source": "", "source_id": "", "doi": doi, "fetched_url": ""}

    crossref_works = str((config.get("verification") or {}).get("crossref_works_endpoint") or "https://api.crossref.org/works")
    openalex_works = str((sources.get("openalex") or {}).get("works_endpoint") or "https://api.openalex.org/works")
    crossref_cfg = sources.get("crossref") or {}
    openalex_cfg = sources.get("openalex") or {}

    if doi:
        url = f"{crossref_works}/{doi}"
        try:
            data, _ = web_fetch.fetch_json(
                url,
                {"mailto": (config.get("network") or {}).get("contact_email")} if (config.get("network") or {}).get("contact_email") else {},
                config=config,
                min_interval=float(crossref_cfg.get("min_interval_seconds", 1.0)),
                offline=offline,
            )
            record = (data or {}).get("message") or {}
            abstract = _strip_jats(str(record.get("abstract") or ""))
            if abstract:
                result.update({"abstract": abstract, "source": "crossref", "source_id": doi, "fetched_url": url})
                return result
        except Exception as exc:  # noqa: BLE001 - 单源失败换下一源
            logger.warning("Crossref 摘要抓取失败 doi=%s err=%s", doi, exc)

        if openalex_cfg.get("enabled", True):
            url = f"{openalex_works}/https://doi.org/{doi}"
            try:
                data, _ = web_fetch.fetch_json(
                    url,
                    {"mailto": (config.get("network") or {}).get("contact_email")} if (config.get("network") or {}).get("contact_email") else {},
                    config=config,
                    min_interval=float(openalex_cfg.get("min_interval_seconds", 0.5)),
                    offline=offline,
                )
                abstract = openalex_abstract_text(data or {})
                if abstract:
                    result.update({"abstract": abstract, "source": "openalex", "source_id": str((data or {}).get("id") or doi), "fetched_url": url})
                    return result
            except Exception as exc:  # noqa: BLE001
                logger.warning("OpenAlex 摘要抓取失败 doi=%s err=%s", doi, exc)

    if title and openalex_cfg.get("enabled", True):
        try:
            data, _ = web_fetch.fetch_json(
                openalex_works,
                {"search": title, "per-page": 1},
                config=config,
                min_interval=float(openalex_cfg.get("min_interval_seconds", 0.5)),
                offline=offline,
            )
            results = (data or {}).get("results") or []
            if results:
                candidate = results[0]
                candidate_title = candidate.get("title") or candidate.get("display_name")
                similarity = title_similarity(title, candidate_title)
                if similarity >= 0.92 and _title_match_is_exact(title, candidate_title):
                    abstract = openalex_abstract_text(candidate)
                    if abstract:
                        result.update(
                            {
                                "abstract": abstract,
                                "source": "openalex-title",
                                "source_id": str(candidate.get("id") or ""),
                                "fetched_url": openalex_works,
                            }
                        )
                        return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenAlex 标题检索摘要失败 title=%r err=%s", title[:60], exc)
    # arXiv 兜底：arXiv DOI 直查（10.48550/arxiv.* 在 Crossref 常为 404）或标题检索
    arxiv_match = _ARXIV_DOI_RE.match(doi or "")
    if arxiv_cfg_enabled(config) or arxiv_match:
        abstract = _arxiv_abstract(
            arxiv_id=arxiv_match.group(1) if arxiv_match else "",
            title="" if arxiv_match else title,
            config=config,
            offline=offline,
        )
        if abstract:
            result.update(
                {
                    "abstract": abstract,
                    "source": "arxiv-doi" if arxiv_match else "arxiv-title",
                    "source_id": arxiv_match.group(1) if arxiv_match else "",
                    "fetched_url": str(((config.get("sources") or {}).get("arxiv") or {}).get("endpoint") or ""),
                }
            )
    return result


def arxiv_cfg_enabled(config: dict[str, Any]) -> bool:
    return bool(((config.get("sources") or {}).get("arxiv") or {}).get("enabled", True))


def fetch_s2_intents(
    doi: str,
    *,
    config: dict[str, Any] | None = None,
    offline: bool | None = None,
) -> list[str]:
    """拉取 Semantic Scholar 免费 intents 字段作交叉信号；失败返回空表。"""
    config = config or load_literature_config()
    s2_cfg = (config.get("sources") or {}).get("semantic-scholar") or {}
    paper_endpoint = str(s2_cfg.get("paper_endpoint") or "https://api.semanticscholar.org/graph/v1/paper")
    if not doi:
        return []
    try:
        data, _ = web_fetch.fetch_json(
            f"{paper_endpoint}/DOI:{doi}",
            {"fields": "intents"},
            config=config,
            min_interval=float(s2_cfg.get("min_interval_seconds", 1.5)),
            offline=offline,
        )
        return [str(item) for item in (data or {}).get("intents") or []]
    except Exception as exc:  # noqa: BLE001 - 交叉信号缺失不影响门禁
        logger.warning("Semantic Scholar intents 抓取失败 doi=%s err=%s", doi, exc)
        return []


# --------------------------------------------------------------------------- #
# claim-citation 对提取
# --------------------------------------------------------------------------- #


def _expand_citation_numbers(inner: str) -> list[int]:
    numbers: list[int] = []
    for token in re.split(r"[,，]", inner):
        token = token.strip()
        if not token:
            continue
        range_match = re.fullmatch(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", token)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if 0 <= end - start <= 200:
                numbers.extend(range(start, end + 1))
        elif token.isdigit():
            numbers.append(int(token))
    return sorted(set(numbers))


def _body_text(text: str) -> str:
    """截掉参考文献区：References 行本身含 [n]，不能当作正文论断。"""
    for index, line in enumerate((text or "").splitlines()):
        if REFERENCE_HEADING_RE.match(line.strip()):
            return "\n".join(text.splitlines()[:index])
    return text or ""


def extract_claim_citation_pairs(
    text: str, references: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """从正文提取 (claim, citation) 对：带 [n] 标记的句子 × 对应参考文献条目。"""
    number_map: dict[int, dict[str, Any]] = {}
    for index, reference in enumerate(references or []):
        if not isinstance(reference, dict):
            continue
        raw_number = reference.get("number", reference.get("id"))
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            match = re.fullmatch(r"R?(\d{1,3})", str(raw_number or ""))
            number = int(match.group(1)) if match else index + 1
        number_map[number] = reference
    pairs: list[dict[str, Any]] = []
    for sentence_index, sentence in enumerate(_SENTENCE_SPLIT_RE.split(_body_text(text))):
        sentence = sentence.strip()
        if not sentence:
            continue
        numbers: list[int] = []
        for marker in CITATION_BRACKET_RE.findall(sentence):
            inner = marker.strip()[1:-1] if marker.startswith("[") else marker
            numbers.extend(_expand_citation_numbers(inner))
        if not numbers:
            continue
        claim_id = f"C{sentence_index + 1:03d}"
        for number in sorted(set(numbers)):
            reference = number_map.get(number) or {}
            pairs.append(
                {
                    "claim_id": claim_id,
                    "claim_text": sentence,
                    "reference_number": number,
                    "reference_id": str(reference.get("id") or f"R{number}"),
                    "reference": reference,
                }
            )
    return pairs


# --------------------------------------------------------------------------- #
# build / apply
# --------------------------------------------------------------------------- #


def build_intent_jobs(
    text: str,
    references: list[dict[str, Any]],
    *,
    language: str = "en",
    out_dir: str | Path = "output/cite-intent",
    offline: bool | None = None,
    config: dict[str, Any] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """为全部 claim-citation 对拉摘要、渲染作业包并落盘。"""
    language = normalize_language(language)
    config = config or load_literature_config()
    out_root = Path(out_dir)
    prompts_dir = out_root / "prompts"
    responses_dir = out_root / "responses"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)

    pairs = extract_claim_citation_pairs(text, references)
    jobs: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, Any]] = []
    for pair in pairs:
        reference = pair["reference"]
        job_id = f"citeintent-{pair['claim_id']}-{pair['reference_id']}"
        abstract_info = fetch_reference_abstract(
            reference, config=config, offline=offline
        ) if (reference.get("doi") or _reference_title(reference)) else {
            "abstract": "",
            "source": "",
            "source_id": "",
            "doi": doi_normalize(reference.get("doi")),
            "fetched_url": "",
        }
        s2_intents = fetch_s2_intents(
            abstract_info["doi"], config=config, offline=offline
        ) if abstract_info["doi"] else []
        s2_mapped = sorted({S2_INTENT_MAP.get(item, "background") for item in s2_intents})
        if not abstract_info["abstract"]:
            fetch_errors.append(
                {"job_id": job_id, "reason": "no_abstract_available", "doi": abstract_info["doi"]}
            )
        source_abstract = abstract_info["abstract"] or (
            "NO ABSTRACT AVAILABLE. You must classify this citation as 'mentioning'."
        )
        prompt = render_template(
            "cite-intent.md",
            language,
            job_id=job_id,
            claim_id=pair["claim_id"],
            claim_text=pair["claim_text"],
            claim_location="",
            reference_id=pair["reference_id"],
            reference_title=_reference_title(reference) or "(untitled)",
            reference_doi=abstract_info["doi"] or "(unknown)",
            source_abstract=source_abstract,
            s2_intents=", ".join(s2_intents) or "(none)",
        )
        prompt_path = prompts_dir / f"{job_id}.prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        jobs.append(
            {
                "job_id": job_id,
                "claim_id": pair["claim_id"],
                "claim_text": pair["claim_text"],
                "reference_id": pair["reference_id"],
                "reference_number": pair["reference_number"],
                "reference_title": clean_text(reference.get("title")),
                "reference_doi": abstract_info["doi"],
                "source_abstract": abstract_info["abstract"],
                "abstract_source": abstract_info["source"],
                "abstract_source_id": abstract_info["source_id"],
                "abstract_available": bool(abstract_info["abstract"]),
                "s2_intents": s2_intents,
                "s2_mapped_intents": s2_mapped,
                "prompt_file": str(prompt_path.resolve()),
                "response_file": str((responses_dir / f"{job_id}.txt").resolve()),
                "status": "pending",
                "response": "",
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "language": language,
        "job_count": len(jobs),
        "jobs": jobs,
        "fetch_errors": fetch_errors,
        "status": "built" if jobs else "no_citation_pairs",
        "warnings": [
            "预填意图状态为 prefilled_pending_confirmation，supporting/contrasting 必须经作者确认或覆写。",
            "无摘要或证据句无法逐字锚定的作业在 apply 阶段一律 fail-closed 降级。",
        ],
    }
    (out_root / "cite-intent-jobs.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def normalize_ws_lower(text: str) -> str:
    """（保留别名）锚定比对统一走 utils.anchor_norm.normalize_anchor。"""
    return normalize_anchor(text)


def _parse_response_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError("引用意图响应不是可解析 JSON")
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("引用意图响应顶层必须是 JSON 对象")
    return payload


def apply_intent_responses(
    jobs_or_path: dict[str, Any] | str | Path,
    responses_dir: str | Path | None = None,
) -> dict[str, Any]:
    """fail-closed 校验回填的意图分类，产出预填清单与被拒/降级记录。

    - intent 不在白名单 / 身份不符 / JSON 非法 → 拒收（rejected）；
    - supporting/contrasting 但证据句缺失、不是被引摘要的逐字子串、
      或作业本身没有摘要 → 降级为 unverified，原因
      unverified_no_verbatim_evidence / no_abstract_available；
    - 通过校验的条目 confirmation_status=prefilled_pending_confirmation。
    """
    if isinstance(jobs_or_path, (str, Path)):
        payload = json.loads(Path(jobs_or_path).read_text(encoding="utf-8"))
    else:
        payload = jobs_or_path
    jobs = [job for job in payload.get("jobs") or [] if isinstance(job, dict)]
    response_root = Path(responses_dir) if responses_dir else None

    prefilled: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    pending: list[str] = []

    for job in jobs:
        job_id = str(job.get("job_id") or "")
        claim_id = str(job.get("claim_id") or "")
        reference_id = str(job.get("reference_id") or "")
        abstract = str(job.get("source_abstract") or "")

        raw_response = str(job.get("response") or "")
        if not raw_response and response_root is not None:
            response_file = response_root / f"{job_id}.txt"
            if response_file.exists():
                raw_response = response_file.read_text(encoding="utf-8-sig")
        if not raw_response.strip():
            pending.append(job_id)
            continue
        try:
            result = _parse_response_json(raw_response)
        except (ValueError, json.JSONDecodeError) as exc:
            rejected.append({"job_id": job_id, "reason": f"invalid_json: {exc}"})
            continue
        if str(result.get("job_id") or "") != job_id:
            rejected.append({"job_id": job_id, "reason": "job_id_mismatch"})
            continue
        if str(result.get("claim_id") or "") != claim_id or str(result.get("reference_id") or "") != reference_id:
            rejected.append({"job_id": job_id, "reason": "identity_mismatch"})
            continue
        intent = str(result.get("intent") or "").strip().lower()
        if intent not in ALLOWED_INTENTS:
            rejected.append({"job_id": job_id, "reason": f"invalid_intent: {intent!r}"})
            continue

        evidence_sentence = str(result.get("verbatim_evidence_sentence") or "").strip()
        final_intent = intent
        confirmation_status = "prefilled_pending_confirmation"
        fail_reasons: list[str] = []
        if intent in EVIDENCE_INTENTS:
            if not abstract:
                final_intent = "unverified"
                confirmation_status = "manual_evidence_required"
                fail_reasons.append("no_abstract_available")
            elif not evidence_sentence:
                final_intent = "unverified"
                confirmation_status = "manual_evidence_required"
                fail_reasons.append("unverified_no_verbatim_evidence")
            elif normalize_ws_lower(evidence_sentence) not in normalize_ws_lower(abstract):
                final_intent = "unverified"
                confirmation_status = "manual_evidence_required"
                fail_reasons.append("unverified_no_verbatim_evidence")
        if intent not in EVIDENCE_INTENTS:
            evidence_sentence = ""

        entry = {
            "job_id": job_id,
            "claim_id": claim_id,
            "claim_text": str(job.get("claim_text") or ""),
            "reference_id": reference_id,
            "reference_number": job.get("reference_number"),
            "reference_title": str(job.get("reference_title") or ""),
            "reference_doi": str(job.get("reference_doi") or ""),
            "proposed_intent": intent,
            "final_intent": final_intent,
            "verbatim_evidence_sentence": evidence_sentence,
            "abstract_source": str(job.get("abstract_source") or ""),
            "abstract_available": bool(job.get("abstract_available")),
            "s2_cross_signal": list(job.get("s2_mapped_intents") or []),
            "rationale": str(result.get("rationale") or "")[:400],
            "confirmation_status": confirmation_status,
            "fail_reasons": fail_reasons,
        }
        if final_intent == "unverified":
            rejected.append({"job_id": job_id, "reason": ";".join(fail_reasons), "downgraded": True})
        prefilled.append(entry)

    confirmed = sum(1 for item in prefilled if item["confirmation_status"] == "prefilled_pending_confirmation")
    return {
        "schema_version": SCHEMA_VERSION,
        "prefilled": prefilled,
        "rejected": rejected,
        "pending_jobs": pending,
        "prefilled_count": len(prefilled),
        "pending_confirmation_count": confirmed,
        "downgraded_count": sum(1 for item in prefilled if item["final_intent"] == "unverified"),
        "pending_count": len(pending),
        "status": "applied" if prefilled else ("pending" if pending else "no_responses"),
    }


# --------------------------------------------------------------------------- #
# run() 契约
# --------------------------------------------------------------------------- #


def run(args: dict[str, Any]) -> dict[str, Any]:
    mode = str(args.get("mode") or "build").lower()
    config = load_literature_config()
    language = normalize_language(str(args.get("language") or "en"))
    offline = args.get("offline")
    force_refresh = bool(args.get("force_refresh", False))

    if mode == "build":
        text = args.get("text")
        if text is None:
            text_file = args.get("text_file")
            if text_file:
                text = Path(str(text_file)).read_text(encoding="utf-8-sig")
        if text is None:
            raise ValueError("cite-intent build 需要 text / text_file 或 package")
        references = args.get("references")
        if references is None:
            package = args.get("package") or args.get("package_file")
            if isinstance(package, (str, Path)):
                package = json.loads(Path(str(package)).read_text(encoding="utf-8"))
            references = list((package or {}).get("references") or [])
        if not isinstance(references, list):
            raise ValueError("references 必须是参考文献条目列表")
        return build_intent_jobs(
            str(text),
            references,
            language=language,
            out_dir=args.get("out") or "output/cite-intent",
            offline=offline,
            config=config,
            force_refresh=force_refresh,
        )

    if mode == "apply":
        jobs_source = args.get("jobs") or args.get("jobs_file")
        if jobs_source is None:
            jobs_path = Path(str(args.get("out") or "output/cite-intent")) / "cite-intent-jobs.json"
            if not jobs_path.exists():
                raise ValueError("cite-intent apply 需要 jobs / jobs_file 或已有产物")
            jobs_source = jobs_path
        return apply_intent_responses(jobs_source, args.get("responses_dir"))

    raise ValueError(f"未知 cite-intent mode: {mode!r}，可选 build / apply")
