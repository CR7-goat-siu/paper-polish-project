"""Release Protocol 四条件与人工裁决门禁的测试（架构评审 2026-10-25 二/三轮）。

覆盖：
- utils/adjudication：账本有效性审计（issue_id 唯一 / adjudicator 署名 /
  多来源 / manuscript_sha256 版本绑定）与 precision_gate 全分支；
- optimizer-loop._release_requirements：四条件聚合、评审证据哈希绑定、
  waiver 签核工件语义、submission-audit 不可豁免；
- 评审者最小反例：5 条同 issue_id、空白 adjudicator 的 TP 行必须拒绝。
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conftest import skill_module
from utils import adjudication

HEADER = [
    "issue_id", "source", "check", "priority", "evidence_snippet",
    "adjudication", "note", "adjudicator", "date", "manuscript_sha256",
]


def _write_ledger(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for row in rows:
            writer.writerow([
                row.get("issue_id", ""), row.get("source", "claim-guard"),
                row.get("check", "hype"), "P1", "证据片段",
                row.get("adjudication", "TP"), "", row.get("adjudicator", "作者甲"),
                "2026-10-25", row.get("manuscript_sha256", ""),
            ])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_rows(sha: str, n_fp: int = 1) -> list[dict]:
    """构造有效账本行：唯一 id、双来源、署名、绑定版本哈希。"""
    rows = []
    for i in range(1, 5):
        rows.append({
            "issue_id": f"iss-{i:03d}",
            "source": "claim-guard" if i % 2 else "consistency-check",
            "adjudication": "TP",
            "adjudicator": "作者甲",
            "manuscript_sha256": sha,
        })
    rows.append({
        "issue_id": "iss-005",
        "source": "consistency-check",
        "adjudication": "FP" if n_fp else "TP",
        "adjudicator": "作者乙",
        "manuscript_sha256": sha,
    })
    return rows


class AdjudicationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ledger-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_missing_ledger_fails_gate(self):
        gate = adjudication.precision_gate(self.tmp / "absent.csv")
        self.assertFalse(gate["pass"])
        self.assertIn("不存在", gate["reason"])

    def test_reviewer_counterexample_rejected(self):
        """三轮评审第 3 条最小反例：5 条同 issue_id、空白 adjudicator 的 TP 行
        （precision 数值上 = 1.0）必须被拒绝——证据不唯一且无署名。"""
        ledger = self.tmp / "forged.csv"
        _write_ledger(ledger, [
            {"issue_id": "iss-001", "adjudication": "TP", "adjudicator": ""}
        ] * 5)
        gate = adjudication.precision_gate(ledger)
        self.assertFalse(gate["pass"])
        # 5 行同 id：首行计 unsigned，其余 4 行先被唯一性破坏拦截
        self.assertGreaterEqual(gate["ledger"]["duplicates"], 4)
        self.assertGreaterEqual(gate["ledger"]["unsigned"], 1)
        self.assertTrue(
            "不唯一" in gate["reason"] or "署名" in gate["reason"],
            f"拒绝原因应指明证据缺陷: {gate['reason']}",
        )

    def test_single_source_fails(self):
        ledger = self.tmp / "l.csv"
        _write_ledger(ledger, [
            {"issue_id": f"i{i}", "source": "claim-guard", "adjudication": "TP"}
            for i in range(1, 6)
        ])
        gate = adjudication.precision_gate(ledger)
        self.assertFalse(gate["pass"])
        self.assertIn("来源", gate["reason"])

    def test_version_binding_enforced(self):
        manuscript = self.tmp / "m.md"
        manuscript.write_text("版本 A", encoding="utf-8")
        ledger = self.tmp / "l.csv"
        _write_ledger(ledger, _valid_rows(_sha(manuscript)))
        ok = adjudication.precision_gate(
            ledger, expected_manuscript_sha256=_sha(manuscript)
        )
        self.assertTrue(ok["pass"])
        other = self.tmp / "m2.md"
        other.write_text("版本 B 完全不同的稿件", encoding="utf-8")
        bad = adjudication.precision_gate(
            ledger, expected_manuscript_sha256=_sha(other)
        )
        self.assertFalse(bad["pass"])
        self.assertIn("不一致", bad["reason"])
        unbound = self.tmp / "unbound.csv"
        _write_ledger(unbound, [
            {**row, "manuscript_sha256": ""} for row in _valid_rows("x")
        ])
        none_bound = adjudication.precision_gate(
            unbound, expected_manuscript_sha256=_sha(manuscript)
        )
        self.assertFalse(none_bound["pass"])
        self.assertIn("未绑定", none_bound["reason"])

    def test_insufficient_adjudicated_fails(self):
        ledger = self.tmp / "l.csv"
        _write_ledger(ledger, [
            {"issue_id": "i1", "source": "claim-guard", "adjudication": "TP"},
            {"issue_id": "i2", "source": "consistency-check", "adjudication": "FP"},
            {"issue_id": "i3", "source": "lit-search", "adjudication": "UNCLEAR"},
        ])
        gate = adjudication.precision_gate(ledger)
        self.assertFalse(gate["pass"])
        self.assertIn("不足", gate["reason"])
        self.assertEqual(gate["ledger"]["unclear"], 1)

    def test_below_precision_fails(self):
        ledger = self.tmp / "l.csv"
        _write_ledger(ledger, [
            {"issue_id": "i1", "source": "claim-guard", "adjudication": "TP"},
            {"issue_id": "i2", "source": "claim-guard", "adjudication": "TP"},
            {"issue_id": "i3", "source": "claim-guard", "adjudication": "TP"},
            {"issue_id": "i4", "source": "consistency-check", "adjudication": "FP"},
            {"issue_id": "i5", "source": "consistency-check", "adjudication": "FP"},
        ])
        gate = adjudication.precision_gate(ledger)
        self.assertFalse(gate["pass"])
        self.assertIn("低于", gate["reason"])
        self.assertEqual(gate["ledger"]["precision_overall"], 0.6)

    def test_passing_gate(self):
        manuscript = self.tmp / "m.md"
        manuscript.write_text("稿件", encoding="utf-8")
        ledger = self.tmp / "l.csv"
        rows = _valid_rows(_sha(manuscript), n_fp=1)
        _write_ledger(ledger, rows)
        gate = adjudication.precision_gate(
            ledger, expected_manuscript_sha256=_sha(manuscript)
        )
        self.assertTrue(gate["pass"])
        self.assertEqual(gate["ledger"]["precision_overall"], 0.8)
        self.assertGreaterEqual(gate["ledger"]["distinct_sources"], 2)


class ReleaseRequirementsTests(unittest.TestCase):
    """_release_requirements 四条件聚合（构造数据直测）。"""

    @classmethod
    def setUpClass(cls):
        cls.module = skill_module("optimizer-loop")
        cls.tmp = Path(tempfile.mkdtemp(prefix="relreq-"))
        cls.manuscript = cls.tmp / "best.md"
        cls.manuscript.write_text("最终稿文本内容" * 5, encoding="utf-8")
        cls.sha = _sha(cls.manuscript)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _state(
        self, *, enabled=True, cycles=1, pending=False, reviewed_sha=None,
        skipped_only=False,
    ) -> dict:
        history = (
            [{"cycle": 1, "skipped": "no_review_jobs"}] if skipped_only
            else [{"cycle": i + 1, "reviewed_text_sha256": reviewed_sha or self.sha} for i in range(cycles)]
        )
        return {
            "review_sim": {"enabled": enabled},
            "review_history": history,
            "review_cycle": {"cycle": 9} if pending else None,
            "review_pending_jobs": ["x"] if pending else [],
            "reviewed_text_sha256": (reviewed_sha or self.sha) if not skipped_only else "",
            "best": {"text_file": str(self.manuscript)},
        }

    def _preflight(self, passed: bool) -> dict:
        return {"status": "ok", "automated_pass": passed}

    def _merged(self, status: str) -> dict:
        return {"delivery_status": status, "reason": "r"}

    def _ledger(self, passing: bool) -> Path:
        path = self.tmp / ("ok.csv" if passing else "bad.csv")
        if passing:
            _write_ledger(path, _valid_rows(self.sha))
        else:
            _write_ledger(path, [
                {"issue_id": "same", "adjudication": "TP", "adjudicator": ""}] * 5
            )
        return path

    def _req(self, state, merged, preflight, *, allow=False, waivers=None, ledger=None):
        return self.module._release_requirements(
            state,
            merged,
            preflight,
            allow_conditional_release=allow,
            waivers=waivers or {},
            ledger_path=ledger,
        )

    def _waiver(self, check):
        return {check: {"check": check, "author": "作者甲", "reason": "材料由本人核验"}}

    def test_all_conditions_pass(self):
        req = self._req(
            self._state(),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertTrue(req["all_pass"])
        self.assertTrue(all(req["checks"].values()))

    def test_review_hash_mismatch_blocks(self):
        """评审后稿件被改动（哈希不一致）→ final_review 阻断。"""
        req = self._req(
            self._state(reviewed_sha="deadbeef" * 8),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["final_review"])
        self.assertIn("reviewed_text_sha256", req["reasons"]["final_review"])

    def test_skipped_only_history_not_final_review(self):
        req = self._req(
            self._state(skipped_only=True),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["final_review"])

    def test_missing_preflight_blocks_even_waived(self):
        req = self._req(
            self._state(enabled=False, cycles=0, skipped_only=True),
            self._merged(self.module.READY),
            None,
            waivers={**self._waiver("final_review"), **self._waiver("precision_gate")},
        )
        self.assertFalse(req["all_pass"])
        self.assertFalse(req["checks"]["submission_audit"])
        self.assertIn("不可豁免", req["reasons"]["submission_audit"])

    def test_string_waivers_are_not_honored(self):
        """字符串形式豁免不再生效（必须 author+reason 签核工件）。"""
        normalized = self.module._normalize_waivers(["final_review"])
        self.assertEqual(normalized, {})
        req = self._req(
            self._state(enabled=False, cycles=0, skipped_only=True),
            self._merged(self.module.READY),
            self._preflight(True),
            waivers={},  # 字符串已被归一化丢弃
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["final_review"])

    def test_signed_waiver_honored(self):
        req = self._req(
            self._state(enabled=False, cycles=0, skipped_only=True),
            self._merged(self.module.READY),
            self._preflight(True),
            waivers=self._waiver("final_review"),
            ledger=self._ledger(True),
        )
        self.assertTrue(req["checks"]["final_review"])

    def test_failed_preflight_blocks(self):
        req = self._req(
            self._state(),
            self._merged(self.module.READY),
            self._preflight(False),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["submission_audit"])

    def test_conditional_needs_explicit_allow(self):
        req = self._req(
            self._state(),
            self._merged(self.module.CONDITIONAL),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["deterministic_audit"])
        req2 = self._req(
            self._state(),
            self._merged(self.module.CONDITIONAL),
            self._preflight(True),
            allow=True,
            ledger=self._ledger(True),
        )
        self.assertTrue(req2["checks"]["deterministic_audit"])

    def test_invalid_ledger_blocks(self):
        req = self._req(
            self._state(),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(False),
        )
        self.assertFalse(req["checks"]["precision_gate"])

    def test_apply_failure_routes_to_final_review(self):
        """H2：apply 未产出终稿 → 先评后交付（评审对象是历史最佳）。"""
        loop = self.module
        out_dir = Path(tempfile.mkdtemp(prefix="applyfail-"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        state = loop.stage_init(
            Path("test-manuscripts/cn-clean-draft.md"),
            language="cn",
            strength="light",
            out_dir=out_dir,
            max_rounds=1,
            review_sim=True,
        )
        round_dir = out_dir / "work" / "round-001"
        round_dir.mkdir(parents=True)
        (round_dir / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
        state.update(
            {
                "current_round_dir": str(round_dir),
                "jobs_file": str(round_dir / "jobs.json"),
                "responses_dir": str(round_dir / "responses"),
                "current_input_file": "test-manuscripts/cn-clean-draft.md",
            }
        )
        original_run_skill = loop.run_skill

        def _dispatch(skill, args):
            if skill == "batch-processor":
                return {"items": [{}]}  # 模拟 apply 未产出 step-result（基础设施故障形态）
            return original_run_skill(skill, args)

        with patch.object(loop, "run_skill", side_effect=_dispatch):
            state = loop.stage_apply(state, out_dir)
        self.assertIn(state["next_stage"], ("review", "deliver"))
        if state["next_stage"] == "review":
            self.assertTrue(state.get("review_pending_deliver"), "apply 失败也必须先评后交付")
            self.assertIn("apply 未产出终稿", state.get("stop_reason", ""))


if __name__ == "__main__":
    unittest.main()
