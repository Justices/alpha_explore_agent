"""P1-P4 offline orchestration contract and fault recovery tests."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from nastu_quant.research.orchestrator import (
    MainOrchestrator, OfflineResearchAdapter, OrchestrationError, STEPS,
)


def fixtures(workflow):
    return {workflow + "." + step: {"step": step, "ok": True} for step in STEPS}


class FaultAdapter(OfflineResearchAdapter):
    def __init__(self, values, fail_step):
        super().__init__(values)
        self.fail_step = fail_step
        self.calls = []

    def run(self, workflow, step, request, checkpoints, context=None):
        self.calls.append(step)
        if step == self.fail_step:
            raise RuntimeError("injected failure")
        return super().run(workflow, step, request, checkpoints)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_three_workflows_finish_with_independent_checkpoints(self):
        for workflow in ("regular", "super", "optimization"):
            with self.subTest(workflow=workflow):
                request = {"objective": "offline research"}
                if workflow == "optimization":
                    request["alpha_id"] = "SOURCE"
                runner = MainOrchestrator(
                    self.root, adapter=OfflineResearchAdapter(fixtures(workflow)))
                task = runner.create(workflow, request)
                finished = runner.execute(task["task_id"])
                self.assertEqual(finished["status"], "COMPLETED")
                self.assertEqual(list(finished["checkpoints"]), list(STEPS))
                self.assertEqual(finished["budget"], {
                    "limit": 1, "reserved": 0, "used": 1})
                self.assertEqual(finished["result"]["qualification"]["step"], "qualify")
                self.assertEqual(runner.resume(task["task_id"]), finished)

    def test_unknown_execute_outcome_is_not_replayed(self):
        adapter = FaultAdapter(fixtures("super"), "execute")
        runner = MainOrchestrator(self.root, adapter=adapter)
        task = runner.create("super", {"objective": "fault"})
        blocked = runner.execute(task["task_id"])
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["budget"]["reserved"], 1)
        self.assertEqual(adapter.calls, ["validate", "execute"])
        with self.assertRaises(OrchestrationError):
            runner.resume(task["task_id"])
        self.assertEqual(adapter.calls, ["validate", "execute"])
        with self.assertRaisesRegex(OrchestrationError, "receipt"):
            runner.reconcile(task["task_id"], "execute", {"receipt": "unverified"})
        self.assertEqual(adapter.calls, ["validate", "execute"])

    def test_non_execute_failure_is_fail_closed(self):
        adapter = FaultAdapter(fixtures("regular"), "qualify")
        runner = MainOrchestrator(self.root, adapter=adapter)
        task = runner.create("regular", {"objective": "fault"})
        blocked = runner.execute(task["task_id"])
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["budget"]["used"], 1)
        with self.assertRaises(OrchestrationError):
            runner.resume(task["task_id"])
        with self.assertRaisesRegex(OrchestrationError, "receipt"):
            runner.reconcile(task["task_id"], "qualify", {"status": "EVIDENCE_PENDING"})
        self.assertEqual(runner.load(task["task_id"])["status"], "BLOCKED")

    def test_missing_fixture_blocks_without_platform_fallback(self):
        runner = MainOrchestrator(self.root, adapter=OfflineResearchAdapter({}))
        task = runner.create("regular", {"objective": "no fixture"})
        self.assertEqual(runner.execute(task["task_id"])["status"], "BLOCKED")

    def test_no_adapter_never_runs(self):
        runner = MainOrchestrator(self.root)
        task = runner.create("super", {"objective": "no live access"})
        with self.assertRaises(OrchestrationError):
            runner.execute(task["task_id"])
        self.assertEqual(runner.load(task["task_id"])["status"], "CREATED")

    def test_request_tampering_is_rejected(self):
        runner = MainOrchestrator(self.root)
        task = runner.create("regular", {"objective": "original"})
        path = runner._path(task["task_id"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["request"]["objective"] = "tampered"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(OrchestrationError):
            runner.load(task["task_id"])

    def test_invalid_workflow_and_missing_source(self):
        runner = MainOrchestrator(self.root)
        with self.assertRaises(OrchestrationError):
            runner.create("other", {"objective": "x"})
        with self.assertRaises(OrchestrationError):
            runner.create("optimization", {"objective": "x"})


if __name__ == "__main__":
    unittest.main()
