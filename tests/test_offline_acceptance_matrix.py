"""Offline acceptance matrix: process isolation, continue-on-failure, evidence hashes."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.offline_acceptance_matrix import CASES, run_matrix


class OfflineAcceptanceMatrixTests(unittest.TestCase):
    def test_all_workflows_are_attempted_and_logged_despite_a_failed_case(self):
        invoked = []

        def fake_run(command, **kwargs):
            self.assertIn("orchestration-e2e-offline", command)
            self.assertIn("--mode", command)
            self.assertEqual(command[command.index("--mode") + 1], "mock")
            self.assertNotIn("--allow-brain-simulation", command)
            name = command[command.index("--objective") + 1].split(": ")[-1]
            invoked.append(name)
            faulty = name == "regular_unknown_outcome"
            invalid = name == "super_native"
            report = {
                "status": "OFFLINE_E2E_BLOCKED" if faulty
                          else "OFFLINE_E2E_PASSED",
                "task_id": "a" * 32,
                "trace_status": "TRACE_VERIFIED",
                "trace_last_event_sha256": "b" * 64,
                "workflow_handoff_audit": {"status": "VERIFIED"},
                "provider_model_invoked": False, "brain_invoked": False,
                "brain_simulation_invoked": False,
                "alpha_submission_invoked": False,
                "production_authorized": False,
            }
            if invalid:
                report["trace_status"] = "TRACE_INCOMPLETE"
            return SimpleNamespace(
                returncode=2 if faulty else 0,
                stdout=json.dumps(report),
                stderr="synthetic stderr",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "acceptance"
            with patch("scripts.offline_acceptance_matrix.subprocess.run",
                       side_effect=fake_run):
                report = run_matrix(root)
            self.assertEqual(invoked, [row[0] for row in CASES])
            self.assertEqual(report["status"], "INCOMPLETE")
            self.assertEqual(report["accepted_count"], len(CASES) - 1)
            self.assertEqual(
                [row["case"] for row in report["cases"] if row["status"] == "FAILED"],
                ["super_native"],
            )
            for row in report["cases"]:
                self.assertTrue(Path(row["stdout_ref"]).is_file())
                self.assertTrue(Path(row["stderr_ref"]).is_file())
                self.assertEqual(len(row["stdout_sha256"]), 64)
            self.assertEqual(
                json.loads((root / "acceptance_matrix.json").read_text())["status"],
                "INCOMPLETE",
            )

    def test_existing_output_root_is_refused_without_rewriting_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "existing-research.json"
            original.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "new or empty"):
                run_matrix(root)
            self.assertEqual(original.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
