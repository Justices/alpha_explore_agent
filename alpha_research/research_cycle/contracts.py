"""Immutable contracts for research hypotheses, experiments, and decisions."""
import json
from dataclasses import dataclass
from typing import Any


class ResearchContractError(ValueError):
    """Invalid or inconsistent research contract."""


ACTIONS = frozenset({
    "OPTIMIZE", "INVESTIGATE", "EXPLORE", "CANDIDATE", "REJECT", "STOP"
})


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    expression: str
    region: str
    settings: dict
    hypothesis: str
    failure_criterion: str

    @classmethod
    def from_dict(cls, value: Any, *, region: str):
        if not isinstance(value, dict):
            raise ResearchContractError("experiment must be an object")
        if value.get("region") != region:
            raise ResearchContractError("experiment must preserve the requested Region")
        required = ("experiment_id", "expression", "hypothesis", "failure_criterion")
        if any(not isinstance(value.get(name), str) or not value[name].strip()
               for name in required):
            raise ResearchContractError(
                "experiment id, expression, hypothesis and failure criterion are required")
        if not isinstance(value.get("settings"), dict) or not value["settings"]:
            raise ResearchContractError("experiment requires explicit nonempty settings")
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ResearchContractError("experiment must be finite JSON") from exc
        return cls(value["experiment_id"], value["expression"], region,
                   dict(value["settings"]), value["hypothesis"], value["failure_criterion"])


@dataclass(frozen=True)
class ResearchDecision:
    action: str
    reason: str
    evidence_refs: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any):
        if not isinstance(value, dict) or value.get("action") not in ACTIONS:
            raise ResearchContractError("research decision has invalid action")
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ResearchContractError("research decision requires a reason")
        refs = value.get("evidence_refs")
        if not isinstance(refs, list) or not refs or any(
                not isinstance(item, str) or not item for item in refs):
            raise ResearchContractError("decision must cite evidence references")
        if len(set(refs)) != len(refs):
            raise ResearchContractError("duplicate evidence references")
        return cls(value["action"], reason, tuple(refs))
