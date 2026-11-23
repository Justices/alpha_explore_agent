"""P7.5 combined offline production acceptance and cross-module audit.

All P7 evidence is observational: P7.2 invoice rows may be caller-supplied,
P7.3 signed driver observations are not authenticated live BRAIN sessions,
and P7.4 local telemetry is not a production watchdog deployment.
Never emit production authority based on locally constructed reports.
"""
import re

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_p6_readiness import (
    inspect_p6_integrated_release,
)
from alpha_research.state.fingerprint import fingerprint_json

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_BILLING = {"P7_INVOICE_MATCHED_UNTRUSTED_INPUT", "P7_RECONCILIATION_REQUIRED"}
_BRAIN = {
    "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE",
    "P7_BRAIN_READ_SCOPE_DENIED",
    "P7_BRAIN_READ_STOP_LATCHED",
    "P7_BRAIN_READ_MONITOR_UNAVAILABLE",
}
_CANARY = {
    "P7_CANARY_STOP_ALREADY_LATCHED", "P7_CANARY_STOP_REQUIRED",
    "P7_CANARY_OFFLINE_OBSERVATION_ONLY",
}


def _check_negative(report, task_id, mandatory_flags):
    if (not isinstance(report, dict)
            or report.get("task_id") != task_id
            or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1
            or report.get("permission_to_dispatch") is not False
            or any(report.get(name) is not False for name in mandatory_flags)):
        raise OrchestrationError("P7 release component contract invalid")


def _check_billing(report, task_id):
    _check_negative(report, task_id, (
        "provider_invoice_independently_verified", "settlement_authorized",
        "budget_refund_authorized", "real_provider_invoked",
        "automatic_retry_allowed"))
    rows = report.get("findings")
    if (report.get("status") not in _BILLING or not isinstance(rows, list)
            or not rows or len(rows) != report.get("operation_count")
            or type(report.get("operation_count")) is not int
            or len(rows) > 10000):
        raise OrchestrationError("P7 billing report shape invalid")
    totals = [0, 0, 0]
    mismatched = False
    attempts, operations = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise OrchestrationError("P7 billing finding invalid")
        attempt, operation = row.get("attempt_id"), row.get("provider_operation_id")
        if (not isinstance(attempt, str) or not attempt
                or not isinstance(operation, str) or not operation
                or attempt in attempts or operation in operations):
            raise OrchestrationError("P7 billing finding duplicate")
        attempts.add(attempt)
        operations.add(operation)
        amounts = [row.get(k) for k in (
            "reserved_usd_micros", "estimated_usd_micros", "billed_usd_micros")]
        if any(type(value) is not int or value < 0 for value in amounts):
            raise OrchestrationError("P7 billing finding cost invalid")
        variance = row.get("variance_usd_micros")
        breach = amounts[1] > amounts[0] or amounts[2] > amounts[0]
        if (type(variance) is not int or variance != amounts[2] - amounts[1]
                or row.get("reservation_breached") is not breach):
            raise OrchestrationError("P7 billing finding variance drift")
        for index in range(3):
            totals[index] += amounts[index]
        mismatched |= variance != 0 or breach
    if (totals != [report.get("reserved_total_usd_micros"),
                   report.get("estimated_total_usd_micros"),
                   report.get("billed_total_usd_micros")]
            or report.get("variance_total_usd_micros") != totals[2] - totals[1]
            or (report["status"] == "P7_RECONCILIATION_REQUIRED") != mismatched):
        raise OrchestrationError("P7 billing aggregation or verdict drift")


def _check_brain(report, task_id):
    _check_negative(report, task_id, (
        "platform_session_independently_authenticated",
        "independent_brain_read_permission_verified", "driver_started",
        "brain_read_invoked", "brain_simulation_invoked", "alpha_submission_invoked",
        "automatic_retry_allowed"))
    frames = report.get("frame_digests")
    if (report.get("status") not in _BRAIN
            or not isinstance(frames, list)
            or not 1 <= len(frames) <= 64
            or report.get("frame_count") != len(frames)
            or any(not isinstance(h, str) or _HEX64.fullmatch(h) is None
                   for h in frames)
            or len(set(frames)) != len(frames)
            or (report["status"] == "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE"
                and (report.get("read_only_frames_valid") is not True
                     or report.get("authority_stop_state") != "ARMED"
                     or report.get("local_offline_admission_passed") is not True))):
        raise OrchestrationError("P7 BRAIN audit report drift")


def _check_canary(report, task_id):
    _check_negative(report, task_id, (
        "admin_stop_invoked", "watchdog_executed",
        "independent_canary_attested", "live_canary_authorized",
        "model_call_authorized", "brain_read_authorized",
        "brain_simulation_authorized", "alpha_submission_authorized",
        "automatic_retry_allowed"))
    reasons = report.get("reasons")
    if (report.get("status") not in _CANARY
            or not isinstance(reasons, list)
            or any(not isinstance(value, str) or not value
                   for value in reasons)
            or reasons != sorted(set(reasons))
            or report.get("stop_required") is not bool(reasons)
            or report.get("stop_latched") is not (
                report["status"] == "P7_CANARY_STOP_ALREADY_LATCHED")
            or report.get("operator_admin_stop_required") is not (
                bool(reasons) and not report["stop_latched"])
            or (report["status"] == "P7_CANARY_OFFLINE_OBSERVATION_ONLY"
                and (reasons or report.get("stop_state") != "ARMED"))):
        raise OrchestrationError("P7 Canary supervision verdict drift")
    expected_status = (
        "P7_CANARY_STOP_ALREADY_LATCHED"
        if report["stop_latched"] else
        "P7_CANARY_STOP_REQUIRED" if reasons else
        "P7_CANARY_OFFLINE_OBSERVATION_ONLY")
    if report["status"] != expected_status:
        raise OrchestrationError("P7 Canary status does not match incident state")


