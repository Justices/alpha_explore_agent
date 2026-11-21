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
        for module in ("alpha_research.workflows.super_alpha.workflow",
                       "alpha_research.workflows.optimization.workflow"):
            stack.enter_context(patch(module + ".KnowledgeService", DomainMockKnowledge))
            stack.enter_context(patch(module + ".DecisionRecorder", RegionRecorder))
        yield


def build_domain_mock_adapter(state_root, task, *, fault=None):
    if task["workflow"] not in ("super", "optimization"):
        raise OrchestrationError(
            "domain mock currently supports Super and Optimization only")
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
    agent = SimpleNamespace(model=DomainMockModel())
    return MockWorkflowAdapter(Path(state_root), cfg, gateway, agent), gateway
