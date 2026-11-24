"""Real platform runtime canary path is one expression without tag PATCH."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alpha_research.integrations.brain.runtime import PlatformError, PlatformRuntime


class DummyClient:
    base_url = "https://api.worldquantbrain.com"
    catalog_account = "offline-account"
    def close(self):
        pass


class CanaryPlatformTests(unittest.TestCase):
    def test_runtime_refuses_multi_expression_canary(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = PlatformRuntime(Path(td) / "state", client=DummyClient())
            one = {"name": "a", "expression": "rank(field)",
                   "research": {"idea": "test"}}
            payload = {"batch_id": "20261124_030000_abcd1234",
                       "dataset_id": "analyst10",
                       "settings": {}, "variants": [one, one]}
            with patch.object(runtime, "_submit_regular_batch",
                              side_effect=AssertionError("must not reach dispatcher")):
                with self.assertRaisesRegex(PlatformError, "exactly one"):
                    runtime.run_canary_simulation(payload)
            payload["variants"] = [one]
            with patch.object(runtime, "_submit_regular_batch",
                              return_value={"status": "fake"}) as dispatch:
                self.assertEqual(runtime.run_canary_simulation(payload)["status"], "fake")
                args, kwargs = dispatch.call_args
                self.assertEqual(args[0]["label"], "production")
                self.assertTrue(kwargs["canary"])

    def test_completed_canary_readback_never_writes_tags(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = PlatformRuntime(root / "state", client=DummyClient())
            runtime.set_case("canary_case", "single_case")
            paths = runtime._batch_paths("20261124_030000_abcd1234", "production")
            request = {
                "case_key": "canary_case", "run_id": "single_case",
                "label": "production", "batch_id": "20261124_030000_abcd1234",
                "canary_no_tags": True,
                "variants": [{"name": "canary", "expression": "rank(field)"}],
                "settings": {},
            }
            progress = {"resume_url": "/simulations/fake",
                        "request_sha256": "abc", "receipt_sha256": "def"}
            runtime._poll = Mock(return_value={"alpha": "ALPHA1"})
            runtime._request = Mock(return_value=object())
            runtime._json = Mock(return_value={"id": "ALPHA1", "is": {"sharpe": 1.2}})
            runtime._tag_plan_path = Mock(side_effect=AssertionError("tag plan forbidden"))
            runtime._apply_completed_tags = Mock(
                side_effect=AssertionError("tag PATCH forbidden"))
            runtime._record_platform_backtest = Mock()
            result = runtime._finish(request, paths, progress)
            self.assertEqual(result["results"][0]["alpha_id"], "ALPHA1")
            self.assertEqual(result["runner"]["status"], "COMPLETE")
            self.assertNotIn("research_tags", result["results"][0])
            runtime._tag_plan_path.assert_not_called()
            runtime._apply_completed_tags.assert_not_called()
            runtime._record_platform_backtest.assert_called_once()
            runtime._request.assert_called_once_with("GET", "/alphas/ALPHA1")


if __name__ == "__main__":
    unittest.main()
