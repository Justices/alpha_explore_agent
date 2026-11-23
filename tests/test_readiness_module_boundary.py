"""Readiness modules preserve original contracts and cannot authorize IO."""
import tempfile
import unittest
from pathlib import Path

from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.orchestration.research_provider_audit import (
    inspect_p6_provider_plan, inspect_p6_provider_metering,
    inspect_p6_signed_provider_readback,
)
from alpha_research.orchestration.research_brain_read_audit import (
    inspect_p6_brain_catalog_frame,
)
from alpha_research.orchestration.research_readiness_matrix import (
    inspect_p6_integrated_release,
)
from alpha_research.orchestration.research_canary_rehearsal import (
    run_p6_offline_canary,
)
from alpha_research.orchestration import research_p6_readiness as previous


class ResearchReadinessBoundaryTests(unittest.TestCase):
    def test_legacy_functions_point_to_single_implementation(self):
        for method in (
            inspect_p6_provider_plan, inspect_p6_provider_metering,
            inspect_p6_signed_provider_readback,
            inspect_p6_brain_catalog_frame, inspect_p6_integrated_release,
            run_p6_offline_canary,
        ):
            self.assertIs(getattr(previous, method.__name__), method)

    def test_release_is_negative_only_and_never_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orchestrator = MainOrchestrator(root)
            task_id = orchestrator.create("super", {
                "objective": "offline readiness boundary",
                "region": "USA", "delay": 1, "universe": "TOP3000",
            })["task_id"]
            before = sorted(root.rglob("*"))
            report = inspect_p6_integrated_release(orchestrator, task_id)
            self.assertEqual(report["status"], "P6_PRODUCTION_BLOCKED")
            self.assertEqual(report["audit_mode"], "READ_ONLY")
            for flag in ("production_authorized", "permission_to_dispatch",
                         "model_call_authorized", "brain_read_authorized",
                         "brain_simulation_authorized",
                         "alpha_submission_authorized",
                         "automatic_retry_allowed"):
                self.assertIs(report[flag], False)
            self.assertEqual(before, sorted(root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
