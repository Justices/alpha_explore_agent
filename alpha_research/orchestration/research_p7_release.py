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
from alpha_research.orchestration.research_p7_contracts import (
    BILLING_STATUSES, BRAIN_STATUSES, CANARY_STATUSES, PROVIDER_HOSTNAMES,
    brain_read_status, canary_status,
)
from alpha_research.state.fingerprint import fingerprint_json

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE_FIELDS = {"region", "delay", "universe", "instrument_type"}
_BILLING_FIELDS = {
    "schema_version", "task_id", "status", "provider_hostname", "invoice_id",
    "currency", "operation_count", "reserved_total_usd_micros",
    "estimated_total_usd_micros", "billed_total_usd_micros",
    "variance_total_usd_micros", "findings", "invoice_rows_sha256",
    "reservation_rows_sha256", "provider_invoice_independently_verified",
    "independent_invoice_source_authenticated", "settlement_authorized",
    "budget_refund_authorized", "automatic_retry_allowed",
    "real_provider_invoked", "permission_to_dispatch",
}
_BRAIN_FIELDS = {
    "schema_version", "task_id", "status", "session_evidence_sha256",
    "grant_ack_sha256", "research_receipt_sha256", "scope",
    "source_alpha_id", "frame_digests", "frame_batch_sha256", "frame_count",
    "read_only_frames_valid", "authority_stop_state",
    "local_offline_admission_passed",
    "platform_session_independently_authenticated",
    "independent_brain_read_permission_verified", "driver_started",
    "brain_read_invoked", "brain_simulation_invoked", "alpha_submission_invoked",
    "automatic_retry_allowed", "permission_to_dispatch",
}
_CANARY_FIELDS = {
    "schema_version", "task_id", "status", "monitor_sha256",
    "monitor_age_seconds", "stop_state", "stop_latched", "stop_required",
    "reasons", "billing_report_sha256", "brain_report_sha256",
    "operator_admin_stop_required", "admin_stop_invoked", "watchdog_executed",
    "independent_canary_attested", "live_canary_authorized",
    "model_call_authorized", "brain_read_authorized",
    "brain_simulation_authorized", "alpha_submission_authorized",
    "automatic_retry_allowed", "permission_to_dispatch",
}


