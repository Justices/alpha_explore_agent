"""P2.1 strict shared model-configuration assembly, offline only."""
import copy
import unittest

from alpha_research.agents.registry import bundled_registry_path
from alpha_research.orchestration.runtime_config import prepare_registered_model_runtime

POLICY = {"max_calls": 5, "max_reported_tokens": 1000, "failover": "manual"}
ROLES = {"research_lead": "deepseek", "research_executor": "deepseek"}


class RuntimeConfigTests(unittest.TestCase):
    def test_strict_assembly_does_not_mutate_legacy_config(self):
        config = {"model": {"provider": "obsolete"}, "model_governance": POLICY}
        prior = copy.deepcopy(config)
        result = prepare_registered_model_runtime(config, ROLES, bundled_registry_path())
        self.assertEqual(config, prior)
        self.assertEqual(result["model"]["provider"], "role_router")
        self.assertEqual(result["model_governance"], POLICY)
        self.assertEqual(set(result["model"]["roles"]), set(ROLES))
        self.assertNotIn("obsolete", str(result))

    def test_missing_role_or_governance_is_blocked(self):
        for selected in ({}, {"research_lead": "deepseek"},
                         {"research_executor": "deepseek"}):
            with self.subTest(selected=selected):
                with self.assertRaises(ValueError):
                    prepare_registered_model_runtime(
                        {"model_governance": POLICY}, selected, bundled_registry_path())
        with self.assertRaises(ValueError):
            prepare_registered_model_runtime(
                {}, ROLES, bundled_registry_path())

    def test_mock_backend_must_not_be_used_for_production_profile(self):
        with self.assertRaises(ValueError):
            prepare_registered_model_runtime(
                {"model_governance": POLICY},
                {"research_lead": "mock", "research_executor": "mock"},
                bundled_registry_path())


if __name__ == "__main__":
    unittest.main()
