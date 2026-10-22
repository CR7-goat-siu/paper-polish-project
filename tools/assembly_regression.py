"""装配层回归检查：用一篇真实稿件验证 apply 之后稿件是否完整。

用途：项目每次升级后跑一遍，确认装配层没有引入静默的内容丢失/错序/篡改。

    py tools/assembly_regression.py <稿件.docx> --language cn [--strength deep]
                                     [--out output/<run>] [--docx-roundtrip]

方法：build → 以「恒等响应」回填（每块原样返回）→ apply。
恒等响应可稳定通过门禁，因此终稿与原文的任何差异都只能来自装配本身——
这是把"装配质量"从"润色质量"中隔离出来的唯一办法。

核心断言：恒等响应下，装配结果必须与解析器的 full_text **逐字节一致**。
（_splice_polished 对 polished==source 是恒等函数，故这是可达到的最强断言。）
"""

from __future__ import annotations

import collections
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.logger import get_logger  # noqa: E402
from utils.skill_loader import run_skill  # noqa: E402

LOG = get_logger("assembly-regression")

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
CITATION_RE = re.compile(r"\[\d+\]")
# 参考文献 chunk 不参与润色，其内容必须原样进入终稿
UNPOLISHED_KINDS = {"title", "references"}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        marker = "PASS" if ok else "FAIL"
        LOG.info("[%s] %s%s", marker, name, f" — {detail}" if detail else "")
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [name for name, ok, _ in self.rows if not ok]

    def render(self) -> str:
        width = max(len(name) for name, _, _ in self.rows) if self.rows else 10
        lines = ["", "=" * (width + 12), "装配层回归检查报告", "=" * (width + 12)]
        for name, ok, detail in self.rows:
            lines.append(f"  {'✓' if ok else '✗'}  {name:<{width}}  {detail}")
        lines.append("=" * (width + 12))
        passed = sum(1 for _, ok, _ in self.rows if ok)
        lines.append(f"  合计 {passed}/{len(self.rows)} 项通过")
        if self.failed:
            lines.append(f"  未通过: {', '.join(self.failed)}")
        return "\n".join(lines)


def _norm_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _counts(text: str, pattern: re.Pattern[str]) -> collections.Counter:
    return collections.Counter(pattern.findall(text))


def _fill_identity(jobs: list[dict]) -> list[dict]:
    for job in jobs:
        job["response"] = job["chunk_content"]
    return jobs


