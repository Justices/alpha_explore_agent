"""P4.16 non-executable research grant, priced envelope and CLI audit."""
import base64
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alpha_research.cli import main
from alpha_research.agents.registry import bundled_registry_path
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.execution_identity import persist_execution_identity
from alpha_research.orchestration.execution_proposal import persist_execution_proposal
from alpha_research.orchestration.research_only_admission import (
    _CONTEXT, build_research_issuer_request, inspect_research_live_readiness,
    verify_research_grant,
)
from alpha_research.orchestration.runtime_config import prepare_registered_model_runtime
from alpha_research.state.fingerprint import canonical_json_bytes


class ResearchOnlyGrantTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runner = MainOrchestrator(self.root)
        self.task = self.runner.create(
            "super", {
                "objective": "USA TOP3000 prepare only, no simulation",
                "region": "USA", "delay": 1, "universe": "TOP3000",
            })
        self.id = self.task["task_id"]
        self.config = {
            "model_governance": {
                "max_calls": 2, "max_reported_tokens": 6000,
                "failover": "manual",
            }
        }
        prepared = prepare_registered_model_runtime(
            self.config,
            {"research_lead": "deepseek", "research_executor": "deepseek"},
            bundled_registry_path(),
        )
        self.model = prepared["model"]
        self.policy = prepared["model_governance"]
        persist_execution_proposal(self.runner, self.id)
        persist_execution_identity(self.runner, self.id, self.model, self.policy)
        self.now = datetime.now(timezone.utc)
        self.private = Ed25519PrivateKey.generate()  # offline test only
        pub = base64.b64encode(self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        self.trust = {
            "schema_version": 1,
            "issuers": {
                "operator-external": {
                    "key_id": "research-only-test",
                    "public_key_b64": pub,
                    "enabled": True,
                    "operations": ["research_prepare_model_readonly"],
                },
            },
            "revoked_grants": [],
        }
        fixed = build_research_issuer_request(
            self.runner, self.id, self.model, self.policy)["fixed_claims"]
        self.claims = {
            **fixed,
            "grant_id": "a" * 32,
            "issuer_id": "operator-external",
            "key_id": "research-only-test",
            "operator_approval_id": "b" * 32,
            "issued_at": (self.now - timedelta(seconds=30)).isoformat(),
            "expires_at": (self.now + timedelta(minutes=10)).isoformat(),
            "price_evidence_sha256": "c" * 64,
            "currency": "USD",
            "input_rate_micros_per_million": 2_000_000,
            "output_rate_micros_per_million": 4_000_000,
            "max_input_tokens_per_call": 1000,
            "max_output_tokens_per_call": 1000,
            "max_total_cost_usd_micros": 15_000,
        }

    def receipt(self, claims=None):
        fields = copy.deepcopy(self.claims if claims is None else claims)
        return {
            "claims": fields,
            "signature": base64.b64encode(self.private.sign(
                _CONTEXT + canonical_json_bytes(fields))).decode(),
        }

    def verify(self, claims=None, trust=None):
        return verify_research_grant(
            self.runner, self.id, self.model, self.policy,
            self.receipt(claims), self.trust if trust is None else trust,
            now=self.now,
        )

    def test_issuer_request_pins_scope_roles_and_budget_without_credentials(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        request = build_research_issuer_request(
            self.runner, self.id, self.model, self.policy)
        self.assertEqual(request["status"], "AWAITING_INDEPENDENT_RESEARCH_APPROVAL")
        self.assertEqual(request["fixed_claims"]["scope"], {
            "region": "USA", "delay": 1, "universe": "TOP3000",
            "instrument_type": "EQUITY",
        })
        self.assertEqual(request["fixed_claims"]["max_model_calls"], 2)
        self.assertEqual(request["fixed_claims"]["max_reported_tokens"], 6000)
        self.assertFalse(request["fixed_claims"]["allow_brain_simulation"])
        self.assertFalse(request["fixed_claims"]["allow_alpha_submission"])
        self.assertFalse(request["model_call_authorized"])
        self.assertFalse(request["provider_price_independently_attested"])
        self.assertNotIn("api_key", json.dumps(request).lower())
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_valid_ed25519_signature_is_audit_only_not_production_grant(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        result = self.verify()
        self.assertEqual(
            result["status"], "EXTERNAL_RESEARCH_SIGNATURE_VERIFIED_AUDIT_ONLY")
        self.assertTrue(result["signature_verified"])
        self.assertEqual(result["cost_envelope"]["worst_case_usd_micros"], 12_000)
        self.assertEqual(result["cost_envelope"]["approved_usd_micros"], 15_000)
        self.assertFalse(result["trusted_issuer_independently_provisioned"])
        self.assertFalse(result["pricing_provenance_independently_verified"])
        self.assertFalse(result["single_use_grant_consumed"])
        self.assertFalse(result["model_call_authorized"])
        self.assertFalse(result["brain_simulation_authorized"])
        self.assertFalse(result["alpha_submission_authorized"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))
        readiness = inspect_research_live_readiness(
            self.runner, self.id, self.model, self.policy,
            config_source="user_config",
            receipt=self.receipt(), trust_policy=self.trust)
        self.assertEqual(readiness["status"], "RESEARCH_LIVE_BLOCKED")
        self.assertIn(
            "RESEARCH_LOCAL_SIGNATURE_ONLY_NOT_INDEPENDENTLY_ATTESTED",
            readiness["blockers"])
        self.assertIn(
            "RESEARCH_PAID_MODEL_ADAPTER_DISABLED", readiness["blockers"])
        self.assertFalse(readiness["brain_read_authorized"])
        self.assertFalse(readiness["model_call_authorized"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_missing_receipt_and_defaults_leave_live_preparation_blocked(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        result = inspect_research_live_readiness(
            self.runner, self.id, self.model, self.policy,
            config_source="builtin_offline_defaults")
        self.assertEqual(result["status"], "RESEARCH_LIVE_BLOCKED")
        self.assertIn("RESEARCH_OFFLINE_DEFAULTS_NOT_LIVE_CONFIG",
                      result["blockers"])
        self.assertIn("RESEARCH_ONLY_GRANT_NOT_INDEPENDENTLY_PROVISIONED",
                      result["blockers"])
        self.assertIsNone(result["grant_verification"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_rejects_expiry_replay_source_and_cross_operation(self):
        mutations = [
            {"issued_at": (self.now + timedelta(minutes=1)).isoformat()},
            {"expires_at": (self.now - timedelta(seconds=1)).isoformat()},
            {"expires_at": (self.now + timedelta(hours=2)).isoformat()},
            {"scope": {"region": "CHN", "delay": 1,
                       "universe": "TOP3000", "instrument_type": "EQUITY"}},
            {"operation": "brain_simulation"},
            {"allow_brain_simulation": True},
            {"allow_alpha_submission": True},
            {"max_model_calls": 3},
            {"model_profile_sha256": "a" * 64},
            {"request_sha256": "a" * 64},
            {"task_id": "f" * 32},
            {"currency": "EUR"},
            {"price_evidence_sha256": "invalid"},
            {"max_total_cost_usd_micros": 1000},
            {"max_total_cost_usd_micros": True},
            {"max_total_cost_usd_micros": 100_000_001},
            {"max_input_tokens_per_call": 2001},
        ]
        for update in mutations:
            with self.subTest(update=update):
                candidate = {**self.claims, **update}
                with self.assertRaises(OrchestrationError):
                    self.verify(candidate)
        revoked = {**self.trust, "revoked_grants": ["a" * 32]}
        with self.assertRaisesRegex(OrchestrationError, "revoked"):
            self.verify(trust=revoked)
        switched = copy.deepcopy(self.trust)
        switched["issuers"]["operator-external"]["operations"] = ["brain_simulation"]
        with self.assertRaisesRegex(OrchestrationError, "not trusted"):
            self.verify(trust=switched)
        invalid_signature = self.receipt()
        invalid_signature["signature"] = "bad"
        with self.assertRaisesRegex(OrchestrationError, "signature invalid"):
            verify_research_grant(
                self.runner, self.id, self.model, self.policy,
                invalid_signature, self.trust, now=self.now)

    def test_rejects_stopped_already_executed_or_mock_workspace(self):
        stop = self.root / "state" / "stop.flag"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("STOPPED", encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "stop"):
            self.verify()
        stop.unlink()
        from alpha_research.orchestration.mock_workspace import marker_path
        marker = marker_path(self.root)
        marker.write_text("not even a trusted mock marker", encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "mock"):
            self.verify()
        marker.unlink()
        from alpha_research.state.store import atomic_write_json
        task = self.runner.load(self.id)
        task["budget"]["used"] = 1
        atomic_write_json(self.runner._path(self.id), task)
        with self.assertRaisesRegex(OrchestrationError, "fresh"):
            self.verify()


    def test_p4_17_model_cost_preflight_is_never_executable(self):
        from alpha_research.orchestration.research_model_cost_boundary import (
            inspect_model_request_cost,
        )
        before = sorted(str(p) for p in self.root.rglob("*"))
        result = inspect_model_request_cost(
            self.runner, self.id, self.model, self.policy,
            self.receipt(), self.trust,
            input_tokens=700, max_output_tokens=900, now=self.now)
        self.assertEqual(
            result["status"], "MODEL_REQUEST_ENVELOPE_VALIDATED_NOT_EXECUTABLE")
        self.assertEqual(result["worst_case_request_usd_micros"], 5_000)
        self.assertFalse(result["model_call_authorized"])
        self.assertFalse(result["permission_to_dispatch"])
        self.assertFalse(result["provider_output_hard_limit_attested"])
        self.assertFalse(result["provider_pricing_attested"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))
        for field, amount in (
            ("input_tokens", 1001), ("max_output_tokens", 1001),
            ("input_tokens", True), ("max_output_tokens", 0),
        ):
            args = {"input_tokens": 700, "max_output_tokens": 900}
            args[field] = amount
            with self.subTest(field=field, amount=amount):
                with self.assertRaises(OrchestrationError):
                    inspect_model_request_cost(
                        self.runner, self.id, self.model, self.policy,
                        self.receipt(), self.trust, now=self.now, **args)

    def test_p4_17_audit_claim_is_single_use_and_never_authorizes_dispatch(self):
        from alpha_research.orchestration.research_grant_audit import (
            reserve_research_grant_audit, inspect_research_grant_claim,
        )
        initial = inspect_research_grant_claim(self.runner, self.id)
        self.assertEqual(initial["status"], "NOT_RESERVED")
        before_task = self.runner.load(self.id)
        reserved = reserve_research_grant_audit(
            self.runner, self.id, self.model, self.policy,
            self.receipt(), self.trust, now=self.now)
        self.assertEqual(reserved["status"], "RESERVED_RESEARCH_GRANT_AUDIT_ONLY")
        self.assertEqual(reserved["grant_id"], "a" * 32)
        self.assertFalse(reserved["permission_to_dispatch"])
        self.assertFalse(reserved["model_call_authorized"])
        self.assertFalse(reserved["brain_read_authorized"])
        self.assertFalse(reserved["brain_simulation_authorized"])
        self.assertFalse(reserved["alpha_submission_authorized"])
        self.assertEqual(self.runner.load(self.id), before_task)
        with self.assertRaisesRegex(OrchestrationError, "already reserved"):
            reserve_research_grant_audit(
                self.runner, self.id, self.model, self.policy,
                self.receipt(), self.trust, now=self.now)
        self.assertEqual(inspect_research_grant_claim(
            self.runner, self.id), reserved)

    def test_p4_17_global_audit_prevents_grant_and_approval_reuse_across_tasks(self):
        from alpha_research.orchestration.research_grant_audit import (
            reserve_research_grant_audit, inspect_research_grant_claim,
        )
        reserve_research_grant_audit(
            self.runner, self.id, self.model, self.policy,
            self.receipt(), self.trust, now=self.now)
        second = self.runner.create(
            "super", {"objective": "another research Task",
                      "region": "USA", "delay": 1, "universe": "TOP3000"})
        second_id = second["task_id"]
        persist_execution_proposal(self.runner, second_id)
        persist_execution_identity(
            self.runner, second_id, self.model, self.policy)
        fixed = build_research_issuer_request(
            self.runner, second_id, self.model, self.policy)["fixed_claims"]
        same_grant = {**self.claims, **fixed,
                      "operator_approval_id": "d" * 32}
        with self.assertRaisesRegex(OrchestrationError, "already reserved"):
            reserve_research_grant_audit(
                self.runner, second_id, self.model, self.policy,
                self.receipt(same_grant), self.trust, now=self.now)
        self.assertEqual(
            inspect_research_grant_claim(
                self.runner, second_id)["status"], "NOT_RESERVED")
        same_approval = {**same_grant, "grant_id": "e" * 32,
                         "operator_approval_id": "b" * 32}
        with self.assertRaisesRegex(OrchestrationError, "already reserved"):
            reserve_research_grant_audit(
                self.runner, second_id, self.model, self.policy,
                self.receipt(same_approval), self.trust, now=self.now)
        distinct = {**same_grant, "grant_id": "e" * 32,
                    "operator_approval_id": "f" * 32}
        accepted = reserve_research_grant_audit(
            self.runner, second_id, self.model, self.policy,
            self.receipt(distinct), self.trust, now=self.now)
        self.assertEqual(accepted["status"], "RESERVED_RESEARCH_GRANT_AUDIT_ONLY")
        self.assertEqual(accepted["grant_id"], "e" * 32)

    def test_p4_17_corrupted_or_symlinked_claim_ledger_fails_closed(self):
        from alpha_research.orchestration.research_grant_audit import (
            reserve_research_grant_audit, inspect_research_grant_claim,
        )
        reserve_research_grant_audit(
            self.runner, self.id, self.model, self.policy,
            self.receipt(), self.trust, now=self.now)
        ledger = (self.root / "state" / "research_v1_1" /
                  "research_grant_audit" / "claims.json")
        raw = json.loads(ledger.read_text(encoding="utf-8"))
        raw["by_task"][self.id]["grant_id"] = "f" * 32
        ledger.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(
            inspect_research_grant_claim(
                self.runner, self.id)["status"], "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "corruption"):
            reserve_research_grant_audit(
                self.runner, self.id, self.model, self.policy,
                self.receipt(), self.trust, now=self.now)
        ledger.unlink()
        foreign = self.root / "outside-journal.json"
        foreign.write_text('{"unrelated":true}', encoding="utf-8")
        try:
            ledger.symlink_to(foreign)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        self.assertEqual(
            inspect_research_grant_claim(
                self.runner, self.id)["status"], "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "unsafe"):
            reserve_research_grant_audit(
                self.runner, self.id, self.model, self.policy,
                self.receipt(), self.trust, now=self.now)
        self.assertEqual(foreign.read_text(encoding="utf-8"),
                         '{"unrelated":true}')

    def test_p4_17_two_concurrent_local_claims_cannot_consume_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        from alpha_research.orchestration.research_grant_audit import (
            reserve_research_grant_audit,
        )
        def claim(_):
            try:
                return reserve_research_grant_audit(
                    self.runner, self.id, self.model, self.policy,
                    self.receipt(), self.trust, now=self.now)["status"]
            except OrchestrationError:
                return "REJECTED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(results.count("RESERVED_RESEARCH_GRANT_AUDIT_ONLY"), 1)
        self.assertEqual(results.count("REJECTED"), 1)


class ResearchGrantCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.prefix = [
            "--state-root", str(self.root),
            "--lead", "deepseek", "--executor", "deepseek",
        ]

    def call(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("BRAIN Gateway forbidden")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("paid model forbidden")),
              redirect_stdout(out), redirect_stderr(err)):
            code = main(self.prefix + arguments)
        return code, out.getvalue(), err.getvalue()

    def test_research_readiness_cli_requires_no_file_and_no_provider(self):
        code, out, err = self.call([
            "orchestration-prepare", "--workflow", "super",
            "--objective", "research-only preflight", "--region", "USA",
            "--delay", "1", "--universe", "TOP3000"])
        self.assertEqual(code, 0, err)
        task_id = json.loads(out)["task_id"]
        before = sorted(str(p) for p in self.root.rglob("*"))
        code, out, err = self.call([
            "orchestration-research-issuer-request", task_id])
        self.assertEqual(code, 0, err)
        request = json.loads(out)
        self.assertEqual(request["status"],
                         "AWAITING_INDEPENDENT_RESEARCH_APPROVAL")
        self.assertFalse(request["permission_to_dispatch"])
        code, out, err = self.call([
            "orchestration-research-live-readiness", task_id])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["status"], "RESEARCH_LIVE_BLOCKED")
        self.assertIn("RESEARCH_OFFLINE_DEFAULTS_NOT_LIVE_CONFIG",
                      report["blockers"])
        self.assertFalse(report["model_call_authorized"])
        self.assertFalse(report["brain_simulation_authorized"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))
        code, out, err = self.call([
            "orchestration-research-live-readiness", task_id,
            "--receipt", str(self.root / "missing.json")])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("research receipt and issuer trust policy must be paired", err)


if __name__ == "__main__":
    unittest.main()
