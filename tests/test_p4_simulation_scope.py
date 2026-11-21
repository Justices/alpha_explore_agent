"""Offline P4.5 Simulation scope and atomic intent regression tests."""
import hashlib
import hmac
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.simulation_scope import (
    CONTEXT, VERIFY_KEY_ENV, inspect_simulation_scope,
)
from alpha_research.simulation.intent import (
    create_simulation_intent, transition_simulation_intent,
)
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json


class SimulationScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runner = MainOrchestrator(self.root)
        self.task = self.runner.create(
            "super", {"objective": "offline scoped admission",
                      "region": "USA", "delay": 1, "universe": "TOP3000"})
        self.plan = {
            "research_id": "super:" + "a" * 32,
            "run_id": "b" * 32,
            "batch_id": "batch_one",
            "request": {"selection": "rank(x)", "combo": "rank(y)"},
            "settings": {"region": "USA", "delay": 1,
                         "universe": "TOP3000", "instrument_type": "EQUITY"},
        }
        self.model = {"roles": "offline"}
        self.policy = {"max_calls": 1, "max_reported_tokens": 100,
                       "failover": "manual"}
        self.key = "test-key-" * 8
        self.now = datetime.now(timezone.utc)
        self.claims = {
            "schema_version": 2, "authorization_id": "c" * 32,
            "issuer": "offline_test_issuer",
            "operation": "brain_simulation",
            "task_id": self.task["task_id"],
            "workflow": "super",
            "request_sha256": self.task["request_sha256"],
            "proposal_sha256": "d" * 64,
            "model_profile_sha256": "e" * 64,
            "model_governance_policy_sha256": "f" * 64,
            "research_id": self.plan["research_id"],
            "run_id": self.plan["run_id"], "batch_id": self.plan["batch_id"],
            "simulation_sha256": fingerprint_json({
                "request": self.plan["request"],
                "settings": self.plan["settings"]}),
            "scope": {k: self.plan["settings"][k]
                      for k in ("region", "delay", "universe", "instrument_type")},
            "max_attempts": 1,
            "issued_at": (self.now - timedelta(minutes=1)).isoformat(),
            "expires_at": (self.now + timedelta(minutes=4)).isoformat(),
        }

    def check(self, *, claims=None, plan=None, signature=None):
        selected = self.claims if claims is None else claims
        signed = hmac.new(self.key.encode(), CONTEXT + canonical_json_bytes(selected),
                          hashlib.sha256).hexdigest()
        receipt = {"claims": selected, "signature": signature or signed}
        with (patch("alpha_research.orchestration.simulation_scope.inspect_execution_proposal",
                    return_value={"proposal_sha256": "d" * 64}),
              patch("alpha_research.orchestration.simulation_scope.inspect_execution_identity",
                    return_value={"identity": {
                        "model_profile_sha256": "e" * 64,
                        "model_governance_policy_sha256": "f" * 64,
                    }})):
            return inspect_simulation_scope(
                self.runner, self.task["task_id"], self.model, self.policy,
                receipt, self.plan if plan is None else plan,
                environ={VERIFY_KEY_ENV: self.key}, now=self.now)

    def test_valid_signature_does_not_authorize_execution(self):
        result = self.check()
        self.assertEqual(result["status"], "SCOPE_VERIFIED_NOT_AUTHORIZED")
        self.assertFalse(result["permission_to_dispatch"])
        self.assertFalse(result["brain_simulation_authorized"])
        self.assertFalse(result["model_call_authorized"])
        self.assertFalse(result["alpha_submission_authorized"])

    def test_mismatched_scope_cannot_be_signed_around(self):
        plan = {**self.plan, "settings": {
            **self.plan["settings"], "region": "CHN"}}
        with self.assertRaisesRegex(OrchestrationError, "not pinned"):
            self.check(plan=plan)

    def test_simulation_request_drift_is_rejected(self):
        plan = {**self.plan, "request": {"selection": "rank(z)"}}
        with self.assertRaisesRegex(OrchestrationError, "drift"):
            self.check(plan=plan)

    def test_bad_signature_is_rejected(self):
        with self.assertRaisesRegex(OrchestrationError, "signature mismatch"):
            self.check(signature="0" * 64)

    def test_expired_receipt_is_rejected(self):
        claims = {**self.claims,
                  "expires_at": (self.now - timedelta(seconds=1)).isoformat()}
        with self.assertRaisesRegex(OrchestrationError, "expired"):
            self.check(claims=claims)

    def test_must_be_single_simulation_attempt(self):
        claims = {**self.claims, "max_attempts": 2}
        with self.assertRaisesRegex(OrchestrationError, "attempt policy"):
            self.check(claims=claims)


class AtomicSimulationIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "simulation.json"

    def create(self):
        return create_simulation_intent(
            self.path, workflow="super",
            run_id="a" * 32, batch_id="batch_one",
            request={"selection": "rank(x)"},
            settings={"region": "USA", "delay": 1})

    def test_duplicate_intents_compete_under_single_writer_lock(self):
        def attempt(_):
            try:
                self.create()
                return "CREATED"
            except ValueError:
                return "BLOCKED"
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcome = list(executor.map(attempt, range(2)))
        self.assertCountEqual(outcome, ["CREATED", "BLOCKED"])
        self.assertEqual(json.loads(self.path.read_text())["status"], "PREPARED")

    def test_competing_post_reservations_have_one_winner(self):
        self.create()
        def attempt(_):
            try:
                transition_simulation_intent(
                    self.path, expected_status="PREPARED", status="POSTING")
                return "POSTING"
            except ValueError:
                return "BLOCKED"
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcome = list(executor.map(attempt, range(2)))
        self.assertCountEqual(outcome, ["POSTING", "BLOCKED"])
        self.assertEqual(json.loads(self.path.read_text())["status"], "POSTING")

    def test_unknown_outcome_requires_verified_local_response(self):
        self.create()
        transition_simulation_intent(self.path, expected_status="PREPARED",
                                     status="POSTING")
        transition_simulation_intent(self.path, expected_status="POSTING",
                                     status="OUTCOME_UNKNOWN")
        response_path = self.path.with_name("recovered.json")
        response = {"results": [{"alpha_id": "OFFLINE"}]}
        response_path.write_text(json.dumps(response), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "verified response"):
            transition_simulation_intent(
                self.path, expected_status="OUTCOME_UNKNOWN",
                status="RESPONSE_RECEIVED")
        with self.assertRaisesRegex(ValueError, "fingerprint changed"):
            transition_simulation_intent(
                self.path, expected_status="OUTCOME_UNKNOWN",
                status="RESPONSE_RECEIVED", response_ref=str(response_path),
                response_sha256="f" * 64)
        digest = hashlib.sha256(json.dumps(
            response, ensure_ascii=False, sort_keys=True,
            default=str).encode("utf-8")).hexdigest()
        transition_simulation_intent(
            self.path, expected_status="OUTCOME_UNKNOWN",
            status="RESPONSE_RECEIVED", response_ref=str(response_path),
            response_sha256=digest)
        self.assertEqual(json.loads(self.path.read_text())["status"],
                         "RESPONSE_RECEIVED")

    def test_unknown_outcome_rejects_external_response_path(self):
        self.create()
        transition_simulation_intent(self.path, expected_status="PREPARED",
                                     status="POSTING")
        transition_simulation_intent(self.path, expected_status="POSTING",
                                     status="OUTCOME_UNKNOWN")
        external = Path(self.temp.name).parent / "external_recovery.json"
        with self.assertRaisesRegex(ValueError, "next to Intent"):
            transition_simulation_intent(
                self.path, expected_status="OUTCOME_UNKNOWN",
                status="RESPONSE_RECEIVED", response_ref=str(external),
                response_sha256="f" * 64)

    def test_unknown_outcome_cannot_return_to_posting(self):
        self.create()
        transition_simulation_intent(self.path, expected_status="PREPARED",
                                     status="POSTING")
        transition_simulation_intent(self.path, expected_status="POSTING",
                                     status="OUTCOME_UNKNOWN")
        with self.assertRaises(ValueError):
            transition_simulation_intent(
                self.path, expected_status="OUTCOME_UNKNOWN", status="POSTING")


if __name__ == "__main__":
    unittest.main()
