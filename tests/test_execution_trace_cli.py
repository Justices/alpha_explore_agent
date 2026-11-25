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


if __name__ == "__main__":
    unittest.main()
