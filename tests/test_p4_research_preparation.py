"""P4.15: single-command offline Agent planning with a non-executable journal."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.orchestration.manifest_builder import (
    inspect_generated_manifest, read_generated_manifest,
)
from alpha_research.orchestration.research_preparation import (
    inspect_research_preparation,
)


class ResearchPreparationCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.prefix = [
            "--state-root", str(self.root),
            "--lead", "deepseek", "--executor", "deepseek",
        ]
        self.runner = MainOrchestrator(self.root)

    def call(self, command):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("live BRAIN forbidden")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("paid model forbidden")),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            result = main(self.prefix + command)
        return result, stdout.getvalue(), stderr.getvalue()

    def prepare(self, workflow):
        args = [
            "orchestration-prepare", "--workflow", workflow,
            "--objective", "single-phase synthetic candidate preparation",
            "--region", "USA", "--delay", "1", "--universe", "TOP3000",
        ]
        if workflow == "optimization":
            args += ["--alpha-id", "SOURCE"]
        code, out, error = self.call(args)
        self.assertEqual(code, 0, error)
        return json.loads(out)["task_id"]

    def test_super_and_optimization_are_automatically_prepared_without_post(self):
        for workflow in ("super", "optimization"):
            with self.subTest(workflow=workflow):
                task_id = self.prepare(workflow)
                before = sorted(str(p) for p in self.root.rglob("*"))
                status, out, error = self.call([
                    "orchestration-research-prepare-status", task_id])
                self.assertEqual(status, 0, error)
                self.assertEqual(
                    json.loads(out)["status"], "AWAITING_RESEARCH_PREPARATION")
                self.assertEqual(
                    sorted(str(p) for p in self.root.rglob("*")), before)
                code, out, error = self.call([
                    "orchestration-research-prepare", task_id,
                    "--mode", "mock"])
                self.assertEqual(code, 0, error)
                report = json.loads(out)
                self.assertEqual(report["status"], "RESEARCH_PREPARED_OFFLINE")
                self.assertEqual(report["mode"], "offline_domain_mock")
                self.assertFalse(report["brain_invoked"])
                self.assertFalse(report["provider_invoked"])
                self.assertFalse(report["permission_to_dispatch"])
                self.assertFalse(report["brain_simulation_authorized"])
                self.assertNotIn("signal_a", out)
                self.assertNotIn("signal_b", out)
                task = self.runner.load(task_id)
                self.assertEqual(task["status"], "CREATED")
                self.assertEqual(task["budget"], {
                    "limit": 1, "used": 0, "reserved": 0})
                self.assertIsNone(task["recovery_target"])
                self.assertIsNone(task.get("research_identity"))
                self.assertEqual(task["checkpoints"], {})
                self.assertEqual(
                    inspect_generated_manifest(self.runner, task_id)["status"],
                    "GENERATED_LOCAL_NOT_AUTHORIZED")
                manifest = read_generated_manifest(self.runner, task_id)
                self.assertEqual(manifest["settings"]["region"], "USA")
                self.assertEqual(manifest["settings"]["universe"], "TOP3000")
                self.assertEqual(manifest["settings"]["instrument_type"], "EQUITY")
                self.assertEqual(
                    manifest["research_id"], workflow + ":" + manifest["run_id"])
                self.assertFalse(list(self.root.rglob("*simulation_intent.json")))
                before_repeat = sorted(str(p) for p in self.root.rglob("*"))
                code, replay, error = self.call([
                    "orchestration-research-prepare", task_id,
                    "--mode", "mock"])
                self.assertEqual(code, 0, error)
                self.assertEqual(json.loads(replay), report)
                self.assertEqual(
                    sorted(str(p) for p in self.root.rglob("*")), before_repeat)
                if workflow == "super":
                    code, _, error = self.call([
                        "orchestration-scope-issuer-request", task_id])
                    self.assertEqual(code, 2)
                    self.assertIn("OrchestrationError", error)

    def test_interrupted_mock_preparation_blocks_replay_without_simulation(self):
        task_id = self.prepare("super")
        with patch(
            "alpha_research.workflows.super_alpha.workflow.run_super_research",
            side_effect=RuntimeError("synthetic interrupted planning"),
        ):
            code, out, error = self.call([
                "orchestration-research-prepare", task_id, "--mode", "mock"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("RuntimeError", error)
        self.assertNotIn("synthetic interrupted planning", error)
        report = inspect_research_preparation(self.runner, task_id)
        self.assertEqual(report["status"], "BLOCKED_MANUAL_REVIEW")
        self.assertEqual(report["failure_type"], "RuntimeError")
        self.assertFalse(report["automatic_retry_allowed"])
        self.assertIsNone(read_generated_manifest(self.runner, task_id))
        task = self.runner.load(task_id)
        self.assertEqual(task["status"], "CREATED")
        self.assertEqual(task["budget"]["used"], 0)
        code, out, error = self.call([
            "orchestration-research-prepare", task_id, "--mode", "mock"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertFalse(list(self.root.rglob("*simulation_intent.json")))

    def test_real_platform_read_evidence_root_cannot_be_claimed_as_mock(self):
        task_id = self.prepare("super")
        evidence = (self.root / "state" / "research_v1_1" /
                    "platform_read_evidence")
        evidence.mkdir(parents=True)
        (evidence / "only-real-read.json").write_text(
            '{"status":"LOCAL_READ_OBSERVED_NOT_ATTESTED"}',
            encoding="utf-8")
        code, out, error = self.call([
            "orchestration-research-prepare", task_id, "--mode", "mock"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("OrchestrationError", error)
        self.assertFalse(
            (self.root / "state" / "research_v1_1" /
             "domain_mock_workspace.json").exists())
        self.assertFalse(
            (self.root / "state" / "research_v1_1" /
             "research_preparation").exists())

    def test_stop_flag_refuses_new_research_preparation(self):
        task_id = self.prepare("super")
        flag = self.root / "state" / "stop.flag"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("STOPPED", encoding="utf-8")
        code, out, error = self.call([
            "orchestration-research-prepare", task_id, "--mode", "mock"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertFalse(
            (self.root / "state" / "research_v1_1" /
             "research_preparation").exists())
        self.assertFalse(
            (self.root / "state" / "research_v1_1" /
             "domain_mock_workspace.json").exists())


if __name__ == "__main__":
    unittest.main()
