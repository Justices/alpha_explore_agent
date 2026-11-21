"""P3 isolation guard contracts: designed for later CI execution.

No provider, BRAIN Gateway, Simulation or model invocation is performed.
"""
import tempfile
import unittest
from pathlib import Path

from alpha_research.orchestration.mock_workspace import (
    claim_mock_workspace, inspect_mock_workspace,
)
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.qualification.pool import CandidatePool
from alpha_research.state.records import ResearchRecordStore, offline_mock_record_scope


class DomainMockIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_new_mock_root_is_marked_and_blocks_unmarked_records(self):
        report = claim_mock_workspace(self.root)
        self.assertEqual(report["status"], "FRESH_EMPTY_MOCK_ROOT")
        self.assertEqual(inspect_mock_workspace(self.root)["status"],
                         "EXISTING_MOCK_ONLY")
        self.assertEqual(CandidatePool(ResearchRecordStore(self.root))
                         .build_index()["candidate_count"], 0)

    def test_mock_origin_is_context_local_and_restored(self):
        from alpha_research.state.records import _ACTIVE_EVIDENCE_ORIGIN
        self.assertIsNone(_ACTIVE_EVIDENCE_ORIGIN.get())
        with offline_mock_record_scope():
            self.assertEqual(_ACTIVE_EVIDENCE_ORIGIN.get(),
                             "OFFLINE_DOMAIN_MOCK")
        self.assertIsNone(_ACTIVE_EVIDENCE_ORIGIN.get())

    def test_context_does_not_leak_to_independent_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        from alpha_research.state.records import _ACTIVE_EVIDENCE_ORIGIN
        with offline_mock_record_scope():
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertIsNone(
                    pool.submit(_ACTIVE_EVIDENCE_ORIGIN.get).result())
        self.assertIsNone(_ACTIVE_EVIDENCE_ORIGIN.get())

    def test_second_claim_reuses_same_mock_workspace_identity(self):
        first = claim_mock_workspace(self.root)
        second = claim_mock_workspace(self.root)
        self.assertEqual(first["status"], "FRESH_EMPTY_MOCK_ROOT")
        self.assertEqual(second["status"], "EXISTING_MOCK_ONLY")
        self.assertEqual(first["marker"], second["marker"])

    def test_existing_real_run_prevents_mock_claim(self):
        directory = self.root / "runs" / "real"
        directory.mkdir(parents=True)
        (directory / "batch.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "existing research runs"):
            claim_mock_workspace(self.root)
        self.assertFalse((self.root / "state" / "research_v1_1"
                          / "domain_mock_workspace.json").exists())

    def test_existing_candidate_prevents_mock_claim(self):
        directory = self.root / "state" / "research_v1_1" / "candidate"
        directory.mkdir(parents=True)
        (directory / "record.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "domain evidence"):
            claim_mock_workspace(self.root)

    def test_corrupt_marker_fails_closed(self):
        claim_mock_workspace(self.root)
        marker = self.root / "state" / "research_v1_1" / "domain_mock_workspace.json"
        marker.write_text('{"kind":"other"}', encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "identity differs"):
            inspect_mock_workspace(self.root)

    def test_mock_pool_always_blank_including_unaudited_raw_record(self):
        claim_mock_workspace(self.root)
        # Even copied/legacy files should never be promoted by the pool reader.
        candidate = (self.root / "state" / "research_v1_1" /
                     "candidate" / ("a" * 32 + ".json"))
        candidate.parent.mkdir(parents=True)
        candidate.write_text('{"not":"validated evidence"}', encoding="utf-8")
        pool = CandidatePool(ResearchRecordStore(self.root))
        self.assertEqual(pool.build_index()["alpha_count"], 0)
        self.assertIsNone(pool.find_reusable(
            "OFFLINE_ALPHA", evidence_sha256="a" * 64,
            policy_sha256="b" * 64))


if __name__ == "__main__":
    unittest.main()
