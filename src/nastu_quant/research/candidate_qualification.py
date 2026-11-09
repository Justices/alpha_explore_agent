"""Shared Candidate Qualification and Preferred Pool lifecycle.

The service enforces RA -> correlation ordering for all workflows. It is
read-only with respect to BRAIN: it fetches Alpha evidence and never submits
or mutates an Alpha.
"""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ..gatekeeper import (evaluate_candidate, parse_correlations,
                         ra_correlation_check_state, local_self_correlation_state,
                         RA_CHECK_NAMES)
from ..preferred import evaluate_preferred
from .records import ResearchRecordStore
from .candidate_pool import CandidatePool, evidence_hash, policy_hash, _hash
from ..state_store import atomic_write_json


def _now():
    return datetime.now(timezone.utc).isoformat()


def _stable_id(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()[:32]


def _candidate_status(failures, pending, fallback="EVIDENCE_PENDING"):
    if any(item.startswith("ra:") or item == "ra_fail_count" for item in failures):
        return "RA_FAILED"
    if any(item.startswith("pairwise:") for item in failures):
        return "PAIRWISE_CONFLICT"
    if any(item.startswith("platform_check:") for item in failures):
        return "PLATFORM_CHECK_FAILED"
    if any("correlation" in item or item.startswith("local_self_") for item in failures):
        return "CORR_REJECTED"
    if failures:
        return "METRICS_FAILED"
    if pending:
        return "EVIDENCE_PENDING"
    return fallback or "CANDIDATE"


def _upsert(store, kind, record):
    """Idempotent update with immutable revision snapshots before replacement.

    A byte-for-byte equivalent assessment makes no new history entry and does
    not change updated_at. Any substantive change archives the prior record.
    """
    try:
        previous = store.get(kind, record["candidate_id"])
    except FileNotFoundError:
        previous = None
    now = datetime.now(timezone.utc)
    if previous:
        record["created_at"] = previous["created_at"]
        without_clock = lambda value: {
            key: item for key, item in value.items()
            if key not in ("created_at", "updated_at")
        }
        if without_clock(previous) == without_clock(record):
            return store.path_for(kind, record["candidate_id"])
        archive_hash = _hash(previous)
        archive_path = (store.root / "evaluation_history" / kind /
                        record["candidate_id"] / (archive_hash + ".json"))
        if not archive_path.exists():
            atomic_write_json(archive_path, previous)
        last = datetime.fromisoformat(previous["updated_at"])
        if now <= last:
            now = last + timedelta(microseconds=1)
    record["updated_at"] = now.isoformat()
    return store.put(kind, record,
                     expected_updated_at=previous["updated_at"] if previous else None)


class CandidateQualificationService:
    """One Candidate gate and two persistent pools shared by all workflows."""

    def __init__(self, gateway, state_root, config):
        self.gateway = gateway
        self.store = ResearchRecordStore(Path(state_root))
        self.config = config
        self.pool = CandidatePool(self.store)

    @staticmethod
    def _platform_check_findings(details):
        is_data = details.get("is") if isinstance(details, dict) else None
        checks = is_data.get("checks") if isinstance(is_data, dict) else None
        failures, pending = [], []
        if not isinstance(checks, list):
            return [], ["platform_checks_unavailable"]
        for row in checks:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                pending.append("platform_check_unreadable")
                continue
            if row["name"] in RA_CHECK_NAMES:
                continue
            result = str(row.get("result") or "").upper()
            if result == "FAIL":
                failures.append("platform_check:" + row["name"])
            elif result != "PASS":
                pending.append("platform_check_pending:" + row["name"])
        return failures, pending

    def _persist(self, *, workflow, alpha_id, candidate_ref, source_ref,
                 details, gate, correlations, evidence_refs):
        candidate_id = _stable_id({"workflow": workflow, "alpha_id": alpha_id,
                                   "candidate_ref": str(candidate_ref)})
        extra_failures, extra_pending = self._platform_check_findings(details)
        failures = sorted(set(gate.get("failures", []) + extra_failures))
        pending = sorted(set(gate.get("pending", []) + extra_pending))
        status = _candidate_status(
            failures, pending, fallback=gate.get("status", "EVIDENCE_PENDING"))
        preferred = evaluate_preferred(details, candidate_status=status, config=self.config)
        metrics = preferred.get("metrics", {})
        now = _now()
        fingerprint = evidence_hash(details, correlations, failures, pending)
        configured_policy = policy_hash(self.config)
        candidate = {
            "schema_version": 1, "candidate_id": candidate_id, "alpha_id": alpha_id,
            "workflow": workflow, "source_ref": str(source_ref),
            "candidate_ref": str(candidate_ref), "status": status,
            "failures": failures, "pending": pending, "metrics": metrics,
            "correlations": correlations or {}, "evidence_refs": evidence_refs or {},
            "preferred_status": preferred["status"],
            "evidence_sha256": fingerprint, "policy_sha256": configured_policy,
            "created_at": now, "updated_at": now,
        }
        candidate_path = self.store.path_for("candidate", candidate_id)
        preferred_path = self.store.path_for("preferred", candidate_id)
        preferred_record = {
            "schema_version": 1, "candidate_id": candidate_id, "alpha_id": alpha_id,
            "workflow": workflow, "source_ref": str(source_ref),
            "candidate_ref": str(candidate_ref), "status": preferred["status"],
            "failures": preferred.get("failures", []), "pending": preferred.get("pending", []),
            "metrics": metrics, "evidence_sha256": fingerprint,
            "policy_sha256": configured_policy,
            "evidence_refs": {**(evidence_refs or {}), "candidate_record": str(candidate_path)},
            "created_at": _now(), "updated_at": _now(),
        }

        def same_assessment(left, right):
            return ({key: value for key, value in left.items()
                     if key not in {"created_at", "updated_at"}} ==
                    {key: value for key, value in right.items()
                     if key not in {"created_at", "updated_at"}})

        try:
            existing_candidate = self.store.get("candidate", candidate_id)
            existing_preferred = self.store.get("preferred", candidate_id)
        except FileNotFoundError:
            existing_candidate, existing_preferred = None, None
        # source_ref identifies the immutable originating research record.
        # A fresh re-audit is evidence, not a new source; keep the original
        # source_ref and attach the new audit under evidence_refs.
        if existing_candidate is not None:
            candidate["source_ref"] = existing_candidate["source_ref"]
            preferred_record["source_ref"] = existing_candidate["source_ref"]
        if (existing_candidate is not None and existing_preferred is not None
                and same_assessment(existing_candidate, candidate)
                and same_assessment(existing_preferred, preferred_record)):
            self.pool.sync_index(current_policy_sha256=configured_policy)
            return {"candidate_id": candidate_id, "alpha_id": alpha_id,
                    "workflow": workflow, "candidate_status": status,
                    "candidate_failures": failures, "candidate_pending": pending,
                    "preferred": preferred, "candidate_ref": str(candidate_path),
                    "preferred_ref": str(preferred_path)}

        # Two-phase publication keeps Candidate invisible until Preferred is durable.
        # If either write fails, a retry can finish the staged evaluation safely.
        staged_candidate = dict(candidate, status="EVALUATION_PENDING")
        _upsert(self.store, "candidate", staged_candidate)
        preferred_path = _upsert(self.store, "preferred", preferred_record)
        candidate_path = _upsert(self.store, "candidate", candidate)
        self.pool.sync_index(current_policy_sha256=configured_policy)
        return {"candidate_id": candidate_id, "alpha_id": alpha_id,
                "workflow": workflow, "candidate_status": status,
                "candidate_failures": failures, "candidate_pending": pending,
                "preferred": preferred, "candidate_ref": str(candidate_path),
                "preferred_ref": str(preferred_path)}

    def qualify_from_evidence(self, *, workflow, alpha_id, candidate_ref,
                              source_ref, details, correlations=None, pairwise=None,
                              expected_pairwise_ids=None, evidence_refs=None,
                              external_failures=None, external_pending=None):
        """Deterministic qualification for already-collected evidence."""
        if workflow not in {"regular", "super", "optimization"}:
            raise ValueError("unsupported Candidate workflow")
        if not isinstance(details, dict) or details.get("id") != alpha_id:
            raise ValueError("Alpha details do not match candidate ID")
        ra_state = ra_correlation_check_state(details)
        if ra_state != "pass":
            correlations, pairwise = None, None
        settings = details.get("settings") if isinstance(details.get("settings"), dict) else {}
        is_data = details.get("is") if isinstance(details.get("is"), dict) else {}
        gate = evaluate_candidate(
            details, {"checks": is_data.get("checks")},
            settings.get("region"), self.config, correlations=correlations,
            pairwise=pairwise or {}, expected_pairwise_ids=expected_pairwise_ids or [])
        if ra_state == "fail":
            gate["status"] = "RA_FAILED"
            gate["failures"] = sorted(set(gate.get("failures", []) + ["ra_fail_count"]))
        elif ra_state != "pass":
            gate["status"] = "EVIDENCE_PENDING"
            gate["pending"] = sorted(set(gate.get("pending", []) + ["ra_checks_unsettled_or_unavailable"]))
        gate["failures"] = sorted(set(gate.get("failures", []) + (external_failures or [])))
        gate["pending"] = sorted(set(gate.get("pending", []) + (external_pending or [])))
        if gate["failures"] or gate["pending"]:
            gate["status"] = ("RA_FAILED" if any(x.startswith("ra:") or x == "ra_fail_count" for x in gate["failures"])
                              else "EVIDENCE_PENDING" if gate["pending"] and not gate["failures"]
                              else "METRICS_FAILED")
        corr = {"prod": (correlations or {}).get("prod"),
                "self": (correlations or {}).get("self"), "pairwise": pairwise or {}}
        refs = dict(evidence_refs or {})
        refs.setdefault("alpha_details", str(source_ref))
        return self._persist(workflow=workflow, alpha_id=alpha_id,
                             candidate_ref=candidate_ref, source_ref=source_ref,
                             details=details, gate=gate, correlations=corr,
                             evidence_refs=refs)

    def _discover_pairwise_contenders(self, alpha_id, details):
        """Build a complete same-region OS comparator set for single-Alpha runs."""
        settings = details.get("settings") if isinstance(details, dict) else None
        region = settings.get("region") if isinstance(settings, dict) else None
        if not isinstance(region, str) or not region:
            raise ValueError("pairwise comparator Region is unavailable")
        delay = settings.get("delay") if isinstance(settings, dict) else None
        rows, offset, page_size = [], 0, 100
        while True:
            page = self.gateway.call("get_user_alphas", stage="OS",
                                     limit=page_size, offset=offset)
            if not isinstance(page, dict) or not isinstance(page.get("results"), list):
                raise ValueError("pairwise comparator pool is unreadable")
            batch = page["results"]
            rows.extend(batch)
            offset += len(batch)
            count = page.get("count")
            if isinstance(count, int) and offset >= count:
                break
            if not batch:
                if isinstance(count, int) and offset < count:
                    raise ValueError("pairwise comparator pool is incomplete")
                break
            if len(batch) < page_size and count is None:
                break
            if offset >= 100000:
                raise ValueError("pairwise comparator pool exceeds safety limit")
        ids = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise ValueError("pairwise comparator row is unreadable")
            if str(row.get("status") or "").upper() != "ACTIVE":
                continue
            other = row.get("settings")
            if not isinstance(other, dict) or not isinstance(other.get("region"), str):
                raise ValueError("pairwise comparator settings are unavailable")
            if other["region"].upper() != region.upper():
                continue
            if delay is not None and other.get("delay") != delay:
                continue
            classifications = row.get("classifications")
            if (not isinstance(classifications, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    for item in classifications)):
                raise ValueError("pairwise comparator classifications are unavailable")
            if any(item["name"] == "Power Pool Alpha" for item in classifications):
                continue
            if row["id"] != alpha_id:
                ids.append(row["id"])
        return sorted(set(ids))

    def qualify_live(self, *, workflow, alpha_id, candidate_ref, source_ref,
                     contender_ids=None, evidence_refs=None, fallback_details=None):
        """Read BRAIN evidence in strict RA -> correlation order; never writes Alpha."""
        if workflow not in {"regular", "super", "optimization"}:
            raise ValueError("unsupported Candidate workflow")
        self.gateway.call("set_case", case_key=str(candidate_ref), run_id=str(candidate_ref))
        try:
            details = self.gateway.call("get_alpha_details", alpha_id=alpha_id)
        except Exception:
            details = None
        if not isinstance(details, dict) or details.get("id") != alpha_id:
            if (isinstance(fallback_details, dict)
                    and fallback_details.get("id") == alpha_id):
                return self.qualify_from_evidence(
                    workflow=workflow, alpha_id=alpha_id,
                    candidate_ref=candidate_ref, source_ref=source_ref,
                    details=fallback_details, correlations=None,
                    evidence_refs=evidence_refs,
                    external_pending=["live_alpha_readback_unavailable"])
            raise ValueError("Alpha details are unavailable")
        if ra_correlation_check_state(details) != "pass":
            return self.qualify_from_evidence(
                workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
                source_ref=source_ref, details=details, correlations=None,
                evidence_refs=evidence_refs)
        # Preserve the existing Regular workflow's local OS-pool self-correlation
        # precheck. It is only reached after terminal RA PASS; PC/SC follows it.
        try:
            local = self.gateway.call(
                "check_local_self_correlation", alpha_id=alpha_id,
                self_corr_max=self.config["gate"]["self_corr_max"])
        except Exception as exc:
            return self.qualify_from_evidence(
                workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
                source_ref=source_ref, details=details, correlations=None,
                evidence_refs={**(evidence_refs or {}),
                               "local_self_correlation_error": type(exc).__name__},
                external_pending=["local_self_correlation_unavailable"])
        if isinstance(local, dict) and isinstance(local.get("ra_details"), dict):
            details = local["ra_details"]
        if ra_correlation_check_state(details) != "pass":
            return self.qualify_from_evidence(
                workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
                source_ref=source_ref, details=details, correlations=None,
                evidence_refs={**(evidence_refs or {}),
                               "local_self_correlation": local},
                external_pending=["ra_changed_during_local_self_precheck"])
        local_state = local_self_correlation_state(
            local, self.config["gate"]["self_corr_max"])
        if local_state == "fail":
            return self.qualify_from_evidence(
                workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
                source_ref=source_ref, details=details, correlations=None,
                evidence_refs={**(evidence_refs or {}),
                               "local_self_correlation": local},
                external_failures=["local_self_correlation"])
        if local_state != "pass":
            return self.qualify_from_evidence(
                workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
                source_ref=source_ref, details=details, correlations=None,
                evidence_refs={**(evidence_refs or {}),
                               "local_self_correlation": local},
                external_pending=["local_self_correlation_precheck_unresolved"])
        if contender_ids is None and workflow in {"super", "optimization"}:
            try:
                contender_ids = self._discover_pairwise_contenders(alpha_id, details)
            except Exception as exc:
                return self.qualify_from_evidence(
                    workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
                    source_ref=source_ref, details=details, correlations=None,
                    evidence_refs={**(evidence_refs or {}),
                                   "pairwise_pool_error": type(exc).__name__},
                    external_pending=["pairwise_comparator_pool_unavailable"])
        contender_ids = contender_ids or []
        # The gateway re-reads Alpha details and independently enforces
        # terminal RA PASS before making either PC or SC request.
        try:
            raw = self.gateway.call(
                "check_correlation", alpha_id=alpha_id,
                contender_ids=contender_ids or [],
                self_corr_max=self.config["gate"]["self_corr_max"])
        except Exception as exc:
            return self.qualify_from_evidence(
                workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
                source_ref=source_ref, details=details, correlations=None,
                evidence_refs={**(evidence_refs or {}),
                               "correlation_error": type(exc).__name__},
                external_pending=["platform_correlation_unavailable:" + type(exc).__name__])
        # Re-read details after the correlation request so the final Candidate
        # record is bound to current RA/metric evidence, not a stale snapshot.
        try:
            latest = self.gateway.call("get_alpha_details", alpha_id=alpha_id)
        except Exception as exc:
            latest = None
            readback_error = type(exc).__name__
        else:
            readback_error = None
        if not isinstance(latest, dict) or latest.get("id") != alpha_id:
            return self.qualify_from_evidence(
                workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
                source_ref=source_ref, details=details, correlations=None,
                evidence_refs={**(evidence_refs or {}),
                               "local_self_correlation": local,
                               "correlation_raw": raw,
                               "post_correlation_readback_error": readback_error},
                external_pending=["post_correlation_alpha_readback_unavailable"])
        parsed = parse_correlations(raw, contender_ids or [])
        if ra_correlation_check_state(latest) != "pass":
            parsed = None
        return self.qualify_from_evidence(
            workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
            source_ref=source_ref, details=latest, correlations=parsed,
            pairwise=(parsed or {}).get("pairwise", {}),
            expected_pairwise_ids=contender_ids or [],
            evidence_refs={**(evidence_refs or {}),
                           "local_self_correlation": local,
                           "correlation_raw": raw})

    def list_candidate_evaluations(self):
        return self.store.list("candidate")

    def list_preferred_evaluations(self):
        return self.store.list("preferred")

    def list_candidates(self):
        current_policy = policy_hash(self.config)
        return [row for row in self.pool.list_candidates()
                if row.get("latest_policy_sha256") == current_policy]

    def list_preferred(self):
        current_policy = policy_hash(self.config)
        return [row for row in self.pool.list_preferred()
                if row.get("latest_policy_sha256") == current_policy]

    def find_reusable_evaluation(self, alpha_id, *, evidence_sha256, policy_sha256):
        """Exact evidence+policy match only; never bypass fresh RA/PC checks."""
        return self.pool.find_reusable(alpha_id, evidence_sha256=evidence_sha256,
                                       policy_sha256=policy_sha256)

    def rebuild_candidate_index(self):
        """Reconstruct global per-Alpha pools solely from versioned evaluations."""
        return self.pool.sync_index()

