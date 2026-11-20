"""Cross-process crash and contention tests for the offline review ledger.

Every child uses a synthetic test-only signing key and never loads a model or
platform gateway.  Paths and processes are controlled by TemporaryDirectory.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpha_research.orchestration.approval_review import KEY_ENV, SIGNING_CONTEXT
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.execution_proposal import persist_execution_proposal
from alpha_research.state.fingerprint import canonical_json_bytes
import hashlib
import hmac


_CHILD = r'''
import json, os, sys, time
from pathlib import Path
from datetime import datetime
from alpha_research.orchestration import approval_review as mod
from alpha_research.orchestration.engine import MainOrchestrator

root, payload_path, ready, start, output, mode = sys.argv[1:]
payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
runner = MainOrchestrator(root)
if mode == "crash_after_canonical_claim":
    original = mod.atomic_write_json
    writes = [0]
    def interrupt_after_first(path, record):
        original(path, record)
        writes[0] += 1
        if writes[0] == 1:
            os._exit(73)
    mod.atomic_write_json = interrupt_after_first
Path(ready).write_text("ready", encoding="utf-8")
limit = time.monotonic() + 20
while not Path(start).exists():
    if time.monotonic() > limit:
        sys.exit(72)
    time.sleep(0.01)
try:
    result = mod.reserve_review_receipt(
        runner, payload["receipt"],
        model_profile_sha256=payload["model_hash"],
        now=datetime.fromisoformat(payload["now"]))
    value = {"status": "reserved", "approval_id": result["approval_id"]}
except Exception as exc:
    value = {"status": "rejected", "type": type(exc).__name__}
Path(output).write_text(json.dumps(value), encoding="utf-8")
'''


class CrossProcessApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runner = MainOrchestrator(self.root)
        self.task = self.runner.create(
            "regular", {"objective": "cross-process synthetic review", "region": "CHN"})
        self.proposal = persist_execution_proposal(self.runner, self.task["task_id"])
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        self.secret = "isolated-cross-process-test-key-32-bytes-minimum"
        self.model_hash = "a" * 64

    def _payload(self, approval_id, task=None, proposal=None):
        task = self.task if task is None else task
        proposal = self.proposal if proposal is None else proposal
        claims = {
            "schema_version": 1, "approval_id": approval_id,
            "issuer": "offline-test-reviewer", "task_id": task["task_id"],
            "workflow": task["workflow"], "request_sha256": task["request_sha256"],
            "proposal_sha256": proposal["proposal_sha256"],
            "model_profile_sha256": self.model_hash,
            "operation": "review_execution_proposal",
            "max_execution_attempts": 1,
            "issued_at": (self.now - timedelta(minutes=1)).isoformat(),
            "expires_at": (self.now + timedelta(minutes=5)).isoformat(),
        }
        signature = hmac.new(
            self.secret.encode(), SIGNING_CONTEXT + canonical_json_bytes(claims),
            hashlib.sha256).hexdigest()
        return {"receipt": {"claims": claims, "signature": signature},
                "now": self.now.isoformat(), "model_hash": self.model_hash}

    def _launch(self, label, payload, mode="reserve"):
        input_file = self.root / (label + "-input.json")
        input_file.write_text(json.dumps(payload), encoding="utf-8")
        ready = self.root / (label + "-ready")
        out = self.root / (label + "-result.json")
        start = self.root / "start"
        env = os.environ.copy()
        env[KEY_ENV] = self.secret
        process = subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(self.root),
             str(input_file), str(ready), str(start), str(out), mode],
            cwd=str(Path(__file__).resolve().parent.parent), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._cleanup_process, process)
        return process, ready, out

    @staticmethod
    def _cleanup_process(process):
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)

    def _ready(self, paths):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if all(p.exists() for p in paths):
                return
            time.sleep(0.02)
        self.fail("child processes did not initialize")

    def test_two_processes_same_task_different_ids_only_one_reserves(self):
        one = self._launch("one", self._payload("1" * 32))
        two = self._launch("two", self._payload("2" * 32))
        self._ready([one[1], two[1]])
        (self.root / "start").write_text("go", encoding="utf-8")
        results = []
        for p, _, out in (one, two):
            _, error = p.communicate(timeout=20)
            self.assertEqual(p.returncode, 0, error[-1000:])
            results.append(json.loads(out.read_text(encoding="utf-8"))["status"])
        self.assertEqual(sorted(results), ["rejected", "reserved"])
        fences = list((self.root / "state" / "research_v1_1" /
                       "approval_reviews" / "tasks").glob("*.json"))
        self.assertEqual(len(fences), 1)

    def test_hard_process_exit_after_canonical_claim_blocks_both_identities(self):
        process, ready, _ = self._launch(
            "crash", self._payload("3" * 32), mode="crash_after_task_fence")
        self._ready([ready])
        (self.root / "start").write_text("go", encoding="utf-8")
        _, error = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 73, error[-1000:])
        from alpha_research.orchestration.approval_review import reserve_review_receipt
        with self.assertRaisesRegex(OrchestrationError, "already reserved"):
            reserve_review_receipt(
                MainOrchestrator(self.root), self._payload("4" * 32)["receipt"],
                model_profile_sha256=self.model_hash,
                environ={KEY_ENV: self.secret}, now=self.now)
        self.assertEqual(self.runner.load(self.task["task_id"])["status"], "CREATED")
        # The approval ID is also consumed, even though the secondary
        # approval-ID projection was never written before the hard exit.
        second_task = self.runner.create(
            "regular", {"objective": "second task", "region": "CHN"})
        second_proposal = persist_execution_proposal(
            self.runner, second_task["task_id"])
        with self.assertRaisesRegex(OrchestrationError, "ID already consumed"):
            reserve_review_receipt(
                MainOrchestrator(self.root),
                self._payload("3" * 32, task=second_task,
                              proposal=second_proposal)["receipt"],
                model_profile_sha256=self.model_hash,
                environ={KEY_ENV: self.secret}, now=self.now)



if __name__ == "__main__":
    unittest.main()
