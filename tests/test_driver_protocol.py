"""Driver protocol tests (offline, fake platform_functions):
FR-01 cascade (credential file first run, cookie second run),
whitelist enforcement (submit must be impossible), reauth, needs_auth.
"""
import json
import sysconfig
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
DRIVER = ROOT / "src" / "nastu_quant" / "driver.py"
FAKE_PF = Path(__file__).parent / "fixtures" / "fake_platform_functions.py"


def run_driver(state_dir, cred_file, commands, timeout=30):
    env = dict(os.environ)
    env["NASTU_QUANT_STATE_DIR"] = str(state_dir)
    env["NASTU_QUANT_PF_PATH"] = str(FAKE_PF)
    if cred_file:
        env["NASTU_QUANT_CREDENTIAL_FILE"] = str(cred_file)
    proc = subprocess.Popen(
        [sys.executable, str(DRIVER)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True, env=env)
    out, _ = proc.communicate(
        input="".join(json.dumps(c) + "\n" for c in commands), timeout=timeout)
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


class TestDriver(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        base = Path(self.td.name)
        self.state = base / "state"
        self.cred = base / "cred.json"
        self.cred.write_text(json.dumps(
            {"email": "a@b.c", "password": "fake-password"}), encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def test_cascade_whitelist_reauth_and_cookie_reuse(self):
        resps = run_driver(self.state, self.cred, [
            {"cmd": "get_datasets"},
            {"cmd": "submit_alpha", "alpha_id": "X"},     # must be impossible
            {"cmd": "reauth"},                            # no credentials sent
            {"cmd": "shutdown"},
        ])
        self.assertTrue(resps[0]["ok"])
        self.assertEqual(resps[0]["via"], "credential_file")
        self.assertEqual(resps[1]["result"], [{"id": "risk60"}, {"id": "pv47"}])
        self.assertFalse(resps[2]["ok"])
        self.assertIn("disallowed", resps[2]["error"])
        self.assertEqual(resps[3]["stage"], "reauthenticated")
        cookie = self.state / "session_cookie.json"
        self.assertTrue(cookie.exists())
        if os.name == "nt":
            acl = subprocess.run(["icacls", str(cookie)], capture_output=True,
                                 text=True, check=True).stdout.lower()
            self.assertNotIn("builtin\\users:", acl)
            self.assertNotIn("everyone:", acl)
        else:
            self.assertEqual(oct(cookie.stat().st_mode & 0o777), "0o600")

        # Second process: cookie must authenticate without the cred file.
        resps2 = run_driver(self.state, None, [{"cmd": "shutdown"}])
        self.assertEqual(resps2[0]["via"], "cookie")

    def test_bad_credentials_fail_closed(self):
        self.cred.write_text(json.dumps(
            {"email": "a@b.c", "password": "wrong-password"}), encoding="utf-8")
        resps = run_driver(self.state, self.cred, [])
        self.assertFalse(resps[0]["ok"])
        self.assertEqual(resps[0]["stage"], "auth")
        self.assertTrue((self.state / "needs_auth").exists())

    def test_no_cookie_no_credfile_needs_auth(self):
        resps = run_driver(self.state, None, [])
        self.assertFalse(resps[0]["ok"])
        self.assertIn("needs_auth", resps[0]["error"])

    def test_platform_progress_does_not_corrupt_json_protocol(self):
        resps = run_driver(self.state, self.cred, [
            {"cmd": "create_multi_simulation", "alpha_expressions": ["x", "y"]},
            {"cmd": "shutdown"},
        ])
        self.assertEqual(resps[1]["result"], {"id": "sim-multi"})
        self.assertEqual(resps[2]["stage"], "shutdown")

    def test_driver_returns_all_field_pages(self):
        resps = run_driver(self.state, self.cred, [
            {"cmd": "get_datafields", "region": "GLB", "delay": 1,
             "universe": "TOP3000", "dataset_id": "risk60"},
            {"cmd": "shutdown"},
        ])
        self.assertEqual([row["id"] for row in resps[1]["result"]["results"]],
                         ["field_a", "field_b", "field_c"])
        self.assertTrue(resps[1]["result"]["complete"])

    def test_dataset_detail_uses_authenticated_single_dataset_endpoint(self):
        resps = run_driver(self.state, self.cred, [
            {"cmd": "get_dataset_details", "dataset_id": "analyst14"},
            {"cmd": "get_dataset_details", "dataset_id": "../alphas"},
            {"cmd": "shutdown"},
        ])
        self.assertEqual(resps[1]["result"]["id"], "analyst14")
        self.assertEqual(resps[1]["result"]["universe"], "TOP3000")
        self.assertFalse(resps[2]["ok"])

    def test_simulation_settings_schema_comes_from_live_options(self):
        resps = run_driver(self.state, self.cred, [
            {"cmd": "get_simulation_settings_schema"}, {"cmd": "shutdown"},
        ])
        schema = resps[1]["result"]
        self.assertEqual(schema["decay"]["max_value"], 50)
        self.assertEqual(schema["truncation"]["max_value"], 1)
        self.assertEqual(schema["pasteurization"]["choices"][0]["value"], "ON")

    def test_default_platform_module_resolves_in_selected_interpreter(self):
        installed = Path(sysconfig.get_paths()["purelib"]) / "cnhkmcp" / "untracked" / "platform_functions.py"
        if not installed.exists():
            self.skipTest("cnhkmcp is not installed in this interpreter")
        env = dict(os.environ)
        env.pop("NASTU_QUANT_PF_PATH", None)
        env.pop("NASTU_QUANT_CREDENTIAL_FILE", None)
        env["NASTU_QUANT_STATE_DIR"] = str(self.state)
        proc = subprocess.run([sys.executable, str(DRIVER)], input="",
                              capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.splitlines()[0])["stage"], "auth")


if __name__ == "__main__":
    unittest.main()
