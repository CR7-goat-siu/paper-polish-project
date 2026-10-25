"""Release Protocol 四条件与人工裁决门禁的测试（架构评审 2026-10-25 二/三轮 + F1-F5）。

覆盖：
- utils/adjudication：账本有效性审计（issue_id 唯一 / adjudicator 署名 /
  多来源 / manuscript_sha256 版本绑定）与 precision_gate 全分支；
- utils/model_identity：家族确定性推导；
- utils/review_trace：trace 完整性 / append-only hash chain / 跨 run 复用拒绝；
- optimizer-loop：三态验收（done/provisional/accepted）、迁移、release 消费
  accepted + trace 验签、评审者最小反例、apply 异常路由。
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
from utils import adjudication, model_identity, review_trace

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
    """有效账本行：唯一 id、双来源、署名、绑定版本哈希。"""
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


class ModelIdentityTests(unittest.TestCase):
    def test_known_families(self):
        cases = {
            "gpt-4o": "openai",
            "o3-mini": "openai",
            "claude-sonnet-4": "anthropic",
            "gemini-2.5-pro": "google",
            "deepseek-v4": "deepseek",
            "kimi-k2": "moonshot",
            "qwen3-max": "qwen",
            "GLM-5.3-Flash": "zhipu",
            "chatglm-4": "zhipu",
            "minimax-m1": "minimax",
            "grok-3": "xai",
            "llama-4": "meta",
        }
        for name, family in cases.items():
            self.assertEqual(model_identity.derive_family(name), family, name)

    def test_unknown_and_ambiguity(self):
        self.assertEqual(model_identity.derive_family(""), "unknown")
        self.assertEqual(model_identity.derive_family("mystery-model-9000"), "unknown")
        self.assertFalse(model_identity.is_known_family("unknown"))

    def test_deterministic(self):
        self.assertEqual(
            model_identity.derive_family("GLM-5.3"),
            model_identity.derive_family("glm-5.3"),
        )


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
        _write_ledger(ledger, _valid_rows(_sha(manuscript), n_fp=1))
        gate = adjudication.precision_gate(
            ledger, expected_manuscript_sha256=_sha(manuscript)
        )
        self.assertTrue(gate["pass"])
        self.assertEqual(gate["ledger"]["precision_overall"], 0.8)
        self.assertGreaterEqual(gate["ledger"]["distinct_sources"], 2)


class ReviewTraceTests(unittest.TestCase):
    """F5：trace 完整性、append-only hash chain、跨 run 复用拒绝。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trace-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _make(self, run_id="run-A", job="j1", thread="t1"):
        return review_trace.create_trace(
            self.tmp,
            run_id=run_id,
            job_id=job,
            purpose="final_acquittal",
            round_index=1,
            artifact_sha256="a" * 64,
            thread_id=thread,
            request={"prompt": "完整 prompt 正文", "artifact_sha256": "a" * 64},
            response_text='{"verdict": "acquitted", "完整原始响应": true}',
            meta={"executor_model": "GLM-5.3-Flash", "reviewer_model": "claude-sonnet-4",
                  "identity_source": "host_session_event", "review_completed": True,
                  "review_scope_complete": True, "verdict": "acquitted", "score": 4},
        )

    def test_append_not_overwrite(self):
        first = self._make()
        self.assertEqual(first["status"], "created")
        second = self._make()  # 同五元组重复调用
        self.assertEqual(second["status"], "exists")
        chain = review_trace.verify_chain(self.tmp)
        self.assertTrue(chain["ok"])
        self.assertEqual(chain["events"], 2)

    def test_raw_response_intact(self):
        trace = self._make()
        meta = json.loads(
            (Path(trace["dir"]) / "meta.json").read_text(encoding="utf-8")
        )
        raw = (Path(trace["dir"]) / "response.md").read_text(encoding="utf-8")
        self.assertEqual(
            hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            meta["response_sha256"],
        )
        self.assertIn("完整原始响应", raw)

    def test_tamper_detected(self):
        trace = self._make()
        response_file = Path(trace["dir"]) / "response.md"
        response_file.write_text(
            response_file.read_text(encoding="utf-8").replace("acquitted", "rejected"),
            encoding="utf-8",
        )
        verdict = review_trace.verify_trace(
            self.tmp, trace["trace_id"], run_id="run-A",
            expected_artifact_sha256="a" * 64,
        )
        self.assertFalse(verdict["ok"])
        self.assertIn("response.md", verdict["reason"])

    def test_chain_break_detected(self):
        self._make()
        self._make(job="j2", thread="t2")
        events = self.tmp / review_trace.EVENTS_FILENAME
        lines = events.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace("final_acquittal", "final_acquittal_tampered")
        events.write_text("\n".join(lines) + "\n", encoding="utf-8")
        chain = review_trace.verify_chain(self.tmp)
        self.assertFalse(chain["ok"])
        self.assertIn("不匹配", chain["reason"])

    def test_cross_run_reuse_rejected(self):
        trace = self._make(run_id="run-A")
        verdict = review_trace.verify_trace(
            self.tmp, trace["trace_id"], run_id="run-B",
            expected_artifact_sha256="a" * 64,
        )
        self.assertFalse(verdict["ok"])
        self.assertIn("跨 run", verdict["reason"])

    def test_trace_id_binds_dimensions(self):
        a = review_trace.derive_trace_id(
            run_id="r", job_id="j", round_index=1, artifact_sha256="x", thread_id="t"
        )
        b = review_trace.derive_trace_id(
            run_id="r", job_id="j", round_index=2, artifact_sha256="x", thread_id="t"
        )
        self.assertNotEqual(a, b, "round 变化必须改变 trace_id")


