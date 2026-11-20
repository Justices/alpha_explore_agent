"""Dataset review cannot manufacture a model-selected simulation or replay an intent."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.state.store import atomic_write_json
from alpha_research.workflows.regular.dataset_review import (
    DatasetReviewError, review_dataset,
)


class Agent:
    def __init__(self, root, selection=None):
        self.runs_root = root / "runs"
        self.runs_root.mkdir(exist_ok=True)
        self.config = {"scope": {"region": "GLB"}}
        self.model = object()
        self.stop_flag = None
        self.selection = selection
        self.calls = []

    def _begin_session(self, research_id):
        self.calls.append(("session", research_id))

    def _decide(self, task, context, batch_dir=None):
        self.calls.append(("decide", task))
        assert task == "review_dataset_results"
        assert context["dataset_id"] == "risk60"
        return {
            "summary": "Measured baseline permits controlled hypothesis testing",
            "selection": self.selection,
            "selection_reason": "The observed turnover suggests testing smoothing",
        }

    def attribute_saved_batch(self, batch, result):
        raise AssertionError("verified completed attribution must be reused")


def setup_case(root, selection=None):
    agent=Agent(root, selection)
    batch=root / "runs" / "GLB" / "D1" / "TOP3000" / "risk60" / "batch-1"
    batch.mkdir(parents=True)
    settings={"region":"GLB","delay":1,"universe":"TOP3000",
              "instrument_type":"EQUITY"}
    atomic_write_json(batch / "prereg.json", {
        "dataset_id": "risk60",
        "variants": [{"expression":"rank(signal_a)","research":{"idea":"test"}}],
        "simulation_settings": settings,
    })
    atomic_write_json(batch / "simulation_intent.json", {
        "status":"READ_BACK","alpha_ids":["A1"],"settings":settings,
    })
    atomic_write_json(batch / "results.json", {
        "alpha_ids":["A1"],"attribution_status":"complete",
        "attribution":{"summary":"observed metrics were read back"},
        "readback":{"A1":{"details":{"is":{"sharpe":1.5,"fitness":0.9}}}},
        "gate":{"A1":{"status":"FAIL","failures":["fitness"]}},
    })
    directory=root / "state" / "research_v1_1" / "campaign_runs" / "dataset_reviews" / "CAMP" / "risk60"
    receipt={"batch_dir":str(batch),"dataset_id":"risk60","alpha_ids":["A1"],
             "attribution_status":"complete"}
    request={"objective":"test alpha", "region":"GLB","delay":1,
             "universe":"TOP3000","ppa_profile":False}
    return agent,directory,receipt,request


class ReviewSafetyTests(unittest.TestCase):
    def test_null_selection_is_idempotent_and_does_not_simulate(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            agent,directory,receipt,request=setup_case(root)
            with patch("alpha_research.workflows.regular.dataset_review.run_optimization",
                       side_effect=AssertionError("must never optimize")):
                result=review_dataset(agent, object(), root, directory,
                                      request,"risk60",[receipt],[])
                again=review_dataset(agent, object(), root, directory,
                                     request,"risk60",[receipt],[])
            self.assertEqual(result["status"],"no_selection")
            self.assertEqual(again,result)
            self.assertEqual([x for x in agent.calls if x[0]=="decide"],
                             [("decide","review_dataset_results")])

    def test_unknown_alpha_selection_is_rejected_before_simulation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            agent,directory,receipt,request=setup_case(root,"A999")
            with patch("alpha_research.workflows.regular.dataset_review.run_optimization",
                       side_effect=AssertionError("must never optimize")):
                with self.assertRaisesRegex(DatasetReviewError,"measured source"):
                    review_dataset(agent, object(), root, directory,
                                   request,"risk60",[receipt],[])
            self.assertEqual(json.loads((directory/"review.json").read_text())["status"],
                             "attribution_pending")

    def test_optimization_intent_resume_uses_exact_existing_target(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            agent,directory,receipt,request=setup_case(root,"A1")
            calls=[]
            def uncertain(gateway, model, config, state_root, *, run_id, **kwargs):
                calls.append(("start",run_id))
                self.assertEqual(kwargs["alpha_id"],"A1")
                self.assertEqual(kwargs["expected_dataset"],"risk60")
                self.assertEqual(kwargs["expected_settings"]["universe"],"TOP3000")
                intent=root / "state" / "research_v1_1" / "optimization" / f"{run_id}_simulation_intent.json"
                atomic_write_json(intent,{"status":"OUTCOME_UNKNOWN"})
                raise RuntimeError("response lost after submission")
            def settle(gateway,state_root,intent,**kwargs):
                calls.append(("recover",Path(intent).stem))
                self.assertTrue(Path(intent).is_file())
                return {"candidate_alpha_id":"OPT1","candidate_status":"EVIDENCE_PENDING"}
            with patch("alpha_research.workflows.regular.dataset_review.run_optimization",
                       side_effect=uncertain) as optimize, \
                 patch("alpha_research.workflows.regular.dataset_review.recover_simulation_intent",
                       side_effect=settle) as recover:
                first=review_dataset(agent, object(), root, directory,
                                     request,"risk60",[receipt],[])
                self.assertEqual(first["status"],"optimization_pending")
                second=review_dataset(agent, object(), root, directory,
                                      request,"risk60",[receipt],[])
                self.assertEqual(second["status"],"complete")
                self.assertEqual(second["optimization"]["candidate_alpha_id"],"OPT1")
                self.assertEqual(review_dataset(agent, object(),root,directory,
                                                 request,"risk60",[receipt],[]),second)
                optimize.assert_called_once()
                recover.assert_called_once()
                self.assertEqual(len([x for x in agent.calls if x[0]=="decide"]),1)

    def test_missing_optimization_intent_fails_closed_without_replaying(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            agent,directory,receipt,request=setup_case(root,"A1")
            with patch("alpha_research.workflows.regular.dataset_review.run_optimization",
                       side_effect=RuntimeError("crashed before intent")) as execute:
                first=review_dataset(agent, object(), root, directory,
                                     request,"risk60",[receipt],[])
                self.assertEqual(first["status"],"optimization_pending")
                with self.assertRaisesRegex(DatasetReviewError,"intent is missing"):
                    review_dataset(agent, object(), root, directory,
                                   request,"risk60",[receipt],[])
                execute.assert_called_once()

    def test_review_root_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            agent,directory,receipt,request=setup_case(root)
            with self.assertRaisesRegex(DatasetReviewError,"directory"):
                review_dataset(agent, object(),root, root / "other", request,
                               "risk60",[receipt],[])


if __name__ == "__main__":
    unittest.main()