def _check_negative(report, task_id, mandatory_flags, expected_fields):
    if (not isinstance(report, dict)
            or set(report) != expected_fields
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
        "automatic_retry_allowed"), _BILLING_FIELDS)
    rows = report.get("findings")
    if (not isinstance(report.get("status"), str)
            or report["status"] not in BILLING_STATUSES or not isinstance(rows, list)
            or not rows or len(rows) != report.get("operation_count")
            or type(report.get("operation_count")) is not int
            or len(rows) > 10000):
        raise OrchestrationError("P7 billing report shape invalid")
    if (not isinstance(report.get("provider_hostname"), str)
            or report["provider_hostname"] not in PROVIDER_HOSTNAMES
            or not isinstance(report.get("invoice_id"), str)
            or not report["invoice_id"].strip()
            or report.get("currency") != "USD"
            or report.get("independent_invoice_source_authenticated") is not False
            or any(not isinstance(report.get(name), str)
                   or _HEX64.fullmatch(report[name]) is None
                   for name in ("invoice_rows_sha256",
                                "reservation_rows_sha256"))):
        raise OrchestrationError("P7 billing provenance metadata invalid")
    totals = [0, 0, 0]
    mismatched = False
    attempts, operations = set(), set()
    for row in rows:
        if (not isinstance(row, dict)
                or set(row) != {"attempt_id", "provider_operation_id",
                                    "reserved_usd_micros", "estimated_usd_micros",
                                    "billed_usd_micros", "variance_usd_micros",
                                    "reservation_breached"}):
            raise OrchestrationError("P7 billing finding invalid")
        attempt, operation = row.get("attempt_id"), row.get("provider_operation_id")
        if (not isinstance(attempt, str) or not attempt
                or not isinstance(operation, str) or not operation
                or len(attempt) > 256 or len(operation) > 256
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


def _check_brain(report, task_id, task):
    _check_negative(report, task_id, (
        "platform_session_independently_authenticated",
        "independent_brain_read_permission_verified", "driver_started",
        "brain_read_invoked", "brain_simulation_invoked", "alpha_submission_invoked",
        "automatic_retry_allowed"), _BRAIN_FIELDS)
    frames = report.get("frame_digests")
    if (not isinstance(report.get("status"), str)
            or report["status"] not in BRAIN_STATUSES
            or not isinstance(frames, list)
            or not 1 <= len(frames) <= 64
            or type(report.get("frame_count")) is not int
            or report.get("frame_count") != len(frames)
            or any(not isinstance(report.get(name), str)
                   or _HEX64.fullmatch(report[name]) is None
                   for name in ("session_evidence_sha256", "grant_ack_sha256",
                                "research_receipt_sha256", "frame_batch_sha256"))
            or any(not isinstance(h, str) or _HEX64.fullmatch(h) is None
                   for h in frames)
            or len(set(frames)) != len(frames)
            ):
        raise OrchestrationError("P7 BRAIN audit report drift")
    valid = report.get("read_only_frames_valid")
    admitted = report.get("local_offline_admission_passed")
    stop = report.get("authority_stop_state")
    if (type(valid) is not bool or type(admitted) is not bool
            or not isinstance(stop, str)
            or stop not in {"ARMED", "STOPPED", "NOT_SUPPLIED"}):
        raise OrchestrationError("P7 BRAIN evidence state invalid")
    expected = brain_read_status(valid, stop)
    if report["status"] != expected or admitted is not (valid and stop == "ARMED"):
        raise OrchestrationError("P7 BRAIN admission verdict inconsistent")
    scope = report.get("scope")
    requested = task.get("request")
    if (not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS
            or not isinstance(requested, dict)
            or scope.get("instrument_type") != "EQUITY"
            or type(scope.get("delay")) is not int
            or any(scope.get(key) != requested.get(key)
                   for key in ("region", "delay", "universe"))
            or (task["workflow"] == "super" and
                report.get("source_alpha_id") is not None)
            or (task["workflow"] == "optimization" and
                report.get("source_alpha_id") != requested.get("alpha_id"))):
        raise OrchestrationError("P7 BRAIN Task scope/source mismatch")


def _check_canary(report, task_id):
    _check_negative(report, task_id, (
        "admin_stop_invoked", "watchdog_executed",
        "independent_canary_attested", "live_canary_authorized",
        "model_call_authorized", "brain_read_authorized",
        "brain_simulation_authorized", "alpha_submission_authorized",
        "automatic_retry_allowed"), _CANARY_FIELDS)
    reasons = report.get("reasons")
    if (not isinstance(report.get("status"), str)
            or report["status"] not in CANARY_STATUSES
            or not isinstance(reasons, list)
            or len(reasons) > 256
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
    for field in ("monitor_sha256", "billing_report_sha256",
                  "brain_report_sha256"):
        digest = report.get(field)
        if digest is not None and (
                not isinstance(digest, str) or _HEX64.fullmatch(digest) is None):
            raise OrchestrationError("P7 Canary evidence digest invalid")
    if (not isinstance(report.get("stop_state"), str)
            or report["stop_state"] not in {"ARMED", "STOPPED", "UNKNOWN"}):
        raise OrchestrationError("P7 Canary Stop state invalid")
    if (("P7_GLOBAL_STOP_LATCHED" in reasons) !=
            (report["stop_state"] == "STOPPED")):
        raise OrchestrationError("P7 Canary Stop incident disclosure drift")
    if (("P7_MONITOR_MISSING" in reasons) !=
            (report["stop_state"] == "UNKNOWN")):
        raise OrchestrationError("P7 Canary monitor incident disclosure drift")
    if report["stop_latched"] is not (report["stop_state"] == "STOPPED"):
        raise OrchestrationError("P7 Canary Stop latch state mismatch")
    if report["stop_state"] == "UNKNOWN":
        if "P7_MONITOR_MISSING" not in reasons or report.get("monitor_sha256") is not None:
            raise OrchestrationError("P7 Canary missing telemetry not disclosed")
    elif report.get("monitor_sha256") is None:
        raise OrchestrationError("P7 Canary monitor fingerprint missing")
    age = report.get("monitor_age_seconds")
    if report["stop_state"] == "UNKNOWN":
        if age is not None:
            raise OrchestrationError("P7 Canary missing monitor has heartbeat age")
    elif type(age) is not int or age < -5:
        raise OrchestrationError("P7 Canary heartbeat age invalid")
    expected_status = canary_status(report["stop_latched"], bool(reasons))
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
        _check_brain(brain_report, task_id, task)
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
        if (billing_report is not None
                and billing_report["status"] == "P7_RECONCILIATION_REQUIRED"
                and "P7_BILLING_RECONCILIATION_REQUIRED" not in canary_report["reasons"]):
            raise OrchestrationError("P7 Canary suppressed billing discrepancy")
        if (brain_report is not None
                and brain_report["status"] != "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE"
                and "P7_BRAIN_SESSION_NOT_ADMITTED" not in canary_report["reasons"]):
            raise OrchestrationError("P7 Canary suppressed BRAIN admission failure")
        if (brain_report is not None and canary_report["stop_state"] == "ARMED"
                and brain_report["status"] == "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE"
                and brain_report["authority_stop_state"] != "ARMED"
                and "P7_BRAIN_MONITOR_STOP_DRIFT" not in canary_report["reasons"]):
            raise OrchestrationError("P7 Canary suppressed BRAIN Stop drift")
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
