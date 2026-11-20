"""Offline durable domain Research ID handoff and failure containment tests."""
import tempfile
import unittest

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError


class BindingAdapter:
    def __init__(self, research_id, *, fail=False):
        self.research_id = research_id
        self.fail = fail
        self.calls = 0

    def run(self, workflow, step, request, checkpoints, context=None):
        if step == "validate":
            return {"validated": True}
        if step == "execute":
            self.calls += 1
            context["bind_research_identity"](self.research_id)
            context["bind_research_identity"](self.research_id)
            if self.fail:
                raise RuntimeError("synthetic execution interruption")
            return {"status": "synthetic_mock_completed"}
        if step == "qualify":
            return {"candidate_status": "EVIDENCE_PENDING"}
        return {"status": "done"}


class ResearchIdentityBindingTests(unittest.TestCase):
    def test_all_workflows_bind_domain_identity_idempotently(self):
        for workflow, prefix in (
            ("regular", "regular:campaign:"),
            ("super", "super:"),
            ("optimization", "optimization:"),
        ):
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as td:
                research_id = prefix + "a" * 32
                adapter = BindingAdapter(research_id)
                runner = MainOrchestrator(td, adapter=adapter)
                request = {"objective": "offline research binding"}
                if workflow == "optimization":
                    request["alpha_id"] = "offline-source"
                task = runner.create(workflow, request)
                done = runner.execute(task["task_id"])
                self.assertEqual(done["status"], "COMPLETED")
                self.assertEqual(done["research_identity"]["research_id"], research_id)
                self.assertEqual(done["research_identity"]["workflow"], workflow)
                self.assertEqual(adapter.calls, 1)
                self.assertEqual(runner.execute(task["task_id"]), done)

    def test_crash_after_identity_binding_blocks_replay_without_refunding_budget(self):
        with tempfile.TemporaryDirectory() as td:
            adapter = BindingAdapter("regular:campaign:" + "b" * 32, fail=True)
            runner = MainOrchestrator(td, adapter=adapter)
            task = runner.create("regular", {"objective": "synthetic crash"})
            blocked = runner.execute(task["task_id"])
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(blocked["research_identity"]["research_id"],
                             "regular:campaign:" + "b" * 32)
            self.assertEqual(blocked["budget"]["reserved"], 1)
            self.assertEqual(blocked["budget"]["used"], 0)
            with self.assertRaisesRegex(OrchestrationError, "explicit reconciliation"):
                runner.resume(task["task_id"])
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(MainOrchestrator(td).load(task["task_id"]), blocked)

    def test_binding_conflict_rejects_research_identity_substitution(self):
        with tempfile.TemporaryDirectory() as td:
            first = "regular:campaign:" + "a" * 32
            class DenyChange:
                def run(self, workflow, step, request, checkpoints, context=None):
                    if step == "execute":
                        context["bind_research_identity"](first)
                        errors = 0
                        for bad in ("regular:campaign:" + "b" * 32,
                                    "super:" + "a" * 32):
                            try:
                                context["bind_research_identity"](bad)
                            except OrchestrationError:
                                errors += 1
                        if errors != 2:
                            raise AssertionError("identity mutation was accepted")
                        raise RuntimeError("intentional fail-closed")
                    return {"ok": True}
            runner = MainOrchestrator(td, adapter=DenyChange())
            task = runner.create("regular", {"objective": "reject identity changes"})
            blocked = runner.execute(task["task_id"])
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(blocked["research_identity"]["research_id"], first)

    def test_cannot_bind_domain_identity_before_running(self):
        with tempfile.TemporaryDirectory() as td:
            runner = MainOrchestrator(td)
            task = runner.create("regular", {"objective": "not started"})
            with self.assertRaisesRegex(OrchestrationError, "during execute"):
                runner.bind_research_identity(task["task_id"],
                                             "regular:campaign:" + "c" * 32)


if __name__ == "__main__":
    unittest.main()
