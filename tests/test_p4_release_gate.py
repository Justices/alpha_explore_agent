"""P4.8 release gate must remain negative and entirely read-only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.orchestration.release_gate import (
    inspect_production_release_gate,
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
        self.assertFalse(report["alpha_submission_authorized"])
        self.assertFalse(report["automatic_retry_allowed"])
        self.assertFalse(report["provider_invoked"])
        self.assertFalse(report["brain_invoked"])
        self.assertIn("simulation_dispatch", report["components"])
        self.assertIn("simulation_recovery", report["components"])
        self.assertEqual(before, sorted(str(x) for x in self.root.rglob("*")),
                         "release inspection must not write even a lock")

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

    def test_release_gate_redacts_private_configuration(self):
        marker = "must-not-appear-authorization-secret"
        report = inspect_production_release_gate(
            self.runner, self.super["task_id"], {"api_key": marker},
            _POLICY, config_source="user_config")
        self.assertNotIn(marker, json.dumps(report))
        self.assertFalse(report["production_authorized"])


if __name__ == "__main__":
    unittest.main()
