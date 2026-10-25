"""采集真实 arXiv 论文全文，构建带黄金标准的多领域审计评测集。

数据源：arXiv 原生 HTML 全文（https://arxiv.org/html/<id>，公开免密钥），
统一走 utils/web_fetch（超时重试 + 每源限速 + 磁盘缓存）。

多领域覆盖（v2）：检索式按领域组织（biomedical-eeg / computer-vision /
nlp / time-series），每领域独立配额与检索轮，杜绝单一领域评测偏置。

黄金标准构造（无需人工标注，金标来自构造本身）：
- 阴性对照：真实发表论文的完整抽取文本跑全源审计——期望 0 个 P0
  （测量审计器在真实学术文本上的误报率）；
- 阳性对照：对每篇论文植入 5 类确定性缺陷（引用越界 / 摘要假数字 /
  幽灵图表引用 / 浮夸句 / 重复文献条目），期望对应检查逐项命中
  （测量检测召回率）。

文件管理（v2）：
- output/gold-standard/corpus/   官方评测集全文（gold.json 收录的论文，且仅这些）
- output/gold-standard/candidates/  已抓取但未入选的候选（含淘汰原因，可复用缓存）
- output/gold-standard/index.json   全集清单（论文×领域×文件 + 候选×原因）
- data/eval/gold-standard/      汇总指标 + 短摘录夹具（提交进 git；全文不入 git）
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import utils.web_fetch as web_fetch  # noqa: E402
from utils.skill_loader import run_skill  # noqa: E402

ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"
API = "https://export.arxiv.org/api/query"
HTML_ENDPOINT = "https://arxiv.org/html/{arxiv_id}"

# 领域 → 检索式 + 入选配额。biomedical-eeg 保留 32 篇既有验证集（全量缓存，
# 零重抓成本，且可用当前代码全量重验）；其余领域各补 8 篇。
DOMAINS: dict[str, dict[str, Any]] = {
    "biomedical-eeg": {
        "target": 32,
        "queries": [
            'all:"EEG denoising"',
            'all:"EEG artifact removal" AND all:"deep learning"',
            'all:"electroencephalogram" AND all:"generative adversarial"',
            'all:"brain signal" AND all:"denoising network"',
            'all:"EEG" AND all:"transformer" AND all:"artifact"',
        ],
    },
    "computer-vision": {
        "target": 8,
        "queries": [
            'all:"image denoising" AND all:"deep learning"',
            'all:"image super-resolution" AND all:"transformer"',
            'all:"semantic segmentation" AND all:"attention module"',
        ],
    },
    "nlp": {
        "target": 8,
        "queries": [
            'all:"machine translation" AND all:"transformer"',
            'all:"named entity recognition" AND all:"BERT"',
            'all:"text classification" AND all:"pretrained language model"',
        ],
    },
    "time-series": {
        "target": 8,
        "queries": [
            'all:"time series forecasting" AND all:"neural network"',
            'all:"anomaly detection" AND all:"deep learning"',
        ],
    },
}
TARGET_PAPERS = sum(cfg["target"] for cfg in DOMAINS.values())
# 每条检索式拉取的候选数：28 与历史版本一致，保证 EEG 32 篇验证集可完整复现
PER_QUERY_CANDIDATES = 28
MIN_CHARS = 3000
MIN_REFERENCES = 8
FIXTURES_PER_DOMAIN = 2

# --------------------------------------------------------------------------- #
# arXiv 元数据
# --------------------------------------------------------------------------- #


def collect_candidates(domain_queries: list[str], seen: set[str]) -> list[dict[str, Any]]:
    """按检索式列表拉取候选元数据；seen 为跨领域全局去重集合。"""
    found: dict[str, dict[str, Any]] = {}
    for query in domain_queries:
        result = web_fetch.fetch_text(
            API,
            params={"search_query": query, "max_results": str(PER_QUERY_CANDIDATES), "sortBy": "relevance"},
        )
        root = ET.fromstring(result.body)
        for entry in root.findall(f"{{{ATOM}}}entry"):
            id_url = entry.findtext(f"{{{ATOM}}}id", "")
            arxiv_id = id_url.rsplit("/", 1)[-1]
            if not arxiv_id or arxiv_id in seen or arxiv_id in found:
                continue
            found[arxiv_id] = {
                "arxiv_id": arxiv_id,
                "title": " ".join((entry.findtext(f"{{{ATOM}}}title", "") or "").split()),
                "published": entry.findtext(f"{{{ATOM}}}published", "")[:10],
                "primary_category": (entry.findtext(f"{{{ARXIV}}}primary_category", "") or ""),
            }
            seen.add(arxiv_id)
    return sorted(found.values(), key=lambda item: item["arxiv_id"])


# --------------------------------------------------------------------------- #
# HTML → Markdown 文本
# --------------------------------------------------------------------------- #

_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form"}
_HEADING_TAGS = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### "}
_ABSTRACT_HINTS = ("abstract",)
_REFERENCES_HINTS = ("references", "bibliography")


class _LaTeXMLExtractor(HTMLParser):
    """从 arXiv 原生 HTML（LaTeXML 风格）抽取结构化 Markdown 文本。

    保留：标题层级、段落、图题注（figcaption）、表格行（| 分隔）、
    参考文献条目（bib list 顺序编号为 [n]）。丢弃：脚本样式、导航、数学标记。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._in_abstract = False
        self._in_references = False
        self._heading_prefix = ""
        self._bib_counter = 0
        self._in_caption = False
        self._in_cell = False

    def _flush(self) -> str:
        text = " ".join("".join(self._buffer).split())
        self._buffer = []
        return text

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str]]) -> None:
        attr = dict(attrs)
        classes = attr.get("class", "") or ""
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _HEADING_TAGS:
            self._flush()
            self._heading_prefix = _HEADING_TAGS[tag]
        if tag == "section" and "ltx_abstract" in classes:
            self._in_abstract = True
        if tag == "figcaption":
            self._in_caption = True
        if tag in ("td", "th"):
            self._in_cell = True
            self._buffer.append(" | ")
        if tag == "li" and "ltx_bibitem" in classes:
            self._flush()
            self._bib_counter += 1
            self._heading_prefix = ""
            self._buffer.append(f"[{self._bib_counter}] ")
        if tag in ("p", "figcaption", "tr", "li"):
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _HEADING_TAGS:
            text = self._flush()
            if text:
                lowered = text.lower()
                if any(hint in lowered for hint in _ABSTRACT_HINTS) and len(text) < 40:
                    self.lines.append("## Abstract")
                elif any(hint in lowered for hint in _REFERENCES_HINTS) and len(text) < 40:
                    self.lines.append("## References")
                    self._in_references = True
                else:
                    self.lines.append(self._heading_prefix + text)
            self._heading_prefix = ""
            return
        if tag in ("p", "figcaption", "tr", "li", "td", "th"):
            if tag in ("td", "th"):
                self._in_cell = False
            text = self._flush()
            if not text:
                return
            if self._in_caption:
                self.lines.append(text)
                self._in_caption = False
            elif self._heading_prefix and not self._in_references:
                self.lines.append(self._heading_prefix + text)
                self._heading_prefix = ""
            else:
                self.lines.append(text)

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        self._buffer.append(data)

    def text(self) -> str:
        blocks: list[str] = []
        for line in self.lines:
            blocks.append(line)
        return "\n\n".join(blocks) + "\n"


