"""Offline driver checks without any other repository or credentials."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
DRIVER = ROOT / "src" / "nastu_quant" / "driver.py"


class TestDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_driver(self, commands):
        env = dict(os.environ)
        env["NASTU_QUANT_STATE_DIR"] = self.tmp.name
        env["BRAIN_FLOW_HOME"] = str(Path(self.tmp.name) / "absent-reference-repository")
        env["BRAIN_PYTHON"] = str(Path(self.tmp.name) / "absent-interpreter")
        env.pop("BRAIN_CREDENTIALS_FILE", None)
        proc = subprocess.run([sys.executable, str(DRIVER)],
            input="".join(json.dumps(item) + "\n" for item in commands),
            capture_output=True, text=True, env=env, timeout=45)
        return proc, [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_driver_starts_without_reference_repository_or_interpreter(self):
        proc, responses = self.run_driver([
            {"cmd": "set_case", "case_key": "case_1", "run_id": "run_1"},
            {"cmd": "create_simulation"}, {"cmd": "submit_alpha"},
            {"cmd": "shutdown"}])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(responses[0]["stage"], "ready")
        self.assertTrue(Path(responses[1]["result"]["run_root"]).is_relative_to(Path(self.tmp.name)))
        self.assertFalse(responses[2]["ok"])
        self.assertFalse(responses[3]["ok"])
        self.assertEqual(responses[4]["stage"], "shutdown")

    def test_missing_platform_credentials_fails_without_network(self):
        proc, responses = self.run_driver([
            {"cmd": "get_dataset_details", "dataset_id": "analyst69"},
            {"cmd": "shutdown"}])
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(responses[1]["ok"])
        self.assertIn("BRAIN_CREDENTIALS_FILE", responses[1]["error"])
        self.assertEqual(responses[2]["stage"], "shutdown")


if __name__ == "__main__":
    unittest.main()
