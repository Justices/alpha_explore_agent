import http.client
from contextlib import closing, redirect_stdout
from http.server import ThreadingHTTPServer
from io import StringIO
from types import SimpleNamespace
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from alpha_research.cli_status import run_status_command
from alpha_research.config import DEFAULT_CONFIG
from alpha_research.qualification.pool import policy_hash
from workbench import serve


class WorkbenchArgumentTests(unittest.TestCase):
    def test_regular_arguments_preserve_user_scope_and_ppa_profile(self):
        workflow, args, primary = serve.research_arguments({
            "workflow": "regular", "region": "USA", "objective": "quality",
            "dataset_ids": ["analyst10", "fundamental6"], "category": "Financial",
            "delay": 1, "universe": "TOP3000", "max_batches": 2, "ppa": True,
        })
        self.assertEqual(workflow, "regular")
        self.assertIsNone(primary)
        self.assertEqual(args, [
            "run-campaign", "--region=USA", "--objective=quality",
            "--max-batches=2", "--category=Financial", "--delay=1",
            "--universe=TOP3000", "--dataset=analyst10",
            "--dataset=fundamental6", "--ppa",
        ])

    def test_super_arguments_are_bounded_and_keep_native_scope(self):
        workflow, args, primary = serve.research_arguments({
            "workflow": "super", "region": "USA", "objective": "selection research",
            "delay": 1, "max_iterations": 4,
        })
        self.assertEqual(workflow, "super")
        self.assertIsNone(primary)
        self.assertEqual(args, [
            "run-super", "--region=USA", "--objective=selection research",
            "--max-iterations=4", "--delay=1",
        ])

    def test_optimization_requires_source_alpha_and_returns_it_as_primary_id(self):
        workflow, args, primary = serve.research_arguments({
            "workflow": "optimization", "alpha_id": "YPaepL9o",
            "objective": "reduce turnover", "region": "USA", "max_iterations": 3,
        })
        self.assertEqual(workflow, "optimization")
        self.assertEqual(primary, "YPaepL9o")
        self.assertEqual(args, [
            "run-optimization", "--alpha-id=YPaepL9o",
            "--objective=reduce turnover", "--max-iterations=3", "--region=USA",
        ])

    def test_invalid_workflow_budgets_and_unknown_fields_are_rejected(self):
        invalid = [
            {"workflow": "super", "region": "USA", "objective": "x", "max_iterations": 0},
            {"workflow": "optimization", "alpha_id": "A", "objective": "x", "max_iterations": 21},
            {"workflow": "optimization", "alpha_id": "A", "objective": "x", "surprise": True},
            {"workflow": "super", "region": "USA", "objective": "x", "delay": -1},
            {"workflow": "unknown", "region": "USA", "objective": "x"},
        ]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                serve.research_arguments(request)


class WorkbenchPlanTests(unittest.TestCase):
    def test_regular_plan_keeps_only_explicit_scope_constraints(self):
        workflow, args, primary = serve.plan_arguments({
            "workflow": "regular", "region": "USA",
            "dataset_ids": ["analyst10", "fundamental6"],
            "category": "Financial", "delay": 1, "universe": "TOP3000",
        })
        self.assertEqual(workflow, "regular")
        self.assertIsNone(primary)
        self.assertEqual(args, [
            "plan-regular", "--region=USA", "--delay=1",
            "--dataset=analyst10", "--dataset=fundamental6",
            "--category=Financial", "--universe=TOP3000",
        ])

    def test_super_plan_uses_native_platform_selection_preflight(self):
        workflow, args, primary = serve.plan_arguments({
            "workflow": "super", "region": "USA",
            "objective": "selection research", "delay": 1,
        })
        self.assertEqual(workflow, "super")
        self.assertIsNone(primary)
        self.assertEqual(args, [
            "plan-super", "--region=USA",
            "--objective=selection research", "--delay=1",
        ])

    def test_optimization_plan_pins_the_source_alpha(self):
        workflow, args, primary = serve.plan_arguments({
            "workflow": "optimization", "alpha_id": "YPaepL9o",
            "objective": "reduce turnover", "region": "USA",
        })
        self.assertEqual(workflow, "optimization")
        self.assertEqual(primary, "YPaepL9o")
        self.assertEqual(args, [
            "plan-optimization", "--alpha-id=YPaepL9o",
            "--objective=reduce turnover", "--region=USA",
        ])

    def test_preflight_rejects_ambiguous_or_unsupported_scope(self):
        invalid = [
            {"workflow": "regular", "region": "USA", "dataset_ids": []},
            {"workflow": "regular", "region": "USA",
             "dataset_ids": ["analyst10", "analyst10"]},
            {"workflow": "regular", "region": "USA", "delay": 0},
            {"workflow": "super", "region": "USA", "objective": "x",
             "dataset_ids": ["analyst10"]},
            {"workflow": "optimization", "alpha_id": "A", "surprise": True},
        ]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                serve.plan_arguments(request)


