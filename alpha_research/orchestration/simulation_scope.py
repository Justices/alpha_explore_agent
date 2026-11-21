"""P4.5 narrowly scoped, verification-only Simulation admission evidence.

Version 2 deliberately differs from P3's general-purpose review receipt.
A valid signature is NOT proof of user consent or permission to POST.
Neither this module nor its CLI creates a provider/Brain Gateway or an intent.
The production adapter remains disabled.
"""
import hashlib
import hmac
import os
import re
from datetime import datetime, timedelta, timezone

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.execution_identity import inspect_execution_identity
from alpha_research.orchestration.execution_proposal import inspect_execution_proposal
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json

SCHEMA_VERSION = 2
CONTEXT = b"alpha_research/brain-simulation-scope/v2:"
VERIFY_KEY_ENV = "ALPHA_RESEARCH_SIMULATION_SCOPE_VERIFY_KEY"
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
SCOPE_FIELDS = frozenset(("region", "delay", "universe", "instrument_type"))
CLAIMS = frozenset((
    "schema_version", "authorization_id", "issuer", "operation",
    "task_id", "workflow", "request_sha256", "proposal_sha256",
    "model_profile_sha256", "model_governance_policy_sha256",
    "research_id", "run_id", "batch_id", "simulation_sha256",
    "scope", "max_attempts", "issued_at", "expires_at",
))
EXPECTED_PREFIX = {
    "regular": "regular:campaign:",
    "super": "super:",
    "optimization": "optimization:",
}


