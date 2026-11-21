"""P4.5 guarded production boundary: no real BRAIN or paid model calls."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from alpha_research.integrations.brain import driver
from alpha_research.integrations.brain.gateway import Gateway, GatewayError
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.orchestration.simulation_dispatch_preflight import (
    inspect_simulation_dispatch_gate,
)


class GuardedGatewayTests(unittest.TestCase):
    def test_simulation_mutations_refused_before_driver(self):
        gateway = Gateway(orchestration_guarded=True)
        for command in (
            "simulate_batch", "simulate_super", "tag_alpha",
            "sync_backtest_cache", "resume_simulation_batch",
            "set_case", "future_mutation_not_in_whitelist",
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                        GatewayError, "trusted live admission"):
                    gateway.call(command, allow=True, authorization_id="a" * 32)
        self.assertIsNone(gateway.proc)

    def test_guarded_driver_prevents_raw_simulation_injection(self):
        class FakeRuntime:
            def dispatch(self, command, params):
                raise AssertionError("forbidden command reached platform")
            def close(self):
                pass
        fake_stdin = io.StringIO(
            '{"cmd":"simulate_super","allow":true,"authorization_id":"foo"}\n'
            '{"cmd":"future_mutation_not_in_whitelist","allow":true}\n'
            '{"cmd":"shutdown"}\n')
        out = io.StringIO()
        with (patch.object(driver.sys, "argv",
                           ["driver.py", "--orchestration-guarded"]),
              patch.object(driver.sys, "stdin", fake_stdin),
              patch.object(driver.sys, "stdout", out),
              patch.object(driver, "PlatformRuntime", return_value=FakeRuntime())):
            rc = driver.main()
        self.assertEqual(rc, 0)
        responses = [json.loads(row) for row in out.getvalue().splitlines()]
        self.assertFalse(responses[1]["ok"])
        self.assertIn("admission", responses[1]["error"])
        self.assertFalse(responses[2]["ok"])


    def test_p4_7_3_raw_driver_rejects_forged_predispatch_escrow(self):
        calls = []

        class FakeRuntime:
            def dispatch(self, command, params):
                calls.append(command)
                raise AssertionError("BRAIN mutation crossed guarded boundary")
            def close(self):
                pass

        requests = [
            {"cmd": "simulate_super", "attempt_id": "a" * 64,
             "grant_id": "b" * 32, "permission_to_dispatch": True,
             "authorization_id": "c" * 32},
            {"cmd": "resume_simulation_batch", "attempt_id": "a" * 64,
             "dispatch_authorized": True},
            {"cmd": "shutdown"},
        ]
        stream = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        output = io.StringIO()
        with (patch.object(driver.sys, "argv",
                           ["driver.py", "--orchestration-guarded"]),
              patch.object(driver.sys, "stdin", stream),
              patch.object(driver.sys, "stdout", output),
              patch.object(driver, "PlatformRuntime", return_value=FakeRuntime())):
            result = driver.main()
        self.assertEqual(result, 0)
        responses = [json.loads(row) for row in output.getvalue().splitlines()]
        self.assertFalse(responses[1]["ok"])
        self.assertFalse(responses[2]["ok"])
        self.assertIn("admission", responses[1]["error"])
        self.assertEqual(calls, [])

    def test_p4_7_3_guarded_gateway_rejects_grant_fields_on_reads(self):
        gateway = Gateway(orchestration_guarded=True)
        forged = (
            {"grant_id": "a" * 32},
            {"attempt_id": "b" * 64},
            {"permission_to_dispatch": True},
            {"dispatch_authorized": True},
            {"allow": True},
        )
        for params in forged:
            with self.subTest(params=params):
                with self.assertRaisesRegex(GatewayError, "untrusted execution claims"):
                    gateway.call("get_alpha_details", alpha_id="offline-alpha",
                                 **params)
        self.assertIsNone(gateway.proc)

    def test_p4_7_3_guarded_driver_rejects_grant_fields_on_reads(self):
        calls = []

        class FakeRuntime:
            def dispatch(self, command, params):
                calls.append(command)
                return {"ok": "offline"}
            def close(self):
                pass

        requests = [
            {"cmd": "get_alpha_details", "alpha_id": "offline-alpha",
             "grant_id": "a" * 32},
            {"cmd": "get_platform_setting_options",
             "permission_to_dispatch": True},
            {"cmd": "shutdown"},
        ]
        input_stream = io.StringIO(
            "".join(json.dumps(row) + "\\n" for row in requests))
        output = io.StringIO()
        with (patch.object(driver.sys, "argv",
                           ["driver.py", "--orchestration-guarded"]),
              patch.object(driver.sys, "stdin", input_stream),
              patch.object(driver.sys, "stdout", output),
              patch.object(driver, "PlatformRuntime", return_value=FakeRuntime())):
            self.assertEqual(driver.main(), 0)
        responses = [json.loads(row) for row in output.getvalue().splitlines()]
        self.assertFalse(responses[1]["ok"])
        self.assertFalse(responses[2]["ok"])
        self.assertIn("untrusted orchestration", responses[1]["error"])
        self.assertEqual(calls, [])

    def test_guarded_driver_redacts_provider_exception(self):
        class FakeRuntime:
            def dispatch(self, command, params):
                raise RuntimeError("sensitive-account-secret")
            def close(self):
                pass
        input_stream = io.StringIO(
            '{"cmd":"get_platform_setting_options"}\n'
            '{"cmd":"shutdown"}\n')
        output = io.StringIO()
        with (patch.object(driver.sys, "argv",
                           ["driver.py", "--orchestration-guarded"]),
              patch.object(driver.sys, "stdin", input_stream),
              patch.object(driver.sys, "stdout", output),
              patch.object(driver, "PlatformRuntime", return_value=FakeRuntime())):
            result = driver.main()
        self.assertEqual(result, 0)
        self.assertNotIn("sensitive-account-secret", output.getvalue())
        lines = [json.loads(x) for x in output.getvalue().splitlines()]
        self.assertFalse(lines[1]["ok"])

    def test_gateway_spawns_guarded_driver_flag(self):
        gateway = Gateway(orchestration_guarded=True)
        class Pipe:
            def write(self, *args):
                pass
            def flush(self):
                pass
        class Process:
            stdin = Pipe()
        def fake_popen(command, **kwargs):
            self.assertIn("--orchestration-guarded", command)
            return Process()
        with patch("alpha_research.integrations.brain.gateway.subprocess.Popen",
                   side_effect=fake_popen):
            gateway._read_response = lambda: {"ok": True, "stage": "ready"}
            gateway.start()


class GuardedDispatchAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.orchestrator = MainOrchestrator(self.root)
        self.task = self.orchestrator.create(
            "super", {"objective": "offline no-live guard",
                      "region": "USA", "delay": 1, "universe": "TOP3000"})
        self.policy = {"max_calls": 1, "max_reported_tokens": 1000,
                       "failover": "manual"}

    def test_audit_never_grants_execution(self):
        status = inspect_simulation_dispatch_gate(
            self.orchestrator, self.task["task_id"], self.policy)
        self.assertEqual(status["status"], "NOT_AUTHORIZED")
        self.assertFalse(status["permission_to_dispatch"])
        self.assertFalse(status["brain_simulation_authorized"])
        self.assertFalse(status["automatic_retry_allowed"])
        self.assertIn("TRUSTED_LIVE_GRANT_CONSUMER_NOT_IMPLEMENTED",
                      status["blockers"])
        self.assertFalse((self.root / "logs" / "model_budget").exists(),
                         "read-only audit must not create budget lock files")

    def test_stop_flag_blocks_dispatch_without_network(self):
        stop = self.root / "state" / "stop.flag"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("stop", encoding="utf-8")
        status = inspect_simulation_dispatch_gate(
            self.orchestrator, self.task["task_id"], self.policy)
        self.assertIn("GLOBAL_STOP_SIGNAL_PRESENT", status["blockers"])


    def test_global_stop_dangling_symlink_blocks_readonly_admission(self):
        from alpha_research.orchestration.admission_audit import (
            audit_execution_admission,
        )
        stop = self.root / "state" / "stop.flag"
        stop.parent.mkdir(parents=True, exist_ok=True)
        try:
            stop.symlink_to(stop.with_name("missing-target"))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable on this host")
        result = audit_execution_admission(
            self.orchestrator, self.task["task_id"], {},
            self.policy, config_source="offline_default")
        self.assertEqual(result["status"], "NOT_AUTHORIZED")
        self.assertIn("GLOBAL_STOP_SIGNAL_PRESENT", result["blockers"])
        self.assertFalse(result["brain_simulation_authorized"])
        self.assertFalse(result["provider_invoked"])
        self.assertFalse(result["brain_invoked"])


    def _bind_domain_research_for_audit(self):
        task = self.orchestrator.load(self.task["task_id"])
        previous = task["updated_at"]
        task["status"] = "RUNNING"
        task["steps"]["execute"] = "RUNNING"
        self.orchestrator._save(task, expected=previous)
        research_id = "super:" + "e" * 32
        self.orchestrator.bind_research_identity(
            self.task["task_id"], research_id)
        return research_id

    def test_p4_7_structured_research_binding_reads_real_model_budget(self):
        research_id = self._bind_domain_research_for_audit()
        with patch(
            "alpha_research.orchestration.simulation_dispatch_preflight.inspect_staged_simulation_attempt",
            return_value={"status": "STAGED_NO_DISPATCH",
                          "research_id": research_id},
        ):
            result = inspect_simulation_dispatch_gate(
                self.orchestrator, self.task["task_id"], self.policy)
        self.assertNotIn("RESEARCH_ID_NOT_BOUND", result["blockers"])
        self.assertNotIn("SIMULATION_RESEARCH_ID_MISMATCH", result["blockers"])
        self.assertEqual(result["facts"]["model_budget"]["attempts"], 0)
        self.assertIn("TASK_NOT_FRESH", result["blockers"])
        self.assertFalse(result["permission_to_dispatch"])

    def test_p4_7_mismatched_staging_research_id_refuses_dispatch(self):
        self._bind_domain_research_for_audit()
        with patch(
            "alpha_research.orchestration.simulation_dispatch_preflight.inspect_staged_simulation_attempt",
            return_value={"status": "STAGED_NO_DISPATCH",
                          "research_id": "super:" + "f" * 32},
        ):
            result = inspect_simulation_dispatch_gate(
                self.orchestrator, self.task["task_id"], self.policy)
        self.assertIn("SIMULATION_RESEARCH_ID_MISMATCH", result["blockers"])
        self.assertFalse(result["permission_to_dispatch"])

    def test_p4_7_unresolved_model_attempt_is_visible_and_blocks(self):
        from alpha_research.agents.governance import ModelCallLedger
        research_id = self._bind_domain_research_for_audit()
        ModelCallLedger(self.root, self.policy).reserve(
            research_id, "test-inflight-model-attempt",
            task="offline-audit", role="research_lead",
            backend="registry:deepseek", backend_sha256="a" * 64)
        with patch(
            "alpha_research.orchestration.simulation_dispatch_preflight.inspect_staged_simulation_attempt",
            return_value={"status": "STAGED_NO_DISPATCH",
                          "research_id": research_id},
        ):
            result = inspect_simulation_dispatch_gate(
                self.orchestrator, self.task["task_id"], self.policy)
        self.assertIn("MODEL_ATTEMPT_RECONCILIATION_REQUIRED", result["blockers"])
        self.assertIn("MODEL_BUDGET_BLOCKED", result["blockers"])
        self.assertEqual(result["facts"]["model_budget"]["unresolved"], 1)
        self.assertFalse(result["provider_invoked"])
        self.assertFalse(result["brain_invoked"])



    def test_p4_7_unknown_token_usage_is_not_treated_as_free(self):
        from alpha_research.agents.governance import ModelCallLedger
        research_id = self._bind_domain_research_for_audit()
        ledger = ModelCallLedger(self.root, self.policy)
        ledger.reserve(
            research_id, "completed-unknown-tokens",
            task="offline-audit", role="research_lead",
            backend="registry:deepseek", backend_sha256="a" * 64)
        ledger.settle(
            research_id, "completed-unknown-tokens", success=True,
            token_in=None, token_out=None)
        with patch(
            "alpha_research.orchestration.simulation_dispatch_preflight.inspect_staged_simulation_attempt",
            return_value={"status": "STAGED_NO_DISPATCH",
                          "research_id": research_id},
        ):
            result = inspect_simulation_dispatch_gate(
                self.orchestrator, self.task["task_id"], self.policy)
        self.assertIn("MODEL_USAGE_UNVERIFIED", result["blockers"])
        self.assertIn("MODEL_BUDGET_BLOCKED", result["blockers"])
        self.assertEqual(
            result["facts"]["model_budget"]["unknown_usage_attempts"], 1)
        self.assertFalse(result["permission_to_dispatch"])



if __name__ == "__main__":
    unittest.main()
