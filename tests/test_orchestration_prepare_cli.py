"""CLI-only offline task preparation and read-only handoff inspection."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.orchestration.engine import MainOrchestrator


class OfflineProductionPreparationCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "scope": {"region": "CHN", "delay": 1},
            "model_governance": {
                "max_calls": 4, "max_reported_tokens": 1000,
                "failover": "manual",
            },
        }), encoding="utf-8")
        self.prefix = [
            "--state-root", str(self.root), "--config", str(self.config),
            "--lead", "deepseek", "--executor", "deepseek",
        ]

    def _call(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("gateway unexpectedly created")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("model unexpectedly created")),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            status = main(args)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_regular_super_optimization_prepare_without_external_calls(self):
        for workflow in ("regular", "super", "optimization"):
            with self.subTest(workflow=workflow):
                args = self.prefix + [
                    "orchestration-prepare", "--workflow", workflow,
                    "--objective", "offline preparation",
                ]
                if workflow == "optimization":
                    args += ["--alpha-id", "offline-source"]
                status, output, error = self._call(args)
                self.assertEqual(status, 0, error)
                result = json.loads(output)
                self.assertEqual(result["status"], "PREPARED_NOT_AUTHORIZED")
                self.assertFalse(result["model_call_authorized"])
                task_id = result["task_id"]
                task = MainOrchestrator(self.root).load(task_id)
                self.assertEqual(task["status"], "CREATED")
                self.assertEqual(task["request"]["region"], "CHN")
                self.assertEqual(task["request"]["delay"], 1)
                self.assertEqual(task["budget"]["reserved"], 0)
                status, output, error = self._call(
                    self.prefix + ["orchestration-handoff", task_id])
                self.assertEqual(status, 0, error)
                inspected = json.loads(output)
                self.assertEqual(inspected["status"], "AWAITING_DOMAIN_RESEARCH_ID")
                self.assertFalse(inspected["inspection_provider_invoked"])
                self.assertFalse(inspected["inspection_brain_invoked"])

    def test_super_and_optimization_prepare_pin_explicit_universe_offline(self):
        # The CLI previously rejected --universe on Super/Optimization even
        # though signed Simulation scope checks require an immutable Universe.
        prefix = ["--state-root", str(self.root),
                  "--lead", "deepseek", "--executor", "deepseek"]
        for workflow in ("super", "optimization"):
            with self.subTest(workflow=workflow):
                args = prefix + [
                    "orchestration-prepare", "--workflow", workflow,
                    "--objective", "USA TOP3000 local-only Simulation preparation",
                    "--region", "USA", "--delay", "1",
                    "--universe", "TOP3000",
                ]
                if workflow == "optimization":
                    args += ["--alpha-id", "offline-reference"]
                code, output, error = self._call(args)
                self.assertEqual(code, 0, error)
                report = json.loads(output)
                self.assertEqual(report["status"], "PREPARED_NOT_AUTHORIZED")
                self.assertEqual(report["config_source"], "builtin_offline_defaults")
                task = MainOrchestrator(self.root).load(report["task_id"])
                self.assertEqual(task["status"], "CREATED")
                self.assertEqual(task["budget"], {
                    "limit": 1, "used": 0, "reserved": 0})
                self.assertEqual(task["request"]["region"], "USA")
                self.assertEqual(task["request"]["delay"], 1)
                self.assertEqual(task["request"]["universe"], "TOP3000")
                self.assertFalse(report["model_call_authorized"])
                self.assertFalse(report["brain_invoked"])

    def test_super_preparation_rejects_conflicting_configured_scope_safely(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["scope"]["universe"] = "TOP1000"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        code, output, error = self._call(self.prefix + [
            "orchestration-prepare", "--workflow", "super",
            "--objective", "offline scope mismatch",
            "--region", "CHN", "--delay", "1", "--universe", "TOP3000"])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("requested Universe conflicts with configured scope", error)
        self.assertEqual(list(
            (self.root / "state" / "research_v1_1" /
             "orchestration").glob("*.json")), [])

    def test_super_preparation_rejects_regular_only_dataset_options(self):
        code, output, error = self._call(self.prefix + [
            "orchestration-prepare", "--workflow", "super",
            "--objective", "dataset not in Super", "--dataset", "fake-ds"])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("Dataset, Category and PPA apply only to Regular", error)

    def test_p4_13_issuer_handoff_is_read_only_exact_and_redacted(self):
        from alpha_research.orchestration.simulation_attempt_admission import (
            stage_simulation_attempt,
        )
        from alpha_research.orchestration.operator_consent import (
            expected_external_consent_fields,
        )
        from alpha_research.state.fingerprint import fingerprint_json
        prep = self.prefix + [
            "orchestration-prepare", "--workflow", "super",
            "--objective", "offline issuer handoff", "--universe", "TOP3000"]
        code, output, err = self._call(prep)
        self.assertEqual(code, 0, err)
        task_id = json.loads(output)["task_id"]
        plan = {
            "research_id": "super:" + "a" * 32,
            "run_id": "b" * 32, "batch_id": "offline_one",
            "request": {
                "selection": "rank(secret-alpha-expression)",
                "combo": "rank(other-private-expression)",
            },
            "settings": {
                "region": "CHN", "delay": 1,
                "universe": "TOP3000", "instrument_type": "EQUITY",
            },
        }
        manifest = self.root / "private-manifest.json"
        manifest.write_text(json.dumps(plan), encoding="utf-8")
        before = sorted(str(p) for p in self.root.rglob("*"))
        cmd = self.prefix + [
            "orchestration-scope-issuer-request", task_id,
            "--manifest", str(manifest)]
        code, output, err = self._call(cmd)
        self.assertEqual(code, 0, err)
        result = json.loads(output)
        self.assertEqual(result["status"],
                         "AWAITING_INDEPENDENT_SIGNATURE_NOT_AUTHORIZED")
        self.assertEqual(result["kind"],
                         "SIMULATION_SCOPE_STAGING_RECEIPT_REQUEST")
        self.assertEqual(result["fixed_claims"]["scope"], plan["settings"])
        self.assertEqual(result["fixed_claims"]["simulation_sha256"],
                         fingerprint_json({
                             "request": plan["request"],
                             "settings": plan["settings"],
                         }))
        self.assertEqual(
            result["issuer_supplied_fields"],
            sorted(["authorization_id", "issuer", "issued_at", "expires_at"]))
        self.assertFalse(result["permission_to_dispatch"])
        self.assertFalse(result["model_call_authorized"])
        self.assertFalse(result["alpha_submission_authorized"])
        self.assertNotIn("secret-alpha-expression", output)
        self.assertNotIn("other-private-expression", output)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

        code, _, err = self._call(self.prefix + [
            "orchestration-consent-issuer-request", task_id])
        self.assertEqual(code, 2, err)
        self.assertIn("OrchestrationError", err)

        # A second, independent scope issuer fills only the advertised
        # fields. The existing P4.5 HMAC verifier checks the complete packet
        # before staging; unlike a mock, this proves schema compatibility.
        import hashlib
        import hmac
        import os
        from datetime import datetime, timedelta, timezone
        from alpha_research.orchestration.runtime_config import (
            load_preparation_config, prepare_registered_model_runtime,
        )
        from alpha_research.agents.registry import bundled_registry_path
        from alpha_research.orchestration.simulation_scope import (
            CONTEXT as SCOPE_CONTEXT, VERIFY_KEY_ENV,
        )
        from alpha_research.state.fingerprint import canonical_json_bytes
        cfg, _ = load_preparation_config(self.config, explicit=True)
        prepared = prepare_registered_model_runtime(
            cfg, {"research_lead": "deepseek",
                  "research_executor": "deepseek"}, bundled_registry_path())
        now = datetime.now(timezone.utc)
        claims = {
            **result["fixed_claims"],
            "authorization_id": "c" * 32,
            "issuer": "offline-scope-issuer",
            "issued_at": (now - timedelta(seconds=30)).isoformat(),
            "expires_at": (now + timedelta(minutes=4)).isoformat(),
        }
        secret = "offline-test-hmac-secret-" * 3
        scope_receipt = {
            "claims": claims,
            "signature": hmac.new(
                secret.encode("utf-8"),
                SCOPE_CONTEXT + canonical_json_bytes(claims),
                hashlib.sha256,
            ).hexdigest(),
        }
        with patch.dict(os.environ, {VERIFY_KEY_ENV: secret}):
            stage_result = stage_simulation_attempt(
                MainOrchestrator(self.root), task_id,
                prepared["model"], prepared["model_governance"],
                scope_receipt, plan)
        self.assertEqual(stage_result["status"], "STAGED_NO_DISPATCH")
        self.assertFalse(stage_result["permission_to_dispatch"])

        before = sorted(str(p) for p in self.root.rglob("*"))
        code, output, err = self._call(self.prefix + [
            "orchestration-consent-issuer-request", task_id])
        self.assertEqual(code, 0, err)
        receipt_request = json.loads(output)
        self.assertEqual(
            receipt_request["kind"],
            "EXTERNAL_OPERATOR_CONSENT_SIGNATURE_REQUEST")
        self.assertEqual(receipt_request["fixed_claims"]["max_attempts"], 1)
        self.assertEqual(receipt_request["fixed_claims"]["max_model_calls"], 0)
        self.assertFalse(
            receipt_request["fixed_claims"]["allow_alpha_submission"])
        self.assertEqual(
            receipt_request["fixed_claims"]["scope_authorization_id"],
            "c" * 32)
        self.assertEqual(receipt_request["fixed_claims"]["scope"],
                         plan["settings"])
        self.assertEqual(
            receipt_request["issuer_supplied_fields"],
            sorted(["grant_id", "issuer_id", "key_id",
                    "operator_approval_id", "issued_at", "expires_at"]))
        self.assertFalse(receipt_request["permission_to_dispatch"])
        self.assertFalse(receipt_request["brain_simulation_authorized"])
        self.assertNotIn("secret-alpha-expression", output)
        self.assertNotIn("other-private-expression", output)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

        # Each pinned field is exactly the field the existing verifier checks.
        expected = expected_external_consent_fields(
            MainOrchestrator(self.root), task_id,
            prepared["model"], prepared["model_governance"])
        for key, value in expected.items():
            self.assertEqual(receipt_request["fixed_claims"][key], value)

        # A distinct Ed25519 operator issuer can sign the exact advertised
        # claims. Verification is an audit, NOT a dispatch authorization.
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from alpha_research.orchestration.operator_consent import (
            _CONTEXT as CONSENT_CONTEXT, verify_external_consent,
        )
        private = Ed25519PrivateKey.generate()  # test-only, never persisted
        public_b64 = base64.b64encode(private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw)).decode()
        consent_claims = {
            **receipt_request["fixed_claims"],
            "grant_id": "d" * 32,
            "issuer_id": "offline-operator-issuer",
            "key_id": "test-only-key",
            "operator_approval_id": "e" * 32,
            "issued_at": (now - timedelta(seconds=30)).isoformat(),
            "expires_at": (now + timedelta(minutes=4)).isoformat(),
        }
        consent_receipt = {
            "claims": consent_claims,
            "signature": base64.b64encode(private.sign(
                CONSENT_CONTEXT + canonical_json_bytes(consent_claims))).decode(),
        }
        trust_policy = {
            "schema_version": 1,
            "issuers": {"offline-operator-issuer": {
                "key_id": "test-only-key",
                "public_key_b64": public_b64,
                "enabled": True,
                "operations": ["brain_simulation"],
            }},
            "revoked_grants": [],
        }
        verified = verify_external_consent(
            MainOrchestrator(self.root), task_id,
            prepared["model"], prepared["model_governance"],
            consent_receipt, trust_policy, now=now)
        self.assertTrue(verified["signature_verified"])
        self.assertEqual(
            verified["status"],
            "EXTERNAL_SIGNATURE_VERIFIED_NOT_AUTHORIZED")
        self.assertFalse(verified["permission_to_dispatch"])
        self.assertFalse(verified["brain_simulation_authorized"])
        self.assertFalse(verified["alpha_submission_authorized"])

    def test_p4_13_missing_manifest_gets_actionable_safe_error(self):
        code, output, err = self._call(self.prefix + [
            "orchestration-prepare", "--workflow", "super",
            "--objective", "offline missing manifest", "--universe", "TOP3000"])
        self.assertEqual(code, 0, err)
        task_id = json.loads(output)["task_id"]
        before = sorted(str(p) for p in self.root.rglob("*"))
        code, output, err = self._call(self.prefix + [
            "orchestration-scope-issuer-request", task_id,
            "--manifest", str(self.root / "reviewed-simulation-manifest.json")])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("--manifest file not found", err)
        self.assertIn("real path", err)
        self.assertFalse((self.root / "reviewed-simulation-manifest.json").exists())
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_p4_13_scope_request_rejects_unpinned_manifest(self):
        code, output, err = self._call(self.prefix + [
            "orchestration-prepare", "--workflow", "super",
            "--objective", "scope mismatch", "--universe", "TOP3000"])
        self.assertEqual(code, 0, err)
        task_id = json.loads(output)["task_id"]
        plan = {
            "research_id": "super:" + "a" * 32,
            "run_id": "b" * 32, "batch_id": "offline_one",
            "request": {"selection": "rank(x)"},
            "settings": {
                "region": "USA", "delay": 1, "universe": "TOP3000",
                "instrument_type": "EQUITY",
            },
        }
        manifest = self.root / "bad-manifest.json"
        manifest.write_text(json.dumps(plan), encoding="utf-8")
        before = sorted(str(p) for p in self.root.rglob("*"))
        code, output, err = self._call(self.prefix + [
            "orchestration-scope-issuer-request", task_id,
            "--manifest", str(manifest)])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("OrchestrationError", err)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_operator_consent_cli_is_read_only_and_never_authorizes(self):
        status, output, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "super",
                           "--objective", "offline consent status"])
        self.assertEqual(status, 0, error)
        task_id = json.loads(output)["task_id"]
        ledger_dir = (self.root / "state" / "research_v1_1"
                      / "operator_consent_audit")
        self.assertFalse(ledger_dir.exists())
        status, output, error = self._call(
            self.prefix + ["orchestration-consent-status", task_id])
        self.assertEqual(status, 0, error)
        result = json.loads(output)
        self.assertEqual(result["status"], "NOT_RESERVED")
        self.assertFalse(result["permission_to_dispatch"])
        self.assertFalse(ledger_dir.exists(),
                         "read-only status must not create a consent journal")

    def test_p4_7_3_predispatch_status_read_only_no_implicit_escrow(self):
        status, output, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "super",
                           "--objective", "offline pre-dispatch status"])
        self.assertEqual(status, 0, error)
        task_id = json.loads(output)["task_id"]
        escrow_root = (self.root / "state" / "research_v1_1"
                       / "simulation_predispatch_escrow")
        self.assertFalse(escrow_root.exists())
        status, output, error = self._call(
            self.prefix + ["orchestration-predispatch-escrow-status", task_id])
        self.assertEqual(status, 0, error)
        result = json.loads(output)
        self.assertEqual(result["status"], "NOT_ESCROWED")
        self.assertFalse(result["permission_to_dispatch"])
        self.assertFalse(result["automatic_retry_allowed"])
        self.assertFalse(escrow_root.exists(),
                         "read-only escrow status must not create lock/index")

    def test_p4_7_4_recovery_audit_cli_is_offline_and_no_writes(self):
        status, output, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "super",
                           "--objective", "offline recovery classification"])
        self.assertEqual(status, 0, error)
        task_id = json.loads(output)["task_id"]
        state_root = self.root / "state" / "research_v1_1"
        before = sorted(str(p) for p in state_root.rglob("*"))
        status, output, error = self._call(
            self.prefix + ["orchestration-simulation-recovery-audit", task_id])
        self.assertEqual(status, 0, error)
        report = json.loads(output)
        self.assertEqual(report["status"], "AWAITING_OFFLINE_STAGING")
        self.assertIn("SIMULATION_INTENT_NOT_STAGED", report["blockers"])
        self.assertEqual(report["facts"]["intent_status"], "NOT_CREATED")
        self.assertFalse(report["brain_invoked"])
        self.assertFalse(report["provider_invoked"])
        self.assertFalse(report["automatic_retry_allowed"])
        self.assertEqual(before, sorted(str(p) for p in state_root.rglob("*")))

    def test_p4_10_release_gate_binds_local_read_id_without_dispatch(self):
        from alpha_research.orchestration.platform_read_evidence import (
            record_platform_read_evidence,
        )
        result = record_platform_read_evidence(self.root, {
            "status": "READ_ONLY_SCOPE_VERIFIED",
            "region": "CHN", "delay": 1, "universe": "TOP3000",
            "dataset_count": 7,
            "platform_settings_readable": True,
            "dataset_catalog_readable": True,
            "brain_read_invoked": True,
            "simulation_invoked": False, "model_invoked": False,
            "alpha_submission_invoked": False,
        })
        status, output, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "super",
                           "--objective", "local platform observation binding"])
        self.assertEqual(status, 0, error)
        task_id = json.loads(output)["task_id"]
        before = sorted(str(p) for p in self.root.rglob("*"))
        status, output, error = self._call(
            self.prefix + [
                "orchestration-release-gate", task_id,
                "--platform-read-evidence-id", result["record_id"]])
        self.assertEqual(status, 0, error)
        audit = json.loads(output)
        local = audit["components"]["local_platform_read_observation"]
        # Super preparation has no Universe until the exact Simulation
        # is staged: do not infer TOP3000 from unrelated platform evidence.
        self.assertEqual(local["status"], "TASK_SCOPE_NOT_EXACT")
        self.assertFalse(local["permission_to_dispatch"])
        self.assertFalse(audit["production_authorized"])
        self.assertFalse(audit["alpha_submission_authorized"])
        self.assertIn("PLATFORM_READ_LOCAL_ONLY_NOT_INDEPENDENTLY_ATTESTED",
                      audit["blockers"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_p4_8_production_release_gate_cli_is_read_only(self):
        status, output, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "super",
                           "--objective", "release readiness"])
        self.assertEqual(status, 0, error)
        task_id = json.loads(output)["task_id"]
        before = sorted(str(p) for p in self.root.rglob("*"))
        status, output, error = self._call(
            self.prefix + ["orchestration-release-gate", task_id])
        self.assertEqual(status, 0, error)
        result = json.loads(output)
        self.assertIn(result["status"], {
            "PRODUCTION_BLOCKED", "RECONCILIATION_BLOCKED"})
        self.assertIn("EXTERNAL_OPERATOR_CONSENT_ISSUER_NOT_PROVISIONED",
                      result["blockers"])
        self.assertFalse(result["production_authorized"])
        self.assertFalse(result["brain_invoked"])
        self.assertFalse(result["provider_invoked"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")),
                         "release audit must not mutate state")

    def test_p4_8_consent_cli_routes_offline_receipts_without_braindispatch(self):
        status, output, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "super",
                           "--objective", "offline signed consent CLI"])
        self.assertEqual(status, 0, error)
        task_id = json.loads(output)["task_id"]
        receipt = self.root / "external-consent-fixture.json"
        trust = self.root / "external-trust-fixture.json"
        receipt.write_text(json.dumps({"claims": {}, "signature": "fixture"}))
        trust.write_text(json.dumps({
            "schema_version": 1, "issuers": {}, "revoked_grants": []}))
        evidence = {
            "status": "EXTERNAL_SIGNATURE_VERIFIED_NOT_AUTHORIZED",
            "permission_to_dispatch": False,
            "brain_simulation_authorized": False,
            "model_call_authorized": False,
        }
        root = (self.root / "state" / "research_v1_1"
                / "operator_consent_audit")
        with patch("alpha_research.cli.verify_external_consent",
                   return_value=evidence) as verified:
            status, output, error = self._call(
                self.prefix + ["orchestration-consent-verify", task_id,
                               "--receipt", str(receipt),
                               "--trust-policy", str(trust)])
        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(output), evidence)
        verified.assert_called_once()
        self.assertFalse(root.exists())
        with patch("alpha_research.cli.reserve_consent_for_audit",
                   return_value={
                       "status": "RESERVED_CONSENT_AUDIT_ONLY",
                       "permission_to_dispatch": False,
                   }) as reserve:
            status, output, error = self._call(
                self.prefix + ["orchestration-consent-reserve", task_id,
                               "--receipt", str(receipt),
                               "--trust-policy", str(trust)])
        self.assertEqual(status, 0, error)
        self.assertFalse(json.loads(output)["permission_to_dispatch"])
        reserve.assert_called_once()

    def test_p4_8_escrow_reserve_cli_refuses_missing_prerequisites(self):
        status, output, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "super",
                           "--objective", "offline escrow CLI"])
        self.assertEqual(status, 0, error)
        task_id = json.loads(output)["task_id"]
        status, _, error = self._call(
            self.prefix + ["orchestration-predispatch-escrow-reserve", task_id])
        self.assertEqual(status, 2)
        self.assertIn("OrchestrationError", error)
        self.assertFalse((
            self.root / "state" / "research_v1_1" /
            "simulation_predispatch_escrow" / "escrow_index.json").exists())

    def test_recovery_check_is_read_only_for_unstarted_task(self):
        status, output, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "regular",
                           "--objective", "offline"])
        self.assertEqual(status, 0, error)
        task_id = json.loads(output)["task_id"]
        status, output, error = self._call(
            self.prefix + ["orchestration-recovery-check", task_id])
        self.assertEqual(status, 0, error)
        inspected = json.loads(output)
        self.assertEqual(inspected["status"], "NOT_BLOCKED_EXECUTE")
        self.assertFalse(inspected["automatic_replay_allowed"])
        self.assertFalse(inspected["brain_invoked"])
        task = MainOrchestrator(self.root).load(task_id)
        self.assertEqual(task["status"], "CREATED")
        self.assertEqual(task["budget"]["reserved"], 0)

    def test_missing_default_config_uses_safe_builtin_only_for_offline_preparation(self):
        args = [
            "--state-root", str(self.root),
            "--lead", "deepseek", "--executor", "deepseek",
            "orchestration-prepare", "--workflow", "regular",
            "--region", "CHN", "--objective", "Mac without user config",
        ]
        status, output, error = self._call(args)
        self.assertEqual(status, 0, error)
        result = json.loads(output)
        self.assertEqual(result["config_source"], "builtin_offline_defaults")
        self.assertFalse(result["model_call_authorized"])
        task_id = result["task_id"]
        task = MainOrchestrator(self.root).load(task_id)
        self.assertEqual(task["request"]["region"], "CHN")
        self.assertEqual(task["request"]["delay"], 1)
        self.assertFalse((self.root / "config" / "nastu_quant.json").exists())
        for action, expected in (
            ("orchestration-handoff", "AWAITING_DOMAIN_RESEARCH_ID"),
            ("orchestration-recovery-check", "NOT_BLOCKED_EXECUTE"),
        ):
            status, output, error = self._call(
                args[:6] + [action, task_id])
            self.assertEqual(status, 0, error)
            self.assertEqual(json.loads(output)["status"], expected)

    def test_missing_default_config_still_requires_explicit_region(self):
        args = [
            "--state-root", str(self.root),
            "--lead", "deepseek", "--executor", "deepseek",
            "orchestration-prepare", "--workflow", "regular",
            "--objective", "scope required",
        ]
        status, _, error = self._call(args)
        self.assertEqual(status, 2)
        self.assertIn("OrchestrationError", error)
        self.assertFalse((self.root / "state" / "research_v1_1" /
                          "orchestration").exists())

    def test_missing_explicit_config_reports_path_and_does_not_fallback(self):
        missing = self.root / "missing-custom-config.json"
        args = [
            "--state-root", str(self.root), "--config", str(missing),
            "--lead", "deepseek", "--executor", "deepseek",
            "orchestration-prepare", "--workflow", "regular",
            "--region", "CHN", "--objective", "must not bypass config",
        ]
        status, _, error = self._call(args)
        self.assertEqual(status, 2)
        self.assertIn(str(missing), error)
        self.assertIn("research configuration file not found", error)
        self.assertFalse((self.root / "state" / "research_v1_1" /
                          "orchestration").exists())

    def test_present_explicit_config_without_governance_never_gets_fallback(self):
        self.config.write_text(json.dumps({"scope": {"region": "CHN"}}),
                               encoding="utf-8")
        status, _, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "regular",
                           "--objective", "no governance bypass"])
        self.assertEqual(status, 2)
        self.assertIn("missing model_governance", error)

    def test_prepare_requires_both_registered_roles_and_policy(self):
        missing_role = [
            "--state-root", str(self.root), "--config", str(self.config),
            "--lead", "deepseek", "orchestration-prepare",
            "--workflow", "regular", "--objective", "offline",
        ]
        status, _, _ = self._call(missing_role)
        self.assertEqual(status, 2)
        self.assertFalse((self.root / "state" / "research_v1_1" /
                          "orchestration").exists())

    def test_scope_conflicts_and_missing_region_fail_before_creating_task(self):
        inconsistent = self.prefix + [
            "orchestration-prepare", "--workflow", "regular",
            "--objective", "offline", "--region", "USA",
        ]
        status, _, _ = self._call(inconsistent)
        self.assertEqual(status, 2)

        bad_delay = self.prefix + [
            "orchestration-prepare", "--workflow", "regular",
            "--objective", "offline", "--delay", "0",
        ]
        status, _, _ = self._call(bad_delay)
        self.assertEqual(status, 2)

        missing_scope = json.loads(self.config.read_text(encoding="utf-8"))
        missing_scope["scope"]["region"] = None
        self.config.write_text(json.dumps(missing_scope), encoding="utf-8")
        status, _, _ = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "super",
                           "--objective", "offline"])
        self.assertEqual(status, 2)
        self.assertFalse((self.root / "state" / "research_v1_1" /
                          "orchestration").exists())

    def test_inspection_rejects_model_policy_drift(self):
        status, output, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "regular",
                           "--objective", "offline"])
        self.assertEqual(status, 0, error)
        task_id = json.loads(output)["task_id"]
        new_config = json.loads(self.config.read_text(encoding="utf-8"))
        new_config["model_governance"]["max_calls"] = 5
        self.config.write_text(json.dumps(new_config), encoding="utf-8")
        status, _, error = self._call(
            self.prefix + ["orchestration-handoff", task_id])
        self.assertEqual(status, 2)
        self.assertIn("OrchestrationError", error)


if __name__ == "__main__":
    unittest.main()
