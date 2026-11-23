"""P7.4 offline Canary supervision, evidence freshness and stop decision.

This pure decision engine never invokes a real Provider, BRAIN, or the
irreversible admin Stop endpoint. A separately authorized operator operates
the existing P5.5 independent Stop; absence of telemetry is NOT permission.
"""
from datetime import datetime, timezone

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_p5_release import _check_monitor
from alpha_research.state.fingerprint import fingerprint_json

_MONOTONIC = ("consumed_grants", "revoked_grants", "total_attempts",
              "total_committed_usd_micros", "signed_recovery_observations")
_BILLING = {"P7_INVOICE_MATCHED_UNTRUSTED_INPUT", "P7_RECONCILIATION_REQUIRED"}
_BRAIN = {
    "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE",
    "P7_BRAIN_READ_SCOPE_DENIED",
    "P7_BRAIN_READ_STOP_LATCHED",
    "P7_BRAIN_READ_MONITOR_UNAVAILABLE",
}


def _timestamp(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OrchestrationError("P7 Canary observation time invalid") from exc
    else:
        raise OrchestrationError("P7 Canary observation time missing")
    if result.tzinfo is None or result.utcoffset() is None:
        raise OrchestrationError("P7 Canary observation must be timezone aware")
    return result.astimezone(timezone.utc)


def inspect_p7_canary_supervision(*, task_id, monitor=None, observed_at=None,
                                  previous_monitor=None,
                                  previous_observed_at=None,
                                  billing_report=None, brain_report=None,
                                  now=None, max_age_seconds=90):
    """Evaluate a bounded telemetry snapshot and independent evidence chain.

    A missing/stale monitor, Stop latch, any P5 alerts, an UNKNOWN attempt,
    budget pressure or mismatched P7 billing is a fail-closed incident signal.
    A prior snapshot detects counter rollback and Stop resurrection. Neither
    an ARMED local monitor nor untrusted invoice validates a real Canary.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise OrchestrationError("P7 Canary Task ID invalid")
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise OrchestrationError("P7 Canary heartbeat bound invalid")
    current_time = _timestamp(now) if now is not None else datetime.now(timezone.utc)
    if (previous_monitor is None) != (previous_observed_at is None):
        raise OrchestrationError("P7 Canary previous snapshot must be paired")
    if monitor is None and (observed_at is not None or previous_monitor is not None):
        raise OrchestrationError("P7 Canary monitoring evidence incomplete")
    reasons = set()
    age = None
    stop_latched = False
    stop_state = "UNKNOWN"
    digest = None
    if monitor is None:
        reasons.add("P7_MONITOR_MISSING")
    else:
        _check_monitor(monitor)
        if observed_at is None:
            raise OrchestrationError("P7 Canary observation timestamp required")
        timestamp = _timestamp(observed_at)
        precise_age = (current_time - timestamp).total_seconds()
        if precise_age < -5:
            raise OrchestrationError("P7 Canary monitor timestamp in future")
        if precise_age > max_age_seconds:
            reasons.add("P7_MONITOR_STALE")
        age = int(precise_age)
        digest = fingerprint_json(monitor)
        stop_state = monitor["stop_state"]
        stop_latched = stop_state == "STOPPED"
        if stop_latched:
            reasons.add("P7_GLOBAL_STOP_LATCHED")
        if monitor["attempts_by_status"].get("UNKNOWN", 0) > 0:
            reasons.add("P7_UNKNOWN_PROVIDER_OUTCOME")
        if monitor["global_cap_usd_micros"] is None:
            reasons.add("P7_GLOBAL_SPEND_CAP_MISSING")
        if monitor["global_cap_usd_micros"] is not None:
            if 5 * monitor["total_committed_usd_micros"] >= (
                    4 * monitor["global_cap_usd_micros"]):
                reasons.add("P7_GLOBAL_BUDGET_PRESSURE")
        for alert in monitor["alerts"]:
            reasons.add("P7_MONITOR_ALERT_" + alert)
        if previous_monitor is not None:
            _check_monitor(previous_monitor)
            previous_time = _timestamp(previous_observed_at)
            if previous_time >= timestamp:
                raise OrchestrationError("P7 Canary monitoring time regressed")
            if any(monitor[key] < previous_monitor[key] for key in _MONOTONIC):
                reasons.add("P7_COUNTER_REGRESSION")
            old_cap = previous_monitor["global_cap_usd_micros"]
            new_cap = monitor["global_cap_usd_micros"]
            if old_cap is not None and old_cap != new_cap:
                reasons.add("P7_SPEND_POLICY_MUTATED")
            if previous_monitor["stop_state"] == "STOPPED":
                if (monitor["stop_state"] != "STOPPED"
                        or monitor["stop_event_id"] != previous_monitor["stop_event_id"]
                        or monitor["stop_time"] != previous_monitor["stop_time"]):
                    reasons.add("P7_STOP_LATCH_REGRESSION")
    if billing_report is not None:
        if (not isinstance(billing_report, dict)
                or billing_report.get("task_id") != task_id
                or not isinstance(billing_report.get("status"), str)
                or billing_report["status"] not in _BILLING
                or billing_report.get("permission_to_dispatch") is not False
                or billing_report.get("provider_invoice_independently_verified") is not False):
            raise OrchestrationError("P7 Canary billing evidence invalid")
        if billing_report["status"] == "P7_RECONCILIATION_REQUIRED":
            reasons.add("P7_BILLING_RECONCILIATION_REQUIRED")
    if brain_report is not None:
        if (not isinstance(brain_report, dict)
                or brain_report.get("task_id") != task_id
                or not isinstance(brain_report.get("status"), str)
                or brain_report["status"] not in _BRAIN
                or brain_report.get("permission_to_dispatch") is not False
                or brain_report.get("brain_read_invoked") is not False
                or brain_report.get("platform_session_independently_authenticated") is not False):
            raise OrchestrationError("P7 Canary BRAIN read evidence invalid")
        if brain_report["status"] != "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE":
            reasons.add("P7_BRAIN_SESSION_NOT_ADMITTED")
        elif monitor is not None and brain_report["authority_stop_state"] != stop_state:
            reasons.add("P7_BRAIN_MONITOR_STOP_DRIFT")
    # Every incident is a stop/veto recommendation, never an autonomous
    # irreversible administrative action or a live-dispatch capability.
    stop_required = bool(reasons)
    status = ("P7_CANARY_STOP_ALREADY_LATCHED" if stop_latched
              else "P7_CANARY_STOP_REQUIRED" if stop_required
              else "P7_CANARY_OFFLINE_OBSERVATION_ONLY")
    return {
        "schema_version": 1, "task_id": task_id, "status": status,
        "monitor_sha256": digest,
        "monitor_age_seconds": age,
        "stop_state": stop_state, "stop_latched": stop_latched,
        "stop_required": stop_required,
        "reasons": sorted(reasons),
        "billing_report_sha256": (fingerprint_json(billing_report)
                                  if billing_report is not None else None),
        "brain_report_sha256": (fingerprint_json(brain_report)
                                if brain_report is not None else None),
        "operator_admin_stop_required": stop_required and not stop_latched,
        "admin_stop_invoked": False, "watchdog_executed": False,
        "independent_canary_attested": False,
        "live_canary_authorized": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "permission_to_dispatch": False,
    }