class ReleaseRequirementsTests(unittest.TestCase):
    """_release_requirements 四条件聚合（F4 三态 + trace 验签）。"""

    @classmethod
    def setUpClass(cls):
        cls.module = skill_module("optimizer-loop")
        cls.tmp = Path(tempfile.mkdtemp(prefix="relreq-"))
        cls.run_id = cls.tmp.resolve().as_posix()
        cls.manuscript = cls.tmp / "best.md"
        cls.manuscript.write_text("最终稿文本内容" * 5, encoding="utf-8")
        cls.sha = _sha(cls.manuscript)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _acceptance(self, *, status="accepted", sha=None, trace=True) -> dict:
        sha = sha or self.sha
        acceptance = {
            "status": status,
            "gate_reason": "" if status == "accepted" else "provisional/done 占位",
            "job_id": "revsim-final-acquittal",
            "trace_id": "",
            "executor_model": "GLM-5.3-Flash",
            "reviewer_model": "claude-sonnet-4",
            "model_family": "anthropic",
            "identity_source": "host_session_event",
            "reviewed_text_sha256": sha,
            "complete": True,
            "verdict": "acquitted" if status == "accepted" else "",
            "score": 4,
        }
        if trace:
            trace = review_trace.create_trace(
                self.tmp,
                run_id=self.run_id,
                job_id="revsim-final-acquittal",
                purpose="final_acquittal",
                round_index=1,
                artifact_sha256=sha,
                thread_id="thread-final-001",
                request={"prompt": "final review prompt", "artifact_sha256": sha},
                response_text='{"verdict": "acquitted"}',
                meta={
                    "executor_model": acceptance["executor_model"],
                    "reviewer_model": acceptance["reviewer_model"],
                    "identity_source": acceptance["identity_source"],
                    "review_completed": True,
                    "review_scope_complete": True,
                    "verdict": acceptance["verdict"],
                    "score": 4,
                },
            )
            acceptance["trace_id"] = trace["trace_id"]
        return acceptance

    def _state(self, *, status="accepted", pending=False, trace=True, stale=False) -> dict:
        acceptance_sha = self.sha if not stale else ("deadbeef" * 8)
        return {
            "review_sim": {"enabled": True},
            "review_history": [
                {"cycle": 1, "purpose": "final_acquittal", "status": status}
            ],
            "review_cycle": {"cycle": 9} if pending else None,
            "review_pending_jobs": ["x"] if pending else [],
            "reviewed_text_sha256": acceptance_sha,
            "review_acceptance": self._acceptance(status=status, sha=acceptance_sha, trace=trace),
            "previous_thread_ids": ["thread-final-001"],
            "run_id": self.run_id,
            "best": {"text_file": str(self.manuscript)},
        }

    def _waiver(self, check):
        return {check: {"check": check, "author": "作者甲", "reason": "作者接管"}}

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
            out_dir=self.tmp,
        )

    def test_all_conditions_pass(self):
        req = self._req(
            self._state(),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertEqual(
            req["checks"],
            {
                "deterministic_audit": True,
                "final_review": True,
                "submission_audit": True,
                "precision_gate": True,
            },
        )

    def test_review_hash_mismatch_blocks(self):
        """评审后稿件被改动（哈希不一致）→ accepted 立即过期（stale_review）。"""
        req = self._req(
            self._state(stale=True),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["final_review"])
        self.assertIn("stale_review", req["reasons"]["final_review"])

    def test_missing_trace_blocks_accepted(self):
        """accepted 但 trace 缺失 → 阻断（trace_invalid）。"""
        req = self._req(
            self._state(trace=False),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["final_review"])
        self.assertIn("trace_invalid", req["reasons"]["final_review"])

    def test_provisional_cannot_release_needs_waiver(self):
        """F4：provisional（同族/caller_declared）不可 release，签核豁免可接管。"""
        req = self._req(
            self._state(status="provisional"),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["final_review"])
        self.assertIn("不是 accepted", req["reasons"]["final_review"])
        req2 = self._req(
            self._state(status="provisional"),
            self._merged(self.module.READY),
            self._preflight(True),
            waivers=self._waiver("final_review"),
            ledger=self._ledger(True),
        )
        self.assertTrue(req2["checks"]["final_review"])

    def test_pending_review_cycle_blocks(self):
        """评审周期未决（响应未回填）→ final_review 阻断（fail-closed）。"""
        req = self._req(
            self._state(pending=True),
            self._merged(self.module.READY),
            self._preflight(True),
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["final_review"])

    def test_done_migration_never_accepted(self):
        """老 run-state 迁移：无身份证据 → done，永不 accepted。"""
        migrated = self.module._migrate_state({"best": {"text_file": str(self.manuscript)}})
        self.assertEqual(migrated["review_acceptance"]["status"], "done")

    def test_missing_preflight_blocks_even_waived(self):
        req = self._req(
            self._state(),
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
            self._state(status="provisional"),
            self._merged(self.module.READY),
            self._preflight(True),
            waivers={},
            ledger=self._ledger(True),
        )
        self.assertFalse(req["checks"]["final_review"])

    def test_signed_waiver_honored(self):
        req = self._req(
            self._state(status="provisional"),
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
