"""Offline-only production execution identity pinning.

Pin the exact selected model routing and governance policy without copying
provider credentials or implying that Task ID equals a domain Research ID.
This prepares, but never authorizes, the CLI-owned gateway/lock/ledger handoff.
"""
import copy
import json
from pathlib import Path

from alpha_research.agents.configuration import RoleRoutingConfig
from alpha_research.agents.governance import validate_policy
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.execution_proposal import inspect_execution_proposal
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

SCHEMA_VERSION = 1
ROLE_NAMES = frozenset(("research_lead", "research_executor"))
_FORBIDDEN_KEYS = frozenset((
    "api_key", "api_key_value", "token", "access_token", "secret",
    "password", "authorization", "credentials", "credential_value",
))


def _refuse_inline_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_KEYS:
                raise OrchestrationError("model profile contains prohibited inline secrets")
            _refuse_inline_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _refuse_inline_secrets(item)


def build_execution_identity(orchestrator, task_id, model_config, governance_policy):
    """Create a read-only identity/ledger handoff snapshot; no model calls."""
    proposal_record = inspect_execution_proposal(orchestrator, task_id)
    task = orchestrator.load(task_id)
    _refuse_inline_secrets(model_config)
    routing = RoleRoutingConfig.parse(model_config)
    if not ROLE_NAMES.issubset(routing.roles):
        raise OrchestrationError("Lead and Executor roles must be registered")
    policy = validate_policy(governance_policy)
    bindings = {}
    for role in sorted(ROLE_NAMES):
        name = routing.roles[role]["backend"]
        if not name.startswith("registry:"):
            raise OrchestrationError("execution identity requires registered role backends")
        backend = routing.backends[name]
        if backend["provider"] == "mock":
            raise OrchestrationError("production identity cannot select mock backend")
        bindings[role] = {
            "backend_name": name,
            "backend_sha256": fingerprint_json(backend),
            "transport": backend["kind"],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PREPARED_NOT_AUTHORIZED",
        "task_id": task_id,
        "workflow": task["workflow"],
        "request_sha256": task["request_sha256"],
        "proposal_sha256": proposal_record["proposal_sha256"],
        "model_profile_sha256": fingerprint_json(model_config),
        "role_bindings": bindings,
        "model_governance_policy_sha256": fingerprint_json(policy),
        "model_budget": {
            "ledger": "ModelCallLedger",
            "research_id": None,
            "status": "UNBOUND_DOMAIN_RESEARCH_ID",
        },
        "orchestrator_budget": {
            "limit": task["budget"]["limit"], "used": 0, "reserved": 0,
        },
        "gateway": {
            "owner": "existing_CLI",
            "session_identity": None,
            "status": "NOT_STARTED",
        },
        "execution_lock": "state/agent.lock",
        "allow_model_call": False,
        "allow_brain_simulation": False,
        "allow_alpha_submission": False,
    }


def persist_execution_identity(orchestrator, task_id, model_config, governance_policy):
    """Pin an immutable identity under a per-task file lock.

    This lock guards identity preparation only. It does not replace the CLI's
    process-wide execution lock or ModelCallLedger's own research lock.
    """
    root = (orchestrator.state_root / "state" / "research_v1_1"
            / "execution_identities")
    path = root / (orchestrator._path(task_id).stem + ".json")
    try:
        with SingleWriterLock(path.with_suffix(".lock")):
            snapshot = build_execution_identity(
                orchestrator, task_id, model_config, governance_policy)
            if path.exists():
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise OrchestrationError("execution identity unreadable") from exc
                if (not isinstance(record, dict)
                        or record.get("identity") != snapshot
                        or record.get("identity_sha256") != fingerprint_json(snapshot)):
                    raise OrchestrationError("execution identity changed; explicit review required")
                return copy.deepcopy(record)
            record = {
                "identity": snapshot,
                "identity_sha256": fingerprint_json(snapshot),
            }
            atomic_write_json(path, record)
            return copy.deepcopy(record)
    except WriterLockError as exc:
        raise OrchestrationError("another writer is preparing execution identity") from exc


def inspect_execution_identity(orchestrator, task_id, model_config, governance_policy):
    """Check saved identity against current config without starting a Gateway."""
    path = (orchestrator.state_root / "state" / "research_v1_1"
            / "execution_identities" / (orchestrator._path(task_id).stem + ".json"))
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError("execution identity is missing or unreadable") from exc
    expected = build_execution_identity(
        orchestrator, task_id, model_config, governance_policy)
    if (not isinstance(record, dict)
            or record.get("identity") != expected
            or record.get("identity_sha256") != fingerprint_json(expected)):
        raise OrchestrationError("execution identity or policy drift; explicit review required")
    return copy.deepcopy(record)


