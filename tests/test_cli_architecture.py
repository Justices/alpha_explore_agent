"""Split CLI adapters must preserve command grammar and fail-closed defaults."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.cli_parser import build_parser
from alpha_research.cli_orchestration import ORCHESTRATION_COMMANDS


class CliBoundaryTests(unittest.TestCase):
    def test_grammar_declares_offline_and_production_entrypoints(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as root:
            for argv, command in (
                (["--state-root", root, "status"], "status"),
                (["--state-root", root, "orchestration-status", "0" * 32],
                 "orchestration-status"),
                (["--state-root", root, "brain-readonly-check", "--region",
                  "USA", "--delay", "1", "--universe", "TOP3000"],
                 "brain-readonly-check"),
            ):
                with self.subTest(command=command):
                    parsed = parser.parse_args(argv)
                    self.assertEqual(parsed.command, command)
            self.assertIn("orchestration-release-gate", ORCHESTRATION_COMMANDS)
            self.assertNotIn("brain-readonly-check", ORCHESTRATION_COMMANDS)
            self.assertNotIn("run-super", ORCHESTRATION_COMMANDS)

    def test_status_adapter_requires_no_credentials_or_external_calls(self):
        with tempfile.TemporaryDirectory() as location:
            output, error = io.StringIO(), io.StringIO()
            with (patch("alpha_research.cli.Gateway",
                        side_effect=AssertionError("BRAIN access denied")),
                  patch("alpha_research.cli.build_model_client",
                        side_effect=AssertionError("paid model denied")),
                  redirect_stdout(output), redirect_stderr(error)):
                code = main(["--state-root", location, "status"])
            self.assertEqual(code, 0, error.getvalue())
            data = json.loads(output.getvalue())
            self.assertIn("global_alpha_pool", data)
            self.assertIn("unfinished_batches", data)
            self.assertIn("campaigns", data)

    def test_probe_and_knowledge_commands_are_not_implicit_live(self):
        from alpha_research.cli_probe import PROBE_COMMANDS
        from alpha_research.cli_knowledge import KNOWLEDGE_COMMANDS
        self.assertIn("brain-readonly-check", PROBE_COMMANDS)
        self.assertNotIn("tag-alpha", PROBE_COMMANDS)
        self.assertNotIn("orchestration-run", KNOWLEDGE_COMMANDS)
        with tempfile.TemporaryDirectory() as location:
            out, err = io.StringIO(), io.StringIO()
            with (patch("alpha_research.cli.Gateway",
                        side_effect=AssertionError("no BRAIN before explicit opt-in")),
                  redirect_stdout(out), redirect_stderr(err)):
                result = main(["--state-root", location,
                               "brain-readonly-check", "--region", "USA",
                               "--delay", "1", "--universe", "TOP3000"])
            self.assertEqual(result, 2)
            self.assertIn("--allow-brain-read", err.getvalue())
            self.assertEqual(out.getvalue(), "")

    def test_orchestration_requires_roles_not_live_transport(self):
        with tempfile.TemporaryDirectory() as location:
            output, error = io.StringIO(), io.StringIO()
            with (patch("alpha_research.cli.Gateway",
                        side_effect=AssertionError("BRAIN access denied")),
                  patch("alpha_research.cli.build_model_client",
                        side_effect=AssertionError("paid model denied")),
                  redirect_stdout(output), redirect_stderr(error)):
                code = main(["--state-root", location,
                             "orchestration-release-gate", "f" * 32])
            self.assertEqual(code, 2)
            self.assertIn("orchestration preparation failed: OrchestrationError", error.getvalue())
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
