"""Workbench model selection and local-only research admission.

A browser selects registry IDs, never transport definitions, provider payloads,
or credentials. No function here invokes a model or BRAIN endpoint.
"""
import hashlib
import json
from pathlib import Path

from alpha_research.agents.doctor import check_registered_roles
from alpha_research.agents.registry import (
    bundled_registry_path, describe_registry, load_registry,
    load_role_profile, select_registered_backends,
)
from alpha_research.state.fingerprint import canonical_json_bytes


def _argument(agent_args, name):
    for index, item in enumerate(agent_args):
        if item == name:
            if index + 1 >= len(agent_args):
                raise ValueError(f"missing {name} value")
            return agent_args[index + 1]
        if item.startswith(name + "="):
            return item[len(name) + 1:]
    return None


def registry_path(agent_args, root):
    configured = _argument(agent_args, "--registry")
    if configured:
        return Path(configured).expanduser().resolve()
    override = Path(root) / "config" / "model_registry.json"
    return override if override.exists() else bundled_registry_path()


def research_config_path(agent_args, root):
    configured = _argument(agent_args, "--config")
    return (Path(configured).expanduser().resolve() if configured
            else Path(root) / "config" / "nastu_quant.json")


def listed_models(agent_args, root):
    """Return no-secret metadata for the UI selection controls."""
    return describe_registry(load_registry(registry_path(agent_args, root)))


def checked_selection(payload, agent_args, root):
    """Return immutable role IDs and the effective profile fingerprint.

    This preflight checks local prerequisites only. A healthy executable or
    present API key does not assert remote login or provider readiness.
    """
    if (not isinstance(payload, dict)
            or set(payload) != {"lead", "executor"}
            or any(not isinstance(payload.get(role), str)
                   or not payload[role].strip()
                   for role in ("lead", "executor"))):
        raise ValueError("研究启动必须明确选择 Lead 与 Executor 模型")
    configured_path = research_config_path(agent_args, root)
    try:
        config = json.loads(configured_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("研究配置文件缺失或无效，请先配置 state-root/config") from exc
    if not isinstance(config, dict):
        raise ValueError("研究配置必须是 JSON 对象")
    registry = load_registry(registry_path(agent_args, root))
    mapping = {"research_lead": payload["lead"],
               "research_executor": payload["executor"]}
    effective = select_registered_backends(load_role_profile(config), registry,
                                           assignments=mapping)
    ready = check_registered_roles(effective)
    if ready["status"] != "READY_LOCAL":
        missing = ", ".join(
            role for role, detail in ready["roles"].items()
            if not detail["ready_local"])
        raise ValueError(
            "模型本地条件不满足（" + missing +
            "）；检查 CLI、SDK 或 API Key 环境变量后重试")
    return {
        "lead": payload["lead"], "executor": payload["executor"],
        "profile_sha256": hashlib.sha256(canonical_json_bytes(effective)).hexdigest(),
    }
