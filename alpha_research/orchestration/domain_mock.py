"""Explicit offline domain runtime for Super and Optimization contract verification.

Invokes the actual domain dispatch/intent/record code while replacing ALL
external model and gateway entrypoints with in-process scripted fakes.
Synthetic data must never be confused with real research evidence.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alpha_research.config import DEFAULT_CONFIG
from alpha_research.orchestration.offline_adapter import MockWorkflowAdapter
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.state.records import offline_mock_record_scope
from alpha_research.state.store import atomic_write_json


class DomainMockGateway:
    is_offline_mock = True

    def __init__(self, region, source_alpha_id=None, *, lost_response=False,
                 dataset_ids=None, category=None):
        self.region = region
        self.source_alpha_id = source_alpha_id
        self.lost_response = lost_response
        self.dataset_ids = list(dataset_ids) if dataset_ids else ["OFFLINE_DATASET"]
        self.category = category or "OTHER"
        self.calls = []

    def call(self, command, **kwargs):
        self.calls.append(command)
        if command == "set_case":
            return {}
        if command == "get_platform_setting_options":
            return {"instrument_options": [{
                "InstrumentType": "EQUITY", "Region": self.region,
                "Delay": 1, "Universe": ["TOP3000"]}]}
        if command == "get_simulation_settings_schema":
            return {"selectionHandling": {"type": "choice",
                    "choices": [{"value": "POSITIVE"}]}}
        if command == "get_datasets":
            return [{"id": dataset_id, "category": self.category,
                     "region": self.region, "delay": 1, "universe": "TOP3000",
                     "description": "offline mock dataset"}
                    for dataset_id in self.dataset_ids]
        if command == "get_user_alphas":
            return {"results": [{"id": "OFFLINE_SUBMITTED", "status": "ACTIVE",
                     "type": "REGULAR", "settings": {
                         "region": self.region, "delay": 1}}], "count": 1}
        if command == "get_operators":
            return [{"name": "rank", "scope": ["SELECTION", "COMBO"]}]
        if command == "get_alpha_details" and kwargs.get("alpha_id") == self.source_alpha_id:
            return {"id": self.source_alpha_id, "type": "SUPER",
                    "settings": {"instrument_type": "EQUITY",
                                 "region": self.region, "delay": 1,
                                 "universe": "TOP3000"},
                    "selection": "rank(signal_a)",
                    "combo": "rank(signal_b)"}
        if command == "simulate_super":
            if self.lost_response:
                raise RuntimeError("injected unknown simulation outcome")
            return {"results": [{
                "alpha_id": "OFFLINE_MOCK_ALPHA",
                "details": {"id": "OFFLINE_MOCK_ALPHA",
                            "is": {"sharpe": 1.4}}}],
                "runner": {"mock": True}}
        raise OrchestrationError("offline gateway rejected unsupported command")


class RegularMockAgent:
    """Scripted per-Dataset batch boundary driven by the real Campaign scheduler."""
    def __init__(self, state_root, *, lost_response=False):
        self.runs_root = Path(state_root) / "runs"
        self.stop_flag = Path(state_root) / "state" / "stop.flag"
        self.lost_response = lost_response
        self.calls = 0
        self.recorder = SimpleNamespace(session_id="OFFLINE_SESSION",
                                        call_id="OFFLINE_REVIEW_CALL")

    def _decide(self, task, context, **kwargs):
        if task != "review_dataset_results":
            raise OrchestrationError("unsupported offline Regular review task")
        return {"summary": "Synthetic review: no verified candidate selected",
                "selection": None}

    def run_once(self, objective, *, dataset_id, **kwargs):
        self.calls += 1
        scope = kwargs["fixed_scope"]
        batch = (self.runs_root / scope["region"] / dataset_id /
                 f"offline_mock_batch_{self.calls:04d}")
        batch.mkdir(parents=True, exist_ok=True)
        intent = batch / "simulation_intent.json"
        alpha_id = f"OFFLINE_REGULAR_ALPHA_{self.calls:04d}"
        atomic_write_json(intent, {
            "workflow": "regular", "mode": "offline_domain_mock",
            "status": "READ_BACK", "alpha_ids": [alpha_id],
            "settings": scope,
        })
        atomic_write_json(batch / "prereg.json", {
            "dataset_id": dataset_id,
            "variants": [{"expression": "rank(signal_a)"}],
            "simulation_settings": scope,
        })
        atomic_write_json(batch / "results.json", {
            "alpha_ids": [alpha_id], "attribution_status": "complete",
            "attribution": {"summary": "Synthetic batch attribution"},
            "readback": {alpha_id: {"details": {"id": alpha_id,
                "is": {"sharpe": 0.2, "fitness": 0.1}}}},
        })
        context = kwargs.get("execution_context")
        if context is not None:
            context["bind_recovery_target"]("regular_batch", batch)
        if self.lost_response:
            raise RuntimeError("injected lost Regular mock batch response")
        return {
            "batch_dir": str(batch),
            "alpha_ids": [alpha_id],
            "attribution_status": "complete",
            "candidate_status": {alpha_id: "EVIDENCE_PENDING"},
        }


class DomainMockModel:
    is_offline_mock = True


class DomainMockRecorder:
    """Scripted decisions. Does not reserve a live ModelCallLedger budget."""
    def __init__(self, model, state_root, *, governance=None):
        if not getattr(model, "is_offline_mock", False):
            raise OrchestrationError("offline model required")
        self.model = model

    def begin_session(self, *, research_id):
        return None

    def complete_json(self, task, context, **kwargs):
        # Only the explicit P4.18 isolated runtime records this scripted
        # attempt. Its write-ahead entry is durable before the model fake
        # returns. Existing domain mocks remain independent.
        from alpha_research.orchestration.research_runtime_adapter import (
            active_offline_runtime,
        )
        runtime = active_offline_runtime()
        if runtime is not None:
            runtime.reserve_scripted_model_call(task, context)
        proposal = {"selection": "rank(signal_a)",
                    "combo": "rank(signal_b)", "hypothesis": "synthetic hypothesis",
                    "rationale": "offline-only test", "failure_criterion": "mock-only"}
        if task == "construct_super":
            proposal["settings"] = {
                "region": context.get("region", "GLB") if isinstance(context, dict) else "GLB",
                "delay": 1, "universe": "TOP3000"}
        elif task == "optimize_alpha":
            proposal["changes"] = ["change combo expression"]
        else:
            if runtime is not None:
                runtime.conclude_scripted_model_call(unknown=True)
            raise OrchestrationError("unsupported offline model decision task")
        if runtime is not None:
            runtime.conclude_scripted_model_call(proposal)
        return proposal


class DomainMockKnowledge:
    def __init__(self, *args, **kwargs):
        pass

    def build_pack(self, task, context):
        return {"task": task, "offline": True}


@contextmanager
def mock_domain_boundary(region):
    """Block domain-originated model and knowledge access in the active scope."""
    class RegionRecorder(DomainMockRecorder):
        def complete_json(self, task, context, **kwargs):
            result = super().complete_json(task, context, **kwargs)
            if task == "construct_super":
                result["settings"]["region"] = region
                result["settings"]["instrument_type"] = "EQUITY"
            return result

    with ExitStack() as stack:
        # ContextVar-scoped provenance protects concurrent workflows from
        # process-wide ResearchRecordStore monkey patches.
        stack.enter_context(offline_mock_record_scope())
        # Regular Dataset review executes its real evidence checks against
        # synthetic prereg, intent, readback and attribution records.
        for module in ("alpha_research.workflows.super_alpha.workflow",
                       "alpha_research.workflows.optimization.workflow"):
            stack.enter_context(patch(module + ".KnowledgeService", DomainMockKnowledge))
            stack.enter_context(patch(module + ".DecisionRecorder", RegionRecorder))
        yield


def build_domain_mock_adapter(state_root, task, *, fault=None):
    if task["workflow"] not in ("regular", "super", "optimization"):
        raise OrchestrationError("unsupported domain mock workflow")
    if fault not in (None, "lost-simulation-response"):
        raise OrchestrationError("unsupported domain mock failure injection")
    request = task["request"]
    region, delay = request.get("region"), request.get("delay")
    if not isinstance(region, str) or not region.strip() or delay != 1:
        raise OrchestrationError("domain mock requires explicit Region and Delay 1")
    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["scope"]["region"] = region
    cfg["scope"]["delay"] = 1
    gateway = DomainMockGateway(
        region, request.get("alpha_id"),
        lost_response=fault == "lost-simulation-response",
        dataset_ids=request.get("dataset_ids"),
        category=request.get("category"))
    if task["workflow"] == "regular":
        agent = RegularMockAgent(
            state_root, lost_response=fault == "lost-simulation-response")
        # MockWorkflowAdapter needs a fake model marker even for Campaign-only mode.
        agent.model = DomainMockModel()
    else:
        agent = SimpleNamespace(model=DomainMockModel())
    return MockWorkflowAdapter(Path(state_root), cfg, gateway, agent), gateway, agent
