"""P0.2 dual-role smoke admission is offline-tested without provider calls."""
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.integrations.model import ConfiguredModelClient


class ModelSmokePairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.args = [
            "--state-root", str(self.root),
            "--lead", "glm", "--executor", "kimi",
            "models", "smoke", "--role", "both",
        ]

    def test_without_explicit_authorization_neither_role_invokes_provider(self):
        out = StringIO()
        with patch("alpha_research.cli.Gateway",
                   side_effect=AssertionError("BRAIN access forbidden")), patch(
                       "alpha_research.cli.build_model_client",
                       side_effect=AssertionError("model access forbidden")), \
                redirect_stdout(out):
            rc = main(self.args)
        self.assertEqual(rc, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "NOT_EXECUTED")
        self.assertEqual([row["role"] for row in result["probes"]],
                         ["research_lead", "research_executor"])
        self.assertFalse(result["provider_invoked"])
        self.assertFalse(result["brain_invoked"])
        self.assertTrue(all(row["status"] == "NOT_EXECUTED"
                            for row in result["probes"]))

    def test_authorized_smoke_calls_each_role_only_once_and_without_brain(self):
        out = StringIO()
        calls = []

        def fake_probe(model_config, root, *, role, allow_model_call):
            self.assertTrue(allow_model_call)
            calls.append(role)
            return {"role": role, "status": "PASS",
                    "research_id": "offline:" + role, "contract": "model_probe_v1"}

        with patch.dict(os.environ, {"ZAI_API_KEY": "fixture",
                                     "MOONSHOT_API_KEY": "fixture"}), patch(
               "alpha_research.cli.run_model_probe", side_effect=fake_probe), patch(
               "alpha_research.cli.Gateway",
               side_effect=AssertionError("BRAIN access forbidden")), \
                redirect_stdout(out):
            rc = main(self.args + ["--allow-model-call"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["research_lead", "research_executor"])
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["provider_invoked"])
        self.assertFalse(result["brain_invoked"])
        self.assertEqual(len(result["probes"]), 2)

    def test_contract_mismatch_short_circuits_second_role(self):
        out = StringIO()
        calls = []

        def mismatch(model_config, root, *, role, allow_model_call):
            calls.append(role)
            return {"role": role, "status": "CONTRACT_MISMATCH",
                    "contract": "model_probe_v1"}

        with patch.dict(os.environ, {"ZAI_API_KEY": "fixture",
                                     "MOONSHOT_API_KEY": "fixture"}), patch(
               "alpha_research.cli.run_model_probe", side_effect=mismatch), \
                redirect_stdout(out):
            rc = main(self.args + ["--allow-model-call"])
        self.assertEqual(rc, 2)
        self.assertEqual(calls, ["research_lead"])
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "CONTRACT_MISMATCH")
        self.assertEqual(len(result["probes"]), 1)

    def test_missing_executor_key_rejects_before_any_paid_call(self):
        error = StringIO()
        with patch.dict(os.environ, {"ZAI_API_KEY": "fixture",
                                     "MOONSHOT_API_KEY": ""}), patch(
               "alpha_research.cli.run_model_probe",
               side_effect=AssertionError("must not invoke model")), \
                redirect_stderr(error):
            rc = main(self.args + ["--allow-model-call"])
        self.assertEqual(rc, 2)
        self.assertIn("ModelProbeError", error.getvalue())

    def test_model_probe_caps_output_tokens_without_affecting_research_tasks(self):
        envelopes = []

        def no_network(url, headers, payload):
            envelopes.append(payload)
            return {"choices": [{"message": {"content":
                '{"ready":true,"contract":"model_probe_v1"}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 11}}

        config = {
            "provider": "openai_compatible",
            "url": "https://example.invalid/chat/completions",
            "api_key_env": "FIXTURE_MODEL_KEY",
            "model": "fixture",
            "response_format": "none",
        }
        client = ConfiguredModelClient(
            config, transport=no_network, environ={"FIXTURE_MODEL_KEY": "secret"})
        response = client.complete_json("model_probe", {"no_tools": True})
        self.assertEqual(response,
                         {"ready": True, "contract": "model_probe_v1"})
        self.assertEqual(envelopes[0]["max_tokens"], 512)
        self.assertNotIn("tools", envelopes[0])
        client.complete_json("attribute_results", {})
        self.assertNotIn("max_tokens", envelopes[1])

    def test_single_role_requires_explicit_permission_for_real_provider(self):
        output = StringIO()
        with redirect_stdout(output):
            rc = main(self.args[:-1] + ["research_lead"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "NOT_EXECUTED")


if __name__ == "__main__":
    unittest.main()
