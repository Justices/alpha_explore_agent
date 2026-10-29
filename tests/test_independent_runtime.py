"""Offline checks for the agent's own BRAIN runtime."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.platform_runtime import PlatformError, PlatformRuntime, WorldQuantClient
from nastu_quant.agent import ResearchAgent
from nastu_quant.backtest_cache import BacktestCache
from nastu_quant.config import DEFAULT_CONFIG
from nastu_quant.gatekeeper import evaluate_candidate, parse_correlations
from nastu_quant.gateway import Gateway


SETTINGS = {
    "instrument_type": "EQUITY", "region": "GLB", "universe": "TOP3000",
    "delay": 1, "decay": 4, "neutralization": "MARKET", "truncation": 0.08,
    "pasteurization": "ON", "unit_handling": "VERIFY", "nan_handling": "OFF",
    "max_trade": "OFF", "language": "FASTEXPR", "visualization": False,
    "test_period": "P0Y0M",
}
RESEARCH = {name: "evidence" for name in (
    "idea", "data_rationale", "operator_rationale", "field_relationship",
    "settings_rationale", "expected_behavior", "failure_criterion")}


def settings_schema():
    def by_region(values):
        return {"instrumentType": {"EQUITY": {"region": {"GLB":
            [{"value": value} for value in values]}}}}
    return {
        "instrumentType": {"choices": [{"value": "EQUITY"}]},
        "region": {"choices": {"instrumentType": {"EQUITY": [{"value": "GLB"}]}}},
        "delay": {"choices": by_region([1])}, "universe": {"choices": by_region(["TOP3000"])},
        "neutralization": {"choices": by_region(["MARKET", "NONE"])},
        "decay": {"minValue": 0, "maxValue": 512},
        "truncation": {"minValue": 0, "maxValue": 1},
        "pasteurization": {"choices": [{"value": "ON"}, {"value": "OFF"}]},
        "unitHandling": {"choices": [{"value": "VERIFY"}]},
        "nanHandling": {"choices": [{"value": "OFF"}]},
        "maxTrade": {"choices": [{"value": "OFF"}]},
        "testPeriod": {"type": "string", "minValue": "P0Y0M0D", "maxValue": "P6Y0M0D"},
    }


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeClient:
    base_url = "https://api.worldquantbrain.com"

    def __init__(self):
        self.calls = []
        self.post_error = None
        self.poll_error = None
        self.multi = False
        self.single_alpha_ids = []
        self.active_alpha = "A1"
        self.sharpe_by_alpha = {}
        self.expressions_by_alpha = {}
        self.pnl = {}
        self.os_alphas = []
        self.include_risk = False
        self.risk = None
        self.ra_result = "PASS"
        self.ra_results = []
        self.correlation_checks = []
        self.correlation_responses = {}

    def request(self, method, path, *, params=None, json=None):
        self.calls.append((method, path, params, json))
        if method == "OPTIONS" and path == "/simulations":
            return Response({"actions": {"POST": {"settings": {"children": settings_schema()}}}})
        if path == "/data-sets":
            return Response({"count": 1, "results": [{"id": "analyst69"}]})
        if path == "/data-sets/analyst69":
            return Response({"id": "analyst69", "data": [
                {"region": "GLB", "universe": "TOP3000", "delay": 1}]})
        if path == "/data-fields":
            return Response({"count": 1, "results": [{"id": "field_a", "type": "MATRIX",
                "dataset": {"id": "analyst69"}, "description": "fixture", "coverage": 1.0,
                "zeroFraction": 0.1, "updateFrequency": "daily", "units": "score",
                "median": 0.5, "distribution": "bounded"}]})
        if path == "/operators":
            return Response([
                {"name": "rank", "scope": ["REGULAR"], "definition": "rank(x, rate=2)"},
                {"name": "ts_mean", "scope": ["REGULAR"], "definition": "ts_mean(x, d)"},
                {"name": "combo_a", "scope": ["COMBO"], "definition": "combo_a(x)"},
            ])
        if path == "/users/self/alphas":
            offset = (params or {}).get("offset", 0)
            limit = (params or {}).get("limit", 50)
            return Response({"count": len(self.os_alphas),
                             "results": self.os_alphas[offset:offset + limit]})
        if method == "POST" and path == "/simulations":
            if self.post_error:
                raise self.post_error
            self.multi = isinstance(json, list)
            if not self.multi and self.single_alpha_ids:
                self.active_alpha = self.single_alpha_ids.pop(0)
            if not self.multi:
                self.expressions_by_alpha[self.active_alpha] = json["regular"]
            return Response({}, status=201, headers={"Location": "/simulations/S1"})
        if path.endswith("/simulations/S1") or path == "/simulations/S1":
            if self.poll_error:
                error, self.poll_error = self.poll_error, None
                raise error
            return Response({"status": "COMPLETE", "children": ["S2", {"id": "S3"}]}
                            if self.multi else {"status": "COMPLETE", "alpha": self.active_alpha})
        if path in {"/simulations/S2", "/simulations/S3"}:
            return Response({"status": "COMPLETE", "alpha": "A1" if path.endswith("S2") else "A2"})
        if path in {"/alphas/A1", "/alphas/A2", "/alphas/A3"}:
            ra_result = self.ra_results.pop(0) if self.ra_results else self.ra_result
            keys = {"instrument_type": "instrumentType", "unit_handling": "unitHandling",
                    "nan_handling": "nanHandling", "test_period": "testPeriod",
                    "max_trade": "maxTrade"}
            alpha_id = path.rsplit("/", 1)[-1]
            details = {"id": alpha_id, "type": "REGULAR",
                "dateCreated": datetime.now(timezone.utc).isoformat(),
                "regular": {"code": self.expressions_by_alpha.get(
                    alpha_id, "rank(field_a)" if alpha_id == "A1" else "rank(ts_mean(field_a, 5))")},
                "settings": {keys.get(k, k): v for k, v in SETTINGS.items()},
                "is": {"sharpe": 1.8, "fitness": 1.2, "sharpe_2y": 1.8,
                       "margin": 0.001, "turnover": 0.2,
                       "checks": [{"name": "LOW_SHARPE", "result": ra_result}]}}
            if self.include_risk:
                details["is"]["riskNeutralized"] = self.risk
            if details["id"] in self.sharpe_by_alpha:
                details["is"]["sharpe"] = self.sharpe_by_alpha[details["id"]]
            details["is"]["checks"].extend(deepcopy(self.correlation_checks))
            return Response(details)
        if path in self.correlation_responses and self.correlation_responses[path]:
            return self.correlation_responses[path].pop(0)
        if path in {f"/alphas/{alpha_id}/correlations/prod" for alpha_id in ("A1", "A2", "A3")}:
            return Response({"max": 0.4})
        if path in {f"/alphas/{alpha_id}/correlations/self" for alpha_id in ("A1", "A2", "A3")}:
            return Response({"count": 1, "next": None,
                "schema": {"properties": [{"name": "alphaId"}, {"name": "correlation"}]},
                "records": [["OTHER", 0.5]]})
        if path.startswith("/alphas/") and path.endswith("/recordsets/pnl"):
            alpha_id = path.split("/")[2]
            if alpha_id in self.pnl:
                return Response(self.pnl[alpha_id])
        raise AssertionError(f"unexpected request {method} {path}")

    def close(self):
        pass


class TestIndependentRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = FakeClient()
        self.runtime = PlatformRuntime(Path(self.tmp.name), client=self.client)
        self.addCleanup(self.runtime.close)
        self.runtime.set_case("case_1", "run_1")
        self.scope = {"instrument_type": "EQUITY", "region": "GLB",
                      "universe": "TOP3000", "delay": 1}
        self.runner = {"case_key": "case_1", "run_id": "run_1",
                       "batch_id": "20261028_120000_abcdef12", "label": "production",
                       "self_corr_max": 0.7}
        self._set_case_pnl()

    def _admit(self):
        self.runtime.dispatch("get_datafields", {**self.scope, "dataset_id": "analyst69"})
        self.runtime.dispatch("get_operators", self.scope)

    def _request(self):
        return {**self.runner, "dataset_id": "analyst69", "settings": SETTINGS,
                "variants": [{"name": "variant_1", "expression": "rank(field_a)",
                              "dataset": "analyst69", "research": RESEARCH}]}

    def _set_case_pnl(self):
        def recordset(values):
            return {"count": len(values), "next": None,
                    "schema": {"properties": [{"name": "date"}, {"name": "pnl"}]},
                    "records": [[f"2024-01-{i + 1:02d}", value]
                                for i, value in enumerate(values)]}
        self.client.pnl = {"A1": recordset([0, 1, 3, 6]),
                           "B1": recordset([0, 1, 4, 5])}
        self.client.os_alphas = [{"id": "B1", "settings": {"region": "GLB"},
                                  "classifications": []}]

    def test_catalogs_and_case_are_local_without_brain_flow(self):
        rows = self.runtime.dispatch("get_datasets", self.scope)
        self.assertEqual(rows, [{"id": "analyst69"}])
        self._admit()
        self.assertTrue(self.runtime.run_root.is_relative_to(Path(self.tmp.name)))
        self.assertFalse(any("brain-flow" in str(call) for call in self.client.calls))
        self.assertEqual(self.client.calls[0][2]["region"], "GLB")
        self.assertNotIn("dataset.id", self.client.calls[0][2])

    def test_duplicate_catalog_rows_are_not_treated_as_complete(self):
        original = self.client.request
        def duplicate(method, path, *, params=None, json=None):
            if path == "/data-sets":
                return Response({"count": 2, "results": [{"id": "same"}, {"id": "same"}]})
            return original(method, path, params=params, json=json)
        self.client.request = duplicate
        with self.assertRaises(PlatformError):
            self.runtime.dispatch("get_datasets", self.scope)

    def test_live_schema_is_translated_to_exact_scope_choices(self):
        def by_region(values):
            return {"instrumentType": {"EQUITY": {"region": {"GLB":
                [{"value": value} for value in values]}}}}
        schema = {
            "instrumentType": {"choices": [{"value": "EQUITY"}]},
            "region": {"choices": {"instrumentType": {"EQUITY": [{"value": "GLB"}]}}},
            "delay": {"choices": by_region([1])},
            "universe": {"choices": by_region(["TOP3000"])},
            "neutralization": {"choices": by_region(["MARKET", "NONE"])},
        }
        original = self.client.request
        def with_options(method, path, *, params=None, json=None):
            if method == "OPTIONS" and path == "/simulations":
                return Response({"actions": {"POST": {"settings": {"children": schema}}}})
            return original(method, path, params=params, json=json)
        self.client.request = with_options
        rows = self.runtime.dispatch("get_platform_setting_options", {})["instrument_options"]
        self.assertEqual(rows, [{"InstrumentType": "EQUITY", "Region": "GLB", "Delay": 1,
                                 "Universe": ["TOP3000"], "Neutralization": ["MARKET", "NONE"]}])

    def test_simulation_preserves_settings_and_creates_local_receipt(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        self.assertEqual(result["results"][0]["alpha_id"], "A1")
        posts = [call for call in self.client.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertIsInstance(posts[0][3], dict)
        self.assertEqual(posts[0][3]["settings"]["testPeriod"], "P0Y0M")
        self.assertEqual(posts[0][3]["settings"]["pasteurization"], "ON")
        self.assertTrue(Path(result["runner"]["output"]).is_relative_to(Path(self.tmp.name)))
        self.assertTrue(Path(result["runner"]["output"]).with_suffix(".admission.json").is_file())

    def test_regular_runtime_admits_more_than_eight_operators(self):
        self._admit()
        request = self._request()
        request["variants"][0]["expression"] = "rank(" * 9 + "field_a" + ")" * 9
        admission = self.runtime._admission(request)
        self.assertTrue(admission[0]["passed"])
        self.assertEqual(admission[0]["validation"]["operator_count"], 9)

    def test_stop_during_admission_prevents_new_simulation_post(self):
        self._admit()
        original = self.runtime._admission
        stop_flag = self.runtime.state_root / "stop.flag"
        def stop_after_admission(request):
            admission = original(request)
            stop_flag.write_text("stop\n", encoding="utf-8")
            return admission
        self.runtime._admission = stop_after_admission

        with self.assertRaisesRegex(PlatformError, "stop"):
            self.runtime.run_simulation_batch(self._request())

        self.assertFalse(any(call[0] == "POST" for call in self.client.calls))

    def test_stopped_unsubmitted_batch_can_resume_once_after_stop_cleared(self):
        self._admit()
        stop_flag = self.runtime.state_root / "stop.flag"
        stop_flag.write_text("stop\n", encoding="utf-8")
        with self.assertRaisesRegex(PlatformError, "stop"):
            self.runtime.run_simulation_batch(self._request())
        self.assertFalse(any(call[0] == "POST" for call in self.client.calls))

        stop_flag.unlink()
        result = self.runtime.resume_simulation_batch(self.runner)

        self.assertEqual(result["results"][0]["alpha_id"], "A1")
        self.assertEqual(sum(call[0] == "POST" for call in self.client.calls), 1)
        self.runtime.resume_simulation_batch(self.runner)
        self.assertEqual(sum(call[0] == "POST" for call in self.client.calls), 1)

    def test_stopped_batch_with_changed_receipt_cannot_resume(self):
        self._admit()
        stop_flag = self.runtime.state_root / "stop.flag"
        stop_flag.write_text("stop\n", encoding="utf-8")
        with self.assertRaises(PlatformError):
            self.runtime.run_simulation_batch(self._request())
        paths = self.runtime._batch_paths(self.runner["batch_id"], "production")
        paths["receipt"].write_text('{"changed": true}', encoding="utf-8")
        stop_flag.unlink()

        with self.assertRaisesRegex(PlatformError, "evidence changed"):
            self.runtime.resume_simulation_batch(self.runner)

        self.assertFalse(any(call[0] == "POST" for call in self.client.calls))

    def test_stopped_status_cannot_override_evidence_of_a_started_post(self):
        self._admit()
        self.client.post_error = TimeoutError("response lost")
        with self.assertRaises(PlatformError):
            self.runtime.run_simulation_batch(self._request())
        paths = self.runtime._batch_paths(self.runner["batch_id"], "production")
        progress = json.loads(paths["progress"].read_text(encoding="utf-8"))
        progress["status"] = "STOPPED_BEFORE_SUBMISSION"
        paths["progress"].write_text(json.dumps(progress), encoding="utf-8")
        self.client.post_error = None

        with self.assertRaisesRegex(PlatformError, "may have been submitted"):
            self.runtime.resume_simulation_batch(self.runner)

        self.assertEqual(sum(call[0] == "POST" for call in self.client.calls), 1)

    def test_stop_cli_waits_for_inflight_post_then_blocks_later_posts(self):
        root = Path(self.tmp.name) / "app"
        runtime = PlatformRuntime(root / "state", client=self.client)
        runtime.set_case("case_1", "run_1")
        runtime.dispatch("get_datafields", {**self.scope, "dataset_id": "analyst69"})
        runtime.dispatch("get_operators", self.scope)
        started, release = threading.Event(), threading.Event()
        original = self.client.request
        observed = {}

        def slow_post(method, path, **kwargs):
            if method == "POST" and path == "/simulations":
                started.set()
                if not release.wait(10):
                    raise TimeoutError("test did not release submission")
            return original(method, path, **kwargs)

        self.client.request = slow_post

        def execute():
            try:
                observed["result"] = runtime.run_simulation_batch(self._request())
            except Exception as exc:
                observed["error"] = exc

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        self.assertTrue(started.wait(5), "simulation did not reach the POST")
        ready = root / "stop-process-ready"
        script = ("import sys; from pathlib import Path; "
                  "from nastu_quant.__main__ import main; "
                  "Path(sys.argv[1]).write_text('ready'); "
                  "sys.exit(main(['--state-root', sys.argv[2], 'stop']))")
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(ready), str(root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "stop process did not reach the command")
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.wait(timeout=0.5)
            release.set()
            stdout, stderr = proc.communicate(timeout=10)
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertNotIn("error", observed)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertTrue(json.loads(stdout)["stopped"])
            next_request = {**self._request(), "batch_id": "20261028_120001_abcdef12"}
            with self.assertRaisesRegex(PlatformError, "stop"):
                runtime.run_simulation_batch(next_request)
            self.assertEqual(sum(call[0] == "POST" for call in self.client.calls), 1)
        finally:
            release.set()
            worker.join(timeout=10)
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=5)
            runtime.close()

    def test_competing_same_batch_submissions_post_at_most_once(self):
        self._admit()
        barrier = threading.Barrier(2)
        admission = self.runtime._admission
        observed = []
        def simultaneous_admission(request):
            result = admission(request)
            barrier.wait(timeout=5)
            return result
        self.runtime._admission = simultaneous_admission
        def execute():
            try:
                observed.append(self.runtime.run_simulation_batch(self._request()))
            except PlatformError:
                observed.append(None)
        workers = [threading.Thread(target=execute, daemon=True) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(observed), 2)
        self.assertTrue(any(result is not None for result in observed))
        self.assertEqual(sum(call[0] == "POST" for call in self.client.calls), 1)

    def test_multisim_preserves_variant_order(self):
        self._admit()
        request = self._request()
        request["variants"].append({"name": "variant_2", "dataset": "analyst69",
            "expression": "rank(ts_mean(field_a, 5))", "research": RESEARCH})
        result = self.runtime.run_simulation_batch(request)
        self.assertEqual([row["alpha_id"] for row in result["results"]], ["A1", "A2"])
        posts = [call for call in self.client.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual([row["regular"] for row in posts[0][3]],
                         ["rank(field_a)", "rank(ts_mean(field_a, 5))"])

    def test_duplicate_expressions_are_blocked_before_post(self):
        self._admit()
        request = self._request()
        request["variants"].append({**request["variants"][0], "name": "variant_2"})
        with self.assertRaises(PlatformError):
            self.runtime.run_simulation_batch(request)
        self.assertFalse(any(call[0] == "POST" for call in self.client.calls))

    def test_non_regular_and_wrong_arity_operators_are_blocked_before_post(self):
        self._admit()
        for expression in ("combo_a(field_a)", "rank(field_a, 2, 3)", "rank(ts_mean(field_a))"):
            request = self._request()
            request["variants"][0]["expression"] = expression
            with self.subTest(expression=expression), self.assertRaises(PlatformError):
                self.runtime.run_simulation_batch(request)
        self.assertFalse(any(call[0] == "POST" for call in self.client.calls))

    def test_invalid_settings_are_rejected_before_post(self):
        self._admit()
        for key, invalid in (("region", "../GLB"), ("delay", True), ("decay", -1),
                             ("truncation", float("nan")), ("visualization", "false")):
            request = self._request()
            request["settings"] = {**SETTINGS, key: invalid}
            with self.subTest(key=key), self.assertRaises(PlatformError):
                self.runtime.run_simulation_batch(request)
        self.assertFalse(any(call[0] == "POST" for call in self.client.calls))

    def test_lost_post_never_reposts(self):
        self._admit()
        self.client.post_error = TimeoutError("response lost")
        with self.assertRaises(PlatformError):
            self.runtime.run_simulation_batch(self._request())
        with self.assertRaises(PlatformError):
            self.runtime.run_simulation_batch(self._request())
        with self.assertRaises(PlatformError):
            self.runtime.resume_simulation_batch(self.runner)
        self.assertEqual(sum(call[0] == "POST" for call in self.client.calls), 1)

    def test_interrupted_poll_resumes_saved_location_without_reposting(self):
        self._admit()
        self.client.poll_error = TimeoutError("poll interrupted")
        with self.assertRaises(PlatformError):
            self.runtime.run_simulation_batch(self._request())
        result = self.runtime.resume_simulation_batch(self.runner)
        self.assertEqual(result["results"][0]["alpha_id"], "A1")
        self.assertEqual(sum(call[0] == "POST" for call in self.client.calls), 1)

    def test_changed_completed_result_is_not_reused_on_recovery(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        output = Path(result["runner"]["output"])
        rows = json.loads(output.read_text(encoding="utf-8"))
        rows[0]["alpha_id"] = "OTHER"
        output.write_text(json.dumps(rows), encoding="utf-8")
        with self.assertRaises(PlatformError):
            self.runtime.resume_simulation_batch(self.runner)
        self.assertEqual(sum(call[0] == "POST" for call in self.client.calls), 1)

    def test_interrupted_result_write_is_verified_by_polling_not_reposting(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        progress = Path(result["runner"]["progress"])
        saved = json.loads(progress.read_text(encoding="utf-8"))
        saved["status"] = "SUBMITTED"
        saved.pop("output_sha256", None)
        progress.write_text(json.dumps(saved), encoding="utf-8")
        polls_before = sum(call[1] == "/simulations/S1" for call in self.client.calls)
        resumed = self.runtime.resume_simulation_batch(self.runner)
        self.assertEqual(resumed["results"][0]["alpha_id"], "A1")
        self.assertGreater(sum(call[1] == "/simulations/S1" for call in self.client.calls), polls_before)
        self.assertEqual(sum(call[0] == "POST" for call in self.client.calls), 1)
        self.assertTrue(json.loads(progress.read_text(encoding="utf-8"))["output_sha256"])

    def test_audit_reads_local_receipt_and_live_platform_evidence(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        output = Path(result["runner"]["output"])
        review = {"status": "clear", "evidence": "reviewed"}
        entries = {"A1": {"dataset": "analyst69", "variant": "variant_1",
            "receipt": str(output.with_suffix(".admission.json")), "results": str(output),
            "reviews": {"robustness": review, "tradeoff": review}}}
        audited = self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"],
                                             "entries": entries, "contender_ids": ["A1"]})
        row = audited["results"][0]
        self.assertTrue(row["evidence_complete"])
        self.assertNotIn("candidate_eligible", row)
        self.assertEqual(row["candidate_status"], "evidence_complete")
        self.assertEqual(row["risk_neutralized_state"], "absent")
        self.assertEqual(row["field_admission"]["fields"][0]["id"], "field_a")
        self.assertEqual(row["prod_corr"], 0.4)
        self.assertTrue(Path(audited["audit_path"]).is_relative_to(Path(self.tmp.name)))

    def test_audit_never_treats_missing_pairwise_as_pass(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        output = Path(result["runner"]["output"])
        review = {"status": "clear", "evidence": "reviewed"}
        entry = {"dataset": "analyst69", "variant": "variant_1",
                 "receipt": str(output.with_suffix(".admission.json")), "results": str(output),
                 "reviews": {"robustness": review, "tradeoff": review}}
        row = self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"],
            "entries": {"A1": entry}, "contender_ids": ["A1", "MISSING"]})["results"][0]
        self.assertFalse(row["evidence_complete"])
        self.assertIn("pairwise_correlation_unavailable", row["gate_pending"])

    def test_audit_checks_correlation_with_pending_ra_but_keeps_audit_unresolved(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        output = Path(result["runner"]["output"])
        review = {"status": "clear", "evidence": "reviewed"}
        entry = {"dataset": "analyst69", "variant": "variant_1",
                 "receipt": str(output.with_suffix(".admission.json")), "results": str(output),
                 "reviews": {"robustness": review, "tradeoff": review}}
        self.client.ra_result = "PENDING"

        row = self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"],
            "entries": {"A1": entry}, "contender_ids": ["A1"]})["results"][0]

        self.assertIsNotNone(row["local_self_correlation"])
        self.assertEqual(row["local_self_correlation"]["status"], "pass")
        self.assertEqual(row["prod_corr"], 0.4)
        self.assertEqual(row["self_corr"], 0.5)
        self.assertFalse(row["evidence_complete"])
        self.assertIn("ra_checks_unsettled_or_unavailable", row["gate_pending"])

    def test_new_ra_failure_during_prefilter_blocks_platform_correlation(self):
        self._admit()
        self.runtime.run_simulation_batch(self._request())
        self.client.ra_results = ["PASS", "FAIL"]

        row = self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"],
                                         "entries": {}, "contender_ids": []})["results"][0]

        self.assertFalse(any("/correlations/" in call[1] for call in self.client.calls))
        self.assertFalse(row["evidence_complete"])
        self.assertIn("local_self_correlation_unavailable", row["gate_pending"])
        self.assertIn("ra_failed_count", row["gate_failures"])
        self.assertEqual(row["raw_details"]["is"]["checks"][0]["result"], "FAIL")

    def test_each_audit_uses_a_fresh_local_self_snapshot(self):
        self._admit()
        self.client.pnl["A2"] = deepcopy(self.client.pnl["A1"])
        request = self._request()
        request["variants"].append({"name": "variant_2", "expression": "rank(ts_mean(field_a, 5))",
                                    "dataset": "analyst69", "research": RESEARCH})
        self.runtime.run_simulation_batch(request)
        params = {**self.runner, "alpha_ids": ["A1", "A2"], "entries": {}, "contender_ids": []}

        first = self.runtime.audit_alphas(params)["results"]
        self.assertEqual([row["local_self_correlation"]["status"] for row in first], ["pass", "pass"])
        self.assertEqual(sum(call[1] == "/users/self/alphas" for call in self.client.calls), 1)
        self.assertEqual(sum(call[1] == "/alphas/B1/recordsets/pnl" for call in self.client.calls), 1)

        self.client.pnl["B1"] = deepcopy(self.client.pnl["A1"])
        second = self.runtime.audit_alphas(params)["results"]
        self.assertEqual([row["local_self_correlation"]["status"] for row in second], ["fail", "fail"])
        self.assertEqual(sum(call[1] == "/users/self/alphas" for call in self.client.calls), 2)

    def test_new_audit_breach_invalidates_earlier_local_self_pass(self):
        self._admit()
        self.runtime.run_simulation_batch(self._request())
        params = {"alpha_id": "A1", "self_corr_max": 0.7, "batch_id": self.runner["batch_id"]}
        self.assertEqual(self.runtime.dispatch("check_local_self_correlation", params)["status"], "pass")
        self.client.pnl["B1"] = deepcopy(self.client.pnl["A1"])
        self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"], "entries": {}, "contender_ids": []})

        with self.assertRaises(PlatformError):
            self.runtime.dispatch("check_correlation", params)

    def test_new_audit_invalidates_unchecked_prior_batch_prefilters(self):
        self._admit()
        self.runtime.run_simulation_batch(self._request())
        self.client.pnl["A2"] = deepcopy(self.client.pnl["A1"])
        batch = self.runner["batch_id"]
        for alpha_id in ("A1", "A2"):
            self.runtime.dispatch("check_local_self_correlation", {
                "alpha_id": alpha_id, "self_corr_max": 0.7, "batch_id": batch})
        self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"], "entries": {}, "contender_ids": []})

        with self.assertRaises(PlatformError):
            self.runtime.dispatch("check_correlation", {"alpha_id": "A2", "self_corr_max": 0.7,
                                                         "batch_id": batch})

    def test_correlation_uses_case_pnl_for_unsubmitted_contenders(self):
        self._set_case_pnl()
        self.runtime.dispatch("check_local_self_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})
        raw = self.runtime.dispatch("check_correlation", {"alpha_id": "A1", "contender_ids": ["B1"],
                                                            "self_corr_max": 0.7})
        self.assertEqual(raw["case_pairwise"]["B1"]["observations"], 3)
        self.assertTrue(Path(raw["case_pairwise"]["B1"]["alpha_pnl"]["path"]).is_file())
        self.assertAlmostEqual(parse_correlations(raw, ["B1"])["pairwise"]["B1"], 0.0)

    def test_platform_correlation_polls_a_retry_after_response(self):
        self.runtime.dispatch("check_local_self_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})
        path = "/alphas/A1/correlations/prod"
        self.client.correlation_responses[path] = [
            Response({}, headers={"Retry-After": "0"}), Response({"max": 0.4})]

        with patch("nastu_quant.platform_runtime.time.sleep"):
            raw = self.runtime.dispatch("check_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})

        self.assertEqual(parse_correlations(raw)["prod"], 0.4)
        self.assertEqual(sum(call[1] == path for call in self.client.calls), 2)

    def test_terminal_platform_check_values_are_reused_after_local_prefilter(self):
        self.client.correlation_checks = [
            {"name": "PROD_CORRELATION", "result": "PASS", "value": 0.35},
            {"name": "SELF_CORRELATION", "result": "FAIL", "value": -0.8},
        ]
        self.runtime.dispatch("check_local_self_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})

        raw = self.runtime.dispatch("check_correlation", {
            "alpha_id": "A1", "self_corr_max": 0.7, "contender_ids": ["B1"]})

        parsed = parse_correlations(raw, ["B1"])
        self.assertEqual(parsed["prod"], 0.35)
        self.assertEqual(parsed["self"], 0.8)
        self.assertIn("B1", parsed["pairwise"])
        self.assertFalse(any("/correlations/" in call[1] for call in self.client.calls))

    def test_pending_platform_check_value_is_not_used_as_correlation(self):
        self.client.correlation_checks = [
            {"name": "PROD_CORRELATION", "result": "PENDING", "value": 0.1}]
        self.client.correlation_responses["/alphas/A1/correlations/prod"] = [Response(status=503)]
        self.runtime.dispatch("check_local_self_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})

        raw = self.runtime.dispatch("check_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})

        self.assertIsNone(parse_correlations(raw)["prod"])

    def test_pending_platform_correlation_poll_is_bounded(self):
        self.runtime.dispatch("check_local_self_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})
        path = "/alphas/A1/correlations/prod"
        self.client.correlation_responses[path] = [Response({}, status=202, headers={"Retry-After": "0"})
                                                   for _ in range(5)]

        with patch("nastu_quant.platform_runtime.time.sleep") as sleep:
            raw = self.runtime.dispatch("check_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})

        self.assertIsNone(parse_correlations(raw)["prod"])
        self.assertEqual(parse_correlations(raw)["self"], 0.5)
        self.assertEqual(sum(call[1] == path for call in self.client.calls), 5)
        self.assertEqual(sleep.call_count, 4)

    def test_unavailable_platform_correlation_preserves_other_evidence(self):
        self.runtime.dispatch("check_local_self_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})
        self.client.correlation_responses["/alphas/A1/correlations/prod"] = [Response(status=503)]

        raw = self.runtime.dispatch("check_correlation", {
            "alpha_id": "A1", "self_corr_max": 0.7, "contender_ids": ["B1"]})

        parsed = parse_correlations(raw, ["B1"])
        self.assertIsNone(parsed["prod"])
        self.assertEqual(parsed["self"], 0.5)
        self.assertIn("B1", parsed["pairwise"])

    def test_local_self_prefilter_uses_same_region_non_power_pool_pnl(self):
        self._set_case_pnl()
        self.client.os_alphas.extend([
            {"id": "OTHER_REGION", "settings": {"region": "USA"}, "classifications": []},
            {"id": "PPAC", "settings": {"region": "GLB"},
             "classifications": [{"name": "Power Pool Alpha"}]},
        ])
        result = self.runtime.dispatch("check_local_self_correlation", {
            "alpha_id": "A1", "self_corr_max": 0.7})
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["complete"])
        self.assertAlmostEqual(result["value"], 0.0)
        self.assertEqual(result["comparison_count"], 1)
        self.assertFalse(any("/correlations/prod" in call[1] for call in self.client.calls))

    def test_local_self_prefilter_rejects_breach_and_keeps_missing_data_pending(self):
        self._set_case_pnl()
        self.client.pnl["B1"] = self.client.pnl["A1"]
        result = self.runtime.dispatch("check_local_self_correlation", {
            "alpha_id": "A1", "self_corr_max": 0.7})
        self.assertEqual(result["status"], "fail")
        self.assertAlmostEqual(result["value"], 1.0)
        del self.client.pnl["B1"]
        result = self.runtime.dispatch("check_local_self_correlation", {
            "alpha_id": "A1", "self_corr_max": 0.7})
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["complete"])
        self.assertFalse(any("/correlations/prod" in call[1] for call in self.client.calls))

    def test_local_self_prefilter_does_not_guess_missing_classification(self):
        self._set_case_pnl()
        self.client.os_alphas[0].pop("classifications")
        result = self.runtime.dispatch("check_local_self_correlation", {
            "alpha_id": "A1", "self_corr_max": 0.7})
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["complete"])

    def test_local_self_prefilter_rejects_invalid_limits(self):
        for value in (True, float("nan"), 1.1, 10 ** 1000):
            with self.subTest(value=str(value)[:20]), self.assertRaises(PlatformError):
                self.runtime.dispatch("check_local_self_correlation", {
                    "alpha_id": "A1", "self_corr_max": value})

    def test_local_self_reuses_pnl_within_batch_and_refreshes_for_next_batch(self):
        self.client.pnl["A2"] = deepcopy(self.client.pnl["A1"])
        first = {"self_corr_max": 0.7, "batch_id": "batch_1"}

        a1 = self.runtime.dispatch("check_local_self_correlation", {**first, "alpha_id": "A1"})
        a2 = self.runtime.dispatch("check_local_self_correlation", {**first, "alpha_id": "A2"})
        self.assertEqual((a1["status"], a2["status"]), ("pass", "pass"))
        self.assertEqual(sum(call[1] == "/users/self/alphas" for call in self.client.calls), 1)
        self.assertEqual(sum(call[1] == "/alphas/B1/recordsets/pnl" for call in self.client.calls), 1)

        self.client.pnl["B1"] = deepcopy(self.client.pnl["A2"])
        next_batch = self.runtime.dispatch("check_local_self_correlation", {
            "alpha_id": "A2", "self_corr_max": 0.7, "batch_id": "batch_2"})
        self.assertEqual(next_batch["status"], "fail")
        self.assertEqual(sum(call[1] == "/users/self/alphas" for call in self.client.calls), 2)
        self.assertEqual(sum(call[1] == "/alphas/B1/recordsets/pnl" for call in self.client.calls), 2)

    def test_local_self_cache_retries_incomplete_pnl_within_batch(self):
        self.client.pnl["A2"] = deepcopy(self.client.pnl["A1"])
        complete = deepcopy(self.client.pnl["B1"])
        self.client.pnl["B1"]["next"] = "unavailable page"
        params = {"self_corr_max": 0.7, "batch_id": "batch_1"}
        self.assertEqual(self.runtime.dispatch("check_local_self_correlation", {
            **params, "alpha_id": "A1"})["status"], "pending")

        self.client.pnl["B1"] = complete
        result = self.runtime.dispatch("check_local_self_correlation", {**params, "alpha_id": "A2"})

        self.assertEqual(result["status"], "pass")
        self.assertEqual(sum(call[1] == "/alphas/B1/recordsets/pnl" for call in self.client.calls), 2)

    def test_case_pairwise_reuses_pnl_from_the_same_batch_prefilter(self):
        self.runtime.dispatch("check_local_self_correlation", {
            "alpha_id": "A1", "self_corr_max": 0.7, "batch_id": "batch_1"})

        raw = self.runtime.dispatch("check_correlation", {
            "alpha_id": "A1", "self_corr_max": 0.7, "batch_id": "batch_1", "contender_ids": ["B1"]})

        self.assertIn("B1", parse_correlations(raw, ["B1"])["pairwise"])
        self.assertEqual(sum(call[1] == "/alphas/A1/recordsets/pnl" for call in self.client.calls), 1)
        self.assertEqual(sum(call[1] == "/alphas/B1/recordsets/pnl" for call in self.client.calls), 1)

    def test_local_self_cache_preserves_other_targets_in_os_pool(self):
        self.client.pnl["A2"] = deepcopy(self.client.pnl["A1"])
        self.client.os_alphas.append({"id": "A1", "settings": {"region": "GLB"},
                                      "classifications": []})
        params = {"self_corr_max": 0.7, "batch_id": "batch_1"}
        self.assertEqual(self.runtime.dispatch("check_local_self_correlation", {
            **params, "alpha_id": "A1"})["status"], "pass")

        result = self.runtime.dispatch("check_local_self_correlation", {**params, "alpha_id": "A2"})

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["comparison_count"], 2)

    def test_changed_pnl_evidence_is_unavailable_not_overwritten(self):
        self._set_case_pnl()
        self.runtime.dispatch("check_local_self_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})
        params = {"alpha_id": "A1", "contender_ids": ["B1"], "self_corr_max": 0.7}
        first = self.runtime.dispatch("check_correlation", params)
        evidence = Path(first["case_pairwise"]["B1"]["alpha_pnl"]["path"])
        evidence.write_text('{"changed": true}', encoding="utf-8")
        second = self.runtime.dispatch("check_correlation", params)
        self.assertNotIn("B1", second["case_pairwise"])
        self.assertIn("B1", second["case_pairwise_unavailable"])
        self.assertEqual(json.loads(evidence.read_text(encoding="utf-8")), {"changed": True})

    def test_platform_correlation_cannot_bypass_local_prefilter(self):
        with self.assertRaises(PlatformError):
            self.runtime.dispatch("check_correlation", {"alpha_id": "A1", "self_corr_max": 0.7})
        self.assertFalse(any("/correlations/" in call[1] for call in self.client.calls))

    def test_audit_accepts_case_pnl_pairwise_evidence(self):
        self._admit()
        self._set_case_pnl()
        result = self.runtime.run_simulation_batch(self._request())
        output = Path(result["runner"]["output"])
        review = {"status": "clear", "evidence": "reviewed"}
        entry = {"dataset": "analyst69", "variant": "variant_1",
                 "receipt": str(output.with_suffix(".admission.json")), "results": str(output),
                 "reviews": {"robustness": review, "tradeoff": review}}
        row = self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"],
            "entries": {"A1": entry}, "contender_ids": ["A1", "B1"]})["results"][0]
        self.assertTrue(row["evidence_complete"])
        self.assertAlmostEqual(row["pairwise"][0]["corr"], 0.0)

    def test_independent_audit_integrates_with_project_candidate_gate(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        batch = Path(self.tmp.name) / self.runner["batch_id"]
        batch.mkdir()
        (batch / "simulation_raw.json").write_text(json.dumps({"runner": result["runner"]}),
                                                    encoding="utf-8")
        prereg = {"dataset_id": "analyst69", "scope": {"region": "GLB", "delay": 1,
                  "universe": "TOP3000"}, "simulation_settings": SETTINGS,
                  "variants": [{"expression": "rank(field_a)"}],
                  "operator_counts": {"rank(field_a)": 1}}
        details = self.client.request("GET", "/alphas/A1").json()
        local = self.runtime.dispatch("check_local_self_correlation", {
            "alpha_id": "A1", "self_corr_max": 0.7})
        raw = self.runtime.dispatch("check_correlation", {
            "alpha_id": "A1", "self_corr_max": 0.7, "contender_ids": []})
        correlations = parse_correlations(raw, [])
        verdict = evaluate_candidate(details, {"checks": details["is"]["checks"]}, "GLB",
            DEFAULT_CONFIG, correlations=correlations, pairwise={}, expected_pairwise_ids=[],
            operator_count=1, local_self=local)
        verdict["correlations"] = correlations
        self.assertEqual(verdict["status"], "CANDIDATE")
        readback = {"A1": {"details": details, "correlation_raw": raw,
                           "review": {"verdict": "CLEAR", "reason": "reviewed",
                                      "evidence_refs": ["readback"]}}}
        runtime = self.runtime
        class Gateway:
            def call(self, command, **params):
                return runtime.dispatch(command, params)
        agent = ResearchAgent.__new__(ResearchAgent)
        agent.gateway, agent.config = Gateway(), DEFAULT_CONFIG
        gate = {"A1": verdict}
        agent._apply_canonical_audit(batch, prereg, ["A1"], readback, gate,
                                     contender_ids=["A1"])
        self.assertEqual(gate["A1"]["status"], "CANDIDATE", gate["A1"])
        self.assertTrue(gate["A1"]["canonical_audit"]["evidence_complete"])

    def test_complete_independent_research_batch_with_mocked_platform(self):
        runtime = PlatformRuntime(Path(self.tmp.name) / "state", client=self.client)
        self.addCleanup(runtime.close)
        gateway = Gateway()
        gateway.proc = type("Process", (), {"poll": lambda self: None})()
        def send(payload):
            return {"ok": True, "result": runtime.dispatch(
                payload["cmd"], {key: value for key, value in payload.items() if key != "cmd"})}
        gateway._send = send
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["scope"].update({key: SETTINGS[key] for key in (
            "region", "universe", "delay", "decay", "neutralization", "truncation",
            "pasteurization", "unit_handling", "nan_handling", "test_period", "max_trade")})
        dimensions = ("nominal_coverage", "usable_observations", "refresh_rate",
                      "scale_units", "typical_level", "distribution")
        decisions = {
            "plan_probes": {"core_fields": ["field_a"], "probes": [], "reason": "catalog evidence"},
            "construct_batch": {"hypothesis": "The field predicts returns", "expected_sign": "positive",
                "structure_window_rationale": "compare field levels", "data_rationale": "field is a forecast",
                "operator_rationale": "rank compares instruments", "field_relationship": "one field",
                "settings_rationale": "fixed supported settings", "failure_criterion": "no return spread",
                "field_report": [{"field_id": "field_a", "verdict": "ADMIT",
                    "constraints": ["use ranked values"], "evidence": {name: {
                        "source": "catalog", "observation": "observed"} for name in dimensions}}],
                "variants": [{"expression": "rank(field_a)", "reason": "baseline"}]},
            "review_candidate": {"verdict": "CLEAR", "reason": "evidence reviewed",
                                 "evidence_refs": ["readback", "correlation"]},
            "attribute_results": {"summary": "measured result"},
        }
        class Model:
            def complete_json(self, task, context):
                return deepcopy(decisions[task])
        stop_flag = Path(self.tmp.name) / "state" / "stop.flag"
        agent = ResearchAgent(gateway, Model(), cfg, Path(self.tmp.name) / "runs", stop_flag=stop_flag)
        admission = runtime._admission
        def stop_after_admission(request):
            result = admission(request)
            stop_flag.write_text("stop\n", encoding="utf-8")
            return result
        runtime._admission = stop_after_admission
        with self.assertRaisesRegex(PlatformError, "stop"):
            agent.run_once("forecast future returns", dataset_id="analyst69")
        self.assertFalse(any(call[0] == "POST" for call in self.client.calls))
        batch = agent._unfinished_batch()
        stop_flag.unlink()
        runtime._admission = admission
        result = agent.recover(batch)
        self.assertEqual(result["candidate_status"], "CANDIDATE", result["gate"])
        self.assertEqual(result["alpha_ids"], ["A1"])
        self.assertEqual(sum(call[0] == "POST" for call in self.client.calls), 1)
        self.assertTrue(agent.backtest_cache.find("rank(field_a)", "analyst69",
                                                 result["scope"], SETTINGS))
        reloaded = BacktestCache(Path(self.tmp.name) / "runs" / "backtest_cache.cache")
        self.assertTrue(reloaded.find("rank(field_a)", "analyst69", result["scope"], SETTINGS))

    def test_retained_candidate_is_reaudited_through_independent_runtime(self):
        def pnl(values):
            return {"count": len(values), "next": None,
                    "schema": {"properties": [{"name": "date"}, {"name": "pnl"}]},
                    "records": [[f"2024-01-{index + 1:02d}", value]
                                for index, value in enumerate(values)]}
        self.client.pnl = {"A1": pnl([0, 0, 1, 1, 0]), "B1": pnl([0, 1, 1, 0, 0]),
                           "A2": pnl([0, 1, 0, 1, 0]), "A3": pnl([0, 1, 0, 1, 0])}
        self.client.single_alpha_ids = ["A1", "A2", "A3"]
        runtime = PlatformRuntime(Path(self.tmp.name) / "state", client=self.client)
        self.addCleanup(runtime.close)
        gateway = Gateway()
        gateway.proc = type("Process", (), {"poll": lambda self: None})()
        gateway._send = lambda payload: {"ok": True, "result": runtime.dispatch(
            payload["cmd"], {key: value for key, value in payload.items() if key != "cmd"})}
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["scope"].update({key: SETTINGS[key] for key in (
            "region", "universe", "delay", "decay", "neutralization", "truncation",
            "pasteurization", "unit_handling", "nan_handling", "test_period", "max_trade")})
        dimensions = ("nominal_coverage", "usable_observations", "refresh_rate",
                      "scale_units", "typical_level", "distribution")
        decisions = {
            "plan_probes": {"core_fields": ["field_a"], "probes": [], "reason": "catalog evidence"},
            "construct_batch": {"hypothesis": "The field predicts returns", "expected_sign": "positive",
                "structure_window_rationale": "compare field levels", "data_rationale": "field is a forecast",
                "operator_rationale": "rank compares instruments", "field_relationship": "one field",
                "settings_rationale": "fixed supported settings", "failure_criterion": "no return spread",
                "field_report": [{"field_id": "field_a", "verdict": "ADMIT", "constraints": ["use ranked values"],
                    "evidence": {name: {"source": "catalog", "observation": "observed"} for name in dimensions}}],
                "variants": [{"expression": "rank(field_a)", "reason": "baseline"}]},
            "review_candidate": {"verdict": "CLEAR", "reason": "evidence reviewed",
                                 "evidence_refs": ["readback", "correlation"]},
            "attribute_results": {"summary": "measured result"},
        }
        class Model:
            def complete_json(self, task, context):
                return deepcopy(decisions[task])
        agent = ResearchAgent(gateway, Model(), cfg, Path(self.tmp.name) / "runs")
        first = agent.run_once("forecast future returns", dataset_id="analyst69")
        self.assertEqual(first["gate"]["A1"]["status"], "CANDIDATE")
        original_audit = runtime.case_root / "audits" / f"{Path(first['batch_dir']).name}.json"
        original_bytes = original_audit.read_bytes()
        decisions["construct_batch"]["variants"] = [
            {"expression": "rank(ts_mean(field_a, 5))", "reason": "slow horizon"}]

        second = agent.run_once("forecast future returns", dataset_id="analyst69")
        case = json.loads(Path(second["case_path"]).read_text(encoding="utf-8"))
        self.assertEqual(case["contenders"]["A1"]["status"], "CANDIDATE")
        clean = json.loads((Path(second["batch_dir"]) / "retained_candidate_audits.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(clean["A1"]["status"], "CANDIDATE")

        self.client.sharpe_by_alpha["A1"] = 1.0
        decisions["construct_batch"]["variants"] = [
            {"expression": "rank(ts_mean(field_a, 7))", "reason": "independent slow horizon"}]
        third = agent.run_once("forecast future returns", dataset_id="analyst69")
        case = json.loads(Path(third["case_path"]).read_text(encoding="utf-8"))
        self.assertNotEqual(case["contenders"]["A1"]["status"], "CANDIDATE")
        evidence = json.loads((Path(third["batch_dir"]) / "retained_candidate_audits.json")
                              .read_text(encoding="utf-8"))
        self.assertIn("sharpe", evidence["A1"]["failures"])
        reaudit = runtime.case_root / "audits" / f"{Path(third['batch_dir']).name}_A1.json"
        self.assertTrue(reaudit.is_file())
        self.assertEqual(original_audit.read_bytes(), original_bytes)

    def test_audit_never_fetches_platform_correlations_after_local_breach(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        self.client.pnl["B1"] = self.client.pnl["A1"]
        output = Path(result["runner"]["output"])
        review = {"status": "clear", "evidence": "reviewed"}
        entry = {"dataset": "analyst69", "variant": "variant_1",
                 "receipt": str(output.with_suffix(".admission.json")), "results": str(output),
                 "reviews": {"robustness": review, "tradeoff": review}}
        row = self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"],
            "entries": {"A1": entry}, "contender_ids": ["A1"]})["results"][0]
        self.assertEqual(row["local_self_correlation"]["status"], "fail")
        self.assertIn("platform_correlation_skipped_after_local_self_failure", row["gate_pending"])
        self.assertFalse(any("/correlations/" in call[1] for call in self.client.calls))

    def test_audit_detects_changed_local_receipt(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        output = Path(result["runner"]["output"])
        receipt = output.with_suffix(".admission.json")
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["dataset"] = "other"
        receipt.write_text(json.dumps(value), encoding="utf-8")
        review = {"status": "clear", "evidence": "reviewed"}
        entry = {"dataset": "analyst69", "variant": "variant_1", "receipt": str(receipt),
                 "results": str(output), "reviews": {"robustness": review, "tradeoff": review}}
        row = self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"],
            "entries": {"A1": entry}, "contender_ids": ["A1"]})["results"][0]
        self.assertFalse(row["evidence_complete"])
        self.assertIn("pre_backtest_evidence_unverified", row["gate_pending"])

    def test_incomplete_risk_neutralized_block_remains_pending(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        output = Path(result["runner"]["output"])
        self.client.include_risk = True
        self.client.risk = {"sharpe": 1.2}
        review = {"status": "clear", "evidence": "reviewed"}
        entry = {"dataset": "analyst69", "variant": "variant_1",
                 "receipt": str(output.with_suffix(".admission.json")), "results": str(output),
                 "reviews": {"robustness": review, "tradeoff": review}}
        row = self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"],
            "entries": {"A1": entry}, "contender_ids": ["A1"]})["results"][0]
        self.assertEqual(row["risk_neutralized_state"], "present")
        self.assertFalse(row["evidence_complete"])
        self.assertIn("risk_neutralized_fitness_unavailable", row["gate_pending"])
        self.assertIn("risk_neutralized_margin_unavailable", row["gate_pending"])

    def test_audit_detects_changed_completed_result(self):
        self._admit()
        result = self.runtime.run_simulation_batch(self._request())
        output = Path(result["runner"]["output"])
        rows = json.loads(output.read_text(encoding="utf-8"))
        rows[0]["details"]["is"]["sharpe"] = -9
        output.write_text(json.dumps(rows), encoding="utf-8")
        review = {"status": "clear", "evidence": "reviewed"}
        entry = {"dataset": "analyst69", "variant": "variant_1",
                 "receipt": str(output.with_suffix(".admission.json")), "results": str(output),
                 "reviews": {"robustness": review, "tradeoff": review}}
        row = self.runtime.audit_alphas({**self.runner, "alpha_ids": ["A1"],
            "entries": {"A1": entry}, "contender_ids": ["A1"]})["results"][0]
        self.assertFalse(row["evidence_complete"])
        self.assertIn("pre_backtest_evidence_unverified", row["gate_pending"])


class TestSessionBehavior(unittest.TestCase):
    def test_simulation_post_401_is_not_retried(self):
        class Session:
            def __init__(self):
                self.requests = []
            def post(self, url, **kwargs):
                return Response({}, status=201)
            def request(self, method, url, **kwargs):
                self.requests.append((method, url))
                return Response({}, status=401)
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            path.write_text(json.dumps({"email": "fixture", "password": "fixture"}), encoding="utf-8")
            old = os.environ.get("BRAIN_CREDENTIALS_FILE")
            os.environ["BRAIN_CREDENTIALS_FILE"] = str(path)
            try:
                session = Session()
                client = WorldQuantClient(session)
                response = client.request("POST", "/simulations", json={})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(len(session.requests), 1)
            finally:
                if old is None:
                    os.environ.pop("BRAIN_CREDENTIALS_FILE", None)
                else:
                    os.environ["BRAIN_CREDENTIALS_FILE"] = old


if __name__ == "__main__":
    unittest.main()
