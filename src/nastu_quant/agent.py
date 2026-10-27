"""One recoverable research batch driven by an interchangeable JSON model.

The model proposes economic decisions. Catalog admission, expression checks,
pre-registration, platform access and evidence persistence remain deterministic.
"""
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import hashlib
import re

from .backtest_cache import BacktestCache, BacktestCacheError
from .state_store import atomic_write_json, lock_prereg, verify_prereg
from .validator import validate
from .recovery import match_alphas_detailed
from .gatekeeper import evaluate_candidate, parse_correlations
from .config import DEFAULT_CONFIG
from .campaign import CampaignStore
from .diagnosis import DiagnosisError, diagnose_batch
from .knowledge import KnowledgeService
from .model_client import DecisionRecorder
import json


class ResearchError(RuntimeError):
    pass


FIELD_EVIDENCE_KEYS = {
    "nominal_coverage": "coverage",
    "usable_observations": "zeroFraction",
    "refresh_rate": "updateFrequency",
    "scale_units": "units",
    "typical_level": "median",
    "distribution": "distribution",
}


def _rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "result"):
            if key in payload:
                return _rows(payload[key])
    raise ResearchError("catalog response is not a readable list")


def _catalog(rows, dataset_id):
    catalog = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        ds = row.get("dataset") or {}
        ds_id = ds.get("id") if isinstance(ds, dict) else ds
        if (ds_id != dataset_id or row.get("type") not in {"MATRIX", "VECTOR"}
                or not isinstance(row.get("description"), str)
                or not row["description"].strip()):
            continue
        catalog[row["id"]] = {"type": row.get("type"), "dataset": dataset_id}
    if not catalog:
        raise ResearchError("selected Dataset has no readable fields")
    return catalog


