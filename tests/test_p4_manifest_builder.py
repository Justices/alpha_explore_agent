"""P4.14: Agent owns manifest construction, CLI does not require a JSON path."""
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.manifest_builder import (
    inspect_generated_manifest, read_generated_manifest,
    record_agent_generated_manifest, make_agent_preparation_handoff,
)
from alpha_research.state.fingerprint import fingerprint_json


class AgentManifestBuilderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runner = MainOrchestrator(self.root)
        self.task = self.runner.create(
            "super", {"objective": "offline automatic handoff", "region": "USA",
                      "delay": 1, "universe": "TOP3000"})
        self.id = self.task["task_id"]
        self.candidate = {
            "run_id": "a" * 32,
            "batch_id": "offline_batch",
            "request": {
                "selection": "rank(private_signal_a)",
                "combo": "rank(private_signal_b)",
                "research": {
                    "hypothesis": "offline-only",
                    "rationale": "test of generated candidate",
                    "failure_criterion": "fails offline",
                },
            },
            "settings": {"region": "USA", "delay": 1,
                         "universe": "TOP3000", "instrument_type": "EQUITY"},
        }
        self.path = (self.root / "state" / "research_v1_1" /
                     "generated_simulation_manifests" / (self.id + ".json"))

    def test_missing_internal_manifest_is_safe_waiting_state(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        status = inspect_generated_manifest(self.runner, self.id)
        self.assertEqual(status["status"], "AWAITING_RESEARCH_OUTPUT")
        self.assertFalse(status["permission_to_dispatch"])
        self.assertIsNone(read_generated_manifest(self.runner, self.id))
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_agent_candidate_is_immutable_redacted_and_scope_pinned(self):
        result = record_agent_generated_manifest(self.runner, self.id,
                                                 self.candidate)
        self.assertEqual(result["status"], "GENERATED_LOCAL_NOT_AUTHORIZED")
        self.assertEqual(result["scope"], self.candidate["settings"])
        self.assertFalse(result["permission_to_dispatch"])
        self.assertNotIn("private_signal_a", json.dumps(result))
        manifest = read_generated_manifest(self.runner, self.id)
        self.assertEqual(manifest["research_id"], "super:" + "a" * 32)
        self.assertEqual(manifest["run_id"], "a" * 32)
        self.assertEqual(manifest["request"], self.candidate["request"])
        self.assertEqual(result["manifest_sha256"], fingerprint_json(manifest))
        self.assertEqual(
            record_agent_generated_manifest(self.runner, self.id,
                                            self.candidate), result)
        changed = {**self.candidate, "request": {
            **self.candidate["request"], "selection": "rank(different)"}}
        with self.assertRaisesRegex(OrchestrationError, "already pinned"):
            record_agent_generated_manifest(self.runner, self.id, changed)
        self.assertEqual(read_generated_manifest(self.runner, self.id), manifest)

    def test_agent_manifest_rejects_wrong_scope_missing_fields_and_corruption(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        wrong = {**self.candidate, "settings": {
            **self.candidate["settings"], "region": "CHN"}}
        with self.assertRaisesRegex(OrchestrationError, "not pinned"):
            record_agent_generated_manifest(self.runner, self.id, wrong)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))
        missing = {**self.candidate, "request": {"selection": "rank(x)"}}
        with self.assertRaisesRegex(OrchestrationError, "missing combo"):
            record_agent_generated_manifest(self.runner, self.id, missing)
        record_agent_generated_manifest(self.runner, self.id, self.candidate)
        altered = json.loads(self.path.read_text(encoding="utf-8"))
        altered["manifest"]["settings"]["universe"] = "TOP500"
        self.path.write_text(json.dumps(altered), encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "digest drift"):
            read_generated_manifest(self.runner, self.id)

    def test_two_writers_are_idempotent_for_same_candidate(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(
                lambda _: record_agent_generated_manifest(
                    self.runner, self.id, self.candidate), range(2)))
        self.assertEqual(statuses[0], statuses[1])
        self.assertEqual(len(list(self.path.parent.glob(self.id + ".json"))), 1)

    def test_completed_task_cannot_publish_new_manifest(self):
        task = self.runner.load(self.id)
        task["status"] = "COMPLETED"
        from alpha_research.state.store import atomic_write_json
        atomic_write_json(self.runner._path(self.id), task)
        with self.assertRaisesRegex(OrchestrationError, "fresh"):
            record_agent_generated_manifest(self.runner, self.id, self.candidate)

    def test_super_agent_pipeline_stops_before_post_and_generates_manifest(self):
        from copy import deepcopy
        from alpha_research.config import DEFAULT_CONFIG
        from alpha_research.orchestration.domain_mock import (
            DomainMockGateway, DomainMockModel, mock_domain_boundary,
        )
        from alpha_research.workflows.super_alpha.workflow import run_super_research
        gateway = DomainMockGateway("USA")
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["scope"]["region"] = "USA"
        cfg["scope"]["delay"] = 1
        cfg["scope"]["universe"] = "TOP3000"
        with mock_domain_boundary("USA"):
            result = run_super_research(
                gateway, DomainMockModel(), cfg, self.root,
                region="USA", delay=1, objective="offline Agent candidate",
                preparation_handoff=make_agent_preparation_handoff(
                    self.runner, self.id))
        self.assertEqual(result["status"], "AWAITING_INDEPENDENT_AUTHORIZATION")
        self.assertFalse(result["simulation_invoked"])
        self.assertNotIn("simulate_super", gateway.calls)
        self.assertNotIn("simulate_batch", gateway.calls)
        self.assertNotIn("submit_alpha", gateway.calls)
        self.assertEqual(self.runner.load(self.id)["status"], "CREATED")
        self.assertEqual(self.runner.load(self.id)["budget"]["used"], 0)
        self.assertEqual(inspect_generated_manifest(
            self.runner, self.id)["status"], "GENERATED_LOCAL_NOT_AUTHORIZED")
        self.assertFalse(list(self.root.rglob("*simulation_intent.json")))

    def test_optimization_pipeline_stops_before_post_and_pins_source(self):
        from copy import deepcopy
        from alpha_research.config import DEFAULT_CONFIG
        from alpha_research.orchestration.domain_mock import (
            DomainMockGateway, DomainMockModel, mock_domain_boundary,
        )
        from alpha_research.workflows.optimization.workflow import run_optimization
        task = self.runner.create(
            "optimization", {
                "objective": "offline optimization proposal", "region": "USA",
                "delay": 1, "universe": "TOP3000", "alpha_id": "SOURCE",
            })
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["scope"]["region"] = "USA"
        cfg["scope"]["delay"] = 1
        cfg["scope"]["universe"] = "TOP3000"
        gw = DomainMockGateway("USA", "SOURCE")
        with mock_domain_boundary("USA"):
            result = run_optimization(
                gw, DomainMockModel(), cfg, self.root,
                alpha_id="SOURCE", region="USA",
                objective="offline optimization proposal",
                preparation_handoff=make_agent_preparation_handoff(
                    self.runner, task["task_id"]))
        self.assertEqual(result["status"], "AWAITING_INDEPENDENT_AUTHORIZATION")
        self.assertFalse(result["simulation_invoked"])
        self.assertNotIn("simulate_super", gw.calls)
        self.assertNotIn("simulate_batch", gw.calls)
        self.assertNotIn("submit_alpha", gw.calls)
        self.assertEqual(self.runner.load(task["task_id"])["status"], "CREATED")
        manifest = read_generated_manifest(self.runner, task["task_id"])
        self.assertEqual(manifest["request"]["origin_alpha_id"], "SOURCE")
        self.assertFalse(list(self.root.rglob("*simulation_intent.json")))


class AutoManifestCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.prefix = [
            "--state-root", str(self.root),
            "--lead", "deepseek", "--executor", "deepseek",
        ]

    def call(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("live BRAIN forbidden")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("paid model forbidden")),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            code = main(self.prefix + argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_issuer_handoff_automatically_reads_agent_snapshot(self):
        code, out, err = self.call([
            "orchestration-prepare", "--workflow", "super",
            "--objective", "offline no user manifest", "--region", "USA",
            "--delay", "1", "--universe", "TOP3000"])
        self.assertEqual(code, 0, err)
        task_id = json.loads(out)["task_id"]
        code, out, err = self.call(["orchestration-manifest-status", task_id])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "AWAITING_RESEARCH_OUTPUT")
        before = sorted(str(p) for p in self.root.rglob("*"))
        code, out, err = self.call(["orchestration-scope-issuer-request", task_id])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "AWAITING_RESEARCH_OUTPUT")
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*"))
        from alpha_research.orchestration.manifest_builder import (
            record_agent_generated_manifest,
        )
        record_agent_generated_manifest(
            MainOrchestrator(self.root), task_id, {
                "run_id": "e" * 32, "batch_id": "agent_batch",
                "request": {
                    "selection": "rank(private_signal_a)",
                    "combo": "rank(private_signal_b)",
                },
                "settings": {
                    "region": "USA", "delay": 1, "universe": "TOP3000",
                    "instrument_type": "EQUITY",
                },
            })
        before = sorted(str(p) for p in self.root.rglob("*"))
        code, out, err = self.call(["orchestration-scope-issuer-request", task_id])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"],
                         "AWAITING_INDEPENDENT_SIGNATURE_NOT_AUTHORIZED")
        self.assertEqual(result["fixed_claims"]["research_id"], "super:"+"e"*32)
        self.assertFalse(result["permission_to_dispatch"])
        self.assertNotIn("private_signal_a", out)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))
        code, out, err = self.call(["orchestration-manifest-status", task_id])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "GENERATED_LOCAL_NOT_AUTHORIZED")


if __name__ == "__main__":
    unittest.main()
