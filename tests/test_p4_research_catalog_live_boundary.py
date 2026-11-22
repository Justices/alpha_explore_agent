"""P4.19 two-sided research Catalog enforcement with no live BRAIN startup."""
import io
import json
import unittest
from unittest.mock import patch

from alpha_research.integrations.brain import driver
from alpha_research.integrations.brain.gateway import Gateway, GatewayError

SCOPE = {"region": "USA", "delay": 1, "universe": "TOP3000",
         "instrument_type": "EQUITY"}


class ResearchCatalogWireTests(unittest.TestCase):
    def test_driver_frame_exact_read_policy_before_dispatch(self):
        self.assertTrue(driver.research_catalog_frame_allowed(
            {"cmd": "get_datasets", **SCOPE}, SCOPE))
        self.assertTrue(driver.research_catalog_frame_allowed(
            {"cmd": "get_user_alphas", "stage": "OS", "limit": 100, "offset": 0},
            SCOPE))
        self.assertFalse(driver.research_catalog_frame_allowed(
            {"cmd": "get_alpha_details", "alpha_id": "alpha1"}, SCOPE))
        self.assertTrue(driver.research_catalog_frame_allowed(
            {"cmd": "get_alpha_details", "alpha_id": "alpha1"}, SCOPE,
            source_alpha_id="alpha1"))
        malformed = [
            {}, {"cmd": "simulate_super", "grant_id": "a" * 32},
            {"cmd": "tag_alpha", "alpha_id": "x"},
            {"cmd": "set_case", "case_key": "x", "run_id": "x"},
            {"cmd": "get_alpha_details", "alpha_id": "../alpha"},
            {"cmd": "get_datasets", **SCOPE, "grant_id": "b" * 32},
            {"cmd": "get_datasets", **{**SCOPE, "region": "CHN"}},
            {"cmd": "get_datasets", **{**SCOPE, "delay": True}},
            {"cmd": "get_datasets", **{**SCOPE, "universe": "TOP1000"}},
            {"cmd": "get_user_alphas", "stage": "IS", "limit": 100, "offset": 0},
            {"cmd": "get_user_alphas", "stage": "OS", "limit": 101, "offset": 0},
            {"cmd": "get_user_alphas", "stage": "OS", "limit": 100, "offset": -1},
            {"cmd": "get_platform_setting_options", "permission_to_dispatch": False},
            {"cmd": "get_datafields", **SCOPE, "dataset_id": "../secret"},
        ]
        for frame in malformed:
            with self.subTest(frame=frame):
                self.assertFalse(driver.research_catalog_frame_allowed(frame, SCOPE))

    def test_driver_mode_refuses_before_credentials_and_runtime(self):
        from alpha_research.integrations.brain import driver as d
        for argv in (
            ["driver.py", "--research-catalog"],
            ["driver.py", "--research-catalog", "--brain-credentials-stdin"],
        ):
            with self.subTest(argv=argv):
                output = io.StringIO()
                with (patch.object(d.sys, "argv", argv),
                      patch.object(d.sys, "stdin", io.StringIO('{"secret":"x"}\n')),
                      patch.object(d.sys, "stdout", output),
                      patch.object(d, "PlatformRuntime",
                                   side_effect=AssertionError("live runtime forbidden")),
                      patch.object(d, "WorldQuantClient",
                                   side_effect=AssertionError("credentials forbidden"))):
                    code = d.main()
                self.assertEqual(code, 2)
                rows = [json.loads(z) for z in output.getvalue().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["stage"], "admission")
                self.assertFalse(rows[0]["ok"])
                self.assertNotIn("secret", output.getvalue())
        for other in ("--read-only", "--orchestration-guarded"):
            out = io.StringIO()
            with (patch.object(d.sys, "argv",
                               ["driver.py", "--research-catalog", other]),
                  patch.object(d.sys, "stdout", out),
                  patch.object(d, "PlatformRuntime",
                               side_effect=AssertionError("runtime forbidden"))):
                self.assertEqual(d.main(), 2)
            self.assertEqual(json.loads(out.getvalue())["stage"], "configuration")

    def test_gateway_research_profile_fails_before_process_and_network(self):
        gw = Gateway(research_catalog_scope=dict(SCOPE))
        calls = []
        def fail_spawn(*args, **kwargs):
            calls.append("spawn")
            raise AssertionError("no live subprocess")
        with patch("alpha_research.integrations.brain.gateway.subprocess.Popen",
                   side_effect=fail_spawn):
            with self.assertRaisesRegex(GatewayError, "independent grant consumer"):
                gw.start()
            for cmd, params in (
                ("get_datasets", SCOPE),
                ("get_platform_setting_options", {}),
                ("get_user_alphas", {"stage": "OS", "limit": 100, "offset": 0}),
            ):
                with self.subTest(cmd=cmd):
                    with self.assertRaisesRegex(GatewayError, "independent grant consumer"):
                        gw.call(cmd, **params)
            for cmd, params in (
                ("simulate_super", {}),
                ("tag_alpha", {"alpha_id": "a"}),
                ("get_datasets", {**SCOPE, "region": "CHN"}),
                ("get_user_alphas", {"stage": "IS", "limit": 100, "offset": 0}),
                ("get_alpha_details", {"alpha_id": "alpha1"}),
            ):
                with self.subTest(cmd=cmd):
                    with self.assertRaisesRegex(GatewayError, "not admitted"):
                        gw.call(cmd, **params)
        self.assertEqual(calls, [])
        self.assertIsNone(gw.proc)

    def test_gateway_exact_source_binding_and_invalid_configuration(self):
        for kwargs in (
            {"research_catalog_scope": {**SCOPE, "delay": True}},
            {"research_catalog_scope": {**SCOPE, "region": "CHN\"junk"}},
            {"research_catalog_source_alpha_id": "alpha1"},
            {"research_catalog_scope": SCOPE, "read_only": True},
            {"research_catalog_scope": SCOPE, "orchestration_guarded": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(GatewayError):
                    Gateway(**kwargs)
        gw = Gateway(research_catalog_scope=dict(SCOPE),
                     research_catalog_source_alpha_id="alpha1")
        with self.assertRaisesRegex(GatewayError, "independent grant consumer"):
            gw.call("get_alpha_details", alpha_id="alpha1")
        with self.assertRaisesRegex(GatewayError, "not admitted"):
            gw.call("get_alpha_details", alpha_id="alpha2")


if __name__ == "__main__":
    unittest.main()
