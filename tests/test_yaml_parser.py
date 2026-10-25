"""轻量 YAML 回退解析器的语法回归 + 全仓库配置冒烟 + PyYAML 一致性。

背景：解析器曾不支持行内列表（review-sim.yaml 踩坑）与列表内联映射
（config/journals/*.yaml 踩坑），被迫把配置写成规避样式。本套件锁定补全后
的语法能力，并对 config/ 下全部 YAML 做加载冒烟，防止回归。
"""

from __future__ import annotations

import unittest

from utils.config_loader import (
    CONFIG_DIR,
    ConfigError,
    _minimal_yaml_load,
    load_yaml_file,
)

try:
    import yaml as _pyyaml  # noqa: F401

    HAS_PYYAML = True
except Exception:
    HAS_PYYAML = False


def _parse(text: str):
    return _minimal_yaml_load(text)


class ScalarTests(unittest.TestCase):
    def test_scalar_typing(self):
        data = _parse(
            'a: 1\nb: -2.5\nc: true\nd: false\ne: null\n'
            'f: "quoted string"\ng: plain string\nh: 1.5e-3\n'
        )
        self.assertEqual(data["a"], 1)
        self.assertIsInstance(data["a"], int)
        self.assertEqual(data["b"], -2.5)
        self.assertIs(data["c"], True)
        self.assertIs(data["d"], False)
        self.assertIsNone(data["e"])
        self.assertEqual(data["f"], "quoted string")
        self.assertEqual(data["g"], "plain string")
        self.assertAlmostEqual(data["h"], 0.0015)

    def test_colon_without_space_is_scalar(self):
        # YAML 规范：a:1 是标量；URL 中的冒号不切键
        data = _parse('endpoint: https://api.example.org/works\nurl_scalar: https://x.org\n')
        self.assertEqual(data["endpoint"], "https://api.example.org/works")
        self.assertEqual(data["url_scalar"], "https://x.org")

    def test_quoted_key_unquoted(self):
        data = _parse('"review-gate::numeric_missing": P0\nplain-key: v\n')
        self.assertIn("review-gate::numeric_missing", data)
        self.assertNotIn('"review-gate::numeric_missing"', data)
        self.assertEqual(data["plain-key"], "v")

    def test_comments_stripped(self):
        data = _parse("# 全行注释\nkey: value  # 行尾注释\nother: '# 不是注释'\n")
        self.assertEqual(data["key"], "value")
        self.assertEqual(data["other"], "# 不是注释")


class FlowSyntaxTests(unittest.TestCase):
    def test_inline_flow_list(self):
        data = _parse("sections: [method, experiment, discussion]\n")
        self.assertEqual(data["sections"], ["method", "experiment", "discussion"])

    def test_inline_flow_list_quoted_items(self):
        data = _parse('patterns: ["^abstract", "^index terms", \'含 空格\']\n')
        self.assertEqual(data["patterns"], ["^abstract", "^index terms", "含 空格"])

    def test_inline_flow_list_empty(self):
        data = _parse("items: []\n")
        self.assertEqual(data["items"], [])

    def test_inline_flow_map(self):
        data = _parse("weights: {title_match: 1.5, recency: 0.8}\n")
        self.assertEqual(data["weights"], {"title_match": 1.5, "recency": 0.8})

    def test_inline_flow_nested(self):
        data = _parse('matrix: [[1, 2], [3, 4]]\nmixed: [{k: v}, "a, b"]\n')
        self.assertEqual(data["matrix"], [[1, 2], [3, 4]])
        self.assertEqual(data["mixed"], [{"k": "v"}, "a, b"])

    def test_unbalanced_flow_raises(self):
        with self.assertRaises(ConfigError):
            _parse("items: [a, b\n")


