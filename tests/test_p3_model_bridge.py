"""P3 contract and permission tests: no real model/BRAIN calls."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from alpha_research.agents.probe import (
    ModelProbeError, prepare_probe, run_model_probe,
)
from alpha_research.cli import main as cli_main
from alpha_research.orchestration.model_evidence_gateway import (
    ModelGatewayPermissionError, ReadOnlyEvidenceGateway,
)

ROOT = Path(__file__).resolve().parent.parent


def mock_config(reply=None):
    config = json.loads((ROOT / "config.mock.example.json").read_text(
        encoding="utf-8"))["model"]
    config["backends"]["codex_mock"]["responses"]["model_probe"] = (
        {"ready": True, "contract": "model_probe_v1"} if reply is None else reply
    )
    return config


class NeverBrain:
    def call(self, name, **params):
        raise AssertionError("BRAIN must never be called by a model probe")


class FakeCatalog:
    def __init__(self):
        self.calls = []

    def call(self, name, **params):
        self.calls.append((name, params))
        return {"records": [{"id": "field_a"}], "action": name}


class ModelProbeTests(unittest.TestCase):
    def test_dry_run_never_constructs_or_invokes_backend(self):
        def no_model(*args):
            raise AssertionError("model cannot be created without authorization")
        with tempfile.TemporaryDirectory() as folder:
            preview = run_model_probe(
                mock_config(), folder, role="research_lead",
                model_factory=no_model)
            self.assertEqual(preview["status"], "NOT_EXECUTED")
            self.assertFalse(preview["provider_invoked"])
            self.assertFalse(preview["brain_invoked"])
            self.assertNotIn("research_id", preview)

    def test_exact_mock_contract_is_audited_once(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run_model_probe(mock_config(), folder, role="research_lead",
                                     allow_model_call=True)
            self.assertEqual(report["status"], "PASS")
            self.assertTrue(report["model_invoked"])
            self.assertFalse(report["provider_invoked"])
            self.assertFalse(report["brain_invoked"])
            self.assertEqual(report["reported_usage"]["attempts"], 1)
            self.assertEqual(report["reported_usage"]["successful"], 1)
            root = Path(folder) / "state" / "logs" / "model_calls"
            self.assertEqual(len(list(root.glob("*.record.json"))), 1)
            self.assertEqual(len(list(root.glob("*.response.json"))), 1)

    def test_model_response_is_strictly_checked_not_executed(self):
        with tempfile.TemporaryDirectory() as folder:
            malicious = {"ready": True, "contract": "model_probe_v1",
                         "tools": [{"action": "simulate_batch"}]}
            result = run_model_probe(
                mock_config(malicious), folder, role="research_lead",
                allow_model_call=True)
            self.assertEqual(result["status"], "CONTRACT_MISMATCH")
            self.assertFalse(result["brain_invoked"])
            self.assertNotIn("tools", result)

    def test_wrong_role_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ModelProbeError):
                run_model_probe(mock_config(), folder, role="unknown")

    def test_cli_smoke_is_always_offline_without_authorization(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("alpha_research.cli.Gateway", side_effect=AssertionError("BRAIN")), \
             patch("alpha_research.cli.run_model_probe",
                   wraps=run_model_probe) as probe, \
             patch("subprocess.run", side_effect=AssertionError("subprocess")):
            output=io.StringIO()
            with redirect_stdout(output):
                code=cli_main([
                    "--state-root", folder, "--lead", "codex", "--executor", "glm",
                    "models", "smoke", "--role", "research_lead",
                ])
            self.assertEqual(code,0)
            self.assertEqual(json.loads(output.getvalue())["status"], "NOT_EXECUTED")
            self.assertFalse(probe.call_args.kwargs.get("allow_model_call"))

    def test_cli_smoke_requires_explicit_role(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("sys.stderr", io.StringIO()):
                result=cli_main([
                    "--state-root", folder, "--lead", "codex", "--executor", "glm",
                    "models", "smoke",
                ])
            self.assertEqual(result, 2)


class ModelEvidenceGatewayTests(unittest.TestCase):
    def test_read_scoped_fields_and_persist_hash_only(self):
        gateway=FakeCatalog()
        with tempfile.TemporaryDirectory() as folder:
            safe=ReadOnlyEvidenceGateway(
                gateway, folder, research_id="R1", region="CHN",
                delay=1, universe="TOP3000")
            result=safe.read("get_datafields", dataset_id="risk60")
            self.assertEqual([x[0] for x in gateway.calls], ["get_datafields"])
            self.assertEqual(gateway.calls[0][1]["region"], "CHN")
            self.assertEqual(gateway.calls[0][1]["dataset_id"], "risk60")
            self.assertEqual(result["response"]["records"][0]["id"], "field_a")
            receipt=json.loads(Path(result["evidence_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(receipt["response_sha256"], result["response_sha256"])
            self.assertNotIn("response", receipt)

    def test_model_cannot_request_simulation_recovery_tag_or_submit(self):
        gateway=FakeCatalog()
        with tempfile.TemporaryDirectory() as folder:
            safe=ReadOnlyEvidenceGateway(gateway, folder, research_id="R1", region="CHN")
            for command in ("simulate_batch","simulate_super","resume_simulation_batch",
                            "tag_alpha","submit_alpha","set_case","get_user_alphas"):
                with self.assertRaisesRegex(ModelGatewayPermissionError, "unauthorized"):
                    safe.read(command)
            self.assertEqual(gateway.calls, [])

    def test_cannot_add_arbitrary_parameters_or_cross_dataset_ids(self):
        gateway=FakeCatalog()
        with tempfile.TemporaryDirectory() as folder:
            safe=ReadOnlyEvidenceGateway(gateway, folder, research_id="R1", region="CHN")
            with self.assertRaises(ModelGatewayPermissionError):
                safe.read("get_datasets", dataset_id="risk60")
            with self.assertRaises(ModelGatewayPermissionError):
                safe.read("get_datafields", dataset_id="../other")
            self.assertEqual(gateway.calls, [])

    def test_bad_response_is_not_stored_as_verified_evidence(self):
        class InvalidCatalog:
            def call(self, name, **params):
                return {"broken": float("nan")}
        with tempfile.TemporaryDirectory() as folder:
            safe=ReadOnlyEvidenceGateway(
                InvalidCatalog(), folder, research_id="R1", region="CHN")
            with self.assertRaises(ModelGatewayPermissionError):
                safe.read("get_datasets")
            self.assertEqual(
                list((Path(folder) / "state" / "model_evidence").rglob("*.json")),
                [])


if __name__ == "__main__":
    unittest.main()
