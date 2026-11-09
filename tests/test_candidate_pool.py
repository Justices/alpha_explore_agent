"""Offline regression contracts for globally deduplicated Alpha pools.

No network, no live BRAIN simulation, and no Alpha submission.
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

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


class PairwiseGateway:
    def __init__(self, details, local_status="pass"):
        self.details = details
        self.local_status = local_status
        self.commands = []
        self.contenders = None

    def call(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command == "set_case":
            return None
        if command == "get_alpha_details":
            return copy.deepcopy(self.details)
        if command == "check_local_self_correlation":
            limit = kwargs["self_corr_max"]
            return {"status": self.local_status, "complete": True,
                    "comparison_count": 1, "window_years": 4,
                    "limit": limit, "value": limit if self.local_status == "fail" else 0.1}
        if command == "get_user_alphas":
            return {"count": 1, "results": [{
                "id": "A_OTHER", "status": "ACTIVE",
                "settings": {"region": "GLB", "delay": 1},
                "classifications": []}]}
        if command == "check_correlation":
            self.contenders = kwargs["contender_ids"]
            return {
                "checks": {
                    "production": {"correlation_data": {"max": 0.1}},
                    "self": {"correlation_data": {
                        "records": [[0.1]], "count": 1,
                        "schema": {"properties": [{"name": "correlation"}]}}},
                },
                "case_pairwise": {"A_OTHER": {
                    "source": "daily_pnl", "observations": 3, "correlation": 0.1}},
            }
        raise AssertionError("unexpected gateway call " + command)


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

    def test_missing_preferred_is_pending_not_not_candidate(self):
        identity = "1" * 32
        _upsert(self.store, "candidate", self.sample(
            kind="candidate", candidate_id=identity,
            workflow="regular", status="CANDIDATE"))
        self.assertEqual(self.pool.build_index()["entries"][0]["preferred_status"],
                         "PENDING_PREFERRED")

    def test_policy_change_invalidates_previously_qualified_candidate(self):
        identity = "2" * 32
        _upsert(self.store, "candidate", self.sample(
            kind="candidate", candidate_id=identity,
            workflow="regular", status="CANDIDATE"))
        _upsert(self.store, "preferred", self.sample(
            kind="preferred", candidate_id=identity,
            workflow="regular", status="PREFERRED"))
        entry = self.pool.build_index(current_policy_sha256="c" * 64)["entries"][0]
        self.assertEqual(entry["candidate_status"], "POLICY_STALE")
        self.assertEqual(entry["preferred_status"], "NOT_CANDIDATE")
        self.assertTrue(entry["requires_reaudit"])

    def test_corrupt_derived_index_is_rebuilt_from_records(self):
        self.store.root.mkdir(parents=True, exist_ok=True)
        self.pool.index_path.write_text("{broken", encoding="utf-8")
        rebuilt = self.pool.sync_index()
        self.assertEqual(json.loads(self.pool.index_path.read_text(encoding="utf-8")), rebuilt)

    def test_local_self_failure_is_rejection_not_pending(self):
        details = {
            "id": "A_SAME", "type": "SUPER",
            "settings": {"region": "GLB", "delay": 1},
            "is": {"checks": [{"name": "LOW_SHARPE", "result": "PASS"}]},
        }
        gateway = PairwiseGateway(details, local_status="fail")
        service = CandidateQualificationService(
            gateway, self.root, copy.deepcopy(DEFAULT_CONFIG))
        verdict = service.qualify_live(
            workflow="super", alpha_id="A_SAME",
            candidate_ref="run-2", source_ref="readback.json")
        self.assertEqual(verdict["candidate_status"], "CORR_REJECTED")
        self.assertNotIn("check_correlation", [name for name, _ in gateway.commands])

    def test_super_and_optimization_discover_pairwise_contenders(self):
        details = {
            "id": "A_SAME", "type": "SUPER",
            "settings": {"region": "GLB", "delay": 1},
            "is": {"checks": [{"name": "LOW_SHARPE", "result": "PASS"}]},
        }
        for workflow in ("super", "optimization"):
            with self.subTest(workflow=workflow):
                gateway = PairwiseGateway(details)
                service = CandidateQualificationService(
                    gateway, self.root, copy.deepcopy(DEFAULT_CONFIG))
                service.qualify_live(
                    workflow=workflow, alpha_id="A_SAME",
                    candidate_ref=("a" if workflow == "super" else "b") * 32,
                    source_ref="readback.json")
                self.assertEqual(gateway.contenders, ["A_OTHER"])

    def test_pairwise_and_platform_check_keep_distinct_statuses(self):
        service = CandidateQualificationService(
            DummyGateway({}), self.root, copy.deepcopy(DEFAULT_CONFIG))
        pairwise = service._persist(
            workflow="super", alpha_id="A_PAIR", candidate_ref="c" * 32,
            source_ref="pairwise.json",
            details={"id": "A_PAIR", "settings": {"region": "GLB"},
                     "is": {"checks": []}},
            gate={"status": "METRICS_FAILED", "failures": ["pairwise:A_OTHER"],
                  "pending": []}, correlations={}, evidence_refs={})
        self.assertEqual(pairwise["candidate_status"], "PAIRWISE_CONFLICT")
        platform = service._persist(
            workflow="super", alpha_id="A_PLATFORM", candidate_ref="d" * 32,
            source_ref="platform.json",
            details={"id": "A_PLATFORM", "settings": {"region": "GLB"},
                     "is": {"checks": [{"name": "CUSTOM_CHECK", "result": "FAIL"}]}},
            gate={"status": "CANDIDATE", "failures": [], "pending": []},
            correlations={}, evidence_refs={})
        self.assertEqual(platform["candidate_status"], "PLATFORM_CHECK_FAILED")

    def test_candidate_stays_hidden_if_preferred_write_fails_then_retry_completes(self):
        service = CandidateQualificationService(
            DummyGateway({}), self.root, copy.deepcopy(DEFAULT_CONFIG))
        details = {"id": "A_TX", "settings": {"region": "GLB"}, "is": {"checks": []}}
        module = __import__("nastu_quant.research.candidate_qualification",
                            fromlist=["_upsert"])
        original = module._upsert
        failed = {"value": False}
        def fail_preferred_once(store, kind, record):
            if kind == "preferred" and not failed["value"]:
                failed["value"] = True
                raise OSError("injected preferred write failure")
            return original(store, kind, record)
        with patch("nastu_quant.research.candidate_qualification._upsert",
                   side_effect=fail_preferred_once):
            with self.assertRaises(OSError):
                service._persist(
                    workflow="regular", alpha_id="A_TX", candidate_ref="e" * 32,
                    source_ref="tx.json", details=details,
                    gate={"status": "CANDIDATE", "failures": [], "pending": []},
                    correlations={}, evidence_refs={})
        staged = self.store.list("candidate")[0]
        self.assertEqual(staged["status"], "EVALUATION_PENDING")
        self.assertEqual(self.pool.build_index()["entries"][0]["preferred_status"],
                         "PENDING_PREFERRED")
        completed = service._persist(
            workflow="regular", alpha_id="A_TX", candidate_ref="e" * 32,
            source_ref="tx.json", details=details,
            gate={"status": "CANDIDATE", "failures": [], "pending": []},
            correlations={}, evidence_refs={})
        self.assertEqual(completed["candidate_status"], "CANDIDATE")
        self.assertEqual(self.store.get("candidate", completed["candidate_id"])["status"],
                         "CANDIDATE")
        self.assertEqual(self.store.get("preferred", completed["candidate_id"])["status"],
                         completed["preferred"]["status"])

    def test_unversioned_legacy_candidate_requires_reaudit(self):
        identity = "f" * 32
        record = self.sample(kind="candidate", candidate_id=identity,
                             workflow="regular", status="CANDIDATE")
        record.pop("evidence_sha256")
        record.pop("policy_sha256")
        _upsert(self.store, "candidate", record)
        entries = self.pool.build_index()["entries"]
        self.assertEqual(entries[0]["candidate_status"], "EVIDENCE_PENDING")
        self.assertTrue(entries[0]["requires_reaudit"])

    def test_strict_correlation_boundary(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.assertFalse(correlation_verdict(0.7, 0.4, {}, cfg)[0])
        self.assertFalse(correlation_verdict(0.4, -0.7, {}, cfg)[0])
        self.assertTrue(correlation_verdict(0.69, -0.69, {}, cfg)[0])


if __name__ == "__main__":
    unittest.main()
