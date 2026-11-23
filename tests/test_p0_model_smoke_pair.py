"""P0.2 dual-role smoke admission is offline-tested without provider calls."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.integrations.model import ConfiguredModelClient
from alpha_research.agents.registry import (
    bundled_registry_path, load_registry, load_role_profile,
    select_registered_backends,
)
from alpha_research.agents.probe import run_model_probe


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
        options = []
        class OfflineSDK:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __init__(self):
                self.chat = SimpleNamespace(completions=SimpleNamespace(
                    create=self.create))
            def create(self, **payload):
                envelopes.append(payload)
                return SimpleNamespace(
                    model=payload["model"], usage=SimpleNamespace(
                        prompt_tokens=20, completion_tokens=11),
                    choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                        content='{"ready":true,"contract":"model_probe_v1"}'))])

        def factory(**kwargs):
            options.append(kwargs)
            return OfflineSDK()

        config = {
            "provider": "openai_sdk",
            "base_url": "https://example.invalid",
            "api_key_env": "FIXTURE_MODEL_KEY",
            "model": "fixture",
            "response_format": "none",
        }
        client = ConfiguredModelClient(
            config, sdk_factory=factory,
            environ={"FIXTURE_MODEL_KEY": "secret"})
        response = client.complete_json("model_probe", {"no_tools": True})
        self.assertEqual(response,
                         {"ready": True, "contract": "model_probe_v1"})
        self.assertEqual(envelopes[0]["max_tokens"], 512)
        self.assertNotIn("tools", envelopes[0])
        self.assertEqual(options[0]["max_retries"], 0)
        client.complete_json("attribute_results", {})
        self.assertNotIn("max_tokens", envelopes[1])

    def test_probe_persists_one_attempt_and_sealed_response_with_fake_transport(self):
        effective = select_registered_backends(
            load_role_profile(), load_registry(bundled_registry_path()),
            assignments={"research_lead": "glm", "research_executor": "kimi"},
        )

        class OfflineClient:
            config = {"provider": "offline-fixture"}
            last_usage = {"prompt_tokens": 7, "completion_tokens": 5}
            last_model = "offline-fixture"
            last_thread_id = None
            last_response_text = None

            def __init__(self):
                self.calls = 0

            def complete_json(self, task, context, *, thread_path=None):
                self.calls += 1
                if task != "model_probe" or not context["no_brain_requests"]:
                    raise AssertionError("only model_probe is authorized")
                return {"ready": True, "contract": "model_probe_v1"}

        client = OfflineClient()
        with patch("alpha_research.agents.probe.build_model_client",
                   side_effect=AssertionError("real client must not be built")):
            report = run_model_probe(
                effective, self.root, role="research_lead",
                allow_model_call=True, model_factory=lambda config: client)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(client.calls, 1)
        self.assertFalse(report["brain_invoked"])
        self.assertEqual(report["reported_usage"]["attempts"], 1)
        self.assertEqual(report["reported_usage"]["successful"], 1)
        self.assertEqual(report["reported_usage"]["reported_tokens"], 12)
        call_files = list((self.root / "state/logs/model_calls").glob("*.record.json"))
        self.assertEqual(len(call_files), 1)
        audit = json.loads(call_files[0].read_text(encoding="utf-8"))
        self.assertEqual(audit["task"], "model_probe")
        self.assertEqual(audit["research_id"], report["research_id"])
        response_file = (self.root / "state/logs/model_calls" /
                         (audit["call_id"] + ".response.json"))
        sealed = json.loads(response_file.read_text(encoding="utf-8"))
        self.assertEqual(sealed["status"], "ok")
        self.assertEqual(len(sealed["state_sha256"]), 64)

    def test_single_role_requires_explicit_permission_for_real_provider(self):
        output = StringIO()
        with redirect_stdout(output):
            rc = main(self.args[:-1] + ["research_lead"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "NOT_EXECUTED")


if __name__ == "__main__":
    unittest.main()
