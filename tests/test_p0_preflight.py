"""P0 local-only readiness and model-free platform planning contracts.

No fixtures here open model, CLI agent, BRAIN or external HTTP connections.
"""
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from alpha_research import cli as entry
from workbench.runtime_models import (
    checked_selection, listed_models, registry_path, research_config_path,
)


class P0PreflightTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cfg = self.root / "config" / "nastu_quant.json"
        self.cfg.parent.mkdir(parents=True)
        self.cfg.write_text("{}", encoding="utf-8")
        self.arguments = ["--state-root", str(self.root)]

    def test_registered_choices_are_public_metadata_without_secrets(self):
        entries = listed_models(self.arguments, self.root)
        names = {row["id"] for row in entries["entries"]}
        self.assertTrue({"codex", "glm", "kimi", "deepseek"} <= names)
        self.assertFalse(entries["provider_invoked"])
        self.assertNotIn("offline-secret", json.dumps(entries))

    def test_launch_rejects_missing_or_unknown_roles_before_side_effect(self):
        for selection in ({}, {"lead": "glm"},
                          {"lead": "unknown", "executor": "kimi"},
                          {"lead": "glm", "executor": "unknown"},
                          {"lead": "../injection", "executor": "kimi"}):
            with self.subTest(selection=selection):
                with self.assertRaises(ValueError):
                    checked_selection(selection, self.arguments, self.root)

    def test_api_credentials_must_be_local_but_are_never_exposed(self):
        with patch.dict(os.environ, {"ZAI_API_KEY": "offline-secret",
                                     "MOONSHOT_API_KEY": "offline-secret"}):
            ready = checked_selection(
                {"lead": "glm", "executor": "kimi"}, self.arguments, self.root)
        self.assertEqual(ready["lead"], "glm")
        self.assertEqual(ready["executor"], "kimi")
        self.assertEqual(len(ready["profile_sha256"]), 64)
        self.assertNotIn("offline-secret", json.dumps(ready))
        with patch.dict(os.environ, {"ZAI_API_KEY": "", "MOONSHOT_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "本地条件"):
                checked_selection(
                    {"lead": "glm", "executor": "kimi"},
                    self.arguments, self.root)

    def test_explicit_registry_overrides_state_root_and_changes_fingerprint(self):
        baseline = listed_models(self.arguments, self.root)
        self.assertTrue(baseline["entries"])
        source = registry_path(self.arguments, self.root)
        customized = json.loads(source.read_text(encoding="utf-8"))
        altered = self.root / "custom-registry.json"
        altered.write_text(json.dumps(customized), encoding="utf-8")
        with patch.dict(os.environ, {"ZAI_API_KEY": "offline",
                                     "MOONSHOT_API_KEY": "offline"}):
            before = checked_selection(
                {"lead": "glm", "executor": "kimi"}, self.arguments, self.root)
            customized["entries"]["glm"]["model"] = "glm-offline-fixture-new"
            altered.write_text(json.dumps(customized), encoding="utf-8")
            new_args = [*self.arguments, "--registry", str(altered)]
            after = checked_selection(
                {"lead": "glm", "executor": "kimi"}, new_args, self.root)
        self.assertNotEqual(before["profile_sha256"], after["profile_sha256"])
        self.assertEqual(registry_path(new_args, self.root), altered.resolve())
        self.assertEqual(research_config_path(new_args, self.root), self.cfg)

    def test_model_free_planning_skips_both_model_registry_and_client(self):
        # Deliberately invalid model profile and missing registry should not
        # block safe platform planning, while the actual gateway is mocked.
        self.cfg.write_text(json.dumps({"model": {"provider": "bogus"}}),
                            encoding="utf-8")
        commands = (
            ["plan-regular", "--region", "CHN"],
            ["plan-super", "--region", "CHN", "--objective", "scope"],
            ["plan-optimization", "--alpha-id", "A1"],
        )
        for arguments in commands:
            with self.subTest(arguments=arguments):
                with patch.object(entry, "Gateway") as gateway, patch.object(
                    entry, "build_model_client",
                    side_effect=AssertionError("model should not be created")
                ), patch.object(
                    entry, "dispatch_research", return_value={"status": "planned"}
                ) as dispatch, redirect_stdout(StringIO()):
                    code = entry.main([
                        *self.arguments, "--registry", str(self.root / "missing.json"),
                        *arguments,
                    ])
                self.assertEqual(code, 0)
                gateway.return_value.start.assert_called_once()
                dispatch.assert_called_once()
                self.assertIsNone(dispatch.call_args.args[-1])

    def test_model_research_without_role_selection_still_fails_closed(self):
        with patch.object(entry, "Gateway") as gateway, redirect_stderr(StringIO()):
            code = entry.main([*self.arguments, "run-once", "--objective", "trial"])
        self.assertEqual(code, 2)
        gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
