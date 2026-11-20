"""Read-only local readiness checks. Never invoke an Agent or model API."""
import importlib.util
import os
import shutil
from urllib.parse import urlsplit

from alpha_research.agents.configuration import RoleRoutingConfig, backend_kind


def _sdk_present():
    try:
        return importlib.util.find_spec("google.antigravity") is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def check_registered_roles(model_config, *, environ=None,
                           which=None, sdk_present=None):
    """Return local readiness, not proof of a provider's remote availability.

    Environment variable values and resolved credentials are never returned.
    """
    routing = RoleRoutingConfig.parse(model_config)
    environment = os.environ if environ is None else environ
    find_cli = shutil.which if which is None else which
    has_sdk = _sdk_present if sdk_present is None else sdk_present
    rows = {}
    for role in sorted(routing.roles):
        backend_id = routing.roles[role]["backend"]
        spec = routing.backends[backend_id]
        provider = spec["provider"]
        kind = backend_kind(provider)
        reason = None
        ready = False
        if provider == "mock":
            ready = True
            reason = "offline fixture backend"
        elif provider in {"codex_cli", "claude_cli"}:
            executable = spec.get("executable") or (
                "codex" if provider == "codex_cli" else "claude"
            )
            try:
                ready = bool(find_cli(executable))
            except (OSError, ValueError, TypeError):
                ready = False
            reason = ("CLI available locally; authentication and behavior unverified"
                      if ready else "CLI executable not found")
        elif provider == "antigravity_sdk":
            env_name = spec["api_key_env"]
            has_key = bool(environment.get(env_name))
            installed = bool(has_sdk())
            ready = has_key and installed
            reason = ("SDK package and credential environment detected; remote access unverified"
                      if ready else "SDK package or API credential environment missing")
        else:
            url = urlsplit(spec["url"])
            env_name = spec.get("api_key_env")
            has_key = bool(environment.get(env_name)) if env_name else True
            endpoint_valid = url.scheme == "https" and bool(url.hostname)
            ready = endpoint_valid and has_key
            reason = ("HTTPS endpoint and credential environment configured; remote access unverified"
                      if ready else "HTTPS endpoint or API credential environment unavailable")
        rows[role] = {
            "registered_backend": backend_id,
            "kind": kind,
            "provider": provider,
            "ready_local": bool(ready),
            "reason": reason,
        }
    return {
        "status": "READY_LOCAL" if all(r["ready_local"] for r in rows.values())
                  else "BLOCKED_LOCAL",
        "roles": rows,
        "provider_invoked": False,
        "brain_invoked": False,
        "remote_health_verified": False,
    }
