import re
import unittest
from pathlib import Path

import utils.web_fetch as web_fetch
from conftest import skill_module
from utils.config_loader import PROJECT_ROOT

FORBIDDEN_TOP_LEVEL_MODULES = {
    "requests",
    "httpx",
    "openai",
    "aiohttp",
    "urllib3",
    "socket",
    "anthropic",
    "google",
    "boto3",
}
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z0-9_\.]+)")
KEY_READ_RE = re.compile(r"(?i)(environ\[|getenv\(|environ\.get\()")
API_KEY_WORD_RE = re.compile(r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret|bearer\s)")


def _python_sources():
    for folder in ("utils", "skills"):
        yield from sorted((PROJECT_ROOT / folder).rglob("*.py"))


class NoApiKeyTests(unittest.TestCase):
    """零 API Key / 零第三方 SDK 的静态与运行时守护。"""

    def test_no_third_party_network_or_sdk_imports(self):
        offenders = []
        for path in _python_sources():
            for line in path.read_text(encoding="utf-8").splitlines():
                match = IMPORT_RE.match(line)
                if not match:
                    continue
                top = match.group(1).split(".")[0]
                if top in FORBIDDEN_TOP_LEVEL_MODULES:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {line.strip()}")
        self.assertEqual(offenders, [], f"禁止第三方网络/SDK 依赖: {offenders}")

    def test_no_api_key_reads_from_environment(self):
        offenders = []
        for path in _python_sources():
            for line in path.read_text(encoding="utf-8").splitlines():
                if KEY_READ_RE.search(line):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {line.strip()[:110]}")
        self.assertEqual(offenders, [], f"禁止从环境/命令行读取密钥: {offenders}")

    def test_no_key_words_outside_the_guard_module(self):
        # utils/web_fetch.py 是"密钥防护"实现本身，允许出现 Authorization / _API_KEY 字样；
        # 其余任何文件出现这些字样都视为违规。
        offenders = []
        for path in _python_sources():
            if path.name == "web_fetch.py" and path.parent.name == "utils":
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if API_KEY_WORD_RE.search(line):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {line.strip()[:110]}")
        self.assertEqual(offenders, [], f"业务代码不得出现密钥字样: {offenders}")

    def test_only_web_fetch_mentions_authorization_header(self):
        mentioned = [
            str(path.relative_to(PROJECT_ROOT))
            for path in _python_sources()
            if "Authorization" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(mentioned, ["utils\\web_fetch.py"] if "\\" in str(PROJECT_ROOT) else ["utils/web_fetch.py"])

    def test_web_fetch_refuses_key_headers(self):
        with self.assertRaises(AssertionError):
            web_fetch._assert_key_free({"Authorization": "Bearer secret"})
        with self.assertRaises(AssertionError):
            web_fetch._assert_key_free({"X-Api-Key": "abcd"})
        web_fetch._assert_key_free({"User-Agent": "paper-polish-project/1.0", "Accept": "application/json"})

    def test_forbidden_env_key_detection(self):
        detected = web_fetch.forbidden_env_keys({"OPENAI_API_KEY": "x", "PATH": "/usr/bin"})
        self.assertEqual(detected, ["OPENAI_API_KEY"])
        self.assertEqual(web_fetch.forbidden_env_keys({}), [])

    def test_web_fetch_has_no_module_level_key_state(self):
        source = (PROJECT_ROOT / "utils" / "web_fetch.py").read_text(encoding="utf-8")
        self.assertNotIn("os.getenv(", source)
        self.assertNotIn("environ[", source)


class LiteraturePipelineTests(unittest.TestCase):
    """batch-processor 的文献步骤接入。"""

    def setUp(self):
        import shutil
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.paper = self.tmp / "paper.md"
        self.paper.write_text(
            "# EEG 去噪研究\n\n"
            "## 1 引言\n\n"
            "近年来，基于深度学习的脑电去噪方法研究进展迅速。"
            "该方法在 EEGDenoiseNet 数据集上的表现明显优于现有主流方法，且首次实现了双判别器协同训练。\n",
            encoding="utf-8",
        )
        self.module = skill_module("batch-processor")

    def test_known_skills_include_literature_steps(self):
        self.assertIn("lit-search", self.module.KNOWN_SKILLS)
        self.assertIn("lit-verify", self.module.KNOWN_SKILLS)
        self.assertIn("submission-audit", self.module.KNOWN_SKILLS)
        steps = self.module._apply_pipeline_steps(
            "cn-polish,claim-guard,lit-search,lit-verify,cite-manager", "cn"
        )
        self.assertEqual(steps[-3:], ["lit-search", "lit-verify", "cite-manager"])

    def test_build_with_literature_flag_reports_gaps(self):
        out = self.tmp / "out"
        summary = self.module.run(
            {
                "inputs": [str(self.paper)],
                "language": "cn",
                "phase": "build",
                "out": str(out),
                "literature": True,
            }
        )
        item = summary["items"][0]
        self.assertEqual(item["status"], "built")
        self.assertGreaterEqual(item["literature"]["gap_count"], 1)
        self.assertTrue(Path(item["literature"]["gaps_file"]).exists())

    def test_build_without_literature_flag_skips_detection(self):
        out = self.tmp / "out2"
        summary = self.module.run(
            {"inputs": [str(self.paper)], "language": "cn", "phase": "build", "out": str(out)}
        )
        self.assertEqual(summary["items"][0]["literature"], {})

    def test_unknown_step_still_rejected(self):
        with self.assertRaises(ValueError):
            self.module._apply_pipeline_steps("cn-polish,nope", "cn")


if __name__ == "__main__":
    unittest.main()