def html_to_markdown(html: str) -> str:
    extractor = _LaTeXMLExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # noqa: BLE001 - 单篇解析失败直接淘汰该篇
        return ""
    return extractor.text()


# --------------------------------------------------------------------------- #
# 埋坑（阳性对照）
# --------------------------------------------------------------------------- #

_HYPE_SENTENCE = (
    "Our approach is revolutionary and undoubtedly state-of-the-art, "
    "perfectly removing all artifacts."
)

_FAKE_PERCENT_CANDIDATES = (99.9, 97.3, 88.8, 91.7, 86.1, 93.5, 89.9)


def _pick_fake_percent(text: str) -> str | None:
    """选一个正文（任意形式）都不存在的百分比候选值；全撞车返回 None。"""
    numbers = [float(m.group()) for m in re.finditer(r"\d+(?:\.\d+)?", text)]
    for candidate in _FAKE_PERCENT_CANDIDATES:
        clash = any(
            num == candidate or (0 < num < 1 and abs(num * 100 - candidate) <= 0.05)
            for num in numbers
        )
        if not clash:
            return f"{candidate:g}"
    return None


def _anchors(text: str) -> tuple[int, tuple[int, int]] | None:
    """结构容错锚点：返回 (摘要段末位置, 引言段区间)。

    部分渲染无独立 Abstract 标题（摘要直接跟在正文标题后），此时把标题后
    第一段视为摘要；引言段取第一个章节标题后的第一段。
    """
    abstract = re.search(r"## Abstract\n\n([^\n]+)", text)
    if abstract:
        abstract_end = abstract.end(1)
    else:
        fallback = re.search(r"\A# [^\n]+\n\n([^\n#]+)", text)
        if not fallback:
            return None
        abstract_end = fallback.end(1)
    intro = re.search(r"^##+ [^\n]+\n\n([^\n#]+)", text, re.M)
    intro_span = (intro.start(1), intro.end(1)) if intro else (abstract_end, abstract_end)
    return abstract_end, intro_span


