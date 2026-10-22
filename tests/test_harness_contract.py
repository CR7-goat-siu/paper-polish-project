"""固定 coding harness 能力层边界：项目内不得恢复 CLI 或内置模型执行器。"""

import unittest
from pathlib import Path

from conftest import ROOT, skill_module


class HarnessArchitectureTests(unittest.TestCase):
    def test_model_execution_remains_outside_project(self):
        forbidden = (
            ROOT / "skills" / "agent-runner",
            ROOT / "skills" / "auto-polish",
            ROOT / "utils" / "agent_runner.py",
            ROOT / "config" / "agent-runner.yaml",
        )
        for path in forbidden:
            self.assertFalse(path.exists(), f"已移除的内置执行链不得回归: {path}")

    def test_skills_expose_programmatic_contract_only(self):
        for skill_dir in sorted((ROOT / "skills").iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / "main.py").exists():
                continue
            self.assertTrue((skill_dir / "SKILL.md").exists(), skill_dir.name)
            module = skill_module(skill_dir.name)
            self.assertTrue(callable(getattr(module, "run", None)), skill_dir.name)
            self.assertFalse(hasattr(module, "build_parser"), skill_dir.name)
            self.assertFalse(hasattr(module, "main"), skill_dir.name)

    def test_python_entrypoints_have_no_cli_parser(self):
        for base in ("skills", "tools", "utils"):
            for path in (ROOT / base).rglob("*.py"):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("import argparse", source, str(path))
                self.assertNotIn("ArgumentParser(", source, str(path))

    def test_docs_do_not_advertise_project_cli_commands(self):
        for path in ROOT.rglob("*.md"):
            if "output" in path.parts or ".workbuddy" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("py skills/", source, str(path))
            self.assertNotIn("python skills/", source, str(path))


if __name__ == "__main__":
    unittest.main()
