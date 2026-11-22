"""P4.25-29 offline trust-boundary integration and negative-permission tests."""
import base64
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alpha_research.agents.registry import bundled_registry_path
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.execution_identity import persist_execution_identity
from alpha_research.orchestration.execution_proposal import persist_execution_proposal
from alpha_research.orchestration.research_attestation_contract import (
    inspect_signed_research_evidence,
)
from alpha_research.orchestration.research_global_consumer import (
    _CONTEXT as CONSUMER_CONTEXT,
    build_global_consumption_request,
)
from alpha_research.orchestration.research_grant_ledger import (
    inspect_research_grant_ledger,
)
from alpha_research.orchestration.research_driver_session import (
    inspect_driver_session_evidence, driver_read_frame_allowed,
)
from alpha_research.orchestration.research_price_provenance import (
    inspect_research_price_provenance,
)
from alpha_research.orchestration.research_remote_reconciliation import (
    inspect_research_remote_reconciliation,
)
from alpha_research.orchestration.research_p4_release import (
    inspect_research_p4_release_gate,
)
from alpha_research.orchestration.research_only_admission import (
    _CONTEXT as GRANT_CONTEXT, build_research_issuer_request,
)
from alpha_research.orchestration.research_sdk_boundary import (
    build_research_sdk_request_plan,
)
from alpha_research.orchestration.runtime_config import (
    prepare_registered_model_runtime,
)
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json


def _pub(private):
    return base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


