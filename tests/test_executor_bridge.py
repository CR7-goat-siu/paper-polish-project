"""T3 免密钥执行桥测试：CLI / 回环 HTTP 通道、fail-closed、审计日志、闭环接入。

红线断言：执行桥绝不读取凭据、命令只能来自配置（argv 列表无 shell）、
HTTP 通道拒绝非回环地址、失败作业保持 pending。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from conftest import ROOT, skill_module
from utils import executor_bridge, web_fetch

ECHO_COMMAND = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
FAIL_COMMAND = [sys.executable, "-c", "import sys; sys.exit(2)"]
SLOW_COMMAND = [sys.executable, "-c", "import time; time.sleep(3)"]


def _bridge_config(channel: dict, *, route: str = "review-sim") -> dict:
    return {
        "enabled": True,
        "defaults": {"timeout_seconds": 30},
        "channels": {"under-test": channel},
        "routing": {route: "under-test"},
    }


def _review_jobs(tmp: Path) -> tuple[dict, Path]:
    """构造一个最小评审作业包（prompt 落盘，响应目录为空）。"""
    jobs_dir = tmp / "cycle"
    prompts = jobs_dir / "prompts"
    responses = jobs_dir / "responses"
    prompts.mkdir(parents=True)
    responses.mkdir(parents=True)
    (prompts / "rev-job-1.prompt.txt").write_text("评审这份摘要", encoding="utf-8")
    (prompts / "rev-job-2.prompt.txt").write_text("评审这段方法", encoding="utf-8")
    payload = {
        "jobs": [
            {"job_id": "rev-job-1", "role": "methodology", "section_id": "a", "prompt_file": str(prompts / "rev-job-1.prompt.txt")},
            {"job_id": "rev-job-2", "role": "statistics", "section_id": "b", "prompt_file": str(prompts / "rev-job-2.prompt.txt")},
        ]
    }
    return payload, responses


class ExecutorBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bridge-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(web_fetch.set_post_transport, None)

    def test_cli_echo_channel_fills_and_logs(self):
        payload, responses = _review_jobs(self.tmp)
        log = self.tmp / "bridge-log.jsonl"
        report = executor_bridge.backfill_jobs(
            payload["jobs"],
            responses,
            kind="review-sim",
            config=_bridge_config({"type": "cli", "command": ECHO_COMMAND, "timeout_seconds": 30}),
            log_file=log,
        )
        self.assertEqual(report["filled"], 2)
        self.assertEqual(report["failed"], [])
        self.assertTrue((responses / "rev-job-1.txt").read_text(encoding="utf-8"))
        entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["status"] == "filled" for e in entries))
        self.assertTrue(all(e["channel"] == "under-test" for e in entries))

    def test_cli_failure_keeps_pending(self):
        payload, responses = _review_jobs(self.tmp)
        report = executor_bridge.backfill_jobs(
            payload["jobs"],
            responses,
            kind="review-sim",
            config=_bridge_config({"type": "cli", "command": FAIL_COMMAND, "timeout_seconds": 30}),
        )
        self.assertEqual(report["filled"], 0)
        self.assertEqual(len(report["failed"]), 2)
        self.assertFalse((responses / "rev-job-1.txt").exists(), "失败作业必须保持 pending")

    def test_cli_timeout_is_failure(self):
        payload, responses = _review_jobs(self.tmp)
        report = executor_bridge.backfill_jobs(
            payload["jobs"][:1],
            responses,
            kind="review-sim",
            config=_bridge_config({"type": "cli", "command": SLOW_COMMAND, "timeout_seconds": 1}),
        )
        self.assertEqual(report["filled"], 0)
        self.assertEqual(len(report["failed"]), 1)

    def test_string_command_rejected(self):
        payload, responses = _review_jobs(self.tmp)
        with self.assertRaises(executor_bridge.BridgeError):
            executor_bridge.backfill_jobs(
                payload["jobs"],
                responses,
                kind="review-sim",
                config=_bridge_config({"type": "cli", "command": "echo hack"}),
            )

    def test_missing_routing_is_error(self):
        payload, responses = _review_jobs(self.tmp)
        with self.assertRaises(executor_bridge.BridgeError):
            executor_bridge.backfill_jobs(
                payload["jobs"],
                responses,
                kind="cite-intent",
                config=_bridge_config({"type": "cli", "command": ECHO_COMMAND}),
            )

    def test_http_ollama_channel_via_transport_injection(self):
        captured: dict = {}

        def fake_post(url, payload, headers, timeout):
            captured["url"] = url
            captured["payload"] = payload
            return json.dumps({"message": {"content": "模型响应正文"}})

        web_fetch.set_post_transport(fake_post)
        payload, responses = _review_jobs(self.tmp)
        report = executor_bridge.backfill_jobs(
            payload["jobs"][:1],
            responses,
            kind="review-sim",
            config=_bridge_config(
                {
                    "type": "http",
                    "api": "ollama-chat",
                    "endpoint": "http://127.0.0.1:11434/api/chat",
                    "model": "test-model",
                }
            ),
        )
        self.assertEqual(report["filled"], 1)
        self.assertEqual(captured["payload"]["model"], "test-model")
        self.assertEqual(captured["payload"]["messages"][0]["content"], "评审这份摘要")
        self.assertEqual(
            (responses / "rev-job-1.txt").read_text(encoding="utf-8"), "模型响应正文"
        )

    def test_loopback_only_enforcement(self):
        with self.assertRaises(AssertionError):
            web_fetch.post_text_loopback("http://example.com/api/chat", {"prompt": "x"})
        with self.assertRaises(AssertionError):
            web_fetch.post_text_loopback("http://10.0.0.1:11434/api/chat", {"prompt": "x"})

    def test_self_test_reports_channels(self):
        result = executor_bridge.self_test(
            _bridge_config({"type": "cli", "command": ECHO_COMMAND, "timeout_seconds": 30})
        )
        self.assertTrue(result["channels"][0]["ok"])


class BridgeLoopIntegrationTests(unittest.TestCase):
    """stage=backfill 与整体模式桥接。"""

    def setUp(self):
        self.loop = skill_module("optimizer-loop")
        self.out_dir = Path(tempfile.mkdtemp(prefix="bridgeloop-"))
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)

    def _stage(self, stage: str, **extra):
        payload = {"stage": stage, "out": str(self.out_dir), "language": "cn", "identity": True}
        payload.update(extra)
        return self.loop.run(payload)

    def _echo_bridge_config(self) -> dict:
        return _bridge_config({"type": "cli", "command": ECHO_COMMAND, "timeout_seconds": 30})

    def test_stage_backfill_fills_review_jobs(self):
        self._stage("init", text_file=str(ROOT / "test-manuscripts" / "cn-eeg-denoising-draft.md"), max_rounds=3, review_sim=True)
        self._stage("build")
        self._stage("apply")
        self._stage("decide")
        report = self._stage("backfill", bridge_config=self._echo_bridge_config())
        self.assertEqual(report["stage"], "backfill")
        self.assertGreater(report["filled"], 0)
        self.assertTrue((self.out_dir / "bridge-log.jsonl").exists())
        # 回填后评审可继续消费（echo 响应非 JSON → 全部拒收 → 无新增意见）
        view = self._stage("review")
        self.assertIn(view["next_stage"], ("build", "deliver"))

    def test_whole_mode_with_bridge_terminates(self):
        result = self.loop.run(
            {
                "text_file": str(ROOT / "test-manuscripts" / "cn-eeg-denoising-draft.md"),
                "language": "cn",
                "identity": True,
                "bridge": True,
                "review_sim": True,
                "max_rounds": 1,
                "out": str(self.out_dir),
            }
        )
        self.assertIn(
            result["status"],
            ("READY_FOR_AUTHOR_REVIEW", "CONDITIONALLY_READY", "BLOCKED"),
        )
        log_file = self.out_dir / "bridge-log.jsonl"
        self.assertTrue(log_file.exists(), "桥接调用必须写穿审计日志")
        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(e.get("job_id") for e in entries))


if __name__ == "__main__":
    unittest.main()
