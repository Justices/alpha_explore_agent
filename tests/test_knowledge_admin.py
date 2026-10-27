import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nastu_quant.knowledge import KnowledgeError, KnowledgeService


class TestKnowledgeAdmin(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.prior = root / "prior.md"
        self.prior.write_text("# Prior case\nA scoped historical observation.",
                              encoding="utf-8")
        self.service = KnowledgeService(
            root / "state", root / "runs",
            {"knowledge": {"prior_knowledge_path": str(self.prior),
                           "field_root": str(root / "fields"),
                           "operator_root": str(root / "operators")}})

    def tearDown(self):
        self.temp.cleanup()

    def _write_closed_batch(self, *, status="READ_BACK", alpha_ids=None,
                            readback_ids=None, gate_ids=None,
                            batch_name="20261027_120000_abcd1234",
                            simulation_response=True):
        alpha_ids = alpha_ids or ["alpha-1"]
        readback_ids = alpha_ids if readback_ids is None else readback_ids
        gate_ids = alpha_ids if gate_ids is None else gate_ids
        batch_dir = (self.service.runs_root / "GLB" / "TOP300" / "1" /
                     "dataset-1" / batch_name)
        batch_dir.mkdir(parents=True, exist_ok=True)
        prereg_path = batch_dir / "prereg.json"
        prereg_path.write_text(
            json.dumps({"objective": "closed evidence fixture",
                        "variants": [{"expression": "rank(field_a)"}]}),
            encoding="utf-8")
        digest = hashlib.sha256(prereg_path.read_bytes()).hexdigest()
        (batch_dir / "state.json").write_text(json.dumps({
            "prereg_hashes": {str(prereg_path): digest},
        }), encoding="utf-8")
        (batch_dir / "simulation_intent.json").write_text(json.dumps({
            "status": status, "alpha_ids": alpha_ids,
        }), encoding="utf-8")
        readback = {alpha_id: {
            "details": {"id": alpha_id, "is": {"turnover": 0.25}},
            "checks": {"checks": []},
        } for alpha_id in readback_ids}
        gate = {alpha_id: {"status": "FAIL", "metrics": {"turnover": 0.25}}
                for alpha_id in gate_ids}
        if simulation_response:
            (batch_dir / "simulation_raw.json").write_text(json.dumps({
                "alpha_results": [{"alpha_id": alpha_id} for alpha_id in alpha_ids],
            }), encoding="utf-8")
        else:
            (batch_dir / "recovery.json").write_text(json.dumps({
                "matched": {"rank(field_a)": alpha_ids[0]}, "missing": [],
                "settings_missing_keys": {},
            }), encoding="utf-8")
        (batch_dir / "gate.json").write_text(json.dumps(gate), encoding="utf-8")
        (batch_dir / "results.json").write_text(json.dumps({
            "readback": readback, "gate": gate,
        }), encoding="utf-8")
        return batch_dir

    def test_note_updates_require_reason_and_preserve_change_log(self):
        self.service.put_note("operator", "ts_mean", "Exact meaning.",
                              "operator handbook#ts_mean", operator="researcher")
        self.assertEqual(self.service.lookup_operator("ts_mean")["content"],
                         "Exact meaning.")
        with self.assertRaises(KnowledgeError):
            self.service.put_note("operator", "ts_mean", "Updated meaning.",
                                  "operator handbook#ts_mean", operator="researcher")

        self.service.put_note("operator", "ts_mean", "Updated meaning.",
                              "operator handbook#ts_mean", replace=True,
                              reason="source correction", operator="researcher")
        self.assertEqual(self.service.lookup_operator("ts_mean")["content"],
                         "Updated meaning.")
        events = (self.service.knowledge_root / "management_log.jsonl").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(len(events), 2)

    def test_remedies_require_explicit_review_and_matching_facts(self):
        batch_dir = self._write_closed_batch()
        entry = self.service.add_remedy({
            "remedy_id": "R-TEST", "applies_to": ["S_TURNOVER_HI"],
            "preconditions": {"field_catalog": {"field_a": {
                "updateFrequency": "Daily"}}},
            "action": "smooth the field", "rationale": "reduce sparse jumps",
            "expected_effect": "lower turnover", "evidence": [{
                "batch": str(batch_dir), "alpha_id": "alpha-1",
                "measured_change": "turnover 0.42 to 0.25"}],
        }, operator="researcher")
        self.assertEqual(entry["status"], "pending_review")
        self.assertEqual(self.service.lookup_remedies(
            ["S_TURNOVER_HI"], {"field_catalog": {"field_a": {
                "updateFrequency": "Daily"}}})["verified"], [])

        reviewed = self.service.review_remedy(
            "R-TEST", "verified", reviewer="researcher",
            reason="evidence reviewed")
        evidence_ref = reviewed["review_history"][-1]["evidence_refs"][0]
        self.assertEqual(evidence_ref["batch"], str(batch_dir.resolve()))
        self.assertEqual(len(evidence_ref["prereg_sha256"]), 64)
        self.assertEqual(len(evidence_ref["results_sha256"]), 64)
        matching = self.service.lookup_remedies(
            ["S_TURNOVER_HI"], {"field_catalog": {"field_a": {
                "updateFrequency": "Daily"}}})["verified"]
        self.assertEqual([row["remedy_id"] for row in matching], ["R-TEST"])
        missing = self.service.lookup_remedies(
            ["S_TURNOVER_HI"], {"field_catalog": {"field_a": {}}})["verified"]
        self.assertEqual(missing, [])

    def test_verified_remedy_requires_structured_historical_evidence(self):
        self.service.add_remedy({
            "remedy_id": "R-NO-EVIDENCE", "applies_to": ["S_SHARPE"],
            "preconditions": {}, "action": "change structure",
            "rationale": "test", "expected_effect": "improve Sharpe",
            "evidence": [],
        }, operator="researcher")
        with self.assertRaises(KnowledgeError):
            self.service.review_remedy("R-NO-EVIDENCE", "verified",
                                       reviewer="researcher", reason="reviewed")

    def test_verified_remedy_rejects_missing_open_and_unmatched_batch_evidence(self):
        open_batch = self._write_closed_batch(
            status="SUBMITTING", batch_name="20261027_120001_abcd1235")
        unmatched_batch = self._write_closed_batch(
            readback_ids=["alpha-other"], gate_ids=["alpha-other"],
            batch_name="20261027_120002_abcd1236")
        for remedy_id, batch_dir in (
                ("R-MISSING", self.service.runs_root / "missing-batch"),
                ("R-OPEN", open_batch), ("R-UNMATCHED", unmatched_batch)):
            self.service.add_remedy({
                "remedy_id": remedy_id, "applies_to": ["S_SHARPE"],
                "preconditions": {}, "action": "change structure",
                "rationale": "test", "expected_effect": "improve Sharpe",
                "evidence": [{"batch": str(batch_dir), "alpha_id": "alpha-1",
                              "measured_change": "observed"}],
            }, operator="researcher")
            with self.subTest(remedy_id=remedy_id):
                with self.assertRaises(KnowledgeError):
                    self.service.review_remedy(
                        remedy_id, "verified", reviewer="researcher",
                        reason="reviewed")
                self.assertEqual(self.service.get_managed(
                    "remedy", remedy_id)["status"], "pending_review")

    def test_verified_remedy_is_disabled_when_its_batch_changes_after_review(self):
        batch_dir = self._write_closed_batch()
        self.service.add_remedy({
            "remedy_id": "R-CHANGED-BATCH", "applies_to": ["S_SHARPE"],
            "preconditions": {}, "action": "change structure",
            "rationale": "test", "expected_effect": "improve Sharpe",
            "evidence": [{"batch": str(batch_dir), "alpha_id": "alpha-1",
                          "measured_change": "turnover 0.42 to 0.25"}],
        })
        self.service.review_remedy("R-CHANGED-BATCH", "verified",
                                   reviewer="researcher", reason="reviewed")
        self.assertEqual(len(self.service.lookup_remedies(["S_SHARPE"])["verified"]), 1)

        changed_gate = {"alpha-1": {"status": "PASS", "metrics": {"turnover": 0.20}}}
        (batch_dir / "gate.json").write_text(json.dumps(changed_gate), encoding="utf-8")
        (batch_dir / "results.json").write_text(json.dumps({
            "readback": {"alpha-1": {"details": {
                "id": "alpha-1", "is": {"turnover": 0.20}},
                "checks": {"checks": []}}},
            "gate": changed_gate,
        }), encoding="utf-8")
        self.assertEqual(self.service.lookup_remedies(["S_SHARPE"])["verified"], [])
        historical = self.service.lookup_remedies(
            ["S_SHARPE"], include_historical=True)["historical"]
        self.assertEqual(historical[0]["validity"], "evidence_invalid")
        shown = self.service.get_managed("remedy", "R-CHANGED-BATCH")
        self.assertEqual(shown["evidence_state"], "invalid")

    def test_verified_remedy_accepts_closed_recovery_mapping_without_raw_response(self):
        batch_dir = self._write_closed_batch(simulation_response=False)
        self.service.add_remedy({
            "remedy_id": "R-RECOVERED", "applies_to": ["S_SHARPE"],
            "preconditions": {}, "action": "change structure",
            "rationale": "test", "expected_effect": "improve Sharpe",
            "evidence": [{"batch": str(batch_dir), "alpha_id": "alpha-1",
                          "measured_change": "turnover 0.42 to 0.25"}],
        })
        self.service.review_remedy("R-RECOVERED", "verified",
                                   reviewer="researcher", reason="reviewed")
        matching = self.service.lookup_remedies(["S_SHARPE"])["verified"]
        self.assertEqual([row["remedy_id"] for row in matching], ["R-RECOVERED"])
        ref = matching[0]["review_history"][-1]["evidence_refs"][0]
        self.assertEqual(len(ref["recovery_sha256"]), 64)
        self.assertIsNone(ref["simulation_raw_sha256"])

    def test_reviewed_measurement_claim_cannot_change_after_review(self):
        batch_dir = self._write_closed_batch()
        self.service.add_remedy({
            "remedy_id": "R-CHANGED-CLAIM", "applies_to": ["S_SHARPE"],
            "preconditions": {}, "action": "change structure",
            "rationale": "test", "expected_effect": "improve Sharpe",
            "evidence": [{"batch": str(batch_dir), "alpha_id": "alpha-1",
                          "measured_change": "turnover 0.42 to 0.25"}],
        })
        self.service.review_remedy("R-CHANGED-CLAIM", "verified",
                                   reviewer="researcher", reason="reviewed")
        payload = json.loads(self.service.remedies_path.read_text(encoding="utf-8"))
        payload["entries"][0]["evidence"][0]["measured_change"] = "turnover 0.50 to 0.10"
        self.service.remedies_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.service.lookup_remedies(["S_SHARPE"])["verified"], [])

    def test_audit_failures_roll_back_each_managed_mutation(self):
        fail_audit = patch.object(
            self.service, "_append_management_event",
            side_effect=OSError("simulated audit write failure"))

        with fail_audit, self.assertRaises(KnowledgeError):
            self.service.put_note("operator", "ts_mean", "Meaning.",
                                  "operator handbook#ts_mean")
        self.assertFalse(self.service.operator_notes_path.exists())

        remedy = {
            "remedy_id": "R-AUDIT", "applies_to": ["S_SHARPE"],
            "preconditions": {}, "action": "change structure",
            "rationale": "test", "expected_effect": "improve Sharpe",
            "evidence": [],
        }
        with patch.object(self.service, "_append_management_event",
                          side_effect=OSError("simulated audit write failure")), \
                self.assertRaises(KnowledgeError):
            self.service.add_remedy(remedy)
        self.assertEqual(self.service._load_remedies(), [])

        self.service.add_remedy(remedy)
        before = self.service.get_managed("remedy", "R-AUDIT")
        with patch.object(self.service, "_append_management_event",
                          side_effect=OSError("simulated audit write failure")), \
                self.assertRaises(KnowledgeError):
            self.service.review_remedy("R-AUDIT", "void", reviewer="researcher",
                                       reason="review test")
        self.assertEqual(self.service.get_managed("remedy", "R-AUDIT"), before)

        evidence_id = self.service.list_managed("evidence")["evidence"][0]["id"]
        with patch.object(self.service, "_append_management_event",
                          side_effect=OSError("simulated audit write failure")), \
                self.assertRaises(KnowledgeError):
            self.service.set_evidence_validity(evidence_id, "void", "test")
        self.assertFalse(self.service.validity_path.exists())

    def test_log_replace_failure_preserves_previous_log_and_note(self):
        self.service.put_note("operator", "ts_mean", "Original meaning.",
                              "operator handbook#ts_mean")
        log_path = self.service.knowledge_root / "management_log.jsonl"
        original_log = log_path.read_bytes()
        with patch.object(self.service, "_replace_management_log",
                          side_effect=OSError("simulated atomic replace failure")), \
                self.assertRaises(KnowledgeError):
            self.service.put_note(
                "operator", "ts_mean", "Changed meaning.",
                "operator handbook#ts_mean", replace=True,
                reason="test replacement")
        self.assertEqual(log_path.read_bytes(), original_log)
        self.assertEqual(self.service.lookup_operator("ts_mean")["content"],
                         "Original meaning.")

    def test_evidence_validity_requires_existing_id_and_preserves_history(self):
        evidence = self.service.list_managed("evidence")["evidence"]
        evidence_id = evidence[0]["id"]
        with self.assertRaises(KnowledgeError):
            self.service.set_evidence_validity("unknown-id", "void", "bad reference")

        self.service.set_evidence_validity(evidence_id, "void", "old conclusion")
        self.service.set_evidence_validity(evidence_id, "valid", "evidence restored")
        import json
        validity = json.loads(self.service.validity_path.read_text(encoding="utf-8"))
        record = validity[evidence_id]
        self.assertEqual(record["validity"], "valid")
        self.assertEqual([event["validity"] for event in record["history"]],
                         ["void", "valid"])


if __name__ == "__main__":
    unittest.main()
