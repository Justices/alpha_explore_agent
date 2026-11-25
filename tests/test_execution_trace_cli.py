"""CLI read-only access to the complete Task execution trace."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout

from alpha_research.cli import main
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.orchestration.event_journal import ExecutionEventJournal


class ExecutionTraceCliTests(unittest.TestCase):
    def test_missing_journal_is_never_reported_as_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            task = MainOrchestrator(directory).create(
                "regular", {"objective": "missing trace", "region": "USA"})
            journal = ExecutionEventJournal(directory, task["task_id"])
            journal.path.unlink()
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([
                    "--state-root", directory,
                    "orchestration-execution-trace", task["task_id"],
                ])
            report = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(report["status"], "TRACE_INCOMPLETE")
            self.assertEqual(report["journal_verification"]["status"], "MISSING")

    def test_trace_correlates_nested_optimization_research_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            task = MainOrchestrator(directory).create(
                "regular", {"objective": "nested optimization", "region": "USA"})
            nested_id = "a" * 32
            ExecutionEventJournal(directory, task["task_id"]).append(
                workflow="regular", event_type="optimization.research_started",
                research_id="optimization:" + nested_id,
                payload={"run_id": nested_id})
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([
                    "--state-root", directory,
                    "orchestration-execution-trace", task["task_id"],
                ])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertIn("regular:campaign:" + task["task_id"], report["task"]["research_ids"]
                          if "regular:campaign:" + task["task_id"] in report["task"]["research_ids"]
                          else report["task"]["research_ids"])
            self.assertIn("optimization:" + nested_id, report["task"]["research_ids"])
            self.assertEqual(len(report["model_budgets"]), 2)
            self.assertEqual(report["domain_evidence"]["optimization_runs"][0]["run_id"],
                             nested_id)

    def test_trace_command_verifies_journal_without_model_roles_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = directory
            task = MainOrchestrator(root).create(
                "regular", {"objective": "trace inspection", "region": "USA"})
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([
                    "--state-root", root,
                    "orchestration-execution-trace", task["task_id"],
                ])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "TRACE_VERIFIED")
            self.assertEqual(report["task"]["task_id"], task["task_id"])
            self.assertEqual(report["journal_verification"]["status"], "VERIFIED")
            self.assertEqual(report["journal_verification"]["event_count"], 1)
            self.assertEqual(report["events"][0]["event_type"], "task.created")
            self.assertEqual(report["model_calls"], [])
            self.assertEqual(report["evidence_manifest"]["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
