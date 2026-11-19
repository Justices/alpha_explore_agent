"""P0 research loop using ONLY explicit offline simulation fixtures.

Production simulation, candidate qualification, persisted recovery, and
submissions remain the responsibility of existing project services. This
module never calls them, so it is safe to use for mock contract testing.
"""
import copy
import hashlib
import json
from dataclasses import asdict
from alpha_research.research_cycle.contracts import (
    Experiment, ResearchContractError, ResearchDecision,
)


def _digest(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class OfflineSimulationGateway:
    is_offline_mock = True

    def __init__(self, fixtures: dict):
        if not isinstance(fixtures, dict):
            raise ResearchContractError("offline simulation fixtures must be an object")
        self.fixtures = copy.deepcopy(fixtures)
        self.invocations = []

    def execute(self, experiment: Experiment) -> dict:
        self.invocations.append(experiment.experiment_id)
        result = self.fixtures.get(experiment.experiment_id)
        if not isinstance(result, dict):
            raise ResearchContractError(
                f"offline simulation fixture missing for {experiment.experiment_id}")
        if result.get("source") != "offline_fixture":
            raise ResearchContractError("mock result must declare source=offline_fixture")
        if result.get("status") not in {"COMPLETED", "FAILED", "PENDING"}:
            raise ResearchContractError("mock result has invalid status")
        if result.get("status") == "COMPLETED" and not isinstance(result.get("metrics"), dict):
            raise ResearchContractError("completed fixture requires metrics object")
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ResearchContractError("fixture is not finite JSON") from exc
        return copy.deepcopy(result)


class OfflineResearchCycle:
    """One bounded turn: Lead plan -> Mock results -> Executor attribution -> Lead decision.

    CANDIDATE is always only a proposed action. Actual qualification is NOT
    evaluated in this mock cycle; no experiment is submitted to BRAIN.
    """

    def __init__(self, model, gateway, *, max_experiments=4):
        if type(getattr(gateway, "is_offline_mock", None)) is not bool or (
                gateway.is_offline_mock is not True):
            raise ResearchContractError("P0 requires an explicitly offline simulation gateway")
        if type(max_experiments) is not int or not 1 <= max_experiments <= 8:
            raise ResearchContractError("max_experiments must be between 1 and 8")
        self.model = model
        self.gateway = gateway
        self.max_experiments = max_experiments

    def run_once(self, *, research_id: str, objective: str, region: str):
        for label, value in (("research_id", research_id), ("objective", objective),
                             ("region", region)):
            if not isinstance(value, str) or not value.strip():
                raise ResearchContractError(f"{label} is required")
        begin_session = getattr(self.model, "begin_session", None)
        if callable(begin_session):
            begin_session(research_id=research_id)
        plan = self.model.complete_json("plan_experiments", {
            "research_id": research_id, "objective": objective, "region": region,
            "max_experiments": self.max_experiments, "execution": "offline_mock",
        })
        if not isinstance(plan, dict) or not isinstance(plan.get("experiments"), list):
            raise ResearchContractError("research lead must return experiments array")
        raw = plan["experiments"]
        if not 1 <= len(raw) <= self.max_experiments:
            raise ResearchContractError("plan exceeds the authorized experiment budget")
        experiments = [Experiment.from_dict(item, region=region) for item in raw]
        ids = [item.experiment_id for item in experiments]
        if len(set(ids)) != len(ids):
            raise ResearchContractError("duplicate experiment IDs")
        signatures = [_digest({"expression": x.expression, "region": x.region,
                               "settings": x.settings}) for x in experiments]
        if len(set(signatures)) != len(signatures):
            raise ResearchContractError("duplicate expressions with identical settings")

        results = {x.experiment_id: self.gateway.execute(x) for x in experiments}
        feedback_context = {
            "research_id": research_id,
            "experiment_contracts": [asdict(item) for item in experiments],
            "results": results, "evidence_source": "offline_fixture",
            "note": "Synthetic data cannot establish BRAIN Candidate eligibility.",
        }
        attribution = self.model.complete_json("attribute_results", feedback_context)
        if not isinstance(attribution, dict) or not isinstance(attribution.get("summary"), str):
            raise ResearchContractError("executor must return structured attribution")
        findings = attribution.get("findings")
        if not isinstance(findings, list):
            raise ResearchContractError("attribution requires findings array")
        for finding in findings:
            if (not isinstance(finding, dict)
                    or finding.get("alpha_id") not in results
                    or not isinstance(finding.get("reason"), str)
                    or not finding["reason"].strip()):
                raise ResearchContractError("attribution refers to unknown experiment")
        evidence_refs = [f"offline:{research_id}:{eid}" for eid in ids]
        decision_raw = self.model.complete_json("research_decide", {
            "research_id": research_id, "objective": objective, "region": region,
            "attribution": attribution, "results": results,
            "evidence_refs": evidence_refs,
            "remaining_simulation_budget": 0,
            "candidate_qualification": "NOT_EVALUATED",
        })
        decision = ResearchDecision.from_dict(decision_raw)
        if not set(decision.evidence_refs).issubset(set(evidence_refs)):
            raise ResearchContractError("decision cites evidence outside this research cycle")
        return {
            "research_id": research_id, "region": region, "objective": objective,
            "execution_mode": "offline_mock",
            "experiments": [asdict(item) for item in experiments],
            "simulation_results": results,
            "attribution": attribution,
            "decision": asdict(decision),
            "candidate_qualification": "NOT_EVALUATED",
            "simulation_submitted": False, "alpha_submitted": False,
        }
