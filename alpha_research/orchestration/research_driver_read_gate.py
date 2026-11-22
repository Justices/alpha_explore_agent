"""P5.2 evidence-bound driver ingress contract, no live BRAIN admission.

The driver must revalidate the Task-specific operator grant, independent
ledger ACK/readback and signed driver-session observation *before* even
classifying a read frame. This is a pure audit boundary, not executable
authority. The actual --research-catalog profile stays disabled.
"""
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_driver_session import (
    driver_read_frame_allowed, inspect_driver_session_evidence,
)
from alpha_research.orchestration.research_grant_ledger import (
    inspect_research_grant_ledger,
)
from alpha_research.state.fingerprint import fingerprint_json


def inspect_evidence_bound_driver_frame(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, global_ack, consumer_trust,
        ledger_readback, ledger_trust, driver_evidence,
        driver_trust, frame, *, now=None):
    """Authenticate the whole read-only evidence chain for an exact frame.

    Every invocation rechecks cryptographic signatures, their Task/Grant/
    scope bindings and the allowed Catalog method. Caller-provided reports
    and precomputed booleans are not accepted as authorization.
    """
    ledger = inspect_research_grant_ledger(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, global_ack, consumer_trust,
        ledger_readback, ledger_trust, now=now)
    session = inspect_driver_session_evidence(
        orchestrator, task_id, receipt, global_ack, driver_evidence,
        driver_trust, now=now)
    if (ledger["task_id"] != task_id
            or session["task_id"] != task_id
            or ledger["grant_id"] != global_ack["claims"]["grant_id"]
            or ledger["ack_sha256"] != fingerprint_json(global_ack)
            or ledger["research_receipt_sha256"] != fingerprint_json(receipt)
            or session["research_receipt_sha256"] != fingerprint_json(receipt)):
        raise OrchestrationError("research driver ledger/session chain mismatch")
    allowed = driver_read_frame_allowed(frame, session)
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": ("DRIVER_READ_FRAME_POLICY_VALIDATED_NO_LIVE"
                   if allowed else "DRIVER_READ_FRAME_DENIED"),
        "audit_mode": "READ_ONLY",
        "frame_policy_valid": allowed,
        "ledger_evidence_id": ledger["evidence_id"],
        "driver_session_evidence_id": session["evidence_id"],
        "scope": session["scope"],
        "source_alpha_id": session["source_alpha_id"],
        "raw_driver_started": False,
        "platform_credentials_loaded": False,
        "brain_invoked": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "permission_to_dispatch": False,
        "automatic_retry_allowed": False,
    }
