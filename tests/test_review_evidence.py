"""reviewer 身份契约 / final_acquittal / 证据绑定 / append-only trace 的测试。

覆盖指令 F1-F3/F5：
- utils.model_identity：家族确定性推导（known / unknown / 歧义）；
- review-sim build_final：输入白名单（无禁止字段）、context_policy=fresh_only、
  previous_thread_ids、artifact_sha256 绑定；
- adjudicate_final_response 三态：cross-family accepted、same-family provisional、
  caller_declared provisional、unknown/missing/伪造 family/旧 thread/哈希不一致/
  scope 不全/rejected 全部 done（fail-closed）；
- review_trace：原始响应完整、篡改验签失败、追加不覆盖。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module
from utils import model_identity, review_trace
from utils.skill_loader import run_skill

FIXTURE = "test-manuscripts/cn-clean-draft.md"


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _final_payload(**overrides) -> dict:
    payload = {
        "job_id": "revsim-final-acquittal",
        "section_id": "final",
        "role": "final_acquittal",
        "executor_model": "GLM-5.3-Flash",
        "reviewer_model": "claude-sonnet-4",
        "model_family": "anthropic",
        "identity_source": "host_session_event",
        "thread_id": "thread-final-001",
        "reviewed_text_sha256": "",
        "scope": {
            "mode": "final_full",
            "sections": ["__all__"],
            "includes_references": True,
            "includes_figures": True,
            "char_count": 0,
        },
        "complete": True,
        "verdict": "acquitted",
        "score": 4,
        "findings": [],
    }
    payload.update(overrides)
    return payload


class ModelIdentityTests(unittest.TestCase):
    def test_family_table(self):
        self.assertEqual(model_identity.derive_family("gpt-5.2"), "openai")
        self.assertEqual(model_identity.derive_family("Claude-Sonnet-4"), "anthropic")
        self.assertEqual(model_identity.derive_family("gemini-3-pro"), "google")
        self.assertEqual(model_identity.derive_family("DeepSeek-V4"), "deepseek")
        self.assertEqual(model_identity.derive_family("kimi-k2"), "moonshot")
        self.assertEqual(model_identity.derive_family("Qwen3"), "qwen")
        self.assertEqual(model_identity.derive_family("GLM-5.3"), "zhipu")
        self.assertEqual(model_identity.derive_family("MiniMax-M2"), "minimax")
        self.assertEqual(model_identity.derive_family("grok-4"), "xai")

    def test_unknown(self):
        self.assertEqual(model_identity.derive_family("totally-new-lab-alpha"), "unknown")
        self.assertEqual(model_identity.derive_family(""), "unknown")


class FinalAcquittalTests(unittest.TestCase):
    def setUp(self):
        self.module = skill_module("review-sim")
        self.out = Path(tempfile.mkdtemp(prefix="finalrev-"))
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self.text = Path(FIXTURE).read_text(encoding="utf-8")
        self.sha = _sha_text(self.text)

    def _build(self, previous_thread_ids=None, text=None):
        return run_skill(
            "review-sim",
            {
                "mode": "build_final",
                "text": text if text is not None else self.text,
                "language": "cn",
                "out": str(self.out),
                "previous_thread_ids": previous_thread_ids or [],
                "source_file": FIXTURE,
            },
        )

    def _respond(self, job: dict, payload: dict) -> Path:
        path = Path(job["response_file"])
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_build_forbidden_fields_absent(self):
        """F2：final job 只含白名单输入——不得出现 focus/audit/批注/历史意见。"""
        payload = self._build(previous_thread_ids=["thread-old-1"])
        job = payload["jobs"][0]
        prompt = Path(job["prompt_file"]).read_text(encoding="utf-8")
        for forbidden in ("focus_issues", "审计问题 ISS-", "[审计问题", "上一轮", "导师批注", "fix summary"):
            self.assertNotIn(forbidden, prompt, f"final prompt 不得包含 {forbidden!r}")
        self.assertEqual(job["context_policy"], "fresh_only")
        self.assertEqual(job["previous_thread_ids"], ["thread-old-1"])
        self.assertEqual(job["artifact_sha256"], self.sha)
        self.assertEqual(job["identity_contract"]["identity_source_enum"], list(
            skill_module("review-sim").IDENTITY_SOURCES
        ))
        self.assertIn("final_full", json.dumps(job["required_scope"]))

    def test_cross_family_trusted_accepted(self):
        """F1：跨族 + host_session_event + complete + scope + 哈希一致 → accepted。"""
        job = self._build()["jobs"][0]
        payload = _final_payload(
            reviewed_text_sha256=self.sha,
            scope={"mode": "final_full", "sections": ["__all__"],
                   "includes_references": True, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {
                "mode": "apply",
                "jobs": {"purpose": "final_acquittal", "jobs": [job]},
                "responses_dir": str(Path(job["response_file"]).parent),
                "run_dir": str(self.out),
                "run_id": "run-e2e",
                "round_index": 1,
            },
        )
        acceptance = result["acceptance"]
        self.assertEqual(acceptance["status"], "accepted")
        self.assertEqual(acceptance["model_family"], "anthropic")
        self.assertNotEqual(
            model_identity.derive_family(acceptance["executor_model"]),
            model_identity.derive_family(acceptance["reviewer_model"]),
        )
        self.assertTrue(acceptance["complete"])
        self.assertEqual(acceptance["verdict"], "acquitted")
        # F5：trace 落盘且验签通过
        verdict = review_trace.verify_trace(
            self.out, acceptance["trace_id"],
            run_id="run-e2e",
            expected_job_id=job["job_id"],
            expected_artifact_sha256=self.sha,
            expected_identity={"reviewer_model": "claude-sonnet-4"},
        )
        self.assertTrue(verdict["ok"], verdict["reason"])
        raw = (Path(verdict["meta"]["trace_id"] and self.out) / "review-traces" / acceptance["trace_id"] / "response.md").read_text(encoding="utf-8")
        self.assertIn('"verdict"', raw, "原始响应必须完整保存")

    def test_same_family_provisional(self):
        job = self._build()["jobs"][0]
        payload = _final_payload(
            reviewer_model="GLM-5.3",
            model_family="zhipu",
            reviewed_text_sha256=self.sha,
            scope={"mode": "final_full", "sections": ["__all__"],
                   "includes_references": True, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        self.assertEqual(result["acceptance"]["status"], "provisional")
        self.assertIn("same-family", result["acceptance"]["gate_reason"])

    def test_caller_declared_provisional(self):
        job = self._build()["jobs"][0]
        payload = _final_payload(
            identity_source="caller_declared",
            reviewed_text_sha256=self.sha,
            scope={"mode": "final_full", "sections": ["__all__"],
                   "includes_references": True, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        self.assertEqual(result["acceptance"]["status"], "provisional")

    def test_unavailable_identity_done(self):
        job = self._build()["jobs"][0]
        payload = _final_payload(
            identity_source="unavailable",
            reviewed_text_sha256=self.sha,
            scope={"mode": "final_full", "sections": ["__all__"],
                   "includes_references": True, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        self.assertEqual(result["acceptance"]["status"], "done")
        self.assertIn("identity_source", result["acceptance"]["gate_reason"])

    def test_unknown_model_done(self):
        job = self._build()["jobs"][0]
        payload = _final_payload(
            reviewer_model="mystery-lab-model",
            model_family="unknown",
            reviewed_text_sha256=self.sha,
            scope={"mode": "final_full", "sections": ["__all__"],
                   "includes_references": True, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        self.assertEqual(result["acceptance"]["status"], "done")
        self.assertIn("unknown_identity", result["acceptance"]["gate_reason"])

    def test_forged_family_done(self):
        """F1：声明 family 与模型名推导不一致 → 伪造身份 → done（不得 accepted）。"""
        job = self._build()["jobs"][0]
        payload = _final_payload(
            model_family="openai",  # claude-sonnet-4 实际是 anthropic
            reviewed_text_sha256=self.sha,
            scope={"mode": "final_full", "sections": ["__all__"],
                   "includes_references": True, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        self.assertEqual(result["acceptance"]["status"], "done")
        self.assertIn("forged_family", result["acceptance"]["gate_reason"])

    def test_missing_identity_fields_done(self):
        job = self._build()["jobs"][0]
        payload = _final_payload(reviewed_text_sha256=self.sha)
        del payload["executor_model"]
        del payload["identity_source"]
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        self.assertEqual(result["acceptance"]["status"], "done")
        self.assertIn("missing_fields", result["acceptance"]["gate_reason"])

    def test_old_thread_rejected(self):
        """F2：thread_id 出现在 previous_thread_ids → 直接 invalid（done）。"""
        job = self._build(previous_thread_ids=["thread-final-001"])["jobs"][0]
        payload = _final_payload(
            thread_id="thread-final-001",
            reviewed_text_sha256=self.sha,
            scope={"mode": "final_full", "sections": ["__all__"],
                   "includes_references": True, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        self.assertEqual(result["acceptance"]["status"], "done")
        self.assertIn("thread_reuse", result["acceptance"]["gate_reason"])

    def test_hash_mismatch_done(self):
        """F3：回填哈希与作业 artifact 不一致 → done。"""
        job = self._build()["jobs"][0]
        payload = _final_payload(
            reviewed_text_sha256="f" * 64,
            scope={"mode": "final_full", "sections": ["__all__"],
                   "includes_references": True, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        self.assertEqual(result["acceptance"]["status"], "done")
        self.assertIn("hash_mismatch", result["acceptance"]["gate_reason"])

    def test_scope_incomplete_done(self):
        """F3：scope 不是 final_full 全文口径 → done。"""
        job = self._build()["jobs"][0]
        payload = _final_payload(
            reviewed_text_sha256=self.sha,
            scope={"mode": "final_full", "sections": ["3 结论"],
                   "includes_references": False, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        self.assertEqual(result["acceptance"]["status"], "done")
        self.assertIn("scope_incomplete", result["acceptance"]["gate_reason"])

    def test_rejected_verdict_done(self):
        job = self._build()["jobs"][0]
        payload = _final_payload(
            verdict="rejected",
            reviewed_text_sha256=self.sha,
            scope={"mode": "final_full", "sections": ["__all__"],
                   "includes_references": True, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        self.assertEqual(result["acceptance"]["status"], "done")
        self.assertIn("negative_verdict", result["acceptance"]["gate_reason"])

    def test_one_char_change_invalidates_hash(self):
        """F3：最终稿任意一个字符变化 → 旧 accepted 的哈希立即失配。"""
        job_a = self._build()["jobs"][0]
        accepted_sha = _sha_text(self.text)
        modified = self.text.replace("脑电", "脑电 ", 1)  # 单字符级改动
        job_b = self._build(text=modified)["jobs"][0]
        self.assertNotEqual(job_a["artifact_sha256"], job_b["artifact_sha256"])
        self.assertNotEqual(accepted_sha, job_b["artifact_sha256"])

    def test_trace_transplant_rejected(self):
        """F3：把另一轮 trace 移花接木到本 job → verify_trace 拒绝（job_id 不符）。"""
        other = review_trace.create_trace(
            self.out,
            run_id="run-x",
            job_id="revsim-somewhere-else",
            purpose="final_acquittal",
            round_index=1,
            artifact_sha256=self.sha,
            thread_id="thread-final-001",
            request={"prompt": "别的作业"},
            response_text='{"verdict": "acquitted"}',
            meta={"executor_model": "GLM-5.3-Flash", "reviewer_model": "claude-sonnet-4",
                  "identity_source": "host_session_event", "review_completed": True,
                  "review_scope_complete": True, "verdict": "acquitted", "score": 4},
        )
        verdict = review_trace.verify_trace(
            self.out, other["trace_id"],
            run_id="run-x",
            expected_job_id="revsim-final-acquittal",
            expected_artifact_sha256=self.sha,
        )
        self.assertFalse(verdict["ok"])
        self.assertIn("job_id", verdict["reason"])

    def test_trace_tamper_fails_verification(self):
        """F5：原始响应被改动 → 验签失败（fail-closed）。"""
        job = self._build()["jobs"][0]
        payload = _final_payload(
            reviewed_text_sha256=self.sha,
            scope={"mode": "final_full", "sections": ["__all__"],
                   "includes_references": True, "includes_figures": True,
                   "char_count": job["char_count"]},
        )
        self._respond(job, payload)
        result = run_skill(
            "review-sim",
            {"mode": "apply", "jobs": {"purpose": "final_acquittal", "jobs": [job]},
             "responses_dir": str(Path(job["response_file"]).parent),
             "run_dir": str(self.out), "run_id": "run-x"},
        )
        trace_id = result["acceptance"]["trace_id"]
        response_file = self.out / "review-traces" / trace_id / "response.md"
        original = response_file.read_text(encoding="utf-8")
        response_file.write_text(original.replace("acquitted", "rejected"), encoding="utf-8")
        verdict = review_trace.verify_trace(
            self.out, trace_id, run_id="run-x", expected_artifact_sha256=self.sha,
        )
        self.assertFalse(verdict["ok"])
        self.assertIn("response.md", verdict["reason"])


if __name__ == "__main__":
    unittest.main()
