"""Offline checks for runtime readiness and persisted model call governance."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from alpha_research.agents.doctor import check_registered_roles
from alpha_research.agents.governance import (
    ModelBudgetError, ModelCallLedger, validate_policy,
)
from alpha_research.agents.registry import (
    bundled_registry_path, load_registry, select_registered_backends,
)
from alpha_research.integrations.model import DecisionRecorder, build_model_client
from alpha_research.cli import main as research_main

ROOT = Path(__file__).resolve().parent.parent
POLICY = {"max_calls": 2, "max_reported_tokens": 20, "failover": "manual"}


def _model_config():
    raw = json.loads((ROOT / "config.mock.example.json").read_text(encoding="utf-8"))
    return raw["model"]


def _selected(lead="codex", executor="glm"):
    profile = json.loads((ROOT / "alpha_research" / "agents" /
                          "role_profile.json").read_text(encoding="utf-8"))
    return select_registered_backends(
        profile, load_registry(bundled_registry_path()),
        assignments={"research_lead": lead, "research_executor": executor},
    )


class ModelDoctorTests(unittest.TestCase):
    def test_reports_local_cli_and_credentials_without_remote_calls(self):
        result = check_registered_roles(
            _selected(), environ={"ZAI_API_KEY": "unprinted-secret"},
            which=lambda executable: "/usr/bin/codex" if executable == "codex" else None,
            sdk_present=lambda: False,
        )
        self.assertEqual(result["status"], "READY_LOCAL")
        self.assertTrue(result["roles"]["research_lead"]["ready_local"])
        self.assertTrue(result["roles"]["research_executor"]["ready_local"])
        self.assertFalse(result["provider_invoked"])
        self.assertFalse(result["brain_invoked"])
        self.assertFalse(result["remote_health_verified"])
        self.assertNotIn("unprinted-secret", json.dumps(result))

    def test_missing_executable_and_key_block_locally(self):
        result = check_registered_roles(
            _selected(), environ={}, which=lambda executable: None,
            sdk_present=lambda: False,
        )
        self.assertEqual(result["status"], "BLOCKED_LOCAL")
        self.assertFalse(result["roles"]["research_lead"]["ready_local"])
        self.assertFalse(result["roles"]["research_executor"]["ready_local"])

    def test_sdk_ready_only_with_package_and_environment(self):
        result = check_registered_roles(
            _selected(lead="antigravity", executor="kimi"),
            environ={"GEMINI_API_KEY": "fixture"}, sdk_present=lambda: True,
        )
        self.assertFalse(result["roles"]["research_executor"]["ready_local"])
        self.assertTrue(result["roles"]["research_lead"]["ready_local"])
        self.assertEqual(result["status"], "BLOCKED_LOCAL")

    def test_cli_doctor_never_calls_provider_or_brain(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("alpha_research.cli.Gateway", side_effect=AssertionError("brain call")), \
             patch("subprocess.run", side_effect=AssertionError("agent call")), \
             patch("alpha_research.agents.doctor.shutil.which", return_value=None):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = research_main([
                    "--state-root", folder, "--lead", "codex",
                    "--executor", "glm", "models", "doctor",
                ])
            self.assertEqual(result, 2)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["status"], "BLOCKED_LOCAL")
            self.assertFalse(report["provider_invoked"])


class ModelLedgerTests(unittest.TestCase):
    def test_attempt_budget_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = ModelCallLedger(folder, POLICY)
            ledger.reserve("R1", "C1", task="plan_experiments", role="research_lead")
            ledger.settle("R1", "C1", success=True, token_in=5, token_out=7)
            reloaded = ModelCallLedger(folder, POLICY)
            reloaded.reserve("R1", "C2", task="attribute_results",
                             role="research_executor")
            reloaded.settle("R1", "C2", success=False)
            status = ledger.status("R1")
            self.assertEqual(status["attempts"], 2)
            self.assertEqual(status["successful"], 1)
            self.assertEqual(status["failed"], 1)
            self.assertEqual(status["reported_tokens"], 12)
            self.assertEqual(status["unknown_usage_attempts"], 1)
            self.assertTrue(status["blocked"])
            self.assertIsNone(status["estimated_cost"])
            with self.assertRaisesRegex(ModelBudgetError, "attempt budget exhausted"):
                ledger.reserve("R1", "C3", task="research_decide")

    def test_reported_token_limit_blocks_next_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = ModelCallLedger(folder, POLICY)
            ledger.reserve("R1", "C1", task="plan_experiments")
            ledger.settle("R1", "C1", success=True, token_in=15, token_out=6)
            self.assertEqual(ledger.status("R1")["reported_tokens"], 21)
            with self.assertRaisesRegex(ModelBudgetError, "token budget exhausted"):
                ledger.reserve("R1", "C2", task="plan_experiments")

    def test_crash_intent_requires_explicit_reconciliation(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = ModelCallLedger(folder, POLICY)
            ledger.reserve("R1", "C1", task="plan_experiments")
            self.assertEqual(ledger.status("R1")["unresolved"], 1)
            after_restart = ModelCallLedger(folder, POLICY)
            with self.assertRaisesRegex(ModelBudgetError, "unresolved model call"):
                after_restart.reserve("R1", "C2", task="plan_experiments")
            after_restart.settle("R1", "C1", success=False)
            after_restart.reserve("R1", "C2", task="research_decide")
            after_restart.settle("R1", "C2", success=True)
            self.assertEqual(after_restart.status("R1")["unresolved"], 0)

    def test_tampered_ledger_and_changed_policy_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = ModelCallLedger(folder, POLICY)
            ledger.reserve("R1", "C1", task="plan_experiments")
            ledger.settle("R1", "C1", success=True)
            altered = dict(POLICY, max_calls=4)
            with self.assertRaisesRegex(ModelBudgetError, "policy changed"):
                ModelCallLedger(folder, altered).status("R1")
            path, _ = ledger._paths("R1")
            row = json.loads(path.read_text(encoding="utf-8"))
            row["calls"][0]["status"] = "IN_FLIGHT"
            path.write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaisesRegex(ModelBudgetError, "invalid"):
                ledger.status("R1")

    def test_automatic_failover_policy_is_disallowed(self):
        with self.assertRaisesRegex(ModelBudgetError, "automatic model failover"):
            validate_policy(dict(POLICY, failover="automatic"))
        for invalid in (True, 0, -1, 10_000_001):
            with self.assertRaises(ModelBudgetError):
                validate_policy(dict(POLICY, max_calls=invalid))

    def test_same_user_can_run_separate_research_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = ModelCallLedger(folder, POLICY)
            ledger.reserve("R1", "C1", task="plan_experiments")
            ledger.settle("R1", "C1", success=False)
            self.assertEqual(ledger.status("R2")["attempts"], 0)
            ledger.reserve("R2", "C1", task="plan_experiments")
            ledger.settle("R2", "C1", success=True)
            self.assertEqual(ledger.status("R1")["attempts"], 1)
            self.assertEqual(ledger.status("R2")["attempts"], 1)

    def test_recorder_counts_calls_before_model_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = DecisionRecorder(
                build_model_client(_model_config()),
                Path(folder), governance=POLICY,
            )
            recorder.begin_session(research_id="R1")
            recorder.complete_json("plan_experiments", {"iteration": 1})
            recorder.complete_json("attribute_results", {"iteration": 2})
            status = recorder.budget.status("R1")
            self.assertEqual(status["attempts"], 2)
            self.assertEqual(status["successful"], 2)
            with self.assertRaisesRegex(ModelBudgetError, "attempt budget exhausted"):
                recorder.complete_json("plan_experiments", {"iteration": 3})

    def test_recorder_counts_error_as_attempt_not_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = DecisionRecorder(
                build_model_client(_model_config()),
                Path(folder), governance=POLICY,
            )
            recorder.begin_session(research_id="R1")
            with self.assertRaises(Exception):
                recorder.complete_json("task_without_mock_response", {})
            report = recorder.budget.status("R1")
            self.assertEqual(report["attempts"], 1)
            self.assertEqual(report["failed"], 1)
            self.assertEqual(report["unresolved"], 0)

    def test_batch_replay_does_not_consume_another_call(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = dict(POLICY, max_calls=1)
            batch = Path(folder) / "batch"
            first = DecisionRecorder(
                build_model_client(_model_config()), Path(folder), governance=policy,
            )
            first.begin_session(research_id="R1")
            first.bind_batch(batch)
            result = first.complete_json("plan_experiments", {"step": 1})
            second = DecisionRecorder(
                build_model_client(_model_config()), Path(folder), governance=policy,
            )
            second.begin_session(research_id="R1")
            second.bind_batch(batch)
            self.assertEqual(
                second.complete_json("plan_experiments", {"step": 1}), result)
            self.assertEqual(second.budget.status("R1")["attempts"], 1)

    def test_models_budget_cli_is_local_only(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("alpha_research.cli.Gateway", side_effect=AssertionError("BRAIN called")):
            root = Path(folder)
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({"model_governance": POLICY}), encoding="utf-8")
            ledger = ModelCallLedger(root / "state", POLICY)
            ledger.reserve("R1", "C1", task="plan_experiments")
            ledger.settle("R1", "C1", success=True, token_in=3, token_out=4)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = research_main([
                    "--state-root", str(root), "--config", str(cfg_path),
                    "models", "budget", "--research-id", "R1",
                ])
            self.assertEqual(rc, 0)
            output = json.loads(buf.getvalue())
            self.assertEqual(output["reported_tokens"], 7)
            self.assertEqual(output["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
