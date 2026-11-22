"""P4.13 issuer handoff packets for staged single-Simulation preparation.

Both builders are read-only and produce NON-EXECUTABLE descriptions of exact
claims an independent issuer would need to review and sign. They never sign,
trust a local key as operator identity, grant authorization, reserve a call,
invoke a model or open BRAIN. Raw Alpha expressions and credentials are NEVER
included in packets; only immutable digests and the pinned scope are exposed.
"""
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.execution_identity import inspect_execution_identity
from alpha_research.orchestration.execution_proposal import inspect_execution_proposal
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.operator_consent import (
    _CLAIMS as CONSENT_CLAIMS, expected_external_consent_fields,
)
from alpha_research.orchestration.simulation_scope import (
    CLAIMS as SCOPE_CLAIMS, SCHEMA_VERSION as SCOPE_VERSION,
    _validate_plan,
)
from alpha_research.state.fingerprint import fingerprint_json


def _fresh_preparation(orchestrator, task_id):
    root = Path(orchestrator.state_root)
    marker = marker_path(root)
    stop = root / "state" / "stop.flag"
    if marker.exists() or marker.is_symlink():
        raise OrchestrationError("issuer handoff refused in offline mock workspace")
    if stop.exists() or stop.is_symlink():
        raise OrchestrationError("issuer handoff refused while stopped")
    task = orchestrator.load(task_id)
    if (task["status"] != "CREATED"
            or task["workflow"] not in {"super", "optimization"}
            or task["budget"] != {"limit": 1, "used": 0, "reserved": 0}
            or task.get("research_identity") is not None
            or task.get("recovery_target") is not None):
        raise OrchestrationError("issuer handoff requires a fresh unexecuted Simulation task")
    return task


def _packet(kind, task_id, fixed, issuer_fields, allowed_fields):
    if (set(fixed).intersection(issuer_fields)
            or set(fixed).union(issuer_fields) != allowed_fields):
        raise OrchestrationError("issuer handoff schema and verifier claims diverged")
    return {
        "schema_version": 1,
        "task_id": task_id,
        "kind": kind,
        "status": "AWAITING_INDEPENDENT_SIGNATURE_NOT_AUTHORIZED",
        "fixed_claims": fixed,
        "fixed_claims_sha256": fingerprint_json(fixed),
        "issuer_supplied_fields": sorted(issuer_fields),
        "issuer_signing_required": True,
        "signed_receipt_present": False,
        "signing_key_present": False,
        "issuer_trust_independently_verified": False,
        "permission_to_dispatch": False,
        "brain_simulation_authorized": False,
        "model_call_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "brain_invoked": False,
        "provider_invoked": False,
    }


def build_scope_issuer_request(orchestrator, task_id, model_config,
                               governance_policy, manifest):
    """Create a signature request for the legacy scope-only HMAC verifier.

    This is a technical staging receipt, NOT operator consent. Its HMAC
    verifier key is not a trusted independent production authority.
    """
    task = _fresh_preparation(orchestrator, task_id)
    scope = _validate_plan(task, manifest)
    proposal = inspect_execution_proposal(orchestrator, task_id)
    identity = inspect_execution_identity(
        orchestrator, task_id, model_config, governance_policy)["identity"]
    fixed = {
        "schema_version": SCOPE_VERSION,
        "operation": "brain_simulation",
        "task_id": task_id,
        "workflow": task["workflow"],
        "request_sha256": task["request_sha256"],
        "proposal_sha256": proposal["proposal_sha256"],
        "model_profile_sha256": identity["model_profile_sha256"],
        "model_governance_policy_sha256": identity["model_governance_policy_sha256"],
        "research_id": manifest["research_id"],
        "run_id": manifest["run_id"],
        "batch_id": manifest["batch_id"],
        "simulation_sha256": fingerprint_json({
            "request": manifest["request"], "settings": manifest["settings"],
        }),
        "scope": scope,
        "max_attempts": 1,
    }
    return _packet(
        "SIMULATION_SCOPE_STAGING_RECEIPT_REQUEST", task_id,
        fixed, {"authorization_id", "issuer", "issued_at", "expires_at"},
        SCOPE_CLAIMS)


def build_operator_consent_issuer_request(orchestrator, task_id, model_config,
                                          governance_policy):
    """Prepare EXACT fixed claims for an external Ed25519 operator issuer.

    Requires fully verified local staging with PREPARED Intent. Missing,
    altered or partially committed staged evidence fails closed.
    """
    _fresh_preparation(orchestrator, task_id)
    fixed = expected_external_consent_fields(
        orchestrator, task_id, model_config, governance_policy)
    fixed.update({
        "schema_version": 1,
        "operation": "brain_simulation",
        "max_attempts": 1,
        "max_model_calls": 0,
        "allow_alpha_submission": False,
    })
    return _packet(
        "EXTERNAL_OPERATOR_CONSENT_SIGNATURE_REQUEST", task_id,
        fixed,
        {"grant_id", "issuer_id", "key_id", "operator_approval_id",
         "issued_at", "expires_at"},
        CONSENT_CLAIMS)
