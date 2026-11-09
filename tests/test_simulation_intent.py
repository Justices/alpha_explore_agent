import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.config import DEFAULT_CONFIG
from nastu_quant.research.records import ResearchRecordStore
from nastu_quant.state_store import atomic_write_json
from nastu_quant.research.simulation_intent import (
    create_simulation_intent, recover_simulation_intent, transition_simulation_intent,
)


class ReadBackGateway:
    def __init__(self, details, limit):
        self.details = details
        self.limit = limit
        self.commands = []

    def call(self, command, **kwargs):
        self.commands.append(command)
        if command == "set_case":
            return None
        if command == "get_alpha_details":
            return self.details
        if command == "check_local_self_correlation":
            return {"status": "fail", "complete": True,
                    "comparison_count": 1, "window_years": 4,
                    "limit": self.limit, "value": self.limit}
        if command == "resume_simulation_batch":
            raise AssertionError("READ_BACK recovery must not resume or repost")
        raise AssertionError("unexpected gateway call " + command)


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

    def test_read_back_recovery_finishes_candidate_lifecycle_without_reposting(self):
        root = Path(self.temp.name)
        store = ResearchRecordStore(root)
        run_id, batch_id = "a" * 32, "batch-1"
        now = datetime.now(timezone.utc).isoformat()
        platform_dir = store.root / "super_runs"
        platform_dir.mkdir(parents=True, exist_ok=True)
        platform_path = platform_dir / f"{run_id}.json"
        atomic_write_json(platform_path, {
            "run_id": run_id, "status": "simulated_pending_audit",
            "prereg_ref": "prereg.json"})
        store.put("super_run", {
            "schema_version": 1, "run_id": run_id, "workflow": "super",
            "region": "GLB", "objective": "recovery test",
            "budget": {"limits": {"experiments": 1},
                       "reserved": {"experiments": 0}, "used": {"experiments": 1}},
            "platform_record_ref": str(platform_path),
            "status": "simulated_pending_audit",
            "created_at": now, "updated_at": now})
        details = {
            "id": "A_RECOVERED", "type": "SUPER",
            "settings": {"region": "GLB", "delay": 1},
            "is": {"checks": [{"name": "LOW_SHARPE", "result": "PASS"}]},
        }
        row = {"alpha_id": "A_RECOVERED", "details": details}
        result = {"results": [row], "runner": {"recovered": True}}
        response_path, receipt_path = platform_dir / "response.json", platform_dir / "receipt.json"
        atomic_write_json(response_path, result)
        atomic_write_json(receipt_path, row)
        digest = hashlib.sha256(json.dumps(
            result, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")).hexdigest()
        intent_path = platform_dir / "intent.json"
        create_simulation_intent(
            intent_path, workflow="super", run_id=run_id, batch_id=batch_id,
            request={"selection": "rank(x)", "combo": "rank(y)"},
            settings={"region": "GLB", "delay": 1})
        transition_simulation_intent(intent_path, expected_status="PREPARED", status="POSTING")
        transition_simulation_intent(
            intent_path, expected_status="POSTING", status="RESPONSE_RECEIVED",
            response_sha256=digest, response_ref=str(response_path))
        transition_simulation_intent(
            intent_path, expected_status="RESPONSE_RECEIVED", status="READ_BACK",
            receipt_ref=str(receipt_path), response_sha256=digest,
            response_ref=str(response_path))
        gateway = ReadBackGateway(details, DEFAULT_CONFIG["gate"]["self_corr_max"])
        first = recover_simulation_intent(gateway, root, intent_path, config=DEFAULT_CONFIG)
        second = recover_simulation_intent(gateway, root, intent_path, config=DEFAULT_CONFIG)
        self.assertEqual(first["status"], "candidate_evaluated")
        self.assertEqual(second["candidate_id"], first["candidate_id"])
        self.assertEqual(store.get("super_run", run_id)["status"], "candidate_evaluated")
        self.assertEqual(store.list("candidate")[0]["status"], "CORR_REJECTED")
        history = store.root / "evaluation_history" / "candidate" / first["candidate_id"]
        self.assertFalse(history.exists())
        self.assertNotIn("resume_simulation_batch", gateway.commands)

    def test_intent_creation_refuses_overwrite(self):
        self.create()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.create()


if __name__ == "__main__":
    unittest.main()