class ResearchAgent:
    def __init__(self, gateway, model, config: dict, runs_root: Path,
                 stop_flag: Path = None):
        self.gateway = gateway
        self.model = model
        self.config = config
        self.runs_root = Path(runs_root)
        self.state_root = self.runs_root.resolve().parent / "state"
        self.stop_flag = Path(stop_flag) if stop_flag else None
        self.knowledge = KnowledgeService(self.state_root, self.runs_root, config)
        self.recorder = DecisionRecorder(model, self.state_root)
        self.campaigns = CampaignStore(self.state_root)
        cache_path = self.runs_root / "backtest_cache.cache"
        cache_missing = not cache_path.exists()
        self.backtest_cache = BacktestCache(cache_path)
        try:
            if cache_missing or self.backtest_cache.needs_rebuild:
                self.backtest_cache.sync_closed_runs(self.runs_root,
                                                     self._platform_settings)
        except BacktestCacheError as exc:
            raise ResearchError(f"backtest cache initialization failed: {exc}") from exc
        self._session_packs = []
        if hasattr(gateway, "alpha_lookup"):
            gateway.alpha_lookup = self._lookup_alpha_state

    def _begin_session(self):
        session_id = self.recorder.begin_session()
        self._session_packs = []
        return session_id

    def _save_knowledge_pack(self, pack, batch_dir=None):
        raw = json.dumps(pack, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        pack_id = digest[:20]
        path = self.state_root / "logs" / "knowledge_packs" / f"{pack_id}.json"
        if not path.exists():
            atomic_write_json(path, pack)
        ref = {"pack_id": pack_id, "sha256": digest, "source_ref": str(path),
               "session_id": self.recorder.session_id}
        self._session_packs.append(ref)
        if batch_dir is not None:
            self._write_batch_knowledge_packs(batch_dir)
        return ref

    def _write_batch_knowledge_packs(self, batch_dir):
        batch_dir = Path(batch_dir)
        batch_dir.mkdir(parents=True, exist_ok=True)
        existing = (json.loads((batch_dir / "knowledge_pack.json").read_text(encoding="utf-8"))
                    if (batch_dir / "knowledge_pack.json").exists() else {})
        packs_by_id = {row.get("pack_id"): row for row in existing.get("packs", [])
                       if isinstance(row, dict) and row.get("pack_id")}
        for ref in self._session_packs:
            try:
                pack = json.loads(Path(ref["source_ref"]).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ResearchError("knowledge pack reference is unreadable") from exc
            packs_by_id.setdefault(ref["pack_id"], {**ref, "pack": pack})
        atomic_write_json(batch_dir / "knowledge_pack.json", {
            "schema_version": 1,
            "session_ids": sorted({row.get("session_id") for row in packs_by_id.values()
                                   if row.get("session_id")}),
            "packs": list(packs_by_id.values())})

    def _decide(self, task, context, batch_dir=None):
        pack = self.knowledge.build_pack(task, context)
        ref = self._save_knowledge_pack(pack, batch_dir)
        if batch_dir is not None:
            self.recorder.bind_batch(batch_dir)
        enriched = {**context, "knowledge_pack": pack}
        return self.recorder.complete_json(task, enriched,
                                           knowledge_pack_ref=ref["source_ref"])

    def _lookup_alpha_state(self, alpha_id):
        path = self.runs_root / "alpha_states.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8")).get(alpha_id)

    def _assert_gate_config(self):
        configured = self.config.get("gate") or {}
        for name, canonical in DEFAULT_CONFIG["gate"].items():
            if configured.get(name) != canonical:
                raise ResearchError(f"configured {name} differs from current research rule")

    def _assert_auth_mode(self):
        mode = (self.config.get("auth") or {}).get("mode", "auto")
        if mode != "auto":
            raise ResearchError(f"authorization mode {mode} is not implemented for unattended execution")

    def _register_alpha_state(self, alpha_id, state):
        path = self.runs_root / "alpha_states.json"
        states = (json.loads(path.read_text(encoding="utf-8"))
                  if path.exists() else {})
        states[alpha_id] = state
        atomic_write_json(path, states)

    def _case_path(self, objective, scope):
        key = hashlib.sha256(json.dumps({
            "objective": objective, "region": scope["region"],
            "universe": scope["universe"], "delay": scope["delay"]},
            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        return self.runs_root / "cases" / f"{key}.json"

    def _resolve_remedy_reference(self, objective, diagnosis_id, remedy_id,
                                  allow_round_overrun=False):
        if (diagnosis_id is None) != (remedy_id is None):
            raise ResearchError("a remedy batch requires both diagnosis_id and remedy_id")
        if diagnosis_id is None:
            return None
        matches = []
        for path in self.runs_root.rglob("diagnosis.json") if self.runs_root.exists() else []:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if row.get("diagnosis_id") == diagnosis_id:
                matches.append((path.resolve(), row))
        if len(matches) != 1:
            raise ResearchError("diagnosis_id must identify exactly one local diagnosis")
        diagnosis_path, diagnosis = matches[0]
        if diagnosis.get("objective") != objective:
            raise ResearchError("remedy diagnosis belongs to a different objective")
        selected = None
        selected_alpha = None
        selected_signatures = []
        for alpha_id, row in (diagnosis.get("alphas") or {}).items():
            candidate = next((item for item in row.get("remedy_candidates", [])
                              if item.get("remedy_id") == remedy_id), None)
            if candidate is not None:
                selected, selected_alpha = candidate, alpha_id
                signatures, facts = row.get("signatures", []), row.get("field_facts", {})
                selected_signatures = list(signatures)
                if not any(item.get("remedy_id") == remedy_id for item in
                           self.knowledge.lookup_remedies(signatures, facts)["verified"]):
                    selected = None
                    break
                break
        if selected is None:
            raise ResearchError("remedy is not a currently verified candidate for this diagnosis")
        mechanism_tags = diagnosis.get("mechanism_tags") or []
        dataset_id = diagnosis.get("dataset_id")
        rounds = 0
        for prereg_path in self.runs_root.rglob("prereg.json"):
            if (prereg_path.parent / "void.json").exists():
                continue
            try:
                prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ref = prereg.get("remedy_reference") or {}
            if (prereg.get("dataset_id") == dataset_id and
                    sorted(ref.get("mechanism_tags") or []) == sorted(mechanism_tags) and
                    {key: (prereg.get("scope") or {}).get(key) for key in
                     ("region", "delay", "universe")} == (diagnosis.get("scope") or {})):
                rounds += 1
        if rounds >= 2 and not allow_round_overrun:
            raise ResearchError("remedy lineage exhausted at two rounds; explicit override required")
        return {"diagnosis_id": diagnosis_id, "diagnosis_ref": str(diagnosis_path),
                "source_batch": diagnosis.get("batch"), "source_alpha_id": selected_alpha,
                "remedy_id": remedy_id, "dataset_id": dataset_id,
                "scope": diagnosis.get("scope") or {},
                "mechanism_tags": mechanism_tags,
                "target_signatures": selected_signatures, "round": rounds + 1,
                "remedy": selected}

    def _read_case(self, objective, scope):
        path = self._case_path(objective, scope)
        case = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                else {"objective": objective,
                      "scope": {key: scope[key] for key in
                                ("region", "universe", "delay")},
                      "contenders": {}, "batches": []})
        if (case.get("objective") != objective or
                case.get("scope") != {key: scope[key] for key in
                                      ("region", "universe", "delay")}):
            raise ResearchError("case scope changed; research pooling blocked")
        return case

    def _scope(self, objective, scope_override=None):
        scope = dict(self.config.get("scope") or {})
        for key, value in (scope_override or {}).items():
            if scope.get(key) is not None and scope[key] != value:
                raise ResearchError(f"fixed case scope conflicts with config: {key}")
            scope[key] = value
        if not scope.get("region"):
            raise ResearchError("Region is required before platform research")
        options = self.gateway.call("get_platform_setting_options")
        combos = options.get("instrument_options") if isinstance(options, dict) else None
        if not isinstance(combos, list) or not combos:
            raise ResearchError("live platform setting options unavailable")
        effective = dict(scope)
        if any(effective.get(key) is None for key in ("universe", "delay", "neutralization")):
            selected = self._decide("select_scope", {
                "objective": objective, "supplied_scope": scope,
                "supported_combinations": combos})
            for key in ("universe", "delay", "neutralization"):
                if effective.get(key) is None:
                    effective[key] = selected.get(key)
        selected_row = next((row for row in combos if isinstance(row, dict)
                             and row.get("InstrumentType") == "EQUITY"
                             and row.get("Region") == effective["region"]
                             and row.get("Delay") == effective.get("delay")
                             and effective.get("universe") in (row.get("Universe") or [])
                             and effective.get("neutralization") in
                             (row.get("Neutralization") or [])), None)
        if selected_row is None:
            raise ResearchError("effective scope is not a live supported combination")
        effective["_supported_neutralizations"] = selected_row["Neutralization"]
        for key in ("decay", "truncation", "pasteurization",
                    "unit_handling", "nan_handling", "test_period", "max_trade"):
            if effective.get(key) is None:
                raise ResearchError(f"missing effective scope setting: {key}")
        return effective

    def _platform_settings(self, scope, *, probe=False):
        return {
            "instrument_type": "EQUITY",
            "region": scope["region"], "universe": scope["universe"],
            "delay": scope["delay"], "decay": 0 if probe else scope["decay"],
            "neutralization": "NONE" if probe else scope["neutralization"],
            "truncation": scope["truncation"],
            "pasteurization": scope["pasteurization"],
            "unit_handling": scope["unit_handling"],
            "nan_handling": scope["nan_handling"],
            "language": "FASTEXPR", "visualization": False,
            "test_period": scope["test_period"],
            "max_trade": scope["max_trade"],
        }

    def _ensure_expressions_uncached(self, expressions, dataset_id, scope, settings,
                                     batch_dir, *, label):
        """Reject repeated expressions and Bloom possible-hits before simulation."""
        seen = set()
        problems = []
        try:
            for expression in expressions:
                fingerprint = self.backtest_cache.fingerprint(
                    expression, dataset_id, scope, settings)
                if fingerprint in seen:
                    problems.append({"expression": expression,
                                     "reason": "duplicate in current batch"})
                    continue
                seen.add(fingerprint)
                if self.backtest_cache.find(expression, dataset_id, scope, settings):
                    problems.append({"expression": expression,
                                     "reason": "Bloom maybe-hit; false positive possible"})
        except BacktestCacheError as exc:
            reason = f"backtest cache lookup failed before {label}: {exc}"
            try:
                self.void_batch(batch_dir, reason)
            except ResearchError as void_error:
                reason += f"; draft needs recovery: {void_error}"
            raise ResearchError(reason) from exc
        if not problems:
            return
        summary = "; ".join(f"{item['expression']} ({item['reason']})"
                             for item in problems)
        reason = f"backtest cache hit before {label}: {summary}"
        try:
            self.void_batch(batch_dir, reason)
        except ResearchError as exc:
            raise ResearchError(f"{reason}; draft requires recovery: {exc}") from exc
        raise ResearchError(f"{reason}; draft marked VOID")

    def _record_completed_expressions(self, expressions, dataset_id, scope, settings):
        records = [{"expression": expression, "dataset_id": dataset_id,
                    "scope": scope, "settings": settings}
                   for expression in expressions]
        try:
            self.backtest_cache.record_many(records)
        except BacktestCacheError as exc:
            raise ResearchError(f"completed backtest could not be cached: {exc}") from exc

    def _record_probe_batch(self, context, expressions, rows, settings):
        alpha_ids = [row.get("alpha_id") for row in rows if isinstance(row, dict)
                     and isinstance(row.get("alpha_id"), str) and row["alpha_id"]]
        if len(alpha_ids) != len(expressions) or len(set(alpha_ids)) != len(alpha_ids):
            raise ResearchError("diagnostic children unresolved; do not mark READ_BACK")
        self._record_completed_expressions(
            expressions, context["dataset_id"], context["scope"], settings)

    def _simulate(self, expressions, settings):
        if self.stop_flag and self.stop_flag.exists():
            raise ResearchError("stop flag is set; no new simulation posted")
        if len(expressions) == 1:
            raw = self.gateway.call("create_simulation", regular=expressions[0],
                                    **settings)
            if isinstance(raw, dict) and raw.get("id") and not raw.get("alpha_id"):
                return {"alpha_id": raw["id"],
                        "details": {key: value for key, value in raw.items()
                                    if key != "note"}}
            return raw
        return self.gateway.call("create_multi_simulation",
                                 alpha_expressions=expressions, **settings)

    def _unfinished_batch(self):
        if not self.runs_root.exists():
            return None
        for path in self.runs_root.rglob("batch_context.json"):
            batch_dir = path.parent
            if (batch_dir / "void.json").exists():
                continue
            intent_path = batch_dir / "simulation_intent.json"
            results_path = batch_dir / "results.json"
            if not results_path.exists():
                return batch_dir
            if intent_path.exists():
                try:
                    intent = json.loads(intent_path.read_text(encoding="utf-8"))
                    closed = intent.get("status") == "READ_BACK"
                except (OSError, ValueError):
                    closed = False
                if not closed:
                    return batch_dir
                prereg_path = batch_dir / "prereg.json"
                if prereg_path.exists():
                    try:
                        prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
                        if prereg.get("remedy_reference"):
                            diagnosis = json.loads((batch_dir / "diagnosis.json").read_text(
                                encoding="utf-8"))
                            if (diagnosis.get("remedy_outcome") or {}).get("status") != "recorded":
                                return batch_dir
                    except (OSError, ValueError):
                        return batch_dir
            elif (batch_dir / "prereg.json").exists():
                return path.parent
        for batch_dir in self.runs_root.glob("*/*/*/*/*"):
            if (batch_dir.is_dir() and
                    re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", batch_dir.name) and
                    not (batch_dir / "results.json").exists() and
                    not (batch_dir / "void.json").exists()):
                return batch_dir
        return None

    def void_batch(self, batch_dir: Path, reason: str) -> dict:
        batch_dir = Path(batch_dir).resolve()
        if not batch_dir.is_relative_to(self.runs_root.resolve()):
            raise ResearchError("batch path outside research runs")
        if not reason or not reason.strip():
            raise ResearchError("void reason is required")
        context_path = batch_dir / "batch_context.json"
        if not context_path.exists():
            # A process can stop after mkdir and before the first durable batch
            # artifact. Only this provably unsubmitted empty directory is voidable.
            relative_parts = batch_dir.relative_to(self.runs_root.resolve()).parts
            if (not batch_dir.is_dir() or len(relative_parts) != 5 or
                    not re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", batch_dir.name)):
                raise ResearchError("contextless batch is not a recognized batch directory")
            if any((batch_dir / name).exists() for name in
                   ("prereg.json", "simulation_intent.json", "simulation_raw.json",
                    "state.json", "gate.json", "results.json")) or list(
                       batch_dir.glob("probe_*_intent.json")) or list(
                       batch_dir.glob("probe_*_raw.json")):
                raise ResearchError("contextless batch has submission evidence; recovery required")
            record = {"status": "VOID", "reason": reason.strip(),
                      "at": datetime.now().astimezone().isoformat()}
            atomic_write_json(batch_dir / "void.json", record)
            return record
        probe_intents = list(batch_dir.glob("probe_*_intent.json"))
        if ((batch_dir / "simulation_intent.json").exists() or any(
                json.loads(path.read_text(encoding="utf-8")).get("status") != "READ_BACK"
                or not (batch_dir / path.name.replace("_intent.json", "_raw.json")).exists()
                for path in probe_intents)):
            raise ResearchError("submitted or possibly submitted batch requires recovery")
        if (batch_dir / "results.json").exists():
            raise ResearchError("completed batch cannot be voided")
        record = {"status": "VOID", "reason": reason.strip(),
                  "at": datetime.now().astimezone().isoformat()}
        atomic_write_json(batch_dir / "void.json", record)
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if not isinstance(context, dict) or not isinstance(context.get("scope"), dict) or not context.get("objective"):
            return record
        case_path = self._case_path(context["objective"], context["scope"])
        campaign = self.campaigns.get(case_path.stem)
        if campaign.get("current_batch") == str(batch_dir):
            self.campaigns.update(case_path.stem, current_batch=None,
                                  dataset_state="draft_voided", status="draft_voided",
                                  candidate_ledger_ref=str(case_path))
        return record

    def _complete(self, batch_dir, prereg, alpha_ids, digest,
                  simulation_settings=None):
        readback = {}
        for alpha_id in alpha_ids:
            details = self.gateway.call("get_alpha_details", alpha_id=alpha_id)
            if not isinstance(details, dict) or details.get("id") != alpha_id:
                raise ResearchError("Alpha read-back ID mismatch; gate remains pending")
            is_data = details.get("is") if isinstance(details, dict) else None
            readback[alpha_id] = {"details": details,
                                  "checks": {"checks": (
                                      is_data.get("checks") if isinstance(is_data, dict)
                                      else None)}}
        scope = prereg["scope"]
        simulation_settings = (simulation_settings or prereg.get("simulation_settings")
                               or self._platform_settings(scope))
        case_path = self._case_path(prereg["objective"], scope)
        case = self._read_case(prereg["objective"], scope)
        operator_counts = prereg.get("operator_counts") or {}
        prelim = {}
        for alpha_id, evidence in readback.items():
            expression = prereg["variants"][alpha_ids.index(alpha_id)]["expression"]
            prelim[alpha_id] = evaluate_candidate(
                evidence["details"], evidence["checks"], scope["region"],
                self.config, operator_count=operator_counts.get(expression))
        contenders = dict(case.get("contenders") or {})
        for alpha_id in alpha_ids:
            verdict = prelim[alpha_id]
            if (verdict["correlation_allowed"] and not verdict["failures"]
                    and all(item.startswith(("ra_pending:", "prod_correlation_",
                                              "self_correlation_"))
                            for item in verdict["pending"])):
                contenders.setdefault(alpha_id, {"status": "EVIDENCE_PENDING",
                                                 "batch": str(batch_dir)})
        gate = {}
        for alpha_id, evidence in readback.items():
            preliminary = prelim[alpha_id]
            correlations = None
            expected = [other for other in contenders if other != alpha_id]
            if preliminary["correlation_allowed"]:
                self._register_alpha_state(alpha_id, {
                    "state": "RA_PASSED", "ra_fail_count": 0,
                    "ra_readable": True})
                raw_correlation = self.gateway.call("check_correlation",
                                                    alpha_id=alpha_id)
                correlations = parse_correlations(raw_correlation, expected)
                evidence["correlation_raw"] = raw_correlation
            expression = prereg["variants"][alpha_ids.index(alpha_id)]["expression"]
            verdict = evaluate_candidate(
                evidence["details"], evidence["checks"],
                scope["region"], self.config,
                correlations=correlations,
                pairwise=(correlations or {}).get("pairwise"),
                expected_pairwise_ids=expected,
                operator_count=operator_counts.get(expression))
            verdict["correlations"] = correlations
            if verdict["status"] == "CANDIDATE":
                review = self._decide("review_candidate", {
                    "objective": prereg["objective"],
                    "dataset_id": prereg["dataset_id"], "scope": scope,
                    "preregistration": prereg,
                    "alpha_id": alpha_id, "evidence": evidence,
                    "gate": verdict}, batch_dir=batch_dir)
                evidence["review"] = review
                if (review.get("verdict") != "CLEAR" or
                        not isinstance(review.get("reason"), str) or
                        not review["reason"].strip() or
                        not isinstance(review.get("evidence_refs"), list) or
                        not review["evidence_refs"] or
                        any(ref not in {"preregistration", "readback", "correlation",
                                        "gate", "probe_evidence"}
                            for ref in review["evidence_refs"])):
                    verdict["status"] = (review.get("verdict")
                                         if review.get("verdict") in {
                                             "ROBUSTNESS_REVIEW", "TRADEOFF_REVIEW"}
                                         else "REVIEW_PENDING")
                    verdict["pending"].append("candidate_review")
            gate[alpha_id] = verdict
            if alpha_id in contenders:
                contenders[alpha_id]["status"] = verdict["status"]
            for failed in verdict["failures"]:
                if failed.startswith("pairwise:"):
                    other = failed.split(":", 1)[1]
                    if other in contenders:
                        contenders[other]["status"] = "PAIRWISE_CONFLICT"
            for item in verdict["pending"]:
                if item.startswith("pairwise_unavailable:"):
                    other = item.split(":", 1)[1]
                    if other in contenders and contenders[other]["status"] == "CANDIDATE":
                        contenders[other]["status"] = "PAIRWISE_PENDING"
        for alpha_id, verdict in gate.items():
            current = contenders.get(alpha_id, {}).get("status")
            if current in {"PAIRWISE_CONFLICT", "PAIRWISE_PENDING"}:
                verdict["status"] = current
        case["contenders"] = contenders
        atomic_write_json(batch_dir / "gate.json", gate)
        proposal = prereg["proposal"]
        try:
            diagnosis = diagnose_batch(
                batch_dir, prereg, gate, readback, self.config,
                self.knowledge.lookup_remedies)
        except DiagnosisError as exc:
            raise ResearchError(f"diagnosis refused: {exc}") from exc
        failed_alphas = {alpha_id: row for alpha_id, row in
                         diagnosis["alphas"].items() if row["signatures"]}
        if not failed_alphas:
            diagnosis["model_diagnosis"] = {"status": "no_failure_signatures"}
        else:
            try:
                model_diagnosis = self._decide("diagnose_failures", {
                    "objective": prereg["objective"],
                    "dataset_id": prereg["dataset_id"],
                    "scope": scope,
                    "mechanism_tags": prereg["proposal"].get("mechanism_tags", []),
                    "field_generation_tags": prereg["proposal"].get("field_generation_tags", []),
                    "variants": prereg["variants"],
                    "failure_signatures": sorted({signature for row in
                        failed_alphas.values() for signature in row["signatures"]}),
                    "diagnosis": diagnosis, "gate": gate,
                    "readback": readback, "preregistration": prereg},
                    batch_dir=batch_dir)
                self._apply_diagnosis_model(diagnosis, model_diagnosis)
            except Exception as exc:
                diagnosis["model_diagnosis"] = {
                    "status": "unavailable", "error_type": type(exc).__name__,
                    "error": str(exc)}
        atomic_write_json(batch_dir / "diagnosis.json", diagnosis)
        attribution = self._decide("attribute_results", {
            "objective": prereg["objective"], "hypothesis": proposal["hypothesis"],
            "dataset_id": prereg["dataset_id"], "scope": scope,
            "mechanism_tags": proposal.get("mechanism_tags", []),
            "field_generation_tags": proposal.get("field_generation_tags", []),
            "failure_criterion": proposal["failure_criterion"],
            "variants": prereg["variants"], "readback": readback, "gate": gate,
            "failure_signatures": {key: row["signatures"]
                                   for key, row in diagnosis["alphas"].items()},
            "diagnosis": diagnosis}, batch_dir=batch_dir)
        if not any(item.get("batch") == str(batch_dir)
                   for item in case.get("batches", [])):
            case.setdefault("batches", []).append({
                "batch": str(batch_dir), "dataset_id": prereg["dataset_id"],
                "hypothesis": prereg["proposal"]["hypothesis"],
                "mechanism_tags": prereg["proposal"].get("mechanism_tags", []),
                "field_generation_tags": prereg["proposal"].get("field_generation_tags", []),
                "expressions": [item["expression"] for item in prereg["variants"]],
                "alpha_ids": alpha_ids,
                "failure_signatures": sorted({signature for row in diagnosis["alphas"].values()
                                               for signature in row["signatures"]}),
                "statuses": {alpha_id: row["status"] for alpha_id, row in gate.items()}})
        atomic_write_json(case_path, case)
        result = {"dataset_id": prereg["dataset_id"], "alpha_ids": alpha_ids,
                  "readback": readback, "attribution": attribution,
                  "gate": gate, "case_path": str(case_path), "scope": scope,
                  "batch_dir": str(batch_dir),
                  "candidate_status": (
                      "CANDIDATE" if any(row["status"] == "CANDIDATE"
                                         for row in gate.values()) else "PENDING_FULL_GATE")}
        context = json.loads((batch_dir / "batch_context.json").read_text(encoding="utf-8"))
        started_at = context.get("started_at")
        try:
            duration_s = max(0, (datetime.now().astimezone() -
                                  datetime.fromisoformat(started_at)).total_seconds())
        except (TypeError, ValueError):
            duration_s = None
        raw_paths = list(batch_dir.glob("probe_*_raw.json")) + [
            batch_dir / "simulation_raw.json"]
        probe_count = len((prereg.get("probe_plan") or {}).get("probes") or [])
        metering = {"started_at": started_at,
                    "finished_at": datetime.now().astimezone().isoformat(),
                    "wall_duration_s": duration_s,
                    "submission_intents": len(list(batch_dir.glob("probe_*_intent.json"))) + 1,
                    "platform_batches": sum(path.exists() for path in raw_paths),
                    "alpha_expressions": len(alpha_ids) + probe_count,
                    "production_expressions": len(alpha_ids),
                    "diagnostic_expressions": probe_count,
                    "retrievals": len(list(batch_dir.glob("*_recovery.json"))) +
                                  int((batch_dir / "recovery.json").exists()),
                    "model_tokens": None}
        atomic_write_json(batch_dir / "metering.json", metering)
        atomic_write_json(batch_dir / "results.json", result)
        atomic_write_json(batch_dir / "simulation_intent.json", {
            "status": "READ_BACK", "prereg_sha256": digest,
            "alpha_ids": alpha_ids, "settings": simulation_settings})
        self._finalize_remedy_outcome(batch_dir, prereg, diagnosis)
        self.campaigns.update(
            case_path.stem, objective=prereg["objective"],
            scope={key: scope[key] for key in ("region", "universe", "delay")},
            current_dataset=prereg["dataset_id"], dataset_state="evaluated",
            current_batch=None, candidate_ledger_ref=str(case_path),
            selection_mode=(prereg.get("dataset_selection") or {}).get("mode"),
            locked=(prereg.get("dataset_selection") or {}).get("mode") == "fixed",
            auth_mode=(self.config.get("auth") or {}).get("mode", "auto"),
            status="batch_complete")
        return result

    def _finalize_remedy_outcome(self, batch_dir, prereg, diagnosis=None):
        """Idempotently record remedy outcomes only after the batch is closed."""
        reference = prereg.get("remedy_reference") or None
        if reference is None:
            return None
        diagnosis_path = Path(batch_dir) / "diagnosis.json"
        if diagnosis is None:
            try:
                diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ResearchError("closed remedy batch has no readable diagnosis") from exc
        if (diagnosis.get("remedy_outcome") or {}).get("status") == "recorded":
            return diagnosis
        try:
            entry = self.knowledge.record_remedy_outcome(reference, diagnosis, batch_dir)
            batch_ref = str(Path(batch_dir).resolve())
            outcomes = [row for row in entry.get("outcomes", [])
                        if isinstance(row, dict)]
            outcome_index = next((index for index, row in enumerate(outcomes)
                                  if row.get("batch") == batch_ref), None)
            outcome = outcomes[outcome_index] if outcome_index is not None else {}
            uses_after = outcome.get("uses_after")
            if uses_after is None and outcome_index is not None:
                uses_after = outcome_index + 1
            successes_after = outcome.get("successes_after")
            if successes_after is None and outcome_index is not None:
                successes_after = sum(bool(row.get("successful"))
                                       for row in outcomes[:outcome_index + 1])
            status_after = outcome.get("status_after")
            if status_after is None and outcome_index == len(outcomes) - 1:
                status_after = entry.get("status")
            diagnosis["remedy_outcome"] = {
                "status": "recorded", "remedy_id": entry.get("remedy_id"),
                "uses": uses_after, "successes": successes_after,
                "remedy_status": status_after}
        except Exception as exc:
            diagnosis["remedy_outcome"] = {
                "status": "unavailable", "error_type": type(exc).__name__,
                "error": str(exc)}
            atomic_write_json(diagnosis_path, diagnosis)
            raise ResearchError("remedy outcome remains pending; recover this closed batch") from exc
        atomic_write_json(diagnosis_path, diagnosis)
        return diagnosis

    @staticmethod
    def _apply_diagnosis_model(diagnosis, response):
        rows = response.get("alphas") if isinstance(response, dict) else None
        if not isinstance(rows, dict):
            raise ResearchError("diagnose_failures response is missing per-Alpha results")
        expected_alphas = {alpha_id for alpha_id, row in diagnosis["alphas"].items()
                           if row.get("signatures")}
        if set(rows) != expected_alphas:
            raise ResearchError("diagnose_failures Alpha set differs from deterministic failures")
        allowed = {"no_known_remedy", "abandon", "mechanism_review", "use_remedy"}
        for alpha_id, item in diagnosis["alphas"].items():
            if not item.get("signatures"):
                continue
            suggestion = rows.get(alpha_id)
            if not isinstance(suggestion, dict):
                raise ResearchError(f"diagnosis suggestion missing for {alpha_id}")
            recommendation = suggestion.get("recommendation")
            if recommendation not in allowed:
                raise ResearchError(f"invalid diagnosis recommendation for {alpha_id}")
            candidate_ids = {row.get("remedy_id")
                             for row in item.get("remedy_candidates", [])}
            remedy_id = suggestion.get("remedy_id")
            if recommendation == "use_remedy" and remedy_id not in candidate_ids:
                raise ResearchError(f"unverified remedy selected for {alpha_id}")
            if recommendation == "no_known_remedy" and candidate_ids:
                raise ResearchError(f"known remedies exist for {alpha_id}")
            if recommendation != "use_remedy" and remedy_id is not None:
                raise ResearchError(f"unexpected remedy reference for {alpha_id}")
            rationale = suggestion.get("rationale")
            refs = suggestion.get("evidence_refs")
            allowed_refs = {"preregistration", "readback", "gate", "diagnosis_signatures"}
            allowed_refs.update(f"remedy:{key}" for key in candidate_ids)
            if not isinstance(rationale, str) or not rationale.strip():
                raise ResearchError(f"diagnosis rationale missing for {alpha_id}")
            if (not isinstance(refs, list) or not refs or
                    any(ref not in allowed_refs for ref in refs)):
                raise ResearchError(f"diagnosis evidence references invalid for {alpha_id}")
            item["recommendation"] = recommendation
            item["remedy_id"] = remedy_id
            item["model_rationale"] = rationale
            item["evidence_refs"] = refs
        diagnosis["model_diagnosis"] = {"status": "complete", "result": response}

    def recover(self, batch_dir: Path) -> dict:
        self._begin_session()
        self._assert_gate_config()
        self._assert_auth_mode()
        batch_dir = Path(batch_dir).resolve()
        if not batch_dir.is_relative_to(self.runs_root.resolve()):
            raise ResearchError("batch path outside research runs")
        production_intent = batch_dir / "simulation_intent.json"
        if not production_intent.exists():
            return self._recover_probes(batch_dir)
        intent = json.loads(production_intent.read_text(encoding="utf-8"))
        prereg_path = batch_dir / "prereg.json"
        state = json.loads((batch_dir / "state.json").read_text(encoding="utf-8"))
        if not verify_prereg(prereg_path, state):
            raise ResearchError("pre-registration hash mismatch; recovery blocked")
        if intent.get("status") == "READ_BACK":
            result_path = batch_dir / "results.json"
            if not result_path.exists():
                raise ResearchError("READ_BACK intent has no saved results; recovery blocked")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
            simulation_settings = (intent.get("settings") or
                                   prereg.get("simulation_settings") or
                                   self._platform_settings(prereg["scope"]))
            self._record_completed_expressions(
                [item["expression"] for item in prereg["variants"]],
                prereg["dataset_id"], prereg["scope"], simulation_settings)
            case_path = self._case_path(prereg["objective"], prereg["scope"])
            result.setdefault("batch_dir", str(batch_dir))
            self._finalize_remedy_outcome(batch_dir, prereg)
            self.campaigns.update(
                case_path.stem, objective=prereg["objective"],
                scope={key: prereg["scope"][key] for key in ("region", "universe", "delay")},
                current_dataset=prereg["dataset_id"], dataset_state="evaluated",
                current_batch=None, candidate_ledger_ref=str(case_path),
                selection_mode=(prereg.get("dataset_selection") or {}).get("mode"),
                locked=(prereg.get("dataset_selection") or {}).get("mode") == "fixed",
                auth_mode=(self.config.get("auth") or {}).get("mode", "auto"),
                status="batch_complete")
            return result
        expressions = intent["expressions"]
        raw_path = batch_dir / "simulation_raw.json"
        if raw_path.exists():
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            rows = raw.get("alpha_results") if isinstance(raw, dict) else None
            if len(expressions) == 1 and isinstance(raw, dict) and raw.get("alpha_id"):
                rows = [raw]
            if isinstance(rows, list) and len(rows) == len(expressions):
                alpha_ids = [row.get("alpha_id") for row in rows
                             if isinstance(row, dict) and row.get("alpha_id")]
                if len(alpha_ids) == len(expressions) and len(set(alpha_ids)) == len(alpha_ids):
                    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
                    self._record_completed_expressions(
                        expressions, prereg["dataset_id"], prereg["scope"],
                        intent["settings"])
                    return self._complete(batch_dir, prereg, alpha_ids,
                                          intent["prereg_sha256"],
                                          intent.get("settings"))
        recent = self._recent_alphas()
        matched, missing, match_notes = match_alphas_detailed(
            expressions, intent["settings"], recent)
        atomic_write_json(batch_dir / "recovery.json", {
            "matched": {expr: row.get("id") for expr, row in matched.items()},
            "missing": missing,
            "settings_missing_keys": match_notes})
        if missing:
            raise ResearchError("platform reconciliation incomplete; no simulation reposted")
        alpha_ids = [matched[expr]["id"] for expr in expressions]
        prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
        self._record_completed_expressions(
            expressions, prereg["dataset_id"], prereg["scope"],
            intent["settings"])
        return self._complete(batch_dir, prereg, alpha_ids, intent["prereg_sha256"],
                              intent.get("settings"))

    def _recent_alphas(self):
        recent = []
        for offset in range(0, 20000, 200):
            page = _rows(self.gateway.call("get_user_alphas", stage="IS",
                                           limit=200, offset=offset))
            recent.extend(page)
            if len(page) < 200:
                return recent
        raise ResearchError("account listing incomplete; recovery remains pending")

    def _recover_probes(self, batch_dir):
        context_path = batch_dir / "batch_context.json"
        plan_path = batch_dir / "probe_plan.json"
        if not context_path.exists():
            raise ResearchError("probe recovery context is incomplete")
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if not plan_path.exists():
            plan = self._decide("plan_probes", {
                "objective": context["objective"], "scope": context["scope"],
                "dataset_id": context["dataset_id"], "fields": context["fields"]},
                batch_dir=batch_dir)
            atomic_write_json(plan_path, plan)
        else:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        probes = plan.get("probes")
        if not isinstance(probes, list):
            raise ResearchError("probe plan is unreadable")
        results = []
        recent = None
        for offset in range(0, len(probes), 8):
            group = probes[offset:offset + 8]
            label = f"probe_{offset // 8 + 1}"
            intent_path = batch_dir / f"{label}_intent.json"
            if not intent_path.exists():
                break  # no POST for this group: resume from this point
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            expressions = [probe["expression"] for probe in group]
            if intent.get("expressions") != expressions:
                raise ResearchError("diagnostic intent differs from saved plan")
            expected_settings = self._platform_settings(context["scope"], probe=True)
            if intent.get("settings") != expected_settings:
                raise ResearchError("diagnostic settings changed; recovery blocked")
            raw_path = batch_dir / f"{label}_raw.json"
            raw = (json.loads(raw_path.read_text(encoding="utf-8"))
                   if raw_path.exists() else None)
            rows = raw.get("alpha_results") if isinstance(raw, dict) else None
            if len(group) == 1 and isinstance(raw, dict) and raw.get("alpha_id"):
                rows = [raw]
            if not isinstance(rows, list) or len(rows) != len(group):
                if recent is None:
                    recent = self._recent_alphas()
                matched, missing, match_notes = match_alphas_detailed(
                    expressions, expected_settings, recent)
                atomic_write_json(batch_dir / f"{label}_recovery.json", {
                    "matched": {expr: row.get("id") for expr, row in matched.items()},
                    "missing": missing,
                    "settings_missing_keys": match_notes})
                if missing:
                    raise ResearchError("diagnostic reconciliation incomplete; no repost")
                rows = [{"alpha_id": matched[expr]["id"],
                         "details": self.gateway.call(
                             "get_alpha_details", alpha_id=matched[expr]["id"]),
                         "recovered": True} for expr in expressions]
                atomic_write_json(raw_path, {"alpha_results": rows})
            alpha_ids = [row.get("alpha_id") for row in rows if isinstance(row, dict)
                         and isinstance(row.get("alpha_id"), str) and row["alpha_id"]]
            if len(alpha_ids) != len(group) or len(set(alpha_ids)) != len(alpha_ids):
                raise ResearchError("diagnostic reconciliation returned invalid Alpha IDs")
            results.extend({"probe": probe, "simulation": row}
                           for probe, row in zip(group, rows))
            self._record_probe_batch(context, expressions, rows,
                                     expected_settings)
            atomic_write_json(intent_path, {
                "status": "READ_BACK", "expressions": expressions,
                "settings": expected_settings})
        return self.run_once(context["objective"], resume_batch_dir=batch_dir,
                             recovered_probe_results=results, cached_context=context)

    def run_once(self, objective: str, dataset_id: str = None, *,
                 resume_batch_dir: Path = None, recovered_probe_results=None,
                 cached_context=None, fixed_scope=None, diagnosis_id=None,
                 remedy_id=None, allow_remedy_round_overrun=False) -> dict:
        self._begin_session()
        if not objective or not objective.strip():
            raise ResearchError("research objective is required")
        remedy_reference = self._resolve_remedy_reference(
            objective, diagnosis_id, remedy_id, allow_remedy_round_overrun)
        if remedy_reference is not None:
            if dataset_id is not None and dataset_id != remedy_reference["dataset_id"]:
                raise ResearchError("remedy must keep the diagnosis Dataset locked")
            dataset_id = remedy_reference["dataset_id"]
        self._assert_gate_config()
        self._assert_auth_mode()
        if resume_batch_dir is None:
            supplied_dataset = (self.config.get("scope") or {}).get("dataset_id")
            if supplied_dataset and dataset_id and dataset_id != supplied_dataset:
                raise ResearchError("requested Dataset conflicts with configured scope")
            requested_dataset = dataset_id
            dataset_id = supplied_dataset or dataset_id
            unfinished = self._unfinished_batch()
            if unfinished:
                raise ResearchError(f"unfinished batch requires recovery: {unfinished}")
            scope = self._scope(objective, fixed_scope)
            if remedy_reference is not None and any(
                    scope.get(key) != value for key, value in
                    remedy_reference["scope"].items() if value is not None):
                raise ResearchError("remedy must keep the diagnosis Region/Delay/Universe locked")
            prior_case = self._read_case(objective, scope)
            case_path = self._case_path(objective, scope)
            campaign_id = case_path.stem
            self.campaigns.update(
                campaign_id, objective=objective,
                scope={key: scope[key] for key in ("region", "universe", "delay")},
                current_dataset=dataset_id, dataset_state="selecting",
                selection_mode="fixed" if dataset_id else "autonomous",
                locked=bool(dataset_id), current_batch=None,
                candidate_ledger_ref=str(case_path),
                auth_mode=(self.config.get("auth") or {}).get("mode", "auto"),
                status="active")
            context = {k: scope[k] for k in ("region", "universe", "delay")}
            datasets = _rows(self.gateway.call("get_datasets", instrument_type="EQUITY", **context))
            available = {row.get("id") for row in datasets if isinstance(row, dict)}
            if dataset_id is None:
                selection = self._decide("select_dataset", {
                    "objective": objective, "scope": scope, "datasets": datasets,
                    "prior_batches": prior_case.get("batches", [])})
                dataset_id = selection.get("dataset_id")
                if not isinstance(selection.get("reason"), str) or not selection["reason"].strip():
                    raise ResearchError("autonomous Dataset choice needs an economic reason")
                dataset_selection = {"mode": "autonomous", "reason": selection["reason"]}
            else:
                dataset_selection = {"mode": "fixed", "reason": (
                    f"remedy reference {diagnosis_id}/{remedy_id}" if remedy_reference else
                    "configured Dataset" if supplied_dataset else
                    "Dataset specified for this run" if requested_dataset else
                    "fixed Dataset")}
            if dataset_id not in available:
                raise ResearchError(f"Dataset not available in exact scope: {dataset_id}")
            self.campaigns.update(
                campaign_id, current_dataset=dataset_id, dataset_state="selected",
                selection_mode=dataset_selection["mode"],
                locked=dataset_selection["mode"] == "fixed",
                candidate_ledger_ref=str(case_path), status="active")
            fields = _rows(self.gateway.call("get_datafields", instrument_type="EQUITY",
                                             dataset_id=dataset_id, **context))
            operator_rows = _rows(self.gateway.call("get_operators"))
            operators = {row["name"] for row in operator_rows
                         if isinstance(row, dict) and row.get("name")}
            operator_catalog = operator_rows
            if not operators:
                raise ResearchError("operator catalog unavailable")
            for value in (scope["region"], scope["universe"], dataset_id):
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                    raise ResearchError("unsafe scope or Dataset path component")
            batch_dir = (self.runs_root / scope["region"] / f"D{scope['delay']}"
                         / scope["universe"] / dataset_id
                         / (datetime.now().strftime("%Y%m%d_%H%M%S")
                            + "_" + uuid4().hex[:8]))
            batch_dir.mkdir(parents=True)
            atomic_write_json(batch_dir / "batch_context.json", {
                "objective": objective, "scope": scope, "dataset_id": dataset_id,
                "dataset_selection": dataset_selection,
                "fields": fields, "operators": sorted(operators),
                "operator_catalog": operator_rows,
                "started_at": datetime.now().astimezone().isoformat()})
            self.recorder.bind_batch(batch_dir)
            self._write_batch_knowledge_packs(batch_dir)
            self.campaigns.update(campaign_id, current_batch=str(batch_dir),
                                  dataset_state="active", status="active")
            probe_plan = self._decide("plan_probes", {
                "objective": objective, "scope": scope, "dataset_id": dataset_id,
                "fields": fields, "prior_batches": prior_case.get("batches", [])},
                batch_dir=batch_dir)
        else:
            batch_dir = Path(resume_batch_dir).resolve()
            if not batch_dir.is_relative_to(self.runs_root.resolve()):
                raise ResearchError("resume batch path outside research runs")
            if not isinstance(cached_context, dict) or recovered_probe_results is None:
                raise ResearchError("recovery context is incomplete")
            scope = cached_context["scope"]
            dataset_id = cached_context["dataset_id"]
            dataset_selection = cached_context["dataset_selection"]
            fields = cached_context["fields"]
            operators = set(cached_context["operators"])
            operator_catalog = cached_context.get("operator_catalog", [])
            probe_plan = json.loads((batch_dir / "probe_plan.json").read_text(
                encoding="utf-8"))
            prior_case = self._read_case(objective, scope)
            self.recorder.bind_batch(batch_dir)
            self._write_batch_knowledge_packs(batch_dir)
            campaign_id = self._case_path(objective, scope).stem
            self.campaigns.update(
                campaign_id, objective=objective,
                scope={key: scope[key] for key in ("region", "universe", "delay")},
                current_dataset=dataset_id, dataset_state="active",
                selection_mode=dataset_selection.get("mode"),
                locked=dataset_selection.get("mode") == "fixed",
                current_batch=str(batch_dir),
                candidate_ledger_ref=str(self._case_path(objective, scope)),
                auth_mode=(self.config.get("auth") or {}).get("mode", "auto"),
                status="active")
        catalog = _catalog(fields, dataset_id)
        probes = probe_plan.get("probes")
        if not isinstance(probes, list):
            raise ResearchError("model probe plan must contain a list")
        if probes and "NONE" not in scope.get("_supported_neutralizations", []):
            raise ResearchError("diagnostic neutralization NONE is not live supported")
        core_fields = probe_plan.get("core_fields")
        if (not isinstance(core_fields, list) or not 1 <= len(core_fields) <= 4
                or len(set(core_fields)) != len(core_fields)
                or any(field_id not in catalog for field_id in core_fields)):
            raise ResearchError("probe plan must choose one to four live core fields")
        required_dimensions = probe_plan.get("required_dimensions")
        if required_dimensions is None:
            required_dimensions = {field_id: list(FIELD_EVIDENCE_KEYS)
                                   for field_id in core_fields}
        elif (not isinstance(required_dimensions, dict)
              or set(required_dimensions) != set(core_fields)
              or not isinstance(probe_plan.get("requirement_reason"), str)
              or not probe_plan["requirement_reason"].strip()
              or any(not isinstance(items, list) or not items
                     or len(items) != len(set(items))
                     or any(item not in FIELD_EVIDENCE_KEYS for item in items)
                     or "nominal_coverage" not in items
                     or "usable_observations" not in items
                     for items in required_dimensions.values())):
            raise ResearchError("required field diagnostics need a justified dimension list")
        field_rows = {row["id"]: row for row in fields
                      if isinstance(row, dict) and row.get("id") in core_fields}
        atomic_write_json(batch_dir / "probe_plan.json", probe_plan)
        probe_results = [] if recovered_probe_results is None else recovered_probe_results
        if probes and len(probe_results) < len(probes):
            for probe in probes:
                if not isinstance(probe, dict) or not all(probe.get(k) for k in
                    ("field_id", "expression", "question", "measures", "economic_relevance",
                     "expected", "failure_criterion")):
                    raise ResearchError("each diagnostic probe needs a question and ex-ante criterion")
                if (probe["field_id"] not in core_fields or not isinstance(probe["measures"], list)
                        or any(item not in FIELD_EVIDENCE_KEYS for item in probe["measures"])):
                    raise ResearchError("probe field or measured property is outside the admitted pool")
                verdict = validate(probe["expression"], dataset_id, catalog, operators)
                if verdict["verdict"] != "PASS":
                    raise ResearchError(f"probe expression rejected: {verdict['errors']}")
            probe_settings = self._platform_settings(scope, probe=True)
            if len(probe_results) % 8:
                raise ResearchError("recovered diagnostic prefix is incomplete")
            self._ensure_expressions_uncached(
                [probe["expression"] for probe in probes[len(probe_results):]],
                dataset_id, scope,
                probe_settings, batch_dir, label="diagnostic probes")
            for offset in range(len(probe_results), len(probes), 8):
                group = probes[offset:offset + 8]
                expressions = [probe["expression"] for probe in group]
                label = f"probe_{offset // 8 + 1}"
                atomic_write_json(batch_dir / f"{label}_intent.json", {
                    "status": "SUBMITTING", "expressions": expressions,
                    "settings": self._platform_settings(scope, probe=True)})
                raw_probe = self._simulate(expressions,
                                           self._platform_settings(scope, probe=True))
                atomic_write_json(batch_dir / f"{label}_raw.json", raw_probe)
                if not isinstance(raw_probe, dict) or raw_probe.get("error"):
                    raise ResearchError("diagnostic simulation unresolved; do not resubmit")
                rows = raw_probe.get("alpha_results") or []
                if len(group) == 1 and raw_probe.get("alpha_id"):
                    rows = [raw_probe]
                if len(rows) != len(group):
                    raise ResearchError("diagnostic children unresolved; do not resubmit")
                alpha_ids = [row.get("alpha_id") for row in rows if isinstance(row, dict)
                             and isinstance(row.get("alpha_id"), str)
                             and row["alpha_id"]]
                if len(alpha_ids) != len(group) or len(set(alpha_ids)) != len(alpha_ids):
                    raise ResearchError("diagnostic children unresolved; do not mark READ_BACK")
                probe_results.extend({"probe": probe, "simulation": row}
                                     for probe, row in zip(group, rows))
                self._record_probe_batch({
                    "dataset_id": dataset_id, "scope": scope}, expressions, rows,
                    probe_settings)
                atomic_write_json(batch_dir / f"{label}_intent.json", {
                    "status": "READ_BACK", "expressions": expressions,
                    "settings": probe_settings})
        sources = {field_id: {dimension: set() for dimension in FIELD_EVIDENCE_KEYS}
                   for field_id in core_fields}
        probe_observations = []
        probe_reports = []
        for field_id in core_fields:
            for dimension, key in FIELD_EVIDENCE_KEYS.items():
                if field_rows[field_id].get(key) is not None:
                    sources[field_id][dimension].add("catalog")
        for index, item in enumerate(probe_results):
            simulation = item["simulation"]
            if not isinstance(simulation, dict) or not simulation.get("alpha_id"):
                continue
            probe = item["probe"]
            probe_source = (batch_dir / f"probe_{index // 8 + 1}_raw.json").resolve()
            report = {
                "probe_id": f"probe:{index}",
                "field_id": probe.get("field_id"),
                "field_type": catalog.get(probe.get("field_id"), {}).get("type"),
                "measures": probe.get("measures", []),
                "expression": probe.get("expression"),
                "question": probe.get("question"),
                "economic_relevance": probe.get("economic_relevance"),
                "expected": probe.get("expected"),
                "failure_criterion": probe.get("failure_criterion"),
                "alpha_id": simulation.get("alpha_id"),
                "source": f"{probe_source}#alpha_id={simulation.get('alpha_id')}",
                "simulation": {"alpha_id": simulation.get("alpha_id"),
                               "details": simulation.get("details")},
            }
            probe_reports.append(report)
            details = simulation.get("details")
            is_data = details.get("is") if isinstance(details, dict) else None
            counts = None
            if isinstance(is_data, dict):
                long_count, short_count = (is_data.get("longCount"),
                                           is_data.get("shortCount"))
                if (all(isinstance(value, (int, float)) and not isinstance(value, bool)
                        and value >= 0 for value in (long_count, short_count))):
                    counts = {"long_count": long_count, "short_count": short_count}
            for dimension in probe["measures"]:
                if counts is None:
                    continue
                if dimension == "nominal_coverage" or (
                        dimension == "usable_observations" and
                        re.search(r"(?:!=|==)\s*0\b", probe["expression"])):
                    sources[probe["field_id"]][dimension].add(f"probe:{index}")
                    observation = {"source": report["source"],
                                   "probe_id": report["probe_id"],
                                   "field_id": probe["field_id"],
                                   "dimension": dimension,
                                   "observation": counts,
                                   "limit": "stock counts are approximate and depend on handling"}
                    probe_observations.append(observation)
                    report.setdefault("observations", []).append(observation)
        atomic_write_json(batch_dir / "probe_evidence.json", {
            "schema_version": 1, "dataset_id": dataset_id,
            "scope": {key: scope.get(key) for key in ("region", "delay", "universe")},
            "observations": probe_reports})
        missing = [(field_id, dimension) for field_id in core_fields
                   for dimension in required_dimensions[field_id]
                   if not sources[field_id][dimension]]
        if missing:
            raise ResearchError(f"field diagnostics unresolved; batch construction blocked: {missing}")

        construction_context = {
            "objective": objective, "scope": scope, "dataset_id": dataset_id,
            "fields": fields, "probe_plan": probe_plan,
            "core_fields": core_fields,
            "operator_catalog": operator_catalog,
            "remedy_reference": remedy_reference,
            "prior_batches": prior_case.get("batches", []),
            "probe_results": probe_results, "probe_observations": probe_observations,
            "available_evidence": {
                field_id: {dimension: sorted(values) for dimension, values in dimensions.items()}
                for field_id, dimensions in sources.items()}}
        proposal = self._decide("construct_batch", construction_context,
                                batch_dir=batch_dir)
        proposed_expressions = [row.get("expression") for row in proposal.get("variants", [])
                                if isinstance(row, dict) and isinstance(row.get("expression"), str)]
        used_operators = sorted({name for expression in proposed_expressions
                                 for name in re.findall(
                                     r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression)})
        draft_pack = self.knowledge.build_pack("construct_batch", {
            **construction_context, "proposal": proposal,
            "hypothesis": proposal.get("hypothesis", ""),
            "operator_names": used_operators})
        exact_operator_notes = draft_pack["l2_operator_notes"]
        related_evidence = draft_pack["l3_evidence"]
        if exact_operator_notes or related_evidence:
            proposal = self._decide("construct_batch", {
                **construction_context, "draft_proposal": proposal,
                "proposal": proposal, "operator_names": used_operators,
                "exact_operator_notes": exact_operator_notes,
                "related_evidence": related_evidence},
                batch_dir=batch_dir)
        if remedy_reference is not None:
            proposed_tags = proposal.get("mechanism_tags")
            if isinstance(proposed_tags, str):
                proposed_tags = [proposed_tags]
            if (not isinstance(proposed_tags, list) or
                    any(not isinstance(item, str) for item in proposed_tags) or
                    sorted(proposed_tags) != sorted(remedy_reference["mechanism_tags"])):
                raise ResearchError("remedy batch must preserve the diagnosed mechanism tags")
            for variant in proposal.get("variants") or []:
                reason = variant.get("reason", "") if isinstance(variant, dict) else ""
                if (not isinstance(reason, str) or remedy_id not in reason or
                        diagnosis_id not in reason):
                    raise ResearchError("each remedy variant must cite diagnosis_id and remedy_id")
        variants = proposal.get("variants")
        if not isinstance(variants, list) or not 1 <= len(variants) <= 8:
            raise ResearchError("batch must contain one to eight variants")
        if not all(isinstance(proposal.get(key), str) and proposal[key].strip()
                   for key in ("hypothesis", "expected_sign",
                               "structure_window_rationale", "failure_criterion")):
            raise ResearchError("pre-backtest mechanism, direction, field constraints and failure criterion are required")
        reports = proposal.get("field_report")
        if not isinstance(reports, list):
            raise ResearchError("structured field report is required")
        report_by_field = {row.get("field_id"): row for row in reports
                           if isinstance(row, dict) and row.get("field_id")}
        expressions = []
        operator_counts = {}
        validation_results = {}
        for variant in variants:
            expr = variant.get("expression") if isinstance(variant, dict) else None
            if not isinstance(expr, str) or not variant.get("reason"):
                raise ResearchError("each variant needs an expression and ex-ante reason")
            verdict = validate(expr, dataset_id, catalog, operators)
            if verdict["verdict"] != "PASS":
                raise ResearchError(f"expression rejected: {verdict['errors']}")
            validation_results[expr] = verdict
            for field_id in verdict["fields"]:
                row = report_by_field.get(field_id)
                if field_id not in core_fields or not row or row.get("verdict") != "ADMIT":
                    raise ResearchError(f"field was not admitted by diagnostic report: {field_id}")
                if not isinstance(row.get("constraints"), list) or not row["constraints"]:
                    raise ResearchError(f"field constraints missing: {field_id}")
                evidence = row.get("evidence") or {}
                unresolved = row.get("unresolved") or {}
                for dimension, available in sources[field_id].items():
                    item = evidence.get(dimension)
                    if item is not None:
                        if (not isinstance(item, dict) or item.get("source") not in available
                                or not item.get("observation")):
                            raise ResearchError(f"unsupported field observation: {field_id}/{dimension}")
                    elif dimension in required_dimensions[field_id]:
                        raise ResearchError(f"required field observation missing: {field_id}/{dimension}")
                    else:
                        gap = unresolved.get(dimension) if isinstance(unresolved, dict) else None
                        if (not isinstance(gap, dict) or not gap.get("reason")
                                or not gap.get("why_not_material")):
                            raise ResearchError(f"unresolved field gap not explained: {field_id}/{dimension}")
            expressions.append(expr)
            operator_counts[expr] = verdict["operator_count"]
        if len(set(expressions)) != len(expressions):
            reason = "duplicate expressions in batch"
            self.void_batch(batch_dir, reason)
            raise ResearchError(reason)
        prior_expressions = {expression for item in prior_case.get("batches", [])
                             if item.get("dataset_id") == dataset_id
                             for expression in item.get("expressions", [])}
        if prior_expressions.intersection(expressions):
            reason = "expression already tested in this case and Dataset"
            self.void_batch(batch_dir, reason)
            raise ResearchError(reason)
        simulation_settings = self._platform_settings(scope)
        self._ensure_expressions_uncached(
            expressions, dataset_id, scope, simulation_settings,
            batch_dir, label="production simulation")

        mechanism_tags = proposal.get("mechanism_tags")
        if isinstance(mechanism_tags, str):
            mechanism_tags = [mechanism_tags]
        if not isinstance(mechanism_tags, list) or not any(
                isinstance(item, str) and item.strip() for item in mechanism_tags):
            mechanism_tags = [re.sub(r"\s+", " ", proposal["hypothesis"]).strip()[:100]]
        generation_tags = proposal.get("field_generation_tags")
        if isinstance(generation_tags, str):
            generation_tags = [generation_tags]
        if not isinstance(generation_tags, list) or not any(
                isinstance(item, str) and item.strip() for item in generation_tags):
            generation_tags = sorted({f"{field_id}:{catalog[field_id]['type']}"
                                      for expr in expressions
                                      for field_id in validation_results[expr]["fields"]})
        proposal["mechanism_tags"] = [item.strip() for item in mechanism_tags
                                      if isinstance(item, str) and item.strip()]
        proposal["field_generation_tags"] = [item.strip() for item in generation_tags
                                             if isinstance(item, str) and item.strip()]

        prereg = {"objective": objective, "dataset_id": dataset_id,
                  "dataset_selection": dataset_selection,
                  "scope": scope, "fields": fields, "probe_plan": probe_plan,
                  "proposal": proposal, "variants": variants,
                  "simulation_settings": simulation_settings,
                  "operator_counts": operator_counts,
                  "validation": validation_results,
                  "remedy_reference": ({key: value for key, value in
                                        remedy_reference.items() if key != "remedy"} | {
                                            "remedy": remedy_reference["remedy"]}
                                       if remedy_reference else None)}
        prereg_path = batch_dir / "prereg.json"
        atomic_write_json(prereg_path, prereg)
        state = {}
        digest = lock_prereg(prereg_path, state)
        atomic_write_json(batch_dir / "state.json", state)
        if not verify_prereg(prereg_path, state):
            raise ResearchError("pre-registration changed before simulation")
        atomic_write_json(batch_dir / "simulation_intent.json", {
            "status": "SUBMITTING", "prereg_sha256": digest,
            "expressions": expressions, "settings": simulation_settings})

        raw = self._simulate(expressions, simulation_settings)
        atomic_write_json(batch_dir / "simulation_raw.json", raw)
        if not isinstance(raw, dict) or raw.get("error"):
            raise ResearchError("simulation unresolved; inspect intent and reconcile before retrying")
        alpha_rows = raw.get("alpha_results") or []
        if len(expressions) == 1 and raw.get("alpha_id"):
            alpha_rows = [raw]
        alpha_ids = [row.get("alpha_id") for row in alpha_rows if isinstance(row, dict)
                     and row.get("alpha_id")]
        if len(alpha_ids) != len(expressions) or len(set(alpha_ids)) != len(alpha_ids):
            raise ResearchError("simulation children unresolved; do not resubmit this batch")
        self._record_completed_expressions(expressions, dataset_id, scope,
                                           simulation_settings)
        return self._complete(batch_dir, prereg, alpha_ids, digest,
                              simulation_settings)

    def run_case(self, objective: str, max_batches: int, dataset_id: str = None, *,
                 diagnosis_id=None, remedy_id=None,
                 allow_remedy_round_overrun=False) -> dict:
        """Run a bounded number of serial research batches in one case."""
        if not isinstance(max_batches, int) or isinstance(max_batches, bool) or max_batches < 1:
            raise ResearchError("max_batches must be a positive integer")
        if (diagnosis_id is not None or remedy_id is not None) and max_batches != 1:
            raise ResearchError("a remedy reference admits exactly one new batch per command")
        new_alpha_ids = []
        candidate_datasets = []
        case_path = None
        fixed_scope = None
        for number in range(max_batches):
            result = self.run_once(objective, dataset_id=dataset_id,
                                   fixed_scope=fixed_scope,
                                   diagnosis_id=diagnosis_id, remedy_id=remedy_id,
                                   allow_remedy_round_overrun=allow_remedy_round_overrun)
            diagnosis_id = remedy_id = None
            allow_remedy_round_overrun = False
            if fixed_scope is None:
                fixed_scope = {key: result["scope"][key] for key in
                               ("region", "universe", "delay", "decay",
                                "neutralization", "truncation", "pasteurization",
                                "unit_handling", "nan_handling", "test_period",
                                "max_trade")}
            new_alpha_ids.extend(result["alpha_ids"])
            case_path = Path(result["case_path"])
            case = json.loads(case_path.read_text(encoding="utf-8"))
            dataset_by_batch = {item["batch"]: item["dataset_id"]
                                for item in case.get("batches", [])}
            candidate_datasets = sorted({dataset_by_batch.get(item.get("batch"))
                                         for item in case.get("contenders", {}).values()
                                         if item.get("status") == "CANDIDATE"
                                         and dataset_by_batch.get(item.get("batch"))})
            if len(candidate_datasets) >= 3:
                status = "TARGET_REACHED"
                break
            if number + 1 == max_batches:
                status = "BATCH_LIMIT_REACHED"
                break
            decision = self._decide("decide_next", {
                "objective": objective, "dataset_id": result["dataset_id"],
                "scope": result["scope"], "last_result": {
                    "dataset_id": result["dataset_id"],
                    "gate": result["gate"],
                    "attribution": result["attribution"]},
                "prior_batches": case.get("batches", []),
                "candidate_datasets": candidate_datasets},
                batch_dir=result.get("batch_dir"))
            if (decision.get("action") not in {"continue", "pause"}
                    or not isinstance(decision.get("reason"), str)
                    or not decision["reason"].strip()):
                raise ResearchError("next research decision is incomplete")
            atomic_write_json(case_path.parent / f"{case_path.stem}_decision.json",
                              {"after_batch": case["batches"][-1]["batch"],
                               **decision})
            if decision["action"] == "pause":
                status = "MODEL_PAUSED"
                break
        if case_path is not None:
            self.campaigns.update(
                case_path.stem, status=status.lower(), current_batch=None,
                candidate_ledger_ref=str(case_path), dataset_state=status.lower())
        return {"status": status, "batches_completed": number + 1,
                "new_alpha_ids": new_alpha_ids,
                "candidate_datasets": candidate_datasets,
                "case_path": str(case_path)}
