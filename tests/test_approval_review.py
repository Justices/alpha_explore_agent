"""Deterministic offline tests of signed review receipts, never live approval."""
import copy
import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.execution_proposal import persist_execution_proposal
from alpha_research.orchestration.approval_review import (
    KEY_ENV, SIGNING_CONTEXT, verify_review_receipt, reserve_review_receipt,
)
from alpha_research.state.fingerprint import canonical_json_bytes


class ApprovalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runner = MainOrchestrator(self.temp.name)
        self.task = self.runner.create(
            "regular", {"objective": "no live execution", "region": "CHN"})
        self.proposal = persist_execution_proposal(self.runner, self.task["task_id"])
        self.now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        self.secret = "test-only-approval-verifier-key-of-adequate-length"
        self.env = {KEY_ENV: self.secret}
        self.model_hash = "a" * 64

    def signed(self, **changes):
        claims = {
            "schema_version": 1,
            "approval_id": "1" * 32,
            "issuer": "isolated-test-reviewer",
            "task_id": self.task["task_id"],
            "workflow": self.task["workflow"],
            "request_sha256": self.task["request_sha256"],
            "proposal_sha256": self.proposal["proposal_sha256"],
            "model_profile_sha256": self.model_hash,
            "operation": "review_execution_proposal",
            "max_execution_attempts": 1,
            "issued_at": (self.now - timedelta(minutes=1)).isoformat(),
            "expires_at": (self.now + timedelta(minutes=5)).isoformat(),
        }
        claims.update(changes)
        signature = hmac.new(
            self.secret.encode(), SIGNING_CONTEXT + canonical_json_bytes(claims),
            hashlib.sha256).hexdigest()
        return {"claims": claims, "signature": signature}

    def verify(self, receipt, **kwargs):
        return verify_review_receipt(
            self.runner, receipt, model_profile_sha256=self.model_hash,
            environ=self.env, now=self.now, **kwargs)

    def test_signed_review_is_single_use_but_never_execution_authority(self):
        receipt = self.signed()
        result = self.verify(receipt)
        self.assertTrue(result["verified"])
        for key in ("simulation_authorized", "model_call_authorized",
                    "alpha_submission_authorized"):
            self.assertFalse(result[key])
        stored = reserve_review_receipt(
            self.runner, receipt, model_profile_sha256=self.model_hash,
            environ=self.env, now=self.now)
        self.assertEqual(stored["status"], "RESERVED_REVIEW_ONLY")
        with self.assertRaisesRegex(OrchestrationError, "already consumed"):
            reserve_review_receipt(
                self.runner, receipt, model_profile_sha256=self.model_hash,
                environ=self.env, now=self.now)
        self.assertEqual(self.runner.load(self.task["task_id"]), self.task)

    def test_reject_tampered_approval_scope_and_model(self):
        receipt = self.signed()
        modified = copy.deepcopy(receipt)
        modified["claims"]["workflow"] = "super"
        with self.assertRaisesRegex(OrchestrationError, "binding"):
            self.verify(modified)
        for field, value in (("request_sha256", "b"*64),
                             ("proposal_sha256", "b"*64),
                             ("model_profile_sha256", "b"*64)):
            with self.subTest(field=field):
                with self.assertRaisesRegex(OrchestrationError, "binding"):
                    self.verify(self.signed(**{field: value}))
        modified = copy.deepcopy(receipt)
        modified["signature"] = "0" * 64
        with self.assertRaisesRegex(OrchestrationError, "signature"):
            self.verify(modified)
        with self.assertRaisesRegex(OrchestrationError, "verifier key"):
            verify_review_receipt(
                self.runner, receipt, model_profile_sha256=self.model_hash,
                environ={}, now=self.now)

    def test_reject_expired_future_bad_permissions_and_bad_budget(self):
        cases = [
            ({"issued_at": (self.now + timedelta(minutes=1)).isoformat()}, "expired"),
            ({"expires_at": (self.now - timedelta(minutes=1)).isoformat()}, "expired"),
            ({"expires_at": (self.now + timedelta(hours=2)).isoformat()}, "duration"),
            ({"operation": "simulate_super"}, "operation"),
            ({"max_execution_attempts": True}, "budget"),
            ({"max_execution_attempts": 2}, "budget"),
        ]
        for changes, reason in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(OrchestrationError, reason):
                    self.verify(self.signed(**changes))

    def test_new_approval_id_cannot_reconsume_same_task(self):
        one = self.signed()
        reserve_review_receipt(
            self.runner, one, model_profile_sha256=self.model_hash,
            environ=self.env, now=self.now)
        two = self.signed(approval_id="2" * 32)
        with self.assertRaisesRegex(OrchestrationError, "task approval review already reserved"):
            reserve_review_receipt(
                self.runner, two, model_profile_sha256=self.model_hash,
                environ=self.env, now=self.now)

    def test_secondary_record_failure_does_not_reopen_task_review(self):
        receipt = self.signed()
        from alpha_research.orchestration.approval_review import atomic_write_json as original_write

        writes = []
        def injected_write(path, record):
            writes.append(str(path))
            if len(writes) == 2:
                raise OSError("injected crash after durable task reservation")
            original_write(path, record)

        with patch("alpha_research.orchestration.approval_review.atomic_write_json",
                   side_effect=injected_write):
            with self.assertRaisesRegex(OSError, "injected crash"):
                reserve_review_receipt(
                    self.runner, receipt, model_profile_sha256=self.model_hash,
                    environ=self.env, now=self.now)

        root = (Path(self.temp.name) / "state" / "research_v1_1" / "approval_reviews")
        self.assertTrue((root / "tasks" / (self.task["task_id"] + ".json")).is_file())
        self.assertFalse((root / ("1" * 32 + ".json")).exists())
        for next_receipt in (receipt, self.signed(approval_id="2" * 32)):
            with self.assertRaisesRegex(OrchestrationError, "task approval review already reserved"):
                reserve_review_receipt(
                    self.runner, next_receipt, model_profile_sha256=self.model_hash,
                    environ=self.env, now=self.now)

    def test_independent_tasks_have_separate_review_reservations(self):
        other_task = self.runner.create(
            "regular", {"objective": "different offline task", "region": "CHN"})
        other_proposal = persist_execution_proposal(self.runner, other_task["task_id"])
        first = reserve_review_receipt(
            self.runner, self.signed(), model_profile_sha256=self.model_hash,
            environ=self.env, now=self.now)
        second = reserve_review_receipt(
            self.runner,
            self.signed(approval_id="2" * 32, task_id=other_task["task_id"],
                        request_sha256=other_task["request_sha256"],
                        proposal_sha256=other_proposal["proposal_sha256"]),
            model_profile_sha256=self.model_hash, environ=self.env, now=self.now)
        self.assertEqual(first["status"], second["status"])
        self.assertFalse(second["simulation_authorized"])
        self.assertFalse(second["model_call_authorized"])


if __name__ == "__main__":
    unittest.main()
