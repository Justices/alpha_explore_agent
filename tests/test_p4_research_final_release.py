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


    def _p5_service(self, *, global_cap=9_000, recovery_trust=None):
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
            global_max_usd_micros=global_cap,
            recovery_trust=recovery_trust)
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


    def test_p5_2_driver_ingress_validates_entire_signed_read_chain(self):
        from alpha_research.integrations.brain import driver
        from unittest.mock import patch
        ledger_evidence, ledger_trust = self.ledger()
        session_evidence, session_trust = self.driver()
        scope = {"region": "USA", "delay": 1, "universe": "TOP3000",
                 "instrument_type": "EQUITY"}
        def inspect(frame, *, receipt=None, session=None):
            return driver.inspect_research_catalog_evidence_frame(
                self.runner, self.task_id, self.model, self.policy,
                self.receipt if receipt is None else receipt,
                self.operator_trust, self.ack, self.consumer_trust,
                ledger_evidence, ledger_trust,
                session_evidence if session is None else session,
                session_trust, frame, now=self.now)
        with (patch.object(driver, "PlatformRuntime",
                           side_effect=AssertionError("BRAIN client forbidden")),
              patch.object(driver, "WorldQuantClient",
                           side_effect=AssertionError("credentials forbidden"))):
            valid = inspect({"cmd": "get_datasets", **scope})
            self.assertEqual(
                valid["status"], "DRIVER_READ_FRAME_POLICY_VALIDATED_NO_LIVE")
            self.assertTrue(valid["frame_policy_valid"])
            self.assertFalse(valid["brain_read_authorized"])
            self.assertFalse(valid["permission_to_dispatch"])
            for frame in (
                {"cmd": "simulate_super"},
                {"cmd": "tag_alpha", "alpha_id": "alpha1"},
                {"cmd": "get_datasets", **scope, "grant_id": "a" * 32},
                {"cmd": "get_datasets", **{**scope, "delay": True}},
                {"cmd": "get_datasets", **{**scope, "region": "CHN"}},
                {"cmd": "get_alpha_details", "alpha_id": "another-alpha"},
            ):
                with self.subTest(frame=frame):
                    rejected = inspect(frame)
                    self.assertEqual(rejected["status"], "DRIVER_READ_FRAME_DENIED")
                    self.assertFalse(rejected["frame_policy_valid"])
                    self.assertFalse(rejected["permission_to_dispatch"])
        corrupted = copy.deepcopy(session_evidence)
        corrupted["claims"]["scope"]["universe"] = "TOP500"
        with self.assertRaises(OrchestrationError):
            inspect({"cmd": "get_datasets", **scope}, session=corrupted)
        changed_receipt = copy.deepcopy(self.receipt)
        changed_receipt["claims"]["grant_id"] = "c" * 32
        with self.assertRaises(OrchestrationError):
            inspect({"cmd": "get_datasets", **scope}, receipt=changed_receipt)


    def test_p5_1_pilot_launcher_requires_distinct_protected_keys(self):
        import json
        import os
        from alpha_research.orchestration.research_authority_server import (
            load_pilot_application,
        )
        consumer = self.root / "consumer-key.pem"
        ledger = self.root / "ledger-key.pem"
        trust_path = self.root / "operator-trust.json"
        config_path = self.root / "pilot-config.json"
        for path, key in ((consumer, self.consumer_private),
                          (ledger, Ed25519PrivateKey.generate())):
            path.write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            if os.name != "nt":
                path.chmod(0o600)
        trust_path.write_text(json.dumps(self.operator_trust), encoding="utf-8")
        cfg = {
            "schema_version": 1,
            "database_file": str((self.root / "pilot.sqlite3").resolve()),
            "operator_trust_file": str(trust_path.resolve()),
            "consumer_private_key_file": str(consumer.resolve()),
            "ledger_private_key_file": str(ledger.resolve()),
            "consumer_id": "consumer",
            "consumer_key_id": "consumer-key",
            "ledger_id": "remote-ledger",
            "ledger_issuer_id": "separate-ledger",
            "ledger_key_id": "ledger-key",
            "global_max_usd_micros": 9_000,
        }
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        app = load_pilot_application(str(config_path.resolve()))
        app.initialize_schema()
        self.assertTrue((self.root / "pilot.sqlite3").exists())
        bad = copy.deepcopy(cfg)
        bad["consumer_private_key_file"] = str(ledger.resolve())
        config_path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(OrchestrationError):
            load_pilot_application(str(config_path.resolve()))
        if os.name != "nt":
            config_path.write_text(json.dumps(cfg), encoding="utf-8")
            consumer.chmod(0o644)
            with self.assertRaises(OrchestrationError):
                load_pilot_application(str(config_path.resolve()))


    def _p5_sdk_inputs(self, *, global_cap=20_000):
        from alpha_research.orchestration.research_global_consumer import (
            build_global_consumption_request,
        )
        app, server, client, ledger_signer, api, admin = self._p5_service(
            global_cap=global_cap)
        handoff = build_global_consumption_request(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, now=self.now)
        ack = client.consume(handoff, self.receipt)["ack"]
        ledger_evidence = client.readback("a" * 32)["evidence"]
        ledger_trust = {
            "schema_version": 1, "revoked_evidence": [],
            "issuers": {"separate-ledger": {
                "key_id": "ledger-key",
                "public_key_b64": _pub(ledger_signer),
                "enabled": True, "operations": ["read_research_grant_ledger"],
            }},
        }
        price_evidence, price_trust = self.price()
        return app, client, {
            "orchestrator": self.runner,
            "task_id": self.task_id,
            "model_config": self.model,
            "governance_policy": self.policy,
            "receipt": self.receipt,
            "operator_trust": self.operator_trust,
            "ack": ack,
            "consumer_trust": self.consumer_trust,
            "ledger_evidence": ledger_evidence,
            "ledger_trust": ledger_trust,
            "price_evidence": price_evidence,
            "price_trust": price_trust,
            "authority_client": client,
            "task_name": "construct_super",
            "declared_input_tokens": 700,
            "max_output_tokens": 900,
            "messages": [{"role": "system", "content": "Build offline alpha only"},
                         {"role": "user", "content": "Construct one expression"}],
            "fixture": {
                "content": "rank(close)",
                "prompt_tokens": 650,
                "completion_tokens": 500,
                "finish_reason": "stop",
            },
            # ACK is issued by the real loopback service *after* setUp().
            # Audit at the current clock, never backdate before consumption.
            "now": datetime.now(timezone.utc),
        }

    def test_p5_3_real_sdk_mock_transport_uses_budget_and_reports_usage(self):
        from alpha_research.orchestration.research_sdk_mock_pilot import (
            run_research_sdk_offline_pilot,
        )
        _app, client, inputs = self._p5_sdk_inputs()
        output = run_research_sdk_offline_pilot(
            **{**inputs, "attempt_id": "5" * 32})
        self.assertEqual(output["status"],
                         "SDK_MOCK_COMPLETED_COST_HELD_AUDIT_ONLY")
        self.assertEqual(output["sdk_http_requests"], 1)
        self.assertEqual(output["sdk_max_retries"], 0)
        self.assertEqual(output["reserved_usd_micros"], 5_000)
        self.assertEqual(output["provider_reported_usd_micros"], 3_300)
        self.assertFalse(output["provider_invoked"])
        self.assertFalse(output["permission_to_dispatch"])
        self.assertFalse(output["independent_provider_billing_verified"])
        self.assertEqual(client.snapshot("a" * 32)["by_status"],
                         {"RESERVED": 1})
        persisted = client.attempt_readback(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="5" * 32, ack_sha256=fingerprint_json(inputs["ack"]))
        self.assertEqual(persisted["provider_operation_id"],
                         "chatcmpl-offline-" + "5" * 32)
        self.assertEqual(persisted["provider_operation_source"], "SDK_REPORTED")
        self.assertFalse(persisted["safe_to_retry"])
        with self.assertRaises(OrchestrationError):
            run_research_sdk_offline_pilot(
                **{**inputs, "attempt_id": "5" * 32})
        self.assertEqual(client.snapshot("a" * 32)["reserved_attempts"], 1)

    def test_p5_3_mock_sdk_timeout_quarantines_without_retry(self):
        from alpha_research.orchestration.research_sdk_mock_pilot import (
            run_research_sdk_offline_pilot,
        )
        _app, client, inputs = self._p5_sdk_inputs()
        with self.assertRaisesRegex(OrchestrationError, "no retry"):
            run_research_sdk_offline_pilot(
                **{**inputs, "attempt_id": "6" * 32,
                   "fixture": None, "fault": "timeout"})
        self.assertEqual(client.snapshot("a" * 32)["by_status"],
                         {"UNKNOWN": 1})
        with self.assertRaises(OrchestrationError):
            run_research_sdk_offline_pilot(
                **{**inputs, "attempt_id": "6" * 32})
        self.assertEqual(client.snapshot("a" * 32)["reserved_attempts"], 1)

    def test_p5_3_bad_usage_and_truncated_completion_quarantine(self):
        from alpha_research.orchestration.research_sdk_mock_pilot import (
            run_research_sdk_offline_pilot,
        )
        _app, client, inputs = self._p5_sdk_inputs()
        for index, extra in enumerate((
            {"finish_reason": "length"},
            {"prompt_tokens": 701},
        )):
            with self.subTest(extra=extra):
                fixture = {**inputs["fixture"], **extra}
                with self.assertRaisesRegex(OrchestrationError, "quarantined"):
                    run_research_sdk_offline_pilot(
                        **{**inputs, "attempt_id": str(index + 7) * 32,
                           "fixture": fixture})
        snap = client.snapshot("a" * 32)
        self.assertEqual(snap["by_status"], {"UNKNOWN": 2})
        self.assertEqual(snap["committed_reservation_usd_micros"], 10_000)

    def test_p5_3_rejects_forged_pricing_and_unsafe_messages_before_reserve(self):
        from alpha_research.orchestration.research_sdk_mock_pilot import (
            run_research_sdk_offline_pilot,
        )
        _app, client, inputs = self._p5_sdk_inputs()
        forged = copy.deepcopy(inputs["price_evidence"])
        forged["claims"]["tariff"]["input_rate_micros_per_million"] = 0
        for changed in (
            {"price_evidence": forged},
            {"messages": [{"role": "tool", "content": "bypass"}]},
            {"messages": [{"role": "user", "content": "hello",
                           "tool_calls": [{"name": "run"}]}]},
            {"declared_input_tokens": True},
            {"max_output_tokens": 1001},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(OrchestrationError):
                    run_research_sdk_offline_pilot(
                        **{**inputs, **changed, "attempt_id": "9" * 32})
        self.assertEqual(client.snapshot("a" * 32)["reserved_attempts"], 0)

    def test_p5_3_quarantine_rejects_wrong_task_and_replay(self):
        from alpha_research.state.fingerprint import fingerprint_json
        _app, client, inputs = self._p5_sdk_inputs()
        reserved = client.reserve(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="a" * 32, input_tokens=700,
            max_output_tokens=900,
            ack_sha256=fingerprint_json(inputs["ack"]))
        self.assertEqual(reserved["status"], "SPEND_RESERVED_REFERENCE_ONLY")
        with self.assertRaises(OrchestrationError):
            client.quarantine(
                grant_id="a" * 32, task_id="e" * 32,
                attempt_id="a" * 32, ack_sha256=fingerprint_json(inputs["ack"]))
        with self.assertRaises(OrchestrationError):
            client.quarantine(
                grant_id="a" * 32, task_id=self.task_id,
                attempt_id="a" * 32, ack_sha256="f" * 64)
        result = client.quarantine(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="a" * 32, ack_sha256=fingerprint_json(inputs["ack"]))
        self.assertFalse(result["automatic_retry_allowed"])
        with self.assertRaises(OrchestrationError):
            client.quarantine(
                grant_id="a" * 32, task_id=self.task_id,
                attempt_id="a" * 32, ack_sha256=fingerprint_json(inputs["ack"]))
        self.assertEqual(client.snapshot("a" * 32)["by_status"], {"UNKNOWN": 1})


    def _p5_recovery_fixture(self, *, consume=True):
        from alpha_research.orchestration.research_global_consumer import (
            build_global_consumption_request,
        )
        from alpha_research.state.fingerprint import fingerprint_json
        private = Ed25519PrivateKey.generate()
        trust = {
            "schema_version": 1, "revoked_evidence": [],
            "issuers": {"remote-provider": {
                "key_id": "provider-readback-key",
                "public_key_b64": _pub(private),
                "enabled": True, "operations": ["observe_research_model_outcome"],
            }},
        }
        app, server, client, _, api, admin = self._p5_service(
            global_cap=20_000, recovery_trust=trust)
        handoff = build_global_consumption_request(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, now=self.now)
        ack = client.consume(handoff, self.receipt)["ack"] if consume else None
        operation_id = "chatcmpl-offline-" + "5" * 32

        def readback(*, attempt_id="5" * 32, provider_operation_id=None,
                     remote_status="SETTLED", prompt_tokens=700,
                     completion_tokens=500, charged_usd_micros=3400,
                     evidence_id=None):
            clock = datetime.now(timezone.utc)
            signed = {
                "schema_version": 1,
                "operation": "observe_research_model_outcome",
                "evidence_id": evidence_id or "f" * 32,
                "task_id": self.task_id,
                "research_receipt_sha256": fingerprint_json(self.receipt),
                "issuer_id": "remote-provider",
                "key_id": "provider-readback-key",
                "issued_at": (clock - timedelta(seconds=1)).isoformat(),
                "expires_at": (clock + timedelta(seconds=90)).isoformat(),
                "grant_ack_sha256": fingerprint_json(ack),
                "model_attempt_id": attempt_id,
                "provider_operation_id": (provider_operation_id
                                          if provider_operation_id is not None
                                          else operation_id),
                "remote_status": remote_status,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "charged_usd_micros": charged_usd_micros,
                "readback_source": "REMOTE_PROVIDER_READBACK",
            }
            return {
                "claims": signed,
                "signature": base64.b64encode(private.sign(
                    b"alpha_research/research-model-readback/v1:"
                    + canonical_json_bytes(signed))).decode(),
            }

        def reserve(attempt="5" * 32):
            return client.reserve(
                grant_id="a" * 32, task_id=self.task_id,
                attempt_id=attempt, input_tokens=700,
                max_output_tokens=900,
                ack_sha256=fingerprint_json(ack))

        def inspect(attempt="5" * 32):
            return client.attempt_readback(
                grant_id="a" * 32, task_id=self.task_id,
                attempt_id=attempt, ack_sha256=fingerprint_json(ack))

        def reconcile(evidence, attempt="5" * 32):
            return client.reconcile_remote_observation(
                grant_id="a" * 32, task_id=self.task_id,
                attempt_id=attempt, ack_sha256=fingerprint_json(ack),
                evidence=evidence)

        return app, client, trust, ack, readback, reserve, inspect, reconcile

    def test_p5_4_sdk_operation_persists_without_billing_settlement(self):
        from alpha_research.orchestration.research_sdk_mock_pilot import (
            run_research_sdk_offline_pilot,
        )
        from alpha_research.state.fingerprint import fingerprint_json
        _app, client, _trust, _ack, _signed, _reserve, _inspect, _reconcile = (
            self._p5_recovery_fixture())
        # Use an existing P5.3 helper to build signed tariff/SDK claims,
        # but its own authority and Grant are independent. Do not merge them.
        # Instead, persist a matching mock operation on the same recovery DB.
        reserve = client.reserve(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="5" * 32, input_tokens=700, max_output_tokens=900,
            ack_sha256=fingerprint_json(_ack))
        self.assertEqual(reserve["reserved_usd_micros"], 5000)
        bound = client.bind_provider_operation(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="5" * 32, ack_sha256=fingerprint_json(_ack),
            provider_operation_id="chatcmpl-offline-" + "5" * 32)
        self.assertFalse(bound["permission_to_dispatch"])
        state = _inspect()
        self.assertEqual(state["provider_operation_id"],
                         "chatcmpl-offline-" + "5" * 32)
        self.assertEqual(state["provider_operation_source"], "SDK_REPORTED")
        self.assertEqual(state["spend_state"], "RESERVED")
        self.assertFalse(state["billing_independently_settled"])
        with self.assertRaises(OrchestrationError):
            client.bind_provider_operation(
                grant_id="a" * 32, task_id=self.task_id,
                attempt_id="5" * 32, ack_sha256=fingerprint_json(_ack),
                provider_operation_id="chatcmpl-offline-" + "5" * 32)
        self.assertEqual(_inspect()["signed_observation_count"], 0)

    def test_p5_4_signed_observation_never_frees_budget_or_retries(self):
        app, client, trust, ack, signed, reserve, inspect, reconcile = (
            self._p5_recovery_fixture())
        from alpha_research.state.fingerprint import fingerprint_json
        reserve()
        first = reconcile(signed())
        self.assertEqual(first["status"],
                         "REMOTE_OBSERVATION_COMMITTED_AUDIT_ONLY")
        self.assertEqual(first["reconciliation_verdict"],
                         "REMOTE_SETTLED_SIGNATURE_VERIFIED_AUDIT_ONLY")
        self.assertFalse(first["spend_settled"])
        self.assertFalse(first["provider_invoice_independently_verified"])
        snap = inspect()
        self.assertEqual(snap["spend_state"], "RESERVED")
        self.assertEqual(snap["signed_observation_count"], 1)
        self.assertEqual(snap["provider_operation_source"], "SIGNED_READBACK")
        self.assertFalse(snap["safe_to_retry"])
        self.assertEqual(client.snapshot("a" * 32)["by_status"],
                         {"RESERVED": 1})
        with self.assertRaises(OrchestrationError):
            reconcile(signed())
        with self.assertRaises(OrchestrationError):
            reserve()
        self.assertEqual(inspect()["signed_observation_count"], 1)
        # A new standalone application process observes the same durable state.
        from alpha_research.orchestration.research_authority_transport import (
            ResearchAuthorityApplication,
        )
        restarted = ResearchAuthorityApplication(
            app.path, consumer_signer=app.grants.signer,
            ledger_signer=app.signer, operator_trust=self.operator_trust,
            consumer_id="consumer", consumer_key_id="consumer-key",
            ledger_id="remote-ledger",
            ledger_issuer_id="separate-ledger",
            ledger_key_id="ledger-key",
            global_max_usd_micros=20_000, recovery_trust=trust)
        restarted.initialize_schema()
        stored = restarted.attempt_readback({
            "grant_id": "a" * 32, "task_id": self.task_id,
            "attempt_id": "5" * 32, "ack_sha256": fingerprint_json(ack),
        })
        self.assertEqual(stored["signed_observation_count"], 1)
        self.assertFalse(stored["safe_to_retry"])

    def test_p5_4_unknown_missing_operation_and_later_readback(self):
        app, client, trust, ack, signed, reserve, inspect, reconcile = (
            self._p5_recovery_fixture())
        from alpha_research.state.fingerprint import fingerprint_json
        reserve()
        client.quarantine(
            grant_id="a" * 32, task_id=self.task_id, attempt_id="5" * 32,
            ack_sha256=fingerprint_json(ack))
        self.assertIsNone(inspect()["provider_operation_id"])
        # NOT_FOUND never proves that it is safe to retry or that a remote
        # provider operation exists. Later signed SETTLED stays audit-only.
        first = reconcile(signed(
            remote_status="NOT_FOUND", evidence_id="1" * 32,
            prompt_tokens=0, completion_tokens=0,
            charged_usd_micros=0))
        self.assertIn("UNCERTAIN", first["reconciliation_verdict"])
        self.assertIsNone(inspect()["provider_operation_id"])
        second = reconcile(signed(
            remote_status="SETTLED", evidence_id="2" * 32))
        self.assertEqual(second["status"],
                         "REMOTE_OBSERVATION_COMMITTED_AUDIT_ONLY")
        status = inspect()
        self.assertEqual(status["signed_observation_count"], 2)
        self.assertEqual(status["spend_state"], "UNKNOWN")
        self.assertEqual(status["provider_operation_source"], "SIGNED_READBACK")
        self.assertFalse(status["safe_to_retry"])
        self.assertEqual(client.snapshot("a" * 32)["by_status"],
                         {"UNKNOWN": 1})

    def test_p5_4_rejects_forged_readback_cross_task_and_operation_drift(self):
        from alpha_research.state.fingerprint import fingerprint_json
        app, client, trust, ack, signed, reserve, inspect, reconcile = (
            self._p5_recovery_fixture())
        reserve()
        client.bind_provider_operation(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="5" * 32, ack_sha256=fingerprint_json(ack),
            provider_operation_id="chatcmpl-offline-" + "5" * 32)
        for mutate in (
            lambda x: x["claims"].update({"task_id": "e" * 32}),
            lambda x: x["claims"].update({"charged_usd_micros": 1}),
            lambda x: x["claims"].update({"model_attempt_id": "e" * 32}),
            lambda x: x["claims"].update({"provider_operation_id": "unrelated"}),
        ):
            evidence = signed()
            mutate(evidence)
            with self.subTest(mutate=mutate):
                with self.assertRaises(OrchestrationError):
                    reconcile(evidence)
        with self.assertRaises(OrchestrationError):
            client.reconcile_remote_observation(
                grant_id="a" * 32, task_id="e" * 32,
                attempt_id="5" * 32, ack_sha256=fingerprint_json(ack),
                evidence=signed())
        self.assertEqual(inspect()["signed_observation_count"], 0)

    def test_p5_4_requires_distinct_recovery_trust_and_admin_role(self):
        import base64 as b64
        from alpha_research.orchestration.research_authority_client import (
            LoopbackAuthorityClient,
        )
        from alpha_research.orchestration.research_authority_transport import (
            ResearchAuthorityApplication,
        )
        from alpha_research.state.fingerprint import fingerprint_json
        app, client, trust, ack, signed, reserve, inspect, reconcile = (
            self._p5_recovery_fixture())
        reserve()
        plain_client = LoopbackAuthorityClient(
            "127.0.0.1", client.port, api_token=client.api_token)
        with self.assertRaises(OrchestrationError):
            plain_client.reconcile_remote_observation(
                grant_id="a" * 32, task_id=self.task_id,
                attempt_id="5" * 32, ack_sha256=fingerprint_json(ack),
                evidence=signed())
        bad = copy.deepcopy(trust)
        bad["issuers"]["remote-provider"]["public_key_b64"] = (
            _pub(app.grants.signer))
        with self.assertRaises(OrchestrationError):
            ResearchAuthorityApplication(
                app.path, consumer_signer=app.grants.signer,
                ledger_signer=app.signer, operator_trust=self.operator_trust,
                consumer_id="consumer", consumer_key_id="consumer-key",
                ledger_id="remote-ledger",
                ledger_issuer_id="separate-ledger",
                ledger_key_id="ledger-key",
                global_max_usd_micros=20_000, recovery_trust=bad)
        bad_operator = copy.deepcopy(trust)
        bad_operator["issuers"]["remote-provider"]["public_key_b64"] = (
            _pub(self.operator_private))
        with self.assertRaises(OrchestrationError):
            ResearchAuthorityApplication(
                app.path, consumer_signer=app.grants.signer,
                ledger_signer=app.signer, operator_trust=self.operator_trust,
                consumer_id="consumer", consumer_key_id="consumer-key",
                ledger_id="remote-ledger",
                ledger_issuer_id="separate-ledger",
                ledger_key_id="ledger-key",
                global_max_usd_micros=20_000, recovery_trust=bad_operator)
        client.revoke("a" * 32)
        result = reconcile(signed())
        self.assertFalse(result["spend_settled"])
        self.assertFalse(inspect()["safe_to_retry"])

    def test_p5_4_overlimit_incident_remains_held(self):
        app, client, trust, ack, signed, reserve, inspect, reconcile = (
            self._p5_recovery_fixture())
        reserve()
        report = reconcile(signed(
            charged_usd_micros=6000, evidence_id="3" * 32))
        self.assertEqual(report["reconciliation_verdict"],
                         "RECONCILIATION_REQUIRED_RESERVATION_EXCEEDED")
        self.assertEqual(inspect()["spend_state"], "RESERVED")
        self.assertFalse(report["reservation_released"])
        self.assertFalse(report["safe_to_retry"])


    def test_p5_4_not_found_cannot_claim_charges_or_token_usage(self):
        app, client, trust, ack, signed, reserve, inspect, reconcile = (
            self._p5_recovery_fixture())
        reserve()
        with self.assertRaises(OrchestrationError):
            reconcile(signed(remote_status="NOT_FOUND"))
        self.assertEqual(inspect()["signed_observation_count"], 0)
        self.assertEqual(inspect()["spend_state"], "RESERVED")


    def test_p5_5_stop_before_consumption_latches_across_restart(self):
        from alpha_research.orchestration.research_canary_control_reference import (
            ReferenceResearchCanaryControl,
        )
        app, _server, client, _, _api, _admin = self._p5_service()
        empty = client.canary_monitor()
        self.assertEqual(empty["stop_state"], "ARMED")
        self.assertIn("GLOBAL_SPEND_POLICY_NOT_INITIALIZED", empty["alerts"])
        stopped = client.canary_stop()
        self.assertEqual(stopped["status"], "CANARY_GLOBAL_STOP_LATCHED")
        self.assertFalse(stopped["permission_to_dispatch"])
        with self.assertRaises(OrchestrationError):
            client.canary_stop()
        handoff = build_global_consumption_request(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, now=self.now)
        with self.assertRaises(OrchestrationError):
            client.consume(handoff, self.receipt)
        restored = ReferenceResearchCanaryControl(app.path)
        restored.initialize_schema()
        self.assertEqual(restored.snapshot()["stop_state"], "STOPPED")
        with self.assertRaises(OrchestrationError):
            restored.assert_armed()

    def test_p5_5_stop_blocks_new_spend_preserves_recovery(self):
        from alpha_research.state.fingerprint import fingerprint_json
        _app, client, inputs = self._p5_sdk_inputs()
        ack_hash = fingerprint_json(inputs["ack"])
        client.reserve(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="1" * 32, input_tokens=700,
            max_output_tokens=900, ack_sha256=ack_hash)
        client.canary_stop()
        with self.assertRaises(OrchestrationError):
            client.reserve(
                grant_id="a" * 32, task_id=self.task_id,
                attempt_id="2" * 32, input_tokens=700,
                max_output_tokens=900, ack_sha256=ack_hash)
        state = client.attempt_readback(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="1" * 32, ack_sha256=ack_hash)
        self.assertEqual(state["spend_state"], "RESERVED")
        client.quarantine(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="1" * 32, ack_sha256=ack_hash)
        self.assertEqual(client.snapshot("a" * 32)["by_status"],
                         {"UNKNOWN": 1})
        self.assertEqual(client.canary_monitor()["stop_state"], "STOPPED")
        with self.assertRaises(OrchestrationError):
            client.canary_preflight(
                grant_id="a" * 32, task_id=self.task_id,
                ack_sha256=ack_hash)

    def test_p5_5_stop_requires_independent_admin_token(self):
        from alpha_research.orchestration.research_authority_client import (
            LoopbackAuthorityClient,
        )
        app, server, client, _, api, admin = self._p5_service()
        no_admin = LoopbackAuthorityClient(
            "127.0.0.1", server.server_address[1], api_token=api)
        self.assertEqual(no_admin.canary_monitor()["stop_state"], "ARMED")
        with self.assertRaises(OrchestrationError):
            no_admin.canary_stop()
        wrong_admin = LoopbackAuthorityClient(
            "127.0.0.1", server.server_address[1],
            api_token=api, admin_token="wrong-" + "5" * 48)
        with self.assertRaises(OrchestrationError):
            wrong_admin.canary_stop()
        self.assertEqual(client.canary_monitor()["stop_state"], "ARMED")
        self.assertEqual(client.canary_stop()["stop_state"], "STOPPED")
        with self.assertRaises(OrchestrationError):
            no_admin._post("canary_reset", {})
        self.assertFalse(app.canary.snapshot()["permission_to_dispatch"])

    def test_p5_5_telemetry_unknown_budget_and_no_private_data(self):
        from alpha_research.state.fingerprint import fingerprint_json
        import json
        app, server, client, ledger, api, admin = self._p5_service(
            global_cap=6_000)
        handoff = build_global_consumption_request(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, now=self.now)
        ack = client.consume(handoff, self.receipt)["ack"]
        ack_hash = fingerprint_json(ack)
        client.reserve(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="1" * 32, input_tokens=700,
            max_output_tokens=900, ack_sha256=ack_hash)
        client.quarantine(
            grant_id="a" * 32, task_id=self.task_id,
            attempt_id="1" * 32, ack_sha256=ack_hash)
        telemetry = client.canary_monitor()
        self.assertEqual(telemetry["total_attempts"], 1)
        self.assertEqual(telemetry["total_committed_usd_micros"], 5000)
        self.assertEqual(telemetry["attempts_by_status"], {"UNKNOWN": 1})
        self.assertIn("UNRESOLVED_PROVIDER_ATTEMPTS", telemetry["alerts"])
        self.assertIn("GLOBAL_RESERVED_BUDGET_AT_LEAST_80_PERCENT",
                      telemetry["alerts"])
        serialized = json.dumps(telemetry)
        for value in (api, admin, self.receipt["signature"],
                      "Build offline alpha only"):
            self.assertNotIn(value, serialized)
        self.assertFalse(telemetry["independent_service_identity_attested"])

    def test_p5_5_sdk_rechecks_stop_after_reserve_before_mock(self):
        from alpha_research.orchestration.research_sdk_mock_pilot import (
            run_research_sdk_offline_pilot,
        )
        _app, client, inputs = self._p5_sdk_inputs()

        class StoppingClient:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

            def canary_preflight(self, **kwargs):
                self.wrapped.canary_stop()
                return self.wrapped.canary_preflight(**kwargs)

        with self.assertRaisesRegex(OrchestrationError,
                                    "canary preflight failed"):
            run_research_sdk_offline_pilot(
                **{**inputs, "attempt_id": "a" * 32,
                   "authority_client": StoppingClient(client)})
        self.assertEqual(client.snapshot("a" * 32)["by_status"],
                         {"UNKNOWN": 1})
        self.assertEqual(client.canary_monitor()["stop_state"], "STOPPED")

    def test_p5_5_production_gate_rejects_forged_monitor_and_stays_blocked(self):
        from alpha_research.orchestration.research_p5_release import (
            inspect_research_p5_canary_gate,
        )
        from alpha_research.orchestration.release_gate import (
            inspect_production_release_gate,
        )
        _app, _server, client, _, _api, _admin = self._p5_service()
        monitor = client.canary_monitor()
        ready = inspect_research_p5_canary_gate(
            self.runner, self.task_id, authority_monitor=monitor)
        self.assertFalse(ready["production_authorized"])
        self.assertFalse(ready["permission_to_dispatch"])
        self.assertIn("REMOTE_SERVICE_IDENTITY_AND_MTLS_NOT_ATTESTED",
                      ready["blockers"])
        self.assertIn("GLOBAL_SPEND_POLICY_NOT_INITIALIZED",
                      ready["blockers"])
        self.assertEqual(ready["authority_monitor"],
                         "UNTRUSTED_LOCAL_AUDIT_ONLY")
        forged = copy.deepcopy(monitor)
        forged["real_canary_authorized"] = True
        with self.assertRaises(OrchestrationError):
            inspect_research_p5_canary_gate(
                self.runner, self.task_id, authority_monitor=forged)
        client.canary_stop()
        stopped = inspect_research_p5_canary_gate(
            self.runner, self.task_id,
            authority_monitor=client.canary_monitor())
        self.assertIn("INDEPENDENT_CANARY_STOP_LATCHED", stopped["blockers"])
        global_release = inspect_production_release_gate(
            self.runner, self.task_id, {}, self.policy,
            config_source="user_config")
        self.assertEqual(
            global_release["components"]["research_p5_canary"]["status"],
            "RESEARCH_P5_CANARY_PRODUCTION_BLOCKED")
        self.assertFalse(global_release["production_authorized"])


    def _p6_mtls_pair(self):
        """Ephemeral offline-only PKI: separate CA, server and client keys."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
        import ipaddress

        now = datetime.now(timezone.utc)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                               "AlphaResearch Test CA")])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name).issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=2))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                           critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    ca_key.public_key()), critical=False)
            .add_extension(
                x509.KeyUsage(digital_signature=False,
                              content_commitment=False, key_encipherment=False,
                              data_encipherment=False, key_agreement=False,
                              key_cert_sign=True, crl_sign=True,
                              encipher_only=False, decipher_only=False),
                critical=True)
            .sign(ca_key, hashes.SHA256()))

        def identity(name, usage, server=False):
            key = ec.generate_private_key(ec.SECP256R1())
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
            builder = (
                x509.CertificateBuilder()
                .subject_name(subject).issuer_name(ca_cert.subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=2))
                .not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                               critical=True)
                .add_extension(
                    x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                    critical=False)
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(
                        ca_key.public_key()), critical=False)
                .add_extension(
                    x509.KeyUsage(digital_signature=True,
                                  content_commitment=False,
                                  key_encipherment=False,
                                  data_encipherment=False,
                                  key_agreement=False,
                                  key_cert_sign=False, crl_sign=False,
                                  encipher_only=False, decipher_only=False),
                    critical=True)
                .add_extension(x509.ExtendedKeyUsage([usage]), critical=False))
            if server:
                builder = builder.add_extension(
                    x509.SubjectAlternativeName([
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]), critical=False)
            cert = builder.sign(ca_key, hashes.SHA256())
            cert_file = self.root / (name + ".crt")
            key_file = self.root / (name + ".key")
            cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_file.write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            return cert_file, key_file

        ca_path = self.root / "p6-test-ca.crt"
        ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        server_cert, server_key = identity(
            "p6-local-server", ExtendedKeyUsageOID.SERVER_AUTH, server=True)
        client_cert, client_key = identity(
            "p6-local-client", ExtendedKeyUsageOID.CLIENT_AUTH)
        return ca_path, server_cert, server_key, client_cert, client_key

    def test_p6_1_real_loopback_mtls_handshake_and_admin_stop(self):
        from threading import Thread
        from alpha_research.orchestration.research_authority_transport import (
            make_loopback_authority_server,
        )
        from alpha_research.orchestration.research_authority_client import (
            LoopbackAuthorityClient,
        )
        from alpha_research.orchestration.research_p6_mtls import (
            make_authority_mtls_server_context,
            make_authority_mtls_client_context,
            inspect_authority_transport_identity,
        )
        app, _, _plain, _, api, admin = self._p5_service()
        ca, sc, sk, cc, ck = self._p6_mtls_pair()
        server_ctx = make_authority_mtls_server_context(
            certificate_file=sc, private_key_file=sk, client_ca_file=ca)
        client_ctx = make_authority_mtls_client_context(
            certificate_file=cc, private_key_file=ck, server_ca_file=ca)
        inspected = inspect_authority_transport_identity(
            host="127.0.0.1", server_context=server_ctx,
            client_context=client_ctx)
        self.assertTrue(inspected["peer_cert_required"])
        self.assertFalse(inspected["production_mtls_deployed"])
        server = make_loopback_authority_server(
            app, api_token=api, admin_token=admin, tls_context=server_ctx)
        th = Thread(target=server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(th.join, timeout=5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        secure = LoopbackAuthorityClient(
            "127.0.0.1", server.server_address[1],
            api_token=api, admin_token=admin, tls_context=client_ctx)
        self.assertEqual(secure.canary_monitor()["stop_state"], "ARMED")
        self.assertEqual(secure.canary_stop()["stop_state"], "STOPPED")
        self.assertFalse(secure.canary_monitor()["permission_to_dispatch"])

    def test_p6_1_mtls_rejects_missing_client_certificate_and_bad_server_ca(self):
        import ssl
        from threading import Thread
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        from alpha_research.orchestration.research_authority_transport import (
            make_loopback_authority_server,
        )
        from alpha_research.orchestration.research_authority_client import (
            LoopbackAuthorityClient,
        )
        from alpha_research.orchestration.research_p6_mtls import (
            make_authority_mtls_server_context,
            make_authority_mtls_client_context,
        )
        app, _, _plain, _, api, admin = self._p5_service()
        ca, sc, sk, cc, ck = self._p6_mtls_pair()
        server_ctx = make_authority_mtls_server_context(
            certificate_file=sc, private_key_file=sk, client_ca_file=ca)
        server = make_loopback_authority_server(
            app, api_token=api, admin_token=admin, tls_context=server_ctx)
        th = Thread(target=server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(th.join, timeout=5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        no_client = ssl.create_default_context(cafile=str(ca))
        with self.assertRaises(OrchestrationError):
            LoopbackAuthorityClient(
                "127.0.0.1", server.server_address[1],
                api_token=api, tls_context=no_client).canary_monitor()
        rogue_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                               "Unknown CA")])
        rogue = (x509.CertificateBuilder()
                 .subject_name(subject).issuer_name(subject)
                 .public_key(rogue_key.public_key())
                 .serial_number(x509.random_serial_number())
                 .not_valid_before(now - timedelta(seconds=10))
                 .not_valid_after(now + timedelta(days=1))
                 .add_extension(x509.BasicConstraints(ca=True,path_length=0),
                                critical=True)
                 .sign(rogue_key, hashes.SHA256()))
        rogue_path = self.root / "rogue.crt"
        rogue_path.write_bytes(rogue.public_bytes(serialization.Encoding.PEM))
        wrong_ca = make_authority_mtls_client_context(
            certificate_file=cc, private_key_file=ck,
            server_ca_file=rogue_path)
        with self.assertRaises(OrchestrationError):
            LoopbackAuthorityClient(
                "127.0.0.1", server.server_address[1],
                api_token=api, tls_context=wrong_ca).canary_monitor()

    def test_p6_2_provider_plan_is_pinned_https_and_never_dispatches(self):
        from alpha_research.orchestration.research_p6_readiness import (
            inspect_p6_provider_plan,
        )
        inputs = dict(
            task_name="construct_super",
            declared_input_tokens=700,
            max_output_tokens=900,
            now=self.now)
        result = inspect_p6_provider_plan(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, **inputs)
        self.assertEqual(result["provider_hostname"], "api.deepseek.com")
        self.assertEqual(result["max_retries"], 0)
        self.assertEqual(result["worst_case_usd_micros"], 5000)
        self.assertFalse(result["permission_to_dispatch"])
        self.assertFalse(result["provider_credentials_loaded"])
        with self.assertRaises(OrchestrationError):
            inspect_p6_provider_plan(
                self.runner, self.task_id, self.model, self.policy,
                self.receipt, self.operator_trust,
                **{**inputs, "max_output_tokens": 1001})
        tampered = copy.deepcopy(self.model)
        for backend in tampered.get("backends", {}).values():
            if backend.get("provider") == "openai_sdk":
                backend["base_url"] = "https://127.0.0.1/evil"
        with self.assertRaises(OrchestrationError):
            inspect_p6_provider_plan(
                self.runner, self.task_id, tampered, self.policy,
                self.receipt, self.operator_trust, **inputs)

    def test_p6_3_precise_metering_and_missing_invoice_hold(self):
        from alpha_research.orchestration.research_p6_readiness import (
            inspect_p6_provider_plan, inspect_p6_provider_metering,
        )
        plan = build_research_sdk_request_plan(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, task_name="construct_super",
            declared_input_tokens=700, max_output_tokens=900, now=self.now)
        bound = inspect_p6_provider_plan(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, task_name="construct_super",
            declared_input_tokens=700, max_output_tokens=900, now=self.now)
        usage = {"prompt_tokens": 650, "completion_tokens": 500,
                 "total_tokens": 1150}
        report = inspect_p6_provider_metering(
            bound, plan, usage, self.receipt)
        self.assertEqual(report["billing_estimate_usd_micros"], 3300)
        self.assertIsNone(report["reported_invoice_usd_micros"])
        self.assertFalse(report["provider_invoice_independently_verified"])
        variant = inspect_p6_provider_metering(
            bound, plan, usage, self.receipt, billed_usd_micros=3400)
        self.assertEqual(variant["estimated_invoice_variance_usd_micros"], 100)
        self.assertEqual(variant["status"],
                         "P6_RECONCILIATION_REQUIRED_INVOICE_VARIANCE")
        for bad in (
            {"prompt_tokens": 701, "completion_tokens": 500,
             "total_tokens": 1201},
            {"prompt_tokens": 650, "completion_tokens": 500,
             "total_tokens": 1100},
        ):
            denied = inspect_p6_provider_metering(
                bound, plan, bad, self.receipt)
            self.assertEqual(
                denied["status"], "P6_RECONCILIATION_REQUIRED_TOKEN_OR_USAGE")
        with self.assertRaises(OrchestrationError):
            inspect_p6_provider_metering(
                bound, plan, usage, self.receipt, billed_usd_micros=True)

    def test_p6_3_signed_readback_correlates_attempt_without_invoice_proof(self):
        from alpha_research.orchestration.research_p6_readiness import (
            inspect_p6_signed_provider_readback,
        )
        evidence, trust = self.readback()
        args = dict(
            max_input_tokens=1000, max_output_tokens=1000,
            max_total_cost_usd_micros=15000, now=self.now)
        report = inspect_p6_signed_provider_readback(
            self.task_id, self.receipt, self.ack, "f"*32,
            evidence, trust, expected_operation_id="remote-provider-op",
            **args)
        self.assertTrue(report["remote_operation_id_matches_journal"])
        self.assertFalse(report["real_provider_invoice_verified"])
        self.assertFalse(report["safe_to_retry"])
        with self.assertRaises(OrchestrationError):
            inspect_p6_signed_provider_readback(
                self.task_id, self.receipt, self.ack, "f"*32,
                evidence, trust, expected_operation_id="another-op",
                **args)

    def test_p6_4_catalog_reads_allow_exact_shape_not_platform_execution(self):
        from alpha_research.orchestration.research_p6_readiness import (
            inspect_p6_brain_catalog_frame,
        )
        scope = {"region": "USA", "delay": 1,
                 "universe": "TOP3000", "instrument_type": "EQUITY"}
        valid = inspect_p6_brain_catalog_frame(
            self.runner, self.task_id, {"cmd": "get_datasets", **scope})
        self.assertTrue(valid["read_only_frame_shape_valid"])
        self.assertFalse(valid["brain_read_invoked"])
        self.assertFalse(valid["permission_to_dispatch"])
        for frame in (
            {"cmd": "simulate_super", **scope},
            {"cmd": "get_datasets", **{**scope, "delay": True}},
            {"cmd": "get_datafields", **scope, "dataset_id": "../secret"},
            {"cmd": "get_user_alphas", "stage": "IS",
             "limit": 100, "offset": 0},
        ):
            with self.subTest(frame=frame):
                denied = inspect_p6_brain_catalog_frame(
                    self.runner, self.task_id, frame)
                self.assertFalse(denied["read_only_frame_shape_valid"])
                self.assertFalse(denied["permission_to_dispatch"])

    def test_p6_5_6_integrated_release_remains_blocked_with_all_offline_inputs(self):
        from alpha_research.orchestration.research_p6_readiness import (
            inspect_p6_provider_plan, inspect_p6_provider_metering,
            inspect_p6_brain_catalog_frame, inspect_p6_integrated_release,
            inspect_p6_signed_provider_readback,
        )
        from alpha_research.orchestration.release_gate import (
            inspect_production_release_gate,
        )
        _app, _server, client, _, _, _ = self._p5_service()
        provider = inspect_p6_provider_plan(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust,
            task_name="construct_super",
            declared_input_tokens=700, max_output_tokens=900, now=self.now)
        sdk = build_research_sdk_request_plan(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust,
            task_name="construct_super",
            declared_input_tokens=700, max_output_tokens=900, now=self.now)
        meter = inspect_p6_provider_metering(
            provider, sdk,
            {"prompt_tokens": 650, "completion_tokens": 500,
             "total_tokens": 1150}, self.receipt)
        brain = inspect_p6_brain_catalog_frame(
            self.runner, self.task_id,
            {"cmd":"get_datasets","region":"USA","delay":1,
             "universe":"TOP3000","instrument_type":"EQUITY"})
        evidence, trust = self.readback()
        signed = inspect_p6_signed_provider_readback(
            self.task_id, self.receipt, self.ack, "f"*32,
            evidence, trust, expected_operation_id="remote-provider-op",
            max_input_tokens=1000,max_output_tokens=1000,
            max_total_cost_usd_micros=15000,now=self.now)
        complete = inspect_p6_integrated_release(
            self.runner, self.task_id,
            authority_monitor=client.canary_monitor(),
            provider_report=provider, metering_report=meter,
            brain_report=brain, signed_readback=signed)
        self.assertEqual(complete["status"], "P6_PRODUCTION_BLOCKED")
        self.assertEqual(complete["milestones"]["p6_2_provider"],
                         "OFFLINE_REQUEST_CONTRACT_VERIFIED")
        self.assertEqual(complete["milestones"]["p6_3_billing"],
                         "SIGNED_OBSERVATION_ONLY_INVOICE_UNVERIFIED")
        self.assertFalse(complete["production_authorized"])
        self.assertFalse(complete["canary_rehearsal_only"] is False)
        release = inspect_production_release_gate(
            self.runner, self.task_id, self.model, self.policy,
            config_source="user_config")
        self.assertEqual(
            release["components"]["research_p6_readiness"]["status"],
            "P6_PRODUCTION_BLOCKED")
        self.assertFalse(release["production_authorized"])
        forged = copy.deepcopy(provider)
        forged["permission_to_dispatch"] = True
        with self.assertRaises(OrchestrationError):
            inspect_p6_integrated_release(
                self.runner, self.task_id, provider_report=forged)


    def test_p6_5_end_to_end_offline_canary_real_sdk_mock_stays_blocked(self):
        from alpha_research.orchestration.research_p6_readiness import (
            run_p6_offline_canary,
        )
        _app, client, inputs = self._p5_sdk_inputs()
        frame = {"cmd": "get_datasets", "region": "USA", "delay": 1,
                 "universe": "TOP3000", "instrument_type": "EQUITY"}
        result = run_p6_offline_canary(
            sdk_inputs=inputs, attempt_id="a" * 32,
            brain_read_frame=frame)
        self.assertEqual(result["status"],
                         "P6_OFFLINE_CANARY_REHEARSAL_COMPLETED")
        self.assertEqual(result["sdk_http_requests"], 1)
        self.assertEqual(result["sdk_max_retries"], 0)
        self.assertEqual(result["declared_reserved_usd_micros"], 5000)
        self.assertEqual(result["estimated_cost_usd_micros"], 3300)
        self.assertTrue(result["brain_frame_policy_valid"])
        self.assertFalse(result["brain_api_invoked"])
        self.assertFalse(result["provider_api_invoked"])
        self.assertFalse(result["invoice_independently_verified"])
        self.assertEqual(result["release_status"], "P6_PRODUCTION_BLOCKED")
        self.assertEqual(client.snapshot("a"*32)["by_status"], {"RESERVED": 1})
        with self.assertRaises(OrchestrationError):
            run_p6_offline_canary(
                sdk_inputs=inputs, attempt_id="a" * 32,
                brain_read_frame=frame)
        self.assertEqual(client.snapshot("a"*32)["reserved_attempts"], 1)

    def test_p6_5_readonly_canary_rejects_mutation_before_budget_reserve(self):
        from alpha_research.orchestration.research_p6_readiness import (
            run_p6_offline_canary,
        )
        _app, client, inputs = self._p5_sdk_inputs()
        with self.assertRaises(OrchestrationError):
            run_p6_offline_canary(
                sdk_inputs=inputs, attempt_id="b" * 32,
                brain_read_frame={"cmd": "simulate_super"})
        self.assertEqual(client.snapshot("a"*32)["reserved_attempts"], 0)


    def test_p6_release_denied_brain_frame_and_bad_metering_are_not_verified(self):
        from alpha_research.orchestration.research_p6_readiness import (
            inspect_p6_integrated_release, inspect_p6_brain_catalog_frame,
            inspect_p6_provider_plan, inspect_p6_provider_metering,
            inspect_p6_signed_provider_readback,
        )
        provider = inspect_p6_provider_plan(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, task_name="construct_super",
            declared_input_tokens=700, max_output_tokens=900, now=self.now)
        sdk = build_research_sdk_request_plan(
            self.runner, self.task_id, self.model, self.policy,
            self.receipt, self.operator_trust, task_name="construct_super",
            declared_input_tokens=700, max_output_tokens=900, now=self.now)
        # A supplier-reported token count which exceeds its declared input
        # ceiling is a reconciliation incident, not successful billing.
        metering = inspect_p6_provider_metering(
            provider, sdk,
            {"prompt_tokens": 701, "completion_tokens": 500,
             "total_tokens": 1201}, self.receipt)
        denied = inspect_p6_brain_catalog_frame(
            self.runner, self.task_id, {"cmd": "simulate_super"})
        evidence, trust = self.readback()
        signed = inspect_p6_signed_provider_readback(
            self.task_id, self.receipt, self.ack, "f" * 32,
            evidence, trust, expected_operation_id="remote-provider-op",
            max_input_tokens=1000, max_output_tokens=1000,
            max_total_cost_usd_micros=15000, now=self.now)
        release = inspect_p6_integrated_release(
            self.runner, self.task_id, provider_report=provider,
            metering_report=metering, brain_report=denied,
            signed_readback=signed)
        self.assertEqual(release["milestones"]["p6_3_billing"],
                         "RECONCILIATION_REQUIRED")
        self.assertEqual(release["milestones"]["p6_4_brain_read"],
                         "READ_ONLY_FRAME_DENIED")
        self.assertIn("P6_METERING_RECONCILIATION_REQUIRED",
                      release["blockers"])
        self.assertIn("P6_BRAIN_READ_FRAME_DENIED", release["blockers"])
        self.assertFalse(release["permission_to_dispatch"])

    def test_p6_release_does_not_accept_forged_negative_audit_claims(self):
        from alpha_research.orchestration.research_p6_readiness import (
            inspect_p6_integrated_release,
        )
        valid = {
            "task_id": self.task_id,
            "status": "P6_SIGNED_READBACK_AUDITED_BILLING_UNVERIFIED",
            "permission_to_dispatch": False,
            "real_provider_invoice_verified": False,
            "server_to_provider_readback_executed": False,
            "remote_operation_id_matches_journal": True,
            "safe_to_retry": False,
        }
        for name, value in (
            ("permission_to_dispatch", True),
            ("real_provider_invoice_verified", True),
            ("server_to_provider_readback_executed", True),
            ("remote_operation_id_matches_journal", False),
            ("safe_to_retry", True),
        ):
            with self.subTest(name=name):
                forged = {**valid, name: value}
                with self.assertRaises(OrchestrationError):
                    inspect_p6_integrated_release(
                        self.runner, self.task_id, signed_readback=forged)


if __name__ == "__main__":
    unittest.main()
