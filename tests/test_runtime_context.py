"""P2 CLI runtime context: one held lock, one gateway/agent identity."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.runtime_context import CliRuntimeContext
from alpha_research.state.writer_lock import SingleWriterLock


class RuntimeContextTests(unittest.TestCase):
    def test_active_cli_context_passes_same_references_without_creating_session(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            gateway = object()
            agent = object()
            model = object()
            config = {"scope": {"region": "CHN"}}
            lock = SingleWriterLock(root / "state" / "agent.lock")
            with self.assertRaisesRegex(OrchestrationError, "already-acquired"):
                CliRuntimeContext.from_active_cli(
                    root, config, gateway, model, agent, lock)
            calls = []
            with lock:
                runtime = CliRuntimeContext.from_active_cli(
                    root, config, gateway, model, agent, lock)
                def fake_dispatch(command, args, cfg, state_root, gw, worker,
                                  execution_context=None):
                    calls.append((command, args, cfg, state_root, gw, worker,
                                  execution_context))
                    return {"status": "synthetic"}
                args = SimpleNamespace(objective="offline")
                context = {"task_id": "synthetic"}
                result = runtime.dispatch(
                    "run-campaign", args, dispatcher=fake_dispatch,
                    execution_context=context)
                self.assertEqual(result, {"status": "synthetic"})
                self.assertEqual(len(calls), 1)
                self.assertIs(calls[0][2], config)
                self.assertIs(calls[0][4], gateway)
                self.assertIs(calls[0][5], agent)
                self.assertIs(calls[0][6], context)
                self.assertEqual(calls[0][3], root)
            with self.assertRaisesRegex(OrchestrationError, "already-acquired"):
                CliRuntimeContext.from_active_cli(
                    root, config, gateway, model, agent, lock)

    def test_lock_for_another_state_root_must_not_be_reused(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            wrong = SingleWriterLock(Path(second) / "state" / "agent.lock")
            with wrong:
                with self.assertRaisesRegex(OrchestrationError, "already-acquired"):
                    CliRuntimeContext.from_active_cli(
                        first, {}, object(), None, None, wrong)


if __name__ == "__main__":
    unittest.main()
