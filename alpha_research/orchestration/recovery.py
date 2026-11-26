"""Unified, fail-closed recovery coordination for Regular, Super and Optimization.

The coordinator is deliberately not a simulation runner. It only resumes
operations that already have durable evidence that submission began, or
finishes downstream work from a verified READ_BACK receipt. It never creates
a new simulation request.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from alpha_research.state.store import atomic_write_json, read_json
from alpha_research.state.json_contract import read_strict_object
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError
from alpha_research.state.fingerprint import fingerprint_json as _canonical_sha256


class RecoveryError(ValueError):
    """A recovery target is invalid, ambiguous, or requires manual review."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


class RecoveryCoordinator:
    """Single-writer coordinator for existing workflow-specific recovery paths.

    The caller must hold the project's SingleWriterLock. A durable journal is
    written before recovery starts. An interrupted or failed recovery is
    blocked rather than replayed automatically; a completed recovery returns
    its saved result without invoking a workflow handler again.
    """

    REGULAR_SUBMITTING = {"SUBMITTING", "READ_BACK"}
    SIMULATION_RECOVERABLE = {
        "POSTING", "OUTCOME_UNKNOWN", "RESPONSE_RECEIVED", "READ_BACK"
    }

    def __init__(self, state_root, *, simulation_recover=None):
        self.state_root = Path(state_root).resolve()
        self.journal_root = (
            self.state_root / "state" / "research_v1_1" / "recoveries"
        )
        self._simulation_recover = simulation_recover

    def _resolve_target(self, target):
        candidate = Path(target).expanduser()
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise RecoveryError("recovery target cannot be a symlink")
        path = candidate.resolve()
        if not path.is_relative_to(self.state_root):
            raise RecoveryError("recovery target is outside the configured state root")
        if not path.exists():
            raise RecoveryError("recovery target does not exist")
        return path

    @staticmethod
    def _read_json(path, label):
        return read_strict_object(path, label, error_type=RecoveryError)

    def _inspect(self, path):
        if path.is_dir():
            intent_path = path / "simulation_intent.json"
            intent = self._read_json(intent_path, "Regular simulation intent")
            status = intent.get("status")
            if status not in self.REGULAR_SUBMITTING:
                raise RecoveryError(
                    "Regular recovery is allowed only after submission began "
                    "or after READ_BACK; PREPARED intents are never submitted by recovery"
                )
            if status == "READ_BACK" and not (path / "results.json").is_file():
                raise RecoveryError("Regular READ_BACK intent has no saved results")
            prereg_path = path / "prereg.json"
            prereg_digest = (
                _sha256_bytes(prereg_path.read_bytes())
                if prereg_path.is_file() else None
            )
            immutable = {
                "workflow": "regular",
                "batch_dir": str(path),
                "expressions": intent.get("expressions"),
                "settings": intent.get("settings"),
                "runner": intent.get("runner"),
                "prereg_sha256": intent.get("prereg_sha256"),
                "prereg_file_sha256": prereg_digest,
            }
            return "regular", path, intent, _canonical_sha256(immutable)

        intent = self._read_json(path, "simulation intent")
        workflow = intent.get("workflow")
        if workflow not in {"super", "optimization"}:
            raise RecoveryError(
                "file recovery target must be a Super or Optimization simulation intent"
            )
        if intent.get("status") not in self.SIMULATION_RECOVERABLE:
            raise RecoveryError(
                "simulation intent is not recoverable; PREPARED intents cannot be submitted"
            )
        request = intent.get("request")
        settings = intent.get("settings")
        if not isinstance(request, dict) or not isinstance(settings, dict):
            raise RecoveryError("simulation intent request/settings are invalid")
        immutable = {
            "schema_version": intent.get("schema_version"),
            "workflow": workflow,
            "run_id": intent.get("run_id"),
            "batch_id": intent.get("batch_id"),
            "request_sha256": intent.get("request_sha256"),
            "request": request,
            "settings": settings,
            "metadata": intent.get("metadata") or {},
        }
        return workflow, path, intent, _canonical_sha256(immutable)

    def _journal_path(self, workflow, target):
        identity = _canonical_sha256({
            "workflow": workflow,
            "target": str(target),
        })
        return self.journal_root / f"{identity}.json"

    def journal_for(self, target):
        """Return the durable journal for a target, if one has been created."""
        path = self._resolve_target(target)
        workflow, _, _, _ = self._inspect(path)
        journal_path = self._journal_path(workflow, path)
        if not journal_path.exists():
            return None
        return self._read_json(journal_path, "recovery journal")

    def record_manual_takeover(self, target, *, operator, decision, reason,
                               evidence_refs=None, task_id=None):
        """Append a human disposition to a BLOCKED journal without unblocking it.

        This is an audit handoff only: it never edits Task/Intent state, calls a
        workflow handler, or permits automatic replay.
        """
        path = self._resolve_target(target)
        workflow, resolved, _, _ = self._inspect(path)
        journal_path = self._journal_path(workflow, resolved)
        if not journal_path.is_file() or journal_path.is_symlink():
            raise RecoveryError("manual takeover requires an existing recovery journal")
        if not isinstance(operator, str) or not operator.strip() or len(operator) > 128:
            raise RecoveryError("manual takeover operator is required")
        if decision not in {"TAKE_OVER", "ABANDON", "ESCALATE"}:
            raise RecoveryError("unsupported manual takeover decision")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise RecoveryError("manual takeover reason is required")
        refs = evidence_refs or []
        if (not isinstance(refs, list) or len(refs) > 20
                or any(not isinstance(value, (str, Path)) for value in refs)):
            raise RecoveryError("manual takeover evidence refs must be a list of at most 20 paths")
        evidence = []
        for value in refs:
            evidence_path = Path(value).expanduser()
            if not evidence_path.is_absolute():
                evidence_path = Path.cwd() / evidence_path
            evidence_path = Path(os.path.abspath(evidence_path))
            resolved_evidence = evidence_path.resolve()
            cursor = evidence_path
            while cursor != cursor.parent:
                if cursor.is_symlink():
                    raise RecoveryError("manual takeover evidence cannot traverse symlinks")
                if cursor == self.state_root:
                    break
                cursor = cursor.parent
            if (not resolved_evidence.is_relative_to(self.state_root)
                    or not resolved_evidence.is_file()):
                raise RecoveryError("manual takeover evidence must be an existing file inside state root")
            evidence.append({
                "ref": str(resolved_evidence),
                "sha256": _sha256_bytes(resolved_evidence.read_bytes()),
            })
        if decision == "TAKE_OVER" and not evidence:
            raise RecoveryError("TAKE_OVER requires at least one verifiable evidence file")

        manual_root = self.journal_root / "manual"
        if manual_root.is_symlink() or manual_root.parent.is_symlink():
            raise RecoveryError("manual takeover journal directory cannot be a symlink")
        manual_root.mkdir(parents=True, exist_ok=True)
        if not manual_root.resolve().is_relative_to(self.state_root):
            raise RecoveryError("manual takeover journal must stay inside state root")
        manual_path = manual_root / f"{journal_path.stem}.json"
        lock_path = manual_path.with_suffix(".lock")
        if manual_path.is_symlink() or lock_path.is_symlink():
            raise RecoveryError("manual takeover journal or lock cannot be a symlink")
        try:
            with SingleWriterLock(lock_path):
                journal = self._read_json(journal_path, "recovery journal")
                if journal.get("status") != "BLOCKED":
                    raise RecoveryError("manual takeover is available only for BLOCKED recovery")
                if task_id is not None and journal.get("task_id") != task_id:
                    raise RecoveryError("manual takeover Task binding does not match recovery journal")
                if manual_path.exists():
                    record = self._read_json(manual_path, "manual takeover journal")
                else:
                    record = {
                        "schema_version": 1,
                        "recovery_id": journal["recovery_id"],
                        "workflow": workflow,
                        "target_ref": str(resolved),
                        "recovery_journal_ref": str(journal_path),
                        "recovery_journal_sha256": _sha256_bytes(journal_path.read_bytes()),
                        "task_id": journal.get("task_id"),
                        "decisions": [],
                        "created_at": _now(),
                    }
                if (record.get("schema_version") != 1
                        or record.get("recovery_id") != journal.get("recovery_id")
                        or record.get("workflow") != workflow
                        or record.get("target_ref") != str(resolved)
                        or record.get("recovery_journal_ref") != str(journal_path)
                        or record.get("recovery_journal_sha256") !=
                            _sha256_bytes(journal_path.read_bytes())
                        or record.get("task_id") != journal.get("task_id")
                        or not isinstance(record.get("decisions"), list)
                        or len(record["decisions"]) >= 1000):
                    raise RecoveryError("manual takeover lineage changed or journal limit reached")
                entry = {
                    "decision_id": _canonical_sha256({
                        "recovery_id": journal["recovery_id"],
                        "operator": operator.strip(), "decision": decision,
                        "reason": reason.strip(), "evidence": evidence,
                        "sequence": len(record["decisions"]) + 1,
                    })[:32],
                    "operator": operator.strip(), "decision": decision,
                    "reason": reason.strip(), "evidence": evidence,
                    "created_at": _now(),
                    "execution_unblocked": False,
                    "automatic_replay_allowed": False,
                    "simulation_invoked": False,
                    "alpha_submission_invoked": False,
                }
                if any(row.get("decision_id") == entry["decision_id"]
                       for row in record["decisions"]):
                    raise RecoveryError("duplicate manual takeover decision")
                record["decisions"].append(entry)
                record["updated_at"] = _now()
                atomic_write_json(manual_path, record)
        except WriterLockError as exc:
            raise RecoveryError("another operator is recording manual takeover") from exc
        return {
            "schema_version": 1, "status": "MANUAL_TAKEOVER_RECORDED",
            "recovery_id": journal["recovery_id"], "workflow": workflow,
            "task_id": journal.get("task_id"), "decision": decision,
            "manual_takeover_ref": str(manual_path),
            "execution_unblocked": False, "automatic_replay_allowed": False,
            "simulation_invoked": False, "alpha_submission_invoked": False,
        }

    def recover(self, target, *, gateway, agent=None, config=None, expected_workflow=None,
                task_id=None, step=None):
        """Serialize recovery by target across processes, including handler work.

        A second caller cannot pass the journal existence check while the
        first is creating its intent. The lock is independent of CLI agent.lock.
        """
        path = self._resolve_target(target)
        workflow, _, _, _ = self._inspect(path)
        journal_path = self._journal_path(workflow, path)
        try:
            with SingleWriterLock(journal_path.with_suffix(".lock")):
                # Re-inspect under the lock: the intent could have changed
                # since the initial safety preflight.
                return self._recover_locked(
                    path, gateway=gateway, agent=agent, config=config,
                    expected_workflow=expected_workflow,
                    task_id=task_id, step=step)
        except WriterLockError as exc:
            raise RecoveryError(
                "another recovery writer is already processing this target") from exc

    def _iteration_research_id(self, workflow, target):
        """Resolve the iteration session from its immutable domain record."""
        from alpha_research.state.records import ResearchRecordStore

        store = ResearchRecordStore(self.state_root)
        if workflow == "regular":
            context_path = Path(target) / "batch_context.json"
            context = read_json(context_path)
            return context.get("research_id") if isinstance(context, dict) else None
        intent = self._read_json(target, "simulation intent")
        run_id = intent.get("run_id")
        if workflow == "super" and isinstance(run_id, str):
            try:
                return store.get("super_run", run_id).get("research_id")
            except (FileNotFoundError, ValueError):
                return None
        if workflow == "optimization":
            matches = [row for row in store.list("candidate_branch")
                       if row.get("simulation_intent_ref") == str(Path(target).resolve())]
            return matches[0].get("research_id") if len(matches) == 1 else None
        return None

    def _reconcile_iteration(self, workflow, target, result, recovery_ref):
        """Close the interrupted P9 round only from a verified recovery receipt."""
        research_id = self._iteration_research_id(workflow, target)
        if not isinstance(research_id, str):
            return result
        prefix = {
            "regular": "regular:campaign:",
            "super": "super:",
            "optimization": "optimization:",
        }.get(workflow)
        if not prefix or not research_id.startswith(prefix):
            return result
        session_id = research_id[len(prefix):]
        if len(session_id) != 32 or any(
                char not in "0123456789abcdef" for char in session_id):
            return result
        from alpha_research.workflows.iteration import IterationLedger

        ledger_path = (self.state_root / "state" / "research_v1_1" /
                       "iterations" / f"{session_id}.json")
        saved = read_json(ledger_path)
        if not isinstance(saved, dict):
            return result
        request = saved.get("request")
        if not isinstance(request, dict):
            raise RecoveryError("iteration ledger request is unreadable during recovery")
        ledger = IterationLedger(
            self.state_root, session_id=session_id, workflow=workflow,
            objective=request.get("objective"), scope=request.get("scope"),
            source_ref=request.get("source_ref"),
            max_iterations=request.get("max_iterations"))
        if not any(row.get("status") == "RECOVERY_REQUIRED"
                   for row in ledger.record.get("rounds", [])):
            return result
        reconciled = ledger.reconcile_recovery(
            recovery_ref=str(recovery_ref), result=result)
        output = dict(result)
        output["iteration_session_id"] = session_id
        output["iteration_ref"] = str(ledger.path)
        output["iteration_status"] = reconciled.get("status")
        return output

    def _recover_locked(self, target, *, gateway, agent=None, config=None,
                        expected_workflow=None, task_id=None, step=None):
        """Recover one existing intent without reposting unknown simulation.

        Regular targets are batch directories. Super and Optimization targets
        are their simulation_intent.json files. All targets must live below the
        configured state root.
        """
        path = self._resolve_target(target)
        workflow, resolved, intent, fingerprint = self._inspect(path)
        if expected_workflow is not None and expected_workflow != workflow:
            raise RecoveryError(
                f"recovery target is {workflow}, not requested {expected_workflow}"
            )
        if workflow == "regular" and agent is None:
            raise RecoveryError("Regular recovery requires the existing ResearchAgent")
        if workflow in {"super", "optimization"} and gateway is None:
            raise RecoveryError("Super/Optimization recovery requires the platform gateway")
        if (task_id is None) != (step is None):
            raise RecoveryError("task_id and step must be supplied together")
        if task_id is not None and step != "execute":
            raise RecoveryError("workflow recovery can reconcile only the execute step")
        journal_path = self._journal_path(workflow, resolved)
        if journal_path.exists():
            journal = self._read_json(journal_path, "recovery journal")
            if (journal.get("target_fingerprint") != fingerprint
                    or journal.get("task_id") != task_id
                    or journal.get("step") != step):
                raise RecoveryError(
                    "recovery target evidence changed or task binding changed; manual review required"
                )
            if journal.get("status") == "COMPLETED":
                result = journal.get("result")
                if (not isinstance(result, dict)
                        or journal.get("result_sha256") != _canonical_sha256(result)):
                    raise RecoveryError("completed recovery journal result is corrupt")
                return result
            raise RecoveryError(
                "recovery is already RUNNING or BLOCKED; automatic replay is prohibited"
            )

        now = _now()
        journal = {
            "schema_version": 1,
            "recovery_id": journal_path.stem,
            "workflow": workflow,
            "target_ref": str(resolved),
            "target_fingerprint": fingerprint,
            "task_id": task_id,
            "step": step,
            "status": "RUNNING",
            "started_at": now,
            "updated_at": now,
            "result": None,
            "result_sha256": None,
            "failure": None,
        }
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(journal_path, journal)

        try:
            if workflow == "regular":
                result = agent.recover(resolved)
            else:
                recover_fn = self._simulation_recover
                if recover_fn is None:
                    from alpha_research.simulation.intent import recover_simulation_intent
                    recover_fn = recover_simulation_intent
                result = recover_fn(gateway, self.state_root, resolved, config=config)
            if not isinstance(result, dict):
                raise RecoveryError("workflow recovery returned a non-object result")
            if workflow in {"super", "optimization"}:
                result = self._reconcile_iteration(
                    workflow, resolved, result, journal_path)
            result_digest = _canonical_sha256(result)
            journal.update(
                status="COMPLETED",
                result=result,
                result_sha256=result_digest,
                completed_at=_now(),
                updated_at=_now(),
            )
            atomic_write_json(journal_path, journal)
            return result
        except Exception as exc:
            journal.update(
                status="BLOCKED",
                failure={
                    "type": type(exc).__name__,
                    # External exception messages can contain request data or
                    # credentials. Persist the class only, never provider text.
                    "reason": "recovery_handler_failed_manual_review_required",
                },
                updated_at=_now(),
            )
            atomic_write_json(journal_path, journal)
            if isinstance(exc, RecoveryError):
                raise
            raise RecoveryError(
                f"{workflow} recovery failed and is blocked for manual review: "
                f"{type(exc).__name__}"
            ) from exc

    def recover_task(self, orchestrator, task_id, *, gateway, agent=None, config=None,
                     expected_workflow=None, saved_pilot=False):
        """Fail fast on duplicate task recovery before replaying any handler."""
        # Separate from the task's execution lock: reconcile() legitimately
        # acquires that lock after the recovery receipt has been written.
        lock_path = orchestrator._path(task_id).with_suffix(".recover.lock")
        try:
            with SingleWriterLock(lock_path):
                return self._recover_task_locked(
                    orchestrator, task_id, gateway=gateway, agent=agent,
                    config=config, expected_workflow=expected_workflow,
                    saved_pilot=saved_pilot)
        except WriterLockError as exc:
            raise RecoveryError(
                "another recovery writer is already processing this task") from exc

    def _recover_task_locked(self, orchestrator, task_id, *, gateway, agent=None,
                             config=None, expected_workflow=None, saved_pilot=False):
        """Recover a blocked orchestration execute step from its bound domain artifact."""
        task = orchestrator.load(task_id)
        local_pilot = (task.get("checkpoints", {}).get("validate") or {}).get(
            "mode") == "supervised_local_real_pilot"
        if local_pilot != saved_pilot or (saved_pilot and gateway is not None):
            raise RecoveryError("Local Regular Pilot requires offline saved-review recovery")
        if expected_workflow is not None and task.get("workflow") != expected_workflow:
            raise RecoveryError("orchestration task workflow does not match the requested workflow")
        step = (task.get("failure") or {}).get("step")
        target = task.get("recovery_target") or {}
        if (task.get("status") != "BLOCKED" or step != "execute"
                or task.get("steps", {}).get("execute") != "RUNNING"):
            raise RecoveryError("only a blocked execute step can use workflow recovery")
        if not target.get("ref") or not target.get("kind"):
            raise RecoveryError("task has no durable workflow recovery target")
        expected_kind = "regular_batch" if task["workflow"] == "regular" else "simulation_intent"
        if target["kind"] != expected_kind:
            raise RecoveryError("task recovery target kind does not match its workflow")

        request = task["request"]
        if task["workflow"] == "regular":
            if (type(request.get("max_batches", 1)) is not int
                    or not 1 <= request.get("max_batches", 1) <= 20):
                raise RecoveryError("Regular Campaign recovery budget is invalid")
            if not request.get("objective") or not request.get("region"):
                raise RecoveryError("Regular task request lacks objective or Region for Campaign reconciliation")

        result = self.recover(
            target["ref"], gateway=gateway, agent=agent, config=config,
            expected_workflow=task["workflow"], task_id=task_id, step=step)
        if task["workflow"] == "regular":
            from alpha_research.workflows.regular.campaign import run_regular_campaign
            result = run_regular_campaign(
                agent, gateway, objective=request["objective"], region=request["region"],
                dataset_ids=request.get("dataset_ids"), category=request.get("category"),
                delay=request.get("delay"), universe=request.get("universe"),
                max_batches=request.get("max_batches", 1),
                ppa_profile=bool(request.get("ppa_profile", request.get("ppa", False))),
                recovery_batch=target["ref"], saved_pilot=saved_pilot)

        journal = self.journal_for(target["ref"])
        if (not isinstance(journal, dict) or journal.get("status") != "COMPLETED"
                or journal.get("task_id") != task_id or journal.get("step") != step):
            raise RecoveryError("domain recovery journal is not verified for this task")
        receipt_root = self.journal_root / "tasks"
        receipt_path = receipt_root / f"{task_id}.json"
        receipt_root.mkdir(parents=True, exist_ok=True)
        journal_path = self._journal_path(
            task["workflow"], Path(target["ref"]).resolve())
        # A recovered Simulation is not the same as a completed research run.
        # If the P9 budget still has rounds (or Regular still has Campaign work),
        # keep the orchestration Task BLOCKED and hand the verified checkpoint
        # back to the operator. Never silently mark a partial Campaign complete.
        iteration_complete = False
        iteration_ref = result.get("iteration_ref")
        if isinstance(iteration_ref, str):
            iteration_path = Path(iteration_ref).expanduser()
            iteration_root = (self.state_root / "state" / "research_v1_1" /
                              "iterations").resolve()
            if (not iteration_path.is_symlink()
                    and not iteration_path.parent.is_symlink()):
                iteration_path = iteration_path.resolve()
                if iteration_path.is_relative_to(iteration_root) and iteration_path.is_file():
                    iteration_record = read_json(iteration_path)
                    iteration_complete = (
                        isinstance(iteration_record, dict)
                        and iteration_record.get("status") == "COMPLETED"
                        and iteration_record.get("budget", {}).get("reserved") == 0)
        if iteration_complete is not True:
            receipt = {
                "schema_version": 1,
                "status": "RECOVERED_AWAITING_EXPLICIT_CONTINUATION",
                "task_id": task_id, "workflow": task["workflow"], "step": step,
                "target_kind": target["kind"], "target_ref": target["ref"],
                "recovery_id": journal["recovery_id"],
                "recovery_journal_ref": str(journal_path),
                "recovery_journal_sha256": _sha256_bytes(journal_path.read_bytes()),
                "outcome": result, "outcome_sha256": _canonical_sha256(result),
                "automatic_resume_allowed": False,
                "simulation_invoked": False, "alpha_submission_invoked": False,
                "created_at": _now(),
            }
            atomic_write_json(receipt_path, receipt)
            return {
                "schema_version": 1,
                "status": "RECOVERED_AWAITING_EXPLICIT_CONTINUATION",
                "task_id": task_id, "workflow": task["workflow"],
                "recovery_receipt_ref": str(receipt_path),
                "iteration_ref": iteration_ref,
                "automatic_resume_allowed": False,
                "simulation_invoked": False, "alpha_submission_invoked": False,
                "task_remains_blocked": True,
            }

        receipt = {
            "schema_version": 1, "status": "COMPLETED",
            "task_id": task_id, "workflow": task["workflow"], "step": step,
            "target_kind": target["kind"], "target_ref": target["ref"],
            "recovery_id": journal["recovery_id"],
            "recovery_journal_ref": str(journal_path),
            "recovery_journal_sha256": _sha256_bytes(journal_path.read_bytes()),
            "outcome": result, "outcome_sha256": _canonical_sha256(result),
            "created_at": _now(),
        }
        atomic_write_json(receipt_path, receipt)
        receipt_sha256 = _sha256_bytes(receipt_path.read_bytes())
        return orchestrator.reconcile(
            task_id, step, result, receipt_ref=str(receipt_path),
            receipt_sha256=receipt_sha256)
