"""P4.8 integrated, READ-ONLY production release gate.

A negative-only release report is useful for recognizing exactly what is
missing; it is not consent and it never produces a live dispatch token.
An external issuer, independent platform execution consumer, credentials,
cost control and operator-approved canary cannot be inferred from source
code or local filesystem evidence. These remain hard release blockers.
"""
from alpha_research.orchestration.admission_audit import audit_execution_admission
from alpha_research.orchestration.simulation_dispatch_preflight import (
    inspect_simulation_dispatch_gate,
)
from alpha_research.orchestration.simulation_recovery_audit import (
    inspect_simulation_recovery_evidence,
)
from alpha_research.orchestration.research_p4_release import (
    inspect_research_p4_release_gate,
)
from alpha_research.orchestration.research_p5_release import (
    inspect_research_p5_canary_gate,
)
from alpha_research.orchestration.research_readiness_matrix import (
    inspect_p6_integrated_release,
)
from alpha_research.orchestration.release_matrix import (
    inspect_research_release,
)
from alpha_research.orchestration.platform_read_scope_binding import (
    inspect_platform_read_scope_binding,
)

_BLOCKERS = (
    "EXTERNAL_OPERATOR_CONSENT_ISSUER_NOT_PROVISIONED",
    "TRUSTED_ISSUER_KEY_CUSTODY_NOT_ATTESTED",
    "PRICING_AND_COST_CEILING_NOT_PROVEN",
    "LIVE_SINGLE_USE_GRANT_CONSUMER_NOT_IMPLEMENTED",
    "DRIVER_BOUND_REMOTE_OPERATION_GRANT_NOT_IMPLEMENTED",
    "BRAIN_PLATFORM_SESSION_NOT_VERIFIED",
    "PRODUCTION_GATEWAY_ADAPTER_DISABLED",
    "PLATFORM_READBACK_RECOVERY_NOT_VERIFIED",
    "PRODUCTION_CANARY_APPROVAL_MISSING",
    "ALPHA_SUBMISSION_INDEPENDENTLY_DISABLED",
)


def inspect_production_release_gate(orchestrator, task_id, model_config,
                                    governance_policy, *, config_source,
                                    platform_read_evidence_id=None,
                                    p7_billing_report=None,
                                    p7_brain_report=None,
                                    p7_canary_report=None):
    """Assemble verified negative evidence; no write or network side effects."""
    task = orchestrator.load(task_id)
    baseline = audit_execution_admission(
        orchestrator, task_id, model_config,
        governance_policy, config_source=config_source)
    reports = {"general_admission": baseline}
    blockers = list(baseline["blockers"]) + list(_BLOCKERS)
    if platform_read_evidence_id is not None:
        local_read = inspect_platform_read_scope_binding(
            orchestrator, task_id, platform_read_evidence_id)
        reports["local_platform_read_observation"] = local_read
        # Local checksums and caller-provided record IDs are NEVER trusted
        # remote attestation or an executable operation grant.
        blockers.append("PLATFORM_READ_LOCAL_ONLY_NOT_INDEPENDENTLY_ATTESTED")
        if local_read["status"] != "LOCAL_SCOPE_MATCH_UNATTESTED":
            blockers.append("LOCAL_PLATFORM_SCOPE_EVIDENCE_" + local_read["status"])
    if task["workflow"] in {"super", "optimization"}:
        dispatch = inspect_simulation_dispatch_gate(
            orchestrator, task_id, governance_policy)
        recovery = inspect_simulation_recovery_evidence(
            orchestrator, task_id, governance_policy)
        reports["research_p4_canary"] = inspect_research_p4_release_gate(
            orchestrator, task_id)
        reports["research_p5_canary"] = inspect_research_p5_canary_gate(
            orchestrator, task_id)
        reports["research_p6_readiness"] = inspect_p6_integrated_release(
            orchestrator, task_id)
        reports["research_p7_acceptance"] = inspect_research_release(
            orchestrator, task_id,
            billing_report=p7_billing_report,
            brain_report=p7_brain_report,
            canary_report=p7_canary_report)
        blockers.extend(reports["research_p6_readiness"]["blockers"])
        blockers.extend(reports["research_p7_acceptance"]["blockers"])
        reports["simulation_dispatch"] = dispatch
        reports["simulation_recovery"] = recovery
        blockers.extend(dispatch["blockers"])
        blockers.extend(recovery["blockers"])
    else:
        blockers.append("REGULAR_ALPHA_WRITE_PATH_NOT_PRODUCTION_ADMITTED")
    needs_reconciliation = any(
        "RECONCILIATION_REQUIRED" in blocker or blocker.startswith("INTENT_UNREADABLE")
        for blocker in blockers
    )
    return {
        "schema_version": 1,
        "task_id": task_id,
        "workflow": task["workflow"],
        "status": ("RECONCILIATION_BLOCKED"
                   if needs_reconciliation else "PRODUCTION_BLOCKED"),
        "audit_mode": "READ_ONLY",
        "components": reports,
        "blockers": sorted(set(blockers)),
        "external_prerequisites_satisfied": False,
        "production_authorized": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "permission_to_dispatch": False,
        "brain_invoked": False,
        "provider_invoked": False,
    }
