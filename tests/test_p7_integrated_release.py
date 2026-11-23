"""P7.3-7.5 offline-only module regression and cross-contract tests.

The signed session verifier itself is covered in P4.26; here its verified
negative outcome is injected to isolate the BRAIN frame/supervisor logic.
No BRAIN session, Provider SDK, admin stop, or Simulation is invoked.
"""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.research_p7_billing import (
    inspect_p7_provider_invoice_batch,
)
from alpha_research.orchestration.research_p7_brain_read import (
    inspect_p7_brain_read_session, inspect_p7_brain_readback_observation,
)
from alpha_research.orchestration.research_p7_canary import (
    inspect_p7_canary_supervision,
)
from alpha_research.orchestration.research_p7_release import (
    inspect_p7_integrated_release,
)
from alpha_research.orchestration.release_gate import (
    inspect_production_release_gate,
)
from alpha_research.state.fingerprint import fingerprint_json

_NOW = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)
_SCOPE = {"region": "USA", "delay": 1, "universe": "TOP3000",
          "instrument_type": "EQUITY"}


def _monitor(*, stop=False, unknown=0, committed=0, consumed=1,
             cap=1000):
    alerts = []
    if stop:
        alerts.append("GLOBAL_STOP_LATCHED")
    if unknown:
        alerts.append("UNRESOLVED_PROVIDER_ATTEMPTS")
    if cap is None:
        alerts.append("GLOBAL_SPEND_POLICY_NOT_INITIALIZED")
    elif 5 * committed >= 4 * cap:
        alerts.append("GLOBAL_RESERVED_BUDGET_AT_LEAST_80_PERCENT")
    return {
        "schema_version": 1, "status": "CANARY_LOCAL_TELEMETRY_AUDIT_ONLY",
        "stop_state": "STOPPED" if stop else "ARMED",
        "stop_event_id": "a" * 32 if stop else None,
        "stop_time": _NOW.isoformat() if stop else None,
        "consumed_grants": consumed, "revoked_grants": 0,
        "attempts_by_status": {"RESERVED": 0, "UNKNOWN": unknown},
        "total_attempts": unknown,
        "total_committed_usd_micros": committed,
        "global_cap_usd_micros": cap,
        "signed_recovery_observations": 0,
        "alerts": alerts,
        "provider_invoice_independently_verified": False,
        "independent_service_identity_attested": False,
        "real_canary_authorized": False,
        "permission_to_dispatch": False,
        "automatic_retry_allowed": False,
    }