def plant_variants(text: str) -> list[dict[str, Any]]:
    """对真实论文文本植入确定性缺陷；返回 [{name, text, expected_source, expected_check}]。"""
    variants: list[dict[str, Any]] = []
    anchors = _anchors(text)
    abstract_end, intro_span = anchors if anchors else (0, (0, 0))

    # 1) 引用越界：正文（参考文献区之前）第一个 [n] 改成 [999]
    body_end = text.find("## References")
    body_text = text if body_end < 0 else text[:body_end]
    match = re.search(r"\[(\d{1,3})\]", body_text)
    if match:
        variants.append(
            {
                "name": "citation_out_of_bounds",
                "text": text[: match.start()] + "[999]" + text[match.end() :],
                "expected_source": "cite-manager",
                "expected_check": "out_of_bounds",
            }
        )

    # 2) 摘要假数字：摘要段落末尾追加正文不存在的百分比声明
    #    （动态选值：避免撞上正文既有的 x% 或 0.0x 小数等价形式——
    #     那样植入值与正文自洽，检测器不报是正确行为）
    if abstract_end:
        fake = _pick_fake_percent(text)
        if fake:
            variants.append(
                {
                    "name": "abstract_fake_percent",
                    "text": text[:abstract_end]
                    + f" Our method achieves {fake}% denoising accuracy."
                    + text[abstract_end:],
                    "expected_source": "consistency-check",
                    "expected_check": "abstract_number",
                }
            )

    # 3) 幽灵图表引用 + 4) 浮夸句：插入引言段末尾
    if intro_span[1] > intro_span[0]:
        insert_at = intro_span[1]
        variants.append(
            {
                "name": "ghost_figure_reference",
                "text": text[:insert_at] + " The results are shown in Figure 99. " + text[insert_at:],
                "expected_source": "consistency-check",
                "expected_check": "figure_reference",
            }
        )
        variants.append(
            {
                "name": "hype_injection",
                "text": text[:insert_at] + f" {_HYPE_SENTENCE} " + text[insert_at:],
                "expected_source": "claim-guard",
                "expected_check": None,
            }
        )

    # 5) 重复文献条目：复制第一条参考文献。
    #    仅限顺序编号文献表（[1] 起始）：年份标注式（ACL 风格 [2018]，同年
    #    多条合法）不存在"重复编号"缺陷语义，植入即产生错误金标。
    ref_match = re.search(r"(## References\n\n)\[(\d+)\][^\n]+", text)
    if ref_match and ref_match.group(2) == "1":
        full_match = re.search(r"(## References\n\n)(\[\d+\][^\n]+)", text)
        variants.append(
            {
                "name": "duplicated_reference",
                "text": text[: full_match.end(2)] + "\n\n" + full_match.group(2) + text[full_match.end(2) :],
                "expected_source": "cite-manager",
                "expected_check": "duplicated_entry",
            }
        )
    return variants


