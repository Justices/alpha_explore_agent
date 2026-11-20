"""A tightly scoped model-only connectivity probe.

Default: validate a selected route, no model call.
Actual model requests require allow_model_call=True and use a fresh,
audited one-call research ID. BRAIN is never imported or exposed.
"""
import copy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from alpha_research.agents.configuration import RoleRoutingConfig
from alpha_research.integrations.model import DecisionRecorder, build_model_client


class ModelProbeError(ValueError):
    pass


_PROBE_POLICY = {"max_calls": 1, "max_reported_tokens": 2048, "failover": "manual"}


def prepare_probe(model_config, *, role):
    if role not in {"research_lead", "research_executor"}:
        raise ModelProbeError("model probe role must be research_lead or research_executor")
    routing = RoleRoutingConfig.parse(model_config)
    if role not in routing.roles:
        raise ModelProbeError("selected role does not exist")
    effective = copy.deepcopy(model_config)
    effective["task_roles"]["model_probe"] = role
    selected = RoleRoutingConfig.parse(effective).resolve("model_probe")
    return effective, {
        "role": role,
        "backend": selected.backend_name,
        "provider": selected.backend["provider"],
        "model": selected.backend.get("model"),
    }


def run_model_probe(model_config, state_root, *, role, allow_model_call=False,
                    model_factory=build_model_client):
    """Attempt exactly one model JSON task, without any BRAIN gateway.

    No retries, no automatic failover, no exposure of credentials or the raw
    returned response in the printed report. Full input/output remains in the
    existing local DecisionRecorder audit log.
    """
    effective, binding = prepare_probe(model_config, role=role)
    if allow_model_call is not True:
        return {
            "status": "NOT_EXECUTED",
            "reason": "real model request requires --allow-model-call",
            **binding,
            "provider_invoked": False,
            "brain_invoked": False,
            "contract": "model_probe_v1",
        }
    state_root = Path(state_root).expanduser().resolve()
    research_id = "model-probe:" + uuid4().hex
    recorder = DecisionRecorder(
        model_factory(effective), state_root / "state",
        governance=_PROBE_POLICY,
    )
    recorder.begin_session(research_id=research_id)
    response = recorder.complete_json("model_probe", {
        "purpose": "model JSON contract connectivity only",
        "schema": {"ready": True, "contract": "model_probe_v1"},
        "no_tools": True,
        "no_brain_requests": True,
    })
    # The model response is untrusted. Never execute or forward it to BRAIN.
    if not isinstance(response, dict):
        raise ModelProbeError("model probe response is not a JSON object")
    valid = (response.get("ready") is True
             and response.get("contract") == "model_probe_v1"
             and set(response) == {"ready", "contract"})
    digest = hashlib.sha256(json.dumps(
        response, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {
        "status": "PASS" if valid else "CONTRACT_MISMATCH",
        **binding,
        "provider_invoked": True,
        "brain_invoked": False,
        "contract": "model_probe_v1",
        "response_sha256": digest,
        "research_id": research_id,
        "reported_usage": recorder.budget.status(research_id),
    }
