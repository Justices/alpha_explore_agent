"""One recoverable research batch driven by an interchangeable JSON model.

The model proposes economic decisions. Catalog admission, expression checks,
pre-registration, platform access and evidence persistence remain deterministic.
"""
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import re

from .state_store import atomic_write_json, lock_prereg, verify_prereg
from .validator import validate
from .recovery import match_alphas, match_alphas_detailed
from .gatekeeper import evaluate_candidate, parse_correlations
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
        if ds_id not in (None, dataset_id):
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

    def _register_alpha_state(self, alpha_id, state):
        path = self.runs_root / "alpha_states.json"
        states = (json.loads(path.read_text(encoding="utf-8"))
                  if path.exists() else {})
        states[alpha_id] = state
        atomic_write_json(path, states)

    def _scope(self, objective):
        scope = self.config.get("scope") or {}
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
        valid = any(row.get("InstrumentType") == "EQUITY"
                    and row.get("Region") == effective["region"]
                    and row.get("Delay") == effective.get("delay")
                    and effective.get("universe") in (row.get("Universe") or [])
                    and effective.get("neutralization") in (row.get("Neutralization") or [])
                    for row in combos if isinstance(row, dict))
        if not valid:
            raise ResearchError("effective scope is not a live supported combination")
        for key in ("decay", "truncation", "pasteurization"):
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
            "unit_handling": scope.get("unit_handling", "VERIFY"),
            "nan_handling": scope.get("nan_handling", "OFF"),
            "language": "FASTEXPR", "visualization": False,
            "test_period": scope.get("test_period", "P0Y0M"),
            "max_trade": scope.get("max_trade", "OFF"),
        }

    def _simulate(self, expressions, settings):
        if self.stop_flag and self.stop_flag.exists():
            raise ResearchError("stop flag is set; no new simulation posted")
        if len(expressions) == 1:
            return self.gateway.call("create_simulation", regular=expressions[0],
                                     **settings)
        return self.gateway.call("create_multi_simulation",
                                 alpha_expressions=expressions, **settings)

    def _unfinished_batch(self):
        if not self.runs_root.exists():
            return None
        for path in self.runs_root.rglob("simulation_intent.json"):
            intent = json.loads(path.read_text(encoding="utf-8"))
            if intent.get("status") != "READ_BACK":
                return path.parent
        return None

    def _complete(self, batch_dir, prereg, alpha_ids, digest):
        readback = {}
        for alpha_id in alpha_ids:
            readback[alpha_id] = {
                "details": self.gateway.call("get_alpha_details", alpha_id=alpha_id),
                "checks": self.gateway.call("get_submission_check", alpha_id=alpha_id),
            }
        gate = {}
        for alpha_id, evidence in readback.items():
            preliminary = evaluate_candidate(
                evidence["details"], evidence["checks"],
                prereg["scope"]["region"], self.config)
            correlations = None
            if preliminary["correlation_allowed"]:
                self._register_alpha_state(alpha_id, {
                    "state": "RA_PASSED", "ra_fail_count": 0,
                    "ra_readable": True})
                raw_correlation = self.gateway.call("check_correlation",
                                                    alpha_id=alpha_id)
                correlations = parse_correlations(raw_correlation)
                evidence["correlation_raw"] = raw_correlation
            verdict = evaluate_candidate(
                evidence["details"], evidence["checks"],
                prereg["scope"]["region"], self.config,
                correlations=correlations, expected_pairwise_ids=[])
            verdict["correlations"] = correlations
            gate[alpha_id] = verdict
        atomic_write_json(batch_dir / "gate.json", gate)
        proposal = prereg["proposal"]
        attribution = self.model.complete_json("attribute_results", {
            "objective": prereg["objective"], "hypothesis": proposal["hypothesis"],
            "failure_criterion": proposal["failure_criterion"],
            "variants": prereg["variants"], "readback": readback})
        result = {"dataset_id": prereg["dataset_id"], "alpha_ids": alpha_ids,
                  "readback": readback, "attribution": attribution,
                  "gate": gate, "candidate_status": "PENDING_FULL_GATE"}
        atomic_write_json(batch_dir / "results.json", result)
        atomic_write_json(batch_dir / "simulation_intent.json", {
            "status": "READ_BACK", "prereg_sha256": digest,
            "alpha_ids": alpha_ids})
        return result

    def recover(self, batch_dir: Path) -> dict:
        batch_dir = Path(batch_dir).resolve()
        if not batch_dir.is_relative_to(self.runs_root.resolve()):
            raise ResearchError("batch path outside research runs")
        intent = json.loads((batch_dir / "simulation_intent.json").read_text(
            encoding="utf-8"))
        prereg_path = batch_dir / "prereg.json"
        state = json.loads((batch_dir / "state.json").read_text(encoding="utf-8"))
        if not verify_prereg(prereg_path, state):
            raise ResearchError("pre-registration hash mismatch; recovery blocked")
        if intent.get("status") == "READ_BACK":
            return json.loads((batch_dir / "results.json").read_text(encoding="utf-8"))
        expressions = intent["expressions"]
        recent = []
        for offset in range(0, 20000, 200):
            page = _rows(self.gateway.call("get_user_alphas", stage="IS",
                                           limit=200, offset=offset))
            recent.extend(page)
            if len(page) < 200:
                break
        else:
            raise ResearchError("account listing incomplete; recovery remains pending")
        matched, missing, match_notes = match_alphas_detailed(
            expressions, intent["settings"], recent)
        atomic_write_json(batch_dir / "recovery.json", {
            "matched": {expr: row.get("id") for expr, row in matched.items()},
            "missing": missing,
            "settings_missing_keys": {
                expr: match_notes[expr] for expr in matched
                if expr in match_notes}})
        if missing:
            raise ResearchError("platform reconciliation incomplete; no simulation reposted")
        alpha_ids = [matched[expr]["id"] for expr in expressions]
        prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
        return self._complete(batch_dir, prereg, alpha_ids, intent["prereg_sha256"])

    def run_once(self, objective: str, dataset_id: str = None) -> dict:
        if not objective or not objective.strip():
            raise ResearchError("research objective is required")
        unfinished = self._unfinished_batch()
        if unfinished:
            raise ResearchError(f"unfinished batch requires recovery: {unfinished}")
        scope = self._scope(objective)
        context = {k: scope[k] for k in ("region", "universe", "delay")}
        datasets = _rows(self.gateway.call("get_datasets", instrument_type="EQUITY", **context))
        available = {row.get("id") for row in datasets if isinstance(row, dict)}
        if dataset_id is None:
            selection = self.model.complete_json("select_dataset", {
                "objective": objective, "scope": scope, "datasets": datasets})
            dataset_id = selection.get("dataset_id")
        if dataset_id not in available:
            raise ResearchError(f"Dataset not available in exact scope: {dataset_id}")

        fields = _rows(self.gateway.call("get_datafields", instrument_type="EQUITY",
                                         dataset_id=dataset_id, **context))
        catalog = _catalog(fields, dataset_id)
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
        probe_plan = self.model.complete_json("plan_probes", {
            "objective": objective, "scope": scope, "dataset_id": dataset_id,
            "fields": fields})
        probes = probe_plan.get("probes")
        if not isinstance(probes, list):
            raise ResearchError("model probe plan must contain a list")
        core_fields = probe_plan.get("core_fields")
        if (not isinstance(core_fields, list) or not 1 <= len(core_fields) <= 4
                or len(set(core_fields)) != len(core_fields)
                or any(field_id not in catalog for field_id in core_fields)):
            raise ResearchError("probe plan must choose one to four live core fields")
        field_rows = {row["id"]: row for row in fields
                      if isinstance(row, dict) and row.get("id") in core_fields}
        atomic_write_json(batch_dir / "probe_plan.json", probe_plan)
        probe_results = []
        if probes:
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
            for offset in range(0, len(probes), 8):
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
                    "status": "READ_BACK", "expressions": expressions})
        sources = {field_id: {dimension: set() for dimension in FIELD_EVIDENCE_KEYS}
                   for field_id in core_fields}
        for field_id in core_fields:
            for dimension, key in FIELD_EVIDENCE_KEYS.items():
                if field_rows[field_id].get(key) is not None:
                    sources[field_id][dimension].add("catalog")
        for index, item in enumerate(probe_results):
            simulation = item["simulation"]
            if not isinstance(simulation, dict) or not simulation.get("alpha_id"):
                continue
            probe = item["probe"]
            for dimension in probe["measures"]:
                sources[probe["field_id"]][dimension].add(f"probe:{index}")
        missing = [(field_id, dimension) for field_id in core_fields
                   for dimension, available in sources[field_id].items() if not available]
        if missing:
            raise ResearchError(f"field diagnostics unresolved; batch construction blocked: {missing}")

        proposal = self.model.complete_json("construct_batch", {
            "objective": objective, "scope": scope, "dataset_id": dataset_id,
            "fields": fields, "probe_plan": probe_plan,
            "probe_results": probe_results, "available_evidence": {
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
                for dimension, available in sources[field_id].items():
                    item = evidence.get(dimension)
                    if (not isinstance(item, dict) or item.get("source") not in available
                            or not item.get("observation")):
                        raise ResearchError(f"unsupported field observation: {field_id}/{dimension}")
            expressions.append(expr)
        if len(set(expressions)) != len(expressions):
            raise ResearchError("duplicate expressions in batch")

        prereg = {"objective": objective, "dataset_id": dataset_id,
                  "scope": scope, "fields": fields, "probe_plan": probe_plan,
                  "proposal": proposal, "variants": variants}
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
        if len(alpha_ids) != len(expressions):
            raise ResearchError("simulation children unresolved; do not resubmit this batch")
        return self._complete(batch_dir, prereg, alpha_ids, digest)
