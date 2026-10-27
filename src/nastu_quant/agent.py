"""One recoverable research batch driven by an interchangeable JSON model.

The model proposes economic decisions. Catalog admission, expression checks,
pre-registration, platform access and evidence persistence remain deterministic.
"""
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import hashlib
import re

from .state_store import atomic_write_json, lock_prereg, verify_prereg
from .validator import validate
from .recovery import match_alphas
from .gatekeeper import evaluate_candidate, parse_correlations
from .config import DEFAULT_CONFIG
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
        self.stop_flag = Path(stop_flag) if stop_flag else None
        if hasattr(gateway, "alpha_lookup"):
            gateway.alpha_lookup = self._lookup_alpha_state

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
            selected = self.model.complete_json("select_scope", {
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
            if not (path.parent / "results.json").exists() and not (
                    path.parent / "void.json").exists():
                return path.parent
        return None

    def void_batch(self, batch_dir: Path, reason: str) -> dict:
        batch_dir = Path(batch_dir).resolve()
        if not batch_dir.is_relative_to(self.runs_root.resolve()):
            raise ResearchError("batch path outside research runs")
        if not reason or not reason.strip():
            raise ResearchError("void reason is required")
        if not (batch_dir / "batch_context.json").exists():
            raise ResearchError("batch context is missing")
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
        return record

    def _complete(self, batch_dir, prereg, alpha_ids, digest):
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
                review = self.model.complete_json("review_candidate", {
                    "objective": prereg["objective"],
                    "preregistration": prereg,
                    "alpha_id": alpha_id, "evidence": evidence,
                    "gate": verdict})
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
        attribution = self.model.complete_json("attribute_results", {
            "objective": prereg["objective"], "hypothesis": proposal["hypothesis"],
            "failure_criterion": proposal["failure_criterion"],
            "variants": prereg["variants"], "readback": readback})
        if not any(item.get("batch") == str(batch_dir)
                   for item in case.get("batches", [])):
            case.setdefault("batches", []).append({
                "batch": str(batch_dir), "dataset_id": prereg["dataset_id"],
                "hypothesis": prereg["proposal"]["hypothesis"],
                "expressions": [item["expression"] for item in prereg["variants"]],
                "alpha_ids": alpha_ids,
                "statuses": {alpha_id: row["status"] for alpha_id, row in gate.items()}})
        atomic_write_json(case_path, case)
        result = {"dataset_id": prereg["dataset_id"], "alpha_ids": alpha_ids,
                  "readback": readback, "attribution": attribution,
                  "gate": gate, "case_path": str(case_path), "scope": scope,
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
            "alpha_ids": alpha_ids})
        return result

    def recover(self, batch_dir: Path) -> dict:
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
            return json.loads((batch_dir / "results.json").read_text(encoding="utf-8"))
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
                    return self._complete(batch_dir, prereg, alpha_ids,
                                          intent["prereg_sha256"])
        recent = self._recent_alphas()
        matched, missing = match_alphas(expressions, intent["settings"], recent)
        atomic_write_json(batch_dir / "recovery.json", {
            "matched": {expr: row.get("id") for expr, row in matched.items()},
            "missing": missing})
        if missing:
            raise ResearchError("platform reconciliation incomplete; no simulation reposted")
        alpha_ids = [matched[expr]["id"] for expr in expressions]
        prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
        return self._complete(batch_dir, prereg, alpha_ids, intent["prereg_sha256"])

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
            plan = self.model.complete_json("plan_probes", {
                "objective": context["objective"], "scope": context["scope"],
                "dataset_id": context["dataset_id"], "fields": context["fields"]})
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
            raw_path = batch_dir / f"{label}_raw.json"
            raw = (json.loads(raw_path.read_text(encoding="utf-8"))
                   if raw_path.exists() else None)
            rows = raw.get("alpha_results") if isinstance(raw, dict) else None
            if len(group) == 1 and isinstance(raw, dict) and raw.get("alpha_id"):
                rows = [raw]
            if not isinstance(rows, list) or len(rows) != len(group):
                if recent is None:
                    recent = self._recent_alphas()
                expected_settings = self._platform_settings(context["scope"], probe=True)
                if intent.get("settings") != expected_settings:
                    raise ResearchError("diagnostic settings changed; recovery blocked")
                matched, missing = match_alphas(expressions, expected_settings, recent)
                atomic_write_json(batch_dir / f"{label}_recovery.json", {
                    "matched": {expr: row.get("id") for expr, row in matched.items()},
                    "missing": missing})
                if missing:
                    raise ResearchError("diagnostic reconciliation incomplete; no repost")
                rows = [{"alpha_id": matched[expr]["id"],
                         "details": self.gateway.call(
                             "get_alpha_details", alpha_id=matched[expr]["id"]),
                         "recovered": True} for expr in expressions]
                atomic_write_json(raw_path, {"alpha_results": rows})
            results.extend({"probe": probe, "simulation": row}
                           for probe, row in zip(group, rows))
            atomic_write_json(intent_path, {
                "status": "READ_BACK", "expressions": expressions,
                "settings": self._platform_settings(context["scope"], probe=True)})
        return self.run_once(context["objective"], resume_batch_dir=batch_dir,
                             recovered_probe_results=results, cached_context=context)

    def run_once(self, objective: str, dataset_id: str = None, *,
                 resume_batch_dir: Path = None, recovered_probe_results=None,
                 cached_context=None, fixed_scope=None) -> dict:
        if not objective or not objective.strip():
            raise ResearchError("research objective is required")
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
            prior_case = self._read_case(objective, scope)
            context = {k: scope[k] for k in ("region", "universe", "delay")}
            datasets = _rows(self.gateway.call("get_datasets", instrument_type="EQUITY", **context))
            available = {row.get("id") for row in datasets if isinstance(row, dict)}
            if dataset_id is None:
                selection = self.model.complete_json("select_dataset", {
                    "objective": objective, "scope": scope, "datasets": datasets,
                    "prior_batches": prior_case.get("batches", [])})
                dataset_id = selection.get("dataset_id")
                if not isinstance(selection.get("reason"), str) or not selection["reason"].strip():
                    raise ResearchError("autonomous Dataset choice needs an economic reason")
                dataset_selection = {"mode": "autonomous", "reason": selection["reason"]}
            else:
                dataset_selection = {"mode": "fixed", "reason": (
                    "configured Dataset" if supplied_dataset else
                    "Dataset specified for this run" if requested_dataset else
                    "fixed Dataset")}
            if dataset_id not in available:
                raise ResearchError(f"Dataset not available in exact scope: {dataset_id}")
            fields = _rows(self.gateway.call("get_datafields", instrument_type="EQUITY",
                                             dataset_id=dataset_id, **context))
            operator_rows = _rows(self.gateway.call("get_operators"))
            operators = {row["name"] for row in operator_rows
                         if isinstance(row, dict) and row.get("name")}
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
                "started_at": datetime.now().astimezone().isoformat()})
            probe_plan = self.model.complete_json("plan_probes", {
                "objective": objective, "scope": scope, "dataset_id": dataset_id,
                "fields": fields, "prior_batches": prior_case.get("batches", [])})
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
            probe_plan = json.loads((batch_dir / "probe_plan.json").read_text(
                encoding="utf-8"))
            prior_case = self._read_case(objective, scope)
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
            if len(probe_results) % 8:
                raise ResearchError("recovered diagnostic prefix is incomplete")
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
                probe_results.extend({"probe": probe, "simulation": row}
                                     for probe, row in zip(group, rows))
                atomic_write_json(batch_dir / f"{label}_intent.json", {
                    "status": "READ_BACK", "expressions": expressions,
                    "settings": self._platform_settings(scope, probe=True)})
        sources = {field_id: {dimension: set() for dimension in FIELD_EVIDENCE_KEYS}
                   for field_id in core_fields}
        probe_observations = []
        for field_id in core_fields:
            for dimension, key in FIELD_EVIDENCE_KEYS.items():
                if field_rows[field_id].get(key) is not None:
                    sources[field_id][dimension].add("catalog")
        for index, item in enumerate(probe_results):
            simulation = item["simulation"]
            if not isinstance(simulation, dict) or not simulation.get("alpha_id"):
                continue
            probe = item["probe"]
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
                    probe_observations.append({"source": f"probe:{index}",
                                               "dimension": dimension,
                                               "observation": counts,
                                               "limit": "stock counts are approximate and depend on handling"})
        missing = [(field_id, dimension) for field_id in core_fields
                   for dimension in required_dimensions[field_id]
                   if not sources[field_id][dimension]]
        if missing:
            raise ResearchError(f"field diagnostics unresolved; batch construction blocked: {missing}")

        proposal = self.model.complete_json("construct_batch", {
            "objective": objective, "scope": scope, "dataset_id": dataset_id,
            "fields": fields, "probe_plan": probe_plan,
            "prior_batches": prior_case.get("batches", []),
            "probe_results": probe_results, "probe_observations": probe_observations,
            "available_evidence": {
                field_id: {dimension: sorted(values) for dimension, values in dimensions.items()}
                for field_id, dimensions in sources.items()}})
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
        for variant in variants:
            expr = variant.get("expression") if isinstance(variant, dict) else None
            if not isinstance(expr, str) or not variant.get("reason"):
                raise ResearchError("each variant needs an expression and ex-ante reason")
            verdict = validate(expr, dataset_id, catalog, operators)
            if verdict["verdict"] != "PASS":
                raise ResearchError(f"expression rejected: {verdict['errors']}")
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
            raise ResearchError("duplicate expressions in batch")
        prior_expressions = {expression for item in prior_case.get("batches", [])
                             if item.get("dataset_id") == dataset_id
                             for expression in item.get("expressions", [])}
        if prior_expressions.intersection(expressions):
            raise ResearchError("expression already tested in this case and Dataset")

        prereg = {"objective": objective, "dataset_id": dataset_id,
                  "dataset_selection": dataset_selection,
                  "scope": scope, "fields": fields, "probe_plan": probe_plan,
                  "proposal": proposal, "variants": variants,
                  "operator_counts": operator_counts}
        prereg_path = batch_dir / "prereg.json"
        atomic_write_json(prereg_path, prereg)
        state = {}
        digest = lock_prereg(prereg_path, state)
        atomic_write_json(batch_dir / "state.json", state)
        if not verify_prereg(prereg_path, state):
            raise ResearchError("pre-registration changed before simulation")
        atomic_write_json(batch_dir / "simulation_intent.json", {
            "status": "SUBMITTING", "prereg_sha256": digest,
            "expressions": expressions, "settings": self._platform_settings(scope)})

        raw = self._simulate(expressions, self._platform_settings(scope))
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
        return self._complete(batch_dir, prereg, alpha_ids, digest)

    def run_case(self, objective: str, max_batches: int, dataset_id: str = None) -> dict:
        """Run a bounded number of serial research batches in one case."""
        if not isinstance(max_batches, int) or isinstance(max_batches, bool) or max_batches < 1:
            raise ResearchError("max_batches must be a positive integer")
        new_alpha_ids = []
        candidate_datasets = []
        case_path = None
        fixed_scope = None
        for number in range(max_batches):
            result = self.run_once(objective, dataset_id=dataset_id,
                                   fixed_scope=fixed_scope)
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
            decision = self.model.complete_json("decide_next", {
                "objective": objective, "last_result": {
                    "dataset_id": result["dataset_id"],
                    "gate": result["gate"],
                    "attribution": result["attribution"]},
                "prior_batches": case.get("batches", []),
                "candidate_datasets": candidate_datasets})
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
        return {"status": status, "batches_completed": number + 1,
                "new_alpha_ids": new_alpha_ids,
                "candidate_datasets": candidate_datasets,
                "case_path": str(case_path)}
