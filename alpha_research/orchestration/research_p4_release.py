"""P4.29: integrated, always non-executable research Canary release matrix.

Evidence records below are audit reports only. An operator may supply signed
reviews and real service observations, but *only* the external driver/global
budget authority could ever authorize live research. Neither exists here.
"""
import re
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.research_attestation_contract import (
    inspect_signed_research_evidence,
)
from alpha_research.state.fingerprint import fingerprint_json

_CANARY_FIELDS = frozenset({
    "component_evidence_sha256", "canary_profile",
    "operator_stop_state", "max_model_calls",
    "max_cost_usd_micros", "canary_status",
})
_STATUSES = {
    "grant_ledger": "GLOBAL_LEDGER_READBACK_SIGNATURE_VERIFIED_AUDIT_ONLY",
    "driver_session": "DRIVER_SESSION_SIGNATURE_VERIFIED_AUDIT_ONLY",
    "model_pricing": "MODEL_TARIFF_SIGNATURE_VERIFIED_AUDIT_ONLY",
    "remote_reconciliation": "REMOTE_SETTLED_SIGNATURE_VERIFIED_AUDIT_ONLY",
}
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_UNRESOLVED = {
    "grant_ledger": "INDEPENDENT_GLOBAL_LEDGER_NOT_DEPLOYED",
    "driver_session": "RESEARCH_DRIVER_AUTHENTICATED_LIVE_BOUNDARY_NOT_DEPLOYED",
    "model_pricing": "HARD_PROVIDER_SPEND_ENFORCEMENT_NOT_DEPLOYED",
    "remote_reconciliation": "REMOTE_READBACK_AND_BILLING_UNRECONCILED",
}


def inspect_research_p4_release_gate(orchestrator, task_id, *, receipt=None,
                                     grant_ledger=None, driver_session=None,
                                     model_pricing=None, remote_reconciliation=None,
                                     canary_approval=None, canary_trust=None,
                                     now=None):
    """Report all five P4 deliverables; no credentials, network or disk writes."""
    task = orchestrator.load(task_id)
    if task["workflow"] not in {"super", "optimization"}:
        raise OrchestrationError("research P4 Canary is only for Super/Optimization")
    reports = {
        "grant_ledger": grant_ledger, "driver_session": driver_session,
        "model_pricing": model_pricing,
        "remote_reconciliation": remote_reconciliation,
    }
    blockers = list(_UNRESOLVED.values())
    presence = {}
    digests = {}
    for name, expected in _STATUSES.items():
        evidence = reports[name]
        if evidence is None:
            presence[name] = "NOT_SUPPLIED"
            blockers.append("P4_" + name.upper() + "_EVIDENCE_MISSING")
            continue
        if (not isinstance(evidence, dict)
                or evidence.get("task_id") != task_id
                or evidence.get("status") != expected
                or evidence.get("permission_to_dispatch") is not False
                or evidence.get("automatic_retry_allowed") is not False
                or evidence.get("brain_simulation_authorized") is not False
                or evidence.get("alpha_submission_authorized") is not False
                or not isinstance(evidence.get("evidence_sha256"), str)
                or not _HEX64.fullmatch(evidence["evidence_sha256"])):
            raise OrchestrationError("research P4 component evidence invalid: " + name)
        presence[name] = "SIGNED_AUDIT_ONLY"
        digests[name] = evidence["evidence_sha256"]
    root = Path(orchestrator.state_root)
    stopped = ((root / "state" / "stop.flag").exists()
               or (root / "state" / "stop.flag").is_symlink()
               or marker_path(root).exists() or marker_path(root).is_symlink())
    if stopped:
        blockers.append("RESEARCH_GLOBAL_STOP_OR_MOCK_WORKSPACE")
    if (canary_approval is None) != (canary_trust is None):
        raise OrchestrationError("research P4 Canary approval and trust must be paired")
    canary = None
    if canary_approval is None:
        blockers.append("INDEPENDENT_OPERATOR_CANARY_APPROVAL_MISSING")
    else:
        if receipt is None or set(digests) != set(_STATUSES):
            raise OrchestrationError(
                "research Canary review requires receipt and all four evidence audits")
        expected = {
            "task_id": task_id,
            "research_receipt_sha256": fingerprint_json(receipt),
            "component_evidence_sha256": digests,
            "canary_profile": "RESEARCH_READ_ONLY",
            "operator_stop_state": "ARMED_NOT_TRIGGERED",
            "max_model_calls": receipt["claims"]["max_model_calls"],
            "max_cost_usd_micros": receipt["claims"]["max_total_cost_usd_micros"],
            "canary_status": "REVIEWED_NOT_ENABLED",
        }
        canary = inspect_signed_research_evidence(
            canary_approval, canary_trust, domain="research-readonly-canary",
            operation="review_research_readonly_canary",
            fields=_CANARY_FIELDS, expected=expected, now=now)
        blockers.append("SIGNED_CANARY_REVIEW_NOT_RUNTIME_AUTHORITY")
    blockers.extend((
        "GLOBAL_GRANT_CONSUMER_RUNTIME_UNAVAILABLE",
        "BRAIN_DRIVER_RESEARCH_STARTUP_DISABLED",
        "PAID_MODEL_SDK_DISPATCH_DISABLED",
        "HARD_SPEND_AND_REMOTE_BILLING_NOT_VERIFIED",
        "SIMULATION_POST_SEPARATE_APPROVAL_REQUIRED",
        "ALPHA_SUBMISSION_SEPARATE_APPROVAL_REQUIRED",
    ))
    return {
        "schema_version": 1, "task_id": task_id,
        "workflow": task["workflow"],
        "status": "RESEARCH_P4_CANARY_PRODUCTION_BLOCKED",
        "audit_mode": "READ_ONLY",
        "components": presence,
        "canary_review": canary,
        "blockers": sorted(set(blockers)),
        "global_stop_or_mock_workspace": stopped,
        "all_four_evidence_audits_supplied": set(digests) == set(_STATUSES),
        "external_services_independently_provisioned": False,
        "canary_live_enabled": False,
        "permission_to_dispatch": False,
        "production_authorized": False,
        "brain_read_authorized": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "brain_invoked": False,
        "provider_invoked": False,
    }
