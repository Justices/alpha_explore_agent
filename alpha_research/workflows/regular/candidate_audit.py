"""Canonical BRAIN audit and retained-candidate revalidation.

All platform operations remain delegated through ResearchAgent's authorized
Gateway and recovery context. This component never submits an Alpha, changes
the user scope, or bypasses RA / platform / correlation qualification.
"""
import json
from datetime import datetime
from pathlib import Path

from alpha_research.config import DEFAULT_CONFIG
from alpha_research.qualification.audit_policy import reconcile_audit_evidence
from alpha_research.qualification.gates import (
    evaluate_candidate, local_self_correlation_state, parse_correlations,
)
from alpha_research.qualification.ppa import ppa_gate_kwargs
from alpha_research.qualification.service import CandidateQualificationService
from alpha_research.state.store import atomic_write_json, verify_prereg
from alpha_research.workflows.regular.errors import ResearchError


class CandidateAuditService:
    def __init__(self, agent):
        self.agent = agent

    def audit(self, batch_dir, prereg, alpha_ids, readback, gate,
                               contender_ids=None, *, audit_id=None, error_dir=None):
        raw_path = Path(batch_dir) / "simulation_raw.json"
        try:
            runner = json.loads(raw_path.read_text(encoding="utf-8")).get("runner")
            if not isinstance(runner, dict):
                raise ResearchError("canonical runner evidence is missing")
            results_path = Path(runner["output"]).resolve()
            receipt_path = results_path.with_suffix(".admission.json")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            variants = receipt.get("variants")
            if not isinstance(variants, list):
                raise ResearchError("canonical admission receipt has no variants")
            entries = {}
            for index, alpha_id in enumerate(alpha_ids):
                expression = prereg["variants"][index]["expression"]
                matches = [row for row in variants
                           if isinstance(row, dict) and row.get("expression") == expression]
                if len(matches) != 1:
                    raise ResearchError("canonical receipt variant does not bind uniquely")
                review = readback[alpha_id].get("review") or {}
                review_reason = review.get("reason")
                if not isinstance(review_reason, str) or not review_reason.strip():
                    review_reason = "No completed model review evidence was recorded."
                review_clear = (review.get("verdict") == "CLEAR"
                                and isinstance(review.get("evidence_refs"), list)
                                and bool(review["evidence_refs"]))
                review_status = "clear" if review_clear else "pending"
                reviews = {
                    "robustness": {"status": review_status, "evidence": review_reason},
                    "tradeoff": {"status": review_status, "evidence": review_reason},
                }
                entries[alpha_id] = {
                    "dataset": prereg["dataset_id"], "subtype": "REGULAR",
                    "receipt": str(receipt_path), "results": str(results_path),
                    "variant": matches[0].get("name"),
                    "field_cache_root": runner.get("field_cache_root"),
                    "reviews": reviews,
                }
            params = {"case_key": runner["case_key"], "run_id": runner["run_id"],
                      "batch_id": Path(batch_dir).name, "alpha_ids": alpha_ids, "entries": entries,
                      "self_corr_max": self.agent.config["gate"]["self_corr_max"],
                      "contender_ids": contender_ids if contender_ids is not None else alpha_ids}
            if audit_id is not None:
                params["audit_id"] = audit_id
            audited = self.agent.gateway.call("audit_alphas", **params)
            rows = audited.get("results") if isinstance(audited, dict) else None
            audit_path = audited.get("audit_path") if isinstance(audited, dict) else None
            if not isinstance(rows, list) or any(
                    not isinstance(row, dict) or not isinstance(row.get("alpha_id"), str)
                    or not row["alpha_id"] for row in rows):
                raise ResearchError("canonical audit rows are malformed")
            by_id, duplicates = {}, set()
            for row in rows:
                alpha_id = row["alpha_id"]
                if alpha_id in by_id:
                    duplicates.add(alpha_id)
                else:
                    by_id[alpha_id] = row
            for alpha_id in duplicates:
                by_id.pop(alpha_id, None)
        except Exception as exc:
            error_path = (Path(error_dir) / f"{audit_id}_error.json"
                          if error_dir is not None and audit_id is not None
                          else Path(batch_dir) / "canonical_audit_error.json")
            atomic_write_json(error_path, {
                "status": "UNAVAILABLE", "error_type": type(exc).__name__,
                "reason": str(exc)[:2000],
                "at": datetime.now().astimezone().isoformat()})
            by_id = {}
            audit_path = None

        for alpha_id, verdict in gate.items():
            result = by_id.get(alpha_id)
            if not isinstance(result, dict):
                verdict.setdefault("pending", []).append("canonical_audit_unavailable")
                verdict["pending"] = sorted(set(verdict["pending"]))
                if verdict.get("status") == "CANDIDATE":
                    verdict["status"] = "PENDING_FULL_AUDIT"
                verdict["canonical_audit"] = {"status": "unavailable",
                                               "audit_path": audit_path}
                continue
            audit_summary = {
                "candidate_eligible": result.get("candidate_eligible"),
                "evidence_complete": result.get("evidence_complete"),
                "candidate_status": result.get("candidate_status"),
                "risk_neutralized_state": result.get("risk_neutralized_state"),
                "gate_failures": result.get("gate_failures", []),
                "gate_pending": result.get("gate_pending", []),
                "audit_path": audit_path,
            }
            configured_gate = self.agent.config.get("gate")
            effective_gate = dict(configured_gate) if isinstance(configured_gate, dict) else {}
            for key in ("risk_neutralized_sharpe_min", "risk_neutralized_fitness_min"):
                effective_gate.setdefault(key, DEFAULT_CONFIG["gate"][key])
            audit_summary["gate_config"] = effective_gate
            verdict["canonical_audit"] = audit_summary
            try:
                expression = prereg["variants"][alpha_ids.index(alpha_id)]["expression"]
                reconciled = reconcile_audit_evidence(
                    alpha_id, result, verdict, readback.get(alpha_id) or {},
                    prereg, self.agent.config, expression)
            except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
                reconciled = {"failures": [], "pending": [
                    f"canonical_audit:reconciliation_unavailable:{type(exc).__name__}"]}
            audit_summary["local_gate"] = reconciled.get("local_gate")
            audit_summary["local_correlations"] = reconciled.get("correlations")
            if (isinstance(reconciled.get("local_gate"), dict)
                    and "ppa_eligibility" in reconciled["local_gate"]):
                verdict["ppa_eligibility"] = reconciled["local_gate"]["ppa_eligibility"]
            verdict["failures"] = sorted(set(verdict.get("failures", []) +
                                              reconciled.get("failures", [])))
            verdict["pending"] = sorted(set(verdict.get("pending", []) +
                                             reconciled.get("pending", [])))
            if any(item.startswith("pairwise:") for item in verdict["failures"]):
                verdict["status"] = "PAIRWISE_CONFLICT"
            elif any(item.startswith("ra:") for item in verdict["failures"]):
                verdict["status"] = "RA_FAILED"
            elif any(item.startswith("platform_check:") for item in verdict["failures"]):
                verdict["status"] = "PLATFORM_CHECK_FAILED"
            elif any(item.endswith("_correlation") for item in verdict["failures"]):
                verdict["status"] = "CORR_REJECTED"
            elif verdict["failures"] and verdict.get("status") == "CANDIDATE":
                verdict["status"] = "METRICS_FAILED"
            elif verdict["pending"] and verdict.get("status") == "CANDIDATE":
                verdict["status"] = "PENDING_FULL_AUDIT"
            # Preferred is strictly downstream of a fully reconciled Candidate.
            # It cannot promote a pending/failed Alpha or alter Candidate status.
            try:
                qualification = CandidateQualificationService(
                    self.agent.gateway, getattr(self, "state_root", batch_dir.parent), self.agent.config).qualify_from_evidence(
                        workflow="regular", alpha_id=alpha_id,
                        candidate_ref=str(batch_dir),
                        source_ref=str(audit_path or batch_dir),
                        details=result.get("raw_details"),
                        correlations=reconciled.get("correlations"),
                        pairwise=(reconciled.get("correlations") or {}).get("pairwise", {}),
                        expected_pairwise_ids=list(
                            ((reconciled.get("correlations") or {}).get("pairwise") or {}).keys()),
                        evidence_refs={"canonical_audit": str(audit_path or ""),
                                       "batch_dir": str(batch_dir),
                                       "local_self_correlation": result.get("local_self_correlation")},
                        external_failures=verdict.get("failures", []),
                        external_pending=verdict.get("pending", []),
                        external_status=verdict.get("status"))
                verdict["status"] = qualification["candidate_status"]
                verdict["candidate_id"] = qualification["candidate_id"]
                verdict["candidate_record_ref"] = qualification["candidate_ref"]
                verdict["preferred_record_ref"] = qualification["preferred_ref"]
                verdict["preferred"] = qualification["preferred"]
                verdict["preferred_status"] = qualification["preferred"]["status"]
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                verdict.setdefault("pending", []).append(
                    "candidate_pool_persistence_unavailable:" + type(exc).__name__)
                verdict["pending"] = sorted(set(verdict["pending"]))
                if verdict.get("status") == "CANDIDATE":
                    verdict["status"] = "PENDING_FULL_AUDIT"
        return by_id

    def refresh_retained(self, contenders, current_ids, scope, self_limit, batch_dir):
        """Recheck retained candidates before using them in the active review pool."""
        evidence, unresolved = {}, []
        for alpha_id, item in contenders.items():
            if alpha_id in current_ids or item.get("local_self_rejected"):
                continue
            details = None
            try:
                details = self.agent.gateway.call("get_alpha_details", alpha_id=alpha_id)
                settings = details.get("settings") if isinstance(details, dict) else None
                if (not isinstance(details, dict) or details.get("id") != alpha_id
                        or details.get("type") != "REGULAR" or not isinstance(settings, dict)
                        or settings.get("instrumentType") != "EQUITY"
                        or any(settings.get(key) != scope.get(key)
                               for key in ("region", "universe", "delay"))
                        or type(settings.get("delay")) is not int):
                    raise ResearchError("retained contender scope is unavailable or changed")
                is_data = details.get("is")
                checks = {"checks": is_data.get("checks") if isinstance(is_data, dict) else None}
                verdict = evaluate_candidate(details, checks, scope["region"], self.agent.config)
                entry = {"details": details, "ra_fail_count": verdict["ra_fail_count"]}
                item["retained_unverified"] = False
                if verdict["ra_fail_count"]:
                    item["ra_rejected"] = True
                    self.agent._register_alpha_state(alpha_id, {"state": "RA_FAILED",
                        "ra_fail_count": verdict["ra_fail_count"], "ra_readable": True,
                        "self_corr_max": self_limit})
                    if item.get("status") != "PAIRWISE_CONFLICT":
                        item["status"] = "RA_FAILED"
                    entry["status"] = "ra_failed"
                elif not verdict["correlation_allowed"]:
                    raise ResearchError("retained contender RA checks are unavailable")
                else:
                    if item.get("ra_rejected") or item.get("status") == "RA_FAILED":
                        item["status"] = "EVIDENCE_PENDING"
                    item["ra_rejected"] = False
                    state = {"state": "RA_PASSED", "ra_fail_count": 0,
                             "ra_readable": True, "self_corr_max": self_limit}
                    self.agent._register_alpha_state(alpha_id, state)
                    try:
                        local_self = self.agent.gateway.call("check_local_self_correlation",
                                                       alpha_id=alpha_id, self_corr_max=self_limit,
                                                       batch_id=Path(batch_dir).name)
                    except Exception as exc:
                        local_self = {"status": "pending", "reason": type(exc).__name__,
                                      "limit": self_limit}
                    latest = local_self.get("ra_details") if isinstance(local_self, dict) else None
                    if isinstance(latest, dict) and latest.get("id") == alpha_id:
                        details = latest
                        is_data = details.get("is")
                        checks = {"checks": is_data.get("checks") if isinstance(is_data, dict) else None}
                        verdict = evaluate_candidate(details, checks, scope["region"], self.agent.config)
                        entry["prefilter_details"] = details
                        entry["ra_fail_count"] = verdict["ra_fail_count"]
                        if verdict["ra_fail_count"]:
                            item["ra_rejected"] = True
                            if item.get("status") != "PAIRWISE_CONFLICT":
                                item["status"] = "RA_FAILED"
                            self.agent._register_alpha_state(alpha_id, {"state": "RA_FAILED",
                                "ra_fail_count": verdict["ra_fail_count"], "ra_readable": True,
                                "self_corr_max": self_limit})
                            entry.update({"status": "ra_failed", "local_self": local_self})
                            evidence[alpha_id] = entry
                            continue
                        if not verdict["correlation_allowed"]:
                            raise ResearchError("retained contender RA checks changed or are unavailable")
                    state["local_self"] = local_self
                    self.agent._register_alpha_state(alpha_id, state)
                    local_state = local_self_correlation_state(local_self, self_limit)
                    item["local_self_rejected"] = local_state == "fail"
                    if local_state == "fail":
                        item["status"] = "CORR_REJECTED"
                    elif (local_state == "pending" or any(
                            value.startswith("ra_pending:") for value in verdict["pending"])):
                        if item.get("status") == "CANDIDATE":
                            item["status"] = "EVIDENCE_PENDING"
                    entry.update({"status": local_state, "local_self": local_self})
                evidence[alpha_id] = entry
            except Exception as exc:
                item["retained_unverified"] = True
                self.agent._register_alpha_state(alpha_id, {"state": "RA_UNAVAILABLE",
                    "ra_fail_count": None, "ra_readable": False, "self_corr_max": self_limit})
                if item.get("status") == "CANDIDATE":
                    item["status"] = "EVIDENCE_PENDING"
                evidence[alpha_id] = {"status": "unavailable", "error_type": type(exc).__name__,
                                      "details": details}
                unresolved.append(alpha_id)
        if evidence:
            atomic_write_json(Path(batch_dir) / "retained_correlation_precheck.json", evidence)
        return unresolved, evidence

    def reaudit_retained(self, contenders, current_ids, prechecks, scope,
                                     batch_dir, unresolved):
        """Re-establish every retained Candidate from current and bound evidence."""
        candidates = [alpha_id for alpha_id, item in contenders.items()
                      if alpha_id not in current_ids and item.get("reaudit_required")
                      and item.get("status") != "PAIRWISE_CONFLICT"
                      and not item.get("local_self_rejected") and not item.get("ra_rejected")
                      and not item.get("retained_unverified")]
        if not candidates:
            return
        context = json.loads((Path(batch_dir) / "batch_context.json").read_text(encoding="utf-8"))
        current_case = {"case_key": context.get("platform_case_key"),
                        "run_id": context.get("platform_run_id")}
        if not all(isinstance(value, str) and value for value in current_case.values()):
            raise ResearchError("current case runner is unavailable for retained re-audit")
        reaudits = {}
        try:
            for alpha_id in candidates:
                item = contenders[alpha_id]
                if item.get("status") == "PAIRWISE_CONFLICT":
                    continue
                verdict = None
                try:
                    prior_dir = Path(item["batch"]).resolve()
                    if not prior_dir.is_relative_to(self.agent.runs_root.resolve()):
                        raise ResearchError("retained batch is outside this research root")
                    prereg_path = prior_dir / "prereg.json"
                    state = json.loads((prior_dir / "state.json").read_text(encoding="utf-8"))
                    if not verify_prereg(prereg_path, state):
                        raise ResearchError("retained pre-registration changed")
                    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
                    prior = json.loads((prior_dir / "results.json").read_text(encoding="utf-8"))
                    intent = json.loads((prior_dir / "simulation_intent.json").read_text(encoding="utf-8"))
                    alpha_ids = prior.get("alpha_ids")
                    variants = prereg.get("variants")
                    if (intent.get("status") != "READ_BACK" or not isinstance(alpha_ids, list)
                            or len(alpha_ids) != len(set(alpha_ids)) or alpha_id not in alpha_ids
                            or not isinstance(variants, list) or len(variants) != len(alpha_ids)):
                        raise ResearchError("retained batch result does not bind the Alpha")
                    variant = variants[alpha_ids.index(alpha_id)]
                    expression = variant.get("expression") if isinstance(variant, dict) else None
                    prior_readback = (prior.get("readback") or {}).get(alpha_id)
                    prior_details = prior_readback.get("details") if isinstance(prior_readback, dict) else None
                    prior_code = ((prior_details.get("regular") or {}).get("code")
                                  if isinstance(prior_details, dict) else None)
                    if not isinstance(expression, str) or prior_code != expression:
                        raise ResearchError("retained expression is not bound to its pre-registration")
                    prereg = {**prereg, "variants": [variant]}
                    self.agent.gateway.call("set_case", **current_case)
                    self_limit = self.agent.config["gate"]["self_corr_max"]
                    state = {"state": "RA_PASSED", "ra_fail_count": 0,
                             "ra_readable": True, "self_corr_max": self_limit}
                    self.agent._register_alpha_state(alpha_id, state)
                    local_self = self.agent.gateway.call("check_local_self_correlation",
                                                   alpha_id=alpha_id, self_corr_max=self_limit,
                                                   batch_id=Path(batch_dir).name)
                    state["local_self"] = local_self
                    self.agent._register_alpha_state(alpha_id, state)
                    detail = ((local_self.get("ra_details") if isinstance(local_self, dict) else None)
                              or (prechecks.get(alpha_id) or {}).get("prefilter_details")
                              or (prechecks.get(alpha_id) or {}).get("details"))
                    if not isinstance(detail, dict) or detail.get("id") != alpha_id:
                        raise ResearchError("retained live read-back is unavailable")
                    checks = {"checks": (detail.get("is") or {}).get("checks")}
                    other_ids = [other for other, row in contenders.items()
                                 if other != alpha_id and other not in current_ids
                                 and not row.get("local_self_rejected") and not row.get("ra_rejected")
                                 and not row.get("retained_unverified")]
                    raw, correlations = None, None
                    verdict = evaluate_candidate(
                        detail, checks, scope["region"], self.agent.config,
                        expected_pairwise_ids=other_ids,
                        operator_count=(prereg.get("operator_counts") or {}).get(expression),
                        local_self=local_self, **ppa_gate_kwargs(prereg, expression))
                    if local_self_correlation_state(local_self, self_limit) == "pass":
                        raw = self.agent.gateway.call("check_correlation", alpha_id=alpha_id,
                                                contender_ids=other_ids, self_corr_max=self_limit,
                                                batch_id=Path(batch_dir).name)
                        correlations = parse_correlations(raw, other_ids)
                    verdict = evaluate_candidate(
                        detail, checks, scope["region"], self.agent.config, correlations=correlations,
                        pairwise=(correlations or {}).get("pairwise"), expected_pairwise_ids=other_ids,
                        operator_count=(prereg.get("operator_counts") or {}).get(expression),
                        local_self=local_self, **ppa_gate_kwargs(prereg, expression))
                    verdict["correlations"] = correlations
                    verdict["local_self_correlation"] = local_self
                    readback = {"details": detail, "checks": checks,
                                "local_self_correlation": local_self, "correlation_raw": raw}
                    if unresolved:
                        verdict["pending"].extend("retained_contender_unverified:" + other
                                                  for other in unresolved if other != alpha_id)
                        if verdict["pending"] and verdict["status"] == "CANDIDATE":
                            verdict["status"] = "EVIDENCE_PENDING"
                    if verdict["status"] == "CANDIDATE":
                        review = self.agent._decide("review_candidate", {
                            "objective": prereg["objective"], "dataset_id": prereg["dataset_id"],
                            "scope": prereg["scope"], "preregistration": prereg,
                            "alpha_id": alpha_id, "evidence": readback, "gate": verdict},
                            batch_dir=batch_dir)
                        readback["review"] = review
                        if (review.get("verdict") != "CLEAR" or not isinstance(review.get("reason"), str)
                                or not review["reason"].strip() or not isinstance(review.get("evidence_refs"), list)
                                or not review["evidence_refs"] or any(ref not in {
                                    "preregistration", "readback", "correlation", "gate", "probe_evidence"}
                                    for ref in review["evidence_refs"])):
                            verdict["status"] = "REVIEW_PENDING"
                            verdict["pending"].append("candidate_review")
                    gate = {alpha_id: verdict}
                    self.agent._apply_canonical_audit(
                        prior_dir, prereg, [alpha_id], {alpha_id: readback}, gate,
                        contender_ids=[alpha_id, *other_ids],
                        audit_id=f"{Path(batch_dir).name}_{alpha_id}", error_dir=batch_dir)
                    verdict = gate[alpha_id]
                    if local_self_correlation_state(local_self, self_limit) == "fail":
                        item["local_self_rejected"] = True
                    if verdict.get("ra_fail_count"):
                        item["ra_rejected"] = True
                except Exception as exc:
                    if not isinstance(verdict, dict):
                        verdict = {"status": "PENDING_FULL_AUDIT", "failures": [], "pending": []}
                    verdict.setdefault("pending", []).append("retained_reaudit_unavailable:" + type(exc).__name__)
                    if verdict.get("status") == "CANDIDATE":
                        verdict["status"] = "PENDING_FULL_AUDIT"
                item["status"] = verdict["status"]
                item["reaudit_required"] = verdict["status"] != "CANDIDATE"
                reaudits[alpha_id] = verdict
                for failure in verdict.get("failures", []):
                    if failure.startswith("pairwise:"):
                        other = failure.split(":", 1)[1]
                        if other in contenders:
                            contenders[other]["status"] = "PAIRWISE_CONFLICT"
                            contenders[other]["reaudit_required"] = True
                            reaudits[other] = {"status": "PAIRWISE_CONFLICT",
                                "failures": ["pairwise:" + alpha_id],
                                "pending": ["pairwise_conflict_requires_user_choice"]}
                for pending in verdict.get("pending", []):
                    if pending.startswith("pairwise_unavailable:"):
                        other = pending.split(":", 1)[1]
                        if other in contenders and contenders[other].get("status") == "CANDIDATE":
                            contenders[other]["status"] = "PAIRWISE_PENDING"
                            contenders[other]["reaudit_required"] = True
            atomic_write_json(Path(batch_dir) / "retained_candidate_audits.json", reaudits)
        finally:
            self.agent.gateway.call("set_case", **current_case)

