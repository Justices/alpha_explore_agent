"""Research case ledger, Alpha state and verified remedy provenance.

This module only reads and writes local evidence. It does not own models,
Gateway calls, qualification policy or BRAIN Simulation intents.
"""
import hashlib
import json
from pathlib import Path

from alpha_research.state.store import atomic_write_json
from alpha_research.workflows.regular.errors import ResearchError


class CaseEvidenceStore:
    def __init__(self, runs_root, knowledge=None):
        self.runs_root = Path(runs_root)
        self.knowledge = knowledge

    def lookup_alpha_state(self, alpha_id):
        path = self.runs_root / "alpha_states.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8")).get(alpha_id)

    def register_alpha_state(self, alpha_id, state):
        path = self.runs_root / "alpha_states.json"
        states = (json.loads(path.read_text(encoding="utf-8"))
                  if path.exists() else {})
        states[alpha_id] = state
        atomic_write_json(path, states)

    def case_path(self, objective, scope):
        key = hashlib.sha256(json.dumps({
            "objective": objective, "region": scope["region"],
            "universe": scope["universe"], "delay": scope["delay"]},
            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        return self.runs_root / "cases" / f"{key}.json"

    def resolve_remedy_reference(self, objective, diagnosis_id, remedy_id,
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

    def read_case(self, objective, scope):
        path = self.case_path(objective, scope)
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

