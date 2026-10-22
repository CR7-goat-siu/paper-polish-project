"""lit-verify Skill：候选文献真实性核验（确定性门禁 + 可选的在线二次核验）。

职责
- 读取候选条目（lit-search 产物 / coding harness 回填）与证据记录（数据源原始响应或已归一条目）；
- 逐条比对 DOI / 标题 / 作者 / 年份 / 期刊会议 / 卷期页，给出
  verified / unverified / rejected 三档结论与 findings；
- 只有 verified 条目才允许进入正文引用（cite-manager insert 只接受 verified_entries）。

设计原则
- 无证据 = 不通过：`require_evidence=true` 时，没有可核对证据的候选一律 unverified，
  这是"禁止编造参考文献"的执行机制，而不是文字约定；
- 在线二次核验（`online`，默认取 config/literature.yaml → verification.online_reverify）：
  对没有本地证据的候选，按 DOI 直抓 Crossref works/{doi}，或按标题 bibliographic
  检索取 top-1 作为证据再比对；抓取失败 / 离线时优雅降级为无证据（unverified），
  绝不让"核验失败"变成"核验通过"；
- 判定逻辑集中在 utils/citation_check.compare_metadata，便于单测与复用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.citation_check import (  # noqa: E402
    canonical_entry,
    clean_text,
    compare_metadata,
    doi_is_valid,
    doi_normalize,
    doi_prefix,
    format_reference,
    title_similarity,
    verdict_from_findings,
)
from utils.config_loader import load_literature_config, normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402
import utils.web_fetch as web_fetch  # noqa: E402

logger = get_logger("lit-verify")

VERDICTS = ("verified", "unverified", "rejected")


def reverify_candidate_online(
    candidate: dict[str, Any],
    *,
    config: dict[str, Any],
    offline: bool | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """在线二次核验：按 DOI 直抓 Crossref works/{doi}；无 DOI 时按标题检索取 top-1。

    返回 (证据条目或 None, 溯源信息)。任何网络失败都返回 (None, info)，
    由调用方按"无证据"处理——核验失败绝不等于通过。
    """
    verification = config.get("verification") or {}
    endpoint = str(verification.get("crossref_works_endpoint") or "https://api.crossref.org/works")
    rows = int(verification.get("title_search_rows", 3))
    doi = doi_normalize(candidate.get("doi"))
    title = clean_text(candidate.get("title"))
    info: dict[str, Any] = {"mode": "", "url": "", "cached": None, "fetched_at": "", "error": ""}
    try:
        if doi and doi_is_valid(doi):
            url = f"{endpoint}/{doi}"
            data, result = web_fetch.fetch_json(url, {}, config=config, offline=offline)
            record = (data or {}).get("message") or {}
            if isinstance(record, dict) and record.get("title"):
                info.update({"mode": "doi", "url": url, "cached": result.cached, "fetched_at": result.fetched_at})
                return canonical_entry(record, "crossref"), info
        if title:
            data, result = web_fetch.fetch_json(
                endpoint,
                {"query.bibliographic": title, "rows": rows},
                config=config,
                offline=offline,
            )
            items = ((data or {}).get("message") or {}).get("items") or []
            if items:
                info.update({"mode": "title", "url": endpoint, "cached": result.cached, "fetched_at": result.fetched_at})
                return canonical_entry(items[0], "crossref"), info
        info["error"] = "未在 Crossref 定位到对应记录"
    except web_fetch.NetworkDisabledError as exc:
        info["error"] = f"offline: {exc}"
    except Exception as exc:  # noqa: BLE001 - 核验抓取失败按无证据处理
        logger.warning("在线二次核验抓取失败 title=%r err=%s", title[:60], exc)
        info["error"] = str(exc)
    return None, info


def _load_json(path: Any, default: Any) -> Any:
    if not path:
        return default
    target = Path(str(path))
    if not target.exists():
        return default
    return json.loads(target.read_text(encoding="utf-8"))


def _load_records(source: Any, default: Any) -> Any:
    """接受 list / dict / 文件路径 / 目录路径。"""
    if source is None:
        return default
    if isinstance(source, (list, dict)):
        return source
    target = Path(str(source))
    if target.is_dir():
        records: list[Any] = []
        for path in sorted(target.glob("*.json")):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - 坏文件跳过
                logger.warning("证据文件无法解析: %s (%s)", path, exc)
        return records
    if target.exists():
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("证据文件无法解析: %s (%s)", target, exc)
            return default
    return default


def _iter_records(payload: Any) -> list[dict[str, Any]]:
    """把任意数据源响应体拍平为记录列表。"""
    if payload is None:
        return []
    if isinstance(payload, list):
        records: list[dict[str, Any]] = []
        for item in payload:
            records.extend(_iter_records(item))
        return records
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("message"), dict) and isinstance(payload["message"].get("items"), list):
        return [item for item in payload["message"]["items"] if isinstance(item, dict)]
    if isinstance(payload.get("results"), list):
        return [item for item in payload["results"] if isinstance(item, dict)]
    if isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, dict)]
    if isinstance(payload.get("resultList"), dict) and isinstance(payload["resultList"].get("result"), list):
        return [item for item in payload["resultList"]["result"] if isinstance(item, dict)]
    if "title" in payload or "name" in payload or "display_name" in payload:
        return [payload]
    return []


def load_evidence(source: Any, *, source_name: str = "") -> list[dict[str, Any]]:
    """证据统一为 canonical entry 列表（保留原始记录在 raw 字段）。"""
    entries: list[dict[str, Any]] = []
    for record in _iter_records(_load_records(source, [])):
        # 候选条目自带的 evidence 列表（lit-search 产物）本身就是可核对的证据记录
        for inline in record.get("evidence") or []:
            if isinstance(inline, dict):
                entry = canonical_entry(inline, str(inline.get("source") or source_name))
                if entry.get("title"):
                    entry["raw"] = inline
                    entries.append(entry)
        entry = canonical_entry(record, source_name)
        if not entry.get("title"):
            continue
        entry["raw"] = record
        entries.append(entry)
    return entries


def _evidence_for(candidate: dict[str, Any], evidence: list[dict[str, Any]], min_title_similarity: float) -> tuple[dict[str, Any] | None, float]:
    """优先按 DOI 命中，其次按标题相似度命中。"""
    doi = str(candidate.get("doi") or "").strip().lower()
    if doi:
        for item in evidence:
            if str(item.get("doi") or "").strip().lower() == doi:
                return item, 1.0
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in evidence:
        score = title_similarity(candidate.get("title"), item.get("title"))
        if score > best_score:
            best, best_score = item, score
    if best is not None and best_score >= min_title_similarity:
        return best, best_score
    return None, best_score


def verify_candidates(
    candidates: list[dict[str, Any]],
    *,
    evidence: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    style: str | None = None,
    require_evidence: bool | None = None,
    online: bool | None = None,
    offline: bool | None = None,
) -> dict[str, Any]:
    """核心核验逻辑（纯函数，便于单测）。

    online=None 时取 config → verification.online_reverify：对没有本地证据的候选
    追加一次 Crossref 在线二次核验（DOI 直抓 / 标题检索）。
    """
    config = config or load_literature_config()
    verification = config.get("verification") or {}
    style = style or (config.get("styles") or {}).get("default") or "ieee"
    require_evidence = (
        bool(verification.get("require_evidence", True)) if require_evidence is None else bool(require_evidence)
    )
    online = bool(verification.get("online_reverify", True)) if online is None else bool(online)
    min_title = float(verification.get("min_title_similarity", 0.92))
    min_venue = float(verification.get("min_venue_similarity", 0.75))
    max_year = int(verification.get("max_year_delta", 1))
    min_authors = float(verification.get("min_author_overlap", 0.5))
    reject_prefixes = verification.get("reject_doi_prefixes") or []

    external_evidence = list(evidence or [])
    results: list[dict[str, Any]] = []
    reverified_count = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        inline_evidence = [item for item in (candidate.get("evidence") or []) if isinstance(item, dict)]
        pool = external_evidence + inline_evidence
        matched, score = _evidence_for(candidate, pool, min_title)
        reverify_info: dict[str, Any] | None = None
        if matched is None and online:
            online_evidence, reverify_info = reverify_candidate_online(
                candidate, config=config, offline=offline
            )
            if online_evidence is not None:
                reverified_count += 1
                pool = pool + [online_evidence]
                matched, score = _evidence_for(candidate, pool, min_title)
        findings: list[dict[str, Any]] = []
        candidate_doi = doi_normalize(candidate.get("doi"))
        if candidate_doi:
            if not doi_is_valid(candidate_doi):
                findings.append(
                    {
                        "field": "doi",
                        "level": "error",
                        "message": "DOI 不符合 10.xxxx/yyy 规范",
                        "detail": candidate_doi,
                    }
                )
            elif doi_prefix(candidate_doi) in {str(item).lower() for item in reject_prefixes}:
                findings.append(
                    {
                        "field": "doi",
                        "level": "error",
                        "message": "DOI 前缀命中拒绝列表，疑似占位/伪造",
                        "detail": candidate_doi,
                    }
                )
        matched_source: dict[str, Any] | None = None
        if matched is None:
            findings.append(
                {
                    "field": "evidence",
                    "level": "warning",
                    "message": "未找到可核对的证据记录（禁止把未核验文献写入正文）",
                    "detail": {"best_title_similarity": score},
                }
            )
        else:
            matched_source = {
                "source": matched.get("source"),
                "source_id": matched.get("source_id"),
                "title": matched.get("title"),
                "doi": matched.get("doi"),
            }
            if reverify_info is not None and reverify_info.get("mode"):
                matched_source["online"] = True
            findings.extend(
                compare_metadata(
                    candidate,
                    matched,
                    min_title_similarity=min_title,
                    min_venue_similarity=min_venue,
                    max_year_delta=max_year,
                    min_author_overlap=min_authors,
                    reject_doi_prefixes=reject_prefixes,
                )
            )
        verdict = verdict_from_findings(findings)
        if verdict == "verified" and matched is None and require_evidence:
            verdict = "unverified"
        entry = dict(candidate)
        entry.pop("evidence", None)
        result_item = {
            "title": clean_text(candidate.get("title")),
            "verdict": verdict,
            "findings": findings,
            "matched_evidence": matched_source,
            "entry": entry,
            "reference": format_reference(entry, style) if verdict == "verified" else "",
        }
        if reverify_info is not None:
            result_item["reverify"] = reverify_info
        results.append(result_item)

    summary = {name: sum(1 for item in results if item["verdict"] == name) for name in VERDICTS}
    verified_entries = [
        dict(item["entry"], verdict="verified")
        for item in results
        if item["verdict"] == "verified"
    ]
    return {
        "skill": "lit-verify",
        "style": style,
        "require_evidence": require_evidence,
        "online": online,
        "reverified_count": reverified_count,
        "results": results,
        "verified_entries": verified_entries,
        "unverified": [item for item in results if item["verdict"] == "unverified"],
        "rejected": [item for item in results if item["verdict"] == "rejected"],
        "summary": summary,
        "verdict": "ok" if summary["rejected"] == 0 else "has_rejected",
    }


def render_report(payload: dict[str, Any]) -> str:
    lines = ["# 候选文献核验报告", ""]
    summary = payload.get("summary") or {}
    lines.append(f"- 通过：{summary.get('verified', 0)}　待核：{summary.get('unverified', 0)}　拒绝：{summary.get('rejected', 0)}")
    if payload.get("online"):
        lines.append(f"- 在线二次核验：开启（实际触发 {payload.get('reverified_count', 0)} 次）")
    lines.append("")
    for item in payload.get("results") or []:
        lines.append(f"## [{item['verdict']}] {item.get('title')}")
        for finding in item.get("findings") or []:
            lines.append(f"- ({finding['level']}) {finding['field']}: {finding['message']}")
        reverify = item.get("reverify") or {}
        if reverify.get("mode"):
            lines.append(f"- 在线核验：mode={reverify['mode']} url={reverify.get('url')} cached={reverify.get('cached')}")
        elif reverify.get("error"):
            lines.append(f"- 在线核验未完成：{reverify['error']}")
        if item.get("reference"):
            lines.append(f"- 建议条目：{item['reference']}")
        lines.append("")
    return "\n".join(lines)


def _write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def run(args: dict[str, Any]) -> dict[str, Any]:
    """lit-verify 入口。

    参数
    - candidates / candidates_file：候选条目（lit-search 的 lit-candidates.json 可直接用）
    - evidence / evidence_dir：证据记录（数据源原始响应 JSON，或已归一条目列表）
    - online：无本地证据时是否按 DOI/标题联网二次核验（默认取 config）
    - offline：强制离线（只用缓存）
    - language；style；require_evidence（默认取 config/literature.yaml）
    - out：产物目录（写 lit-verification.json / lit-verification.md）
    """
    config = load_literature_config()
    language = normalize_language(args.get("language") or "cn")
    candidates = _load_records(args.get("candidates"), None)
    if candidates is None:
        candidates = _load_json(args.get("candidates_file"), [])
    if isinstance(candidates, dict):
        candidates = (
            candidates.get("candidates")
            or candidates.get("verified_entries")
            or candidates.get("entries")
            or candidates.get("results")
            or []
        )
    if not isinstance(candidates, list):
        raise ValueError("candidates 必须是候选条目列表或 lit-candidates.json")

    evidence_arg = args.get("evidence")
    if evidence_arg is None:
        evidence_arg = args.get("evidence_dir")
    evidence = load_evidence(evidence_arg, source_name=str(args.get("evidence_source") or ""))

    result = verify_candidates(
        candidates,
        evidence=evidence,
        config=config,
        style=args.get("style"),
        require_evidence=args.get("require_evidence"),
        online=args.get("online"),
        offline=args.get("offline"),
    )
    result["language"] = language
    result["evidence_count"] = len(evidence)
    out_dir = Path(str(args.get("out") or "output/lit-verify"))
    result["files"] = {
        "json": _write(out_dir / "lit-verification.json", json.dumps(result, ensure_ascii=False, indent=2)),
        "report": _write(out_dir / "lit-verification.md", render_report(result)),
    }
    return result