class ListOfMapsTests(unittest.TestCase):
    def test_list_inline_map_with_nested_list(self):
        text = (
            "required:\n"
            "  - type: abstract\n"
            '    patterns:\n'
            '      - "^abstract"\n'
            '      - "^index terms"\n'
            "  - type: method\n"
            '    patterns:\n'
            '      - "^method"\n'
        )
        data = _parse(text)
        self.assertEqual(
            data["required"],
            [
                {"type": "abstract", "patterns": ["^abstract", "^index terms"]},
                {"type": "method", "patterns": ["^method"]},
            ],
        )

    def test_list_inline_map_with_nested_map(self):
        text = (
            "servers:\n"
            "  - name: crossref\n"
            "    network:\n"
            "      timeout: 20\n"
            "      retries: 2\n"
        )
        data = _parse(text)
        self.assertEqual(
            data["servers"],
            [{"name": "crossref", "network": {"timeout": 20, "retries": 2}}],
        )

    def test_list_inline_map_single_line(self):
        data = _parse("entries:\n  - id: a\n  - id: b\n")
        self.assertEqual(data["entries"], [{"id": "a"}, {"id": "b"}])

    def test_empty_dash_is_null_then_nested_block(self):
        data = _parse("tree:\n  -\n    - nested\n  - plain\n")
        self.assertEqual(data["tree"], [["nested"], "plain"])
        data = _parse("hole:\n  -\n")
        self.assertEqual(data["hole"], [None])

    def test_list_items_with_colons_not_maps(self):
        # 引号内的冒号 / URL 冒号都不是映射
        data = _parse('refs:\n  - "Smith et al. 2020: found"\n  - https://example.org/x\n')
        self.assertEqual(data["refs"], ["Smith et al. 2020: found", "https://example.org/x"])


class BlockStructureTests(unittest.TestCase):
    def test_nested_block_map_and_list(self):
        text = (
            "network:\n"
            "  enabled: true\n"
            "  retries: 2\n"
            "sources:\n"
            "  - crossref\n"
            "  - openalex\n"
        )
        data = _parse(text)
        self.assertEqual(data["network"], {"enabled": True, "retries": 2})
        self.assertEqual(data["sources"], ["crossref", "openalex"])

    def test_mismatched_indent_raises(self):
        with self.assertRaises(ConfigError):
            _parse("a:\n    b: 1\n   c: 2\n")

    def test_tab_indent_raises(self):
        with self.assertRaises(ConfigError):
            _parse("a:\n\tb: 1\n")

    def test_top_level_list_raises(self):
        with self.assertRaises(ConfigError):
            _parse("- a\n- b\n")


class RepoConfigSmokeTests(unittest.TestCase):
    """config/ 下全部 YAML 必须可用回退解析器加载且顶层为映射。"""

    def test_all_repo_configs_parse_with_fallback(self):
        configs = sorted(CONFIG_DIR.rglob("*.yaml"))
        self.assertGreater(len(configs), 10)
        for path in configs:
            with self.subTest(config=str(path.relative_to(CONFIG_DIR))):
                data = load_yaml_file(path)
                self.assertIsInstance(data, dict)

    def test_regression_journal_profiles(self):
        profile = load_yaml_file(CONFIG_DIR / "journals" / "ieee-trans.yaml")
        declarations = profile["required_declarations"]["conflict_of_interest"]
        self.assertIsInstance(declarations["patterns"], list)
        self.assertIn("conflict of interest", declarations["patterns"])
        self.assertEqual(profile["word_limits"]["abstract_max_words"], 250)
        sections = profile["sections"]["required"]
        self.assertEqual(sections["abstract"]["patterns"], ["^abstract", "^index terms"])

    def test_regression_issue_rules_unquoted_keys(self):
        rules = load_yaml_file(CONFIG_DIR / "issue-rules.yaml")
        overrides = rules["priority_overrides"]
        self.assertIn("review-gate::numeric_missing", overrides)
        self.assertEqual(overrides["review-gate::numeric_missing"], "P0")
        self.assertIn("lit-verify::rejected", overrides)

    def test_regression_literature_sources(self):
        lit = load_yaml_file(CONFIG_DIR / "literature.yaml")
        crossref = lit["sources"]["crossref"]
        self.assertEqual(crossref["kind"], "crossref")
        self.assertEqual(crossref["rows"], 8)
        self.assertAlmostEqual(crossref["weight"], 1.0)
        self.assertEqual(lit["network"]["timeout_seconds"], 20)


@unittest.skipUnless(HAS_PYYAML, "环境装有 PyYAML 时校验两套实现一致性")
class PyYamlParityTests(unittest.TestCase):
    def _parity(self, text: str):
        self.assertEqual(_minimal_yaml_load(text), yaml_safe_load_dict(text))

    def test_parity_on_repo_syntax_subset(self):
        cases = [
            "a: 1\nb: [x, y]\nc: {k: v}\n",
            'overrides:\n  "x::y": P0\n',
            "items:\n  - type: a\n    pats:\n      - \"^a\"\n",
            "urls:\n  - https://example.org/1\n  - https://example.org/2\n",
        ]
        for text in cases:
            with self.subTest(text=text.splitlines()[0]):
                self._parity(text)


def yaml_safe_load_dict(text: str):
    import yaml as _y

    data = _y.safe_load(text)
    return data if isinstance(data, dict) else {}


if __name__ == "__main__":
    unittest.main()
