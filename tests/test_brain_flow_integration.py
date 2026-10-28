import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.agent import ResearchAgent
from nastu_quant.brain_flow_runtime import BrainFlowRuntime


class TestBrainFlowRecovery(unittest.TestCase):
    def test_resumed_runner_results_are_saved_in_agent_result_format(self):
        runner = {"case_key": "case", "run_id": "run", "batch_id": "batch",
                  "label": "production"}
        metadata = {"case_key": "case", "run_id": "run", "output": "result.json"}
        row = {"name": "variant_1", "expression": "rank(field)", "success": True,
               "alpha_id": "A1", "is": {"sharpe": 1.8}}

        class Gateway:
            def call(self, command, **params):
                if command != "resume_simulation_batch" or params != runner:
                    raise AssertionError("unexpected recovery request")
                return {"results": [row], "runner": metadata}

        agent = ResearchAgent.__new__(ResearchAgent)
        agent.gateway = Gateway()
        with tempfile.TemporaryDirectory() as tmp:
            batch_dir = Path(tmp)
            raw_path = batch_dir / "simulation_raw.json"
            recovered = agent._resume_canonical_batch(
                {"runner": runner}, batch_dir, raw_path, "production")

            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["alpha_results"][0]["alpha_id"], "A1")
            self.assertEqual(recovered["alpha_results"][0]["details"]["is"], row["is"])
            self.assertEqual(recovered["runner"], metadata)
            self.assertEqual(json.loads(raw_path.read_text(encoding="utf-8")), recovered)
            status = json.loads((batch_dir / "production_runner_recovery.json").read_text(
                encoding="utf-8"))
            self.assertEqual(status["status"], "RESUMED")


class TestBrainFlowSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "scripts").mkdir()
        self.runtime = BrainFlowRuntime(self.home, sys.executable)
        self.addCleanup(self.runtime.close)
        self.runtime.set_case("case", "run")
        self.runtime._execute_batch = lambda request, paths: json.loads(
            paths["input"].read_text(encoding="utf-8"))
        self.settings = {
            "instrument_type": "EQUITY", "region": "GLB", "universe": "TOP3000",
            "delay": 1, "decay": 4, "neutralization": "MARKET",
            "truncation": 0.08, "pasteurization": "ON", "unit_handling": "VERIFY",
            "nan_handling": "OFF", "max_trade": "OFF", "language": "FASTEXPR",
            "visualization": False, "test_period": None,
        }

    def _run(self):
        return self.runtime.run_simulation_batch({
            "batch_id": "20261028_120000_abcdef12", "label": "production",
            "dataset_id": "analyst69", "settings": self.settings,
            "variants": [{"name": "variant_1", "expression": "rank(field_a)"},
                         {"name": "variant_2", "expression": "rank(field_b)"}],
        })

    def test_test_period_is_forwarded_in_every_runner_variant(self):
        self.settings["test_period"] = "P0Y0M"
        rows = self._run()
        self.assertEqual([row["settings"]["testPeriod"] for row in rows],
                         ["P0Y0M", "P0Y0M"])

    def test_pasteurization_override_is_forwarded_to_runner(self):
        self.settings["pasteurization"] = "OFF"
        rows = self._run()
        self.assertEqual([row["settings"]["pasteurization"] for row in rows],
                         ["OFF", "OFF"])


if __name__ == "__main__":
    unittest.main()
