"""Offline-only acceptance matrix for the EXISTING three research workflows.

Runs each existing orchestration-e2e-offline command in a fresh, permanently
mock-only State Root. Always finishes the matrix even when a case fails.
This script never creates a real Gateway, invokes a paid model, submits an
Alpha or grants any production authority.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


CASES = (
    ("regular_single", "regular", ["--dataset", "analyst10"]),
    ("regular_multi", "regular",
     ["--dataset", "analyst10", "--dataset", "analyst11", "--category", "OTHER"]),
    ("super_native", "super", []),
    ("optimization_super_source", "optimization", ["--alpha-id", "SOURCE"]),
    ("regular_unknown_outcome", "regular",
     ["--dataset", "analyst10", "--fault", "lost-simulation-response"]),
)


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_matrix(output_root, *, lead="deepseek", executor="deepseek", python=None):
    """Run every case, never reusing an existing research/acceptance directory."""
    root = Path(output_root).expanduser().resolve()
    from alpha_research.state.paths import default_state_root
    if root == default_state_root().resolve() or root.name in {
            ".alpha-runtime", "alpha-research-live"}:
        raise ValueError("live research state root is forbidden for offline acceptance")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("acceptance output root must be new or empty")
    if not lead or not executor:
        raise ValueError("registered Lead and Executor must be supplied")
    python = python or sys.executable
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for case_name, workflow, options in CASES:
        case_root = root / case_name
        command = [
            str(python), "-m", "alpha_research",
            "--state-root", str(case_root),
            "--lead", lead, "--executor", executor,
            "orchestration-e2e-offline", "--mode", "mock",
            "--workflow", workflow, "--objective",
            "isolated offline acceptance: " + case_name,
            "--region", "USA", "--delay", "1", "--universe", "TOP3000",
            *options,
        ]
        stdout_path = root / (case_name + ".stdout.json")
        stderr_path = root / (case_name + ".stderr.txt")
        try:
            response = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False, timeout=180,
            )
            stdout_path.write_text(response.stdout, encoding="utf-8")
            stderr_path.write_text(response.stderr, encoding="utf-8")
            report = json.loads(response.stdout)
            if not isinstance(report, dict):
                raise ValueError("CLI result must be a JSON object")
            expected_fault = case_name == "regular_unknown_outcome"
            expected_status = ("OFFLINE_E2E_BLOCKED" if expected_fault
                               else "OFFLINE_E2E_PASSED")
            accepted = (
                response.returncode == (2 if expected_fault else 0)
                and report.get("status") == expected_status
                and report.get("trace_status") == "TRACE_VERIFIED"
                and report.get("provider_model_invoked") is False
                and report.get("brain_invoked") is False
                and report.get("brain_simulation_invoked") is False
                and report.get("alpha_submission_invoked") is False
                and report.get("production_authorized") is False
                and (expected_fault or
                     (report.get("workflow_handoff_audit") or {}).get("status")
                     == "VERIFIED")
            )
            result = {
                "case": case_name, "workflow": workflow,
                "status": "ACCEPTED" if accepted else "FAILED",
                "observed_status": report.get("status"),
                "task_id": report.get("task_id"),
                "trace_status": report.get("trace_status"),
                "trace_last_event_sha256": report.get("trace_last_event_sha256"),
                "workflow_handoff_audit": report.get("workflow_handoff_audit"),
                "exit_code": response.returncode,
                "report_ref": report.get("report_path"),
                "stdout_ref": str(stdout_path),
                "stdout_sha256": _digest(stdout_path),
                "stderr_ref": str(stderr_path),
                "stderr_sha256": _digest(stderr_path),
            }
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            if not stdout_path.exists():
                stdout_path.write_text("", encoding="utf-8")
            if not stderr_path.exists():
                stderr_path.write_text(type(exc).__name__, encoding="utf-8")
            result = {
                "case": case_name, "workflow": workflow, "status": "FAILED",
                "failure_type": type(exc).__name__,
                "stdout_ref": str(stdout_path), "stdout_sha256": _digest(stdout_path),
                "stderr_ref": str(stderr_path), "stderr_sha256": _digest(stderr_path),
            }
        results.append(result)
    summary = {
        "schema_version": 1, "mode": "OFFLINE_ONLY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": ("ACCEPTED" if all(r["status"] == "ACCEPTED" for r in results)
                   else "INCOMPLETE"),
        "case_count": len(results),
        "accepted_count": sum(r["status"] == "ACCEPTED" for r in results),
        "cases": results,
        "provider_model_invoked": False,
        "brain_simulation_invoked": False,
        "alpha_submission_invoked": False,
        "production_authorized": False,
    }
    summary_path = root / "acceptance_matrix.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Execute all isolated offline research workflow E2E cases")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--lead", default="deepseek")
    parser.add_argument("--executor", default="deepseek")
    args = parser.parse_args()
    try:
        summary = run_matrix(
            args.output_root, lead=args.lead, executor=args.executor)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "ACCEPTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
