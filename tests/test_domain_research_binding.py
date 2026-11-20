"""Prove the three actual domain entrypoints bind IDs before gateway work.

The callback intentionally aborts each workflow. No model is built, no
gateway request can be made, and no BRAIN credential is read.
"""
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from alpha_research.workflows.regular.campaign import run_regular_campaign
from alpha_research.workflows.super_alpha.workflow import run_super_research
from alpha_research.workflows.optimization.workflow import run_optimization


class AbortAfterIdentity(Exception):
    pass


class NoGateway:
    def call(self, *args, **kwargs):
        raise AssertionError("gateway invoked before Research ID binding")


class DomainBindingTests(unittest.TestCase):
    def test_all_domain_entrypoints_bind_before_any_gateway_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            observed = []
            def bind(value):
                observed.append(value)
                raise AbortAfterIdentity()

            context = {"bind_research_identity": bind}
            agent = SimpleNamespace(runs_root=root / "runs", stop_flag=None)
            with self.assertRaises(AbortAfterIdentity):
                run_regular_campaign(
                    agent, NoGateway(), objective="offline identity", region="CHN",
                    delay=1, max_batches=1, execution_context=context)
            self.assertEqual(len(observed), 1)
            self.assertRegex(observed.pop(), r"^regular:campaign:[0-9a-f]{32}$")

            with self.assertRaises(AbortAfterIdentity):
                run_super_research(
                    NoGateway(), None, {}, root,
                    region="CHN", objective="offline identity", delay=1,
                    execution_context=context)
            self.assertEqual(len(observed), 1)
            self.assertRegex(observed.pop(), r"^super:[0-9a-f]{32}$")

            with self.assertRaises(AbortAfterIdentity):
                run_optimization(
                    NoGateway(), None, {}, root,
                    alpha_id="synthetic-source", objective="offline identity",
                    region="CHN", execution_context=context)
            self.assertEqual(len(observed), 1)
            self.assertRegex(observed.pop(), r"^optimization:[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
