"""Offline ModelCallLedger concurrency, OS-lock, and crash-gap contracts."""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from alpha_research.agents.governance import ModelBudgetError, ModelCallLedger
from alpha_research.state.writer_lock import SingleWriterLock


POLICY = {"max_calls": 4, "max_reported_tokens": 1000, "failover": "manual"}


class BudgetConcurrencyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def ledger(self, policy=None):
        return ModelCallLedger(self.root, policy or POLICY)

    def test_reservation_waits_for_short_ledger_transaction(self):
        ledger = self.ledger()
        _, lock = ledger._paths("R1")
        entered, completed = threading.Event(), threading.Event()

        def reserve():
            entered.set()
            ledger.reserve("R1", "C1", task="task")
            completed.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with SingleWriterLock(lock):
                future = pool.submit(reserve)
                self.assertTrue(entered.wait(5))
                time.sleep(0.04)
                self.assertFalse(completed.is_set())
            future.result(timeout=6)
        self.assertEqual(ledger.status("R1")["unresolved"], 1)

    def test_two_contenders_never_reserve_beyond_single_attempt_limit(self):
        ledger = self.ledger({**POLICY, "max_calls": 1})
        ready = threading.Barrier(2, timeout=6)

        def reserve(call_id):
            ready.wait()
            try:
                ledger.reserve("R1", call_id, task="task")
                return "reserved"
            except ModelBudgetError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reserve, cid) for cid in ("C1", "C2")]
            self.assertEqual(sorted(f.result(timeout=8) for f in futures),
                             ["rejected", "reserved"])
        state = ledger.status("R1")
        self.assertEqual((state["attempts"], state["unresolved"]), (1, 1))

    def test_settlement_and_status_wait_for_shared_ledger_file(self):
        ledger = self.ledger()
        ledger.reserve("R1", "C1", task="task")
        _, lock = ledger._paths("R1")
        ready = threading.Event()

        def settle():
            ready.set()
            return ledger.settle("R1", "C1", success=True,
                                 token_in=6, token_out=4)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with SingleWriterLock(lock):
                future = pool.submit(settle)
                self.assertTrue(ready.wait(5))
                time.sleep(0.04)
                self.assertFalse(future.done())
            summary = future.result(timeout=6)
        self.assertEqual((summary["successful"], summary["reported_tokens"]),
                         (1, 10))
        self.assertEqual(ledger.status("R1")["unresolved"], 0)

    def test_unrelated_research_remains_live_when_other_ledger_busy(self):
        ledger = self.ledger()
        _, lock = ledger._paths("R1")
        with SingleWriterLock(lock):
            ledger.reserve("R2", "C1", task="task")
            ledger.settle("R2", "C1", success=True, token_in=1, token_out=2)
        self.assertEqual(ledger.status("R2")["reported_tokens"], 3)
        self.assertEqual(ledger.status("R1")["attempts"], 0)

    def test_ledger_lock_timeout_preserves_state_without_new_reservation(self):
        ledger = self.ledger()
        _, lock = ledger._paths("R1")
        with SingleWriterLock(lock):
            # The timeout is deliberately short in this isolated test. It
            # proves that lock competition never turns into an unbounded wait.
            with patch("alpha_research.agents.governance.time.monotonic",
                       side_effect=[0.0, 3.0]):
                with self.assertRaisesRegex(ModelBudgetError, "writer remained busy"):
                    ledger.reserve("R1", "C1", task="task")
        self.assertEqual(ledger.status("R1")["attempts"], 0)

    def test_invalid_identity_cannot_poison_durable_ledger(self):
        ledger = self.ledger()
        invalid = [
            {"role": {"unexpected": "object"}},
            {"role": "  "},
            {"backend": ["bad"]},
            {"backend": ""},
            {"backend_sha256": "z" * 64},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ModelBudgetError, "invalid model"):
                    ledger.reserve("R1", "C1", task="task", **fields)
        self.assertEqual(ledger.status("R1")["attempts"], 0)
        ledger.reserve("R1", "C1", task="task", role="lead",
                       backend="offline", backend_sha256="a" * 64)
        self.assertEqual(ledger.status("R1")["unresolved"], 1)

    def test_hard_interrupt_before_atomic_budget_save_does_not_count_attempt(self):
        ledger = self.ledger()
        ledger.reserve("R1", "C1", task="task")
        ledger.settle("R1", "C1", success=True, token_in=1, token_out=2)
        path, _ = ledger._paths("R1")
        previous = path.read_bytes()
        with patch.object(ledger, "_write",
                          side_effect=KeyboardInterrupt("synthetic crash")):
            with self.assertRaisesRegex(KeyboardInterrupt, "synthetic crash"):
                ledger.reserve("R1", "C2", task="task")
        self.assertEqual(path.read_bytes(), previous)
        restarted = self.ledger()
        self.assertEqual(restarted.status("R1")["attempts"], 1)
        restarted.reserve("R1", "C2", task="task")
        self.assertEqual(restarted.status("R1")["unresolved"], 1)

    def test_reconcile_is_idempotent_under_concurrent_contenders(self):
        ledger = self.ledger()
        ledger.reserve("R1", "C1", task="task")
        ready = threading.Barrier(2, timeout=6)

        def repair():
            ready.wait()
            return ledger.reconcile_verified_response(
                "R1", "C1", task="task", role=None, backend=None,
                backend_sha256=None, success=True, token_in=9, token_out=3)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(repair)
            b = pool.submit(repair)
            self.assertEqual(a.result(timeout=8)["successful"], 1)
            self.assertEqual(b.result(timeout=8)["successful"], 1)
        state = ledger.status("R1")
        self.assertEqual((state["attempts"], state["successful"],
                          state["reported_tokens"]), (1, 1, 12))

    def test_child_process_waits_for_parent_ledger_lock_then_reads(self):
        ledger = self.ledger()
        ledger.reserve("R1", "C1", task="task")
        _, lock = ledger._paths("R1")
        script = """import sys
from alpha_research.agents.governance import ModelCallLedger
policy = {"max_calls":4, "max_reported_tokens":1000, "failover":"manual"}
print(ModelCallLedger(sys.argv[1], policy).status("R1")["attempts"])
"""
        with SingleWriterLock(lock):
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(self.root)],
                cwd=Path(__file__).resolve().parent.parent,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(0.12)
                self.assertIsNone(child.poll())
            except BaseException:
                child.kill()
                child.communicate()
                raise
        out, err = child.communicate(timeout=8)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(out.strip(), "1")


if __name__ == "__main__":
    unittest.main()
