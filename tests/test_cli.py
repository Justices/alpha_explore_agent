import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent


def cli(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-m", "nastu_quant", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=10)


class TestCli(unittest.TestCase):
    def test_status_and_stop_need_no_platform_session(self):
        with tempfile.TemporaryDirectory() as temp:
            first = cli("--state-root", temp, "status")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertFalse(json.loads(first.stdout)["stopped"])
            stopped = cli("--state-root", temp, "stop")
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            second = cli("--state-root", temp, "status")
            self.assertTrue(json.loads(second.stdout)["stopped"])
            resumed = cli("--state-root", temp, "resume")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            third = cli("--state-root", temp, "status")
            self.assertFalse(json.loads(third.stdout)["stopped"])

    def test_draft_status_and_void_need_no_platform_session(self):
        with tempfile.TemporaryDirectory() as temp:
            batch = Path(temp) / "runs" / "GLB" / "draft"
            batch.mkdir(parents=True)
            (batch / "batch_context.json").write_text("{}", encoding="utf-8")
            before = cli("--state-root", temp, "status")
            self.assertIn(str(batch), json.loads(before.stdout)["unfinished_batches"])
            voided = cli("--state-root", temp, "void", str(batch),
                         "--reason", "invalid model proposal")
            self.assertEqual(voided.returncode, 0, voided.stderr)
            after = cli("--state-root", temp, "status")
            self.assertEqual(json.loads(after.stdout)["unfinished_batches"], [])


if __name__ == "__main__":
    unittest.main()
