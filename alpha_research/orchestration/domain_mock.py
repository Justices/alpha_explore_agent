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


class DomainMockGateway:
    is_offline_mock = True

    def __init__(self, region, source_alpha_id=None, *, lost_response=False):
        self.region = region
        self.source_alpha_id = source_alpha_id
        self.lost_response = lost_response
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
            return [{"id": "OFFLINE_DATASET", "category": "OTHER",
                     "region": self.region, "delay": 1, "universe": "TOP3000",
                     "description": "offline mock dataset"}]
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
    """Scripted batch, executed by the real Campaign scheduler (not ResearchAgent)."""
    def __init__(self, state_root, *, lost_response=False):
        self.runs_root = Path(state_root) / "runs"
        self.stop_flag = Path(state_root) / "state" / "stop.flag"
        self.lost_response = lost_response
        self.calls = 0

    def run_once(self, objective, *, dataset_id, **kwargs):
        self.calls += 1
        if self.calls > 1:
            raise OrchestrationError("offline Regular attempted additional batch")
        scope = kwargs["fixed_scope"]
        batch = self.runs_root / scope["region"] / dataset_id / "offline_mock_batch"
        batch.mkdir(parents=True, exist_ok=True)
        intent = batch / "simulation_intent.json"
        intent.write_text('{"workflow":"regular","mode":"offline_domain_mock"}',
                          encoding="utf-8")
        context = kwargs.get("execution_context")
        if context is not None:
            context["bind_recovery_target"]("regular_batch", batch)
        if self.lost_response:
            raise RuntimeError("injected lost Regular mock batch response")
        return {
            "batch_dir": str(batch),
            "alpha_ids": ["OFFLINE_REGULAR_ALPHA"],
            "candidate_status": {"OFFLINE_REGULAR_ALPHA": "EVIDENCE_PENDING"},
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
            raise OrchestrationError("unsupported offline model decision task")
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
            return result

    with ExitStack() as stack:
        # Campaign review is a model-mediated attribution step; replace it in
        # this standalone offline integration without suppressing scheduling.
        stack.enter_context(patch(
            "alpha_research.workflows.regular.campaign.review_dataset",
            return_value={"status": "no_selection", "mode": "offline_domain_mock"}))
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
        lost_response=fault == "lost-simulation-response")
    if task["workflow"] == "regular":
        agent = RegularMockAgent(
            state_root, lost_response=fault == "lost-simulation-response")
        # MockWorkflowAdapter needs a fake model marker even for Campaign-only mode.
        agent.model = DomainMockModel()
    else:
        agent = SimpleNamespace(model=DomainMockModel())
    return MockWorkflowAdapter(Path(state_root), cfg, gateway, agent), gateway, agent
