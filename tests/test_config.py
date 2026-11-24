"""System defaults and Scope-only configuration contract."""
import json
import tempfile
import unittest
from pathlib import Path

from alpha_research.config import (
    DEFAULT_CONFIG, ConfigError, load_config, save_config,
)


class SystemConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config" / "nastu_quant.json"

    def test_default_template_matches_the_single_code_owned_default(self):
        template = Path(__file__).resolve().parents[1] / "config.example.json"
        self.assertEqual(json.loads(template.read_text(encoding="utf-8")),
                         DEFAULT_CONFIG)

    def test_missing_default_config_is_created_complete(self):
        config = load_config(self.path)
        self.assertTrue(self.path.is_file())
        self.assertEqual(config, DEFAULT_CONFIG)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         DEFAULT_CONFIG)
        self.assertIn("model_governance", config)
        self.assertNotIn("model", config)

    def test_only_scope_is_preserved_and_fixed_sections_are_restored(self):
        raw = {
            "scope": {
                "region": "USA", "dataset_id": "analyst10",
                "category": "ANALYST", "delay": 1, "universe": "TOP3000",
            },
            "gate": {"sharpe_min": -999},
            "preferred": {"weight_max": 999},
            "auth": {"mode": "manual", "credential_env": "WRONG_ENV"},
            "knowledge": {"field_root": "C:/user/override"},
            "model_governance": {
                "max_calls": 1, "max_reported_tokens": 1, "failover": "automatic",
            },
            "probe": {"windows": []},
            "exec": {"multisim_wait_threshold_s": 0},
            "model": {"provider": "untrusted-inline-provider"},
            "user_defined_section": {"unexpected": True},
        }
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(raw), encoding="utf-8")

        config = load_config(self.path)

        self.assertEqual(config["scope"]["region"], "USA")
        self.assertEqual(config["scope"]["dataset_id"], "analyst10")
        self.assertEqual(config["scope"]["category"], "ANALYST")
        self.assertEqual(config["scope"]["delay"], 1)
        self.assertEqual(config["scope"]["universe"], "TOP3000")
        for section in ("gate", "preferred", "auth", "knowledge",
                        "model_governance", "probe", "exec"):
            self.assertEqual(config[section], DEFAULT_CONFIG[section])
        self.assertNotIn("model", config)
        self.assertNotIn("user_defined_section", config)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         config)

    def test_unknown_scope_keys_fail_closed_without_overwriting_file(self):
        self.path.parent.mkdir(parents=True)
        original = json.dumps({"scope": {"region": "USA", "unknown": "value"}})
        self.path.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "unsupported research scope keys"):
            load_config(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_explicit_missing_path_can_remain_missing(self):
        with self.assertRaises(FileNotFoundError):
            load_config(self.path, initialize=False)
        self.assertFalse(self.path.exists())

    def test_save_config_cannot_persist_user_overrides_to_fixed_sections(self):
        config = {
            "scope": {"region": "CHN"},
            "gate": {"sharpe_min": 0},
            "model_governance": {"max_calls": 1},
        }
        save_config(config, self.path)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["scope"]["region"], "CHN")
        self.assertEqual(saved["gate"], DEFAULT_CONFIG["gate"])
        self.assertEqual(saved["model_governance"], DEFAULT_CONFIG["model_governance"])


if __name__ == "__main__":
    unittest.main()
