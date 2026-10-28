"""Offline protocol checks against isolated brain-flow script fixtures."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent.parent
DRIVER = ROOT / "src" / "nastu_quant" / "driver.py"
CASE = {"cmd": "set_case", "case_key": "case_1", "run_id": "run_1"}
SETTINGS = {
    "instrument_type": "EQUITY", "region": "GLB", "universe": "TOP3000",
    "delay": 1, "decay": 4, "neutralization": "MARKET", "truncation": 0.08,
    "pasteurization": "OFF", "unit_handling": "VERIFY", "nan_handling": "OFF",
    "max_trade": "OFF", "language": "FASTEXPR", "visualization": False,
    "test_period": "P0Y0M",
}

FAKE_SESSION = r'''
class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self.payload

class Session:
    def get(self, url, timeout):
        return Response({"id": url.rsplit("/", 1)[-1], "universe": "TOP3000"})
    def options(self, url, timeout):
        return Response({"actions": {"POST": {"settings": {"children": {
            "decay": {"maxValue": 50}
        }}}}})

class Client:
    base_url = "https://example.invalid"
    session = Session()

class Platform:
    async def get_alpha_details(self, alpha_id):
        print("platform progress")
        return {"id": alpha_id}

async def ensure_authenticated(client=None):
    return client or Client()

def get_platform_module():
    return Platform()
'''

FAKE_CATALOG = r'''
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
for name in ("region", "universe", "delay", "dataset", "output-dir", "out"):
    parser.add_argument("--" + name)
args, _ = parser.parse_known_args()
metadata = {"region": args.region, "universe": args.universe,
            "delay": int(args.delay) if args.delay is not None else None,
            "status": "complete"}
name = Path(__file__).name
if name == "fetch_datasets.py":
    out = Path(args.output_dir) / "raw" / (
        f"brain_{args.region.lower()}_equity_d{args.delay}_{args.universe.lower()}_datasets_raw.json")
    payload = {"metadata": metadata, "raw_payload": {"results": [{"id": "analyst69"}]}}
elif name == "inspect_fields.py":
    out = Path(args.out)
    payload = {"metadata": metadata, "fields": [{"id": "field_a"}]}
else:
    out = Path(args.output_dir) / "processed" / "brain_operators.json"
    payload = {"operators": [{"name": "rank"}]}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload), encoding="utf-8")
'''

FAKE_SIMULATION = r'''
import argparse
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--file")
parser.add_argument("--out")
parser.add_argument("--resume")
args, _ = parser.parse_known_args()
variants = json.loads(Path(args.file).read_text(encoding="utf-8"))
calls_path = Path(__file__).parent.parent / "simulation_calls.json"
calls = json.loads(calls_path.read_text(encoding="utf-8")) if calls_path.exists() else []
calls.append({"resume": args.resume, "variants": variants})
calls_path.write_text(json.dumps(calls), encoding="utf-8")
if os.getenv("FAKE_INTERRUPT") == "1" and not args.resume:
    print("submitted https://api.worldquantbrain.com/simulations/parent1", flush=True)
    sys.exit(1)
rows = [{"name": variant["name"], "expression": variant["expression"],
         "settings": variant["settings"], "alpha_id": f"A{index + 1}",
         "success": True, "is": {"sharpe": 1.8}}
        for index, variant in enumerate(variants)]
Path(args.out).write_text(json.dumps(rows), encoding="utf-8")
'''


class TestDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "brain-flow"
        scripts = self.home / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "brain_session.py").write_text(FAKE_SESSION, encoding="utf-8")
        (scripts / "simulate_batch.py").write_text(FAKE_SIMULATION, encoding="utf-8")
        for name in ("fetch_datasets.py", "inspect_fields.py", "fetch_brain_operators.py"):
            (scripts / name).write_text(FAKE_CATALOG, encoding="utf-8")

    def run_driver(self, commands, *, configured=True, interrupt=False):
        env = dict(os.environ)
        for name in ("BRAIN_FLOW_HOME", "BRAIN_PYTHON", "BRAIN_CREDENTIALS_FILE",
                     "BIGMODEL_API_KEY"):
            env.pop(name, None)
        if configured:
            env["BRAIN_FLOW_HOME"] = str(self.home)
            env["BRAIN_PYTHON"] = sys.executable
        if interrupt:
            env["FAKE_INTERRUPT"] = "1"
        else:
            env.pop("FAKE_INTERRUPT", None)
        proc = subprocess.run(
            [sys.executable, str(DRIVER)],
            input="".join(json.dumps(item) + "\n" for item in commands),
            capture_output=True, text=True, env=env, timeout=15)
        return proc, [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_missing_configuration_fails_before_session_start(self):
        proc, responses = self.run_driver([], configured=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(responses[0]["stage"], "configuration")
        self.assertIn("BRAIN_FLOW_HOME", responses[0]["error"])

    def test_case_and_whitelist_are_enforced_in_protocol(self):
        proc, responses = self.run_driver([
            CASE, {"cmd": "create_simulation"}, {"cmd": "create_multi_simulation"},
            {"cmd": "submit_alpha"}, {"cmd": "shutdown"}])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(responses[0]["stage"], "ready")
        self.assertTrue(Path(responses[1]["result"]["run_root"]).is_relative_to(self.home / "runs"))
        self.assertTrue(all(not item["ok"] for item in responses[2:5]))
        self.assertEqual(responses[5]["stage"], "shutdown")

    def test_catalog_calls_use_case_scoped_script_outputs(self):
        scope = {"region": "GLB", "universe": "TOP3000", "delay": 1}
        _, responses = self.run_driver([
            CASE, {"cmd": "get_datasets", **scope},
            {"cmd": "get_datafields", "dataset_id": "analyst69", **scope},
            {"cmd": "get_operators", **scope}, {"cmd": "shutdown"}])
        self.assertEqual(responses[2]["result"], [{"id": "analyst69"}])
        self.assertEqual(responses[3]["result"], [{"id": "field_a"}])
        self.assertEqual(responses[4]["result"], [{"name": "rank"}])

    def test_shared_session_reads_and_platform_progress_preserve_json_protocol(self):
        _, responses = self.run_driver([
            {"cmd": "get_dataset_details", "dataset_id": "analyst14"},
            {"cmd": "get_dataset_details", "dataset_id": "../alphas"},
            {"cmd": "get_simulation_settings_schema"},
            {"cmd": "get_alpha_details", "alpha_id": "A1"}, {"cmd": "shutdown"}])
        self.assertEqual(responses[1]["result"]["id"], "analyst14")
        self.assertFalse(responses[2]["ok"])
        self.assertEqual(responses[3]["result"]["decay"]["maxValue"], 50)
        self.assertEqual(responses[4]["result"]["id"], "A1")
        self.assertEqual(responses[5]["stage"], "shutdown")

    def test_interrupted_runner_resumes_existing_url_and_reuses_result(self):
        runner = {"case_key": "case_1", "run_id": "run_1",
                  "batch_id": "20261028_120000_abcdef12", "label": "production"}
        simulate = {"cmd": "simulate_batch", "batch_id": runner["batch_id"],
                    "label": runner["label"], "dataset_id": "analyst69",
                    "settings": SETTINGS,
                    "variants": [{"name": "variant_1", "expression": "rank(field_a)",
                                  "research": {"idea": "fixture"}}]}
        _, responses = self.run_driver([
            CASE, simulate, {"cmd": "resume_simulation_batch", **runner},
            {"cmd": "resume_simulation_batch", **runner}, {"cmd": "shutdown"}],
            interrupt=True)
        self.assertFalse(responses[2]["ok"])
        self.assertEqual(responses[3]["result"]["results"][0]["alpha_id"], "A1")
        self.assertEqual(responses[4]["result"]["results"][0]["alpha_id"], "A1")
        calls = json.loads((self.home / "simulation_calls.json").read_text(encoding="utf-8"))
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0]["resume"])
        self.assertEqual(calls[1]["resume"],
                         "https://api.worldquantbrain.com/simulations/parent1")
        self.assertEqual(calls[0]["variants"][0]["settings"],
                         {"pasteurization": "OFF", "testPeriod": "P0Y0M"})


if __name__ == "__main__":
    unittest.main()
