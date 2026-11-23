"""P4.8 release gate must remain negative and entirely read-only."""
import json
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.orchestration.release_gate import (
    inspect_production_release_gate,
)
from alpha_research.orchestration.platform_read_evidence import (
    record_platform_read_evidence,
)
from alpha_research.orchestration.platform_read_scope_binding import (
    inspect_platform_read_scope_binding,
)

_POLICY = {"max_calls": 1, "max_reported_tokens": 1000, "failover": "manual"}


class ReleaseGateOfflineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runner = MainOrchestrator(self.root)
        self.super = self.runner.create(
            "super", {"objective": "offline release audit",
                      "region": "USA", "delay": 1, "universe": "TOP3000"})

    def test_super_release_is_blocked_even_with_valid_local_evidence(self):
        before = sorted(str(x) for x in self.root.rglob("*"))
        with (patch("alpha_research.integrations.brain.gateway.Gateway",
                    side_effect=AssertionError("release cannot connect to BRAIN")),
              patch("alpha_research.integrations.model.build_model_client",
                    side_effect=AssertionError("release cannot call paid models"))):
            report = inspect_production_release_gate(
                self.runner, self.super["task_id"], {}, _POLICY,
                config_source="builtin_offline_defaults")
        self.assertIn(report["status"], {"PRODUCTION_BLOCKED", "RECONCILIATION_BLOCKED"})
        self.assertIn("EXTERNAL_OPERATOR_CONSENT_ISSUER_NOT_PROVISIONED",
                      report["blockers"])
        self.assertIn("LIVE_SINGLE_USE_GRANT_CONSUMER_NOT_IMPLEMENTED",
                      report["blockers"])
        self.assertIn("PLATFORM_READBACK_RECOVERY_NOT_VERIFIED",
                      report["blockers"])
        self.assertIn("PRODUCTION_GATEWAY_ADAPTER_DISABLED",
                      report["blockers"])
        self.assertEqual(report["audit_mode"], "READ_ONLY")
        self.assertFalse(report["production_authorized"])
        self.assertFalse(report["brain_simulation_authorized"])
        self.assertFalse(report["model_call_authorized"])
        self.assertIs(report["brain_read_authorized"], False)
        self.assertIs(report["permission_to_dispatch"], False)
        self.assertFalse(report["alpha_submission_authorized"])
        self.assertFalse(report["automatic_retry_allowed"])
        self.assertFalse(report["provider_invoked"])
        self.assertFalse(report["brain_invoked"])
        self.assertIn("simulation_dispatch", report["components"])
        self.assertIn("simulation_recovery", report["components"])
        self.assertEqual(before, sorted(str(x) for x in self.root.rglob("*")),
                         "release inspection must not write even a lock")

    def test_p4_12_unstaged_fresh_super_is_preparation_not_reconciliation(self):
        before = sorted(str(x) for x in self.root.rglob("*"))
        with (patch("alpha_research.integrations.brain.gateway.Gateway",
                    side_effect=AssertionError("recovery inspection cannot contact BRAIN")),
              patch("alpha_research.integrations.model.build_model_client",
                    side_effect=AssertionError("recovery inspection cannot call a model"))):
            report = inspect_production_release_gate(
                self.runner, self.super["task_id"], {}, _POLICY,
                config_source="builtin_offline_defaults")
        recovery = report["components"]["simulation_recovery"]
        self.assertEqual(recovery["status"], "AWAITING_OFFLINE_STAGING")
        self.assertEqual(recovery["facts"]["intent_status"], "NOT_CREATED")
        self.assertIn("SIMULATION_INTENT_NOT_STAGED", recovery["blockers"])
        self.assertNotIn("INTENT_MISSING_OR_SYMLINKED", recovery["blockers"])
        self.assertEqual(report["status"], "PRODUCTION_BLOCKED")
        self.assertFalse(report["production_authorized"])
        self.assertFalse(recovery["permission_to_dispatch"])
        self.assertFalse(recovery["automatic_retry_allowed"])
        self.assertEqual(before, sorted(str(x) for x in self.root.rglob("*")))

    def test_p4_12_unexpected_intent_requires_reconciliation_not_preparation(self):
        path = (self.root / "state" / "research_v1_1" /
                "simulation_attempt_admission" / "intents" /
                (self.super["task_id"] + ".intent.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":"POSTING"}', encoding="utf-8")
        from alpha_research.orchestration.simulation_recovery_audit import (
            inspect_simulation_recovery_evidence,
        )
        report = inspect_simulation_recovery_evidence(
            self.runner, self.super["task_id"], _POLICY)
        self.assertEqual(report["status"], "RECONCILIATION_REQUIRED")
        self.assertIn("SIMULATION_STAGING_RECONCILIATION_REQUIRED",
                      report["blockers"])
        self.assertFalse(report["permission_to_dispatch"])
        release = inspect_production_release_gate(
            self.runner, self.super["task_id"], {}, _POLICY,
            config_source="builtin_offline_defaults")
        self.assertEqual(release["status"], "RECONCILIATION_BLOCKED")
        self.assertFalse(release["production_authorized"])

    def test_regular_alpha_production_is_independently_disabled(self):
        regular = self.runner.create(
            "regular", {"objective": "offline", "region": "USA", "delay": 1})
        report = inspect_production_release_gate(
            self.runner, regular["task_id"], {}, _POLICY,
            config_source="builtin_offline_defaults")
        self.assertIn("REGULAR_ALPHA_WRITE_PATH_NOT_PRODUCTION_ADMITTED",
                      report["blockers"])
        self.assertNotIn("simulation_dispatch", report["components"])
        self.assertFalse(report["alpha_submission_authorized"])
        self.assertIs(report["brain_read_authorized"], False)
        self.assertIs(report["permission_to_dispatch"], False)

    def _local_probe(self, *, region="USA", universe="TOP3000"):
        return {
            "status": "READ_ONLY_SCOPE_VERIFIED",
            "region": region, "delay": 1, "universe": universe,
            "dataset_count": 286,
            "platform_settings_readable": True,
            "dataset_catalog_readable": True,
            "brain_read_invoked": True,
            "simulation_invoked": False,
            "model_invoked": False,
            "alpha_submission_invoked": False,
        }

    def test_p4_10_local_read_scope_is_reported_but_not_authority(self):
        record = record_platform_read_evidence(
            self.root, self._local_probe())
        before = sorted(str(p) for p in self.root.rglob("*"))
        with (patch("alpha_research.integrations.brain.gateway.Gateway",
                    side_effect=AssertionError("audit must not open BRAIN")),
              patch("alpha_research.integrations.model.build_model_client",
                    side_effect=AssertionError("audit must not invoke model"))):
            result = inspect_production_release_gate(
                self.runner, self.super["task_id"], {}, _POLICY,
                config_source="user_config",
                platform_read_evidence_id=record["record_id"])
        local = result["components"]["local_platform_read_observation"]
        self.assertEqual(local["status"], "LOCAL_SCOPE_MATCH_UNATTESTED")
        self.assertEqual(local["dataset_count"], 286)
        self.assertEqual(local["scope"], {
            "region": "USA", "delay": 1, "universe": "TOP3000"})
        self.assertFalse(local["platform_account_verified"])
        self.assertFalse(local["permission_to_dispatch"])
        self.assertFalse(result["brain_simulation_authorized"])
        self.assertFalse(result["production_authorized"])
        self.assertIn("PLATFORM_READ_LOCAL_ONLY_NOT_INDEPENDENTLY_ATTESTED",
                      result["blockers"])
        self.assertIn("LIVE_SINGLE_USE_GRANT_CONSUMER_NOT_IMPLEMENTED",
                      result["blockers"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")),
                         "binding audit must never mutate files or create locks")

    def test_p4_10_mismatched_or_bad_read_record_fails_closed(self):
        mismatch = record_platform_read_evidence(
            self.root, self._local_probe(region="CHN"))
        wrong_scope = inspect_platform_read_scope_binding(
            self.runner, self.super["task_id"], mismatch["record_id"])
        self.assertEqual(wrong_scope["status"], "LOCAL_SCOPE_MISMATCH")
        self.assertFalse(wrong_scope["permission_to_dispatch"])
        for bad_id in ("../../etc/passwd", "f" * 32, True):
            with self.subTest(bad_id=bad_id):
                result = inspect_platform_read_scope_binding(
                    self.runner, self.super["task_id"], bad_id)
                self.assertEqual(
                    result["status"], "EVIDENCE_MISSING_OR_CHANGED")
                self.assertFalse(result["production_authorized"])
        unknown = self.runner.create(
            "super", {"objective": "missing universe", "region": "USA",
                      "delay": 1})
        local = record_platform_read_evidence(self.root, self._local_probe())
        result = inspect_platform_read_scope_binding(
            self.runner, unknown["task_id"], local["record_id"])
        self.assertEqual(result["status"], "TASK_SCOPE_NOT_EXACT")
        self.assertFalse(result["brain_simulation_authorized"])

    def test_p4_10_expired_local_observation_is_not_live_proof(self):
        evidence = record_platform_read_evidence(
            self.root, self._local_probe())
        # The local record can be complete but too old for a current audit.
        now = datetime.fromisoformat(evidence["recorded_at"])
        stale = inspect_platform_read_scope_binding(
            self.runner, self.super["task_id"], evidence["record_id"],
            now=now + timedelta(hours=25))
        self.assertEqual(stale["status"], "LOCAL_EVIDENCE_STALE_OR_INVALID_TIME")
        self.assertFalse(stale["permission_to_dispatch"])
        future = inspect_platform_read_scope_binding(
            self.runner, self.super["task_id"], evidence["record_id"],
            now=now - timedelta(minutes=1))
        self.assertEqual(future["status"], "LOCAL_EVIDENCE_STALE_OR_INVALID_TIME")

    def test_p4_10_tamper_detected_across_scope_binding_and_release(self):
        evidence = record_platform_read_evidence(
            self.root, self._local_probe())
        path = (self.root / "state" / "research_v1_1" /
                "platform_read_evidence" / (evidence["record_id"] + ".json"))
        altered = json.loads(path.read_text(encoding="utf-8"))
        altered["report"]["universe"] = "TOP500"
        path.write_text(json.dumps(altered), encoding="utf-8")
        audit = inspect_production_release_gate(
            self.runner, self.super["task_id"], {}, _POLICY,
            config_source="user_config",
            platform_read_evidence_id=evidence["record_id"])
        local = audit["components"]["local_platform_read_observation"]
        self.assertEqual(local["status"], "EVIDENCE_MISSING_OR_CHANGED")
        self.assertIn(
            "LOCAL_PLATFORM_SCOPE_EVIDENCE_EVIDENCE_MISSING_OR_CHANGED",
            audit["blockers"])
        self.assertFalse(audit["production_authorized"])

    def test_release_gate_redacts_private_configuration(self):
        marker = "must-not-appear-authorization-secret"
        report = inspect_production_release_gate(
            self.runner, self.super["task_id"], {"api_key": marker},
            _POLICY, config_source="user_config")
        self.assertNotIn(marker, json.dumps(report))
        self.assertFalse(report["production_authorized"])


if __name__ == "__main__":
    unittest.main()
