"""Read-only local prerequisites for a *later* authorized research smoke.

Never authenticates, invokes a model, requests a BRAIN catalog or submits a
simulation. Detects only what can be established from local configuration.
"""
import json
import os
from pathlib import Path

from alpha_research.agents.governance import validate_policy
from alpha_research.agents.readiness import inspect_live_model_roles
from alpha_research.state.paths import expand_path


def _credentials_present(config, environment):
    auth = config.get("auth") or {}
    if not isinstance(auth, dict) or auth.get("mode", "auto") != "auto":
        return False
    variable = auth.get("credential_env", "BRAIN_CREDENTIALS_FILE")
    if not isinstance(variable, str) or not variable:
        return False
    location = environment.get("BRAIN_CREDENTIALS_FILE") or environment.get(variable)
    if not location:
        return False
    try:
        data = json.loads(expand_path(location).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return False
        nested = data.get("credentials") or {}
        if not isinstance(nested, dict):
            return False
        email = nested.get("email") or data.get("email")
        password = nested.get("password") or data.get("password")
        return all(isinstance(item, str) and item.strip()
                   for item in (email, password))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def local_preflight(config_path, registry_path, *, lead, executor, region=None,
                    environment=None):
    """Return local readiness only, without exposing any credential contents."""
    env = os.environ if environment is None else environment
    result = {
        "status": "BLOCKED_LOCAL",
        "config": "BLOCKED",
        "scope": "BLOCKED",
        "models": "BLOCKED",
        "model_budget": "BLOCKED",
        "brain_credentials": "BLOCKED",
        "model_invoked": False,
        "brain_invoked": False,
        "remote_health_verified": False,
        "simulation_authorized": False,
    }
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("configuration is not a JSON object")
    except (OSError, ValueError):
        return result
    result["config"] = "READY_LOCAL"
    try:
        policy = validate_policy(cfg.get("model_governance"))
    except ValueError:
        pass
    else:
        result["model_budget"] = "READY_LOCAL"
        result["max_model_calls"] = policy["max_calls"]
        result["max_reported_tokens"] = policy["max_reported_tokens"]
    configured_scope = cfg.get("scope") or {}
    if isinstance(configured_scope, dict):
        selected_region = configured_scope.get("region")
        if region is not None and selected_region not in (None, region):
            selected_region = None
        elif region is not None:
            selected_region = region
        if isinstance(selected_region, str) and selected_region.strip():
            result["scope"] = "READY_LOCAL"
            result["region"] = selected_region
    if _credentials_present(cfg, env):
        result["brain_credentials"] = "READY_LOCAL"
    if (isinstance(lead, str) and lead.strip()
            and isinstance(executor, str) and executor.strip()):
        try:
            readiness = inspect_live_model_roles(
                cfg, registry_path, lead=lead, executor=executor,
                environment=env)
            result["models"] = readiness["status"]
            result["roles"] = readiness["roles"]
            result["profile_sha256"] = readiness["profile_sha256"]
        except (OSError, ValueError, TypeError):
            pass
    if all(result[field] == "READY_LOCAL" for field in (
            "config", "scope", "models", "model_budget",
            "brain_credentials")):
        result["status"] = "READY_LOCAL"
    return result
