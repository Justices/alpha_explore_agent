"""Actual MainOrchestrator->real domain dispatcher bridge with zero network I/O.

The platform/LLM are scripted only at the boundary. No live tokens are read.
These tests do not claim P7 production-release authorization.
"""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alpha_research.config import DEFAULT_CONFIG
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.local_regular_pilot import (
    _SinglePostGateway, _assert_single_regular, LocalRegularPilotAdapter,
)
from alpha_research.orchestration.runtime_context import CliRuntimeContext
from alpha_research.state.store import atomic_write_json


class FakeGateway:
    is_offline_mock = False

    def __init__(self):
        self.calls = []
        self.alpha_lookup = None

    def call(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command == "simulate_batch":
            return {"status": "COMPLETE", "alpha_results": [{"alpha_id": "A1"}]}
        return {}


class FakeModel:
    is_offline_mock = False


class TestLocalRegularPilot(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.gateway = FakeGateway()
        self.model = FakeModel()
        self.agent = SimpleNamespace(model=self.model, gateway=self.gateway)
        self.cfg = deepcopy(DEFAULT_CONFIG)
        self.cfg["scope"]["region"] = "USA"
        self.cfg["scope"]["universe"] = "TOP3000"
        self.cfg["scope"]["delay"] = 1
        self.runtime = CliRuntimeContext(
            state_root=self.root.resolve(), config=self.cfg,
            gateway=self.gateway, model=self.model, agent=self.agent,
            execution_lock_path=self.root / "state" / "agent.lock")
        self.runner = MainOrchestrator(self.root, config=self.cfg)
        self.request = {
            "objective": "one real-batch path without fake post",
            "region": "USA", "dataset_ids": ["analyst10"],
            "delay": 1, "universe": "TOP3000",
            "max_batches": 1, "ppa_profile": False,
        }
        self.task = self.runner.create("regular", self.request)
        proposal_patch = patch(
            "alpha_research.orchestration.local_regular_pilot.inspect_execution_proposal",
            return_value={"proposal_sha256": "a" * 64})
        identity_patch = patch(
            "alpha_research.orchestration.local_regular_pilot.inspect_execution_identity",
            return_value={"identity_sha256": "b" * 64})
        model_patch = patch(
            "alpha_research.orchestration.local_regular_pilot.assert_live_model_routes")
        for p in (proposal_patch, identity_patch, model_patch):
            p.start()
            self.addCleanup(p.stop)

    def _make_adapter(self):
        adapter = LocalRegularPilotAdapter(
            self.runner, self.runtime, self.task["task_id"],
            model_config={"provider": "fake"}, governance_policy={"max_calls": 1})
        self.runner.adapter = adapter
        return adapter

    def _intent(self, context):
        context["bind_research_identity"]("regular:campaign:" + "a" * 32)
        target = self.root / "runs" / "case" / "batch_1"
        target.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target / "simulation_intent.json", {
            "status": "PREPARED", "workflow": "regular"})
        context["bind_recovery_target"]("regular_batch", target)
        return target

    def test_whole_task_dispatch_with_original_regular_domain_contract(self):
        adapter = self._make_adapter()
        called = []

        def domain(agent, gateway, *, execution_context=None, **kwargs):
            called.append((agent, gateway, kwargs))
            self._intent(execution_context)
            self.assertIs(agent.gateway, gateway)
            gateway.call("simulate_batch", label="production",
                         variants=[{"name": "a", "expression": "rank(close)"}])
            return {
                "status": "budget_complete", "completed": [
                    {"alpha_ids": ["A1"],
                     "candidate_status": {"A1": "EVIDENCE_PENDING"}}],
            }

        with patch("alpha_research.orchestration.dispatch.run_regular_campaign",
                   side_effect=domain):
            done = self.runner.execute(self.task["task_id"])
        self.assertEqual(done["status"], "COMPLETED")
        self.assertEqual(done["budget"], {"limit": 1, "used": 1, "reserved": 0})
        self.assertEqual(done["steps"],
                         {"validate": "DONE", "execute": "DONE",
                          "qualify": "DONE", "collect": "DONE"})
        self.assertEqual(done["checkpoints"]["execute"]["mode"],
                         "supervised_local_real_pilot")
        self.assertEqual(done["checkpoints"]["execute"]["simulation_commands_reserved"], 1)
        self.assertEqual(done["result"]["qualification"]["completed"][0][
            "candidate_status"]["A1"], "EVIDENCE_PENDING")
        self.assertFalse(done["result"]["production_release_authorized"])
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0][2]["max_batches"], 1)
        self.assertEqual(called[0][2]["dataset_ids"], ["analyst10"])
        self.assertIs(called[0][2]["execution_context"]["allow_diagnostic_simulations"], False)
        self.assertEqual([x[0] for x in self.gateway.calls], ["simulate_batch"])
        self.assertIs(self.agent.gateway, self.gateway)
        self.assertEqual(self.runner.resume(self.task["task_id"]), done)

    def test_unresolved_campaign_without_real_simulation_is_never_completed(self):
        self._make_adapter()
        def no_work(agent, gateway, *, execution_context=None, **kwargs):
            return {
                "status": "unresolved", "completed": [],
                "unresolved": [{"reason": "platform_fields_unavailable"}],
            }
        with patch("alpha_research.orchestration.dispatch.run_regular_campaign",
                   side_effect=no_work) as domain:
            blocked = self.runner.execute(self.task["task_id"])
        self.assertEqual(domain.call_count, 1)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["steps"]["execute"], "RUNNING")
        self.assertEqual(blocked["budget"], {
            "limit": 1, "reserved": 1, "used": 0})
        self.assertIsNone(blocked["result"])
        self.assertEqual(self.gateway.calls, [])
        with self.assertRaisesRegex(OrchestrationError, "explicit reconciliation"):
            self.runner.resume(self.task["task_id"])

    def test_one_simulation_without_intent_provenance_never_completes(self):
        self._make_adapter()
        def broken_domain(agent, gateway, *, execution_context=None, **kwargs):
            gateway.call("simulate_batch", label="production",
                         variants=[{"name": "a", "expression": "rank(close)"}])
            return {
                "status": "budget_complete", "completed": [
                    {"alpha_ids": ["A1"],
                     "candidate_status": {"A1": "EVIDENCE_PENDING"}}],
            }
        with patch("alpha_research.orchestration.dispatch.run_regular_campaign",
                   side_effect=broken_domain):
            blocked = self.runner.execute(self.task["task_id"])
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["budget"]["reserved"], 1)
        self.assertIsNone(blocked["recovery_target"])
        self.assertEqual(len(self.gateway.calls), 1)

    def test_unknown_post_blocks_and_never_replays(self):
        adapter = self._make_adapter()
        def uncertain(agent, gateway, *, execution_context=None, **kwargs):
            self._intent(execution_context)
            gateway.call("simulate_batch", label="production",
                         variants=[{"name": "a", "expression": "rank(close)"}])
            raise TimeoutError("synthetic uncertain provider ACK")
        with patch("alpha_research.orchestration.dispatch.run_regular_campaign",
                   side_effect=uncertain) as function:
            blocked = self.runner.execute(self.task["task_id"])
            with self.assertRaisesRegex(OrchestrationError, "explicit reconciliation"):
                self.runner.resume(self.task["task_id"])
        self.assertEqual(function.call_count, 1)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["steps"]["execute"], "RUNNING")
        self.assertEqual(blocked["budget"]["reserved"], 1)
        self.assertEqual(blocked["budget"]["used"], 0)
        self.assertEqual(adapter.gateway.simulation_commands_reserved, 1)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(blocked["recovery_target"]["kind"], "regular_batch")
        self.assertEqual(blocked["research_identity"]["workflow"], "regular")

    def test_second_simulation_rejected_before_transport(self):
        adapter = self._make_adapter()
        self._intent = lambda ctx: None
        def repeated(agent, gateway, **kwargs):
            for _ in range(2):
                gateway.call("simulate_batch", label="production",
                             variants=[{"name": "a", "expression": "rank(close)"}])
        with patch("alpha_research.orchestration.dispatch.run_regular_campaign",
                   side_effect=repeated):
            blocked = self.runner.execute(self.task["task_id"])
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(adapter.gateway.simulation_commands_reserved, 1)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_gateway_forbids_mutations_other_workflows_and_oversize_batches(self):
        bounded = _SinglePostGateway(self.gateway, self.root)
        for command in ("simulate_super", "submit_alpha", "tag_alpha",
                        "simulate_canary_once", "set_alpha_properties"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(OrchestrationError, "forbids"):
                    bounded.call(command)
        for variants in ([], [{}] * 9, {}, None):
            with self.assertRaisesRegex(OrchestrationError, "1-8"):
                bounded.call("simulate_batch", label="production",
                             variants=variants)
        self.assertEqual(self.gateway.calls, [])

    def test_pinned_task_scope_and_budget_are_required(self):
        for name, mutation in (
            ("unbounded datasets", {"dataset_ids": None}),
            ("multi datasets", {"dataset_ids": ["analyst10", "price"]}),
            ("missing universe", {"universe": None}),
            ("wrong delay", {"delay": 0}),
            ("unbounded batches", {"max_batches": 2}),
            ("malformed ppa", {"ppa_profile": "false"}),
            ("unexpected key", {"allow_simulation": True}),
        ):
            with self.subTest(name=name):
                changed = deepcopy(self.task)
                changed["request"] = {**self.request, **mutation}
                with self.assertRaises(OrchestrationError):
                    _assert_single_regular(changed)
        changed = deepcopy(self.task)
        changed["status"] = "RUNNING"
        with self.assertRaisesRegex(OrchestrationError, "fresh"):
            _assert_single_regular(changed)
        self.assertEqual(self.gateway.calls, [])

    def test_existing_campaign_or_mock_marker_refuses_before_dispatch(self):
        from alpha_research.workflows.regular.campaign import _key
        campaign = {**self.request, "category": None,
                    "batch_budget": "per_dataset"}
        id_ = _key(campaign)
        dest = self.root / "state" / "research_v1_1" / "campaign_runs" / (id_ + ".json")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "already exists"):
            self._make_adapter()
        dest.unlink()
        from alpha_research.orchestration.mock_workspace import marker_path
        marker = marker_path(self.root)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "mock workspace"):
            self._make_adapter()

    def test_cli_denies_without_explicit_both_flags_before_any_live_io(self):
        from alpha_research.cli import main
        with patch("alpha_research.cli.Gateway") as real_gateway, patch(
                "alpha_research.cli.build_model_client") as real_model:
            result = main([
                "--state-root", str(self.root),
                "orchestration-local-pilot-regular", self.task["task_id"]])
        self.assertEqual(result, 2)
        real_gateway.assert_not_called()
        real_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
