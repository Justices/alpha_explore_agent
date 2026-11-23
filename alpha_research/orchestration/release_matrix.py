"""Cross-module read-only release aggregation: no execution rights.

This is the canonical integration entrypoint organized by architectural
responsibility, not historical delivery phase.
"""
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_p6_readiness import (
    inspect_p6_integrated_release,
)
from alpha_research.orchestration.release_evidence import (
    _check_billing, _check_brain, _check_canary,
)
from alpha_research.state.fingerprint import fingerprint_json

def inspect_research_release(orchestrator, task_id, *, billing_report=None,
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