def inspect_p7_integrated_release(orchestrator, task_id, *, billing_report=None,
                                  brain_report=None, canary_report=None):
    """Combine complete P7 module audits with the existing P6 negative gate.

    Cross-check Task identity, immutable report digests and P7.2 arithmetic
    without trusting report status strings as external attestations.
    """
    task = orchestrator.load(task_id)
    if task.get("workflow") not in {"super", "optimization"}:
        raise OrchestrationError("P7 release is limited to Super/Optimization")
    p6 = inspect_p6_integrated_release(
        orchestrator, task_id, p7_billing_report=billing_report)
    blockers = set(p6["blockers"]) | {
        "P7_REAL_PROVIDER_INVOICE_SOURCE_NOT_ATTESTED",
        "P7_LIVE_BRAIN_READ_SESSION_NOT_AUTHORIZED",
        "P7_SUPERVISED_LIVE_CANARY_NOT_PROVISIONED",
        "P7_INDEPENDENT_PRODUCTION_DEPLOYMENT_NOT_ATTESTED",
        "P7_SIMULATION_AND_SUBMISSION_SEPARATE_APPROVAL_REQUIRED",
    }
    milestones = {
        "p7_1_authority": "LOCAL_DEPLOYMENT_CONTRACT_ONLY",
        "p7_2_billing": "MISSING",
        "p7_3_brain_read": "MISSING",
        "p7_4_canary": "MISSING",
        "p7_5_release": "PRODUCTION_BLOCKED",
    }
    if billing_report is not None:
        _check_billing(billing_report, task_id)
        milestones["p7_2_billing"] = billing_report["status"]
        if billing_report["status"] == "P7_RECONCILIATION_REQUIRED":
            blockers.add("P7_BILLING_RECONCILIATION_REQUIRED")
    else:
        blockers.add("P7_BILLING_EVIDENCE_MISSING")
    if brain_report is not None:
        _check_brain(brain_report, task_id)
        milestones["p7_3_brain_read"] = brain_report["status"]
        if brain_report["status"] != "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE":
            blockers.add("P7_BRAIN_SCOPE_OR_STOP_DENIED")
    else:
        blockers.add("P7_BRAIN_SESSION_EVIDENCE_MISSING")
    if canary_report is not None:
        _check_canary(canary_report, task_id)
        milestones["p7_4_canary"] = canary_report["status"]
        if billing_report is not None and (canary_report.get(
                "billing_report_sha256") != fingerprint_json(billing_report)):
            raise OrchestrationError("P7 Canary billing evidence lineage mismatch")
        if brain_report is not None and (canary_report.get(
                "brain_report_sha256") != fingerprint_json(brain_report)):
            raise OrchestrationError("P7 Canary BRAIN evidence lineage mismatch")
        if billing_report is None and canary_report.get("billing_report_sha256") is not None:
            blockers.add("P7_CANARY_BILLING_SOURCE_UNAVAILABLE")
        if brain_report is None and canary_report.get("brain_report_sha256") is not None:
            blockers.add("P7_CANARY_BRAIN_SOURCE_UNAVAILABLE")
        if canary_report["stop_required"]:
            blockers.add("P7_CANARY_OPERATOR_STOP_REQUIRED")
        if canary_report["stop_latched"]:
            blockers.add("P7_GLOBAL_STOP_ALREADY_LATCHED")
        if brain_report is not None and (
                canary_report.get("stop_state") !=
                brain_report.get("authority_stop_state")):
            blockers.add("P7_BRAIN_CANARY_STOP_STATE_INCONSISTENT")
    else:
        blockers.add("P7_CANARY_SUPERVISION_EVIDENCE_MISSING")
    if p6.get("status") != "P6_PRODUCTION_BLOCKED":
        raise OrchestrationError("P7 requires blocked P6 release baseline")
    return {
        "schema_version": 1, "task_id": task_id,
        "workflow": task["workflow"], "status": "P7_PRODUCTION_BLOCKED",
        "audit_mode": "READ_ONLY",
        "milestones": milestones, "blockers": sorted(blockers),
        "p6_release_status": p6["status"],
        "p6_release_sha256": fingerprint_json(p6),
        "billing_report_sha256": (fingerprint_json(billing_report)
                                  if billing_report is not None else None),
        "brain_report_sha256": (fingerprint_json(brain_report)
                                if brain_report is not None else None),
        "canary_report_sha256": (fingerprint_json(canary_report)
                                 if canary_report is not None else None),
        "provider_invoice_independently_verified": False,
        "platform_session_independently_authenticated": False,
        "independent_canary_attested": False,
        "external_prerequisites_satisfied": False,
        "production_authorized": False,
        "brain_read_authorized": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "brain_invoked": False, "provider_invoked": False,
        "permission_to_dispatch": False,
    }
