import json
import tempfile
import unittest
from pathlib import Path

from nastu_quant.research.simulation_intent import (
    create_simulation_intent, transition_simulation_intent,
)


class TestSimulationIntent(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "intent.json"

    def tearDown(self):
        self.temp.cleanup()

    def create(self):
        return create_simulation_intent(
            self.path, workflow="super", run_id="run-1", batch_id="batch-1",
            request={"selection": "rank(x)", "combo": "rank(y)"},
            settings={"region": "GLB", "delay": 1},
        )

    def test_intent_is_persisted_before_post(self):
        record = self.create()
        self.assertEqual(record["status"], "PREPARED")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["request_sha256"], record["request_sha256"])
        self.assertEqual(saved["status"], "PREPARED")

    def test_state_machine_requires_ordered_transitions(self):
        self.create()
        with self.assertRaisesRegex(ValueError, "state mismatch"):
            transition_simulation_intent(
                self.path, expected_status="RESPONSE_RECEIVED", status="READ_BACK",
                receipt_ref="receipt.json")
        transition_simulation_intent(
            self.path, expected_status="PREPARED", status="POSTING")
        transition_simulation_intent(
            self.path, expected_status="POSTING", status="RESPONSE_RECEIVED")
        transition_simulation_intent(
            self.path, expected_status="RESPONSE_RECEIVED", status="READ_BACK",
            receipt_ref="receipt.json")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "READ_BACK")
        self.assertEqual(saved["receipt_ref"], "receipt.json")

    def test_uncertain_outcome_is_terminal_and_cannot_be_reposted(self):
        self.create()
        transition_simulation_intent(
            self.path, expected_status="PREPARED", status="POSTING")
        transition_simulation_intent(
            self.path, expected_status="POSTING", status="OUTCOME_UNKNOWN")
        with self.assertRaisesRegex(ValueError, "invalid simulation intent transition"):
            transition_simulation_intent(
                self.path, expected_status="OUTCOME_UNKNOWN", status="POSTING")

    def test_intent_creation_refuses_overwrite(self):
        self.create()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.create()


if __name__ == "__main__":
    unittest.main()
