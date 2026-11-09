"""Shared Candidate Qualification and Preferred Pool lifecycle.

The service enforces RA -> correlation ordering for all workflows. It is
read-only with respect to BRAIN: it fetches Alpha evidence and never submits
or mutates an Alpha.
"""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .gatekeeper import (evaluate_candidate, parse_correlations,
                         ra_correlation_check_state, local_self_correlation_state,
                         RA_CHECK_NAMES)
from .preferred import evaluate_preferred
from .records import ResearchRecordStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def _stable_id(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()[:32]


def _upsert(store, kind, record):
    try:
        previous = store.get(kind, record["candidate_id"])
    except FileNotFoundError:
        previous = None
    now = datetime.now(timezone.utc)
    if previous:
        last = datetime.fromisoformat(previous["updated_at"])
        if now <= last:
            now = last + timedelta(microseconds=1)
        record["created_at"] = previous["created_at"]
    else:
        record["created_at"] = now.isoformat()
    record["updated_at"] = now.isoformat()
    return store.put(kind, record,
                     expected_updated_at=previous["updated_at"] if previous else None)


class CandidateQualificationService:
    """One Candidate gate and two persistent pools shared by all workflows."""

    def __init__(self, gateway, state_root, config):
        self.gateway = gateway
        self.store = ResearchRecordStore(Path(state_root))
        self.config = config

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
        status = gate.get("status", "EVIDENCE_PENDING")
        if failures:
            status = ("RA_FAILED" if any(x.startswith("ra:") or x == "ra_fail_count" for x in failures)
                      else "CORR_REJECTED" if any("correlation" in x for x in failures)
                      else "METRICS_FAILED")
        elif pending:
            status = "EVIDENCE_PENDING"
        else:
            status = "CANDIDATE"
        preferred = evaluate_preferred(details, candidate_status=status, config=self.config)
        metrics = preferred.get("metrics", {})
        now = _now()
        candidate = {
            "schema_version": 1, "candidate_id": candidate_id, "alpha_id": alpha_id,
            "workflow": workflow, "source_ref": str(source_ref),
            "candidate_ref": str(candidate_ref), "status": status,
            "failures": failures, "pending": pending, "metrics": metrics,
            "correlations": correlations or {}, "evidence_refs": evidence_refs or {},
            "preferred_status": preferred["status"], "created_at": now, "updated_at": now,
        }
        candidate_path = _upsert(self.store, "candidate", candidate)
        preferred_record = {
            "schema_version": 1, "candidate_id": candidate_id, "alpha_id": alpha_id,
            "workflow": workflow, "source_ref": str(source_ref),
            "candidate_ref": str(candidate_ref), "status": preferred["status"],
            "failures": preferred.get("failures", []), "pending": preferred.get("pending", []),
            "metrics": metrics,
            "evidence_refs": {**(evidence_refs or {}), "candidate_record": str(candidate_path)},
            "created_at": _now(), "updated_at": _now(),
        }
        preferred_path = _upsert(self.store, "preferred", preferred_record)
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
        gate = evaluate_candidate(
            details, {"checks": (details.get("is") or {}).get("checks")},
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

    def qualify_live(self, *, workflow, alpha_id, candidate_ref, source_ref,
                     contender_ids=None, evidence_refs=None):
        """Read BRAIN evidence in strict RA -> correlation order; never writes Alpha."""
        if workflow not in {"regular", "super", "optimization"}:
            raise ValueError("unsupported Candidate workflow")
        self.gateway.call("set_case", case_key=str(candidate_ref), run_id=str(candidate_ref))
        details = self.gateway.call("get_alpha_details", alpha_id=alpha_id)
        if not isinstance(details, dict) or details.get("id") != alpha_id:
            raise ValueError("Alpha details are unavailable")
        if ra_correlation_check_state(details) != "pass":
            return self.qualify_from_evidence(
                workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
                source_ref=source_ref, details=details, correlations=None,
                evidence_refs=evidence_refs)
        local = self.gateway.call("check_local_self_correlation", alpha_id=alpha_id,
                                  self_corr_max=self.config["gate"]["self_corr_max"])
        if isinstance(local, dict) and isinstance(local.get("ra_details"), dict):
            details = local["ra_details"]
        ra_state = ra_correlation_check_state(details)
        local_state = local_self_correlation_state(
            local, self.config["gate"]["self_corr_max"])
        refs = {**(evidence_refs or {}), "local_self_correlation": local}
        if ra_state != "pass" or local_state != "pass":
            result = self.qualify_from_evidence(
                workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
                source_ref=source_ref, details=details, correlations=None,
                evidence_refs=refs)
            if local_state == "fail":
                result["candidate_status"] = "CORR_REJECTED"
                result["candidate_failures"] = sorted(set(result["candidate_failures"] + ["local_self_correlation"]))
            else:
                result["candidate_status"] = "EVIDENCE_PENDING"
                result["candidate_pending"] = sorted(set(result["candidate_pending"] + ["local_self_correlation_unavailable"]))
            return result
        raw = self.gateway.call("check_correlation", alpha_id=alpha_id,
                                contender_ids=contender_ids or [],
                                self_corr_max=self.config["gate"]["self_corr_max"])
        parsed = parse_correlations(raw, contender_ids or [])
        return self.qualify_from_evidence(
            workflow=workflow, alpha_id=alpha_id, candidate_ref=candidate_ref,
            source_ref=source_ref, details=details, correlations=parsed,
            pairwise=parsed.get("pairwise", {}),
            expected_pairwise_ids=contender_ids or [],
            evidence_refs={**refs, "correlation_raw": raw})

    def list_candidates(self):
        return self.store.list("candidate")

    def list_preferred(self):
        return self.store.list("preferred")