class ResearchP4FinalModulesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runner = MainOrchestrator(self.root)
        task = self.runner.create(
            "super", {"objective": "offline P4 final",
                      "region": "USA", "delay": 1, "universe": "TOP3000"})
        self.task_id = task["task_id"]
        prepared = prepare_registered_model_runtime(
            {"model_governance": {"max_calls": 2, "max_reported_tokens": 6000,
                                  "failover": "manual"}},
            {"research_lead": "deepseek", "research_executor": "deepseek"},
            bundled_registry_path())
        self.model = prepared["model"]
        self.policy = prepared["model_governance"]
        persist_execution_proposal(self.runner, self.task_id)
        persist_execution_identity(
            self.runner, self.task_id, self.model, self.policy)
        self.now = datetime.now(timezone.utc)
        self.operator_private = Ed25519PrivateKey.generate()
        self.operator_trust = {
            "schema_version": 1, "revoked_grants": [],
            "issuers": {"operator": {
                "key_id": "operator-key",
                "public_key_b64": _pub(self.operator_private),
                "enabled": True,
                "operations": ["research_prepare_model_readonly"],
            }},
        }
        fixed = build_research_issuer_request(
            self.runner, self.task_id, self.model, self.policy)["fixed_claims"]
        self.tariff = {
            "currency": "USD",
            "input_rate_micros_per_million": 2_000_000,
            "output_rate_micros_per_million": 4_000_000,
            "billing_unit": "TOKENS_PER_MILLION",
        }
        self.claims = {
            **fixed,
            "grant_id": "a" * 32,
            "issuer_id": "operator",
            "key_id": "operator-key",
            "operator_approval_id": "b" * 32,
            "issued_at": (self.now - timedelta(seconds=10)).isoformat(),
            "expires_at": (self.now + timedelta(minutes=10)).isoformat(),
            "price_evidence_sha256": fingerprint_json(self.tariff),
            "currency": "USD",
            "input_rate_micros_per_million": 2_000_000,
            "output_rate_micros_per_million": 4_000_000,
            "max_input_tokens_per_call": 1000,
            "max_output_tokens_per_call": 1000,
            "max_total_cost_usd_micros": 15_000,
        }
        self.receipt = {
            "claims": self.claims,
            "signature": base64.b64encode(self.operator_private.sign(
                GRANT_CONTEXT + canonical_json_bytes(self.claims))).decode(),
        }
        self.consumer_private = Ed25519PrivateKey.generate()
        self.consumer_trust = {
            "schema_version": 1,
            "consumers": {"consumer": {
                "key_id": "consumer-key",
                "public_key_b64": _pub(self.consumer_private),
                "enabled": True,
                "operation": "consume_research_grant_once",
            }},
            "revoked_consumptions": [],
        }
        fixed_consumer = build_global_consumption_request(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, now=self.now)["fixed_claims"]
        self.ack_claims = {
            **fixed_consumer,
            "consumer_id": "consumer", "key_id": "consumer-key",
            "consumption_id": "d" * 32,
            "ledger_id": "remote-ledger",
            "ledger_sequence": 1,
            "consumed_at": (self.now - timedelta(seconds=10)).isoformat(),
            "expires_at": (self.now + timedelta(seconds=60)).isoformat(),
            "outcome": "CONSUMED_ONCE",
        }
        self.ack = {
            "claims": self.ack_claims,
            "signature": base64.b64encode(self.consumer_private.sign(
                CONSUMER_CONTEXT + canonical_json_bytes(self.ack_claims))).decode(),
        }

    def external(self, domain, operation, fields):
        private = Ed25519PrivateKey.generate()
        claims = {
            "schema_version": 1, "operation": operation,
            "evidence_id": "e" * 32, "task_id": self.task_id,
            "research_receipt_sha256": fingerprint_json(self.receipt),
            "issuer_id": "external-auditor", "key_id": "external-key",
            "issued_at": (self.now - timedelta(seconds=10)).isoformat(),
            "expires_at": (self.now + timedelta(minutes=1)).isoformat(),
            **fields,
        }
        evidence = {
            "claims": claims,
            "signature": base64.b64encode(private.sign(
                ("alpha_research/" + domain + "/v1:").encode()
                + canonical_json_bytes(claims))).decode(),
        }
        trust = {
            "schema_version": 1, "revoked_evidence": [],
            "issuers": {"external-auditor": {
                "key_id": "external-key", "public_key_b64": _pub(private),
                "enabled": True, "operations": [operation],
            }},
        }
        return evidence, trust

    def ledger(self):
        return self.external("research-grant-ledger",
                             "read_research_grant_ledger", {
            "grant_id": "a" * 32,
            "consumption_id": "d" * 32,
            "ack_sha256": fingerprint_json(self.ack),
            "ledger_id": "remote-ledger",
            "ledger_sequence": 1,
            "consumer_id": "consumer",
            "remote_grant_state": "CONSUMED_ONCE",
            "readback_source": "REMOTE_LEDGER",
        })

    def driver(self):
        from alpha_research.orchestration.research_catalog_protocol import (
            inspect_catalog_read_contract,
        )
        catalog = inspect_catalog_read_contract(self.runner, self.task_id)
        return self.external("research-driver-session",
                             "observe_research_driver_session", {
            "scope": catalog["scope"],
            "source_alpha_id": None,
            "allowed_read_actions": catalog["allowed_read_actions"],
            "session_id": "read-only-session",
            "platform_account_sha256": "1" * 64,
            "driver_build_sha256": "2" * 64,
            "grant_ack_sha256": fingerprint_json(self.ack),
            "session_status": "AUTHENTICATED_READ_ONLY",
        })

    def price(self):
        sdk = build_research_sdk_request_plan(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust,
            task_name="construct_super", declared_input_tokens=700,
            max_output_tokens=900, now=self.now)
        evidence, trust = self.external("research-model-price",
                                        "observe_research_model_tariff", {
            "tariff": self.tariff,
            "tariff_sha256": fingerprint_json(self.tariff),
            "backend_model": sdk["fixed_sdk_options"]["model"],
            "model_profile_sha256": self.claims["model_profile_sha256"],
            "sdk_request_sha256": fingerprint_json(sdk["fixed_sdk_options"]),
            "price_status": "PUBLISHED_TARIFF",
        })
        return evidence, trust

    def readback(self, status="SETTLED", *, completion=800):
        return self.external("research-model-readback",
                             "observe_research_model_outcome", {
            "grant_ack_sha256": fingerprint_json(self.ack),
            "model_attempt_id": "f" * 32,
            "provider_operation_id": "remote-provider-op",
            "remote_status": status,
            "prompt_tokens": 700,
            "completion_tokens": completion,
            "total_tokens": 700 + completion,
            "charged_usd_micros": 4_600,
            "readback_source": "REMOTE_PROVIDER_READBACK",
        })

    def test_p4_25_second_source_ledger_audit_never_dispatches(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        evidence, trust = self.ledger()
        result = inspect_research_grant_ledger(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, self.ack,
            self.consumer_trust, evidence, trust, now=self.now)
        self.assertEqual(result["status"],
                         "GLOBAL_LEDGER_READBACK_SIGNATURE_VERIFIED_AUDIT_ONLY")
        self.assertFalse(result["atomic_single_use_proven"])
        self.assertFalse(result["permission_to_dispatch"])
        changed = copy.deepcopy(evidence)
        changed["claims"]["ledger_sequence"] = True
        with self.assertRaises(OrchestrationError):
            inspect_research_grant_ledger(
                self.runner, self.task_id, self.model, self.policy,
                self.receipt, self.operator_trust, self.ack,
                self.consumer_trust, changed, trust, now=self.now)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_p4_25_ledger_cannot_self_attest_and_revocation(self):
        evidence, trust = self.ledger()
        same_key = copy.deepcopy(trust)
        same_key["issuers"]["external-auditor"]["public_key_b64"] = (
            self.consumer_trust["consumers"]["consumer"]["public_key_b64"])
        with self.assertRaises(OrchestrationError):
            inspect_research_grant_ledger(
                self.runner, self.task_id, self.model, self.policy,
                self.receipt, self.operator_trust, self.ack,
                self.consumer_trust, evidence, same_key, now=self.now)
        trust["revoked_evidence"] = ["e" * 32]
        with self.assertRaisesRegex(OrchestrationError, "revoked"):
            inspect_research_grant_ledger(
                self.runner, self.task_id, self.model, self.policy,
                self.receipt, self.operator_trust, self.ack,
                self.consumer_trust, evidence, trust, now=self.now)

    def test_p4_26_driver_session_exact_scope_and_read_frames(self):
        evidence, trust = self.driver()
        report = inspect_driver_session_evidence(
            self.runner, self.task_id, self.receipt, self.ack,
            evidence, trust, now=self.now)
        self.assertTrue(driver_read_frame_allowed(
            {"cmd": "get_datasets",
             "region": "USA", "delay": 1, "universe": "TOP3000",
             "instrument_type": "EQUITY"}, report))
        for frame in (
            {"cmd": "simulate_super"},
            {"cmd": "get_alpha_details", "alpha_id": "a"},
            {"cmd": "get_datasets", "region": "USA", "delay": True,
             "universe": "TOP3000", "instrument_type": "EQUITY"},
            {"cmd": "get_user_alphas", "stage": "IS", "limit": 10, "offset": 0},
        ):
            with self.subTest(frame=frame):
                self.assertFalse(driver_read_frame_allowed(frame, report))
        self.assertFalse(report["brain_read_authorized"])
        self.assertFalse(report["driver_started"])
        tampered = copy.deepcopy(evidence)
        tampered["claims"]["scope"]["delay"] = True
        with self.assertRaises(OrchestrationError):
            inspect_driver_session_evidence(
                self.runner, self.task_id, self.receipt, self.ack,
                tampered, trust, now=self.now)

    def test_p4_27_tariff_binds_model_and_ceilings(self):
        evidence, trust = self.price()
        report = inspect_research_price_provenance(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, evidence, trust,
            task_name="construct_super", input_tokens=700,
            max_output_tokens=900, now=self.now)
        self.assertEqual(report["worst_case_request_usd_micros"], 5000)
        self.assertFalse(report["global_spend_reserved"])
        self.assertFalse(report["sdk_output_hard_cap_enforced"])
        tampered = copy.deepcopy(evidence)
        tampered["claims"]["backend_model"] = "unregistered"
        with self.assertRaises(OrchestrationError):
            inspect_research_price_provenance(
                self.runner, self.task_id, self.model, self.policy,
                self.receipt, self.operator_trust, tampered, trust,
                task_name="construct_super", input_tokens=700,
                max_output_tokens=900, now=self.now)

    def test_p4_28_unknown_failed_and_overlimit_are_never_retried(self):
        for remote, output, expected in (
            ("SETTLED", 800, "REMOTE_SETTLED_SIGNATURE_VERIFIED_AUDIT_ONLY"),
            ("NOT_FOUND", 800, "RECONCILIATION_REQUIRED_REMOTE_OUTCOME_UNCERTAIN"),
            ("IN_FLIGHT", 800, "RECONCILIATION_REQUIRED_REMOTE_OUTCOME_UNCERTAIN"),
            ("FAILED", 800, "RECONCILIATION_REQUIRED_REMOTE_FAILURE"),
            ("UNKNOWN", 800, "RECONCILIATION_REQUIRED_REMOTE_OUTCOME_UNCERTAIN"),
            ("SETTLED", 1100, "RECONCILIATION_REQUIRED_TOKEN_CEILING_VIOLATION"),
        ):
            with self.subTest(remote=remote, output=output):
                evidence, trust = self.readback(remote, completion=output)
                result = inspect_research_remote_reconciliation(
                    self.task_id, self.receipt, self.ack, "f" * 32,
                    evidence, trust, max_input_tokens=1000,
                    max_output_tokens=1000,
                    max_total_cost_usd_micros=15_000, now=self.now)
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["safe_to_retry"])
                self.assertFalse(result["permission_to_dispatch"])

    def test_p4_28_cross_attempt_replay_and_bad_time_fail_closed(self):
        evidence, trust = self.readback()
        with self.assertRaises(OrchestrationError):
            inspect_research_remote_reconciliation(
                self.task_id, self.receipt, self.ack, "a" * 32,
                evidence, trust, max_input_tokens=1000,
                max_output_tokens=1000,
                    max_total_cost_usd_micros=15_000, now=self.now)
        with self.assertRaisesRegex(OrchestrationError, "expired"):
            inspect_research_remote_reconciliation(
                self.task_id, self.receipt, self.ack, "f" * 32,
                evidence, trust, max_input_tokens=1000,
                max_output_tokens=1000,
                    max_total_cost_usd_micros=15_000,
                    now=self.now + timedelta(minutes=10))

    def test_p4_29_integrated_canary_stays_blocked_even_with_four_audits(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        missing = inspect_research_p4_release_gate(self.runner, self.task_id)
        self.assertEqual(len(missing["components"]), 4)
        self.assertFalse(missing["all_four_evidence_audits_supplied"])
        self.assertIn("INDEPENDENT_OPERATOR_CANARY_APPROVAL_MISSING",
                      missing["blockers"])
        ledger_evidence, ledger_trust = self.ledger()
        driver_evidence, driver_trust = self.driver()
        price_evidence, price_trust = self.price()
        readback_evidence, readback_trust = self.readback()
        ledger = inspect_research_grant_ledger(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, self.ack, self.consumer_trust,
            ledger_evidence, ledger_trust, now=self.now)
        driver = inspect_driver_session_evidence(
            self.runner, self.task_id, self.receipt, self.ack,
            driver_evidence, driver_trust, now=self.now)
        price = inspect_research_price_provenance(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, price_evidence, price_trust,
            task_name="construct_super", input_tokens=700,
            max_output_tokens=900, now=self.now)
        outcome = inspect_research_remote_reconciliation(
            self.task_id, self.receipt, self.ack, "f" * 32,
            readback_evidence, readback_trust,
            max_input_tokens=1000, max_output_tokens=1000,
                    max_total_cost_usd_micros=15_000, now=self.now)
        components = {
            "grant_ledger": ledger, "driver_session": driver,
            "model_pricing": price, "remote_reconciliation": outcome,
        }
        approval, trust = self.external(
            "research-readonly-canary", "review_research_readonly_canary", {
                "component_evidence_sha256": {
                    key: report["evidence_sha256"]
                    for key, report in components.items()},
                "canary_profile": "RESEARCH_READ_ONLY",
                "operator_stop_state": "ARMED_NOT_TRIGGERED",
                "max_model_calls": 2,
                "max_cost_usd_micros": 15_000,
                "canary_status": "REVIEWED_NOT_ENABLED",
            })
        report = inspect_research_p4_release_gate(
            self.runner, self.task_id, receipt=self.receipt,
            canary_approval=approval, canary_trust=trust,
            now=self.now, **components)
        self.assertTrue(report["all_four_evidence_audits_supplied"])
        self.assertEqual(report["status"],
                         "RESEARCH_P4_CANARY_PRODUCTION_BLOCKED")
        self.assertEqual(report["canary_review"]["status"],
                         "EXTERNAL_SIGNATURE_VERIFIED_AUDIT_ONLY")
        self.assertFalse(report["canary_live_enabled"])
        self.assertFalse(report["production_authorized"])
        self.assertFalse(report["automatic_retry_allowed"])
        self.assertFalse(report["brain_simulation_authorized"])
        tampered = copy.deepcopy(driver)
        tampered["task_id"] = "f" * 32
        with self.assertRaises(OrchestrationError):
            inspect_research_p4_release_gate(
                self.runner, self.task_id, receipt=self.receipt,
                canary_approval=approval, canary_trust=trust,
                now=self.now, **{**components, "driver_session": tampered})
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_signed_record_bool_identity_forgery_and_key_revocation(self):
        evidence, trust = self.driver()
        expected = {
            "task_id": self.task_id,
            "research_receipt_sha256": fingerprint_json(self.receipt),
        }
        fields = (set(evidence["claims"])
                  - {"schema_version", "operation", "evidence_id", "task_id",
                     "research_receipt_sha256", "issuer_id", "key_id",
                     "issued_at", "expires_at"})
        with self.assertRaises(OrchestrationError):
            inspect_signed_research_evidence(
                evidence, trust, domain="research-model-price",
                operation="observe_research_driver_session",
                fields=frozenset(fields), expected=expected, now=self.now)
        trust["revoked_evidence"] = ["e" * 32]
        with self.assertRaisesRegex(OrchestrationError, "revoked"):
            inspect_driver_session_evidence(
                self.runner, self.task_id, self.receipt, self.ack,
                evidence, trust, now=self.now)


    def test_p4_25_external_reference_consumer_atomic_across_connections(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from alpha_research.orchestration.research_grant_service_reference import (
            ReferenceExternalGrantLedger,
        )
        from alpha_research.orchestration.research_global_consumer import (
            verify_global_consumption_ack,
        )
        handoff = build_global_consumption_request(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, now=self.now)
        db = self.root / "independent-service" / "grant.sqlite3"
        def new_service():
            return ReferenceExternalGrantLedger(
                db, signer=self.consumer_private,
                operator_trust=self.operator_trust,
                consumer_id="consumer", key_id="consumer-key",
                ledger_id="remote-ledger")
        new_service().initialize_schema()
        barrier = Barrier(2)
        def consume():
            barrier.wait(timeout=10)
            try:
                return new_service().consume_once(
                    handoff, self.receipt, now=self.now)
            except OrchestrationError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(consume) for _ in range(2)]
            results = [f.result() for f in futures]
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(new_service().read_consumption("a" * 32)["ack"], winners[0])
        verified = verify_global_consumption_ack(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, winners[0],
            self.consumer_trust, now=self.now)
        self.assertEqual(verified["status"],
                         "GLOBAL_ACK_SIGNATURE_VERIFIED_AUDIT_ONLY")
        self.assertFalse(verified["model_call_authorized"])
        new_service().revoke("a" * 32, now=self.now)
        self.assertEqual(new_service().read_consumption("a" * 32)["status"], "REVOKED")
        with self.assertRaises(OrchestrationError):
            new_service().consume_once(handoff, self.receipt, now=self.now)

    def test_p4_25_reference_revoke_before_consume(self):
        from alpha_research.orchestration.research_grant_service_reference import (
            ReferenceExternalGrantLedger,
        )
        service = ReferenceExternalGrantLedger(
            self.root / "isolated" / "ledger.sqlite3",
            signer=self.consumer_private, operator_trust=self.operator_trust,
            consumer_id="consumer", key_id="consumer-key", ledger_id="remote-ledger")
        service.initialize_schema()
        service.revoke("a" * 32, now=self.now)
        handoff = build_global_consumption_request(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, now=self.now)
        with self.assertRaises(OrchestrationError):
            service.consume_once(handoff, self.receipt, now=self.now)
        self.assertIsNone(service.read_consumption("a" * 32)["ack"])


    def test_p4_27_external_reference_spend_reservation_atomic(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from alpha_research.orchestration.research_spend_service_reference import (
            ReferenceExternalSpendLedger,
        )
        path = self.root / "external-metering" / "metering.sqlite3"
        def new_ledger():
            return ReferenceExternalSpendLedger(
                path, grant_id="a" * 32, max_model_calls=2,
                max_total_cost_usd_micros=9_000,
                global_max_total_cost_usd_micros=9_000)
        new_ledger().initialize_schema()
        barrier = Barrier(2)
        def reserve(attempt_id):
            barrier.wait(timeout=10)
            try:
                return new_ledger().reserve_once(
                    task_id=self.task_id, attempt_id=attempt_id,
                    worst_case_usd_micros=5_000)
            except OrchestrationError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(reserve, "1" * 32)
            b = pool.submit(reserve, "2" * 32)
            results = [a.result(), b.result()]
        self.assertEqual(sum(result is not None for result in results), 1)
        snapshot = new_ledger().snapshot()
        self.assertEqual(snapshot["reserved_attempts"], 1)
        self.assertEqual(snapshot["committed_reservation_usd_micros"], 5_000)
        winner_id = next(result["attempt_id"] for result in results if result)
        with self.assertRaises(OrchestrationError):
            new_ledger().reserve_once(
                task_id=self.task_id, attempt_id=winner_id,
                worst_case_usd_micros=1_000)
        unknown = new_ledger().mark_unknown(winner_id)
        self.assertFalse(unknown["automatic_retry_allowed"])
        self.assertEqual(new_ledger().snapshot()["by_status"]["UNKNOWN"], 1)
        with self.assertRaises(OrchestrationError):
            new_ledger().settle_once(winner_id, 100)
        with self.assertRaises(OrchestrationError):
            new_ledger().reserve_once(
                task_id=self.task_id, attempt_id="3" * 32,
                worst_case_usd_micros=5_000)

    def test_p4_27_reference_spend_settlement_keeps_reservation_capacity(self):
        from alpha_research.orchestration.research_spend_service_reference import (
            ReferenceExternalSpendLedger,
        )
        ledger = ReferenceExternalSpendLedger(
            self.root / "meter.sqlite3", grant_id="a" * 32,
            max_model_calls=1, max_total_cost_usd_micros=5_000,
            global_max_total_cost_usd_micros=5_000)
        ledger.initialize_schema()
        report = ledger.reserve_once(
            task_id=self.task_id, attempt_id="1" * 32,
            worst_case_usd_micros=5_000)
        self.assertFalse(report["permission_to_dispatch"])
        with self.assertRaises(OrchestrationError):
            ledger.settle_once("1" * 32, 5_001)
        settled = ledger.settle_once("1" * 32, 2_000)
        self.assertFalse(settled["provider_invoice_independently_verified"])
        self.assertEqual(ledger.snapshot()["committed_reservation_usd_micros"], 5_000)
        with self.assertRaises(OrchestrationError):
            ledger.reserve_once(
                task_id=self.task_id, attempt_id="2" * 32,
                worst_case_usd_micros=1_000)


    def test_p4_27_global_usd_ceiling_across_different_grants(self):
        from alpha_research.orchestration.research_spend_service_reference import (
            ReferenceExternalSpendLedger,
        )
        shared = self.root / "global-spend.sqlite3"
        def ledger(grant_id):
            return ReferenceExternalSpendLedger(
                shared, grant_id=grant_id, max_model_calls=2,
                max_total_cost_usd_micros=7_000,
                global_max_total_cost_usd_micros=7_000)
        ledger("a" * 32).initialize_schema()
        ledger("b" * 32).initialize_schema()
        ledger("a" * 32).reserve_once(
            task_id=self.task_id, attempt_id="1" * 32,
            worst_case_usd_micros=4_000)
        with self.assertRaises(OrchestrationError):
            ledger("b" * 32).reserve_once(
                task_id=self.task_id, attempt_id="2" * 32,
                worst_case_usd_micros=4_000)
        self.assertEqual(
            ledger("b" * 32).snapshot()["global_committed_reservation_usd_micros"],
            4_000)
        self.assertFalse(ledger("b" * 32).snapshot()["permission_to_dispatch"])


    def test_p4_27_reopened_ledger_cannot_raise_persisted_caps(self):
        from alpha_research.orchestration.research_spend_service_reference import (
            ReferenceExternalSpendLedger,
        )
        path = self.root / "persistent-policy.sqlite3"
        def ledger(grant_cap, global_cap):
            return ReferenceExternalSpendLedger(
                path, grant_id="a" * 32, max_model_calls=1,
                max_total_cost_usd_micros=grant_cap,
                global_max_total_cost_usd_micros=global_cap)
        ledger(3_000, 5_000).initialize_schema()
        with self.assertRaises(OrchestrationError):
            ledger(10_000, 20_000).initialize_schema()
        with self.assertRaises(OrchestrationError):
            ledger(10_000, 20_000).reserve_once(
                task_id=self.task_id, attempt_id="9" * 32,
                worst_case_usd_micros=9_000)
        self.assertEqual(ledger(3_000, 5_000).snapshot()["reserved_attempts"], 0)


    def _p5_service(self, *, global_cap=9_000):
        from threading import Thread
        from alpha_research.orchestration.research_authority_transport import (
            ResearchAuthorityApplication, make_loopback_authority_server,
        )
        from alpha_research.orchestration.research_authority_client import (
            LoopbackAuthorityClient,
        )
        ledger_signer = Ed25519PrivateKey.generate()
        app = ResearchAuthorityApplication(
            self.root / "authority-pilot.sqlite3",
            consumer_signer=self.consumer_private,
            ledger_signer=ledger_signer, operator_trust=self.operator_trust,
            consumer_id="consumer", consumer_key_id="consumer-key",
            ledger_id="remote-ledger",
            ledger_issuer_id="separate-ledger",
            ledger_key_id="ledger-key",
            global_max_usd_micros=global_cap)
        app.initialize_schema()
        api_token = "api-" + "1" * 48
        admin_token = "admin-" + "2" * 48
        server = make_loopback_authority_server(
            app, api_token=api_token, admin_token=admin_token)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, timeout=5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = LoopbackAuthorityClient(
            "127.0.0.1", server.server_address[1],
            api_token=api_token, admin_token=admin_token)
        return app, server, client, ledger_signer, api_token, admin_token

    def test_p5_1_loopback_grant_ack_readback_budget_and_revoke(self):
        from alpha_research.orchestration.research_grant_ledger import (
            inspect_research_grant_ledger,
        )
        from alpha_research.state.fingerprint import fingerprint_json
        app, server, client, ledger_signer, _api, _admin = self._p5_service()
        handoff = build_global_consumption_request(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, now=self.now)
        consumed = client.consume(handoff, self.receipt)
        self.assertFalse(consumed["permission_to_dispatch"])
        ack = consumed["ack"]
        self.assertEqual(ack["claims"]["grant_id"], "a" * 32)
        evidence = client.readback("a" * 32)["evidence"]
        ledger_trust = {
            "schema_version": 1, "revoked_evidence": [],
            "issuers": {"separate-ledger": {
                "key_id": "ledger-key",
                "public_key_b64": _pub(ledger_signer),
                "enabled": True, "operations": ["read_research_grant_ledger"],
            }},
        }
        audited = inspect_research_grant_ledger(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, ack, self.consumer_trust,
            evidence, ledger_trust, now=datetime.now(timezone.utc))
        self.assertFalse(audited["permission_to_dispatch"])
        self.assertEqual(audited["status"],
                         "GLOBAL_LEDGER_READBACK_SIGNATURE_VERIFIED_AUDIT_ONLY")
        reserved = client.reserve(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="1" * 32, input_tokens=700,
            max_output_tokens=900, ack_sha256=fingerprint_json(ack))
        self.assertEqual(reserved["total_reserved_worst_case_usd_micros"], 5_000)
        self.assertFalse(reserved["permission_to_dispatch"])
        self.assertEqual(client.snapshot("a" * 32)["reserved_attempts"], 1)
        with self.assertRaises(OrchestrationError):
            client.consume(handoff, self.receipt)
        with self.assertRaises(OrchestrationError):
            client.reserve(
                grant_id="a" * 32, task_id=self.task_id,
                attempt_id="2" * 32, input_tokens=700,
                max_output_tokens=900, ack_sha256=fingerprint_json(ack))
        self.assertEqual(client.revoke("a" * 32)["status"], "REVOKED")
        with self.assertRaises(OrchestrationError):
            client.readback("a" * 32)
        with self.assertRaises(OrchestrationError):
            client.snapshot("a" * 32)
        self.assertFalse(app.grants.read_consumption("a" * 32)["dispatch_authorized"])

    def test_p5_1_transport_authentication_and_request_filtering(self):
        import http.client
        import json
        from alpha_research.orchestration.research_authority_client import (
            LoopbackAuthorityClient,
        )
        app, server, client, _ledger, api, admin = self._p5_service()
        self.assertIsNotNone(app)
        port = server.server_address[1]
        with self.assertRaises(OrchestrationError):
            LoopbackAuthorityClient(
                "0.0.0.0", port, api_token=api)
        with self.assertRaises(OrchestrationError):
            LoopbackAuthorityClient(
                "127.0.0.1", port, api_token="short")
        with self.assertRaises(OrchestrationError):
            client.revoke("z" * 32)
        noadmin = LoopbackAuthorityClient(
            "127.0.0.1", port, api_token=api)
        with self.assertRaises(OrchestrationError):
            noadmin.revoke("a" * 32)
        wrong = LoopbackAuthorityClient(
            "127.0.0.1", port, api_token="wrong-" + "9" * 48)
        with self.assertRaises(OrchestrationError):
            wrong.readback("a" * 32)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            # Duplicate keys, including conflicting grant scopes, are refused.
            raw = b'{"grant_id":"a","grant_id":"b"}'
            conn.request(
                "POST", "/v1/grant/readback", body=raw,
                headers={"Authorization": "Bearer " + api,
                         "Content-Type": "application/json"})
            response = conn.getresponse()
            body = response.read()
            self.assertEqual(response.status, 409)
            self.assertNotIn(api.encode(), body)
        finally:
            conn.close()
        self.assertFalse(client.revoke("a" * 32)["permission_to_dispatch"])

    def test_p5_1_server_refuses_replayed_or_mismatched_spend(self):
        from alpha_research.state.fingerprint import fingerprint_json
        _app, _server, client, _ledger, _api, _admin = self._p5_service(
            global_cap=20_000)
        handoff = build_global_consumption_request(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, now=self.now)
        ack = client.consume(handoff, self.receipt)["ack"]
        good = {
            "grant_id": "a" * 32, "task_id": self.task_id,
            "attempt_id": "3" * 32,
            "input_tokens": 700, "max_output_tokens": 900,
            "ack_sha256": fingerprint_json(ack),
        }
        for patch in (
            {"task_id": "e" * 32},
            {"max_output_tokens": 1001},
            {"input_tokens": True},
            {"ack_sha256": "f" * 64},
            {"attempt_id": "../escape"},
        ):
            with self.subTest(patch=patch):
                with self.assertRaises(OrchestrationError):
                    client.reserve(**{**good, **patch})
        self.assertEqual(client.snapshot("a" * 32)["reserved_attempts"], 0)
        client.reserve(**good)
        with self.assertRaises(OrchestrationError):
            client.reserve(**good)
        self.assertEqual(client.snapshot("a" * 32)["reserved_attempts"], 1)

    def test_p5_1_corrupted_handoff_does_not_consume_grant(self):
        app, _server, client, _ledger, _api, _admin = self._p5_service()
        handoff = build_global_consumption_request(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, now=self.now)
        tampered = copy.deepcopy(handoff)
        tampered["fixed_claims"]["task_id"] = "f" * 32
        with self.assertRaises(OrchestrationError):
            client.consume(tampered, self.receipt)
        self.assertIsNone(app.grants.read_consumption("a" * 32)["ack"])


if __name__ == "__main__":
    unittest.main()
