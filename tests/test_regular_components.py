"""Component-level regression tests for extracted Regular workflow boundaries.

No model CLI, HTTP provider, BRAIN Gateway or Simulation calls occur here.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.workflows.regular.agent import ResearchAgent, ResearchError
from alpha_research.workflows.regular.case_evidence import CaseEvidenceStore
from alpha_research.workflows.regular.candidate_audit import CandidateAuditService
from alpha_research.workflows.regular.decisions import ResearchDecisionSession
from alpha_research.workflows.regular.field_catalog import (
    _admissible_field_rows, _campaign_field_pool, _rows,
)
from alpha_research.workflows.regular.settings_validation import _validate_test_period


class FakeRecorder:
    def __init__(self):
        self.session_id = None
        self.bound = []
        self.inputs = []

    def begin_session(self, *, research_id=None):
        self.session_id = research_id
        return research_id

    def bind_batch(self, batch_dir):
        self.bound.append(str(batch_dir))

    def complete_json(self, task, context, **kwargs):
        self.inputs.append((task, context, kwargs))
        return {"summary": "fixture attribution only"}


class FakeKnowledge:
    def build_pack(self, task, context):
        return {"task": task, "context_label": context.get("label", "")}


class RegularBoundaryTests(unittest.TestCase):
    def test_exported_error_type_remains_one_canonical_class(self):
        from alpha_research.workflows.regular.errors import ResearchError as Shared
        self.assertIs(ResearchError, Shared)

    def test_case_ledger_scope_invariant_and_alpha_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CaseEvidenceStore(Path(directory))
            scope = {"region": "CHN", "universe": "TOP3000", "delay": 1}
            case = store.read_case("model selection", scope)
            self.assertEqual(case["contenders"], {})
            self.assertEqual(case["scope"], scope)
            store.register_alpha_state("A1", {"state": "RA_PASSED"})
            self.assertEqual(store.lookup_alpha_state("A1")["state"], "RA_PASSED")
            self.assertIsNone(store.lookup_alpha_state("A2"))
            path = store.case_path("model selection", scope)
            self.assertEqual(store.case_path("model selection", dict(scope)), path)
            changed = {**scope, "delay": 0}
            self.assertNotEqual(store.case_path("model selection", changed), path)
            with self.assertRaisesRegex(ResearchError, "requires both"):
                store.resolve_remedy_reference("model selection", "D1", None)

    def test_decision_session_stores_pack_and_passes_read_only_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = FakeRecorder()
            session = ResearchDecisionSession(FakeKnowledge(), recorder, root)
            self.assertEqual(session.begin_session("R1"), "R1")
            batch = root / "batch"
            outcome = session.decide(
                "attribute_results", {"label": "synthetic"}, batch_dir=batch)
            self.assertEqual(outcome["summary"], "fixture attribution only")
            self.assertEqual(recorder.bound, [str(batch)])
            task, enriched, opts = recorder.inputs[0]
            self.assertEqual(task, "attribute_results")
            self.assertEqual(enriched["knowledge_pack"]["context_label"], "synthetic")
            ref = Path(opts["knowledge_pack_ref"])
            self.assertEqual(json.loads(ref.read_text(encoding="utf-8"))["task"],
                             "attribute_results")
            manifest = json.loads((batch / "knowledge_pack.json").read_text(
                encoding="utf-8"))
            self.assertEqual(len(manifest["packs"]), 1)
            self.assertEqual(manifest["session_ids"], ["R1"])
            session.begin_session("R2")
            self.assertEqual(session.session_packs, [])
            self.assertEqual(len(manifest["packs"]), 1)

    def test_field_catalog_admission_does_not_broaden_dataset(self):
        rows = [
            {"id": "signal_a", "dataset": {"id": "risk60"}, "type": "MATRIX",
             "description": "economic sentiment", "coverage": 0.9,
             "dateCoverage": 0.9, "alphaCount": 15},
            {"id": "signal_b", "dataset": {"id": "other"}, "type": "MATRIX",
             "description": "outside", "coverage": 0.9,
             "dateCoverage": 0.9, "alphaCount": 15},
        ]
        self.assertEqual(set(_admissible_field_rows(rows, "risk60")), {"signal_a"})
        self.assertEqual(len(_rows({"results": rows})), 2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pool.json"
            scope = {"region": "CHN", "delay": 1, "universe": "TOP3000"}
            pool = _campaign_field_pool(path, rows, "risk60", scope)
            self.assertEqual([row["id"] for row in pool], ["signal_a"])
            self.assertEqual(_campaign_field_pool(
                path, list(reversed(rows)), "risk60", scope), pool)
            with self.assertRaisesRegex(ResearchError, "scope"):
                _campaign_field_pool(path, rows, "risk60", {**scope, "region": "USA"})

    def test_duration_range_fail_closed(self):
        schema = {"type": "string", "minValue": "P0Y0M0D",
                  "maxValue": "P6Y0M0D"}
        self.assertEqual(
            _validate_test_period("P1Y0M0D", schema)["value"], "P1Y0M0D")
        with self.assertRaisesRegex(ResearchError, "test_period"):
            _validate_test_period("P9Y0M0D", schema)
        with self.assertRaisesRegex(ResearchError, "test_period"):
            _validate_test_period("P1Y0M0D", {})

    def test_agent_audit_wrapper_works_for_minimal_fixture_instance(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        with patch.object(CandidateAuditService, "audit",
                          return_value={"safe": True}) as call:
            result = agent._apply_canonical_audit(
                Path("batch"), {"dataset_id": "risk60"}, ["A1"], {}, {})
            self.assertEqual(result, {"safe": True})
            self.assertEqual(call.call_count, 1)
        with patch.object(CandidateAuditService, "refresh_retained",
                          return_value=([], {})):
            self.assertEqual(
                agent._refresh_retained_contenders({}, [], {}, 0.7, Path("batch")),
                ([], {}))

    def test_agent_field_workflow_delegates_without_starting_platform(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        class FakeFieldPlans:
            def construct(self, *args):
                return {"plan": "verified"}
            def execute(self, *args):
                return {"completed": True}
        agent.field_plans = FakeFieldPlans()
        self.assertEqual(agent._execute_field_plan(Path("batch"), {}),
                         {"completed": True})
        self.assertEqual(agent._construct_field_plan(
            Path("batch"), {}, [], {}, {}, {}, [], {}), {"plan": "verified"})


if __name__ == "__main__":
    unittest.main()
