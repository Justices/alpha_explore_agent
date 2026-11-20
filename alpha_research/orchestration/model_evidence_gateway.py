"""P3 read-only model evidence bridge to the EXISTING BRAIN Gateway.

Only the orchestrator may instantiate this bridge. A model gets the returned
evidence, never a Gateway instance or executable tool handle. The bridge
cannot submit/resume Simulation, tag an Alpha, or modify platform state.
"""
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from alpha_research.state.store import atomic_write_json


class ModelGatewayPermissionError(ValueError):
    pass


READ_ACTIONS = frozenset({
    "get_platform_setting_options", "get_simulation_settings_schema",
    "get_datasets", "get_datafields", "get_operators",
})


def _digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ModelGatewayPermissionError("unserializable BRAIN evidence") from exc
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ReadOnlyEvidenceGateway:
    """Strictly scoped, receipt-backed catalog calls; never simulation methods."""

    def __init__(self, gateway, state_root, *, research_id, region,
                 delay=1, universe=None):
        if not isinstance(research_id, str) or not research_id.strip():
            raise ModelGatewayPermissionError("research ID required")
        if not isinstance(region, str) or not region.strip():
            raise ModelGatewayPermissionError("Region required")
        if type(delay) is not int or delay < 0:
            raise ModelGatewayPermissionError("Delay must be an integer")
        if universe is not None and (
                not isinstance(universe, str) or not universe.strip()):
            raise ModelGatewayPermissionError("Universe must be specified or None")
        if not callable(getattr(gateway, "call", None)):
            raise ModelGatewayPermissionError("orchestrator Gateway.call required")
        self._gateway = gateway
        self._root = (Path(state_root).resolve() / "state" /
                      "model_evidence" /
                      hashlib.sha256(research_id.encode("utf-8")).hexdigest())
        self._scope = {"region": region, "delay": delay, "universe": universe}

    def read(self, action, *, dataset_id=None):
        if action not in READ_ACTIONS:
            raise ModelGatewayPermissionError(
                "model requested an unauthorized or mutating BRAIN action")
        if action == "get_datafields":
            if (not isinstance(dataset_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", dataset_id)):
                raise ModelGatewayPermissionError("valid Dataset ID required")
        elif dataset_id is not None:
            raise ModelGatewayPermissionError("Dataset argument not allowed for this action")
        params = {"region": self._scope["region"], "delay": self._scope["delay"]}
        if self._scope["universe"] is not None:
            params["universe"] = self._scope["universe"]
        if action in {"get_datafields", "get_datasets", "get_operators"}:
            params["instrument_type"] = "EQUITY"
        if action == "get_datafields":
            params["dataset_id"] = dataset_id
        if action == "get_platform_setting_options":
            params = {"include_schema": True}
        if action == "get_simulation_settings_schema":
            params = {}
        # The gateway's original permission, BRAIN client and catalog audit
        # rules remain authoritative. No model callback is passed through.
        response = self._gateway.call(action, **params)
        receipt = {
            "schema_version": 1,
            "action": action,
            "scope": dict(self._scope),
            "params": params,
            "response_sha256": _digest(response),
            "request_id": uuid4().hex,
            "mode": "read_only",
        }
        path = self._root / (receipt["request_id"] + ".json")
        atomic_write_json(path, receipt)
        return {
            "action": action,
            "scope": dict(self._scope),
            "evidence_ref": str(path),
            "response_sha256": receipt["response_sha256"],
            "response": response,
        }
