"""Core production wiring test: exact Task -> real dispatcher -> model/Brain boundary.

All external calls are intercepted; these tests do NOT issue BRAIN Simulation,
model calls or genuine independent grants. The P7 production release is still
blocked until an independently trusted authority consumer is integrated.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alpha_research.config import DEFAULT_CONFIG
from alpha_research.integrations.brain.gateway import Gateway
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.live_domain_bridge import (
    AdmittedCliDomainAdapter, BoundedSimulationGateway,
)
from alpha_research.orchestration.runtime_context import CliRuntimeContext
from alpha_research.state.writer_lock import SingleWriterLock


class Consumer:
    def __init__(self, *, allowed=True, max_simulations=1):
        self.allowed = allowed
        self.max_simulations = max_simulations
        self.calls = []

    def consume(self, descriptor):
        self.calls.append(dict(descriptor))
        if not self.allowed:
            raise OrchestrationError("external authority denied")
        return {
            "status": "CONSUMED_SINGLE_USE",
            "task_id": descriptor["task_id"],
            "request_sha256": descriptor["request_sha256"],
            "operation": "regular_research_with_simulation",
            "external_consumption_id": "trusted-test-ACK-01",
            "max_simulation_requests": self.max_simulations,
        }


REQUEST = {
    "objective": "fixed one-batch live boundary rehearsal",
    "region": "USA", "delay": 1, "universe": "TOP3000",
    "category": "analyst", "dataset_ids": ["analyst10"],
    "max_batches": 1,
}


class LiveDomainBridgeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name).resolve()
        self.gateway = Gateway()
        self.gateway.call = Mock(return_value={"alpha_id": "OFFLINE_ONLY"})
        self.model = SimpleNamespace()
        self.agent = SimpleNamespace(model=self.model, gateway=self.gateway)
        self.cfg = dict(DEFAULT_CONFIG)
        self.lock = SingleWriterLock(self.root / "state" / "agent.lock")

    def _run(self, *, consumer=None, request=None, domain=None):
        consumer = consumer or Consumer()
        with self.lock:
            runtime = CliRuntimeContext.from_active_cli(
                self.root, self.cfg, self.gateway, self.model,
                self.agent, self.lock)
            runner = MainOrchestrator(self.root, config=self.cfg)
            runner.adapter = AdmittedCliDomainAdapter(
                runner, runtime, authority_consumer=consumer)
            task = runner.create("regular", request or REQUEST)
            if domain is None:
                def domain(agent, gateway, **kwargs):
                    self.assertIs(gateway, agent.gateway)
                    context = kwargs["execution_context"]
                    context["bind_research_identity"](
                        "regular:campaign:" + "a" * 32)
                    target = self.root / "runs" / "one-batch"
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "simulation_intent.json").write_text(
                        json.dumps({"status": "PREPARED"}), encoding="utf-8")
                    context["bind_recovery_target"]("regular_batch", target)
                    gateway.call("simulate_batch", settings={"delay": 1})
                    return {
                        "status": "budget_complete", "failures": [],
                        "completed": [{
                            "alpha_ids": ["OFFLINE_ONLY"],
                            "candidate_status": {"OFFLINE_ONLY": "EVIDENCE_PENDING"},
                        }],
                    }
            with patch(
                    "alpha_research.orchestration.dispatch.run_regular_campaign",
                    side_effect=domain) as mocked:
                result = runner.execute(task["task_id"])
            return runner, task, result, consumer, mocked.call_count

    def test_complete_real_dispatch_wiring_persists_task_and_exact_recovery(self):
        runner, task, completed, consumer, calls = self._run()
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(completed["budget"], {
            "limit": 1, "used": 1, "reserved": 0})
        self.assertEqual(completed["research_identity"]["research_id"],
                         "regular:campaign:" + "a" * 32)
        self.assertEqual(completed["recovery_target"]["kind"], "regular_batch")
        self.assertEqual(completed["checkpoints"]["execute"][
            "simulation_requests_attempted"], 1)
        self.assertEqual(completed["checkpoints"]["execute"][
            "independent_consumption_id"], "trusted-test-ACK-01")
        self.assertEqual(completed["result"]["qualification"]["completed"][0][
            "candidate_status"]["OFFLINE_ONLY"], "EVIDENCE_PENDING")
        self.assertEqual(calls, 1)
        self.assertEqual(len(consumer.calls), 1)
        self.assertEqual(consumer.calls[0]["scope"]["dataset_id"], "analyst10")
        self.assertEqual(runner.resume(task["task_id"]), completed)
        self.assertEqual(self.gateway.call.call_count, 1)
        self.assertIs(self.agent.gateway, self.gateway)

    def test_refused_external_consumer_never_calls_model_or_brain(self):
        _, _, result, consumer, calls = self._run(consumer=Consumer(allowed=False))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["steps"]["execute"], "RUNNING")
        self.assertEqual(result["budget"]["reserved"], 1)
        self.assertEqual(calls, 0)
        self.assertEqual(len(consumer.calls), 1)
        self.gateway.call.assert_not_called()

    def test_invalid_scope_fails_in_validate_without_consumer(self):
        for bad in (
                {**REQUEST, "dataset_ids": ["analyst10", "analyst11"]},
                {**REQUEST, "delay": 0},
                {**REQUEST, "region": None},
                {**REQUEST, "category": None},
                {**REQUEST, "universe": None}):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as td:
                    self.root = Path(td).resolve()
                    self.lock = SingleWriterLock(self.root / "state" / "agent.lock")
                    _, _, result, consumer, calls = self._run(
                        request=bad)
                    self.assertEqual(result["status"], "BLOCKED")
                    self.assertEqual(result["steps"]["validate"], "RUNNING")
                    self.assertEqual(result["budget"]["reserved"], 0)
                    self.assertEqual(consumer.calls, [])
                    self.assertEqual(calls, 0)
        self.gateway.call.assert_not_called()

    def test_simulation_cap_enforced_before_second_platform_request(self):
        def domain(agent, gateway, **kwargs):
            gateway.call("simulate_batch")
            gateway.call("simulate_batch")
        runner, task, result, consumer, calls = self._run(domain=domain)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["budget"]["reserved"], 1)
        self.assertEqual(self.gateway.call.call_count, 1)
        self.assertEqual(calls, 1)
        with self.assertRaisesRegex(OrchestrationError, "explicit reconciliation"):
            runner.resume(task["task_id"])

    def test_gateway_disallows_tag_submission_and_unbounded_requests(self):
        bounded = BoundedSimulationGateway(
            self.gateway, max_simulation_requests=1)
        for command in ("tag_alpha", "submit_alpha",
                        "sync_backtest_cache"):
            with self.assertRaisesRegex(OrchestrationError, "forbids"):
                bounded.call(command)
        bounded.call("simulate_batch")
        with self.assertRaisesRegex(OrchestrationError, "exhausted"):
            bounded.call("simulate_batch")
        self.assertEqual(self.gateway.call.call_count, 1)

    def test_bridge_requires_active_cli_runtime_and_external_consumer(self):
        runner = MainOrchestrator(self.root, config=self.cfg)
        with self.lock:
            runtime = CliRuntimeContext.from_active_cli(
                self.root, self.cfg, self.gateway, self.model,
                self.agent, self.lock)
            with self.assertRaisesRegex(OrchestrationError, "independent authority"):
                AdmittedCliDomainAdapter(runner, runtime,
                                         authority_consumer=None)
            self.gateway.orchestration_guarded = True
            with self.assertRaisesRegex(OrchestrationError, "independent authority"):
                AdmittedCliDomainAdapter(runner, runtime,
                                         authority_consumer=Consumer())
            self.gateway.orchestration_guarded = False

    def test_unknown_domain_outcome_does_not_reissue_simulation(self):
        def interrupted(agent, gateway, **kwargs):
            gateway.call("simulate_batch")
            raise ConnectionError("lost readback")
        runner, task, blocked, consumer, calls = self._run(domain=interrupted)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["budget"]["used"], 0)
        self.assertEqual(blocked["budget"]["reserved"], 1)
        self.assertEqual(calls, 1)
        self.assertEqual(len(consumer.calls), 1)
        with self.assertRaises(OrchestrationError):
            runner.resume(task["task_id"])
        self.assertEqual(self.gateway.call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
