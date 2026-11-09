"""Real Super/Optimization executor paths with strictly offline scripted gateways.

Mocks replace the platform transport, model completion and candidate qualification
boundary. The real workflow code persists its preregistration, simulation intent,
readback and research records; no real BRAIN endpoint can be reached.
"""
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from nastu_quant.config import DEFAULT_CONFIG
from nastu_quant.research.orchestrator import MainOrchestrator, OrchestrationError
from nastu_quant.research.workflow_adapter import MockWorkflowAdapter


class ScriptedGateway:
    is_offline_mock = True

    def __init__(self, fail_simulation=False):
        self.calls = []
        self.fail_simulation = fail_simulation
        self.source = {
            "id": "SOURCE", "type": "SUPER",
            "settings": {"instrument_type": "EQUITY", "region": "GLB",
                         "delay": 1, "universe": "TOP3000"},
            "selection": "rank(signal_a)", "combo": "rank(signal_b)",
        }

    def call(self, command, **kwargs):
        self.calls.append(command)
        if command == "set_case":
            return {}
        if command == "get_platform_setting_options":
            return {"instrument_options": [
                {"InstrumentType": "EQUITY", "Region": "GLB", "Delay": 1,
                 "Universe": ["TOP3000"]}]}
        if command == "get_simulation_settings_schema":
            return {"selectionHandling": {"type": "choice", "choices": [
                {"value": "POSITIVE"}]}}
        if command == "get_user_alphas":
            return {"results": [{"id": "SUBMITTED", "status": "ACTIVE",
                                 "type": "REGULAR",
                                 "settings": {"region": "GLB", "delay": 1}}],
                    "count": 1}
        if command == "get_operators":
            return [{"name": "rank", "scope": ["SELECTION", "COMBO"]}]
        if command == "get_alpha_details" and kwargs.get("alpha_id") == "SOURCE":
            return deepcopy(self.source)
        if command == "simulate_super":
            if self.fail_simulation:
                raise RuntimeError("injected lost simulation response")
            return {"results": [{"alpha_id": "MOCK_ALPHA",
                                 "details": {"id": "MOCK_ALPHA",
                                             "is": {"sharpe": 1.4}}}],
                    "runner": {"mock": True}}
        raise AssertionError("unexpected offline gateway call: " + command)


class ScriptedRecorder:
    def __init__(self, model, state_root):
        self.model = model

    def begin_session(self):
        pass

    def complete_json(self, task, context, **kwargs):
        proposal = {
            "selection": "rank(signal_a)", "combo": "rank(signal_b)",
            "hypothesis": "test hypothesis", "rationale": "test rationale",
            "failure_criterion": "test failure",
        }
        if task == "construct_super":
            proposal["settings"] = {
                "region": "GLB", "delay": 1, "universe": "TOP3000"}
        elif task == "optimize_alpha":
            proposal["changes"] = ["change combo expression"]
        else:
            raise AssertionError(task)
        return proposal


class FakeKnowledge:
    def __init__(self, *args, **kwargs):
        pass

    def build_pack(self, task, context):
        return {"task": task, "offline": True}


class FullMockWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = deepcopy(DEFAULT_CONFIG)
        self.cfg["scope"]["region"] = "GLB"
        self.gateway = ScriptedGateway()

    def _run(self, workflow, gateway=None):
        gateway = gateway or self.gateway
        adapter = MockWorkflowAdapter(
            self.root, self.cfg, gateway,
            SimpleNamespace(model=SimpleNamespace(is_offline_mock=True)))
        original_run = adapter.run
        self.last_error = None
        def traced_run(*args, **kwargs):
            try:
                return original_run(*args, **kwargs)
            except Exception as exc:
                self.last_error = repr(exc)
                raise
        adapter.run = traced_run
        runner = MainOrchestrator(self.root, adapter=adapter)
        request = {"objective": "mock-only integration", "region": "GLB",
                   "delay": 1}
        if workflow == "optimization":
            request["alpha_id"] = "SOURCE"
        task = runner.create(workflow, request)
        patches = [
            patch("nastu_quant.research.super.research.KnowledgeService", FakeKnowledge),
            patch("nastu_quant.research.super.research.DecisionRecorder", ScriptedRecorder),
            patch("nastu_quant.research.optimization.research.KnowledgeService", FakeKnowledge),
            patch("nastu_quant.research.optimization.research.DecisionRecorder", ScriptedRecorder),
        ]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = runner.execute(task["task_id"])
        return runner, task, result, gateway

    def test_super_executor_persists_real_intent_and_readback(self):
        runner, task, result, gateway = self._run("super")
        self.assertEqual(result["status"], "COMPLETED", (result.get("failure"), self.last_error))
        self.assertEqual(gateway.calls.count("simulate_super"), 1)
        self.assertEqual(result["budget"]["used"], 1)
        self.assertEqual(result["result"]["qualification"]["candidate_status"],
                         "EVIDENCE_PENDING")
        intent_paths = list(self.root.rglob("*simulation_intent.json"))
        self.assertEqual(len(intent_paths), 1)
        intent = json.loads(intent_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(intent["workflow"], "super")
        self.assertNotIn("submit_alpha", gateway.calls)
        self.assertEqual(runner.resume(task["task_id"]), result)
        self.assertEqual(gateway.calls.count("simulate_super"), 1)

    def test_optimization_executor_preserves_source_and_intent(self):
        runner, task, result, gateway = self._run("optimization")
        self.assertEqual(result["status"], "COMPLETED", (result.get("failure"), self.last_error))
        self.assertEqual(gateway.calls.count("simulate_super"), 1)
        self.assertEqual(result["checkpoints"]["execute"]["source_alpha_id"], "SOURCE")
        self.assertTrue(result["checkpoints"]["execute"]["source_unchanged_verified"])
        self.assertEqual(len(list(self.root.rglob("*simulation_intent.json"))), 1)
        self.assertNotIn("submit_alpha", gateway.calls)
        self.assertEqual(runner.resume(task["task_id"]), result)
        self.assertEqual(gateway.calls.count("simulate_super"), 1)

    def test_lost_super_response_blocks_and_never_replays(self):
        gateway = ScriptedGateway(fail_simulation=True)
        runner, task, result, gateway = self._run("super", gateway)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["steps"]["execute"], "RUNNING")
        self.assertEqual(result["budget"]["reserved"], 1)
        self.assertEqual(gateway.calls.count("simulate_super"), 1)
        with self.assertRaises(OrchestrationError):
            runner.resume(task["task_id"])
        self.assertEqual(gateway.calls.count("simulate_super"), 1)
        self.assertNotIn("submit_alpha", gateway.calls)


if __name__ == "__main__":
    unittest.main()
