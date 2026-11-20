"""Offline cross-process-lock and crash-gap regression tests.

Every adapter/recovery handler is a local fixture: these tests never call
BRAIN, models, CLI agents, external APIs or a real Simulation.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.offline_adapter import OfflineResearchAdapter
from alpha_research.orchestration.recovery import RecoveryCoordinator, RecoveryError
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock


def fixtures(workflow):
    return {
        workflow + "." + step: {"ok": True, "step": step}
        for step in ("validate", "execute", "qualify", "collect")
    }


def intent(root, workflow="super", name="first"):
    path = root / "state" / "research_v1_1" / workflow / (name + ".json")
    atomic_write_json(path, {
        "schema_version": 1, "workflow": workflow, "status": "POSTING",
        "run_id": name, "batch_id": name, "request_sha256": "a" * 64,
        "request": {"step": "submit", "batch": name},
        "settings": {"region": "GLB"},
    })
    return path.resolve()


class OrchestrationConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_concurrent_executor_fails_before_adapter_and_cannot_block_owner(self):
        suite = self
        executed = []

        class ProbeAdapter(OfflineResearchAdapter):
            def run(self, workflow, step, request, checkpoints, context=None):
                if step == "execute":
                    executed.append(step)
                    with suite.assertRaisesRegex(
                            OrchestrationError, "another executor"):
                        contender.execute(context["task_id"])
                return super().run(workflow, step, request, checkpoints,
                                   context=context)

        runner = MainOrchestrator(
            self.root, adapter=ProbeAdapter(fixtures("super")))
        contender = MainOrchestrator(
            self.root, adapter=OfflineResearchAdapter(fixtures("super")))
        task_id = runner.create("super", {"objective": "concurrent fixture"})["task_id"]
        finished = runner.execute(task_id)
        self.assertEqual(finished["status"], "COMPLETED")
        self.assertEqual(executed, ["execute"])
        self.assertEqual(finished["budget"],
                         {"limit": 1, "used": 1, "reserved": 0})

    def test_concurrent_reconcile_cannot_interleave_with_running_executor(self):
        suite = self

        class ProbeAdapter(OfflineResearchAdapter):
            def run(self, workflow, step, request, checkpoints, context=None):
                if step == "execute":
                    with suite.assertRaisesRegex(
                            OrchestrationError, "another executor"):
                        contender.reconcile(context["task_id"], "execute", {})
                return super().run(workflow, step, request, checkpoints,
                                   context=context)

        runner = MainOrchestrator(
            self.root, adapter=ProbeAdapter(fixtures("super")))
        contender = MainOrchestrator(self.root)
        task_id = runner.create("super", {"objective": "reconcile fixture"})["task_id"]
        self.assertEqual(runner.execute(task_id)["status"], "COMPLETED")

    def test_compare_and_save_fails_without_changing_task_when_writer_busy(self):
        runner = MainOrchestrator(self.root)
        task = runner.create("super", {"objective": "write lock fixture"})
        path = runner._path(task["task_id"])
        before = path.read_bytes()
        with SingleWriterLock(runner._mutation_lock_path(task["task_id"])):
            with self.assertRaisesRegex(OrchestrationError, "another writer"):
                runner._save(task, expected=task["updated_at"])
            with self.assertRaisesRegex(OrchestrationError, "another writer"):
                runner.create("super", {"objective": "same id"},
                              task_id=task["task_id"])
        self.assertEqual(path.read_bytes(), before)

    def test_execution_lock_is_respected_by_a_separate_python_process(self):
        runner = MainOrchestrator(self.root)
        task_id = runner.create("super", {"objective": "OS lock fixture"})["task_id"]
        lock_path = runner._execution_lock_path(task_id)
        script = (
            "import sys\n"
            "from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError\n"
            "try:\n"
            "    with SingleWriterLock(sys.argv[1]):\n"
            "        print('ACQUIRED')\n"
            "except WriterLockError:\n"
            "    print('BUSY')\n"
        )
        def probe():
            return subprocess.run(
                [sys.executable, "-c", script, str(lock_path)],
                cwd=Path(__file__).resolve().parent.parent,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=15, check=True).stdout.strip()
        with SingleWriterLock(lock_path):
            self.assertEqual(probe(), "BUSY")
        self.assertEqual(probe(), "ACQUIRED")

    def test_hard_interrupt_releases_lock_but_unknown_step_never_replays(self):
        calls = []

        class InterruptedAdapter(OfflineResearchAdapter):
            def run(self, workflow, step, request, checkpoints, context=None):
                if step == "execute":
                    calls.append("provider-intent")
                    raise KeyboardInterrupt("synthetic process crash")
                return super().run(workflow, step, request, checkpoints,
                                   context=context)

        runner = MainOrchestrator(
            self.root, adapter=InterruptedAdapter(fixtures("super")))
        task_id = runner.create("super", {"objective": "crash fixture"})["task_id"]
        with self.assertRaisesRegex(KeyboardInterrupt, "synthetic process crash"):
            runner.execute(task_id)
        state = runner.load(task_id)
        self.assertEqual(state["steps"]["execute"], "RUNNING")
        self.assertEqual(state["budget"]["reserved"], 1)

        restarted = MainOrchestrator(
            self.root, adapter=InterruptedAdapter(fixtures("super")))
        blocked = restarted.execute(task_id)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["failure"]["reason"],
                         "outcome_unknown_manual_reconciliation_required")
        self.assertEqual(blocked["budget"]["reserved"], 1)
        self.assertEqual(calls, ["provider-intent"])

    def test_existing_lock_file_without_live_owner_does_not_block(self):
        runner = MainOrchestrator(
            self.root, adapter=OfflineResearchAdapter(fixtures("super")))
        task_id = runner.create("super", {"objective": "stale metadata"})["task_id"]
        lock_path = runner._execution_lock_path(task_id)
        lock_path.write_text('{"pid": 999999999, "acquired_at": "stale"}',
                             encoding="utf-8")
        self.assertEqual(runner.execute(task_id)["status"], "COMPLETED")


class RecoveryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.gateway = object()

    def test_recovery_lock_blocks_before_handler_and_journal_creation(self):
        target = intent(self.root)
        handlers = []
        coordinator = RecoveryCoordinator(
            self.root, simulation_recover=lambda *a, **kw: (
                handlers.append(1) or {"ok": True}))
        path = coordinator._journal_path("super", target)
        with SingleWriterLock(path.with_suffix(".lock")):
            with self.assertRaisesRegex(RecoveryError, "another recovery writer"):
                coordinator.recover(target, gateway=self.gateway)
        self.assertEqual(handlers, [])
        self.assertFalse(path.exists())

    def test_recovery_handler_cannot_recursively_start_same_target(self):
        target = intent(self.root)
        calls = []
        suite = self

        def handler(gateway, root, path, config=None):
            calls.append("first")
            with suite.assertRaisesRegex(RecoveryError, "another recovery writer"):
                contender.recover(target, gateway=suite.gateway)
            return {"status": "read_back"}

        coordinator = RecoveryCoordinator(self.root, simulation_recover=handler)
        contender = RecoveryCoordinator(self.root, simulation_recover=handler)
        first = coordinator.recover(target, gateway=self.gateway)
        self.assertEqual(first, {"status": "read_back"})
        self.assertEqual(contender.recover(target, gateway=self.gateway), first)
        self.assertEqual(calls, ["first"])

    def test_keyboard_interrupt_keeps_intent_running_and_never_replays(self):
        target = intent(self.root)
        calls = []

        def crash(gateway, root, path, config=None):
            calls.append("started")
            raise KeyboardInterrupt("synthetic process termination")

        coordinator = RecoveryCoordinator(self.root, simulation_recover=crash)
        with self.assertRaises(KeyboardInterrupt):
            coordinator.recover(target, gateway=self.gateway)
        journal = coordinator.journal_for(target)
        self.assertEqual(journal["status"], "RUNNING")
        with self.assertRaisesRegex(RecoveryError, "automatic replay is prohibited"):
            coordinator.recover(target, gateway=self.gateway)
        self.assertEqual(calls, ["started"])

    def test_independent_recovery_targets_do_not_share_operation_lock(self):
        first = intent(self.root, name="one")
        second = intent(self.root, name="two")
        calls = []
        coordinator = RecoveryCoordinator(
            self.root, simulation_recover=lambda gw, root, path, config=None:
            (calls.append(Path(path).name) or {"ok": True}))
        with SingleWriterLock(
                coordinator._journal_path("super", first).with_suffix(".lock")):
            self.assertEqual(coordinator.recover(second, gateway=self.gateway),
                             {"ok": True})
        self.assertEqual(calls, ["two.json"])

    def test_recover_task_lock_blocks_before_domain_handler(self):
        target = intent(self.root)

        class BindThenFail(OfflineResearchAdapter):
            def run(self, workflow, step, request, checkpoints, context=None):
                if step == "execute":
                    context["bind_recovery_target"]("simulation_intent", target)
                    raise RuntimeError("synthetic lost result")
                return super().run(workflow, step, request, checkpoints,
                                   context=context)

        runner = MainOrchestrator(
            self.root, adapter=BindThenFail(fixtures("super")))
        task_id = runner.create("super", {"objective": "task recovery"})["task_id"]
        blocked = runner.execute(task_id)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["budget"]["reserved"], 1)
        calls = []
        coordinator = RecoveryCoordinator(
            self.root, simulation_recover=lambda *a, **kw:
            (calls.append(1) or {"ok": True}))
        lock_path = runner._path(task_id).with_suffix(".recover.lock")
        with SingleWriterLock(lock_path):
            with self.assertRaisesRegex(RecoveryError, "another recovery writer"):
                coordinator.recover_task(
                    runner, task_id, gateway=self.gateway)
        self.assertEqual(calls, [])
        self.assertEqual(runner.load(task_id)["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