# --------------------------------------------------------------------------- #
# 评测
# --------------------------------------------------------------------------- #


def audit_issues(text: str) -> list[dict[str, Any]]:
    registry = run_skill("issue-registry", {"text": text, "language": "en"})
    return registry.get("issues") or []


def evaluate_paper(paper: dict[str, Any], text: str) -> dict[str, Any]:
    # 阴性对照：全源审计，期望无 P0
    intact_issues = audit_issues(text)
    p0 = [i for i in intact_issues if i["priority"] == "P0"]
    p1 = [i for i in intact_issues if i["priority"] == "P1"]

    positives: list[dict[str, Any]] = []
    for variant in plant_variants(text):
        issues = audit_issues(variant["text"])
        planted_terms = ("revolutionary", "undoubtedly", "state-of-the-art", "perfectly")
        detected = False
        evidence = ""
        for issue in issues:
            if issue["source"] != variant["expected_source"]:
                continue
            check = (issue.get("metadata") or {}).get("check")
            found = str((issue.get("metadata") or {}).get("found") or "")
            if variant["expected_check"] is not None:
                hit = check == variant["expected_check"]
            else:
                # 浮夸句：命中必须锚定植入词本身，不能把原文既有的
                # claim-guard 发现误计为检测成功
                hit = any(term in found.lower() or term in issue["title"].lower() for term in planted_terms)
            if hit:
                detected = True
                evidence = f"{issue['issue_id']} [{issue['priority']}] {issue['title'][:60]}"
                break
        positives.append(
            {
                "name": variant["name"],
                "expected_source": variant["expected_source"],
                "expected_check": variant["expected_check"],
                "detected": detected,
                "evidence": evidence,
            }
        )
    ref_section = text.split("## References")[-1]
    return {
        "arxiv_id": paper["arxiv_id"],
        "domain": paper.get("domain", ""),
        "title": paper["title"],
        "published": paper["published"],
        "chars": len(text),
        "reference_count": len(re.findall(r"^\[\d+\]", ref_section, re.M)),
        "negative_control": {
            "p0_count": len(p0),
            "p1_count": len(p1),
            "p0_details": [
                f"{i['source']}::{(i.get('metadata') or {}).get('check')} {i['title'][:50]}" for i in p0
            ],
            "p1_details": [
                f"{i['source']}::{(i.get('metadata') or {}).get('check')} {i['title'][:50]}" for i in p1
            ],
        },
        "positives": positives,
    }


