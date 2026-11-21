"""P4.7.2 consent signature and crash-safe audit reservation: offline only."""
import base64
import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.operator_consent import (
    _CONTEXT, inspect_consent_reservation, reserve_consent_for_audit,
    verify_external_consent,
)
from alpha_research.orchestration.simulation_attempt_admission import (
    stage_simulation_attempt, inspect_staged_simulation_attempt,
)
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json
from alpha_research.state.store import atomic_write_json


class ExternalConsentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runner = MainOrchestrator(self.root)
        self.task = self.runner.create(
            "super", {"objective": "offline consent fixture", "region": "USA",
                      "delay": 1, "universe": "TOP3000"})
        self.model = {"offline": "identity-fixture"}
        self.policy = {"max_calls": 1, "max_reported_tokens": 1000,
                       "failover": "manual"}
        self.now = datetime.now(timezone.utc)
        self.private_key = Ed25519PrivateKey.generate()
        key_b64 = base64.b64encode(self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        self.trust = {
            "schema_version": 1,
            "issuers": {"operator-service": {
                "key_id": "staging-test-key", "public_key_b64": key_b64,
                "enabled": True, "operations": ["brain_simulation"]}},
            "revoked_grants": [],
        }
        self.plan = {
            "research_id": "super:" + "a" * 32,
            "run_id": "b" * 32,
            "batch_id": "batch_one",
            "request": {"selection": "rank(x)", "combo": "rank(y)"},
            "settings": {"region": "USA", "delay": 1,
                         "universe": "TOP3000", "instrument_type": "EQUITY"},
        }
        self.stage(self.task, "c" * 32, self.plan)
        self.claims = self._claims(self.task, self.plan, "c" * 32)

    def stage(self, task, scope_id, plan):
        checked = {
            "workflow": task["workflow"],
            "authorization_id": scope_id,
            "research_id": plan["research_id"],
            "simulation_sha256": fingerprint_json({
                "request": plan["request"], "settings": plan["settings"]}),
            "permission_to_dispatch": False,
        }
        with patch(
            "alpha_research.orchestration.simulation_attempt_admission.inspect_simulation_scope",
            return_value=checked,
        ):
            status = stage_simulation_attempt(
                self.runner, task["task_id"], self.model, self.policy,
                {"synthetic": True}, plan)
        self.assertEqual(status["status"], "STAGED_NO_DISPATCH")

    def _claims(self, task, plan, scope_id):
        intent_path = (self.root / "state" / "research_v1_1" /
                       "simulation_attempt_admission" / "intents" /
                       (task["task_id"] + ".intent.json"))
        intent = json.loads(intent_path.read_text())
        return {
            "schema_version": 1, "grant_id": "f" * 32,
            "issuer_id": "operator-service", "key_id": "staging-test-key",
            "operator_approval_id": "e" * 32,
            "operation": "brain_simulation",
            "task_id": task["task_id"], "workflow": task["workflow"],
            "request_sha256": task["request_sha256"],
            "proposal_sha256": "d" * 64,
            "model_profile_sha256": "1" * 64,
            "model_governance_policy_sha256": "2" * 64,
            "research_id": plan["research_id"], "run_id": plan["run_id"],
            "batch_id": plan["batch_id"],
            "scope_authorization_id": scope_id,
            "simulation_sha256": fingerprint_json({
                "request": plan["request"], "settings": plan["settings"]}),
            "intent_sha256": fingerprint_json(intent),
            "scope": {key: plan["settings"][key] for key in (
                "region", "delay", "universe", "instrument_type")},
            "max_attempts": 1, "max_model_calls": 0,
            "allow_alpha_submission": False,
            "issued_at": (self.now - timedelta(seconds=30)).isoformat(),
            "expires_at": (self.now + timedelta(minutes=5)).isoformat(),
        }

    def receipt(self, claims=None, *, signer=None):
        body = copy.deepcopy(self.claims if claims is None else claims)
        key = signer or self.private_key
        signature = key.sign(_CONTEXT + canonical_json_bytes(body))
        return {"claims": body, "signature": base64.b64encode(signature).decode()}

    def _prepared(self):
        return (patch(
            "alpha_research.orchestration.operator_consent.inspect_execution_proposal",
            return_value={"proposal_sha256": "d" * 64}),
            patch(
            "alpha_research.orchestration.operator_consent.inspect_execution_identity",
            return_value={"identity": {
                "model_profile_sha256": "1" * 64,
                "model_governance_policy_sha256": "2" * 64}}))

    def verify(self, receipt=None, trust=None, *, task=None):
        p, i = self._prepared()
        with p, i:
            return verify_external_consent(
                self.runner, (task or self.task)["task_id"],
                self.model, self.policy, receipt or self.receipt(),
                self.trust if trust is None else trust, now=self.now)

    def reserve(self, receipt=None, trust=None, *, task=None):
        p, i = self._prepared()
        with p, i:
            return reserve_consent_for_audit(
                self.runner, (task or self.task)["task_id"],
                self.model, self.policy, receipt or self.receipt(),
                self.trust if trust is None else trust, now=self.now)

    def test_external_signature_never_authorizes_dispatch(self):
        verified = self.verify()
        self.assertEqual(verified["status"],
                         "EXTERNAL_SIGNATURE_VERIFIED_NOT_AUTHORIZED")
        self.assertTrue(verified["signature_verified"])
        for key in ("permission_to_dispatch", "model_call_authorized",
                    "brain_simulation_authorized", "alpha_submission_authorized"):
            self.assertFalse(verified[key])
        self.assertEqual(inspect_consent_reservation(
            self.runner, self.task["task_id"])["status"], "NOT_RESERVED")
        self.assertFalse((self.root / "state" / "research_v1_1" /
                          "operator_consent_audit").exists())

    def test_signature_tampering_and_untrusted_issuer(self):
        bad = self.receipt(signer=Ed25519PrivateKey.generate())
        with self.assertRaisesRegex(OrchestrationError, "signature mismatch"):
            self.verify(bad)
        wrong_key = copy.deepcopy(self.trust)
        wrong_key["issuers"]["operator-service"]["enabled"] = False
        with self.assertRaisesRegex(OrchestrationError, "not trusted"):
            self.verify(trust=wrong_key)
        wrong_issuer = dict(self.claims, issuer_id="unknown-service")
        with self.assertRaisesRegex(OrchestrationError, "not trusted"):
            self.verify(self.receipt(wrong_issuer))

    def test_revocation_and_time_enforced(self):
        trust = copy.deepcopy(self.trust)
        trust["revoked_grants"] = ["f" * 32]
        with self.assertRaisesRegex(OrchestrationError, "revoked"):
            self.verify(trust=trust)
        for changes in (
            {"expires_at": (self.now - timedelta(seconds=1)).isoformat()},
            {"issued_at": (self.now + timedelta(seconds=1)).isoformat()},
            {"expires_at": (self.now + timedelta(hours=1)).isoformat()},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(OrchestrationError, "time window"):
                    self.verify(self.receipt(dict(self.claims, **changes)))

    def test_cost_scope_and_intent_binding(self):
        cases = (
            ({"max_attempts": 2}, "budget"),
            ({"max_model_calls": 1}, "budget"),
            ({"allow_alpha_submission": True}, "budget"),
            ({"operation": "alpha_submission"}, "budget"),
            ({"run_id": "altered"}, "drift"),
            ({"intent_sha256": "a" * 64}, "drift"),
            ({"research_id": "super:" + "b" * 32}, "drift"),
            ({"scope_authorization_id": "0" * 32}, "drift"),
            ({"scope": {**self.claims["scope"], "region": "CHN"}}, "drift"),
        )
        for updates, message in cases:
            with self.subTest(updates=updates):
                with self.assertRaisesRegex(OrchestrationError, message):
                    self.verify(self.receipt(dict(self.claims, **updates)))

    def test_atomic_reservation_once_not_dispatch(self):
        receipt = self.receipt()
        result = self.reserve(receipt)
        self.assertEqual(result["status"], "RESERVED_CONSENT_AUDIT_ONLY")
        self.assertFalse(result["permission_to_dispatch"])
        with self.assertRaisesRegex(OrchestrationError, "already reserved"):
            self.reserve(receipt)
        ledger_path = (self.root / "state" / "research_v1_1" /
                       "operator_consent_audit" / "consumed.json")
        index = json.loads(ledger_path.read_text())
        self.assertEqual(index["by_task"][self.task["task_id"]], "f" * 32)

    def test_grant_replay_on_different_task_refused(self):
        self.reserve()
        other = self.runner.create(
            "super", {"objective": "second", "region": "USA",
                      "delay": 1, "universe": "TOP3000"})
        other_plan = {**self.plan, "run_id": "0" * 32,
                      "research_id": "super:" + "0" * 32}
        self.stage(other, "3" * 32, other_plan)
        claims = self._claims(other, other_plan, "3" * 32)
        with self.assertRaisesRegex(OrchestrationError, "already reserved"):
            self.reserve(self.receipt(claims), task=other)

    def test_same_operator_approval_cannot_fund_another_grant(self):
        self.reserve()
        other = self.runner.create(
            "super", {"objective": "another approved experiment",
                      "region": "USA", "delay": 1, "universe": "TOP3000"})
        other_plan = {**self.plan, "run_id": "0" * 32,
                      "research_id": "super:" + "0" * 32}
        self.stage(other, "3" * 32, other_plan)
        claims = self._claims(other, other_plan, "3" * 32)
        claims["grant_id"] = "4" * 32
        # Approval identity is intentionally reused even though grant differs.
        with self.assertRaisesRegex(OrchestrationError, "already reserved"):
            self.reserve(self.receipt(claims), task=other)

    def test_dispatch_audit_keeps_signed_reservation_non_executable(self):
        from alpha_research.orchestration.simulation_dispatch_preflight import (
            inspect_simulation_dispatch_gate,
        )
        self.reserve()
        result = inspect_simulation_dispatch_gate(
            self.runner, self.task["task_id"], self.policy)
        self.assertEqual(result["status"], "NOT_AUTHORIZED")
        self.assertEqual(
            result["facts"]["operator_consent_audit_status"],
            "RESERVED_CONSENT_AUDIT_ONLY")
        self.assertIn("OPERATOR_CONSENT_AUDIT_NOT_LIVE_AUTHORIZATION",
                      result["blockers"])
        self.assertFalse(result["brain_invoked"])
        self.assertFalse(result["provider_invoked"])
        self.assertFalse(result["permission_to_dispatch"])

    def test_symlinked_consent_directory_rejected(self):
        parent = self.root / "state" / "research_v1_1"
        original = parent / "operator_consent_audit"
        external = self.root / "external-consent-audit"
        external.mkdir()
        try:
            original.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink unavailable")
        with self.assertRaisesRegex(OrchestrationError, "directory unsafe"):
            self.reserve()

    def test_crash_after_canonical_index_refuses_replay(self):
        from alpha_research.orchestration import operator_consent as module
        original = atomic_write_json

        def failing_write(path, obj):
            if Path(path).name == self.task["task_id"] + ".json":
                raise OSError("synthetic crash after canonical index write")
            return original(path, obj)

        p, i = self._prepared()
        with p, i, patch.object(module, "atomic_write_json",
                              side_effect=failing_write):
            with self.assertRaisesRegex(OSError, "synthetic crash"):
                reserve_consent_for_audit(
                    self.runner, self.task["task_id"], self.model,
                    self.policy, self.receipt(), self.trust, now=self.now)
        status = inspect_consent_reservation(self.runner, self.task["task_id"])
        self.assertEqual(status["status"], "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "projection missing"):
            self.reserve()

    def test_missing_index_and_projection_drift_fail_closed(self):
        self.reserve()
        root = self.root / "state" / "research_v1_1" / "operator_consent_audit"
        projection = root / (self.task["task_id"] + ".json")
        record = json.loads(projection.read_text())
        record["permission_to_dispatch"] = True
        projection.write_text(json.dumps(record))
        self.assertEqual(inspect_consent_reservation(
            self.runner, self.task["task_id"])["status"],
            "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "drifted"):
            self.reserve()
        projection.write_text(json.dumps({**record, "permission_to_dispatch": False}))
        (root / "consumed.json").unlink()
        self.assertEqual(inspect_consent_reservation(
            self.runner, self.task["task_id"])["status"],
            "RECONCILIATION_REQUIRED")

    def test_concurrent_reservation_has_single_winner(self):
        receipt = self.receipt()
        p, i = self._prepared()
        def attempt(_):
            try:
                return reserve_consent_for_audit(
                    self.runner, self.task["task_id"], self.model, self.policy,
                    receipt, self.trust, now=self.now)["status"]
            except (OrchestrationError, ValueError):
                return "BLOCKED"
        with p, i, ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(attempt, range(2)))
        self.assertCountEqual(
            outcomes, ["RESERVED_CONSENT_AUDIT_ONLY", "BLOCKED"])

    def test_stop_and_mock_marker_are_rejected(self):
        stop = self.root / "state" / "stop.flag"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("stop")
        with self.assertRaisesRegex(OrchestrationError, "stop signal"):
            self.reserve()
        stop.unlink()
        from alpha_research.orchestration.mock_workspace import marker_path
        marker = marker_path(self.root)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"kind":"OFFLINE_DOMAIN_MOCK_ONLY"}')
        with self.assertRaisesRegex(OrchestrationError, "mock workspace"):
            self.reserve()


if __name__ == "__main__":
    unittest.main()