def run_regression(
    docx_path: Path,
    *,
    language: str,
    strength: str,
    out_dir: Path,
    docx_roundtrip: bool,
) -> tuple[Report, Path]:
    report = Report()
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dir = out_dir / docx_path.stem

    # ---------- 1. 解析 ----------
    parsed = run_skill("paper-parser", {"input": str(docx_path), "format": "auto"})
    chunks = parsed.get("chunks", [])
    full_text = str(parsed.get("full_text") or "")
    report.check(
        "解析无损重建",
        bool(parsed.get("integrity", {}).get("reconstructs")),
        f"{len(chunks)} chunks / {len(full_text)} chars",
    )

    polishable = [
        c
        for c in chunks
        if c.get("kind") in {"body", "abstract", "appendix", "section", "table"}
        and (c.get("heading") or "").strip() != str(parsed.get("title") or "").strip()
    ]
    LOG.info("可润色章节 %d 个，标题/参考文献等 %d 个不参与润色",
             len(polishable), len(chunks) - len(polishable))

    # ---------- 2. build ----------
    build = run_skill(
        "batch-processor",
        {
            "inputs": [str(docx_path)],
            "language": language,
            "phase": "build",
            "strength": strength,
            "out": str(out_dir),
        },
    )
    jobs_path = run_dir / "jobs.json"
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"] if jobs_path.exists() else []
    report.check("build 产出作业包", bool(jobs), f"{len(jobs)} 作业")

    sub_per_parent = collections.Counter(j.get("parent_chunk_id") for j in jobs)
    multi = {k: v for k, v in sub_per_parent.items() if v > 1}
    LOG.info("作业 %d 个；被切成多个子块的章节 %d 个", len(jobs), len(multi))
    if multi:
        LOG.info("  多子块章节: %s", dict(sorted(multi.items())))
    else:
        LOG.info("  注意：本篇没有章节被切成多个子块，多子块装配路径未被本文稿覆盖"
                 "（需更长章节或更小 max_chunk_tokens）")

    # ---------- 3. 恒等回填 ----------
    responses_dir = run_dir / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    for job in jobs:
        (responses_dir / f"{job['job_id']}.txt").write_text(
            job["chunk_content"], encoding="utf-8"
        )

    # ---------- 4. apply ----------
    apply_result = run_skill(
        "batch-processor",
        {
            "inputs": [str(docx_path)],
            "language": language,
            "phase": "apply",
            "strength": strength,
            "out": str(out_dir),
        },
    )
    result_json = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))

    # 各步骤的详细结果在 step-<NN>-<skill>/step-result.json，不在 result.json 里
    polish_step_file = run_dir / f"step-01-{language}-polish" / "step-result.json"
    report.check("apply 产出步骤结果", polish_step_file.exists())
    step_result = json.loads(polish_step_file.read_text(encoding="utf-8")) if polish_step_file.exists() else {}
    assembled = str(step_result.get("polished_text") or "")
    report.check("apply 产出终稿", bool(assembled), f"{len(assembled)} chars")

    # 恒等响应下，含浮夸词的章节会被门禁判 fail（响应没改掉"最佳"这类词），
    # 这是门禁在正确工作而非装配问题——Fail-Closed 会回退源文，装配结果不受影响。
    gate_failed = step_result.get("gate_failed_chunks") or []
    if gate_failed:
        LOG.info(
            "门禁未通过章节 %s（恒等响应预期现象：源文含浮夸词，响应未改写）；"
            "这些章节按 Fail-Closed 回退源文，不影响装配完整性断言",
            gate_failed,
        )

    # ---------- 5. 装配完整性 ----------
    report.check(
        "恒等装配逐字节一致（最强断言）",
        assembled == full_text,
        "终稿与解析 full_text 完全一致"
        if assembled == full_text
        else f"长度 {len(assembled)} vs {len(full_text)}",
    )
    report.check(
        "去空白后一致",
        _norm_whitespace(assembled) == _norm_whitespace(full_text),
        "无字符级丢失或新增",
    )

    missing = [
        c.get("id")
        for c in polishable
        if _norm_whitespace(str(c.get("content") or "")) not in _norm_whitespace(assembled)
    ]
    report.check("全部可润色章节在场", not missing, f"缺失: {missing}" if missing else f"{len(polishable)}/{len(polishable)}")

    order = [
        _norm_whitespace(str(c.get("content") or ""))[:40] for c in polishable
    ]
    positions = []
    ok_order = True
    cursor = 0
    for token in order:
        found = _norm_whitespace(assembled).find(token, cursor)
        if found < 0:
            ok_order = False
            break
        positions.append(found)
        cursor = found + len(token)
    report.check("章节顺序保持", ok_order and positions == sorted(positions), "按解析顺序递增")

    src_citations = _counts(full_text, CITATION_RE)
    out_citations = _counts(assembled, CITATION_RE)
    report.check(
        "引用编号零丢失",
        src_citations == out_citations,
        f"{sum(src_citations.values())} 处 → {sum(out_citations.values())} 处",
    )

    src_numbers = _counts(full_text, NUMBER_RE)
    out_numbers = _counts(assembled, NUMBER_RE)
    lost = src_numbers - out_numbers
    added = out_numbers - src_numbers
    report.check(
        "数值锚点零丢失零新增",
        not lost and not added,
        f"{sum(src_numbers.values())} 处；丢失 {dict(lost) or '无'}；新增 {dict(added) or '无'}",
    )

    for chunk in chunks:
        if chunk.get("kind") in UNPOLISHED_KINDS:
            content = str(chunk.get("content") or "")
            if content and content not in assembled:
                report.check(f"未润色块原样保留（{chunk.get('kind')}）", False, chunk.get("heading"))
                break
    else:
        report.check("未润色块原样保留", True, "标题/参考文献未被改动")

    # ---------- 6. 幂等 ----------
    with tempfile.TemporaryDirectory() as tmp:
        second = Path(tmp)
        run_skill(
            "batch-processor",
            {
                "inputs": [str(docx_path)],
                "language": language,
                "phase": "build",
                "strength": strength,
                "out": str(second),
            },
        )
        resp2 = second / run_dir.name / "responses"
        resp2.mkdir(parents=True, exist_ok=True)
        for job in json.loads((second / run_dir.name / "jobs.json").read_text(encoding="utf-8"))["jobs"]:
            (resp2 / f"{job['job_id']}.txt").write_text(job["chunk_content"], encoding="utf-8")
        run_skill(
            "batch-processor",
            {
                "inputs": [str(docx_path)],
                "language": language,
                "phase": "apply",
                "strength": strength,
                "out": str(second),
            },
        )
        second_step = second / run_dir.name / f"step-01-{language}-polish" / "step-result.json"
        second_text = (
            str(json.loads(second_step.read_text(encoding="utf-8")).get("polished_text") or "")
            if second_step.exists()
            else ""
        )
    report.check("重复 apply 幂等", second_text == assembled, "两次结果逐字节一致")

    # ---------- 7. docx 回写回路 ----------
    if docx_roundtrip:
        from utils.docx_io import write_polished_docx

        target = run_dir / f"{docx_path.stem}.polished.docx"
        replacements = {}
        for chunk in polishable:
            content = str(chunk.get("content") or "")
            if content in assembled:
                replacements[content] = content
        try:
            write_polished_docx(docx_path, target, replacements)
            reparsed = run_skill("paper-parser", {"input": str(target), "format": "auto"})
            report.check(
                "docx 回写后重读一致",
                _norm_whitespace(str(reparsed.get("full_text") or ""))
                == _norm_whitespace(full_text),
                f"{target.name}",
            )
        except Exception as exc:  # noqa: BLE001
            report.check("docx 回写后重读一致", False, f"{type(exc).__name__}: {exc}")

    return report, run_dir
