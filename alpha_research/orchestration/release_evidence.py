"""Strict evidence verifiers for the always-blocked research release.

Caller-supplied local observations are not external attestations. Pure checks
never access files, SDKs, BRAIN, Provider credentials or live transports.
"""
import re

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.release_contracts import (
    BILLING_STATUSES, BRAIN_STATUSES, CANARY_STATUSES, PROVIDER_HOSTNAMES,
    brain_read_status, canary_status,
)

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
    "monitor_age_seconds", "monitor_age_microseconds",
    "heartbeat_limit_seconds", "stop_state", "stop_latched", "stop_required",
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
            or type(requested.get("delay")) is not int
            or not isinstance(requested.get("region"), str)
            or not isinstance(requested.get("universe"), str)
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
    age_us = report.get("monitor_age_microseconds")
    limit = report.get("heartbeat_limit_seconds")
    if type(limit) is not int or not 1 <= limit <= 3600:
        raise OrchestrationError("P7 Canary heartbeat policy invalid")
    if report["stop_state"] == "UNKNOWN":
        if age is not None or age_us is not None:
            raise OrchestrationError("P7 Canary missing monitor has heartbeat age")
        if "P7_MONITOR_STALE" in reasons:
            raise OrchestrationError("P7 Canary missing monitor cannot be stale")
    else:
        if (type(age) is not int or type(age_us) is not int
                or age_us < -5_000_000
                or age != ((abs(age_us) // 1_000_000)
                           * (1 if age_us >= 0 else -1))
                or ("P7_MONITOR_STALE" in reasons)
                   != (age_us > limit * 1_000_000)):
            raise OrchestrationError("P7 Canary heartbeat audit drift")
    expected_status = canary_status(report["stop_latched"], bool(reasons))
    if report["status"] != expected_status:
        raise OrchestrationError("P7 Canary status does not match incident state")


