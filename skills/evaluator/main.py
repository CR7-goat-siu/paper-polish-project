"""evaluator Skill：润色质量评测（纯离线）。

实现四类结构化评测（对应用户评估方法论）：
1. objectivity  —— 对抗性测试：植入浮夸表述，检验去浮夸 + 数据锚定保持。
2. terminology  —— 术语退化测试：术语召回率 + 自造词拦截率。
3. grammar      —— GEC 基线：GLEU、逐错误修正率；clean 用例防过度修正。
4. fidelity     —— 全套件数字保留率（信息保真度底线）。

polisher 来源（本项目不发起任何网络请求）：
- mode=oracle    直接用测试集参考答案作为输出，校准指标上界（非实测）。
- mode=offline   输出=输入，指标地板（回归基线）。
- mode=archived  从 responses_dir 读取归档的模型响应逐用例评分——
  基线跑分唯一可复现的方式：归档响应文件 + 本模式，永远可用当前代码重算。
- args["polisher"] 注入 callable(text, meta) -> str（测试/自研执行器）。

另外支持 prepare_ab：把结果写成匿名 A/B 文件供人工盲评。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import fidelity  # noqa: E402
from utils.config_loader import EVAL_DIR, normalize_language  # noqa: E402
from utils.differ import diff_stats  # noqa: E402
from utils.eval_metrics import (  # noqa: E402
    correction_stats,
    gleu,
    objectivity_score,
    terminology_scores,
)
from utils.logger import get_logger  # noqa: E402
from utils.skill_loader import run_skill  # noqa: E402

logger = get_logger("evaluator")

SKILL_NAME = "evaluator"
Polisher = Callable[[str, dict[str, Any]], str]


def _load_suite(name: str) -> dict[str, Any]:
    path = EVAL_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"评测数据集不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _make_polisher(
    *,
    language: str,
    strength: str,
    mode: str,
    domain: str,
    inject: Polisher | None,
    responses_dir: str | Path | None = None,
) -> Polisher:
    if inject is not None:
        return inject

    if mode == "archived":
        # 可复现评测：从 responses_dir/<case_id>.txt 读取coding harness 的归档响应。
        if not responses_dir:
            raise ValueError("archived 模式需要 responses_dir")
        directory = Path(responses_dir)
        cache = {path.stem: path.read_text(encoding="utf-8-sig") for path in directory.glob("*.txt")}

        def polish(text: str, meta: dict[str, Any]) -> str:
            case_id = str(meta.get("id", ""))
            if case_id not in cache:
                raise KeyError(f"归档响应缺少用例 {case_id}（{directory}）")
            return cache[case_id]

        return polish

    def polish(text: str, meta: dict[str, Any]) -> str:
        # oracle：参考答案即输出；offline：原样输出（指标地板）。
        if mode == "oracle":
            return str(meta.get("reference") or text)
        return text

    return polish



def _safe_polish(polish: Polisher, text: str, meta: dict[str, Any]) -> tuple[str, str]:
    """单用例容错：润色失败返回错误说明，不中断整轮评测。"""
    try:
        return polish(text, meta), ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("polish failed for case: %s", exc)
        return text, str(exc)

def _run_claim_guard(text: str, language: str) -> dict[str, Any]:
    try:
        return run_skill("claim-guard", {"text": text, "language": language, "mode": "local"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("claim-guard 调用失败: %s", exc)
        return {"findings": [], "summary": {"error": 0, "warning": 0, "info": 0, "total": 0}}


def _eval_objectivity(
    suite: dict[str, Any], polish: Polisher, language: str
) -> dict[str, Any]:
    cases = suite.get("cases", [])
    results: list[dict[str, Any]] = []
    detection_hits = 0
    for case in cases:
        source = str(case["text"])
        output, polish_error = _safe_polish(polish, source, case)
        guard = _run_claim_guard(output, language)
        source_guard = _run_claim_guard(source, language)
        if source_guard["summary"]["error"] > 0:
            detection_hits += 1
        numbers = fidelity.numbers_preserved(source, output)
        score = objectivity_score(
            source,
            output,
            [str(w) for w in case.get("hype_to_remove", [])],
            guard["summary"]["error"],
            len(numbers["missing"]),
        )
        stats = diff_stats(source, output)
        results.append(
            {
                "id": case["id"],
                **score,
                "polish_error": polish_error,
                "numbers_added": numbers["added"][:5],
                "change_ratio": stats["change_ratio"],
                "guard_detected_in_source": source_guard["summary"]["error"] > 0,
                "output_preview": output[:240],
            }
        )
    succeeded = sum(1 for r in results if r["success"])
    return {
        "cases": results,
        "dehype_rate": round(succeeded / max(1, len(results)), 4),
        "guard_detection_rate": round(detection_hits / max(1, len(results)), 4),
        "case_count": len(results),
    }


def _eval_terminology(
    suite: dict[str, Any], polish: Polisher, language: str
) -> dict[str, Any]:
    cases = suite.get("cases", [])
    results: list[dict[str, Any]] = []
    for case in cases:
        source = str(case["text"])
        output, polish_error = _safe_polish(polish, source, case)
        scores = terminology_scores(
            output,
            [str(t) for t in case.get("expect_canonical", [])],
            [str(t) for t in case.get("expect_removed", [])],
            language,
        )
        results.append(
            {"id": case["id"], **scores, "polish_error": polish_error, "output_preview": output[:240]}
        )
    recall = sum(r["recall"] for r in results) / max(1, len(results))
    interception = sum(r["interception"] for r in results) / max(1, len(results))
    return {
        "cases": results,
        "terminology_recall": round(recall, 4),
        "coined_word_interception": round(interception, 4),
        "case_count": len(results),
    }


def _eval_grammar(
    suite: dict[str, Any], polish: Polisher, language: str
) -> dict[str, Any]:
    cases = suite.get("cases", [])
    clean_cases = suite.get("clean_cases", [])
    threshold = float(suite.get("overcorrection_changed_ratio_max", 0.15))
    results: list[dict[str, Any]] = []
    for case in cases:
        source = str(case["text"])
        output, polish_error = _safe_polish(polish, source, case)
        stats = correction_stats(source, output, [str(e) for e in case.get("expected_errors", [])])
        numbers = fidelity.numbers_preserved(source, output)
        results.append(
            {
                "id": case["id"],
                **stats,
                "gleu": round(gleu(source, output, str(case.get("reference", ""))), 4),
                "numbers_missing": numbers["missing"][:5],
                "polish_error": polish_error,
                "output_preview": output[:240],
            }
        )
    clean_results: list[dict[str, Any]] = []
    overcorrected = 0
    for case in clean_cases:
        source = str(case["text"])
        # 防过度修正本质上考核的是轻改行为：clean 用例固定用 light 档
        output, _ = _safe_polish(polish, source, {**case, "strength": "light"})
        stats = diff_stats(source, output)
        preserved = stats["change_ratio"] <= threshold
        if not preserved:
            overcorrected += 1
        clean_results.append(
            {"id": case["id"], "change_ratio": stats["change_ratio"], "preserved": preserved}
        )
    return {
        "cases": results,
        "clean_cases": clean_results,
        "gleu_avg": round(sum(r["gleu"] for r in results) / max(1, len(results)), 4),
        "fix_rate_avg": round(sum(r["fix_rate"] for r in results) / max(1, len(results)), 4),
        "overcorrection_rate": round(overcorrected / max(1, len(clean_results)), 4),
        "overcorrection_threshold": threshold,
        "case_count": len(results),
        "clean_case_count": len(clean_results),
    }


def _eval_guardrails(language: str, strength: str = "deep") -> dict[str, Any]:
    """guardrails 基准（docs/hallucination-guardrails-plan.md L4）。

    负样本（expect_severity=error）必须 100% 判 fail（Recall）；
    负样本（expect_severity=warning）必须命中对应规则；
    正样本不得产生 error（FPR=0）。纯离线、可复现。
    """
    import time

    from utils.guardrails import evaluate_pair

    negative = _load_suite(f"guardrails-negative-{language}").get("cases", [])
    positive = _load_suite(f"guardrails-positive-{language}").get("cases", [])
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    error_total = error_blocked = 0
    warn_total = warn_hit = 0
    for case in negative:
        report = evaluate_pair(
            str(case["source"]),
            str(case["polished"]),
            language=language,
            strength=strength,
        )
        rules = {item.rule for item in report.violations}
        expect = str(case.get("expect_severity") or "error")
        if expect == "error":
            error_total += 1
            blocked = report.verdict == "fail"
            error_blocked += 1 if blocked else 0
        else:
            warn_total += 1
            hit = case["rule"] in rules or report.verdict == "fail"
            warn_hit += 1 if hit else 0
        results.append(
            {
                "id": case["id"],
                "rule": case["rule"],
                "expect_severity": expect,
                "verdict": report.verdict,
                "blocked": report.verdict == "fail",
                "rule_hit": case["rule"] in rules,
            }
        )
    false_positives: list[dict[str, Any]] = []
    for case in positive:
        report = evaluate_pair(
            str(case["source"]),
            str(case["polished"]),
            language=language,
            strength=strength,
        )
        errors = [item.rule for item in report.violations if item.severity == "error"]
        if errors:
            false_positives.append({"id": case["id"], "rules": errors})
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    total_cases = len(negative) + len(positive)
    return {
        "cases": results,
        "negative_count": len(negative),
        "positive_count": len(positive),
        "recall": round(error_blocked / max(1, error_total), 4),
        "warning_rule_hit_rate": round(warn_hit / max(1, warn_total), 4),
        "fpr": round(len(false_positives) / max(1, len(positive)), 4),
        "false_positives": false_positives,
        "blocked": error_blocked,
        "leaked": error_total - error_blocked,
        "elapsed_ms": elapsed_ms,
        "avg_ms_per_case": round(elapsed_ms / max(1, total_cases), 3),
    }


def _eval_fidelity(
    suites_results: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """全套件数字保真度汇总：objectivity 与 grammar 用例的数字保留情况。"""
    checked = 0
    violated = 0
    for suite_name in ("objectivity", "grammar"):
        suite = suites_results.get(suite_name) or {}
        for case in suite.get("cases", []):
            if "numbers_missing" in case:
                checked += 1
                if case.get("numbers_missing"):
                    violated += 1
    return {
        "cases_with_numbers": checked,
        "number_violations": violated,
        "number_preservation_rate": round(1 - violated / max(1, checked), 4) if checked else None,
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# 润色质量评测报告（{report['language']} / {report['mode']} / {report['strength']}）", ""]
    overall = report.get("overall", {})
    lines.append("| 指标 | 数值 | 目标 |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| 去浮夸成功率 dehype_rate | {overall.get('dehype_rate')} | ≥ 0.8 |")
    lines.append(f"| 守卫检出率 guard_detection_rate | {overall.get('guard_detection_rate')} | ≥ 0.8 |")
    lines.append(f"| 术语召回率 terminology_recall | {overall.get('terminology_recall')} | ≥ 0.8 |")
    lines.append(f"| 自造词拦截率 coined_word_interception | {overall.get('coined_word_interception')} | ≥ 0.8 |")
    lines.append(f"| GLEU（grammar） | {overall.get('gleu_avg')} | ≥ 0.6 |")
    lines.append(f"| 错误修正率 fix_rate_avg | {overall.get('fix_rate_avg')} | ≥ 0.8 |")
    lines.append(f"| 过度修正率 overcorrection_rate | {overall.get('overcorrection_rate')} | ≤ 0.2 |")
    lines.append(f"| 数字保真率 number_preservation_rate | {overall.get('number_preservation_rate')} | = 1.0 |")
    if overall.get("guardrail_recall") is not None:
        lines.append(f"| 负样本拦截率 guardrail_recall | {overall.get('guardrail_recall')} | = 1.0 |")
        lines.append(f"| 正样本误杀率 guardrail_fpr | {overall.get('guardrail_fpr')} | = 0.0 |")
    lines.append("")
    for suite_name, suite in report.get("suites", {}).items():
        lines.append(f"## {suite_name}")
        lines.append("")
        for case in suite.get("cases", []):
            flag = ""
            if "success" in case:
                flag = "✅" if case["success"] else "❌"
            detail = {
                k: v
                for k, v in case.items()
                if k in ("hype_residual", "canonical_missing", "nonstandard_leaked", "remained", "numbers_missing", "change_ratio", "gleu")
                and v
            }
            lines.append(f"- {flag} {case.get('id')}: {detail if detail else 'ok'}")
        lines.append("")
    return "\n".join(lines)


def evaluate(
    *,
    language: str = "en",
    mode: str = "offline",
    strength: str = "deep",
    domain: str = "auto",
    suites: list[str] | None = None,
    polisher: Polisher | None = None,
    responses_dir: str | Path | None = None,
) -> dict[str, Any]:
    language = normalize_language(language)
    mode = (mode or "offline").lower()
    if mode not in ("offline", "oracle", "archived", "guardrails"):
        raise ValueError(f"未知 mode: {mode!r}，可选 offline / oracle / archived / guardrails")
    if mode == "guardrails":
        suites = ["guardrails"]
    else:
        suites = suites or ["objectivity", "terminology", "grammar"]
    polish = _make_polisher(
        language=language, strength=strength, mode=mode, domain=domain, inject=polisher,
        responses_dir=responses_dir,
    )

    suite_results: dict[str, dict[str, Any]] = {}
    if "objectivity" in suites:
        suite_results["objectivity"] = _eval_objectivity(
            _load_suite(f"objectivity-{language}"), polish, language
        )
    if "terminology" in suites:
        suite_results["terminology"] = _eval_terminology(
            _load_suite(f"terminology-{language}"), polish, language
        )
    if "grammar" in suites:
        suite_results["grammar"] = _eval_grammar(
            _load_suite(f"grammar-{language}"), polish, language
        )
    if "guardrails" in suites:
        suite_results["guardrails"] = _eval_guardrails(language, strength)

    fidelity_summary = _eval_fidelity(suite_results)
    overall: dict[str, Any] = {
        "dehype_rate": (suite_results.get("objectivity") or {}).get("dehype_rate"),
        "guard_detection_rate": (suite_results.get("objectivity") or {}).get("guard_detection_rate"),
        "terminology_recall": (suite_results.get("terminology") or {}).get("terminology_recall"),
        "coined_word_interception": (suite_results.get("terminology") or {}).get("coined_word_interception"),
        "gleu_avg": (suite_results.get("grammar") or {}).get("gleu_avg"),
        "fix_rate_avg": (suite_results.get("grammar") or {}).get("fix_rate_avg"),
        "overcorrection_rate": (suite_results.get("grammar") or {}).get("overcorrection_rate"),
        "number_preservation_rate": fidelity_summary.get("number_preservation_rate"),
        "guardrail_recall": (suite_results.get("guardrails") or {}).get("recall"),
        "guardrail_fpr": (suite_results.get("guardrails") or {}).get("fpr"),
    }
    return {
        "skill": SKILL_NAME,
        "language": language,
        "mode": mode,
        "strength": strength,
        "domain": domain,
        "suites": suite_results,
        "fidelity": fidelity_summary,
        "overall": overall,
    }


def score_text_pair(
    source_text: str,
    polished_text: str,
    *,
    language: str = "en",
    strength: str = "deep",
    domain: str = "auto",
    references_text: str | None = None,
) -> dict[str, Any]:
    """给单个 source/polished 文本对打分，供迭代润色循环选择最佳版本。"""
    language = normalize_language(language)
    source_text = str(source_text or "")
    polished_text = str(polished_text or "")
    gate = run_skill(
        "review-gate",
        {
            "source_text": source_text,
            "polished_text": polished_text,
            "language": language,
            "strength": strength,
            "references_text": references_text or "",
            "mode": "local",
        },
    )
    claim = run_skill(
        "claim-guard",
        {"text": polished_text, "language": language, "domain": domain, "mode": "local"},
    )
    numbers = fidelity.numbers_preserved(source_text, polished_text)
    stats = diff_stats(source_text, polished_text)
    source_chars = len(source_text.strip())
    polished_chars = len(polished_text.strip())
    length_ratio = round(polished_chars / max(1, source_chars), 4)
    gate_summary = gate.get("summary") or {}
    claim_summary = claim.get("summary") or {}
    score = 100.0
    feedback: list[str] = []

    if gate.get("verdict") == "fail":
        score -= 60
        feedback.append("硬门禁未通过，禁止作为最佳候选")
    elif gate.get("verdict") == "warn":
        score -= 5
        feedback.append("硬门禁存在 warning，需进一步降低风险")
    score -= int(gate_summary.get("error") or 0) * 15
    score -= int(gate_summary.get("warning") or 0) * 3
    score -= int(claim_summary.get("error") or 0) * 10
    score -= int(claim_summary.get("warning") or 0) * 2
    score -= len(numbers.get("missing") or []) * 20
    score -= len(numbers.get("added") or []) * 12

    if numbers.get("missing"):
        feedback.append("必须恢复原文数字: " + ", ".join(map(str, numbers["missing"][:8])))
    if numbers.get("added"):
        feedback.append("不得新增原文不存在的数字: " + ", ".join(map(str, numbers["added"][:8])))

    similarity = float(stats.get("similarity_ratio") or 0.0)
    ideal_ranges = {
        "light": (0.72, 0.995),
        "deep": (0.42, 0.96),
        "rewrite": (0.18, 0.88),
    }
    low, high = ideal_ranges.get(strength, ideal_ranges["deep"])
    if similarity < low or similarity > high:
        score -= 8
        feedback.append(
            f"改动幅度不在 {strength} 档理想区间 {low:.2f}–{high:.2f}（当前 {similarity:.2f}）"
        )
    if length_ratio < 0.55 or length_ratio > 1.45:
        score -= 18
        feedback.append(f"长度比例异常（当前 {length_ratio:.2f}，目标 0.55–1.45）")
    elif length_ratio < 0.72 or length_ratio > 1.28:
        score -= 6
        feedback.append(f"长度比例偏离目标区间（当前 {length_ratio:.2f}）")

    gate_feedback = str(gate.get("retry_feedback") or "").strip()
    if gate_feedback:
        feedback.append(gate_feedback)
    for finding in claim.get("findings") or []:
        if finding.get("severity") in {"error", "warning"}:
            feedback.append(str(finding.get("message") or finding.get("code") or ""))
    score = round(max(0.0, min(100.0, score)), 2)
    hard_gate_pass = (
        gate.get("verdict") != "fail"
        and int(claim_summary.get("error") or 0) == 0
        and not numbers.get("missing")
        and (gate.get("metrics") or {}).get("citation_missing", 0) == 0
    )
    return {
        "skill": SKILL_NAME,
        "mode": "pair",
        "language": language,
        "strength": strength,
        "domain": domain,
        "score": score,
        "hard_gate_pass": hard_gate_pass,
        "feedback": "\n".join(item for item in feedback if item)[:4000],
        "metrics": {
            "gate_verdict": gate.get("verdict"),
            "gate_summary": gate_summary,
            "claim_summary": claim_summary,
            "similarity_ratio": similarity,
            "change_ratio": stats.get("change_ratio"),
            "length_ratio": length_ratio,
            "numbers_missing": numbers.get("missing") or [],
            "numbers_added": numbers.get("added") or [],
            "source_words": stats.get("source_words"),
            "polished_words": stats.get("polished_words"),
        },
    }


def prepare_ab(
    report: dict[str, Any],
    out_dir: Path,
    *,
    language: str,
    pairs: list[dict[str, str]] | None = None,
) -> Path:
    """把 (原文, 润色稿) 对写成匿名 A/B 盲评文件。

    盲化方式：先打乱文本顺序再分配 A/B 标签，答案表记录随机指派。
    pairs 为空时从评测集构建（reference 作为润色稿）；也可传入真实
    run 目录的 (full_text, processed) 对。
    """
    import random

    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random()
    if pairs is None:
        pairs = []
        objectivity = _load_suite(f"objectivity-{language}")
        for case in objectivity.get("cases", []):
            reference = str(case.get("reference") or "")
            if reference:
                pairs.append({"name": f"objectivity/{case['id']}", "source": str(case["text"]), "polished": reference})
        grammar_path = EVAL_DIR / f"grammar-{language}.json"
        if grammar_path.exists():
            for case in json.loads(grammar_path.read_text(encoding="utf-8")).get("cases", []):
                reference = str(case.get("reference") or "")
                if reference:
                    pairs.append({"name": f"grammar/{case['id']}", "source": str(case["text"]), "polished": reference})

    answer_key: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs, start=1):
        texts = [str(pair["source"]), str(pair["polished"])]
        rng.shuffle(texts)
        polished_label = "A" if texts[0] == str(pair["polished"]) else "B"
        pair_dir = out_dir / f"pair-{index:03d}"
        pair_dir.mkdir(parents=True, exist_ok=True)
        for label, text in zip(("A", "B"), texts):
            (pair_dir / f"{label}.txt").write_text(text, encoding="utf-8")
        answer_key.append({"pair": index, "name": pair.get("name", ""), "polished": polished_label})
    key_path = out_dir / "answer-key.json"
    key_path.write_text(json.dumps(answer_key, ensure_ascii=False, indent=2), encoding="utf-8")
    instructions = out_dir / "README.md"
    instructions.write_text(
        "# 盲评说明\n\n每个 pair-XXX 目录下 A/B 两个文件，随机一份为原文、另一份为润色结果。\n"
        "评分维度：数据客观度、学术阅读流畅度、信息保真度（各 1-5 分）。\n"
        "评分后对照 answer-key.json 统计两版得分差异（polished 字段记录随机指派结果）。\n",
        encoding="utf-8",
    )
    return key_path


def run(args: dict[str, Any]) -> dict[str, Any]:
    language = normalize_language(str(args.get("language", "en")))
    started = time.time()
    if str(args.get("mode") or "").lower() == "pair":
        return score_text_pair(
            str(args.get("source_text") or args.get("source") or ""),
            str(args.get("polished_text") or args.get("polished") or ""),
            language=language,
            strength=str(args.get("strength", "deep")),
            domain=str(args.get("domain", "auto")),
            references_text=args.get("references_text"),
        )
    polisher_inject = args.get("polisher")
    report = evaluate(
        language=language,
        mode=str(args.get("mode", "offline")),
        strength=str(args.get("strength", "deep")),
        domain=str(args.get("domain", "auto")),
        suites=args.get("suites"),
        polisher=polisher_inject if callable(polisher_inject) else None,
        responses_dir=args.get("responses_dir"),
    )
    if isinstance(args.get("meta"), dict):
        report["meta"] = args["meta"]
    report["elapsed_seconds"] = round(time.time() - started, 3)
    out_root = Path(args.get("out") or ROOT / "output" / "eval")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = out_root / f"{language}-{args.get('mode', 'offline')}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / "report.md"
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    report["output"] = str(out_dir)
    if args.get("prepare_ab"):
        pairs = None
        if args.get("ab_run_dir"):
            run_dir = Path(args["ab_run_dir"])
            parsed = json.loads((run_dir / "parsed.json").read_text(encoding="utf-8"))
            processed_files = sorted(run_dir.glob("*.processed.*"))
            if not processed_files:
                raise FileNotFoundError(f"{run_dir} 下未找到 *.processed.* 产物")
            pairs = [
                {
                    "name": run_dir.name,
                    "source": str(parsed.get("full_text", "")),
                    "polished": processed_files[0].read_text(encoding="utf-8"),
                }
            ]
        key_path = prepare_ab(report, out_dir / "ab", language=language, pairs=pairs)
        report["ab_answer_key"] = str(key_path)
    return report
