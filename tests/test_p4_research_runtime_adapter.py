"""P4.18 offline-only research runtime ports and recovery safety.

These tests use genuine Super/Optimization preparation entrypoints with
strictly scripted transports. They never instantiate the platform driver or
a paid model SDK. Model-attempt uncertainty cannot be replayed.
"""
import json
import tempfile
import unittest
from pathlib import Path

from alpha_research.orchestration.domain_mock import (
    DomainMockGateway, DomainMockModel, DomainMockRecorder,
)
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.mock_workspace import claim_mock_workspace
from alpha_research.orchestration.research_runtime_adapter import (
    OfflineResearchRuntimeAdapter, inspect_runtime_attempt,
)
from alpha_research.orchestration.research_preparation import (
    prepare_offline_research, inspect_research_preparation,
)
from alpha_research.state.sealed import write_sealed_json


class OfflineResearchRuntimeAdapterTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.runner = MainOrchestrator(self.root)
        self.task = self.runner.create(
            "super", {"objective": "offline runtime boundary",
                      "region": "USA", "delay": 1, "universe": "TOP3000"})
        self.task_id = self.task["task_id"]
        self.gateway = DomainMockGateway("USA")
        self.model = DomainMockModel()
        self.policy = {"max_calls": 1, "max_reported_tokens": 4096,
                       "failover": "manual"}

    def adapter(self):
        return OfflineResearchRuntimeAdapter(
            self.runner, self.task_id, self.gateway,
            self.model, self.policy)

    def test_requires_marked_mock_and_exact_scripted_transports(self):
        with self.assertRaisesRegex(OrchestrationError, "claimed mock workspace"):
            self.adapter()
        claim_mock_workspace(self.root)
        from alpha_research.integrations.brain.gateway import Gateway
        with self.assertRaisesRegex(OrchestrationError, "real Gateway"):
            OfflineResearchRuntimeAdapter(
                self.runner, self.task_id, Gateway(),
                self.model, self.policy)
        with self.assertRaisesRegex(OrchestrationError, "real model"):
            OfflineResearchRuntimeAdapter(
                self.runner, self.task_id, self.gateway,
                object(), self.policy)
        self.assertEqual(
            inspect_runtime_attempt(self.runner, self.task_id)["status"],
            "NO_MODEL_ATTEMPT")

    def test_client_boundary_blocks_writes_and_unscoped_catalog(self):
        claim_mock_workspace(self.root)
        with self.adapter() as runtime:
            run = "e" * 32
            self.assertEqual(runtime.gateway.call(
                "set_case", case_key=run, run_id=run), {})
            self.assertEqual(runtime.gateway.local_case_id, run)
            # Local set_case never crosses the mocked transport.
            self.assertNotIn("set_case", self.gateway.calls)
            self.assertEqual(len(runtime.gateway.call(
                "get_operators")), 1)
            self.assertEqual(len(runtime.gateway.call(
                "get_user_alphas", stage="OS", limit=100, offset=0)["results"]), 1)
            self.assertEqual(len(runtime.gateway.call(
                "get_datasets", region="USA", delay=1, universe="TOP3000",
                instrument_type="EQUITY")), 1)
            bad = [
                ("simulate_super", {}),
                ("simulate_batch", {"allow": False}),
                ("tag_alpha", {"alpha_id": "x"}),
                ("get_alpha_details", {"alpha_id": "anything"}),
                ("get_user_alphas", {"stage": "IS", "limit": 100, "offset": 0}),
                ("get_user_alphas", {"stage": "OS", "limit": 101, "offset": 0}),
                ("get_datasets", {"region": "CHN"}),
                ("get_datasets", {"delay": True}),
                ("get_operators", {"grant_id": "d" * 32}),
            ]
            before = list(self.gateway.calls)
            for command, params in bad:
                with self.subTest(command=command, params=params):
                    with self.assertRaises(OrchestrationError):
                        runtime.gateway.call(command, **params)
            self.assertEqual(self.gateway.calls, before)
            with self.assertRaisesRegex(OrchestrationError, "local case"):
                runtime.gateway.call("set_case", case_key=run, run_id=run)
        self.assertEqual(
            inspect_runtime_attempt(self.runner, self.task_id)["status"],
            "NO_MODEL_ATTEMPT")

    def test_driver_frame_rejection_prevents_even_scripted_catalog_read(self):
        from unittest.mock import patch
        claim_mock_workspace(self.root)
        with self.adapter() as runtime:
            self.assertEqual(self.gateway.calls, [])
            with patch(
                "alpha_research.integrations.brain.driver.research_catalog_frame_allowed",
                return_value=False,
            ) as driver_guard:
                with self.assertRaisesRegex(
                        OrchestrationError, "scripted driver rejected"):
                    runtime.gateway.call("get_operators")
                self.assertEqual(driver_guard.call_count, 1)
                self.assertEqual(self.gateway.calls, [])

    def test_two_concurrent_adapter_instances_reserve_one_model_attempt(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        claim_mock_workspace(self.root)
        runtime_a, runtime_b = self.adapter(), self.adapter()
        barrier = Barrier(2)
        def attempt(runtime):
            with runtime:
                barrier.wait(timeout=3)
                try:
                    runtime.reserve_scripted_model_call(
                        "construct_super", {"region": "USA"})
                    return "RESERVED"
                except OrchestrationError:
                    return "REJECTED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(attempt, runtime_a)
            b = pool.submit(attempt, runtime_b)
            statuses = [a.result(), b.result()]
        self.assertEqual(statuses.count("RESERVED"), 1)
        self.assertEqual(statuses.count("REJECTED"), 1)
        self.assertEqual(
            inspect_runtime_attempt(self.runner, self.task_id)["status"],
            "MODEL_CALL_MAY_HAVE_STARTED")
        self.assertFalse(list(self.root.rglob("*simulation_intent.json")))

    def test_model_write_ahead_completion_and_no_second_call(self):
        claim_mock_workspace(self.root)
        before = self.runner.load(self.task_id)
        with self.adapter() as runtime:
            runtime.gateway.call(
                "set_case", case_key="a" * 32, run_id="a" * 32)
            recorder = DomainMockRecorder(self.model, self.root)
            result = recorder.complete_json(
                "construct_super", {"region": "USA"})
            self.assertEqual(result["selection"], "rank(signal_a)")
            report = runtime.verify_completed()
            self.assertEqual(report["status"], "MODEL_MOCK_COMPLETED")
            self.assertTrue(report["attempt_outcome_known"])
            self.assertFalse(report["provider_invoked"])
            self.assertFalse(report["brain_invoked"])
            self.assertFalse(report["model_call_authorized"])
            self.assertFalse(report["permission_to_dispatch"])
            with self.assertRaises(OrchestrationError):
                recorder.complete_json("construct_super", {"region": "USA"})
        self.assertEqual(self.runner.load(self.task_id), before)
        with self.assertRaisesRegex(OrchestrationError, "already exists"):
            self.adapter()

    def test_uncertain_model_attempt_requires_manual_reconciliation(self):
        claim_mock_workspace(self.root)
        with self.adapter() as runtime:
            runtime.reserve_scripted_model_call(
                "construct_super", {"region": "USA"})
            pending = inspect_runtime_attempt(self.runner, self.task_id)
            self.assertEqual(pending["status"], "MODEL_CALL_MAY_HAVE_STARTED")
            self.assertFalse(pending["automatic_retry_allowed"])
            self.assertFalse(pending["permission_to_dispatch"])
        with self.assertRaisesRegex(OrchestrationError, "already exists"):
            self.adapter()
        with self.assertRaisesRegex(OrchestrationError, "already exists"):
            self.adapter()
        self.assertEqual(self.runner.load(self.task_id)["budget"]["used"], 0)

    def test_two_concurrent_model_completions_have_one_terminal_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from alpha_research.state.fingerprint import fingerprint_json

        claim_mock_workspace(self.root)
        with self.adapter() as runtime:
            runtime.reserve_scripted_model_call(
                "construct_super", {"region": "USA"})
            barrier = Barrier(2)
            def complete(marker):
                barrier.wait(timeout=5)
                try:
                    runtime.conclude_scripted_model_call({"marker": marker})
                    return marker
                except OrchestrationError:
                    return None

            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(complete, "first")
                b = pool.submit(complete, "second")
                results = [a.result(), b.result()]
            winners = [item for item in results if item is not None]
            self.assertEqual(len(winners), 1)
            summary = inspect_runtime_attempt(self.runner, self.task_id)
            self.assertEqual(summary["status"], "MODEL_MOCK_COMPLETED")
            self.assertEqual(
                summary["response_sha256"],
                fingerprint_json({"marker": winners[0]}))
            with self.assertRaises(OrchestrationError):
                runtime.conclude_scripted_model_call(unknown=True)

        self.assertEqual(self.runner.load(self.task_id)["budget"]["used"], 0)
        self.assertFalse(list(self.root.rglob("*simulation_intent.json")))

    def test_model_terminal_rejects_resealed_policy_drift(self):
        claim_mock_workspace(self.root)
        with self.adapter() as runtime:
            runtime.reserve_scripted_model_call(
                "construct_super", {"region": "USA"})
            path = (self.root / "state" / "research_v1_1" /
                    "research_runtime_attempts" / (self.task_id + ".json"))
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["policy_sha256"] = "f" * 64
            write_sealed_json(path, raw)
            with self.assertRaisesRegex(OrchestrationError, "reservation changed"):
                runtime.conclude_scripted_model_call({"ok": True})
        self.assertEqual(inspect_runtime_attempt(
            self.runner, self.task_id)["status"], "MODEL_CALL_MAY_HAVE_STARTED")

    def test_model_terminal_rejects_symlinked_writer_lock(self):
        claim_mock_workspace(self.root)
        with self.adapter() as runtime:
            runtime.reserve_scripted_model_call(
                "construct_super", {"region": "USA"})
            lock = runtime._attempt.with_suffix(".lock")
            sentinel = self.root / "terminal-lock-sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            lock.unlink()
            try:
                lock.symlink_to(sentinel)
            except (OSError, NotImplementedError):
                self.skipTest("symlink not supported")
            with self.assertRaisesRegex(OrchestrationError, "writer lock is unsafe"):
                runtime.conclude_scripted_model_call({"ok": True})
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(inspect_runtime_attempt(
            self.runner, self.task_id)["status"], "MODEL_CALL_MAY_HAVE_STARTED")

    def test_unknown_model_outcome_stays_non_replayable(self):
        claim_mock_workspace(self.root)
        with self.adapter() as runtime:
            runtime.reserve_scripted_model_call(
                "construct_super", {"region": "USA"})
            runtime.conclude_scripted_model_call(unknown=True)
            report = inspect_runtime_attempt(self.runner, self.task_id)
            self.assertEqual(report["status"], "MODEL_MOCK_UNKNOWN")
            self.assertFalse(report["automatic_retry_allowed"])
        with self.assertRaises(OrchestrationError):
            self.adapter()

    def test_corrupted_or_symlinked_attempt_requires_reconciliation(self):
        claim_mock_workspace(self.root)
        with self.adapter() as runtime:
            runtime.reserve_scripted_model_call(
                "construct_super", {"region": "USA"})
        path = (self.root / "state" / "research_v1_1" /
                "research_runtime_attempts" / (self.task_id + ".json"))
        state = json.loads(path.read_text(encoding="utf-8"))
        state["status"] = "MODEL_MOCK_COMPLETED"
        path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(
            inspect_runtime_attempt(self.runner, self.task_id)["status"],
            "RECONCILIATION_REQUIRED")
        path.unlink()
        outside = self.root / "outside.json"
        outside.write_text("sentinel", encoding="utf-8")
        try:
            path.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not supported")
        self.assertEqual(
            inspect_runtime_attempt(self.runner, self.task_id)["status"],
            "RECONCILIATION_REQUIRED")
        self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")


class OfflineRuntimeIntegratedWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runner = MainOrchestrator(self.root)

    def test_super_and_optimization_run_through_runtime_and_stop_before_post(self):
        for workflow in ("super", "optimization"):
            with self.subTest(workflow=workflow):
                request = {"objective": "offline integrated runtime",
                           "region": "USA", "delay": 1, "universe": "TOP3000"}
                if workflow == "optimization":
                    request["alpha_id"] = "SOURCE"
                task = self.runner.create(workflow, request)
                # One isolated marker covers multiple synthetic Tasks.
                if workflow == "super":
                    claim_mock_workspace(self.root)
                result = prepare_offline_research(
                    self.runner, task["task_id"])
                self.assertEqual(result["status"], "RESEARCH_PREPARED_OFFLINE")
                self.assertEqual(result["catalog_reads"][0],
                                 "get_platform_setting_options" if workflow == "super"
                                 else "get_alpha_details")
                self.assertTrue(result["model_response_sha256"])
                attempt = inspect_runtime_attempt(self.runner, task["task_id"])
                self.assertEqual(attempt["status"], "MODEL_MOCK_COMPLETED")
                self.assertEqual(attempt["response_sha256"],
                                 result["model_response_sha256"])
                self.assertEqual(
                    self.runner.load(task["task_id"])["status"], "CREATED")
                self.assertEqual(
                    self.runner.load(task["task_id"])["budget"],
                    {"limit": 1, "used": 0, "reserved": 0})
                self.assertFalse(list(self.root.rglob("*simulation_intent.json")))
                self.assertEqual(
                    inspect_research_preparation(
                        self.runner, task["task_id"])["status"],
                    "RESEARCH_PREPARED_OFFLINE")

    def test_tampered_preparation_journal_is_rejected_without_replay(self):
        task = self.runner.create(
            "super", {"objective": "integrity journal test",
                      "region": "USA", "delay": 1, "universe": "TOP3000"})
        claim_mock_workspace(self.root)
        report = prepare_offline_research(self.runner, task["task_id"])
        self.assertEqual(report["status"], "RESEARCH_PREPARED_OFFLINE")
        journal = (self.root / "state" / "research_v1_1" /
                   "research_preparation" / (task["task_id"] + ".json"))
        raw = json.loads(journal.read_text(encoding="utf-8"))
        self.assertRegex(raw["state_sha256"], r"^[0-9a-f]{64}$")
        raw["manifest_sha256"] = "f" * 64
        journal.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "integrity drift"):
            inspect_research_preparation(self.runner, task["task_id"])
        with self.assertRaisesRegex(OrchestrationError, "integrity drift"):
            prepare_offline_research(self.runner, task["task_id"])
        self.assertEqual(self.runner.load(task["task_id"])["status"], "CREATED")
        self.assertFalse(list(self.root.rglob("*simulation_intent.json")))

    def test_tampered_model_response_invalidates_completed_preparation(self):
        task = self.runner.create(
            "super", {"objective": "tamper",
                      "region": "USA", "delay": 1, "universe": "TOP3000"})
        claim_mock_workspace(self.root)
        prepare_offline_research(self.runner, task["task_id"])
        attempt_path = (self.root / "state" / "research_v1_1" /
                        "research_runtime_attempts" /
                        (task["task_id"] + ".json"))
        body = json.loads(attempt_path.read_text(encoding="utf-8"))
        body["response_sha256"] = "f" * 64
        write_sealed_json(attempt_path, body)
        with self.assertRaisesRegex(OrchestrationError, "attempt evidence drift"):
            inspect_research_preparation(self.runner, task["task_id"])
        with self.assertRaisesRegex(OrchestrationError, "attempt evidence drift"):
            prepare_offline_research(self.runner, task["task_id"])


if __name__ == "__main__":
    unittest.main()
