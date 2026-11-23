"""P5.5 negative-only integrated Canary readiness and production-blocker audit.

The service monitor, local signed reviews and event counters are observations,
not independently authenticated remote transport identity or provider billing.
This gate never emits executable grants or performs external operations.
"""
import re

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_p4_release import (
    inspect_research_p4_release_gate,
)

_BLOCKERS = (
    "REMOTE_SERVICE_IDENTITY_AND_MTLS_NOT_ATTESTED",
    "OPERATOR_CANARY_APPROVAL_NOT_BOUND_TO_LIVE_DISPATCH",
    "LIVE_PROVIDER_READBACK_NOT_INDEPENDENTLY_VERIFIED",
    "PROVIDER_INVOICE_AND_TOKENIZER_NOT_INDEPENDENTLY_VERIFIED",
    "LIVE_PROVIDER_HARD_BUDGET_NOT_ENFORCED",
    "READ_ONLY_BRAIN_DRIVER_NOT_LIVE_AUTHORIZED",
    "SDK_PAID_DISPATCH_NOT_LIVE_AUTHORIZED",
    "PRODUCTION_CANARY_SUPERVISION_NOT_PROVISIONED",
    "SIMULATION_POST_REQUIRES_SEPARATE_APPROVAL",
    "ALPHA_SUBMISSION_REQUIRES_SEPARATE_APPROVAL",
)
_METRIC_KEYS = frozenset({
    "schema_version", "status", "stop_state", "stop_event_id", "stop_time",
    "consumed_grants", "revoked_grants", "attempts_by_status",
    "total_attempts", "total_committed_usd_micros", "global_cap_usd_micros",
    "signed_recovery_observations", "alerts",
    "provider_invoice_independently_verified",
    "independent_service_identity_attested", "real_canary_authorized",
    "permission_to_dispatch", "automatic_retry_allowed",
})
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_ALERT = re.compile(r"[A-Z0-9_]{3,80}\Z")


def _check_monitor(monitor):
    if (not isinstance(monitor, dict)
            or set(monitor) != _METRIC_KEYS
            or type(monitor["schema_version"]) is not int
            or monitor["schema_version"] != 1
            or monitor["status"] != "CANARY_LOCAL_TELEMETRY_AUDIT_ONLY"
            or monitor["stop_state"] not in ("ARMED", "STOPPED")
            or monitor["provider_invoice_independently_verified"] is not False
            or monitor["independent_service_identity_attested"] is not False
            or monitor["real_canary_authorized"] is not False
            or monitor["permission_to_dispatch"] is not False
            or monitor["automatic_retry_allowed"] is not False):
        raise OrchestrationError("research P5 canary monitoring schema invalid")
    for name in ("consumed_grants", "revoked_grants", "total_attempts",
                 "total_committed_usd_micros", "signed_recovery_observations"):
        if type(monitor[name]) is not int or monitor[name] < 0:
            raise OrchestrationError("research P5 canary counters invalid")
    if (not isinstance(monitor["attempts_by_status"], dict)
            or not set(monitor["attempts_by_status"]).issubset(
                {"RESERVED", "UNKNOWN", "SETTLED"})
            or any(type(n) is not int or n < 0
                   for n in monitor["attempts_by_status"].values())
            or sum(monitor["attempts_by_status"].values())
               != monitor["total_attempts"]):
        raise OrchestrationError("research P5 canary attempt counters invalid")
    cap = monitor["global_cap_usd_micros"]
    if cap is not None and (type(cap) is not int or cap < 1):
        raise OrchestrationError("research P5 canary budget cap invalid")
    if (cap is not None
            and monitor["total_committed_usd_micros"] > cap):
        raise OrchestrationError("research P5 canary global reservation drift")
    alerts = monitor["alerts"]
    if (not isinstance(alerts, list)
            or len(alerts) != len(set(alerts))
            or any(not isinstance(s, str) or not _ALERT.fullmatch(s)
                   for s in alerts)):
        raise OrchestrationError("research P5 canary alert schema invalid")
    state = monitor["stop_state"]
    if (state == "ARMED" and
            (monitor["stop_event_id"] is not None
             or monitor["stop_time"] is not None)):
        raise OrchestrationError("research P5 stop state invalid")
    if (state == "STOPPED"
            and (not isinstance(monitor["stop_event_id"], str)
                 or not _HEX32.fullmatch(monitor["stop_event_id"])
                 or not isinstance(monitor["stop_time"], str)
                 or not monitor["stop_time"]
                 or "GLOBAL_STOP_LATCHED" not in alerts)):
        raise OrchestrationError("research P5 stop evidence invalid")
    if (monitor["attempts_by_status"].get("UNKNOWN", 0) > 0
            and "UNRESOLVED_PROVIDER_ATTEMPTS" not in alerts):
        raise OrchestrationError("research P5 unknown attempts suppressed")
    if (cap is None and "GLOBAL_SPEND_POLICY_NOT_INITIALIZED" not in alerts):
        raise OrchestrationError("research P5 spend policy missing silently")


def inspect_research_p5_canary_gate(orchestrator, task_id, *,
                                   authority_monitor=None,
                                   receipt=None, grant_ledger=None,
                                   driver_session=None, model_pricing=None,
                                   remote_reconciliation=None,
                                   canary_approval=None, canary_trust=None,
                                   now=None):
    """Always read-only; all supplied monitor/approval evidence remains audit-only."""
    baseline = inspect_research_p4_release_gate(
        orchestrator, task_id, receipt=receipt, grant_ledger=grant_ledger,
        driver_session=driver_session, model_pricing=model_pricing,
        remote_reconciliation=remote_reconciliation,
        canary_approval=canary_approval, canary_trust=canary_trust, now=now)
    blockers = set(baseline["blockers"]) | set(_BLOCKERS)
    monitor_status = "NOT_SUPPLIED"
    alerts = []
    stopped = baseline["global_stop_or_mock_workspace"]
    if authority_monitor is None:
        blockers.add("INDEPENDENT_AUTHORITY_MONITOR_NOT_SUPPLIED")
    else:
        _check_monitor(authority_monitor)
        monitor_status = "UNTRUSTED_LOCAL_AUDIT_ONLY"
        alerts = list(authority_monitor["alerts"])
        stopped = stopped or authority_monitor["stop_state"] == "STOPPED"
        blockers.add("LOCAL_CANARY_MONITOR_NOT_REMOTE_ATTESTATION")
        if authority_monitor["stop_state"] == "STOPPED":
            blockers.add("INDEPENDENT_CANARY_STOP_LATCHED")
        if authority_monitor["attempts_by_status"].get("UNKNOWN", 0):
            blockers.add("UNRESOLVED_PROVIDER_ATTEMPTS")
        if authority_monitor["global_cap_usd_micros"] is None:
            blockers.add("GLOBAL_SPEND_POLICY_NOT_INITIALIZED")
        for alert in alerts:
            blockers.add("CANARY_ALERT_" + alert)
    return {
        "schema_version": 1, "task_id": task_id,
        "workflow": baseline["workflow"],
        "status": "RESEARCH_P5_CANARY_PRODUCTION_BLOCKED",
        "audit_mode": "READ_ONLY",
        "p4_canary": baseline,
        "authority_monitor": monitor_status,
        "alerts": sorted(set(alerts)),
        "blockers": sorted(blockers),
        "independent_stop_observed": stopped,
        "canary_live_enabled": False,
        "production_authorized": False,
        "brain_read_authorized": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "permission_to_dispatch": False,
        "brain_invoked": False, "provider_invoked": False,
    }
