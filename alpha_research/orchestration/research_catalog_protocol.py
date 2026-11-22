"""P4.17 exact research-only BRAIN Catalog read protocol.

This is a request validator, NOT a live Gateway. The current platform
Gateway/driver keep their existing stricter wire admission until separately
trusted research grants can be consumed and authenticated. A model never
receives a Gateway instance or executable command handle.
"""
import re

RESEARCH_CATALOG_READS = frozenset({
    "get_platform_setting_options", "get_simulation_settings_schema",
    "readonly_probe_datasets", "get_datasets", "get_datafields",
    "get_operators", "get_alpha_details",
})
_MUTATING = frozenset({
    "simulate_batch", "simulate_super", "resume_simulation_batch",
    "set_case", "tag_alpha", "sync_backtest_cache",
    "check_simulation_cache", "validate_simulation_settings",
})
_SCOPE = frozenset({"region", "delay", "universe", "instrument_type"})
_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_FORBIDDEN = frozenset({
    "live", "allow", "authorization_id", "grant_id", "operator_approval_id",
    "attempt_id", "permission_to_dispatch", "dispatch_authorized",
    "model_call_authorized", "brain_simulation_authorized",
})


def research_catalog_request_allowed(command, params, scope, *,
                                    source_alpha_id=None):
    """Exact command/parameter/scope gate shared by future client and driver.

    Single-Alpha details are denied unless bound to the exact Optimization
    source Alpha. A Super task has no source Alpha and cannot open arbitrary
    details by supplying an ID.
    """
    if (not isinstance(command, str)
            or command not in RESEARCH_CATALOG_READS
            or command in _MUTATING
            or not isinstance(params, dict)
            or _FORBIDDEN.intersection(params)
            or not isinstance(scope, dict)
            or set(scope) != _SCOPE
            or scope["instrument_type"] != "EQUITY"
            or not isinstance(scope["region"], str)
            or re.fullmatch(r"[A-Z]{3,8}", scope["region"]) is None
            or type(scope["delay"]) is not int
            or scope["delay"] not in (0, 1)
            or not isinstance(scope["universe"], str)
            or _ID.fullmatch(scope["universe"]) is None):
        return False
    if command == "get_platform_setting_options":
        return params == {"include_schema": True} or params == {}
    if command == "get_simulation_settings_schema":
        return params == {}
    if command == "get_alpha_details":
        return (set(params) == {"alpha_id"}
                and isinstance(params["alpha_id"], str)
                and _ID.fullmatch(params["alpha_id"]) is not None
                and isinstance(source_alpha_id, str)
                and _ID.fullmatch(source_alpha_id) is not None
                and params["alpha_id"] == source_alpha_id)
    if command == "get_datafields":
        return (
            set(params) == (_SCOPE | {"dataset_id"})
            and isinstance(params["dataset_id"], str)
            and _ID.fullmatch(params["dataset_id"]) is not None
            and all(type(params[k]) is type(scope[k])
                    and params[k] == scope[k] for k in _SCOPE)
        )
    if command in {"readonly_probe_datasets", "get_datasets", "get_operators"}:
        return (
            set(params) == _SCOPE
            and all(type(params[k]) is type(scope[k])
                    and params[k] == scope[k] for k in _SCOPE)
        )
    return False


def inspect_catalog_read_contract(orchestrator, task_id):
    """Return a read-only protocol description, never runnable admission."""
    task = orchestrator.load(task_id)
    scope = {
        "region": task["request"].get("region"),
        "delay": task["request"].get("delay"),
        "universe": task["request"].get("universe"),
        "instrument_type": "EQUITY",
    }
    if not research_catalog_request_allowed(
            "readonly_probe_datasets", scope, scope):
        from alpha_research.orchestration.engine import OrchestrationError
        raise OrchestrationError("catalog contract needs exact task scope")
    return {
        "schema_version": 1,
        "task_id": task_id,
        "scope": scope,
        "allowed_read_actions": sorted(
            RESEARCH_CATALOG_READS
            if task["workflow"] == "optimization"
            else RESEARCH_CATALOG_READS - {"get_alpha_details"}),
        "status": "CATALOG_PROTOCOL_VALIDATED_NO_LIVE_DRIVER",
        "driver_authorized": False,
        "model_call_authorized": False,
        "permission_to_dispatch": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "brain_invoked": False, "provider_invoked": False,
    }
