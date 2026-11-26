"""Regression tests for scope-bound Super research feedback."""
import unittest

from alpha_research.workflows.super_alpha.workflow import (
    SuperScopeError, _prior_super_findings,
)


class FakeRecordStore:
    def __init__(self, rows):
        self.rows = rows

    def list(self, kind):
        if kind != "super_run":
            raise AssertionError("unexpected record kind")
        return list(self.rows)


class SuperFeedbackTests(unittest.TestCase):
    def test_feedback_is_bounded_to_exact_region_and_objective(self):
        rows = [{
            "run_id": f"run-{index}", "region": "USA",
            "objective": "quality alpha", "status": "candidate_evaluated",
            "candidate_alpha_id": f"ALPHA-{index}",
            "candidate_status": "CANDIDATE",
            "preferred_status": "NOT_PREFERRED",
            "audit_ref": f"audit-{index}", "readback_ref": f"readback-{index}",
            "updated_at": f"2026-11-{index + 1:02d}T00:00:00+00:00",
        } for index in range(7)]
        rows.extend([
            {**rows[-1], "run_id": "other-region", "region": "CHN"},
            {**rows[-1], "run_id": "other-objective", "objective": "other"},
        ])
        feedback = _prior_super_findings(
            FakeRecordStore(rows), "USA", "quality alpha", limit=3)
        self.assertEqual([row["run_id"] for row in feedback],
                         ["run-6", "run-5", "run-4"])
        self.assertNotIn("other-region", repr(feedback))
        self.assertNotIn("other-objective", repr(feedback))

    def test_feedback_limit_must_be_positive(self):
        with self.assertRaises(SuperScopeError):
            _prior_super_findings(FakeRecordStore([]), "USA", "quality alpha", limit=0)


if __name__ == "__main__":
    unittest.main()