class P7IntegratedOfflineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.orch = MainOrchestrator(Path(temporary.name))
        self.task_id = self.orch.create("super", {
            "objective": "P7 offline scoped audit",
            "region": "USA", "delay": 1, "universe": "TOP3000",
        })["task_id"]
        self.monitor = _monitor()
        self.frames = [{"cmd": "get_datasets", **_SCOPE}]
        self.billing = inspect_p7_provider_invoice_batch(
            task_id=self.task_id, provider_hostname="api.deepseek.com",
            invoice_id="invoice-example-1",
            rates={"input_rate_micros_per_million": 1000000,
                   "output_rate_micros_per_million": 2000000},
            reservations=[{"attempt_id": "a1",
                           "provider_operation_id": "op1",
                           "reserved_usd_micros": 1000}],
            invoice_rows=[{"attempt_id": "a1",
                           "provider_operation_id": "op1",
                           "input_tokens": 100, "output_tokens": 50,
                           "billed_usd_micros": 200}])

    def session(self, *, frames=None, monitor=None):
        session = {
            "schema_version": 1, "task_id": self.task_id,
            "status": "DRIVER_SESSION_SIGNATURE_VERIFIED_AUDIT_ONLY",
            "scope": dict(_SCOPE), "source_alpha_id": None,
            "allowed_read_actions": ["get_datasets"],
            "permission_to_dispatch": False,
            "brain_read_authorized": False,
            "platform_session_independently_verified": False,
            "driver_started": False,
        }
        with patch(
            "alpha_research.orchestration.research_p7_brain_read"
            ".inspect_driver_session_evidence", return_value=session
        ) as verifier:
            result = inspect_p7_brain_read_session(
                self.orch, self.task_id, {"claims": {}}, {"claims": {}},
                {"claims": {"task_id": self.task_id}}, {"issuers": {}},
                frames=frames if frames is not None else self.frames,
                authority_monitor=monitor if monitor is not None else self.monitor,
                now=_NOW)
            verifier.assert_called_once()
        return result

    def canary(self, *, monitor=None, brain=None, billing=None, observed_at=None):
        return inspect_p7_canary_supervision(
            task_id=self.task_id,
            monitor=monitor if monitor is not None else self.monitor,
            observed_at=observed_at or _NOW,
            now=_NOW,
            brain_report=brain,
            billing_report=billing)

    def test_brain_allowlist_only_audits_no_dispatch(self):
        report = self.session()
        self.assertEqual(report["status"], "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE")
        self.assertTrue(report["local_offline_admission_passed"])
        self.assertFalse(report["brain_read_invoked"])
        self.assertFalse(report["permission_to_dispatch"])
        observation = inspect_p7_brain_readback_observation(
            report, frame_index=0,
            observed_response={"task_id": self.task_id,
                               "frame_sha256": report["frame_digests"][0],
                               "scope": dict(_SCOPE),
                               "http_status": 200, "record_count": 10,
                               "payload_sha256": "c" * 64})
        self.assertFalse(observation["platform_payload_authenticated"])

    def test_brain_rejects_mutations_scope_drift_and_duplicates(self):
        for bad in (
            {"cmd": "simulate_batch", **_SCOPE},
            {"cmd": "get_datasets", **{**_SCOPE, "region": "CHN"}},
            {"cmd": "get_datasets", **_SCOPE, "permission_to_dispatch": True},
            {"cmd": "get_alpha_details", "alpha_id": "unapproved"},
        ):
            with self.subTest(frame=bad):
                report = self.session(frames=[bad])
                self.assertEqual(report["status"], "P7_BRAIN_READ_SCOPE_DENIED")
                self.assertFalse(report["local_offline_admission_passed"])
        with self.assertRaises(OrchestrationError):
            self.session(frames=[self.frames[0], self.frames[0]])

    def test_brain_stop_and_missing_monitor_veto(self):
        stopped = self.session(monitor=_monitor(stop=True))
        self.assertEqual(stopped["status"], "P7_BRAIN_READ_STOP_LATCHED")
        self.assertFalse(stopped["local_offline_admission_passed"])
        with patch(
            "alpha_research.orchestration.research_p7_brain_read"
            ".inspect_driver_session_evidence",
            return_value={
                "task_id": self.task_id, "status":
                "DRIVER_SESSION_SIGNATURE_VERIFIED_AUDIT_ONLY",
                "scope": dict(_SCOPE), "source_alpha_id": None,
                "allowed_read_actions": ["get_datasets"],
                "permission_to_dispatch": False, "brain_read_authorized": False,
                "platform_session_independently_verified": False,
                "driver_started": False,
            }):
            without_monitor = inspect_p7_brain_read_session(
                self.orch, self.task_id, {}, {}, {}, {},
                frames=self.frames, authority_monitor=None)
        self.assertEqual(without_monitor["status"],
                         "P7_BRAIN_READ_MONITOR_UNAVAILABLE")

    def test_readback_rejects_cross_task_and_frame(self):
        report = self.session()
        wrong = {"task_id": "f" * 32,
                 "frame_sha256": report["frame_digests"][0],
                 "scope": dict(_SCOPE), "http_status": 200,
                 "record_count": 1, "payload_sha256": "d" * 64}
        with self.assertRaises(OrchestrationError):
            inspect_p7_brain_readback_observation(
                report, frame_index=0, observed_response=wrong)

    def test_canary_healthy_is_observation_only(self):
        report = self.canary()
        self.assertEqual(report["status"], "P7_CANARY_OFFLINE_OBSERVATION_ONLY")
        self.assertFalse(report["stop_required"])
        self.assertFalse(report["admin_stop_invoked"])
        self.assertFalse(report["live_canary_authorized"])

    def test_canary_alerts_stale_and_unknown_trigger_stop(self):
        report = self.canary(monitor=_monitor(unknown=1))
        self.assertEqual(report["status"], "P7_CANARY_STOP_REQUIRED")
        self.assertIn("P7_UNKNOWN_PROVIDER_OUTCOME", report["reasons"])
        stale = self.canary(observed_at=_NOW - timedelta(minutes=5))
        self.assertIn("P7_MONITOR_STALE", stale["reasons"])
        stopped = self.canary(monitor=_monitor(stop=True))
        self.assertEqual(stopped["status"], "P7_CANARY_STOP_ALREADY_LATCHED")
        self.assertFalse(stopped["operator_admin_stop_required"])

    def test_canary_snapshot_rollback_and_budget_pressure(self):
        current = _monitor(committed=800, consumed=1)
        earlier = _monitor(consumed=2)
        report = inspect_p7_canary_supervision(
            task_id=self.task_id, monitor=current, observed_at=_NOW,
            previous_monitor=earlier,
            previous_observed_at=_NOW - timedelta(seconds=20),
            now=_NOW)
        self.assertIn("P7_COUNTER_REGRESSION", report["reasons"])
        self.assertIn("P7_GLOBAL_BUDGET_PRESSURE", report["reasons"])

    def test_p7_release_links_all_three_evidence_digests(self):
        brain = self.session()
        canary = self.canary(brain=brain, billing=self.billing)
        release = inspect_p7_integrated_release(
            self.orch, self.task_id, billing_report=self.billing,
            brain_report=brain, canary_report=canary)
        self.assertEqual(release["status"], "P7_PRODUCTION_BLOCKED")
        self.assertEqual(release["milestones"]["p7_3_brain_read"],
                         "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE")
        self.assertFalse(release["production_authorized"])
        self.assertIn("P7_REAL_PROVIDER_INVOICE_SOURCE_NOT_ATTESTED",
                      release["blockers"])
        forged = {**canary, "brain_report_sha256": "f" * 64}
        with self.assertRaises(OrchestrationError):
            inspect_p7_integrated_release(
                self.orch, self.task_id, billing_report=self.billing,
                brain_report=brain, canary_report=forged)
        altered = copy.deepcopy(self.billing)
        altered["findings"][0]["billed_usd_micros"] += 1
        with self.assertRaises(OrchestrationError):
            inspect_p7_integrated_release(
                self.orch, self.task_id, billing_report=altered)

    def test_canary_detects_irreversible_stop_resurrection(self):
        report = inspect_p7_canary_supervision(
            task_id=self.task_id, monitor=_monitor(),
            observed_at=_NOW,
            previous_monitor=_monitor(stop=True),
            previous_observed_at=_NOW - timedelta(seconds=20),
            now=_NOW)
        self.assertIn("P7_STOP_LATCH_REGRESSION", report["reasons"])
        self.assertTrue(report["operator_admin_stop_required"])
        self.assertFalse(report["permission_to_dispatch"])

    def test_reconciliation_cannot_be_overwritten_by_clean_canary(self):
        mismatch = inspect_p7_provider_invoice_batch(
            task_id=self.task_id, provider_hostname="api.deepseek.com",
            invoice_id="invoice-variance",
            rates={"input_rate_micros_per_million": 1000000,
                   "output_rate_micros_per_million": 2000000},
            reservations=[{"attempt_id": "a1",
                           "provider_operation_id": "op1",
                           "reserved_usd_micros": 1000}],
            invoice_rows=[{"attempt_id": "a1",
                           "provider_operation_id": "op1",
                           "input_tokens": 100, "output_tokens": 50,
                           "billed_usd_micros": 210}])
        brain = self.session()
        canary = self.canary(brain=brain, billing=mismatch)
        result = inspect_p7_integrated_release(
            self.orch, self.task_id, billing_report=mismatch,
            brain_report=brain, canary_report=canary)
        self.assertIn("P7_BILLING_RECONCILIATION_REQUIRED",
                      result["blockers"])
        self.assertEqual(result["milestones"]["p7_2_billing"],
                         "P7_RECONCILIATION_REQUIRED")
        self.assertFalse(result["production_authorized"])

    def test_forged_positive_permission_is_rejected(self):
        report = self.session()
        forged = {**report, "brain_read_invoked": True}
        with self.assertRaises(OrchestrationError):
            inspect_p7_integrated_release(
                self.orch, self.task_id, brain_report=forged)
        healthy = self.canary()
        forged = {**healthy, "admin_stop_invoked": True}
        with self.assertRaises(OrchestrationError):
            inspect_p7_integrated_release(
                self.orch, self.task_id, canary_report=forged)

    def test_brain_release_rejects_contradictory_state_and_scope(self):
        good = self.session()
        for mutation in (
            {"status": "P7_BRAIN_READ_SCOPE_DENIED"},
            {"status": "P7_BRAIN_READ_STOP_LATCHED"},
            {"authority_stop_state": "STOPPED"},
            {"local_offline_admission_passed": False},
            {"read_only_frames_valid": False},
            {"scope": {**_SCOPE, "region": "CHN"}},
            {"source_alpha_id": "other-alpha"},
        ):
            with self.subTest(mutation=mutation):
                bad = {**good, **mutation}
                with self.assertRaises(OrchestrationError):
                    inspect_p7_integrated_release(
                        self.orch, self.task_id, brain_report=bad)

    def test_billing_release_rejects_forged_invoice_metadata(self):
        for mutation in (
            {"provider_hostname": "api.evil.example"},
            {"currency": "CNY"},
            {"invoice_id": ""},
            {"invoice_rows_sha256": "wrong"},
            {"reservation_rows_sha256": None},
            {"independent_invoice_source_authenticated": True},
        ):
            with self.subTest(mutation=mutation):
                forged = {**self.billing, **mutation}
                with self.assertRaises(OrchestrationError):
                    inspect_p7_integrated_release(
                        self.orch, self.task_id, billing_report=forged)

    def test_canary_release_rejects_suppressed_incidents(self):
        brain = self.session()
        clean = self.canary(brain=brain, billing=self.billing)
        stopped = self.canary(monitor=_monitor(stop=True))
        for forged in (
            {**stopped, "reasons": []},
            {**clean, "monitor_age_seconds": None},
            {**clean, "reasons": ["P7_MONITOR_MISSING"],
             "status": "P7_CANARY_STOP_REQUIRED", "stop_required": True,
             "operator_admin_stop_required": True},
        ):
            with self.subTest(forged=forged):
                with self.assertRaises(OrchestrationError):
                    inspect_p7_integrated_release(
                        self.orch, self.task_id, canary_report=forged)
        mismatched = copy.deepcopy(self.billing)
        mismatched["findings"][0]["billed_usd_micros"] += 10
        mismatched["findings"][0]["variance_usd_micros"] += 10
        mismatched["variance_total_usd_micros"] += 10
        mismatched["billed_total_usd_micros"] += 10
        mismatched["status"] = "P7_RECONCILIATION_REQUIRED"
        forged = {**clean,
                  "billing_report_sha256": fingerprint_json(mismatched)}
        with self.assertRaises(OrchestrationError):
            inspect_p7_integrated_release(
                self.orch, self.task_id, billing_report=mismatched,
                brain_report=brain, canary_report=forged)

    def test_readback_rejects_report_with_forged_admission(self):
        clean = self.session()
        response = {
            "task_id": self.task_id, "frame_sha256": clean["frame_digests"][0],
            "scope": dict(_SCOPE), "http_status": 200, "record_count": 1,
            "payload_sha256": "c" * 64,
        }
        for mutation in (
            {"local_offline_admission_passed": False},
            {"read_only_frames_valid": False},
            {"authority_stop_state": "STOPPED"},
            {"frame_digests": None},
            {"frame_count": 0},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(OrchestrationError):
                    inspect_p7_brain_readback_observation(
                        {**clean, **mutation}, frame_index=0,
                        observed_response=response)

    def test_p7_rejects_non_string_enum_and_unhashable_inputs(self):
        cases = (
            ("billing", {"status": []}),
            ("billing", {"provider_hostname": []}),
            ("brain", {"status": []}),
            ("brain", {"authority_stop_state": []}),
            ("canary", {"status": []}),
            ("canary", {"stop_state": []}),
        )
        reports = {
            "billing": self.billing, "brain": self.session(),
            "canary": self.canary(),
        }
        for kind, mutation in cases:
            with self.subTest(kind=kind, mutation=mutation):
                data = {**reports[kind], **mutation}
                kwargs = {kind + "_report": data}
                with self.assertRaises(OrchestrationError):
                    inspect_p7_integrated_release(self.orch, self.task_id, **kwargs)

    def test_readback_scope_rejects_bool_int_equality(self):
        clean = self.session()
        response = {
            "task_id": self.task_id, "frame_sha256": clean["frame_digests"][0],
            "scope": {**_SCOPE, "delay": True},
            "http_status": 200, "record_count": 1,
            "payload_sha256": "c" * 64,
        }
        with self.assertRaises(OrchestrationError):
            inspect_p7_brain_readback_observation(
                clean, frame_index=0, observed_response=response)

    def test_canary_precise_heartbeat_expiry(self):
        report = self.canary(observed_at=_NOW - timedelta(seconds=90, milliseconds=500))
        self.assertIn("P7_MONITOR_STALE", report["reasons"])
        self.assertTrue(report["stop_required"])

    def test_top_level_release_includes_p7_and_stays_blocked(self):
        result = inspect_production_release_gate(
            self.orch, self.task_id, {},
            {"max_calls": 1, "max_reported_tokens": 1000,
             "failover": "manual"},
            config_source="builtin_offline_defaults")
        self.assertIn("research_p7_acceptance", result["components"])
        self.assertEqual(
            result["components"]["research_p7_acceptance"]["status"],
            "P7_PRODUCTION_BLOCKED")
        self.assertFalse(result["production_authorized"])


if __name__ == "__main__":
    unittest.main()
