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
    routing = RoleRoutingConfig.parse(model_config)
    if not ROLE_NAMES.issubset(routing.roles):
        raise OrchestrationError("Lead and Executor roles must be registered")
    _refuse_inline_secrets(model_config)
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
