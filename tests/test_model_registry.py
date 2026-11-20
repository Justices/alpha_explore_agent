"""Registry selection and CLI inspection are fully offline."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.agents.configuration import RoleRoutingConfig
from alpha_research.agents.registry import (
    ModelRegistryError, bundled_registry_path, load_registry,
    describe_registry, select_registered_backends,
)
from alpha_research.cli import main as research_main

ROOT = Path(__file__).resolve().parent.parent


def registry():
    return load_registry(ROOT / "model_registry.example.json")


def role_profile():
    return json.loads((ROOT / "alpha_research" / "agents" /
                       "role_profile.json").read_text(encoding="utf-8"))


class ModelRegistryTests(unittest.TestCase):
    def test_default_bundled_registry_matches_editable_preset(self):
        self.assertEqual(load_registry(bundled_registry_path()), registry())
        items = describe_registry(registry())
        self.assertFalse(items["provider_invoked"])
        self.assertEqual({x["id"] for x in items["entries"]}, {
            "codex", "claude-code", "antigravity", "deepseek", "glm", "kimi",
        })
        self.assertEqual({x["kind"] for x in items["entries"]},
                         {"agent_runtime", "llm_api"})
        self.assertNotIn('"api_key":', json.dumps(items))
        self.assertEqual(registry()["entries"]["claude-code"]["provider"], "claude_cli")

    def test_compile_roles_from_registered_backends_only(self):
        profile = role_profile()
        original = copy.deepcopy(profile)
        profile["backends"] = {"old": {"provider": "bogus"}}
        selected = select_registered_backends(
            profile, registry(),
            assignments={"research_lead": "claude-code",
                         "research_executor": "glm"})
        self.assertEqual(profile["backends"], {"old": {"provider": "bogus"}})
        self.assertEqual(selected["backends"].keys(),
                         {"registry:claude-code", "registry:glm"})
        route = RoleRoutingConfig.parse(selected)
        self.assertEqual(route.resolve("plan_experiments").backend["provider"],
                         "claude_cli")
        self.assertEqual(route.resolve("attribute_results").backend["model"], "glm-4.6")
        self.assertEqual(original["roles"], profile["roles"])

    def test_same_llm_can_serve_multiple_roles(self):
        cfg = select_registered_backends(role_profile(), registry(), assignments={
            "research_lead": "deepseek", "research_executor": "deepseek",
        })
        self.assertEqual(len(cfg["backends"]), 1)
        self.assertEqual(cfg["roles"]["research_lead"]["backend"],
                         cfg["roles"]["research_executor"]["backend"])

    def test_missing_unknown_or_unsafe_registration_rejected(self):
        with self.assertRaisesRegex(ModelRegistryError, "every configured role"):
            select_registered_backends(role_profile(), registry(),
                                       assignments={"research_lead": "codex"})
        with self.assertRaisesRegex(ModelRegistryError, "unregistered backend"):
            select_registered_backends(role_profile(), registry(), assignments={
                "research_lead": "none", "research_executor": "kimi",
            })
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "registered.json"
            edited = registry()
            edited["entries"]["deepseek"]["kind"] = "agent_runtime"
            path.write_text(json.dumps(edited), encoding="utf-8")
            with self.assertRaisesRegex(ModelRegistryError, "invalid kind"):
                load_registry(path)
            edited["entries"]["deepseek"]["kind"] = "llm_api"
            edited["entries"]["deepseek"]["api_key"] = "SECRET"
            path.write_text(json.dumps(edited), encoding="utf-8")
            with self.assertRaisesRegex(ModelRegistryError, "secrets"):
                load_registry(path)

    def test_cli_list_is_offline_and_does_not_require_config(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("alpha_research.cli.Gateway", side_effect=AssertionError("BRAIN called")), \
             patch("subprocess.run", side_effect=AssertionError("Agent called")):
            output=io.StringIO()
            with redirect_stdout(output):
                rc=research_main(["--state-root", td, "models", "list"])
            self.assertEqual(rc,0)
            rendered=json.loads(output.getvalue())
            self.assertEqual(len(rendered["entries"]),6)
            self.assertFalse(rendered["provider_invoked"])

    def test_cli_inspect_assigns_llm_api_and_agent_runtime_without_calls(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("alpha_research.cli.Gateway", side_effect=AssertionError("BRAIN called")), \
             patch("subprocess.run", side_effect=AssertionError("Agent called")):
            output=io.StringIO()
            with redirect_stdout(output):
                rc=research_main([
                    "--state-root", td, "--lead", "kimi", "--executor",
                    "claude-code", "models", "inspect",
                ])
            self.assertEqual(rc,0)
            rendered=json.loads(output.getvalue())
            self.assertEqual(rendered["task_routes"]["plan_experiments"]["role"],
                             "research_lead")
            self.assertEqual(rendered["task_routes"]["attribute_results"]["provider"],
                             "claude_cli")
            self.assertEqual(rendered["roles"]["research_lead"]["kind"], "llm_api")
            self.assertEqual(rendered["roles"]["research_executor"]["kind"],
                             "agent_runtime")
            self.assertEqual(rendered["selected_registrations"], {
                "research_lead": "kimi", "research_executor": "claude-code",
            })

    def test_cli_inspect_fails_closed_for_partial_assignment(self):
        with tempfile.TemporaryDirectory() as td:
            err=io.StringIO()
            with redirect_stderr(err):
                rc=research_main(["--state-root", td, "--lead", "codex",
                                  "models", "inspect"])
            self.assertEqual(rc,2)
            self.assertIn("every configured role",err.getvalue())


if __name__ == "__main__":
    unittest.main()
