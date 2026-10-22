"""配置与数据完整性测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from utils.config_loader import (
    CONFIG_DIR,
    DATA_DIR,
    detect_domain,
    get_model_config,
    list_domains,
    load_domain_pack,
    load_lexicon,
    load_models_config,
    load_rules_config,
    load_terms,
    normalize_language,
    resolve_model_key,
)


class ConfigLoaderTests(unittest.TestCase):
    def test_models_config_contract(self):
        cfg = load_models_config()
        self.assertEqual(cfg["provider"]["protocol"], "responses")
        self.assertIn("v4-pro", cfg["models"])
        self.assertIn("v4-flash", cfg["models"])
        self.assertEqual(cfg["models"]["v4-pro"]["model_id"], "deepseek-v4-pro")
        self.assertGreater(cfg["models"]["v4-pro"]["context_window"], 0)
        # 纯离线项目：request 块仅保留并发建议，不再有超时/重试/API 端点
        self.assertNotIn("timeout_seconds", cfg.get("request", {}))
        self.assertNotIn("api_key_env", cfg.get("provider", {}))
        self.assertGreaterEqual(int(cfg["request"].get("max_concurrent_requests", 0)), 1)

    def test_model_config_lookup(self):
        flash = get_model_config("v4-flash")
        self.assertEqual(flash["tier"], "flash")

    def test_routing_by_strength(self):
        self.assertEqual(resolve_model_key("en-polish", "light"), "v4-flash")
        self.assertEqual(resolve_model_key("en-polish", "deep"), "v4-pro")
        self.assertEqual(resolve_model_key("cn-polish", "rewrite"), "v4-pro")

    def test_rules_have_three_strengths_and_gates(self):
        for language in ("cn", "en"):
            rules = load_rules_config(language)
            self.assertIn("strengths", rules)
            self.assertIn("objectivity", rules)
            self.assertIn("coherence", rules)
            self.assertIn("conciseness", rules)
            for strength in ("light", "deep", "rewrite"):
                strength_cfg = rules["strengths"][strength]
                self.assertIn("prompt_template", strength_cfg)
                template = CONFIG_DIR.parent / "prompts" / language / strength_cfg["prompt_template"]
                self.assertTrue(template.exists(), f"missing template {template}")
                gates = strength_cfg["quality_gates"]
                for key in (
                    "unchanged_min_ratio",
                    "length_ratio_min",
                    "length_ratio_max",
                    "numbers_required",
                    "citations_required",
                    "paragraph_mode",
                    "hype_residue_allowed",
                    "term_error_max",
                    "max_gate_retries",
                ):
                    self.assertIn(key, gates)
                self.assertIn("temperature", strength_cfg)

    def test_glossary_json_loads(self):
        for language in ("cn", "en"):
            terms = load_terms(language)
            self.assertGreaterEqual(len(terms["entries"]), 10)
            for entry in terms["entries"]:
                self.assertTrue(entry["canonical"])

    def test_domain_packs(self):
        self.assertIn("generic", list_domains())
        self.assertIn("deep-learning", list_domains())
        pack = load_domain_pack("deep-learning")
        self.assertEqual(pack["id"], "deep-learning")
        with self.assertRaises(Exception):
            load_domain_pack("no-such-domain")

    def test_domain_terms_files_exist(self):
        for domain in list_domains():
            for language in ("cn", "en"):
                terms = load_terms(language, domain)
                self.assertGreaterEqual(len(terms["entries"]), 1, f"{domain}-{language}")

    def test_lexicons_load(self):
        for kind in ("hype", "informal"):
            for language in ("cn", "en"):
                lex = load_lexicon(kind, language)
                self.assertGreaterEqual(len(lex.get("entries", [])), 5)

    def test_detect_domain(self):
        eeg_text = "We record electroencephalography (EEG) signals and remove artifact with independent component analysis before motor imagery classification."
        self.assertEqual(detect_domain(eeg_text, "en"), "biomedical-eeg")
        dl_text = "Our convolutional neural network with long short-term memory is trained by backpropagation."
        self.assertEqual(detect_domain(dl_text, "en"), "deep-learning")
        self.assertEqual(detect_domain("A plain sentence about nothing special.", "en"), "generic")

    def test_normalize_language(self):
        self.assertEqual(normalize_language("zh-CN"), "cn")
        self.assertEqual(normalize_language("English"), "en")


if __name__ == "__main__":
    unittest.main()
