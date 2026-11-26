"""Regression tests for source-bound optimization feedback."""
import unittest

from alpha_research.workflows.optimization.workflow import (
    OptimizationError, _prior_optimization_findings,
)


class FakeRecordStore:
    def __init__(self, rows):
        self.rows = rows

    def list(self, kind):
        if kind != "candidate_branch":
            raise AssertionError("unexpected record kind")
        return list(self.rows)


class OptimizationFeedbackTests(unittest.TestCase):
    def test_only_same_immutable_source_and_bounded_latest_branches_are_returned(self):
        rows = []
        for index in range(7):
            rows.append({
                "branch_id": f"branch-{index}",
                "source_alpha_id": "ALPHA-1",
                "source_properties_sha256": "a" * 64,
                "objective": f"objective-{index}",
                "rationale": f"rationale-{index}",
                "changes": [f"change-{index}"],
                "status": "awaiting_independent_authorization",
                "receipt_ref": None,
                "audit_ref": None,
                "created_at": f"2026-11-{index + 1:02d}T00:00:00+00:00",
                "updated_at": f"2026-11-{index + 1:02d}T00:00:00+00:00",
            })
        rows.append({
            **rows[-1], "branch_id": "different-definition",
            "source_properties_sha256": "b" * 64,
            "objective": "must not leak",
        })
        rows.append({
            **rows[-1], "branch_id": "different-alpha",
            "source_alpha_id": "ALPHA-2",
            "source_properties_sha256": "a" * 64,
        })
        findings = _prior_optimization_findings(
            FakeRecordStore(rows), {
                "alpha_id": "ALPHA-1",
                "source_properties_sha256": "a" * 64,
            }, limit=3)
        self.assertEqual([row["branch_id"] for row in findings],
                         ["branch-6", "branch-5", "branch-4"])
        self.assertNotIn("must not leak", repr(findings))

    def test_feedback_requires_verified_source_identity(self):
        with self.assertRaises(OptimizationError):
            _prior_optimization_findings(FakeRecordStore([]), {
                "alpha_id": "ALPHA-1",
            })


if __name__ == "__main__":
    unittest.main()
