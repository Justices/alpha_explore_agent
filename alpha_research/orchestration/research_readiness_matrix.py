"""Aggregate negative-only research release blockers across external boundaries.

The audit intentionally cannot produce an executable grant. All externally
authenticated Provider/BRAIN/Authority/Canary prerequisites remain blocked.
"""
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_p5_release import (
    inspect_research_p5_canary_gate,
)

# These are observations, not permissions. A failed audit must not be
# re-labelled as a completed milestone by the aggregate release gate.
_METERING_VERDICTS = frozenset({
    "P6_REPORTED_USAGE_ESTIMATE_ONLY",
    "P6_RECONCILIATION_REQUIRED_TOKEN_OR_USAGE",
    "P6_RECONCILIATION_REQUIRED_RESERVATION_BREACH",
    "P6_RECONCILIATION_REQUIRED_INVOICE_VARIANCE",
})


def inspect_p6_integrated_release(orchestrator, task_id, *,
                                  authority_monitor=None,
                                  provider_report=None, metering_report=None,
                                  brain_report=None, signed_readback=None,
                                  p7_billing_report=None):
    """P6.5/6.6 dry-run readiness matrix remains strictly non-executable."""
    baseline = inspect_research_p5_canary_gate(
        orchestrator, task_id, authority_monitor=authority_monitor)
    statuses = {
        "p6_1_service_identity": "LOCAL_MTLS_NOT_PRODUCTION_ATTESTED",
        "p6_2_provider": "NOT_VERIFIED",
        "p6_3_billing": "NOT_VERIFIED",
        "p6_4_brain_read": "NOT_VERIFIED",
        "p6_5_canary": "NOT_APPROVED",
        "p6_6_release": "PRODUCTION_BLOCKED",
    }
    if provider_report is not None:
        if (not isinstance(provider_report, dict)
                or provider_report.get("task_id") != task_id
                or provider_report.get("status")
                   != "P6_PROVIDER_REQUEST_CONTRACT_AUDITED_NO_LIVE"
                or provider_report.get("permission_to_dispatch") is not False
                or provider_report.get("real_provider_invoked") is not False):
            raise OrchestrationError("P6 provider audit report invalid")
        statuses["p6_2_provider"] = "OFFLINE_REQUEST_CONTRACT_VERIFIED"
    if metering_report is not None:
        if (not isinstance(metering_report, dict)
                or metering_report.get("task_id") != task_id
                or metering_report.get("permission_to_dispatch") is not False
                or metering_report.get("provider_invoice_independently_verified")
                   is not False
                or metering_report.get("real_provider_invoked") is not False
                or not isinstance(metering_report.get("status"), str)
                or metering_report["status"] not in _METERING_VERDICTS):
            raise OrchestrationError("P6 metering audit report invalid")
        if metering_report["status"] == "P6_REPORTED_USAGE_ESTIMATE_ONLY":
            statuses["p6_3_billing"] = "OFFLINE_ESTIMATE_ONLY_INVOICE_UNVERIFIED"
        else:
            statuses["p6_3_billing"] = "RECONCILIATION_REQUIRED"
    if brain_report is not None:
        if (not isinstance(brain_report, dict)
                or brain_report.get("task_id") != task_id
                or brain_report.get("status") not in (
                    "P6_READ_ONLY_FRAME_SHAPE_VERIFIED_NO_LIVE",
                    "P6_BRAIN_FRAME_DENIED")
                or brain_report.get("permission_to_dispatch") is not False
                or brain_report.get("brain_read_invoked") is not False
                or brain_report.get("brain_session_authenticated") is not False
                or brain_report.get("read_only_frame_shape_valid") is not (
                    brain_report["status"] == "P6_READ_ONLY_FRAME_SHAPE_VERIFIED_NO_LIVE")):
            raise OrchestrationError("P6 BRAIN read report invalid")
        statuses["p6_4_brain_read"] = (
            "OFFLINE_FRAME_POLICY_VERIFIED"
            if brain_report["read_only_frame_shape_valid"]
            else "READ_ONLY_FRAME_DENIED")
    if signed_readback is not None:
        if (not isinstance(signed_readback, dict)
                or signed_readback.get("task_id") != task_id
                or signed_readback.get("permission_to_dispatch") is not False
                or signed_readback.get("status")
                   != "P6_SIGNED_READBACK_AUDITED_BILLING_UNVERIFIED"
                or signed_readback.get("real_provider_invoice_verified") is not False
                or signed_readback.get("server_to_provider_readback_executed") is not False
                or signed_readback.get("remote_operation_id_matches_journal") is not True
                or signed_readback.get("safe_to_retry") is not False):
            raise OrchestrationError("P6 signed readback report invalid")
        # A signed operation report cannot clear a previously observed
        # token, reservation or invoice discrepancy.
        if statuses["p6_3_billing"] != "RECONCILIATION_REQUIRED":
            statuses["p6_3_billing"] = "SIGNED_OBSERVATION_ONLY_INVOICE_UNVERIFIED"
    if p7_billing_report is not None:
        if (not isinstance(p7_billing_report, dict)
                or p7_billing_report.get("task_id") != task_id
                or p7_billing_report.get("status") not in (
                    "P7_RECONCILIATION_REQUIRED",
                    "P7_INVOICE_MATCHED_UNTRUSTED_INPUT")
                or p7_billing_report.get("provider_invoice_independently_verified") is not False
                or p7_billing_report.get("settlement_authorized") is not False
                or p7_billing_report.get("budget_refund_authorized") is not False
                or p7_billing_report.get("real_provider_invoked") is not False
                or p7_billing_report.get("permission_to_dispatch") is not False):
            raise OrchestrationError("P7 billing reconciliation report invalid")
        if p7_billing_report["status"] == "P7_RECONCILIATION_REQUIRED":
            statuses["p6_3_billing"] = "RECONCILIATION_REQUIRED"
        elif statuses["p6_3_billing"] != "RECONCILIATION_REQUIRED":
            statuses["p6_3_billing"] = "OFFLINE_INVOICE_MATCH_UNTRUSTED"
    blockers = set(baseline["blockers"]) | {
        "P6_REAL_MTLS_DEPLOYMENT_NOT_PROVEN",
        "P6_LIVE_PROVIDER_DISPATCH_DISABLED",
        "P6_INDEPENDENT_PROVIDER_INVOICE_NOT_VERIFIED",
        "P6_LIVE_BRAIN_READ_NOT_AUTHORIZED",
        "P6_LIVE_CANARY_AND_OPERATOR_APPROVAL_MISSING",
        "P6_SEPARATE_SIMULATION_SUBMISSION_APPROVAL_REQUIRED",
    }
    if statuses["p6_3_billing"] == "RECONCILIATION_REQUIRED":
        blockers.add("P6_METERING_RECONCILIATION_REQUIRED")
        if p7_billing_report is not None and p7_billing_report["status"] == "P7_RECONCILIATION_REQUIRED":
            blockers.add("P7_PROVIDER_INVOICE_VARIANCE_OR_RESERVATION_BREACH")
    if statuses["p6_4_brain_read"] == "READ_ONLY_FRAME_DENIED":
        blockers.add("P6_BRAIN_READ_FRAME_DENIED")
    return {
        "schema_version": 1, "task_id": task_id,
        "workflow": baseline["workflow"],
        "status": "P6_PRODUCTION_BLOCKED",
        "audit_mode": "READ_ONLY",
        "milestones": statuses,
        "blockers": sorted(blockers),
        "canary_rehearsal_only": True,
        "external_service_deployed": False,
        "provider_invoice_independently_verified": False,
        "production_authorized": False,
        "brain_read_authorized": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "permission_to_dispatch": False,
        "brain_invoked": False, "provider_invoked": False,
    }



