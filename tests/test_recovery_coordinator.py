"""Offline tests for the unified fail-closed recovery coordinator."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_research.orchestration.recovery import RecoveryCoordinator, RecoveryError
from alpha_research.orchestration.engine import MainOrchestrator, OfflineResearchAdapter
from alpha_research.integrations.brain.gateway import GatewayError
from alpha_research.workflows.regular.campaign import (
    _key, _persist_record, _reconcile_active, run_regular_campaign,
)
from alpha_research.state.records import ResearchRecordStore
from alpha_research.state.store import atomic_write_json
from alpha_research.simulation.intent import (
    create_simulation_intent, transition_simulation_intent,
)


class FakeAgent:
    def __init__(self, result=None, error=None):
        self.result = result or {"status": "candidate_evaluated", "alpha_ids": ["A1"]}
        self.error = error
        self.calls = 0

    def recover(self, batch_dir):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class FakeGateway:
    def __init__(self):
        self.calls = []

    def call(self, command, **kwargs):
        self.calls.append(command)
        raise AssertionError("coordinator test must not call a live gateway")


class RecoveryCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gateway = FakeGateway()

    def test_retry_retires_only_a_provably_unsubmitted_draft(self):
        agent = Mock(runs_root=self.root / "runs")
        batch = agent.runs_root / "HKG" / "D1" / "TOP500" / "analyst4" / "batch"
        batch.mkdir(parents=True)
        atomic_write_json(batch / "batch_context.json", {
            "objective": "test", "started_at": "2026-11-10T01:00:01+00:00",
            "research_id": "regular:campaign:case"})
        state = {
            "campaign_id": "case", "request": {"objective": "test"},
            "active_item": 0, "active_started_at": "2026-11-10T01:00:00+00:00",
            "items": [{"region": "HKG", "delay": 1, "universe": "TOP500", "dataset_id": "analyst4"}],
            "status": "unresolved", "error": "model service timed out during expression planning",
            "budget": {"reserved": {"batches": 1}}}
        intent = batch / "probe_1_intent.json"
        atomic_write_json(batch / "probe_plan.json", {"core_fields": ["field_a"], "probes": []})
        atomic_write_json(intent, {"status": "SUBMITTING"})
        with self.assertRaisesRegex(ValueError, "requires recovery"):
            _reconcile_active(agent, state)
        agent.void_batch.assert_not_called()
        intent.unlink()
        _reconcile_active(agent, state)
        self.assertEqual(agent.void_batch.call_args.args[0], batch)
        self.assertEqual(state["status"], "planned")
        self.assertEqual(state["budget"]["reserved"]["batches"], 0)

    def test_unspecified_campaign_attempts_all_d1_datasets_with_budget_one_each(self):
        agent = Mock(runs_root=self.root / "runs", stop_flag=None)
        agent.run_once.return_value = {"batch_dir": "batch", "alpha_ids": ["A1"],
                                       "candidate_status": {"A1": "EVIDENCE_PENDING"}}
        ids = [f"model{number}" for number in range(101, 106)]
        gateway = Mock()

        def call(command, **kwargs):
            if command == "get_platform_setting_options":
                return {"instrument_options": [
                    {"InstrumentType": "EQUITY", "Region": "HKG", "Delay": delay,
                     "Universe": ["TOP500", "TOP800"]} for delay in (0, 1)]}
            if command == "get_datasets":
                self.assertEqual(kwargs["delay"], 1)
                return [{"id": name, "category": {"id": "analyst", "name": "Analyst Estimates"}, **kwargs}
                        for name in reversed(ids)]
            return {}

        gateway.call.side_effect = call
        result = run_regular_campaign(
            agent, gateway, region="HKG", category="Analyst",
            objective="all eligible datasets", max_batches=1)
        self.assertEqual([call.kwargs["dataset_id"] for call in agent.run_once.call_args_list],
                         ids)
        self.assertEqual(result["budget"]["limits"]["batches"], 5)
        self.assertEqual(result["batches_completed"], 5)
        self.assertEqual(sum(call.args[0] == "get_platform_setting_options"
                             for call in gateway.call.call_args_list), 1)
        self.assertTrue(all(call.kwargs["dataset_scope_proof"]["delay"] == 1
                            for call in agent.run_once.call_args_list))
        agent._decide.assert_not_called()
        paths = [call.kwargs["field_pool_path"] for call in agent.run_once.call_args_list]
        self.assertEqual(len(set(paths)), 5)
        with self.assertRaisesRegex(ValueError, "Delay 1 only"):
            run_regular_campaign(agent, gateway, region="HKG", delay=0,
                                 objective="unsupported delay", max_batches=1)
        agent.run_once.reset_mock()
        result = run_regular_campaign(
            agent, gateway, region="HKG", category="Analyst",
            objective="two rounds across eligible datasets", max_batches=2)
        self.assertEqual([call.kwargs["dataset_id"] for call in agent.run_once.call_args_list], ids * 2)
        self.assertEqual([call.kwargs["fixed_scope"]["universe"] for call in agent.run_once.call_args_list],
                         ["TOP500"] * 5 + ["TOP800"] * 5)
        self.assertEqual(result["budget"]["used"]["batches"], 10)

    def test_campaign_records_failed_attempt_and_continues_to_next_dataset(self):
        agent = Mock(runs_root=self.root / "runs", stop_flag=None)
        result = {"batch_dir": "batch", "alpha_ids": ["A1"],
                  "candidate_status": {"A1": "EVIDENCE_PENDING"}}
        agent.run_once.side_effect = [RuntimeError("model response lost"), result]
        combo = {"region": "GLB", "delay": 1, "universe": "TOP3000"}
        plan = {"unresolved": [], "datasets": [
            {"dataset_id": name, "category": "analyst", "combinations": [combo]}
            for name in ("analyst69", "analyst70")]}
        request = {"objective": "audit child state", "region": "GLB",
                   "dataset_ids": ["analyst69", "analyst70"], "max_batches": 1}
        store = ResearchRecordStore(self.root)
        with patch("alpha_research.workflows.regular.campaign.discover_regular_scope",
                   return_value=plan):
            gateway = Mock()
            outcome = run_regular_campaign(agent, gateway, **request)

            campaign, = store.list("campaign")
            blocked, untouched = sorted(store.list("dataset_subtask"),
                                        key=lambda row: row["dataset_id"])
            blocked_combo = store.get("combination", blocked["combination_ids"][0])
            self.assertEqual(blocked["status"], "unresolved")
            self.assertEqual(blocked_combo["status"], "unresolved")
            self.assertEqual(blocked_combo["failure_history"][0]["reason"],
                             "model response lost")
            self.assertEqual(untouched["status"], "covered")
            self.assertEqual(campaign["budget"]["reserved"]["batches"], 0)
            self.assertEqual(campaign["coverage"]["unexamined_combinations"], 1)
            self.assertEqual([call.kwargs["dataset_id"] for call in agent.run_once.call_args_list],
                             ["analyst69", "analyst70"])

            resumed = run_regular_campaign(agent, gateway, **request)
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(resumed["status"], "partial")
        self.assertEqual(resumed["budget"]["used"]["batches"], 2)
        self.assertEqual(resumed["batches_completed"], 1)
        self.assertEqual(agent.run_once.call_count, 2)

    def test_unavailable_catalog_scope_does_not_block_other_verified_combinations(self):
        agent = Mock(runs_root=self.root / "runs", stop_flag=None)
        agent.run_once.return_value = {"batch_dir": "batch", "alpha_ids": ["A1"],
                                       "candidate_status": {"A1": "EVIDENCE_PENDING"}}
        gateway = Mock()

        def call(command, **kwargs):
            if command == "get_platform_setting_options":
                return {"instrument_options": [{"InstrumentType": "EQUITY",
                    "Region": "HKG", "Delay": 1, "Universe": ["TOP500", "TOP800"]}]}
            if command == "get_datasets":
                if kwargs["universe"] == "TOP500":
                    raise GatewayError("catalog request failed")
                return [{"id": "model101", **kwargs}]
            return {}

        gateway.call.side_effect = call
        outcome = run_regular_campaign(agent, gateway, objective="partial scope", region="HKG",
                                       max_batches=1)
        self.assertEqual(agent.run_once.call_args.kwargs["fixed_scope"]["universe"], "TOP800")
        self.assertEqual(outcome["batches_completed"], 1)
        self.assertEqual(outcome["unresolved"][0]["scope"]["universe"], "TOP500")
        self.assertEqual(outcome["unresolved"][0]["reason"], "dataset_catalog_unavailable")

    def test_campaign_keeps_running_with_bad_batch_receipt_and_failed_projection(self):
        agent = Mock(runs_root=self.root / "runs", stop_flag=None)
        agent.run_once.side_effect = [
            {},
            {"batch_dir": "second-batch", "alpha_ids": ["A2"],
             "candidate_status": {"A2": "EVIDENCE_PENDING"}},
        ]
        combo = {"region": "GLB", "delay": 1, "universe": "TOP3000"}
        plan = {"unresolved": [], "datasets": [
            {"dataset_id": name, "category": "analyst", "combinations": [combo]}
            for name in ("analyst69", "analyst70")]}

        def persist(store, kind, record):
            if kind == "combination" and record["effective_settings"]["dataset_id"] == "analyst69":
                raise OSError("derived record unavailable")
            return _persist_record(store, kind, record)

        with (
            patch("alpha_research.workflows.regular.campaign.discover_regular_scope", return_value=plan),
            patch("alpha_research.workflows.regular.campaign._persist_record", side_effect=persist),
        ):
            result = run_regular_campaign(agent, Mock(), objective="isolation", region="GLB", max_batches=1)
        saved = json.loads(Path(result["state_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "partial")
        self.assertEqual([row["dataset_id"] for row in saved["completed"]], ["analyst70"])
        self.assertEqual(len(saved["failures"]), 1)
        self.assertEqual(saved["budget"]["used"]["batches"], 2)

    def _regular_target(self, status="SUBMITTING"):
        batch = self.root / "runs" / "batch-001"
        batch.mkdir(parents=True)
        (batch / "prereg.json").write_text('{"variants":[]}', encoding="utf-8")
        (batch / "simulation_intent.json").write_text(json.dumps({
            "status": status,
            "expressions": ["rank(x)"],
            "settings": {"region": "GLB"},
            "runner": {"case_key": "case", "run_id": "run",
                       "batch_id": "batch-001", "label": "production"},
            "prereg_sha256": "a" * 64,
        }), encoding="utf-8")
        if status == "READ_BACK":
            (batch / "results.json").write_text('{"alpha_ids":["A1"]}', encoding="utf-8")
        return batch

    def _simulation_intent(self, workflow="super", status="POSTING"):
        path = self.root / "state" / "research_v1_1" / workflow / "intent.json"
        path.parent.mkdir(parents=True)
        create_simulation_intent(
            path, workflow=workflow, run_id="run-1", batch_id="batch-1",
            request=({"selection": "rank(x)", "combo": "rank(y)"}
                     if workflow == "super" else {"regular": "rank(x)"}),
            settings={"region": "GLB", "delay": 1},
        )
        if status == "POSTING":
            transition_simulation_intent(
                path, expected_status="PREPARED", status="POSTING")
        return path

    def test_regular_recovery_is_journaled_and_idempotent(self):
        batch = self._regular_target()
        agent = FakeAgent()
        coordinator = RecoveryCoordinator(self.root)
        first = coordinator.recover(batch, gateway=self.gateway, agent=agent)
        second = coordinator.recover(batch, gateway=self.gateway, agent=agent)
        self.assertEqual(first, second)
        self.assertEqual(agent.calls, 1)
        journal = coordinator.journal_for(batch)
        self.assertEqual(journal["status"], "COMPLETED")
        self.assertEqual(journal["workflow"], "regular")
        self.assertEqual(self.gateway.calls, [])

    def test_regular_prepared_intent_is_never_submitted_by_recovery(self):
        batch = self._regular_target(status="PREPARED")
        agent = FakeAgent()
        with self.assertRaisesRegex(RecoveryError, "PREPARED"):
            RecoveryCoordinator(self.root).recover(
                batch, gateway=self.gateway, agent=agent)
        self.assertEqual(agent.calls, 0)

    def test_read_back_regular_requires_saved_results(self):
        batch = self._regular_target(status="READ_BACK")
        (batch / "results.json").unlink()
        with self.assertRaisesRegex(RecoveryError, "no saved results"):
            RecoveryCoordinator(self.root).recover(
                batch, gateway=self.gateway, agent=FakeAgent())

    def test_super_and_optimization_use_existing_intent_handler(self):
        for workflow in ("super", "optimization"):
            with self.subTest(workflow=workflow):
                path = self._simulation_intent(workflow)
                calls = []

                def recover_fn(gateway, state_root, intent_path, config=None):
                    calls.append((gateway, state_root, intent_path, config))
                    return {"status": "candidate_evaluated", "workflow": workflow}

                coordinator = RecoveryCoordinator(
                    self.root, simulation_recover=recover_fn)
                result = coordinator.recover(
                    path, gateway=self.gateway, config={"offline": True})
                self.assertEqual(result["status"], "candidate_evaluated")
                self.assertEqual(len(calls), 1)
                self.assertEqual(coordinator.journal_for(path)["status"], "COMPLETED")
                self.assertEqual(self.gateway.calls, [])

    def test_super_prepared_intent_is_rejected_without_handler_call(self):
        path = self._simulation_intent(status="PREPARED")
        calls = []

        def recover_fn(*args, **kwargs):
            calls.append(True)
            return {"status": "unexpected"}

        with self.assertRaisesRegex(RecoveryError, "PREPARED"):
            RecoveryCoordinator(
                self.root, simulation_recover=recover_fn).recover(
                    path, gateway=self.gateway)
        self.assertEqual(calls, [])

    def test_failed_recovery_is_blocked_and_never_replayed(self):
        batch = self._regular_target()
        agent = FakeAgent(error=RuntimeError("injected recovery failure"))
        coordinator = RecoveryCoordinator(self.root)
        with self.assertRaisesRegex(RecoveryError, "blocked"):
            coordinator.recover(batch, gateway=self.gateway, agent=agent)
        with self.assertRaisesRegex(RecoveryError, "replay"):
            coordinator.recover(batch, gateway=self.gateway, agent=agent)
        self.assertEqual(agent.calls, 1)
        self.assertEqual(coordinator.journal_for(batch)["status"], "BLOCKED")

    def test_target_outside_state_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside_root:
            outside = Path(outside_root)
            with self.assertRaisesRegex(RecoveryError, "outside"):
                RecoveryCoordinator(self.root).recover(
                    outside, gateway=self.gateway, agent=FakeAgent())

    def test_task_recovery_reconciles_only_with_coordinator_receipt(self):
        for workflow in ("super", "optimization"):
            with self.subTest(workflow=workflow):
                path = self._simulation_intent(workflow=workflow, status="POSTING")
                qualification = {
                    "candidate_id": "candidate-1", "candidate_status": "EVIDENCE_PENDING",
                    "preferred_status": "NOT_CANDIDATE", "candidate_ref": "candidate.json",
                    "preferred_ref": "preferred.json",
                }
                fixtures = {
                    workflow + ".validate": {"validated": True},
                    workflow + ".execute": {"unused": True},
                    workflow + ".qualify": qualification,
                    workflow + ".collect": {"collected": True},
                }

                class BindAndFailAdapter(OfflineResearchAdapter):
                    def run(self, current_workflow, step, request, checkpoints, context=None):
                        if step == "execute":
                            context["bind_recovery_target"]("simulation_intent", path)
                            raise RuntimeError("lost response")
                        return super().run(
                            current_workflow, step, request, checkpoints, context=context)

                runner = MainOrchestrator(
                    self.root, adapter=BindAndFailAdapter(fixtures))
                request = {"objective": "recovery task"}
                if workflow == "optimization":
                    request["alpha_id"] = "SOURCE"
                task = runner.create(workflow, request)
                blocked = runner.execute(task["task_id"])
                self.assertEqual(blocked["status"], "BLOCKED")
                self.assertEqual(blocked["recovery_target"]["ref"], str(path.resolve()))

                calls = []
                def recover_fn(gateway, state_root, intent_path, config=None):
                    calls.append(str(intent_path))
                    return {
                        "candidate_id": "candidate-1", "candidate_status": "EVIDENCE_PENDING",
                        "preferred_status": "NOT_CANDIDATE", "candidate_ref": "candidate.json",
                        "preferred_ref": "preferred.json",
                    }

                coordinator = RecoveryCoordinator(self.root, simulation_recover=recover_fn)
                reconciled = coordinator.recover_task(
                    runner, task["task_id"], gateway=self.gateway, config={"offline": True},
                    expected_workflow=workflow)
                self.assertEqual(reconciled["steps"]["execute"], "DONE")
                self.assertEqual(reconciled["budget"], {"limit": 1, "used": 1, "reserved": 0})
                self.assertEqual(len(calls), 1)
                completed = runner.execute(task["task_id"])
                self.assertEqual(completed["status"], "COMPLETED")
                self.assertEqual(len(calls), 1)

    def test_regular_task_recovery_reconciles_campaign_without_starting_another_batch(self):
        request = {
            "objective": "regular recovery task", "region": "GLB",
            "dataset_ids": ["analyst69"], "category": None, "delay": 1,
            "universe": "TOP3000", "max_batches": 1, "ppa_profile": False,
        }
        campaign_id = _key(request)
        runs_root = self.root / "runs"
        batch = (runs_root / "GLB" / "D1" / "TOP3000" / "analyst69" / "batch-regular")
        batch.mkdir(parents=True)
        active_at = datetime.now(timezone.utc)
        started_at = active_at + timedelta(seconds=1)
        atomic_write_json(batch / "batch_context.json", {
            "objective": request["objective"], "started_at": started_at.isoformat(),
        })
        # An unrelated batch can have the same scope, objective and time range.
        atomic_write_json(batch.with_name("other-batch") / "batch_context.json", {
            "objective": request["objective"], "started_at": started_at.isoformat(),
        })
        atomic_write_json(batch / "prereg.json", {"variants": [{"expression": "rank(x)"}]})
        atomic_write_json(batch / "simulation_intent.json", {
            "status": "SUBMITTING", "expressions": ["rank(x)"],
            "settings": {"region": "GLB", "delay": 1, "universe": "TOP3000"},
            "runner": {"case_key": "case", "run_id": "run",
                       "batch_id": "batch-regular", "label": "production"},
            "prereg_sha256": "a" * 64,
        })
        plan_path = self.root / "state" / "research_v1_1" / "campaign_runs" / f"{campaign_id}_scope_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan = {"unresolved": [], "unselected_dataset_ids": []}
        atomic_write_json(plan_path, plan)
        state_path = plan_path.with_name(f"{campaign_id}.json")
        atomic_write_json(state_path, {
            "schema_version": 1, "campaign_id": campaign_id,
            "plan_evidence_ref": str(plan_path), "request": request, "plan": plan,
            "items": [{"dataset_id": "analyst69", "category": None,
                       "region": "GLB", "delay": 1, "universe": "TOP3000"}],
            "completed": [], "active_item": 0,
            "active_started_at": active_at.isoformat(),
            "budget": {"limits": {"batches": 1}, "reserved": {"batches": 1},
                       "used": {"batches": 0}},
            "status": "unresolved",
        })

        class RegularAgent:
            def __init__(self):
                self.runs_root = runs_root
                self.stop_flag = None
                self.calls = 0

            def recover(self, batch_dir):
                self.calls += 1
                intent_path = Path(batch_dir) / "simulation_intent.json"
                intent = json.loads(intent_path.read_text(encoding="utf-8"))
                intent["status"] = "READ_BACK"
                atomic_write_json(intent_path, intent)
                result = {"alpha_ids": ["A1"],
                          "candidate_status": {"A1": "EVIDENCE_PENDING"}}
                atomic_write_json(Path(batch_dir) / "results.json", result)
                return {"dataset_id": "analyst69", **result}

        fixtures = {
            "regular.validate": {"validated": True},
            "regular.execute": {"unused": True},
            "regular.qualify": {"status": "EVIDENCE_PENDING"},
            "regular.collect": {"collected": True},
        }

        class BindAndFailAdapter(OfflineResearchAdapter):
            def run(self, workflow, step, task_request, checkpoints, context=None):
                if step == "execute":
                    context["bind_recovery_target"]("regular_batch", batch)
                    raise RuntimeError("lost Regular executor response")
                return super().run(workflow, step, task_request, checkpoints, context=context)

        runner = MainOrchestrator(self.root, adapter=BindAndFailAdapter(fixtures))
        task = runner.create("regular", request)
        self.assertEqual(runner.execute(task["task_id"])["status"], "BLOCKED")
        agent = RegularAgent()
        coordinator = RecoveryCoordinator(self.root)
        reconciled = coordinator.recover_task(
            runner, task["task_id"], gateway=self.gateway, agent=agent)
        self.assertEqual(reconciled["checkpoints"]["execute"]["status"], "budget_complete")
        self.assertEqual(reconciled["checkpoints"]["execute"]["completed"][0]["batch_dir"],
                         str(batch))
        with self.assertRaisesRegex(ValueError, "missing from Campaign receipts"):
            run_regular_campaign(
                agent, self.gateway, **request, recovery_batch=batch.with_name("other-batch"))
        self.assertEqual(agent.calls, 1)
        self.assertEqual(self.gateway.calls, [])
        completed = runner.execute(task["task_id"])
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(agent.calls, 1)

    def test_immutable_target_change_blocks_recovery_replay(self):
        batch = self._regular_target()
        coordinator = RecoveryCoordinator(self.root)
        agent = FakeAgent()
        coordinator.recover(batch, gateway=self.gateway, agent=agent)
        intent_path = batch / "simulation_intent.json"
        saved = json.loads(intent_path.read_text(encoding="utf-8"))
        saved["settings"]["region"] = "USA"
        intent_path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(RecoveryError, "evidence changed"):
            coordinator.recover(batch, gateway=self.gateway, agent=agent)
        self.assertEqual(agent.calls, 1)


if __name__ == "__main__":
    unittest.main()
