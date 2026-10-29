import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant import gatekeeper
from nastu_quant import agent as agent_module
from nastu_quant.validator import validate


class TestPpaRules(unittest.TestCase):
    def test_explicit_ppa_objective_is_case_insensitive_and_negation_is_not_a_request(self):
        self.assertTrue(hasattr(agent_module, "ppa_requested"))
        for objective in ("探索PPA", "研究 ppa 因子", "Explore Power Pool Alpha", "探索 PPA 作为对照"):
            with self.subTest(objective=objective):
                self.assertTrue(agent_module.ppa_requested(objective))
        for objective in ("研究基本面 Alpha", "不要探索PPA", "不要探索PPA（Power Pool Alpha）",
                          "do not explore ppa", "do not explore ppa (Power Pool Alpha)",
                          "PPA 不是研究目标", "PPA 不作为目标", "以 PPA 为参考，探索普通 Alpha",
                          "参考 PPA 规则，探索普通 Alpha",
                          "Use PPA as a reference and explore regular Alpha"):
            with self.subTest(objective=objective):
                self.assertFalse(agent_module.ppa_requested(objective))

    def test_ppa_structural_limits_are_inclusive(self):
        self.assertIn("ppa", inspect.signature(validate).parameters)
        catalog = {name: {"type": "MATRIX", "dataset": "data"} for name in ("a", "b", "c", "d")}
        for count, expected in ((8, "PASS"), (9, "REJECT")):
            expression = "rank(" * count + "a" + ")" * count
            result = validate(expression, "data", catalog, {"rank"}, ppa=True)
            self.assertEqual(result["verdict"], expected, result["errors"])
        expression = "rank(max(max(a, b), max(c, d)))"
        result = validate(expression, "data", catalog, {"rank", "max"}, ppa=True)
        self.assertIn("E_TOO_MANY_FIELDS", {error["code"] for error in result["errors"]})
        result = validate("rank(max(max(a, b), c))", "data", catalog, {"rank", "max"}, ppa=True)
        self.assertEqual(result["verdict"], "PASS", result["errors"])

    def test_ppa_three_criteria_and_missing_evidence(self):
        self.assertTrue(hasattr(gatekeeper, "evaluate_ppa"))
        evaluate = gatekeeper.evaluate_ppa
        self.assertEqual(evaluate(8, 3, 1.0)["status"], "PASS")
        for values, failure in (((9, 3, 1.0), "operator_count"),
                                ((8, 4, 1.0), "field_count"),
                                ((8, 3, 0.999), "sharpe")):
            with self.subTest(values=values):
                result = evaluate(*values)
                self.assertEqual(result["status"], "FAIL")
                self.assertIn(failure, result["failures"])
        for values in ((None, 3, 1.0), (8, None, 1.0), (8, 3, None),
                       (True, 3, 1.0), (8, 3, float("nan"))):
            with self.subTest(values=values):
                self.assertEqual(evaluate(*values)["status"], "PENDING")
