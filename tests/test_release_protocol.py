"""Release Protocol 四条件与人工裁决门禁的单元测试（架构评审 2026-10-25 二轮）。

- utils/adjudication：账本加载（TP/FP/UNCLEAR 三值）与 precision_gate 四分支；
- optimizer-loop._release_requirements：四条件聚合、waive 语义、
  submission-audit 不可豁免。
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils import adjudication

HEADER = ["issue_id", "source", "check", "priority", "evidence_snippet", "adjudication", "note", "adjudicator", "date"]


def _write_ledger(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for issue_id, verdict in rows:
            writer.writerow([issue_id, "claim-guard", "hype", "P1", "证据片段", verdict, "", "作者", "2026-10-25"])


class AdjudicationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ledger-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_missing_ledger_fails_gate(self):
        gate = adjudication.precision_gate(self.tmp / "absent.csv")
        self.assertFalse(gate["pass"])
        self.assertIn("不存在", gate["reason"])

    def test_insufficient_adjudicated_fails(self):
        ledger = self.tmp / "l.csv"
        _write_ledger(ledger, [("i1", "TP"), ("i2", "FP"), ("i3", "UNCLEAR")])
        gate = adjudication.precision_gate(ledger)
        self.assertFalse(gate["pass"])
        self.assertIn("不足", gate["reason"])
        self.assertEqual(gate["ledger"]["unclear"], 1)
        self.assertEqual(gate["ledger"]["unresolved"], 0)

    def test_below_precision_fails(self):
        ledger = self.tmp / "l.csv"
        _write_ledger(ledger, [("i1", "TP"), ("i2", "TP"), ("i3", "TP"), ("i4", "FP"), ("i5", "FP")])
        gate = adjudication.precision_gate(ledger)
        self.assertFalse(gate["pass"])
        self.assertIn("低于", gate["reason"])
        self.assertEqual(gate["ledger"]["precision_overall"], 0.6)

    def test_passing_gate(self):
        ledger = self.tmp / "l.csv"
        _write_ledger(
            ledger,
            [("i1", "TP"), ("i2", "TP"), ("i3", "TP"), ("i4", "TP"), ("i5", "FP"), ("i6", "UNCLEAR")],
        )
        gate = adjudication.precision_gate(ledger)
        self.assertTrue(gate["pass"])
        self.assertEqual(gate["ledger"]["precision_overall"], 0.8)
        # UNCLEAR 不入分母：5 裁决条中 4 TP 1 FP = 0.8
        self.assertEqual(gate["ledger"]["adjudicated"], 5)

    def test_unresolved_rows_not_counted(self):
        ledger = self.tmp / "l.csv"
        _write_ledger(ledger, [("i1", ""), ("i2", "TP")] * 3)
        state = adjudication.load_ledger(ledger)
        self.assertEqual(state["unresolved"], 3)
        self.assertEqual(state["adjudicated"], 3)


class ReleaseRequirementsTests(unittest.TestCase):
    """_release_requirements 四条件聚合（构造数据直测，不跑端到端）。"""

    @classmethod
    def setUpClass(cls):
        cls.module = skill_module("optimizer-loop")
        cls.tmp = Path(tempfile.mkdtemp(prefix="relreq-"))

    @classmethod
    def tearDownClass(cls):
        __import__("shutil").rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def _state(*, enabled=True, cycles=1, pending=False) -> dict:
        return {
            "review_sim": {"enabled": enabled},
            "review_history": [{}] * cycles,
            "review_cycle": {"cycle": 9} if pending else None,
            "review_pending_jobs": ["x"] if pending else [],
        }

    @staticmethod
    def _merged(status: str) -> dict:
        return {"delivery_status": status, "reason": "r"}

    def _preflight(self, passed: bool) -> dict:
        return {"status": "ok", "automated_pass": passed}

    def _ledger(self, passing: bool) -> Path:
        path = self.tmp / ("ok.csv" if passing else "bad.csv")
        rows = (
            [("i1", "TP"), ("i2", "TP"), ("i3", "TP"), ("i4", "TP"), ("i5", "FP")]
            if passing
            else [("i1", "TP"), ("i2", "FP"), ("i3", "FP"), ("i4", "FP"), ("i5", "FP")]
        )
        _write_ledger(path, rows)
        return path

    def _req(self, state, merged, preflight, *, allow=False, waivers=(), ledger=None):
        return self.module._release_requirements(
            state,
            merged,
            preflight,
            allow_conditional_release=allow,
            waivers=set(waivers),
            ledger_path=ledger,
        )

    def test_all_conditions_pass_releases(self):
        req = self._req(
            self._state(),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertTrue(req["all_pass"])
        self.assertTrue(all(req["checks"].values()))

    def test_missing_preflight_blocks_even_waived(self):
        req = self._req(
            self._state(enabled=False, cycles=0),
            self._merged(self.module.READY),
            None,
            waivers=("final_review", "precision_gate"),
        )
        self.assertFalse(req["all_pass"])
        self.assertFalse(req["checks"]["submission_audit"])
        self.assertIn("不可豁免", req["reasons"]["submission_audit"])

    def test_failed_preflight_blocks(self):
        req = self._req(
            self._state(),
            self._merged(self.module.READY),
            self._preflight(False),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["submission_audit"])

    def test_review_not_enabled_needs_waiver(self):
        req = self._req(
            self._state(enabled=False, cycles=0),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["final_review"])
        req2 = self._req(
            self._state(enabled=False, cycles=0),
            self._merged(self.module.READY),
            self._preflight(True),
            waivers=("final_review",),
            ledger=self._ledger(True),
        )
        self.assertTrue(req2["checks"]["final_review"])

    def test_pending_review_cycle_blocks(self):
        req = self._req(
            self._state(cycles=1, pending=True),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["final_review"])

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

    def test_no_ledger_blocks_unless_waived(self):
        req = self._req(
            self._state(),
            self._merged(self.module.READY),
            self._preflight(True),
        )
        self.assertFalse(req["checks"]["precision_gate"])
        self.assertIn("未提供裁决账本", req["reasons"]["precision_gate"])
        req2 = self._req(
            self._state(),
            self._merged(self.module.READY),
            self._preflight(True),
            waivers=("precision_gate",),
        )
        self.assertTrue(req2["checks"]["precision_gate"])


if __name__ == "__main__":
    unittest.main()
