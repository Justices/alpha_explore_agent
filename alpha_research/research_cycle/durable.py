"""Durable, bounded OFFLINE research orchestration; deliberately no live BRAIN client.

Journaled task model:
  immutable request -> round model checkpoints -> execution intents -> evidence
  -> attribution -> decision -> next round or terminal status.

In-flight simulation intents are never automatically replayed, even when the
fixture gateway is demonstrably idempotent. They need explicit reconciliation.
This conservative contract matches existing live BRAIN recovery requirements.
"""
import copy
import hashlib
import json
from pathlib import Path

from alpha_research.research_cycle.contracts import ResearchContractError
from alpha_research.research_cycle.engine import (
    OfflineResearchCycle, OfflineSimulationGateway,
)
from alpha_research.state.store import atomic_write_json, read_json
from alpha_research.state.writer_lock import SingleWriterLock


def fingerprint(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ResearchContractError("research state must be finite JSON") from exc
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _model_profile_hash(model):
    """Bind each run to its effective backend routing, never to secrets."""
    client = getattr(model, "model", model)  # DecisionRecorder wrapper
    routing = getattr(client, "routing", None)
    if routing is not None:
        return fingerprint({
            "default_role": routing.default_role,
            "task_roles": routing.task_roles,
            "roles": routing.roles,
            "backends": routing.backends,
        })
    config = getattr(client, "config", None)
    if isinstance(config, dict):
        return fingerprint(config)
    raise ResearchContractError("durable runner requires a configurable model profile")


class _RoundModel:
    """Cache every completed model response; never replace a persisted answer."""

    def __init__(self, runner, state, index):
        self.runner, self.state, self.index = runner, state, index

    def begin_session(self, *, research_id):
        begin = getattr(self.runner.model, "begin_session", None)
        if callable(begin):
            begin(research_id=research_id)

    def complete_json(self, task, context):
        if not isinstance(context, dict):
            raise ResearchContractError("model context must be an object")
        round_record = self.state["rounds"][self.index]
        key = task
        digest = fingerprint(context)
        cached = round_record["model_calls"].get(key)
        if cached is not None:
            if cached["context_sha256"] != digest:
                raise ResearchContractError("model checkpoint context mismatch")
            return copy.deepcopy(cached["response"])
        result = self.runner.model.complete_json(task, context)
        if not isinstance(result, dict):
            raise ResearchContractError("model checkpoint requires a JSON object")
        fingerprint(result)
        round_record["model_calls"][key] = {
            "context_sha256": digest, "response": copy.deepcopy(result),
        }
        self.runner._save(self.state)
        return copy.deepcopy(result)


class _JournaledSimulationGateway:
    is_offline_mock = True

    def __init__(self, runner, state, index):
        self.runner, self.state, self.index = runner, state, index

    def execute(self, experiment):
        from dataclasses import asdict
        row = self.state["rounds"][self.index]
        key = experiment.experiment_id
        experiment_sha256 = fingerprint(asdict(experiment))
        old = row["simulations"].get(key)
        if old is not None:
            if old["experiment_sha256"] != experiment_sha256:
                raise ResearchContractError("simulation intent changed after preregistration")
            if old["status"] != "COMPLETED":
                raise ResearchContractError(
                    "simulation outcome unresolved; explicit offline reconciliation required")
            return copy.deepcopy(old["result"])
        row["simulations"][key] = {
            "status": "IN_FLIGHT", "experiment_sha256": experiment_sha256,
            "experiment": asdict(experiment), "result": None,
        }
        self.runner._save(self.state)  # preregister BEFORE potentially external call
        result = self.runner.gateway.execute(experiment)
        fingerprint(result)
        row["simulations"][key]["result"] = copy.deepcopy(result)
        row["simulations"][key]["status"] = (
            "COMPLETED" if result.get("status") == "COMPLETED" else "UNSETTLED"
        )
        self.runner._save(self.state)
        return result


class DurableOfflineResearchRunner:
    """Exactly-one-writer, persisted multi-round P1 mock workflow.

    This does not prove exactly-once calls to a live BRAIN API. The only
    permitted simulator is the concrete OfflineSimulationGateway fixture.
    """

    TERMINAL = frozenset({"STOP", "REJECT", "CANDIDATE"})

    def __init__(self, model, gateway, state_root, *,
                 max_rounds=3, max_total_experiments=12, max_per_round=4):
        if type(gateway) is not OfflineSimulationGateway:
            raise ResearchContractError("durable P1 only accepts the concrete offline fixture gateway")
        for name, value, bound in (
            ("max_rounds", max_rounds, 10),
            ("max_total_experiments", max_total_experiments, 40),
            ("max_per_round", max_per_round, 8),
        ):
            if type(value) is not int or not 1 <= value <= bound:
                raise ResearchContractError(f"{name} must be in range 1..{bound}")
        self.model = model
        self.model_profile_sha256 = _model_profile_hash(model)
        self.gateway = gateway
        self.state_root = Path(state_root).expanduser().resolve()
        self.max_rounds = max_rounds
        self.max_total_experiments = max_total_experiments
        self.max_per_round = max_per_round
        self.lock_path = self.state_root / "state" / "agent.lock"

    def _path(self, research_id):
        if not _is_nonempty(research_id):
            raise ResearchContractError("research_id must be nonempty")
        key = hashlib.sha256(research_id.encode("utf-8")).hexdigest()
        return self.state_root / "state" / "research_cycle" / (key + ".json")

    @staticmethod
    def _state_payload(state):
        return {key: value for key, value in state.items()
                if key != "state_sha256"}

    def _save(self, state):
        # Detect accidental/corrupt state edits. This is an integrity check,
        # not an HMAC or protection against someone able to rewrite the file.
        state["state_sha256"] = fingerprint(self._state_payload(state))
        atomic_write_json(self._path(state["request"]["research_id"]), state)

    def load(self, research_id):
        path = self._path(research_id)
        state = read_json(path)
        if not isinstance(state, dict):
            raise ResearchContractError("no readable durable research state")
        if (not isinstance(state.get("state_sha256"), str)
                or state["state_sha256"] != fingerprint(self._state_payload(state))):
            raise ResearchContractError(
                "durable state checksum invalid or unsealed; explicit migration required")
        request = state.get("request")
        rounds = state.get("rounds")
        if (state.get("schema_version") != 1 or state.get("mode") != "offline_mock"
                or not isinstance(request, dict)
                or request.get("research_id") != research_id
                or not isinstance(rounds, list)
                or state.get("status") not in {
                    "CREATED", "RUNNING", "BLOCKED", "COMPLETED", "BUDGET_EXHAUSTED"}
                or state.get("candidate_qualification") != "NOT_EVALUATED"
                or state.get("simulation_submitted") is not False
                or state.get("alpha_submitted") is not False):
            raise ResearchContractError("durable research state is invalid")
        if (state.get("request_sha256") != fingerprint(request)
                or type(state.get("used_experiments")) is not int
                or state["used_experiments"] < 0
                or len(rounds) > request.get("max_rounds", 0)):
            raise ResearchContractError("durable request or budget evidence is invalid")
        observed_ids = set()
        completed_intents = 0
        for index, row in enumerate(rounds):
            if (not isinstance(row, dict) or row.get("index") != index
                    or row.get("status") not in {"RUNNING", "COMPLETED"}
                    or not isinstance(row.get("model_calls"), dict)
                    or not isinstance(row.get("simulations"), dict)):
                raise ResearchContractError("durable round state is invalid")
            for experiment_id, intent in row["simulations"].items():
                if (not isinstance(experiment_id, str) or experiment_id in observed_ids
                        or not isinstance(intent, dict)
                        or intent.get("status") not in {"IN_FLIGHT", "UNSETTLED", "COMPLETED"}
                        or not isinstance(intent.get("experiment"), dict)
                        or intent.get("experiment_sha256") != fingerprint(intent["experiment"])):
                    raise ResearchContractError("durable simulation intent is invalid")
                observed_ids.add(experiment_id)
                if intent["status"] == "COMPLETED":
                    completed_intents += 1
            if row["status"] == "COMPLETED":
                report = row.get("report")
                if (not isinstance(report, dict)
                        or report.get("execution_mode") != "offline_mock"
                        or report.get("candidate_qualification") != "NOT_EVALUATED"
                        or report.get("simulation_submitted") is not False
                        or report.get("alpha_submitted") is not False
                        or any(intent["status"] != "COMPLETED"
                               for intent in row["simulations"].values())):
                    raise ResearchContractError("completed round has unresolved evidence")
        if (state["used_experiments"] > len(observed_ids)
                or (state["status"] in {"COMPLETED", "BUDGET_EXHAUSTED"}
                    and state["used_experiments"] != len(observed_ids))):
            raise ResearchContractError("durable research budget accounting is invalid")
        return state

    def _settle_completed_round(self, state):
        """Close a crash gap after a completed round, without another model call."""
        if not state["rounds"] or state["rounds"][-1]["status"] != "COMPLETED":
            return
        report = state["rounds"][-1]["report"]
        if not isinstance(report, dict) or not isinstance(report.get("decision"), dict):
            raise ResearchContractError("completed round is missing its decision")
        action = report["decision"].get("action")
        if action not in {"OPTIMIZE", "INVESTIGATE", "EXPLORE", "CANDIDATE", "REJECT", "STOP"}:
            raise ResearchContractError("completed round has invalid decision")
        state["used_experiments"] = sum(
            len(row["simulations"]) for row in state["rounds"])
        if action in self.TERMINAL:
            state["status"] = "COMPLETED"
            state["stop_reason"] = action
        elif (len(state["rounds"]) >= self.max_rounds
                or state["used_experiments"] >= self.max_total_experiments):
            state["status"] = "BUDGET_EXHAUSTED"
            state["stop_reason"] = "research_budget_reached"
        else:
            state["status"] = "RUNNING"
        self._save(state)

    def _initialize(self, request):
        path = self._path(request["research_id"])
        if path.exists():
            state = self.load(request["research_id"])
            if state.get("request_sha256") != fingerprint(request):
                raise ResearchContractError("cannot silently change persisted research scope or budget")
            return state
        state = {
            "schema_version": 1, "mode": "offline_mock",
            "request": copy.deepcopy(request),
            "request_sha256": fingerprint(request),
            "status": "CREATED", "rounds": [], "used_experiments": 0,
            "failure": None, "candidate_qualification": "NOT_EVALUATED",
            "simulation_submitted": False, "alpha_submitted": False,
        }
        self._save(state)
        return state

    def run(self, *, research_id, objective, region, resume_blocked=False):
        for label, value in (
            ("research_id", research_id), ("objective", objective), ("region", region),
        ):
            if not _is_nonempty(value):
                raise ResearchContractError(f"{label} must be nonempty")
        request = {
            "research_id": research_id, "objective": objective, "region": region,
            "max_rounds": self.max_rounds,
            "max_total_experiments": self.max_total_experiments,
            "max_per_round": self.max_per_round,
            "model_profile_sha256": self.model_profile_sha256,
        }
        with SingleWriterLock(self.lock_path):
            state = self._initialize(request)
            if state["status"] == "RUNNING":
                self._settle_completed_round(state)
            if state["status"] in {"COMPLETED", "BUDGET_EXHAUSTED"}:
                return copy.deepcopy(state)
            if state["status"] == "BLOCKED":
                if resume_blocked is not True:
                    raise ResearchContractError("blocked task requires explicit resume")
                for round_record in state["rounds"]:
                    if any(item["status"] != "COMPLETED"
                           for item in round_record["simulations"].values()):
                        raise ResearchContractError(
                            "unresolved simulation intent requires explicit reconciliation")
                state["status"] = "RUNNING"
                state["failure"] = None
                self._save(state)
            while len(state["rounds"]) < self.max_rounds or (
                    state["rounds"] and state["rounds"][-1]["status"] == "RUNNING"):
                used = sum(len(row["simulations"]) for row in state["rounds"])
                remaining = self.max_total_experiments - used
                if remaining <= 0:
                    state["status"] = "BUDGET_EXHAUSTED"
                    self._save(state)
                    break
                if state["rounds"] and state["rounds"][-1]["status"] == "RUNNING":
                    index = len(state["rounds"]) - 1
                else:
                    index = len(state["rounds"])
                    state["rounds"].append({
                        "index": index, "status": "RUNNING",
                        "model_calls": {}, "simulations": {}, "report": None,
                    })
                    state["status"] = "RUNNING"
                    self._save(state)
                history = [
                    copy.deepcopy(row["report"])
                    for row in state["rounds"][:index] if row["status"] == "COMPLETED"
                ]
                cycle = OfflineResearchCycle(
                    _RoundModel(self, state, index),
                    _JournaledSimulationGateway(self, state, index),
                    max_experiments=min(self.max_per_round, remaining),
                )
                try:
                    report = cycle.run_once(
                        research_id=research_id, objective=objective, region=region,
                        round_index=index, history=history,
                    )
                except Exception as exc:
                    state["status"] = "BLOCKED"
                    state["failure"] = {"round_index": index, "type": type(exc).__name__,
                                        "message": str(exc)[:500]}
                    self._save(state)
                    raise
                row = state["rounds"][index]
                row["report"] = report
                row["status"] = "COMPLETED"
                state["used_experiments"] = sum(
                    len(r["simulations"]) for r in state["rounds"])
                state["status"] = "RUNNING"
                self._save(state)
                self._settle_completed_round(state)
                if state["status"] in {"COMPLETED", "BUDGET_EXHAUSTED"}:
                    break
            return copy.deepcopy(state)

    def invalidate_model_checkpoint(self, research_id, task, reason):
        """Explicitly invalidate an erroneous model output, retaining audit hashes.

        A plan/dispatch checkpoint cannot be replaced after any simulation
        intent has been posted. Attribution/decision may be re-run only once
        every experiment result is already settled. The original model call
        remains in DecisionRecorder audit logs.
        """
        if task not in {
            "plan_experiments", "prepare_execution",
            "attribute_results", "research_decide",
        }:
            raise ResearchContractError("invalidatable checkpoint task is unsupported")
        if not _is_nonempty(reason) or len(reason.strip()) < 5:
            raise ResearchContractError("checkpoint invalidation requires a meaningful reason")
        with SingleWriterLock(self.lock_path):
            state = self.load(research_id)
            if state["status"] != "BLOCKED" or not state["rounds"]:
                raise ResearchContractError("checkpoint invalidation requires a blocked round")
            row = state["rounds"][-1]
            if row["status"] != "RUNNING" or task not in row["model_calls"]:
                raise ResearchContractError("checkpoint was not recorded in the blocked round")
            sims = row["simulations"]
            if any(x["status"] != "COMPLETED" for x in sims.values()):
                raise ResearchContractError("cannot replace model output with unresolved simulation intent")
            if task in {"plan_experiments", "prepare_execution"} and sims:
                raise ResearchContractError("cannot change experiment plan after simulation started")
            call = row["model_calls"].pop(task)
            state.setdefault("checkpoint_invalidations", []).append({
                "round_index": row["index"],
                "task": task,
                "context_sha256": call["context_sha256"],
                "response_sha256": fingerprint(call["response"]),
                "reason": reason.strip(),
            })
            self._save(state)
            return copy.deepcopy(state)

    def reconcile_offline_fixture(self, research_id, experiment_id):
        """Explicitly reconcile an ambiguous simulated experiment from a fixture.

        NEVER use this method for the real platform: live recovery must use
        its own immutable intent and verified remote simulation receipts.
        """
        with SingleWriterLock(self.lock_path):
            state = self.load(research_id)
            if state["status"] != "BLOCKED":
                raise ResearchContractError("reconciliation requires BLOCKED research")
            if not _is_nonempty(experiment_id):
                raise ResearchContractError("experiment_id required")
            pending = []
            for row in state["rounds"]:
                item = row["simulations"].get(experiment_id)
                if item and item["status"] in {"IN_FLIGHT", "UNSETTLED"}:
                    pending.append(item)
            if len(pending) != 1:
                raise ResearchContractError("must identify one unresolved offline experiment")
            item = pending[0]
            # This reads the declared fixture without calling execute a second time.
            fixture = self.gateway.fixtures.get(experiment_id)
            if not isinstance(fixture, dict) or fixture.get("source") != "offline_fixture":
                raise ResearchContractError("no authoritative offline fixture to reconcile")
            # Validate the readback with the same deterministic schema as the
            # original offline executor, without calling the interrupted executor.
            from alpha_research.research_cycle.contracts import Experiment
            verified = OfflineSimulationGateway({experiment_id: fixture}).execute(
                Experiment.from_dict(item["experiment"], region=state["request"]["region"]))
            fingerprint(verified)
            if verified["status"] != "COMPLETED":
                raise ResearchContractError(
                    "reconciliation requires a completed offline fixture readback")
            item["result"] = copy.deepcopy(verified)
            item["status"] = "COMPLETED"
            state["reconciliation"] = {
                "mode": "offline_fixture", "experiment_id": experiment_id,
                "result_sha256": fingerprint(verified),
            }
            self._save(state)
            return copy.deepcopy(state)