def derive_fixtures(out_root: Path, per_domain: int = FIXTURES_PER_DOMAIN) -> Path:
    """从已评测论文派生短摘录夹具（提交进 data/，离线回归用）。

    每领域取前 per_domain 篇入选论文；摘录 = 标题 + 摘要段 + 两段正文 +
    完整参考文献表（保留全部条目，避免截断引用表在阴性对照上制造假 P0）。
    真实论文全文只留在 output/。夹具目录同步清理不在新 manifest 里的旧文件。
    """
    fixtures_dir = ROOT / "data" / "eval" / "gold-standard" / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = out_root / "corpus"
    manifest: dict[str, Any] = {}
    papers = json.loads((out_root / "gold.json").read_text(encoding="utf-8"))
    taken_per_domain: dict[str, int] = {}
    for paper in papers:
        domain = str(paper.get("domain") or "unknown")
        if taken_per_domain.get(domain, 0) >= per_domain:
            continue
        corpus_file = corpus_dir / f"{paper['arxiv_id'].replace('/', '-')}.md"
        if not corpus_file.exists():
            continue
        text = corpus_file.read_text(encoding="utf-8")
        anchor = _anchors(text)
        if not anchor:
            continue
        parts = []
        title_match = re.match(r"(# [^\n]+)", text)
        if title_match:
            parts.append(title_match.group(1))
        abstract = re.search(r"## Abstract\n\n([^\n#]+)", text) or re.search(
            r"\A# [^\n]+\n\n([^\n#]+)", text
        )
        if abstract:
            parts.append(abstract.group(1).strip())
        paragraphs = re.findall(r"^([^\n#].{80,})$", text, re.M)
        for paragraph in paragraphs[:2]:
            if abstract and paragraph.strip() == abstract.group(1).strip():
                continue
            parts.append(paragraph.strip())
        refs = text.split("## References")
        if len(refs) == 2:
            parts.append("## References\n\n" + refs[1].strip())
        excerpt = "\n\n".join(parts) + "\n"
        name = f"{paper['arxiv_id'].replace('/', '-')}-excerpt.md"
        (fixtures_dir / name).write_text(excerpt, encoding="utf-8")
        expected = [v["name"] for v in plant_variants(excerpt)]
        manifest[name] = {
            "arxiv_id": paper["arxiv_id"],
            "domain": domain,
            "title": paper["title"],
            "expected_variants": expected,
        }
        taken_per_domain[domain] = taken_per_domain.get(domain, 0) + 1
    # 清理不在新 manifest 里的旧夹具（换届后遗留）
    for stale in fixtures_dir.glob("*-excerpt.md"):
        if stale.name not in manifest:
            stale.unlink()
    (fixtures_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return fixtures_dir


def _reject_file(corpus_dir: Path, candidates_dir: Path, arxiv_id: str) -> str:
    """把未入选论文的缓存全文移入 candidates/；返回移动后的文件名。"""
    source = corpus_dir / f"{arxiv_id.replace('/', '-')}.md"
    if not source.exists():
        return ""
    candidates_dir.mkdir(parents=True, exist_ok=True)
    target = candidates_dir / source.name
    shutil.move(str(source), str(target))
    return target.name


def build(out_root: Path = ROOT / "output" / "gold-standard", target: int | None = None) -> dict[str, Any]:
    """按领域配额构建评测集；corpus/ 只保留入选论文，淘汰稿移入 candidates/。

    target 为总篇数上限（按领域配额等比缩放）；默认取 DOMAINS 配额之和。
    """
    out_root.mkdir(parents=True, exist_ok=True)
    corpus_dir = out_root / "corpus"
    corpus_dir.mkdir(exist_ok=True)
    candidates_dir = out_root / "candidates"
    candidates_dir.mkdir(exist_ok=True)

    scale = 1.0 if target is None else target / TARGET_PAPERS
    seen: set[str] = set()
    papers: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for domain, domain_cfg in DOMAINS.items():
        domain_target = max(1, round(domain_cfg["target"] * scale))
        if len([p for p in papers if p.get("domain") == domain]) >= domain_target:
            continue
        print(f"[{domain}] 候选检索：{len(domain_cfg['queries'])} 条检索式")
        candidates = collect_candidates(domain_cfg["queries"], seen)
        print(f"[{domain}] 候选 {len(candidates)} 篇，目标 {domain_target} 篇可评测全文")
        for paper in candidates:
            if len([p for p in papers if p.get("domain") == domain]) >= domain_target:
                break
            arxiv_id = paper["arxiv_id"]
            cache_file = corpus_dir / f"{arxiv_id.replace('/', '-')}.md"
            if cache_file.exists():
                text = cache_file.read_text(encoding="utf-8")
            else:
                try:
                    fetched = web_fetch.fetch_text(HTML_ENDPOINT.format(arxiv_id=arxiv_id))
                    text = html_to_markdown(fetched.body or "")
                except Exception as exc:  # noqa: BLE001 - 单篇失败继续
                    print(f"  跳过 {arxiv_id}: {str(exc)[:60]}")
                    continue
                if len(text) < MIN_CHARS:
                    print(f"  跳过 {arxiv_id}: 抽取文本过短 ({len(text)} chars)")
                    continue
                cache_file.write_text(text, encoding="utf-8")
            ref_count = len(re.findall(r"^\[\d+\]", text.split("## References")[-1], re.M))
            if ref_count < MIN_REFERENCES:
                print(f"  跳过 {arxiv_id}: 参考文献不足 ({ref_count})")
                rejected.append(
                    {"arxiv_id": arxiv_id, "domain": domain, "reason": f"references_lt_{MIN_REFERENCES}",
                     "file": _reject_file(corpus_dir, candidates_dir, arxiv_id)}
                )
                continue
            print(f"  评测 {arxiv_id} ({len(text)} chars, {ref_count} refs) — {paper['title'][:50]}")
            record = evaluate_paper({**paper, "domain": domain}, text)
            if record["negative_control"]["p0_count"]:
                # 阴性对照不干净 = 抽取工件（HTML→Markdown 引用错位等），不入官方集
                print(f"  淘汰 {arxiv_id}: 阴性对照出现 P0（抽取工件）")
                rejected.append(
                    {"arxiv_id": arxiv_id, "domain": domain, "reason": "negative_control_p0",
                     "file": _reject_file(corpus_dir, candidates_dir, arxiv_id),
                     "p0": record["negative_control"]["p0_details"][:3]}
                )
                continue
            papers.append(record)

    (out_root / "gold.json").write_text(
        json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 收尾清扫：corpus/ 里不属于官方集的旧缓存全部移入 candidates/；
    # candidates/ 里历史遗留文件也登记进清单（标记 previous_run_candidate）。
    official = {f"{p['arxiv_id'].replace('/', '-')}.md" for p in papers}
    for stale in corpus_dir.glob("*.md"):
        if stale.name not in official:
            rejected.append(
                {"arxiv_id": stale.stem, "domain": "", "reason": "not_selected_stale_cache",
                 "file": _reject_file(corpus_dir, candidates_dir, stale.stem)}
            )
    listed_files = {item.get("file") for item in rejected}
    for existing in sorted(candidates_dir.glob("*.md")):
        if existing.name not in listed_files:
            rejected.append(
                {"arxiv_id": existing.stem, "domain": "", "reason": "previous_run_candidate",
                 "file": existing.name}
            )
    index = {
        "schema_version": 2,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "paper_count": len(papers),
        "papers": [
            {
                "arxiv_id": p["arxiv_id"],
                "domain": p.get("domain", ""),
                "title": p["title"],
                "published": p["published"],
                "chars": p["chars"],
                "reference_count": p["reference_count"],
                "file": f"corpus/{p['arxiv_id'].replace('/', '-')}.md",
            }
            for p in papers
        ],
        "candidate_count": len(rejected),
        "candidates": rejected,
        "layout": {
            "corpus/": "官方评测集全文（gold.json 收录论文，且仅这些）",
            "candidates/": "已抓取未入选的候选全文（含淘汰原因，可作缓存复用）",
            "gold.json": "逐篇金标判定（阴性对照 + 5 类阳性对照）",
            "index.json": "本清单",
        },
    }
    (out_root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = summarize(papers, out_root)
    # 汇总与短摘录夹具进入 data/（提交）；全文只在本地 output/
    summary_path = ROOT / "data" / "eval" / "gold-standard" / "gold-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_payload = {**summary, "papers": [
        {
            "arxiv_id": p["arxiv_id"],
            "domain": p.get("domain", ""),
            "title": p["title"],
            "published": p["published"],
            "chars": p["chars"],
            "reference_count": p["reference_count"],
            "negative_control": p["negative_control"],
            "positives": p["positives"],
        }
        for p in papers
    ]}
    summary_path.write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    derive_fixtures(out_root)
    return summary


def summarize(papers: list[dict[str, Any]], out_root: Path) -> dict[str, Any]:
    total = len(papers)
    clean = [p for p in papers if p["negative_control"]["p0_count"] == 0]
    all_positives = [v for p in papers for v in p["positives"]]
    detected = [v for v in all_positives if v["detected"]]
    by_name: dict[str, dict[str, int]] = {}
    for v in all_positives:
        bucket = by_name.setdefault(v["name"], {"total": 0, "detected": 0})
        bucket["total"] += 1
        bucket["detected"] += 1 if v["detected"] else 0
    by_domain: dict[str, dict[str, int]] = {}
    for p in papers:
        domain = str(p.get("domain") or "unknown")
        bucket = by_domain.setdefault(domain, {"papers": 0, "positives": 0, "detected": 0})
        bucket["papers"] += 1
        bucket["positives"] += len(p["positives"])
        bucket["detected"] += sum(1 for v in p["positives"] if v["detected"])
    summary = {
        "paper_count": total,
        "domain_count": len(by_domain),
        "papers_by_domain": {domain: bucket["papers"] for domain, bucket in by_domain.items()},
        "recall_by_domain": {
            domain: round(bucket["detected"] / bucket["positives"], 3) if bucket["positives"] else None
            for domain, bucket in by_domain.items()
        },
        "negative_control_clean_rate": round(len(clean) / total, 3) if total else None,
        "p0_false_positive_papers": [
            {"arxiv_id": p["arxiv_id"], "p0": p["negative_control"]["p0_details"]}
            for p in papers
            if p["negative_control"]["p0_count"]
        ],
        "positive_recall_overall": round(len(detected) / len(all_positives), 3)
        if all_positives
        else None,
        "positive_recall_by_defect": by_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    (out_root / "report.md").write_text(render_report(papers, summary), encoding="utf-8")
    return summary


def render_report(papers: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# 真实论文黄金标准评测报告",
        "",
        f"- 论文数：{summary['paper_count']}（arXiv 原生 HTML 全文，公开免密钥）",
        f"- 领域数：{summary.get('domain_count', 1)}（"
        + ", ".join(f"{k}={v}" for k, v in summary.get("papers_by_domain", {}).items())
        + "）",
        f"- 阴性对照干净率（真实论文全源审计无 P0）：{summary['negative_control_clean_rate']}",
        f"- 阳性对照总召回（5 类植入缺陷）：{summary['positive_recall_overall']}",
        "",
        "## 分领域召回",
        "",
        "| 领域 | 论文数 | 召回 |",
        "| --- | --- | --- |",
    ]
    for domain, recall in summary.get("recall_by_domain", {}).items():
        count = summary.get("papers_by_domain", {}).get(domain, 0)
        lines.append(f"| {domain} | {count} | {recall} |")
    lines += [
        "",
        "## 分缺陷召回",
        "",
        "| 缺陷 | 召回 |",
        "| --- | --- |",
    ]
    for name, bucket in summary["positive_recall_by_defect"].items():
        lines.append(f"| {name} | {bucket['detected']}/{bucket['total']} |")
    lines += ["", "## P0 误报明细（真实论文）", ""]
    if not summary["p0_false_positive_papers"]:
        lines.append("无。")
    for item in summary["p0_false_positive_papers"]:
        lines.append(f"- {item['arxiv_id']}: {item['p0']}")
    lines += ["", "## 逐篇结果", ""]
    for p in papers:
        pos = ", ".join(f"{v['name']}={'√' if v['detected'] else '×'}" for v in p["positives"])
        lines.append(
            f"- [{p.get('domain', '?')}] {p['arxiv_id']} {p['title'][:48]}："
            f"P0={p['negative_control']['p0_count']} P1={p['negative_control']['p1_count']} | {pos}"
        )
    return "\n".join(lines) + "\n"


def run(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = args or {}
    out_root = Path(args.get("out") or ROOT / "output" / "gold-standard")
    target = args.get("target")
    return build(out_root=out_root, target=int(target) if target else None)


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
