"""文档同步闸门（S8）：文档落后于代码时测试直接红。

覆盖三类机械可判定的同步：
1. AGENTS.md 调度表必须恰好覆盖 skills/ 下全部 Skill（多写 / 少写都红）；
2. prompts/{cn,en}/ 下每个模板必须有消费者（配置或代码引用其文件名；
   review-sim-* 为 f"review-sim-{role}.md" 动态构造，按前缀族判定）；
3. CHANGELOG 与验证报告存在且覆盖关键锚点。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from conftest import ROOT

AGENTS = ROOT / "AGENTS.md"
SKILLS_DIR = ROOT / "skills"
PROMPTS_DIR = ROOT / "prompts"
CHANGELOG = ROOT / "CHANGELOG.md"
VALIDATION_REPORT = ROOT / "docs" / "validation-report.md"

# 动态构造模板名的消费证据：skill 文件中必须出现的构造前缀
_DYNAMIC_TEMPLATE_FAMILIES = {
    "review-sim-": "skills/review-sim/main.py",
    "cite-intent.md": None,  # 字面引用，走通用路径
}


def _agents_table_skills() -> set[str]:
    skills = set()
    in_table = False
    for line in AGENTS.read_text(encoding="utf-8").splitlines():
        if line.startswith("| Skill |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and cells[0] and not set(cells[0]) <= {"-", " "}:
                skills.add(cells[0])
    return skills


def _repo_text() -> str:
    parts = []
    for pattern in ("config/**/*.yaml", "skills/**/main.py", "utils/*.py", "tools/*.py"):
        for path in ROOT.glob(pattern):
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


class DocsSyncTests(unittest.TestCase):
    def test_agents_table_matches_skills_dir(self):
        on_disk = {p.name for p in SKILLS_DIR.iterdir() if (p / "main.py").exists()}
        in_table = _agents_table_skills()
        self.assertEqual(
            on_disk - in_table,
            set(),
            f"以下 Skill 未登记进 AGENTS.md 调度表: {sorted(on_disk - in_table)}",
        )
        self.assertEqual(
            in_table - on_disk,
            set(),
            f"AGENTS.md 调度表列出了不存在的 Skill: {sorted(in_table - on_disk)}",
        )

    def test_every_prompt_template_has_consumer(self):
        repo_text = _repo_text()
        orphans = []
        for language in ("cn", "en"):
            for template in sorted((PROMPTS_DIR / language).glob("*.md")):
                if template.name in repo_text:
                    continue
                # 动态构造家族：review-sim-{role}.md 由前缀 f-string 生成
                prefix = next(
                    (p for p in _DYNAMIC_TEMPLATE_FAMILIES if template.name.startswith(p)),
                    None,
                )
                if prefix and _DYNAMIC_TEMPLATE_FAMILIES[prefix]:
                    consumer = ROOT / _DYNAMIC_TEMPLATE_FAMILIES[prefix]
                    if prefix in consumer.read_text(encoding="utf-8"):
                        continue
                orphans.append(f"{language}/{template.name}")
        self.assertEqual(
            orphans,
            [],
            "以下 prompt 模板没有任何消费者（配置或代码未引用其文件名）: " + ", ".join(orphans),
        )

    def test_changelog_exists_and_covers_releases(self):
        self.assertTrue(CHANGELOG.exists(), "CHANGELOG.md 不存在")
        text = CHANGELOG.read_text(encoding="utf-8")
        self.assertIn("v0.11.0", text)
        self.assertIn("v0.12.0", text)
        # 本仓库的标志性新增必须进入变更记录
        for token in ("review-sim", "cite-intent", "stat-check", "journal"):
            self.assertIn(token, text, f"CHANGELOG 缺少 {token} 相关条目")

    def test_validation_report_covers_semantic_features(self):
        self.assertTrue(VALIDATION_REPORT.exists(), "docs/validation-report.md 不存在")
        text = VALIDATION_REPORT.read_text(encoding="utf-8")
        for token in ("review-sim", "cite-intent", "validated"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