def _parse_time(value):
    if not isinstance(value, str):
        raise OrchestrationError("Simulation receipt timestamp invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrchestrationError("Simulation receipt timestamp invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise OrchestrationError("Simulation receipt timestamp must use UTC")
    return parsed.astimezone(timezone.utc)


def _validate_plan(task, plan):
    if not isinstance(plan, dict) or set(plan) != {
            "research_id", "run_id", "batch_id", "request", "settings"}:
        raise OrchestrationError("Simulation plan manifest invalid")
    workflow = task["workflow"]
    if workflow not in EXPECTED_PREFIX or workflow == "regular":
        # Regular uses a different campaign batch/Intent contract.
        raise OrchestrationError("P4.5 Simulation scope supports Super/Optimization only")
    rid = plan["research_id"]
    if (not isinstance(rid, str)
            or re.fullmatch(re.escape(EXPECTED_PREFIX[workflow]) +
                            r"[0-9a-f]{32}", rid) is None):
        raise OrchestrationError("Simulation Research ID scope invalid")
    for key in ("run_id", "batch_id"):
        if (not isinstance(plan[key], str)
                or IDENTIFIER.fullmatch(plan[key]) is None):
            raise OrchestrationError("Simulation run/batch ID invalid")
    if not isinstance(plan["request"], dict) or not isinstance(plan["settings"], dict):
        raise OrchestrationError("Simulation request/settings must be objects")
    scope = {key: plan["settings"].get(key) for key in SCOPE_FIELDS}
    if (not isinstance(scope["region"], str)
            or not isinstance(scope["universe"], str)
            or not IDENTIFIER.fullmatch(scope["region"])
            or not IDENTIFIER.fullmatch(scope["universe"])
            or scope["instrument_type"] != "EQUITY"
            or type(scope["delay"]) is not int
            or scope["delay"] not in (0, 1)):
        raise OrchestrationError("Simulation scope invalid")
    task_request = task["request"]
    for key in ("region", "delay", "universe"):
        pinned = task_request.get(key)
        # A broad/unset scope MUST NOT be enlarged by a receipt.
        if pinned is None or pinned != scope[key]:
            raise OrchestrationError("Simulation scope not pinned in prepared task")
    return scope


def inspect_simulation_scope(orchestrator, task_id, model_config,
                             governance_policy, receipt, plan, *, environ=None,
                             now=None):
    """Strict signed scope verification; returns no dispatch permission."""
    task = orchestrator.load(task_id)
    if (task["status"] != "CREATED" or task["budget"] != {
            "limit": 1, "used": 0, "reserved": 0}):
        raise OrchestrationError("Simulation scope requires fresh one-batch task")
    path = marker_path(orchestrator.state_root)
    if path.exists() or path.is_symlink():
        raise OrchestrationError("Simulation scope refused in mock workspace")
    scope = _validate_plan(task, plan)
    if not isinstance(receipt, dict) or set(receipt) != {"claims", "signature"}:
        raise OrchestrationError("Simulation receipt envelope invalid")
    claims, signature = receipt["claims"], receipt["signature"]
    if not isinstance(claims, dict) or set(claims) != CLAIMS:
        raise OrchestrationError("Simulation receipt claims invalid")
    if (type(claims["schema_version"]) is not int
            or claims["schema_version"] != SCHEMA_VERSION
            or claims["operation"] != "brain_simulation"
            or type(claims["max_attempts"]) is not int
            or claims["max_attempts"] != 1):
        raise OrchestrationError("Simulation receipt operation or attempt policy invalid")
    if (not isinstance(claims["authorization_id"], str)
            or HEX32.fullmatch(claims["authorization_id"]) is None
            or not isinstance(claims["issuer"], str)
            or not claims["issuer"].strip()
            or not isinstance(signature, str)
            or HEX64.fullmatch(signature) is None):
        raise OrchestrationError("Simulation receipt signing envelope invalid")
    if (not isinstance(claims["scope"], dict)
            or set(claims["scope"]) != SCOPE_FIELDS):
        raise OrchestrationError("Simulation receipt scope invalid")
    if (not isinstance(claims["simulation_sha256"], str)
            or HEX64.fullmatch(claims["simulation_sha256"]) is None):
        raise OrchestrationError("Simulation digest invalid")
    issued, expires = _parse_time(claims["issued_at"]), _parse_time(claims["expires_at"])
    clock = datetime.now(timezone.utc) if now is None else now
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        raise OrchestrationError("Simulation verification clock invalid")
    clock = clock.astimezone(timezone.utc)
    if (issued > clock or expires <= clock or expires <= issued
            or expires - issued > timedelta(minutes=15)):
        raise OrchestrationError("Simulation receipt expired or outside permitted TTL")
    proposal = inspect_execution_proposal(orchestrator, task_id)
    identity = inspect_execution_identity(
        orchestrator, task_id, model_config, governance_policy)
    pinned = identity["identity"]
    expected = {
        "task_id": task_id,
        "workflow": task["workflow"],
        "request_sha256": task["request_sha256"],
        "proposal_sha256": proposal["proposal_sha256"],
        "model_profile_sha256": pinned["model_profile_sha256"],
        "model_governance_policy_sha256": pinned["model_governance_policy_sha256"],
        "research_id": plan["research_id"],
        "run_id": plan["run_id"],
        "batch_id": plan["batch_id"],
        "simulation_sha256": fingerprint_json({
            "request": plan["request"], "settings": plan["settings"]}),
        "scope": scope,
    }
    if any(claims.get(key) != value for key, value in expected.items()):
        raise OrchestrationError("Simulation scope, proposal or model identity drift")
    environment = os.environ if environ is None else environ
    secret = environment.get(VERIFY_KEY_ENV)
    if not isinstance(secret, str) or len(secret) < 32:
        raise OrchestrationError("Simulation verifier unavailable")
    check = hmac.new(
        secret.encode("utf-8"), CONTEXT + canonical_json_bytes(claims),
        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(check, signature):
        raise OrchestrationError("Simulation receipt signature mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "workflow": task["workflow"],
        "research_id": plan["research_id"],
        "authorization_id": claims["authorization_id"],
        "simulation_sha256": expected["simulation_sha256"],
        "scope": scope,
        "status": "SCOPE_VERIFIED_NOT_AUTHORIZED",
        "signature_verified": True,
        "permission_to_dispatch": False,
        "brain_simulation_authorized": False,
        "model_call_authorized": False,
        "alpha_submission_authorized": False,
    }
