"""P4 non-executable receipt ledger regression contracts, no network."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.execution_reservation import (
    inspect_non_executable_reservation, reserve_non_executable_receipt,
    _load_ledger,
)


class NonExecutableReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = MainOrchestrator(self.tmp.name)
        self.task_id = "a" * 32
        self.auth_id = "b" * 32
        self.response = {
            "task_id": self.task_id, "authorization_id": self.auth_id,
            "operation": "brain_simulation", "receipt_sha256": "c" * 64,
            "permission_to_dispatch": False,
        }
        self.root = (Path(self.tmp.name) / "state" / "research_v1_1" /
                     "execution_receipt_reservations")

    def reserve(self):
        with patch(
            "alpha_research.orchestration.execution_reservation.inspect_execution_receipt",
            return_value=self.response,
        ):
            return reserve_non_executable_receipt(
                self.runner, self.task_id, {}, {}, {"synthetic": True})

    def test_reservation_is_non_executable_and_single_use(self):
        result = self.reserve()
        self.assertEqual(result["status"], "RESERVED_INSPECTION_ONLY")
        self.assertFalse(result["permission_to_dispatch"])
        self.assertFalse(result["brain_simulation_authorized"])
        self.assertEqual(inspect_non_executable_reservation(
            self.runner, self.task_id)["status"], "RESERVED_INSPECTION_ONLY")
        with self.assertRaisesRegex(OrchestrationError, "already reserved"):
            self.reserve()

    def test_interrupted_projection_rejects_replay(self):
        self.reserve()
        (self.root / (self.auth_id + ".json")).unlink()
        inspected = inspect_non_executable_reservation(self.runner, self.task_id)
        self.assertEqual(inspected["status"], "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "already reserved"):
            self.reserve()

    def test_canonical_ledger_corruption_is_fatal(self):
        self.reserve()
        path = self.root / "reservations.json"
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger["by_id"]["d" * 32] = "e" * 32
        path.write_text(json.dumps(ledger), encoding="utf-8")
        self.assertEqual(inspect_non_executable_reservation(
            self.runner, self.task_id)["status"], "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "reciprocal"):
            self.reserve()

    def test_missing_canonical_ledger_with_orphan_requires_manual_review(self):
        self.reserve()
        (self.root / "reservations.json").unlink()
        status = inspect_non_executable_reservation(self.runner, self.task_id)
        self.assertEqual(status["status"], "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "canonical ledger missing"):
            self.reserve()

    def test_mock_workspace_forbids_receipt_reservation(self):
        from alpha_research.orchestration.mock_workspace import claim_mock_workspace
        claim_mock_workspace(self.tmp.name)
        with self.assertRaisesRegex(OrchestrationError, "mock workspace"):
            self.reserve()


if __name__ == "__main__":
    unittest.main()
