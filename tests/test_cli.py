import hashlib
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

    def test_knowledge_note_cli_records_and_lists_exact_notes(self):
        with tempfile.TemporaryDirectory() as temp:
            content = Path(temp) / "note.md"
            content.write_text("Exact ts_mean semantics.", encoding="utf-8")
            added = cli("--state-root", temp, "knowledge", "note", "put",
                        "--kind", "operator", "--key", "ts_mean",
                        "--content-file", str(content),
                        "--source", "operator handbook#ts_mean",
                        "--operator", "researcher")
            self.assertEqual(added.returncode, 0, added.stderr)
            listing = cli("--state-root", temp, "knowledge", "list",
                          "--kind", "operator")
            self.assertEqual(listing.returncode, 0, listing.stderr)
            notes = json.loads(listing.stdout)["operator_notes"]
            self.assertEqual(notes["ts_mean"]["content"], "Exact ts_mean semantics.")

    def test_remedy_cli_requires_review_before_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            batch_dir = (Path(temp) / "runs" / "GLB" / "TOP300" / "1" /
                         "dataset-1" / "20261027_120000_abcd1234")
            batch_dir.mkdir(parents=True)
            prereg_path = batch_dir / "prereg.json"
            prereg_path.write_text(json.dumps({"objective": "CLI evidence fixture"}),
                                   encoding="utf-8")
            digest = hashlib.sha256(prereg_path.read_bytes()).hexdigest()
            (batch_dir / "state.json").write_text(json.dumps({
                "prereg_hashes": {str(prereg_path): digest},
            }), encoding="utf-8")
            (batch_dir / "simulation_intent.json").write_text(json.dumps({
                "status": "READ_BACK", "alpha_ids": ["alpha-1"],
            }), encoding="utf-8")
            readback = {"alpha-1": {
                "details": {"id": "alpha-1", "is": {"turnover": 0.25}},
                "checks": {"checks": []},
            }}
            gate = {"alpha-1": {"status": "FAIL", "metrics": {"turnover": 0.25}}}
            (batch_dir / "simulation_raw.json").write_text(json.dumps({
                "alpha_results": [{"alpha_id": "alpha-1"}],
            }), encoding="utf-8")
            (batch_dir / "gate.json").write_text(json.dumps(gate), encoding="utf-8")
            (batch_dir / "results.json").write_text(json.dumps({
                "readback": readback, "gate": gate,
            }), encoding="utf-8")
            remedy_file = Path(temp) / "remedy.json"
            remedy_file.write_text(json.dumps({
                "remedy_id": "R-CLI", "applies_to": ["S_TURNOVER_HI"],
                "preconditions": {}, "action": "smooth the field",
                "rationale": "reduce sparse jumps", "expected_effect": "lower turnover",
                "evidence": [{"batch": str(batch_dir), "alpha_id": "alpha-1",
                              "measured_change": "turnover 0.42 to 0.25"}],
            }), encoding="utf-8")
            added = cli("--state-root", temp, "knowledge", "remedy", "add",
                        "--file", str(remedy_file), "--operator", "researcher")
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertEqual(json.loads(added.stdout)["status"], "pending_review")
            reviewed = cli("--state-root", temp, "knowledge", "remedy", "review",
                           "--id", "R-CLI", "--status", "verified",
                           "--reviewer", "researcher", "--reason", "evidence checked")
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            self.assertEqual(json.loads(reviewed.stdout)["status"], "verified")


if __name__ == "__main__":
    unittest.main()
