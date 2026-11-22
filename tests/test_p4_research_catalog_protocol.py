"""P4.17 research catalog validation: no live BRAIN or model calls."""
import unittest

from alpha_research.orchestration.research_catalog_protocol import (
    RESEARCH_CATALOG_READS, inspect_catalog_read_contract,
    research_catalog_request_allowed,
)
from alpha_research.orchestration.engine import MainOrchestrator


class ResearchCatalogProtocolTests(unittest.TestCase):
    def setUp(self):
        self.scope = {
            "region": "USA", "delay": 1,
            "universe": "TOP3000", "instrument_type": "EQUITY",
        }

    def allowed(self, command, params):
        return research_catalog_request_allowed(command, params, self.scope)

    def test_only_exact_read_actions_with_exact_scope_and_params(self):
        self.assertEqual(len(RESEARCH_CATALOG_READS), 7)
        self.assertTrue(self.allowed("get_platform_setting_options", {}))
        self.assertTrue(self.allowed(
            "get_platform_setting_options", {"include_schema": True}))
        self.assertTrue(self.allowed("get_simulation_settings_schema", {}))
        self.assertTrue(self.allowed("get_alpha_details", {"alpha_id": "alpha-id"}))
        for action in ("readonly_probe_datasets", "get_datasets",
                       "get_operators"):
            self.assertTrue(self.allowed(action, self.scope))
        self.assertTrue(self.allowed(
            "get_datafields", {**self.scope, "dataset_id": "fundamental_v1"}))

    def test_command_mutation_and_scope_smuggling_are_refused(self):
        bad = [
            ("simulate_super", self.scope), ("simulate_batch", self.scope),
            ("resume_simulation_batch", self.scope),
            ("set_case", {"case_key": "x", "run_id": "y"}),
            ("tag_alpha", {"alpha_id": "a"}),
            ("sync_backtest_cache", {}), ("get_dataset_details", {}),
            ("check_simulation_cache", {}),
            ("get_user_alphas", {}), ("unknown_api", {}),
            ("get_datasets", {**self.scope, "grant_id": "abc"}),
            ("get_datasets", {**self.scope, "permission_to_dispatch": False}),
            ("get_datasets", {**self.scope, "region": "CHN"}),
            ("get_datasets", {**self.scope, "universe": "TOP1000"}),
            ("get_datasets", {**self.scope, "delay": True}),
            ("get_datasets", {**self.scope, "instrument_type": "FUTURE"}),
            ("get_datafields", {**self.scope, "dataset_id": "../secret"}),
            ("get_datafields", {**self.scope, "dataset_id": "ds", "offset": 0}),
            ("get_alpha_details", {"alpha_id": "../alpha"}),
            ("get_alpha_details", {"alpha_id": "a", "allow": False}),
            ("get_simulation_settings_schema", {"include_schema": True}),
            ("get_platform_setting_options", {"include_schema": False}),
        ]
        for command, params in bad:
            with self.subTest(command=command, params=params):
                self.assertFalse(self.allowed(command, params))
        self.assertFalse(research_catalog_request_allowed(
            "get_datasets", self.scope,
            {**self.scope, "delay": True}))
        self.assertFalse(research_catalog_request_allowed(
            "get_datasets", self.scope,
            {**self.scope, "region": "../USA"}))

    def test_contract_is_read_only_metadata(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = MainOrchestrator(root)
            task = runner.create("super", {
                "objective": "catalog read contract",
                "region": "USA", "delay": 1, "universe": "TOP3000"})
            before = sorted(str(p) for p in root.rglob("*"))
            report = inspect_catalog_read_contract(runner, task["task_id"])
            self.assertEqual(report["status"],
                             "CATALOG_PROTOCOL_VALIDATED_NO_LIVE_DRIVER")
            self.assertEqual(report["scope"], self.scope)
            self.assertFalse(report["driver_authorized"])
            self.assertFalse(report["permission_to_dispatch"])
            self.assertFalse(report["model_call_authorized"])
            self.assertEqual(before, sorted(str(p) for p in root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
