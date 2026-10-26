"""Driver protocol tests (offline, fake platform_functions):
FR-01 cascade (credential file first run, cookie second run),
whitelist enforcement (submit must be impossible), reauth, needs_auth.
"""
import json
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


if __name__ == "__main__":
    unittest.main()