class WorkbenchRecoveryPathTests(unittest.TestCase):
    def test_recovery_accepts_only_existing_intents_inside_state_root(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as outside:
            intent = Path(root) / "state" / "research_v1_1" / "optimization" / "run_simulation_intent.json"
            intent.parent.mkdir(parents=True)
            intent.write_text('{"status":"POSTING"}', encoding="utf-8")
            self.assertEqual(serve.recovery_intent_path(root, str(intent)), intent)
            arbitrary = intent.with_name("report.json")
            arbitrary.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                serve.recovery_intent_path(root, str(arbitrary))
            foreign = Path(outside) / "run_simulation_intent.json"
            foreign.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "之外"):
                serve.recovery_intent_path(root, str(foreign))
            with self.assertRaises(ValueError):
                serve.recovery_intent_path(root, str(intent.parent / ".." / intent.name))


    def test_recovery_inspection_is_read_only_and_rejects_wrong_workflow(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "run_simulation_intent.json"
            record = {"workflow": "super", "status": "POSTING",
                      "request": {}, "settings": {}, "run_id": "run-1"}
            path.write_text(json.dumps(record), encoding="utf-8")
            coordinator = serve.RecoveryCoordinator(root)
            self.assertEqual(coordinator.inspect_target(path,
                expected_workflow="super")["status"], "RECOVERABLE")
            self.assertFalse(coordinator.journal_root.exists())
            with self.assertRaises(serve.RecoveryError):
                coordinator.inspect_target(path, expected_workflow="optimization")
            record["status"] = "PREPARED"
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(serve.RecoveryError):
                coordinator.inspect_target(path, expected_workflow="super")
            self.assertFalse(coordinator.journal_root.exists())

    def test_recovery_inspection_rejects_unsettled_or_changed_journal(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "run_simulation_intent.json"
            path.write_text(json.dumps({"workflow": "optimization", "status": "POSTING",
                "request": {}, "settings": {}, "run_id": "run-1"}), encoding="utf-8")
            coordinator = serve.RecoveryCoordinator(root)
            workflow, resolved, _, fingerprint = coordinator._inspect(path.resolve())
            journal = coordinator._journal_path(workflow, resolved)
            journal.parent.mkdir(parents=True)
            value = {"target_fingerprint": fingerprint, "status": "RUNNING",
                     "task_id": None, "step": None}
            journal.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(coordinator.inspect_target(path,
                expected_workflow="optimization")["status"], "BLOCKED")
            value["target_fingerprint"] = "other"
            journal.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(serve.RecoveryError):
                coordinator.inspect_target(path, expected_workflow="optimization")


class WorkbenchStatusPolicyTests(unittest.TestCase):
    def test_status_keeps_unqualified_candidate_and_preferred_evaluations_visible(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            config = root_path / "config" / "nastu_quant.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"scope": {"region": "USA"}}), encoding="utf-8")
            candidate_dir = root_path / "state" / "research_v1_1" / "candidate"
            preferred_dir = root_path / "state" / "research_v1_1" / "preferred"
            candidate_dir.mkdir(parents=True)
            preferred_dir.mkdir(parents=True)
            (candidate_dir / "candidate-1.json").write_text(json.dumps({
                "candidate_id": "candidate-1", "alpha_id": "ALPHA-1",
                "workflow": "super", "status": "RA_UNAVAILABLE",
                "failures": [], "pending": ["ra_checks_unsettled_or_unavailable"],
                "metrics": {"sharpe": 1.25}, "gate_evidence": {"ra_fail_count": None},
                "updated_at": "2026-11-27T10:00:00+00:00",
            }), encoding="utf-8")
            (preferred_dir / "candidate-1.json").write_text(json.dumps({
                "candidate_id": "candidate-1", "alpha_id": "ALPHA-1",
                "workflow": "super", "status": "NOT_PREFERRED",
                "qualified": False, "pending": ["candidate_not_qualified"],
                "updated_at": "2026-11-27T10:00:00+00:00",
            }), encoding="utf-8")
            output = StringIO()
            with patch("alpha_research.cli_status.CandidatePool.build_index",
                       return_value={"entries": [], "candidate_count": 0, "preferred_count": 0}):
                with redirect_stdout(output):
                    self.assertEqual(run_status_command(
                        SimpleNamespace(config=config), root_path,
                        root_path / "state" / "stop.flag"), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(len(report["v11_candidate_evaluations"]), 1)
            self.assertEqual(report["global_alpha_pool"]["status"], "available")
            candidate = report["v11_candidate_evaluations"][0]
            self.assertEqual(candidate["alpha_id"], "ALPHA-1")
            self.assertEqual(candidate["candidate_id"], "candidate-1")
            self.assertEqual(candidate["status"], "RA_UNAVAILABLE")
            self.assertEqual(candidate["pending"], ["ra_checks_unsettled_or_unavailable"])
            self.assertEqual(candidate["metrics"]["sharpe"], 1.25)
            self.assertEqual(len(report["v11_preferred_evaluations"]), 1)
            self.assertEqual(report["v11_preferred_evaluations"][0]["status"], "NOT_PREFERRED")
            self.assertEqual(report["v11_candidates"], [])
            self.assertEqual(report["v11_preferred"], [])

    def test_current_evaluation_is_not_lost_when_history_is_bounded(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            folder = root_path / "state" / "research_v1_1" / "candidate"
            folder.mkdir(parents=True)
            for index in range(201):
                identifier = f"candidate-{index:03d}"
                (folder / f"{identifier}.json").write_text(json.dumps({
                    "candidate_id": identifier, "alpha_id": f"ALPHA-{index}",
                    "workflow": "regular", "status": "RA_UNAVAILABLE",
                    "updated_at": f"{index:03d}",
                }), encoding="utf-8")
            output = StringIO()
            with patch("alpha_research.cli_status.CandidatePool.build_index",
                       return_value={"entries": [{"alpha_id": "ALPHA-0",
                           "candidate_id": "candidate-000",
                           "candidate_status": "RA_UNAVAILABLE",
                           "preferred_status": "NOT_CANDIDATE"}]}):
                with redirect_stdout(output):
                    self.assertEqual(run_status_command(
                        SimpleNamespace(config=None), root_path,
                        root_path / "state" / "stop.flag"), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(len(report["v11_candidate_evaluations"]), 200)
            self.assertNotIn("candidate-000", {row["candidate_id"]
                for row in report["v11_candidate_evaluations"]})
            self.assertEqual([row["candidate_id"] for row in
                report["v11_current_candidate_evaluations"]], ["candidate-000"])

    def test_campaign_projection_preserves_scope_budget_and_partial_coverage(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            folder = root_path / "state" / "research_v1_1" / "campaign_runs"
            folder.mkdir(parents=True)
            campaign_id = "a" * 32
            first = {"dataset_id": "analyst10", "region": "USA",
                     "delay": 1, "universe": "TOP3000", "category": "Analyst"}
            second = {**first, "universe": "TOP1000"}
            budget = {"limits": {"batches": 2}, "used": {"batches": 1},
                      "reserved": {"batches": 1}}
            request = {"objective": "revision research", "region": "USA",
                       "dataset_ids": ["analyst10"], "category": None,
                       "delay": 1, "universe": None, "max_batches": 2,
                       "ppa_profile": False}
            (folder / f"{campaign_id}.json").write_text(json.dumps({
                "campaign_id": campaign_id, "status": "unresolved",
                "request": request, "items": [first, second],
                "completed": [{"item_index": 0}], "active_item": 1,
                "budget": budget, "subtasks": [],
                "plan": {"unresolved": [{"reason": "catalog unavailable"}],
                         "unselected_dataset_ids": ["other"]},
                "error": "evidence missing",
            }), encoding="utf-8")
            output = StringIO()
            with patch("alpha_research.cli_status.CandidatePool.build_index",
                       return_value={"entries": []}), patch(
                           "alpha_research.cli_status.campaign_can_resume",
                           return_value=True):
                with redirect_stdout(output):
                    self.assertEqual(run_status_command(
                        SimpleNamespace(config=None), root_path,
                        root_path / "state" / "stop.flag"), 0)
            row = json.loads(output.getvalue())["v11_campaigns"][0]
            self.assertEqual(row["request"], request)
            self.assertEqual(row["budget"], budget)
            self.assertEqual(row["active_scope"], second)
            self.assertEqual(row["coverage"], {"eligible_combinations": 2,
                "completed_combinations": 1, "unexamined_combinations": 1})
            self.assertEqual(row["unselected_dataset_ids"], ["other"])
            self.assertEqual(row["error"], "evidence missing")

    def test_status_projects_optimization_source_and_candidate_metrics_side_by_side(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            config = root_path / "config" / "nastu_quant.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"scope": {"region": "USA"}}), encoding="utf-8")
            state = root_path / "state" / "research_v1_1"
            snapshot_dir = state / "optimization" / "snapshots"
            source_dir = state / "optimization_source"
            branch_dir = state / "candidate_branch"
            candidate_dir = state / "candidate"
            for folder in (snapshot_dir, source_dir, branch_dir, candidate_dir):
                folder.mkdir(parents=True)
            snapshot = snapshot_dir / "source-1.json"
            snapshot.write_text(json.dumps({
                "id": "SOURCE", "type": "REGULAR",
                "settings": {"region": "USA", "delay": 1, "universe": "TOP3000"},
                "regular": "rank(signal_a)",
                "is": {"sharpe": 1.0, "turnover": 0.2, "drawdown": 0.1},
            }), encoding="utf-8")
            (source_dir / "source-1.json").write_text(json.dumps({
                "source_id": "source-1", "alpha_id": "SOURCE",
                "snapshot_ref": str(snapshot), "status": "captured",
                "workflow": "optimization",
            }), encoding="utf-8")
            (branch_dir / "branch-1.json").write_text(json.dumps({
                "branch_id": "branch-1", "workflow": "optimization",
                "source_alpha_id": "SOURCE", "source_snapshot_ref": str(snapshot),
                "candidate_alpha_id": "CANDIDATE", "status": "completed",
                "effective_settings": {"region": "USA", "delay": 1, "universe": "TOP3000"},
                "objective": "improve risk adjusted return",
                "changes": ["revise combo expression"],
            }), encoding="utf-8")
            (candidate_dir / "candidate-1.json").write_text(json.dumps({
                "candidate_id": "candidate-1", "candidate_ref": "branch-1",
                "alpha_id": "CANDIDATE", "workflow": "optimization",
                "status": "RA_UNAVAILABLE", "metrics": {
                    "sharpe": 1.4, "turnover": 0.25, "drawdown": 0.08,
                }, "failures": [], "pending": ["ra_checks_unsettled_or_unavailable"],
            }), encoding="utf-8")
            output = StringIO()
            with patch("alpha_research.cli_status.CandidatePool.build_index",
                       return_value={"status": "available", "entries": []}):
                with redirect_stdout(output):
                    self.assertEqual(run_status_command(
                        SimpleNamespace(config=config), root_path,
                        root_path / "state" / "stop.flag"), 0)
            report = json.loads(output.getvalue())
            branch = report["v11_candidate_branches"][0]
            self.assertEqual(branch["source_metrics"]["sharpe"], 1.0)
            self.assertEqual(branch["candidate_metrics"]["sharpe"], 1.4)
            self.assertAlmostEqual(branch["metric_deltas"]["sharpe"], 0.4)
            self.assertAlmostEqual(branch["metric_deltas"]["turnover"], 0.05)
            self.assertAlmostEqual(branch["metric_deltas"]["drawdown"], -0.02)
            self.assertEqual(branch["candidate_pending"], [
                "ra_checks_unsettled_or_unavailable"])

    def test_status_exposes_canonical_application_owned_policy(self):
        with TemporaryDirectory() as root:
            config = Path(root) / "config" / "nastu_quant.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"scope": {"region": "USA"}}),
                              encoding="utf-8")
            output = StringIO()
            args = SimpleNamespace(config=config)
            with patch("alpha_research.cli_status.CandidatePool.build_index",
                       return_value={"status": "available", "entries": []}), \
                    redirect_stdout(output):
                self.assertEqual(
                    run_status_command(args, Path(root), Path(root) / "state" / "stop.flag"),
                    0)
            report = json.loads(output.getvalue())
            policy = report["qualification_policy"]
            self.assertEqual(policy["status"], "available")
            self.assertEqual(policy["candidate"], DEFAULT_CONFIG["gate"])
            self.assertEqual(policy["preferred"], DEFAULT_CONFIG["preferred"])
            self.assertEqual(policy["policy_sha256"], policy_hash(DEFAULT_CONFIG))


    def test_status_projects_super_scope_eligibility_and_unresolved_evidence(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            config = root_path / "config" / "nastu_quant.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"scope": {"region": "USA"}}), encoding="utf-8")
            state = root_path / "state" / "research_v1_1"
            records = state / "super_run"
            snapshots = state / "super_runs"
            records.mkdir(parents=True)
            snapshots.mkdir(parents=True)
            run_id = "a" * 32
            snapshot = snapshots / f"{run_id}.json"
            snapshot.write_text(json.dumps({
                "run_id": run_id, "region": "USA",
                "objective": "selection quality", "requested_delay": 1,
                "allowed_delays": [1, 2],
                "active_submitted_pool": [{"alpha_id": "A1"}, {"alpha_id": "A2"}],
                "unresolved": [{"reason": "settings_unreadable"}],
                "platform_super_schema": {"selectionLimit": {"type": "integer"}},
            }), encoding="utf-8")
            batch_id = "20261127_120000_abcd1234"
            (snapshots / f"{run_id}_{batch_id}_prereg.json").write_text(json.dumps({
                "proposal": {
                    "selection": "rank(signal_a)", "combo": "rank(signal_b)",
                    "hypothesis": "test selection", "rationale": "quality signal",
                    "failure_criterion": "sharpe below target",
                    "settings": {"region": "USA", "delay": 1},
                },
                "validation": {"status": "PASS"},
            }), encoding="utf-8")
            (records / f"{run_id}.json").write_text(json.dumps({
                "run_id": run_id, "batch_id": batch_id,
                "workflow": "super", "region": "USA",
                "objective": "selection quality", "status": "candidate_evaluated",
                "platform_record_ref": str(snapshot),
                "simulation_intent_ref": str(root_path / "intent.json"),
                "candidate_alpha_id": "C1", "candidate_status": "CANDIDATE",
                "preferred_status": "NOT_PREFERRED",
                "updated_at": "2026-11-27T10:00:00+00:00",
            }), encoding="utf-8")
            (records / "malformed.json").write_text("[]", encoding="utf-8")
            output = StringIO()
            with patch("alpha_research.cli_status.CandidatePool.build_index",
                       return_value={"status": "available", "entries": []}):
                with redirect_stdout(output):
                    self.assertEqual(run_status_command(
                        SimpleNamespace(config=config), root_path,
                        root_path / "state" / "stop.flag"), 0)
            report = json.loads(output.getvalue())
            run = next(row for row in report["v11_super_runs"] if row.get("run_id") == run_id)
            self.assertEqual(run["region"], "USA")
            self.assertEqual(run["requested_delay"], 1)
            self.assertEqual(run["allowed_delays"], [1, 2])
            self.assertEqual(run["eligible_alpha_count"], 2)
            self.assertEqual(run["unresolved_count"], 1)
            self.assertEqual(run["selection"], "rank(signal_a)")
            self.assertEqual(run["combo"], "rank(signal_b)")
            self.assertEqual(run["settings"]["delay"], 1)
            self.assertEqual(run["validation"]["status"], "PASS")
            self.assertEqual(run["candidate_alpha_id"], "C1")
            self.assertEqual(run["simulation_intent_ref"], str(root_path / "intent.json"))
            self.assertTrue(any(row.get("status") == "unreadable"
                                for row in report["v11_super_runs"]))


class WorkbenchRecordStoreTests(unittest.TestCase):
    def test_project_records_round_trip_through_locked_store(self):
        with TemporaryDirectory() as root:
            payload = {
                "version": 1,
                "cases": [{
                    "id": "case-1", "name": "USA Regular", "workflow": "regular",
                    "region": "USA", "phase": "scope", "ppa": False,
                }],
                "alphas": [{
                    "id": "alpha-1", "caseId": "case-1", "name": "Analyst Alpha",
                    "metrics": {},
                }],
            }
            result = serve.save_workbench_records(root, payload)
            self.assertEqual(result, {
                "saved": True, "case_count": 1, "alpha_count": 1,
            })
            self.assertEqual(serve.load_workbench_records(root), payload)

    def test_project_records_reject_stale_revision(self):
        with TemporaryDirectory() as root:
            original = serve.load_workbench_records(root)
            revision = serve.workbench_revision(original)
            first = {"version": 1, "cases": [{"id": "first", "name": "First",
                "workflow": "regular", "region": "USA", "phase": "scope"}], "alphas": []}
            second = {"version": 1, "cases": [{"id": "second", "name": "Second",
                "workflow": "regular", "region": "USA", "phase": "scope"}], "alphas": []}
            serve.save_workbench_records(root, first, expected_revision=revision)
            with self.assertRaises(serve.WorkbenchRecordConflict):
                serve.save_workbench_records(root, second, expected_revision=revision)
            self.assertEqual(serve.load_workbench_records(root), first)
            serve.save_workbench_records(root, second,
                expected_revision=serve.workbench_revision(first))
            self.assertEqual(serve.load_workbench_records(root), second)

    def test_records_http_contract_rejects_missing_and_stale_preconditions(self):
        with TemporaryDirectory() as root:
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
            port = server.server_address[1]
            server.RequestHandlerClass = serve.handler_for(["--state-root", root], port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            origin = f"http://127.0.0.1:{port}"

            def call(method, payload=None, revision=None):
                headers = {"Origin": origin, "Content-Type": "application/json"}
                if revision is not None:
                    headers["If-Match"] = revision
                with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as conn:
                    conn.request(method, "/api/workbench-records",
                                 json.dumps(payload) if payload is not None else None,
                                 headers)
                    response = conn.getresponse()
                    return response.status, response.getheader("ETag"), json.loads(response.read())

            status, revision, original = call("GET")
            self.assertEqual(status, 200)
            self.assertEqual(revision, serve.workbench_revision(original))
            first = {"version": 1, "cases": [{"id": "first", "name": "First",
                "workflow": "regular", "region": "USA", "phase": "scope"}], "alphas": []}
            self.assertEqual(call("POST", first)[0], 428)
            status, next_revision, _ = call("POST", first, revision)
            self.assertEqual(status, 200)
            self.assertEqual(next_revision, serve.workbench_revision(first))
            self.assertEqual(call("POST", original, revision)[0], 412)
            self.assertEqual(call("GET")[2], first)

    def test_project_records_reject_duplicate_ids_and_non_finite_values(self):
        valid_case = {
            "id": "same", "name": "USA Regular", "workflow": "regular",
            "region": "USA", "phase": "scope", "ppa": False,
        }
        duplicate = {
            "version": 1,
            "cases": [valid_case, dict(valid_case)],
            "alphas": [],
        }
        non_finite = {
            "version": 1,
            "cases": [{
                "id": "case-1", "name": "USA Regular", "workflow": "regular",
                "region": "USA", "phase": "scope", "metric": float("nan"),
            }],
            "alphas": [],
        }
        with self.assertRaisesRegex(ValueError, "重复"):
            serve._validate_workbench_records(duplicate)
        with self.assertRaisesRegex(ValueError, "无法保存"):
            serve._validate_workbench_records(non_finite)


class WorkbenchPlanEndpointTests(unittest.TestCase):
    def test_read_only_preflight_does_not_require_model_roles(self):
        entered = threading.Event()
        commands = []
        child_credentials = []

        def run(command, **kwargs):
            commands.append(command)
            child_credentials.append(json.loads(kwargs["input"]))
            entered.set()
            return CompletedProcess(command, 0, json.dumps({
                "status": "planned", "plan_id": "plan-123",
                "datasets": [{"combinations": [{}, {}]}], "unresolved": [],
            }))

        with TemporaryDirectory() as root, \
                patch.object(serve.subprocess, "run", side_effect=run):
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
            port = server.server_address[1]
            server.RequestHandlerClass = serve.handler_for(["--state-root", root], port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            origin = f"http://127.0.0.1:{port}"

            def post(path, payload, cookie=""):
                with closing(http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5)) as connection:
                    connection.request("POST", path, json.dumps(payload), {
                        "Origin": origin, "Content-Type": "application/json",
                        "Cookie": cookie,
                    })
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    return response.status, response.getheader("Set-Cookie"), body

            status, cookie_header, _ = post("/api/credentials", {
                "username": "offline@example.invalid", "password": "offline-secret",
            })
            self.assertEqual(status, 200)
            cookie = cookie_header.split(";", 1)[0]
            status, _, launched = post("/api/plan", {
                "workflow": "regular", "region": "USA",
                "dataset_ids": ["analyst10"], "delay": 1,
            }, cookie)
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(3))

            deadline = time.monotonic() + 3
            report = None
            while time.monotonic() < deadline:
                with closing(http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5)) as connection:
                    connection.request("GET", "/api/launch")
                    response = connection.getresponse()
                    report = json.loads(response.read())
                if report["status"] != "running":
                    break
                time.sleep(0.01)

            self.assertEqual(report["status"], "exited")
            self.assertEqual(report["workflow"], "regular_plan")
            self.assertEqual(report["plan_id"], "plan-123")
            self.assertEqual(report["dataset_count"], 1)
            self.assertEqual(report["combination_count"], 2)
            self.assertEqual(child_credentials, [{
                "username": "offline@example.invalid", "password": "offline-secret",
            }])
            self.assertIn("plan-regular", commands[0])
            self.assertNotIn("--lead", commands[0])
            self.assertNotIn("--executor", commands[0])
            self.assertNotIn("run-campaign", commands[0])
            self.assertEqual(launched["workflow"], "regular_plan")


class WorkbenchConfiguredCredentialsTests(unittest.TestCase):
    def test_file_mode_delegates_credentials_to_cli_without_browser_password(self):
        with TemporaryDirectory() as root:
            credential_path = Path(root) / "credentials.json"
            credential_path.write_text(json.dumps({"email": "fixture@example.invalid",
                "password": "fixture-secret"}), encoding="utf-8")
            calls = []

            def run(command, **kwargs):
                calls.append((command, kwargs))
                return CompletedProcess(command, 0, json.dumps({
                    "status": "planned", "plan_id": "fixture-plan"}))

            with patch.dict(os.environ, {"BRAIN_CREDENTIALS_FILE": str(credential_path)}), \
                    patch.object(serve.subprocess, "run", side_effect=run):
                server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
                port = server.server_address[1]
                server.RequestHandlerClass = serve.handler_for(
                    ["--state-root", root], port, credentials_from_config=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    def call(method, path, payload=None):
                        with closing(http.client.HTTPConnection(
                                "127.0.0.1", port, timeout=5)) as conn:
                            conn.request(method, path,
                                json.dumps(payload) if payload is not None else None,
                                {"Origin": f"http://127.0.0.1:{port}",
                                 "Content-Type": "application/json"})
                            response = conn.getresponse()
                            return response.status, json.loads(response.read())

                    status, credentials = call("GET", "/api/credentials")
                    self.assertEqual(status, 200)
                    self.assertEqual(credentials, {"configured": True,
                        "source": "config_file", "authentication_verified": False})
                    self.assertEqual(call("POST", "/api/credentials",
                        {"username": "other", "password": "other"})[0], 409)
                    status, launch = call("POST", "/api/plan",
                        {"workflow": "regular", "region": "USA", "delay": 1})
                    self.assertEqual(status, 202)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        _, report = call("GET", "/api/launch")
                        if report["status"] != "running":
                            break
                        time.sleep(0.01)
                    self.assertEqual(report["status"], "exited")
                    self.assertEqual(len(calls), 1)
                    command, kwargs = calls[0]
                    self.assertNotIn("--brain-credentials-stdin", command)
                    self.assertNotIn("input", kwargs)
                    self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                    self.assertNotIn("fixture-secret", json.dumps(report))
                    credential_path.unlink()
                    self.assertFalse(call("GET", "/api/credentials")[1]["configured"])
                    self.assertEqual(call("POST", "/api/plan",
                        {"workflow": "regular", "region": "USA", "delay": 1})[0], 401)
                    self.assertEqual(len(calls), 1)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)


class WorkbenchFrontendSyntaxTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_inline_workbench_javascript_parses(self):
        html_path = Path(__file__).resolve().parent.parent / "workbench" / "AlphaResearchWorkbench.html"
        html = html_path.read_text(encoding="utf-8")
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.IGNORECASE | re.DOTALL)
        self.assertTrue(scripts, "Workbench must contain an inline application script")
        with TemporaryDirectory() as root:
            for index, source in enumerate(scripts):
                script_path = Path(root) / f"workbench-{index}.js"
                script_path.write_text(source, encoding="utf-8")
                result = subprocess.run(
                    ["node", "--check", str(script_path)],
                    capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)


    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_agent_projection_requires_matching_identity_and_evidence(self):
        html = (Path(__file__).resolve().parent.parent / "workbench" /
                "AlphaResearchWorkbench.html").read_text(encoding="utf-8")
        source = html.split("function agentAlphaRows(){", 1)[1].split(
            "let agentSelected=", 1)[0]
        script = """
const assert=require("node:assert/strict");
const localAgent=true;
const numeric=v=>typeof v==="number"&&Number.isFinite(v);
const sha="a".repeat(64), policy="b".repeat(64);
const entry={alpha_id:"A",candidate_id:"C",candidate_status:"CANDIDATE",
 preferred_status:"PREFERRED",latest_evidence_sha256:sha,latest_policy_sha256:policy};
const evaluation={alpha_id:"A",candidate_id:"C",evidence_sha256:sha,
 policy_sha256:policy,metrics:{sharpe:2},correlations:{prod:0.2,self:0.3}};
let agentStatus={global_alpha_pool:{status:"available",entries:[entry]},
 v11_candidate_evaluations:[evaluation]};
""" + "function agentAlphaRows(){" + source + """
assert.equal(agentAlphaRows()[0].metrics.sharpe,2);
assert.equal(agentAlphaRows()[0].preferred,"PREFERRED");
agentStatus.v11_current_candidate_evaluations=[evaluation];
agentStatus.v11_candidate_evaluations=[];
assert.equal(agentAlphaRows()[0].metrics.sharpe,2);
delete agentStatus.v11_current_candidate_evaluations;
for(const wrong of [{alpha_id:"B"},{candidate_id:"D"},
 {evidence_sha256:"c".repeat(64)},{policy_sha256:"d".repeat(64)},
 {evidence_origin:"OFFLINE_DOMAIN_MOCK"}]){
 agentStatus.v11_candidate_evaluations=[{...evaluation,...wrong}];
 assert.equal(agentAlphaRows()[0].evidenceAvailable,false);
 assert.equal(agentAlphaRows()[0].metrics.sharpe,undefined);
}
agentStatus.v11_candidate_evaluations=[evaluation,evaluation];
assert.equal(agentAlphaRows()[0].evidenceAvailable,false);
agentStatus.global_alpha_pool.status="unavailable";
assert.deepEqual(agentAlphaRows(),[]);
"""
        with TemporaryDirectory() as root:
            path = Path(root) / "agent-projection.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(["node", str(path)], capture_output=True,
                                    text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)


class WorkbenchLaunchPersistenceTests(unittest.TestCase):
    def test_inflight_launch_is_restored_as_unknown_and_blocks_replay(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root) / "state" / "logs" / "workbench"
            launch_id = "a" * 32
            serve.write_launch_manifest(log_dir, launch_id, {
                "workflow": "super", "status": "running",
                "started_at": "2026-11-27T10:00:00+00:00",
                "args": ["run-super"], "models": None,
            })

            restored = serve.restore_launch_state(log_dir)

            self.assertEqual(restored["launch_id"], launch_id)
            self.assertEqual(restored["status"], "execution_unknown")
            self.assertTrue(restored["recovery_required"])
            self.assertIn("避免并发 Simulation", restored["recovery_message"])
            persisted = json.loads(
                (log_dir / f"{launch_id}.launch.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "execution_unknown")
            self.assertEqual(serve.read_launch_history(log_dir)[0]["launch_id"], launch_id)

    def test_legacy_terminal_progress_is_not_misclassified_as_running(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root) / "state" / "logs" / "workbench"
            launch_id = "b" * 32
            serve.write_launch_manifest(log_dir, launch_id, {
                "workflow": "regular", "status": "starting",
                "started_at": "2026-11-27T10:00:00+00:00",
            })
            (log_dir / f"{launch_id}.jsonl").write_text(
                '{"time":"2026-11-27T10:01:00+00:00","message":"研究进程已结束，结果见 Agent 状态与证据记录"}\n',
                encoding="utf-8")

            restored = serve.restore_launch_state(log_dir)

            self.assertEqual(restored["status"], "exited")
            self.assertTrue(restored["recovered_from_legacy_log"])
            self.assertEqual(restored["result_status"], "legacy_completion_observed")

    def test_older_unresolved_launch_is_not_hidden_by_newer_terminal_record(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root) / "state" / "logs" / "workbench"
            old_id, new_id = "a" * 32, "b" * 32
            serve.write_launch_manifest(log_dir, old_id, {"status": "running"})
            serve.write_launch_manifest(log_dir, new_id, {"status": "exited"})
            restored = serve.restore_launch_state(log_dir)
            self.assertEqual(restored["launch_id"], old_id)
            self.assertEqual(restored["status"], "execution_unknown")

    def test_corrupt_status_blocks_launch_restoration(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root) / "state" / "logs" / "workbench"
            serve.write_launch_manifest(log_dir, "a" * 32, {"status": []})
            restored = serve.restore_launch_state(log_dir)
            self.assertEqual(restored["status"], "execution_unknown")
            self.assertTrue(restored["manifest_history_unreadable"])

    def test_legacy_terminal_log_does_not_hide_other_unresolved_launch(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root) / "state" / "logs" / "workbench"
            old_id, new_id = "a" * 32, "b" * 32
            serve.write_launch_manifest(log_dir, old_id, {"status": "running"})
            serve.write_launch_manifest(log_dir, new_id, {"status": "starting"})
            (log_dir / f"{new_id}.jsonl").write_text(json.dumps({
                "time": "2026-11-27T10:01:00+00:00",
                "message": "研究进程已结束，结果见 Agent 状态与证据记录",
            }) + "\n", encoding="utf-8")
            restored = serve.restore_launch_state(log_dir)
            self.assertEqual(restored["launch_id"], old_id)
            self.assertEqual(restored["status"], "execution_unknown")

    def test_launch_manifest_rejects_untrusted_identifiers(self):
        with TemporaryDirectory() as root:
            for launch_id in ("../escape", "A" * 32, "", "not-a-uuid"):
                with self.subTest(launch_id=launch_id), self.assertRaises(ValueError):
                    serve.launch_manifest_path(root, launch_id)


class WorkbenchLaunchRestartGuardTests(unittest.TestCase):
    def test_restart_blocks_new_launch_until_operator_acknowledges_old_process_exit(self):
        old_launch_id = "a" * 32
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            self.assertEqual(json.loads(kwargs["input"]), {
                "username": "offline@example.invalid", "password": "offline-secret",
            })
            return CompletedProcess(command, 0, json.dumps({
                "status": "COMPLETED", "run_id": "new-super-run",
            }))

        with TemporaryDirectory() as root, \
                patch.dict(os.environ, {
                    "ZAI_API_KEY": "offline-fixture",
                    "MOONSHOT_API_KEY": "offline-fixture",
                }), patch.object(serve.subprocess, "run", side_effect=run):
            config = Path(root) / "config" / "nastu_quant.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({
                "model_governance": {"max_calls": 5, "max_reported_tokens": 1000,
                                     "failover": "manual"},
            }), encoding="utf-8")
            log_dir = Path(root) / "state" / "logs" / "workbench"
            serve.write_launch_manifest(log_dir, old_launch_id, {
                "workflow": "super", "status": "running",
                "started_at": "2026-11-27T10:00:00+00:00",
                "args": ["run-super"], "models": None,
            })
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
            port = server.server_address[1]
            server.RequestHandlerClass = serve.handler_for(["--state-root", root], port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            origin = f"http://127.0.0.1:{port}"

            def post(path, payload, cookie=""):
                with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
                    connection.request("POST", path, json.dumps(payload), {
                        "Origin": origin, "Content-Type": "application/json", "Cookie": cookie,
                    })
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    return response.status, response.getheader("Set-Cookie"), body

            def get(path):
                with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())

            status, cookie_header, _ = post("/api/credentials", {
                "username": "offline@example.invalid", "password": "offline-secret",
            })
            self.assertEqual(status, 200)
            cookie = cookie_header.split(";", 1)[0]
            status, restored = get("/api/launch")
            self.assertEqual(status, 200)
            self.assertEqual(restored["status"], "execution_unknown")
            request = {
                "workflow": "super", "region": "USA", "objective": "offline restart guard",
                "max_iterations": 1, "lead": "glm", "executor": "kimi",
            }
            status, _, blocked = post("/api/launch", request, cookie)
            self.assertEqual(status, 409)
            self.assertIn("旧子进程", blocked["error"])
            self.assertEqual(commands, [])
            for action in ("resume", "delete"):
                payload = {"action": action, "id": "b" * 32}
                if action == "resume":
                    payload.update(lead="glm", executor="kimi")
                status, _, blocked = post("/api/research", payload, cookie)
                self.assertEqual(status, 409)
                self.assertIn("旧子进程", blocked["error"])
            self.assertEqual(commands, [])

            status, _, acknowledged = post("/api/research", {
                "action": "acknowledge-interrupted", "id": old_launch_id,
            }, cookie)
            self.assertEqual(status, 200)
            self.assertEqual(acknowledged["status"], "interrupted_acknowledged")
            old_manifest = json.loads(
                (log_dir / f"{old_launch_id}.launch.json").read_text(encoding="utf-8"))
            self.assertEqual(old_manifest["status"], "interrupted_acknowledged")

            status, _, started = post("/api/launch", request, cookie)
            self.assertEqual(status, 202)
            deadline = time.monotonic() + 3
            latest = None
            while time.monotonic() < deadline:
                _, latest = get("/api/launch")
                if latest["status"] != "running":
                    break
                time.sleep(0.01)
            self.assertEqual(latest["status"], "exited")
            self.assertEqual(latest["run_id"], "new-super-run")
            self.assertEqual(len(commands), 1)
            self.assertNotEqual(started["launch_id"], old_launch_id)


class WorkbenchLaunchTests(unittest.TestCase):
    def test_launch_keeps_scope_and_rejects_cross_origin_and_duplicate_start(self):
        entered = threading.Event()
        release = threading.Event()
        commands = []
        child_credentials = []

        def run(command, **kwargs):
            commands.append(command)
            child_credentials.append(json.loads(kwargs["input"]))
            log_path = kwargs["env"].get("ALPHA_RESEARCH_PROGRESS_FILE")
            if log_path:
                with Path(log_path).open("a", encoding="utf-8") as stream:
                    stream.write('{"time":"2026-11-10T07:32:00Z","message":"正在规划字段诊断"}\n')
            entered.set()
            release.wait(5)
            return CompletedProcess(command, 0, '{"campaign_id":"case","status":"budget_complete"}')

        with TemporaryDirectory() as root, \
                patch.dict(os.environ, {"ZAI_API_KEY": "offline-fixture",
                                         "MOONSHOT_API_KEY": "offline-fixture"}), \
                patch.object(serve.subprocess, "run", side_effect=run), \
                patch.object(serve, "monotonic", create=True, return_value=100) as clock:
            cfg = Path(root) / "config" / "nastu_quant.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({
                "model_governance": {
                    "max_calls": 5, "max_reported_tokens": 1000,
                    "failover": "manual",
                },
            }), encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
            port = server.server_address[1]
            server.RequestHandlerClass = serve.handler_for(["--state-root", root], port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            request = {"lead": "glm", "executor": "kimi",
                       "region": "EUR", "objective": "--example",
                       "dataset_ids": ["risk60", "model110"], "delay": 1,
                       "universe": "TOPCS1600", "ppa": True}
            body = json.dumps(request)
            saved_id = "a" * 32
            saved_path = Path(root) / "state/research_v1_1/campaign_runs" / f"{saved_id}.json"
            saved_path.parent.mkdir(parents=True)
            saved_path.write_text(json.dumps({
                "campaign_id": saved_id, "status": "user_stopped", "active_item": 0,
            }), encoding="utf-8")
            batch = Path(root) / "runs/EUR/D1/TOPCS1600/risk60/batch1"
            batch.mkdir(parents=True)
            (batch / "batch_context.json").write_text(
                json.dumps({"research_id": f"regular:campaign:{saved_id}"}), encoding="utf-8")
            intent = batch / "simulation_intent.json"
            intent.write_text('{"status":"SUBMITTING"}', encoding="utf-8")
            summary = saved_path.with_name(f"{saved_id}_summary.json")
            summary.write_text(json.dumps({"campaign_id": saved_id}), encoding="utf-8")

            def post(origin, data=body, *, path="/api/launch", cookie=""):
                with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
                    connection.request("POST", path, data, {
                        "Origin": origin, "Content-Type": "application/json", "Cookie": cookie})
                    response = connection.getresponse()
                    response.read()
                    return response.status, response.getheader("Set-Cookie")

            def status():
                with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
                    connection.request("GET", "/api/launch")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    return json.loads(response.read())

            try:
                self.assertEqual(post("https://example.com")[0], 403)
                origin = f"http://127.0.0.1:{port}"
                credentials = {"username": "offline@example.invalid", "password": "offline-secret"}
                self.assertEqual(post(origin)[0], 401)
                code, header = post(origin, json.dumps(credentials), path="/api/credentials")
                self.assertEqual(code, 200)
                self.assertIn("Max-Age=86400", header)
                self.assertIn("HttpOnly", header)
                self.assertNotIn(credentials["password"], header)
                cookie = header.split(";", 1)[0]
                with closing(http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5)) as connection:
                    connection.request("GET", "/api/models")
                    response = connection.getresponse()
                    models = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertIn("glm", {row["id"] for row in models["entries"]})
                    self.assertFalse(models["provider_invoked"])
                    self.assertNotIn("offline-secret", json.dumps(models))
                self.assertEqual(post(origin, '{"region":"EUR"}', cookie=cookie)[0], 400)
                self.assertEqual(post(origin, json.dumps({
                    key: value for key, value in request.items() if key != "executor"
                }), cookie=cookie)[0], 400)
                self.assertEqual(post(origin, json.dumps({
                    **request, "lead": "unregistered"
                }), cookie=cookie)[0], 400)
                self.assertEqual(post(origin, json.dumps({**request, "delay": 0}), cookie=cookie)[0], 400)
                self.assertEqual(post(origin, cookie=cookie)[0], 202)
                self.assertTrue(entered.wait(3))
                running = status()
                self.assertEqual(running["status"], "running")
                self.assertIn("正在规划字段诊断", [event["message"] for event in running["events"]])
                self.assertEqual(post(origin, cookie=cookie)[0], 409)
                def action(name, target, session=cookie, **model_override):
                    payload = {"action": name, "id": target}
                    if name == "resume":
                        payload.update(lead="glm", executor="kimi")
                    payload.update(model_override)
                    return post(origin, json.dumps(payload),
                                path="/api/research", cookie=session)[0]

                self.assertEqual(action("stop", "stale-launch"), 409)
                self.assertEqual(action("delete", saved_id), 409)
                self.assertEqual(action("stop", running["launch_id"]), 202)
                launch_flag = Path(root) / "state" / f"workbench-{running['launch_id']}.stop"
                self.assertTrue(launch_flag.exists())
                self.assertFalse((Path(root) / "state/stop.flag").exists())
                self.assertTrue(status()["stop_requested"])
                self.assertEqual(len(commands), 1)
                self.assertEqual(child_credentials, [credentials])
                metadata = Path(root) / "state/logs/workbench" / f"{running['launch_id']}.launch.json"
                recorded = json.loads(metadata.read_text(encoding="utf-8"))
                self.assertEqual(recorded["args"][0], "run-campaign")
                self.assertEqual(recorded["models"]["lead"], "glm")
                self.assertEqual(recorded["models"]["executor"], "kimi")
                self.assertEqual(len(recorded["models"]["profile_sha256"]), 64)
                self.assertIn("--region=EUR", recorded["args"])
                self.assertNotIn(credentials["password"], metadata.read_text(encoding="utf-8"))
                self.assertEqual(commands[0][4:], [
                    "--state-root", root, "--lead", "glm", "--executor", "kimi",
                    "run-campaign", "--region=EUR",
                    "--objective=--example",
                    "--delay=1", "--universe=TOPCS1600",
                    "--dataset=risk60", "--dataset=model110", "--ppa",
                    "--brain-credentials-stdin"])
            finally:
                release.set()
            deadline = time.monotonic() + 3
            report = status()
            while report["status"] == "running" and time.monotonic() < deadline:
                report = status()
            self.assertEqual(report["status"], "exited")
            self.assertEqual(report["campaign_status"], "budget_complete")
            self.assertEqual(report["campaign_id"], "case")
            self.assertIn("正在规划字段诊断", [event["message"] for event in report["events"]])
            self.assertNotIn(credentials["password"], json.dumps(report))
            self.assertNotIn("password", report)
            manifest_path = Path(root) / "state/logs/workbench" / f"{report['launch_id']}.launch.json"
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "exited")
            self.assertEqual(persisted["campaign_status"], "budget_complete")
            with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
                connection.request("GET", "/api/launches")
                response = connection.getresponse()
                history = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertIn(report["launch_id"], {item["launch_id"] for item in history["items"]})
            self.assertFalse(launch_flag.exists())
            entered.clear()
            self.assertEqual(action("resume", saved_id, ""), 401)
            pinned = Path(root) / "state/logs/workbench" / f"{saved_id}.models.json"
            pinned.write_text(json.dumps({
                "campaign_id": saved_id, **recorded["models"]}), encoding="utf-8")
            self.assertEqual(action("resume", saved_id, lead="kimi"), 400)
            self.assertEqual(len(commands), 1)
            self.assertEqual(action("resume", saved_id), 202)
            self.assertTrue(entered.wait(3))
            self.assertEqual(commands[-1][10:], [
                "run-campaign", f"--resume-campaign={saved_id}", "--brain-credentials-stdin"])
            deadline = time.monotonic() + 3
            while status()["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(action("delete", saved_id), 400)
            self.assertTrue(batch.exists())
            intent.write_text('{"status":"READ_BACK"}', encoding="utf-8")
            self.assertEqual(action("delete", saved_id), 200)
            self.assertFalse(saved_path.exists())
            self.assertFalse(summary.exists())
            self.assertFalse(batch.exists())
            self.assertEqual(action("resume", saved_id), 400)
            clock.return_value = 86500
            self.assertEqual(post(origin, cookie=cookie)[0], 401)
            self.assertEqual(len(commands), 2)
