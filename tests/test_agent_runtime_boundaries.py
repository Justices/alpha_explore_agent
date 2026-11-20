"""Offline regression tests for role routing and shared admission primitives.

All providers are synthetic: no SDK, CLI subprocess or BRAIN requests.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.agents.antigravity import (
    AntigravityAdapterError, decode_json_object,
)
from alpha_research.agents.configuration import AgentConfigError, RoleRoutingConfig
from alpha_research.agents.registry import bundled_registry_path, load_registry
from alpha_research.agents.router import RoleRoutedModelClient
from alpha_research.integrations.model import DecisionRecorder
from alpha_research.integrations.model_json import decode_decision
from alpha_research.orchestration.model_evidence_gateway import (
    ModelGatewayPermissionError, _digest,
)
from alpha_research.orchestration.recovery import _canonical_sha256
from alpha_research.state.fingerprint import fingerprint_json


def routes():
    return {
        "provider": "role_router",
        "default_role": "research_lead",
        "task_roles": {"sample_task": "research_executor"},
        "roles": {
            "research_lead": {"backend": "offline_backend"},
            "research_executor": {"backend": "offline_backend"},
        },
        "backends": {"offline_backend": {
            "provider": "mock",
            "responses": {"sample_task": {"result": "original"}},
        }},
    }


class AgentRuntimeBoundaryTests(unittest.TestCase):
    def test_resolved_nested_backend_state_cannot_mutate_router(self):
        router = RoleRoutedModelClient(routes())
        first = router.route_for("sample_task")
        first.backend["responses"]["sample_task"]["result"] = "injected"
        first.backend["responses"]["sample_task"]["extra"] = True
        second = router.route_for("sample_task")
        self.assertEqual(second.backend["responses"]["sample_task"],
                         {"result": "original"})
        self.assertEqual(router.complete_json("sample_task", {}),
                         {"result": "original"})

    def test_recorder_uses_one_metadata_resolution_per_decision(self):
        class CountingRoutes:
            def __init__(self, routing):
                self.inner = routing
                self.resolutions = 0

            def resolve(self, task):
                self.resolutions += 1
                return self.inner.resolve(task)

        router = RoleRoutedModelClient(routes())
        wrapper = CountingRoutes(router.routing)
        router.routing = wrapper
        with tempfile.TemporaryDirectory() as directory:
            recorder = DecisionRecorder(router, Path(directory))
            recorder.begin_session(research_id="R1")
            result = recorder.complete_json("sample_task", {"round_index": 0})
            self.assertEqual(result, {"result": "original"})
            self.assertEqual(wrapper.resolutions, 2)
            record_path, = (Path(directory) / "logs" / "model_calls").glob("*.record.json")
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["agent_role"], "research_executor")
            self.assertEqual(record["agent_backend"], "offline_backend")
            self.assertTrue(record["route_sha256"])

    def test_invalid_followup_route_clears_previous_backend_identity(self):
        router = RoleRoutedModelClient(routes())
        router.complete_json("sample_task", {})
        self.assertEqual(router.last_model, "offline-mock")
        with self.assertRaisesRegex(AgentConfigError, "model task"):
            router.complete_json("", {})
        self.assertIsNone(router.last_model)
        self.assertIsNone(router.last_usage)
        self.assertIsNone(router.last_thread_id)

    def test_registry_admission_never_builds_dummy_router(self):
        with patch("alpha_research.agents.registry.RoleRoutingConfig.parse",
                   side_effect=AssertionError("registry must not construct a router")):
            registry = load_registry(bundled_registry_path())
        self.assertIn("deepseek", registry["entries"])
        self.assertIn("codex", registry["entries"])

    def test_antigravity_reuses_canonical_decoder_and_rejects_bad_payloads(self):
        for raw in (
            '{"alpha":1}',
            'notes ```json\n{"alpha":1}\n```',
        ):
            with self.subTest(raw=raw):
                self.assertEqual(decode_json_object(raw), decode_decision(raw))
        for invalid in (
            None, ["not a string"], "not json",
            '```json\n{"a":1}\n``` and ```json\n{"b":2}\n```',
            '[1, 2]',
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AntigravityAdapterError):
                    decode_json_object(invalid)

    def test_recovery_and_readonly_evidence_use_canonical_fingerprint(self):
        document = {
            "beta": [1, "中", {"unit": None, "pass": True}],
            "alpha": {"z": 0, "a": "catalog"},
        }
        baseline = fingerprint_json(document)
        self.assertEqual(_canonical_sha256(document), baseline)
        self.assertEqual(_digest(document), baseline)
        self.assertEqual(_digest(dict(reversed(list(document.items())))), baseline)
        with self.assertRaisesRegex(ModelGatewayPermissionError,
                                    "unserializable BRAIN evidence"):
            _digest({"nonfinite": float("nan")})


if __name__ == "__main__":
    unittest.main()