def verify_identity_bound_review(orchestrator, task_id, model_config,
                                 governance_policy, receipt, *,
                                 environ=None, now=None, consume=False):
    """Verify an inspect-only receipt against the saved, drift-checked model.

    The hash is derived from the selected registry profile, never accepted
    from a caller-provided claim. Consuming still records REVIEW ONLY;
    it never makes ProductionWorkflowAdapter executable.
    """
    from alpha_research.orchestration.approval_review import (
        verify_review_receipt, reserve_review_receipt,
    )

    identity = inspect_execution_identity(
        orchestrator, task_id, model_config, governance_policy)["identity"]
    if not isinstance(receipt, dict) or not isinstance(receipt.get("claims"), dict):
        raise OrchestrationError("review receipt is not an object")
    if receipt["claims"].get("task_id") != task_id:
        raise OrchestrationError("review receipt task identity mismatch")
    callback = reserve_review_receipt if consume else verify_review_receipt
    return callback(
        orchestrator, receipt,
        model_profile_sha256=identity["model_profile_sha256"],
        environ=environ, now=now)


def inspect_domain_research_handoff(orchestrator, task_id, model_config,
                                    governance_policy):
    """Read existing domain Research ID and model ledger; never reserve calls.

    Unlike identity preparation, this accepts started/blocked tasks. It checks
    the saved immutable proposal and profile without creating a new identity.
    """
    import re
    from alpha_research.agents.governance import ModelCallLedger

    task = orchestrator.load(task_id)
    _refuse_inline_secrets(model_config)
    RoleRoutingConfig.parse(model_config)
    policy = validate_policy(governance_policy)
    path = (orchestrator.state_root / "state" / "research_v1_1"
            / "execution_identities" / (orchestrator._path(task_id).stem + ".json"))
    proposal_path = (orchestrator.state_root / "state" / "research_v1_1"
                     / "execution_proposals" / (task_id + ".json"))
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError("saved production handoff identity unavailable") from exc
    identity = record.get("identity") if isinstance(record, dict) else None
    proposed = proposal.get("proposal") if isinstance(proposal, dict) else None
    if (not isinstance(identity, dict) or not isinstance(proposed, dict)
            or record.get("identity_sha256") != fingerprint_json(identity)
            or proposal.get("proposal_sha256") != fingerprint_json(proposed)
            or identity.get("task_id") != task_id
            or identity.get("workflow") != task["workflow"]
            or identity.get("request_sha256") != task["request_sha256"]
            or proposed.get("request_sha256") != task["request_sha256"]
            or proposed.get("task_id") != task_id
            or identity.get("proposal_sha256") != proposal["proposal_sha256"]
            or identity.get("model_profile_sha256") != fingerprint_json(model_config)
            or identity.get("model_governance_policy_sha256") != fingerprint_json(policy)
            or identity.get("allow_model_call") is not False
            or identity.get("allow_brain_simulation") is not False):
        raise OrchestrationError(
            "saved production handoff identity or policy has drifted")
    binding = task.get("research_identity")
    if binding is None:
        return {"task_id": task_id, "status": "AWAITING_DOMAIN_RESEARCH_ID",
                "model_profile_sha256": identity["model_profile_sha256"],
                "inspection_provider_invoked": False, "inspection_brain_invoked": False}
    if not isinstance(binding, dict) or binding.get("workflow") != task["workflow"]:
        raise OrchestrationError("domain Research ID binding is invalid")
    research_id = binding.get("research_id")
    prefix = {
        "regular": "regular:campaign:", "super": "super:",
        "optimization": "optimization:",
    }[task["workflow"]]
    if (not isinstance(research_id, str)
            or re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}", research_id) is None):
        raise OrchestrationError("domain Research ID binding has changed")
    ledger = ModelCallLedger(orchestrator.state_root / "state", policy)
    status = ledger.status(research_id)
    return {
        "task_id": task_id,
        "workflow": task["workflow"],
        "research_id": research_id,
        "model_profile_sha256": identity["model_profile_sha256"],
        "model_governance_policy_sha256": identity["model_governance_policy_sha256"],
        "model_budget": status,
        "orchestration_budget": copy.deepcopy(task["budget"]),
        "recovery_required": (task["status"] == "BLOCKED"
                              or status["unresolved"] > 0 or status["unknown"] > 0),
        "inspection_provider_invoked": False,
        "inspection_brain_invoked": False,
    }
