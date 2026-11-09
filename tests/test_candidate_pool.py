"""Offline regression contracts for globally deduplicated Alpha pools.

No network, no live BRAIN simulation, and no Alpha submission.
"""
import copy
import tempfile
import unittest
from pathlib import Path

from nastu_quant.config import DEFAULT_CONFIG
from nastu_quant.gatekeeper import correlation_verdict
from nastu_quant.research.candidate_pool import CandidatePool
from nastu_quant.research.candidate_qualification import (
    CandidateQualificationService, _upsert,
)
from nastu_quant.research.records import ResearchRecordStore


class DummyGateway:
    def __init__(self, details):
        self.details = details
        self.commands = []

    def call(self, command, **kwargs):
        self.commands.append(command)
        if command == "set_case":
            return None
        if command == "get_alpha_details":
            return self.details
        raise AssertionError("unexpected platform call " + command)


class TestGlobalCandidatePool(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ResearchRecordStore(self.root)
        self.pool = CandidatePool(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def sample(self, *, kind, candidate_id, workflow, status):
        record = {
            "schema_version": 1, "candidate_id": candidate_id,
            "alpha_id": "A_SAME", "workflow": workflow,
            "source_ref": "evidence/" + candidate_id,
            "candidate_ref": "branch/" + candidate_id,
            "status": status, "failures": [], "pending": [],
            "metrics": {"sharpe": 2.1}, "evidence_refs": {},
            "evidence_sha256": "a" * 64,
            "policy_sha256": "b" * 64,
            "created_at": "2026-11-09T00:00:00+00:00",
            "updated_at": "2026-11-09T00:00:00+00:00",
        }
        if kind == "candidate":
            record["correlations"] = {"prod": 0.2, "self": 0.3}
        return record

    def test_deduplicates_cross_workflow_but_retains_sources(self):
        first = "a" * 32
        second = "b" * 32
        _upsert(self.store, "candidate", self.sample(
            kind="candidate", candidate_id=first,
            workflow="regular", status="CANDIDATE"))
        _upsert(self.store, "preferred", self.sample(
            kind="preferred", candidate_id=first,
            workflow="regular", status="PREFERRED"))
        _upsert(self.store, "candidate", self.sample(
            kind="candidate", candidate_id=second,
            workflow="optimization", status="EVIDENCE_PENDING"))
        index = self.pool.sync_index()
        self.assertEqual(index["alpha_count"], 1)
        self.assertEqual(index["candidate_count"], 0)
        self.assertEqual(index["preferred_count"], 0)
        self.assertEqual(index["entries"][0]["evaluation_count"], 2)
        self.assertEqual(len(index["entries"][0]["sources"]), 2)
        self.assertEqual(self.pool.list_candidates(), [])
        self.assertEqual(self.pool.list_preferred(), [])

    def test_same_evidence_is_noop_and_change_archives_old_record(self):
        identity = "c" * 32
        original = self.sample(kind="candidate", candidate_id=identity,
                               workflow="super", status="EVIDENCE_PENDING")
        path = _upsert(self.store, "candidate", copy.deepcopy(original))
        previous = self.store.get("candidate", identity)
        _upsert(self.store, "candidate", copy.deepcopy(original))
        self.assertEqual(previous["updated_at"],
                         self.store.get("candidate", identity)["updated_at"])
        history = self.store.root / "evaluation_history" / "candidate" / identity
        self.assertFalse(history.exists())
        changed = copy.deepcopy(original)
        changed["status"] = "RA_FAILED"
        changed["failures"] = ["ra:LOW_SHARPE"]
        changed["evidence_sha256"] = "c" * 64
        _upsert(self.store, "candidate", changed)
        files = list(history.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(self.store.get("candidate", identity)["status"], "RA_FAILED")
        self.assertEqual(self.pool.sync_index(), self.pool.sync_index())
        self.assertTrue(path.exists())

    def test_exact_policy_and_evidence_required_for_reuse(self):
        identity = "d" * 32
        _upsert(self.store, "candidate", self.sample(
            kind="candidate", candidate_id=identity,
            workflow="regular", status="CANDIDATE"))
        self.assertIsNotNone(self.pool.find_reusable(
            "A_SAME", evidence_sha256="a" * 64, policy_sha256="b" * 64))
        self.assertIsNone(self.pool.find_reusable(
            "A_SAME", evidence_sha256="a" * 64, policy_sha256="c" * 64))
        self.assertIsNone(self.pool.find_reusable(
            "A_OTHER", evidence_sha256="a" * 64, policy_sha256="b" * 64))

    def test_unmatched_preferred_cannot_promote_candidate(self):
        identity = "e" * 32
        _upsert(self.store, "candidate", self.sample(
            kind="candidate", candidate_id=identity,
            workflow="super", status="CANDIDATE"))
        previous = self.sample(kind="preferred", candidate_id=identity,
                               workflow="super", status="PREFERRED")
        previous["evidence_sha256"] = "f" * 64
        _upsert(self.store, "preferred", previous)
        self.assertEqual(self.pool.build_index()["preferred_count"], 0)

    def test_failed_ra_never_requests_correlations(self):
        details = {
            "id": "A_SAME", "type": "REGULAR",
            "settings": {"region": "GLB"},
            "is": {"checks": [{"name": "LOW_SHARPE", "result": "FAIL"}]},
        }
        gateway = DummyGateway(details)
        service = CandidateQualificationService(
            gateway, self.root, copy.deepcopy(DEFAULT_CONFIG))
        verdict = service.qualify_live(
            workflow="super", alpha_id="A_SAME",
            candidate_ref="run-1", source_ref="readback.json")
        self.assertEqual(verdict["candidate_status"], "RA_FAILED")
        self.assertEqual(gateway.commands,
                         ["set_case", "get_alpha_details"])

    def test_strict_correlation_boundary(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.assertFalse(correlation_verdict(0.7, 0.4, {}, cfg)[0])
        self.assertFalse(correlation_verdict(0.4, -0.7, {}, cfg)[0])
        self.assertTrue(correlation_verdict(0.69, -0.69, {}, cfg)[0])


if __name__ == "__main__":
    unittest.main()
