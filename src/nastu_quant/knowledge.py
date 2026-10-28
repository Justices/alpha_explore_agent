"""Deterministic, provenance-first knowledge service for the research agent.

L1 rules come from the current canonical AGENTS contract. Construction advice
is injected as advisory material and can never override that contract. L2 is
an exact lookup only. L3 indexes this agent's closed batch artifacts; it never
queries or caches current platform state.
"""
from collections import Counter
import copy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re

from .paths import expand_path
from .state_store import atomic_write_json, verify_prereg


class KnowledgeError(RuntimeError):
    pass


VALIDITY = {"valid", "void", "falsified", "pending"}
CORE_TAGS = ("dataset_id", "mechanism", "failure_signature", "field_generation")

RULE_MAP = {
    "select_scope": [("canonical", "Research scope and evidence"),
                     ("canonical", "Model and platform responsibilities")],
    "select_dataset": [("canonical", "Research scope and evidence"),
                       ("canonical", "Model and platform responsibilities")],
    "plan_probes": [("canonical", "Research scope and evidence"),
                    ("canonical", "Model and platform responsibilities")],
    "construct_batch": [
        ("canonical", "Model and platform responsibilities"),
        ("canonical", "Research scope and evidence"),
        ("advisory", "## 一、回测设置"),
        ("advisory", "## 二、模型的选择"),
        ("advisory", "## 三、时序窗口与日期的选择依赖"),
        ("advisory", "## 四、Alpha 优化方法论"),
    ],
    "attribute_results": [
        ("canonical", "Model and platform responsibilities"),
        ("canonical", "Research scope and evidence"),
        ("advisory", "## 四、Alpha 优化方法论"),
    ],
    "diagnose_failures": [
        ("canonical", "Research scope and evidence"),
        ("canonical", "Model and platform responsibilities"),
    ],
    "review_candidate": [("canonical", "Research scope and evidence")],
    "decide_next": [("canonical", "Research scope and evidence"),
                    ("canonical", "Model and platform responsibilities")],
}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _document_date(text: str, path: Path) -> str:
    match = re.search(r"20\d{2}-\d{2}-\d{2}", text[:6000])
    return match.group(0) if match else "unknown"


def _matches_preconditions(expected, observed):
    """Match an exact nested subset; missing facts never satisfy a condition."""
    if isinstance(expected, dict):
        return (isinstance(observed, dict) and
                all(key in observed and _matches_preconditions(value, observed[key])
                    for key, value in expected.items()))
    return expected == observed


def _tokens(text: str):
    text = str(text).casefold()
    tokens = re.findall(r"[a-z0-9_]+", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return tokens


def _heading_sections(text: str):
    lines = text.splitlines()
    headings = []
    for i, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append((i, len(match.group(1)), match.group(2).strip()))
    sections = []
    for index, (start, level, title) in enumerate(headings):
        end = len(lines)
        for next_start, next_level, _ in headings[index + 1:]:
            if next_level <= level:
                end = next_start
                break
        sections.append((title, "\n".join(lines[start:end]).strip()))
    return sections


class KnowledgeService:
    def __init__(self, state_root: Path, runs_root: Path,
                 config: dict = None):
        self.state_root = Path(state_root).resolve()
        self.runs_root = Path(runs_root).resolve()
        cfg = config or {}
        self.cfg = cfg.get("knowledge", {})
        self.knowledge_root = self.state_root / "knowledge"
        self.project_root = Path(__file__).resolve().parents[2]
        configured_root = self.cfg.get("brain_flow_root") or os.environ.get("BRAIN_FLOW_HOME")
        self.brain_flow_root = self._resolve_path(configured_root) if configured_root else None
        self.canonical_path = self.project_root / "AGENTS.md"
        self.advisory_path = self._knowledge_path("construction_advisory_path")
        self.prior_knowledge_path = self._knowledge_path(
            "prior_knowledge_path", "RESEARCH_MEMORY.md")
        self.operator_root = self._knowledge_path(
            "operator_root", "knowledge_base", "operator")
        self.field_root = self._knowledge_path(
            "field_root", "knowledge_base", "data_fields")
        self.operator_notes_path = self.knowledge_root / "operator_notes.json"
        self.field_notes_path = self.knowledge_root / "field_notes.json"
        self.remedies_path = self.knowledge_root / "remedies.json"
        self.index_path = self.knowledge_root / "evidence_index.json"
        self.validity_path = self.knowledge_root / "evidence_validity.json"

    def _resolve_path(self, value):
        path = expand_path(value)
        if not path.is_absolute():
            path = self.state_root / path
        return path.resolve()

    def _knowledge_path(self, key, *root_parts):
        configured = self.cfg.get(key)
        if configured:
            return self._resolve_path(configured)
        if self.brain_flow_root is not None and root_parts:
            return self.brain_flow_root.joinpath(*root_parts)
        return None

    def _read_source(self, path: Path | None, *, required: bool):
        if path is None:
            if required:
                raise KnowledgeError(
                    "this project's AGENTS.md is unavailable; restore it in the project root")
            return None
        try:
            raw = path.read_bytes()
        except OSError as exc:
            if required:
                raise KnowledgeError(f"required knowledge source unavailable: {path}") from exc
            return None
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            if required:
                raise KnowledgeError(f"knowledge source is not UTF-8: {path}") from exc
            return None
        return {"text": text, "sha256": _sha256(raw),
                "date": _document_date(text, path), "path": str(path.resolve())}

    def inject_rules(self, task: str):
        selectors = RULE_MAP.get(task)
        if selectors is None:
            raise KnowledgeError(f"no deterministic rule mapping for decision task: {task}")
        canonical = self._read_source(self.canonical_path, required=True)
        advisory = self._read_source(self.advisory_path, required=False)
        sources = {"canonical": (canonical, "normative"),
                   "advisory": (advisory, "advisory_only")}
        fragments = []
        for source_key, requested_title in selectors:
            source, source_kind = sources[source_key]
            if source is None:
                continue
            matches = [(title, body) for title, body in _heading_sections(source["text"])
                       if title == requested_title.lstrip("# ").strip()]
            if not matches:
                raise KnowledgeError(
                    f"mapped knowledge section missing: {source['path']}#{requested_title}")
            for title, body in matches:
                fragments.append({
                    "source": source["path"], "section": title,
                    "date": source["date"], "validity": "valid",
                    "source_kind": source_kind, "sha256": source["sha256"],
                    "content": body,
                })
        return fragments

    def lookup_operator(self, name: str):
        """Exact operator note lookup; broad question guides are not a match."""
        if not isinstance(name, str) or not name:
            return None
        if self.operator_notes_path.exists():
            try:
                notes = json.loads(self.operator_notes_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise KnowledgeError("operator_notes.json is unreadable") from exc
            entry = notes.get(name) if isinstance(notes, dict) else None
            if (isinstance(entry, dict) and entry.get("content") and entry.get("source")
                    and entry.get("validity", "valid") == "valid"):
                return {**entry, "name": name, "validity": entry.get("validity", "valid")}
        operator_json = (sorted(self.operator_root.rglob("*.json"))
                         if self.operator_root and self.operator_root.exists() else [])
        for path in operator_json:
            source = self._read_source(path, required=False)
            if source is None:
                continue
            try:
                payload = json.loads(source["text"])
            except ValueError:
                continue
            rows = payload if isinstance(payload, list) else []
            if isinstance(payload, dict):
                for key in ("operators", "results", "result", "data"):
                    if isinstance(payload.get(key), list):
                        rows = payload[key]
                        break
            for index, row in enumerate(rows):
                if isinstance(row, dict) and row.get("name") == name:
                    content = row.get("description") or row.get("definition")
                    if isinstance(content, str) and content.strip():
                        return {"name": name, "content": content,
                                "source": f"{source['path']}#/{index}",
                                "date": source["date"], "validity": "valid",
                                "source_kind": "operator_reference",
                                "sha256": source["sha256"]}
        # Search exact identifier mentions, not prose about a broad operator family.
        operator_markdown = (sorted(self.operator_root.rglob("*.md"))
                             if self.operator_root and self.operator_root.exists() else [])
        for path in operator_markdown:
            source = self._read_source(path, required=False)
            if source is None:
                continue
            sections = _heading_sections(source["text"])
            for title, body in sections:
                if title.casefold() == name.casefold():
                    return {"name": name, "content": body,
                            "source": f"{source['path']}#{title}",
                            "date": source["date"], "validity": "valid",
                            "source_kind": "operator_reference",
                            "sha256": source["sha256"]}
        return None

    def lookup_field_note(self, field_id: str):
        if not isinstance(field_id, str) or not field_id:
            return None
        if self.field_notes_path.exists():
            try:
                notes = json.loads(self.field_notes_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise KnowledgeError("field_notes.json is unreadable") from exc
            note = notes.get(field_id) if isinstance(notes, dict) else None
            if (isinstance(note, dict) and note.get("content") and note.get("source")
                    and note.get("validity", "valid") == "valid"):
                return {**note, "field_id": field_id,
                        "validity": note.get("validity", "valid")}
        # Snapshots contribute identifiers and types only. Their platform facts
        # are deliberately excluded from model evidence and never override live API.
        field_snapshots = (sorted(self.field_root.rglob("*.json"))
                           if self.field_root and self.field_root.exists() else [])
        for path in field_snapshots:
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            rows = payload if isinstance(payload, list) else payload.get("results", []) if isinstance(payload, dict) else []
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get("id") == field_id:
                    dataset = row.get("dataset") or {}
                    return {"field_id": field_id, "type": row.get("type"),
                            "dataset_id": dataset.get("id") if isinstance(dataset, dict) else dataset,
                            "source": str(path.resolve()), "date": _document_date("", path),
                            "validity": "pending", "reference_only": True,
                            "content": "Exact field identifier and type reference only; confirm facts with live scoped API."}
        return None

    def _load_remedies(self):
        self.knowledge_root.mkdir(parents=True, exist_ok=True)
        if not self.remedies_path.exists():
            atomic_write_json(self.remedies_path, {"schema_version": 1, "entries": []})
        try:
            payload = json.loads(self.remedies_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KnowledgeError("remedies.json is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("entries"), list):
            raise KnowledgeError("remedies.json schema is unsupported")
        return payload["entries"]

    def _prior_knowledge_documents(self, overrides):
        source = self._read_source(self.prior_knowledge_path, required=False)
        if source is None:
            return []
        known_datasets = ({path.stem for path in self.field_root.rglob("*.json")
                           if path.is_file() and not path.stem.startswith("_")}
                          if self.field_root and self.field_root.exists() else set())
        lines = source["text"].splitlines()
        headings = []
        for index, line in enumerate(lines):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if match:
                headings.append((index, len(match.group(1)), match.group(2).strip()))
        if not headings:
            headings = [(0, 1, "document")]
        docs = []
        for offset, (start, level, title) in enumerate(headings):
            end = len(lines)
            child = False
            for next_start, next_level, _ in headings[offset + 1:]:
                if next_level <= level:
                    end = next_start
                    break
                child = True
            if child:
                continue
            content = "\n".join(lines[start:end]).strip()
            if not content:
                continue
            code_terms = re.findall(r"`([^`]+)`", content)
            datasets = sorted(set(code_terms).intersection(known_datasets)) or ["*"]
            signatures = sorted(set(re.findall(r"\b(?:S_[A-Z0-9_]+|C_[A-Z0-9_]+)\b", content)))
            if not signatures:
                signature_map = {"LOW_SHARPE": "S_SHARPE", "LOW_FITNESS": "S_FITNESS",
                                 "LOW_2Y_SHARPE": "S_2Y", "HIGH_TURNOVER": "S_TURNOVER_HI",
                                 "LOW_TURNOVER": "S_TURNOVER_LO"}
                signatures = sorted({value for key, value in signature_map.items()
                                     if key in content}) or ["none"]
            slug = re.sub(r"[^a-z0-9]+", "_", title.casefold()).strip("_")[:48]
            if not slug:
                slug = hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
            tags = {"dataset_id": datasets,
                    "mechanism": [f"prior:{slug}"],
                    "failure_signature": signatures,
                    "field_generation": ["unclassified"]}
            pointer = f"#/sections/{start + 1}/{title}"
            source_ref = f"{source['path']}{pointer}"
            digest = _sha256((content + source_ref).encode("utf-8"))
            evidence_id = digest[:20]
            override = overrides.get(evidence_id) or overrides.get(source_ref) or {}
            validity = override.get("validity", "valid")
            if validity not in VALIDITY:
                raise KnowledgeError(f"invalid prior validity for {source_ref}")
            docs.append({"id": evidence_id, "content": content, "source": source_ref,
                         "date": source["date"], "validity": validity,
                         "validity_reason": override.get("reason"),
                         "source_kind": "prior_advisory", "tags": tags,
                         "scope": {"region": None, "delay": None, "universe": None},
                         "batch": None})
        return docs

    def lookup_remedies(self, signatures, field_facts=None, include_historical=False):
        field_facts = field_facts or {}
        candidates, historical = [], []
        for entry in self._load_remedies():
            if not isinstance(entry, dict) or not set(("remedy_id", "applies_to", "preconditions",
                                                       "action", "rationale", "expected_effect",
                                                       "evidence", "status")) <= set(entry):
                continue
            status = entry.get("status")
            if not set(signatures).intersection(entry.get("applies_to", [])):
                continue
            if status == "verified":
                evidence_current, evidence_issue = self._reviewed_remedy_evidence_state(entry)
                if not evidence_current:
                    if include_historical:
                        historical.append({**entry, "validity": "evidence_invalid",
                                           "evidence_issue": evidence_issue})
                    continue
                preconditions = entry.get("preconditions") or {}
                if (not isinstance(preconditions, dict) or
                        not _matches_preconditions(preconditions, field_facts)):
                    continue
                candidates.append(entry)
            elif include_historical and status in {"falsified", "void", "pending_review", "seed"}:
                historical.append({**entry, "validity": status})
        return {"verified": candidates, "historical": historical}

    def record_remedy_outcome(self, reference: dict, diagnosis: dict, batch_dir: Path):
        """Record a remedy batch once; the target gate signature is measured, not inferred."""
        remedy_id = reference.get("remedy_id")
        entries = self._load_remedies()
        entry = next((row for row in entries if isinstance(row, dict) and
                      row.get("remedy_id") == remedy_id), None)
        if entry is None:
            raise KnowledgeError(f"verified remedy disappeared from Remedy KB: {remedy_id}")
        outcomes = entry.setdefault("outcomes", [])
        if not isinstance(outcomes, list):
            raise KnowledgeError("remedy outcome history schema is unsupported")
        batch_ref = str(Path(batch_dir).resolve())
        if any(row.get("batch") == batch_ref for row in outcomes if isinstance(row, dict)):
            return entry
        if entry.get("status") != "verified":
            raise KnowledgeError("remedy status changed before outcome was recorded")
        before = set(reference.get("target_signatures") or [])
        per_alpha = {alpha_id: set(row.get("signatures", []))
                     for alpha_id, row in diagnosis.get("alphas", {}).items()}
        after = sorted({signature for values in per_alpha.values() for signature in values})
        successful_alpha_ids = sorted(alpha_id for alpha_id, values in per_alpha.items()
                                       if before.isdisjoint(values))
        successful = bool(before) and bool(successful_alpha_ids)
        resolved = sorted(before) if successful else []
        uses = entry.get("uses", 0)
        successes = entry.get("successes", 0)
        if (not isinstance(uses, int) or isinstance(uses, bool) or uses < 0 or
                not isinstance(successes, int) or isinstance(successes, bool) or successes < 0):
            raise KnowledgeError("remedy usage counters must be non-negative integers")
        entry["uses"] = uses + 1
        entry["successes"] = successes + int(successful)
        outcomes.append({"batch": batch_ref,
                         "diagnosis_id": diagnosis.get("diagnosis_id"),
                         "source_diagnosis_id": reference.get("diagnosis_id"),
                         "source_alpha_id": reference.get("source_alpha_id"),
                         "target_signatures": sorted(before),
                         "after_signatures": after, "resolved_signatures": resolved,
                         "successful": successful,
                         "successful_alpha_ids": successful_alpha_ids,
                         "alpha_signatures": {alpha_id: sorted(values)
                                              for alpha_id, values in per_alpha.items()},
                         "gate_statuses": {alpha_id: row.get("status")
                                           for alpha_id, row in diagnosis.get("alphas", {}).items()},
                         "at": datetime.now().astimezone().isoformat()})
        if len(outcomes) >= 2 and all(not row.get("successful") for row in outcomes[-2:]):
            entry["status"] = "falsified"
            entry["status_reason"] = "two consecutive remedy batches did not resolve a target signature"
        outcomes[-1].update({"uses_after": entry["uses"],
                             "successes_after": entry["successes"],
                             "status_after": entry.get("status")})
        atomic_write_json(self.remedies_path, {"schema_version": 1, "entries": entries})
        return entry

    def _replace_management_log(self, temporary_path, log_path):
        os.replace(temporary_path, log_path)

    def _append_management_event(self, event):
        """Atomically replace the JSONL file so a failed write cannot leave a partial row."""
        self.knowledge_root.mkdir(parents=True, exist_ok=True)
        path = self.knowledge_root / "management_log.jsonl"
        existing = path.read_bytes() if path.exists() else b""
        if existing and not existing.endswith(b"\n"):
            raise KnowledgeError("management audit log has an incomplete trailing event")
        try:
            for line in existing.splitlines():
                json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise KnowledgeError("management audit log contains an unreadable event") from exc
        event_bytes = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary_path.open("wb") as handle:
                payload = existing + event_bytes
                if handle.write(payload) != len(payload):
                    raise OSError("short write while preparing management audit log")
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_management_log(temporary_path, path)
        except OSError:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _persist_with_audit(self, path, payload, previous_payload, event):
        """Commit state and its audit event together from the caller's perspective."""
        existed = path.exists()
        atomic_write_json(path, payload)
        try:
            self._append_management_event(event)
        except Exception as exc:
            try:
                if existed:
                    atomic_write_json(path, previous_payload)
                else:
                    path.unlink(missing_ok=True)
            except Exception as rollback_error:
                raise KnowledgeError(
                    "management audit write failed and the knowledge state rollback also failed"
                ) from rollback_error
            raise KnowledgeError(
                "knowledge change was rolled back because its audit event could not be recorded"
            ) from exc

    def _verified_remedy_evidence(self, evidence):
        if not isinstance(evidence, list) or not evidence:
            raise KnowledgeError(
                "verified remedies require batch, alpha_id and measured_change evidence"
            )
        verified_refs = []
        for row in evidence:
            if (not isinstance(row, dict) or
                    any(not isinstance(row.get(key), str) or not row[key].strip()
                        for key in ("batch", "alpha_id", "measured_change"))):
                raise KnowledgeError(
                    "verified remedies require batch, alpha_id and measured_change evidence"
                )
            batch_ref = row["batch"].strip()
            alpha_id = row["alpha_id"].strip()
            try:
                candidate = Path(batch_ref)
                if not candidate.is_absolute():
                    if candidate.parts and candidate.parts[0] == self.runs_root.name:
                        candidate = self.runs_root.parent / candidate
                    else:
                        candidate = self.runs_root / candidate
                batch_dir = candidate.resolve()
            except (OSError, ValueError) as exc:
                raise KnowledgeError("verified remedy batch path is invalid") from exc
            if not batch_dir.is_relative_to(self.runs_root) or not batch_dir.is_dir():
                raise KnowledgeError(
                    "verified remedy batch must be a local directory under runs_root"
                )

            prereg_path = batch_dir / "prereg.json"
            state_path = batch_dir / "state.json"
            intent_path = batch_dir / "simulation_intent.json"
            simulation_path = batch_dir / "simulation_raw.json"
            recovery_path = batch_dir / "recovery.json"
            results_path = batch_dir / "results.json"
            try:
                prereg_raw = prereg_path.read_bytes()
                state = json.loads(state_path.read_text(encoding="utf-8"))
                intent_raw = intent_path.read_bytes()
                intent = json.loads(intent_raw.decode("utf-8"))
                gate_raw = (batch_dir / "gate.json").read_bytes()
                saved_gate = json.loads(gate_raw.decode("utf-8"))
                results_raw = results_path.read_bytes()
                results = json.loads(results_raw.decode("utf-8"))
                prereg = json.loads(prereg_raw.decode("utf-8"))
                if simulation_path.exists():
                    simulation_raw_raw = simulation_path.read_bytes()
                    simulation_raw = json.loads(simulation_raw_raw.decode("utf-8"))
                    recovery_raw = None
                    recovery = None
                else:
                    recovery_raw = recovery_path.read_bytes()
                    recovery = json.loads(recovery_raw.decode("utf-8"))
                    simulation_raw_raw = None
                    simulation_raw = None
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                raise KnowledgeError(
                    f"verified remedy batch is missing readable prereg/state/intent/gate/results and simulation or recovery evidence: {batch_dir}"
                ) from exc
            try:
                prereg_is_locked = (isinstance(state, dict) and
                                    verify_prereg(prereg_path, state))
            except (OSError, AttributeError, TypeError):
                prereg_is_locked = False
            if not isinstance(prereg, dict) or not prereg_is_locked:
                raise KnowledgeError(
                    "verified remedy batch preregistration is invalid or its hash does not match"
                )
            intent_alpha_ids = intent.get("alpha_ids") if isinstance(intent, dict) else None
            if (not isinstance(intent, dict) or intent.get("status") != "READ_BACK" or
                    not isinstance(intent_alpha_ids, list) or
                    alpha_id not in intent_alpha_ids):
                raise KnowledgeError(
                    f"verified remedy batch is not closed with alpha read-back: {batch_dir}"
                )
            recovery_digest = None
            if simulation_raw is not None:
                raw_rows = (simulation_raw.get("alpha_results")
                            if isinstance(simulation_raw, dict) else None)
                if isinstance(simulation_raw, dict) and simulation_raw.get("alpha_id"):
                    raw_rows = [simulation_raw]
                if (not isinstance(raw_rows, list) or
                        not any(isinstance(item, dict) and item.get("alpha_id") == alpha_id
                                for item in raw_rows)):
                    raise KnowledgeError(
                        f"verified remedy Alpha is absent from saved simulation response: {alpha_id}"
                    )
                simulation_digest = _sha256(simulation_raw_raw)
            else:
                variants = prereg.get("variants")
                matched = recovery.get("matched") if isinstance(recovery, dict) else None
                missing = recovery.get("missing") if isinstance(recovery, dict) else None
                try:
                    alpha_index = intent_alpha_ids.index(alpha_id)
                    expression = variants[alpha_index].get("expression")
                except (AttributeError, IndexError, TypeError, ValueError):
                    expression = None
                if (not isinstance(variants, list) or
                        not isinstance(matched, dict) or not isinstance(missing, list) or
                        missing or not isinstance(expression, str) or
                        matched.get(expression) != alpha_id):
                    raise KnowledgeError(
                        f"verified remedy Alpha is absent from saved recovery mapping: {alpha_id}"
                    )
                recovery_digest = _sha256(recovery_raw)
                simulation_digest = None
            readback = results.get("readback") if isinstance(results, dict) else None
            gate = results.get("gate") if isinstance(results, dict) else None
            alpha_readback = readback.get(alpha_id) if isinstance(readback, dict) else None
            details = (alpha_readback.get("details")
                       if isinstance(alpha_readback, dict) else None)
            if (not isinstance(readback, dict) or
                    not isinstance(details, dict) or details.get("id") != alpha_id or
                    not isinstance(gate, dict) or not isinstance(gate.get(alpha_id), dict) or
                    not isinstance(saved_gate, dict) or
                    saved_gate.get(alpha_id) != gate.get(alpha_id) or
                    not isinstance(gate[alpha_id].get("status"), str) or
                    not gate[alpha_id]["status"].strip()):
                raise KnowledgeError(
                    f"verified remedy alpha is absent from saved readback or gate: {alpha_id}"
                )
            verified_refs.append({
                "batch": str(batch_dir), "alpha_id": alpha_id,
                "prereg_sha256": _sha256(prereg_raw),
                "simulation_raw_sha256": simulation_digest,
                "recovery_sha256": recovery_digest,
                "gate_sha256": _sha256(gate_raw),
                "results_sha256": _sha256(results_raw),
                "intent_sha256": _sha256(intent_raw),
            })
        return verified_refs

    def _reviewed_remedy_evidence_state(self, entry):
        history = entry.get("review_history")
        review = history[-1] if isinstance(history, list) and history else None
        if not isinstance(review, dict) or review.get("to_status") != "verified":
            return False, "verified review record is missing"
        evidence = entry.get("evidence")
        evidence_digest = _sha256(json.dumps(
            evidence, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8"))
        if evidence_digest != review.get("evidence_sha256"):
            return False, "reviewed evidence claim changed after approval"
        try:
            current_refs = self._verified_remedy_evidence(evidence)
        except KnowledgeError as exc:
            return False, str(exc)
        if current_refs != review.get("evidence_refs"):
            return False, "reviewed batch artifacts changed after approval"
        return True, None

    def _remedy_view(self, entry):
        view = dict(entry)
        if entry.get("status") == "verified":
            current, reason = self._reviewed_remedy_evidence_state(entry)
            view["evidence_state"] = "current" if current else "invalid"
            if reason:
                view["evidence_issue"] = reason
        return view

    def _read_note_store(self, kind):
        path = (self.operator_notes_path if kind == "operator" else
                self.field_notes_path if kind == "field" else None)
        if path is None:
            raise KnowledgeError("note kind must be operator or field")
        if not path.exists():
            return path, {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KnowledgeError(f"{path.name} is unreadable") from exc
        if not isinstance(payload, dict):
            raise KnowledgeError(f"{path.name} must contain a JSON object")
        return path, payload

    def put_note(self, kind: str, key: str, content: str, source: str, *,
                 date="unknown", validity="valid", replace=False,
                 reason=None, operator="user"):
        """Add or explicitly revise an exact operator or field note."""
        if not isinstance(key, str) or not key.strip():
            raise KnowledgeError("note key is required")
        if not isinstance(content, str) or not content.strip():
            raise KnowledgeError("note content is required")
        if not isinstance(source, str) or not source.strip():
            raise KnowledgeError("note source reference is required")
        if not isinstance(date, str) or not date.strip():
            raise KnowledgeError("note date must be a non-empty string")
        if validity not in VALIDITY:
            raise KnowledgeError("note validity is unsupported")
        if not isinstance(operator, str) or not operator.strip():
            raise KnowledgeError("operator identity is required")
        if validity != "valid" and (not isinstance(reason, str) or not reason.strip()):
            raise KnowledgeError("non-valid notes require a reason")
        key = key.strip()
        path, notes = self._read_note_store(kind)
        old = notes.get(key)
        if old is not None and not replace:
            raise KnowledgeError("note already exists; use replace with a reason")
        if old is not None and (not isinstance(reason, str) or not reason.strip()):
            raise KnowledgeError("replacing a note requires a reason")
        stamp = datetime.now().astimezone().isoformat()
        note = {"content": content.strip(), "source": source.strip(),
                "date": date.strip(), "validity": validity,
                "updated_at": stamp, "updated_by": operator.strip()}
        if validity != "valid":
            note["validity_reason"] = reason.strip()
        encoded_old = json.dumps(old, ensure_ascii=False, sort_keys=True) if old is not None else ""
        encoded_new = json.dumps(note, ensure_ascii=False, sort_keys=True)
        event = {
            "at": stamp, "action": "replace_note" if old is not None else "add_note",
            "kind": kind, "key": key, "operator": operator.strip(),
            "reason": reason.strip() if isinstance(reason, str) else None,
            "before_sha256": _sha256(encoded_old.encode("utf-8")) if old is not None else None,
            "after_sha256": _sha256(encoded_new.encode("utf-8")),
        }
        self._persist_with_audit(path, {**notes, key: note}, notes, event)
        return note

    def add_remedy(self, remedy: dict, *, operator="user"):
        """Register a remedy as pending review; adding cannot make it usable."""
        required = {"remedy_id", "applies_to", "preconditions", "action",
                    "rationale", "expected_effect", "evidence"}
        if not isinstance(remedy, dict) or not required <= set(remedy):
            raise KnowledgeError("remedy entry is missing required fields")
        remedy_id = remedy.get("remedy_id")
        applies_to = remedy.get("applies_to")
        if (not isinstance(remedy_id, str) or not remedy_id.strip() or
                not isinstance(applies_to, list) or not applies_to or
                any(not isinstance(item, str) or not item.strip() for item in applies_to)):
            raise KnowledgeError("remedy ID and applicable signatures are required")
        if not isinstance(remedy.get("preconditions"), dict):
            raise KnowledgeError("remedy preconditions must be a JSON object")
        for key in ("action", "rationale", "expected_effect"):
            value = remedy.get(key)
            if not ((isinstance(value, str) and value.strip()) or
                    (isinstance(value, dict) and value)):
                raise KnowledgeError(f"remedy {key} must be non-empty")
        if not isinstance(remedy.get("evidence"), list):
            raise KnowledgeError("remedy evidence must be a JSON array")
        if not isinstance(operator, str) or not operator.strip():
            raise KnowledgeError("operator identity is required")
        if remedy.get("status") not in {None, "seed", "pending_review"}:
            raise KnowledgeError("new remedies must enter as seed or pending_review")
        entries = self._load_remedies()
        remedy_id = remedy_id.strip()
        if any(isinstance(row, dict) and row.get("remedy_id") == remedy_id
               for row in entries):
            raise KnowledgeError("remedy ID already exists")
        stamp = datetime.now().astimezone().isoformat()
        entry = {key: value for key, value in remedy.items()
                 if key not in {"uses", "successes", "outcomes", "review_history"}}
        entry.update({"remedy_id": remedy_id, "status": "pending_review",
                      "uses": 0, "successes": 0, "outcomes": [],
                      "review_history": [{"from_status": None,
                                          "to_status": "pending_review",
                                          "reviewer": operator.strip(),
                                          "reason": "registered for human review",
                                          "at": stamp}]})
        event = {"at": stamp, "action": "add_remedy",
                 "remedy_id": remedy_id, "operator": operator.strip(),
                 "after_sha256": _sha256(json.dumps(
                     entry, ensure_ascii=False, sort_keys=True).encode("utf-8"))}
        self._persist_with_audit(
            self.remedies_path,
            {"schema_version": 1, "entries": [*entries, entry]},
            {"schema_version": 1, "entries": entries}, event)
        return entry

    def review_remedy(self, remedy_id: str, status: str, *, reviewer: str, reason: str):
        allowed = {"pending_review", "verified", "falsified", "void"}
        if status not in allowed:
            raise KnowledgeError("unsupported remedy review status")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise KnowledgeError("reviewer identity is required")
        if not isinstance(reason, str) or not reason.strip():
            raise KnowledgeError("review reason is required")
        entries = self._load_remedies()
        entry = next((row for row in entries if isinstance(row, dict) and
                      row.get("remedy_id") == remedy_id), None)
        if entry is None:
            raise KnowledgeError(f"unknown remedy ID: {remedy_id}")
        evidence_refs = (self._verified_remedy_evidence(entry.get("evidence"))
                         if status == "verified" else [])
        evidence_sha256 = (_sha256(json.dumps(
            entry.get("evidence"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8"))
                           if status == "verified" else None)
        previous_entries = copy.deepcopy(entries)
        old_status = entry.get("status")
        stamp = datetime.now().astimezone().isoformat()
        entry["status"] = status
        entry.setdefault("review_history", []).append({
            "from_status": old_status, "to_status": status,
            "reviewer": reviewer.strip(), "reason": reason.strip(), "at": stamp,
            "evidence_refs": evidence_refs, "evidence_sha256": evidence_sha256})
        event = {"at": stamp, "action": "review_remedy",
                 "remedy_id": remedy_id, "from_status": old_status,
                 "to_status": status, "reviewer": reviewer.strip(),
                 "reason": reason.strip(), "evidence_refs": evidence_refs,
                 "evidence_sha256": evidence_sha256}
        self._persist_with_audit(
            self.remedies_path, {"schema_version": 1, "entries": entries},
            {"schema_version": 1, "entries": previous_entries}, event)
        return entry

    def list_managed(self, kind="all"):
        if kind not in {"all", "operator", "field", "remedy", "evidence"}:
            raise KnowledgeError("knowledge kind must be all, operator, field, remedy or evidence")
        result = {}
        if kind in {"all", "operator"}:
            _, notes = self._read_note_store("operator")
            result["operator_notes"] = notes
        if kind in {"all", "field"}:
            _, notes = self._read_note_store("field")
            result["field_notes"] = notes
        if kind in {"all", "remedy"}:
            result["remedies"] = [self._remedy_view(row)
                                   for row in self._load_remedies()]
        if kind in {"all", "evidence"}:
            result["evidence"] = self._index_documents()
        return result

    def get_managed(self, kind: str, key: str):
        if kind in {"operator", "field"}:
            _, notes = self._read_note_store(kind)
            value = notes.get(key)
        elif kind == "remedy":
            value = next((row for row in self._load_remedies()
                          if isinstance(row, dict) and row.get("remedy_id") == key), None)
        elif kind == "evidence":
            value = next((row for row in self._index_documents()
                          if row.get("id") == key), None)
        else:
            raise KnowledgeError("knowledge item kind must be operator, field, remedy or evidence")
        if value is None:
            raise KnowledgeError(f"unknown {kind} ID: {key}")
        return self._remedy_view(value) if kind == "remedy" else value

    def set_evidence_validity(self, evidence_id: str, validity: str, reason: str,
                              operator="user"):
        """Append an explicit invalidation/reinstatement without deleting evidence."""
        if validity not in VALIDITY or not isinstance(reason, str) or not reason.strip():
            raise KnowledgeError("evidence validity needs an allowed status and reason")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise KnowledgeError("evidence ID is required")
        if not isinstance(operator, str) or not operator.strip():
            raise KnowledgeError("operator identity is required")
        evidence_id = evidence_id.strip()
        indexed = {row["id"]: row for row in self._index_documents()}
        if evidence_id not in indexed:
            raise KnowledgeError(f"unknown evidence ID: {evidence_id}")
        try:
            rows = json.loads(self.validity_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            rows = {}
        except (OSError, ValueError) as exc:
            raise KnowledgeError("evidence_validity.json is unreadable") from exc
        if not isinstance(rows, dict):
            raise KnowledgeError("evidence validity index schema is unsupported")
        previous_rows = copy.deepcopy(rows)
        previous = rows.get(evidence_id) or {}
        history = previous.get("history", []) if isinstance(previous, dict) else []
        if not isinstance(history, list):
            raise KnowledgeError("evidence validity history schema is unsupported")
        stamp = datetime.now().astimezone().isoformat()
        event = {"validity": validity, "reason": reason.strip(),
                 "operator": operator.strip(), "updated_at": stamp,
                 "from_validity": indexed[evidence_id].get("validity")}
        history = [*history, event]
        rows[evidence_id] = {**event, "history": history}
        self._persist_with_audit(
            self.validity_path, rows, previous_rows,
            {"at": stamp, "action": "set_evidence_validity",
             "evidence_id": evidence_id,
             "from_validity": event["from_validity"],
             "to_validity": validity,
             "operator": operator.strip(), "reason": reason.strip()})
        return rows[evidence_id]

    def build_pack(self, task: str, context: dict):
        """Assemble deterministic L1/L2/L3 evidence for one model decision."""
        fragments = self.inject_rules(task)
        field_ids = set(context.get("core_fields") or [])
        proposal = context.get("proposal") or {}
        for row in proposal.get("field_report") or []:
            if isinstance(row, dict) and row.get("field_id"):
                field_ids.add(row["field_id"])
        field_notes = []
        for field_id in sorted(item for item in field_ids if isinstance(item, str)):
            note = self.lookup_field_note(field_id)
            if note is not None:
                field_notes.append(note)

        expression_text = []
        for row in (context.get("variants") or proposal.get("variants") or []):
            if isinstance(row, dict) and isinstance(row.get("expression"), str):
                expression_text.append(row["expression"])
        operator_names = {name for name in context.get("operator_names", [])
                          if isinstance(name, str)}
        for expression in expression_text:
            operator_names.update(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression))
        operator_notes = []
        for name in sorted(item for item in operator_names if isinstance(item, str)):
            note = self.lookup_operator(name)
            if note is not None:
                operator_notes.append(note)

        dataset_id = context.get("dataset_id") or "*"
        scope = context.get("scope") or {}
        prior_batches = [row for row in context.get("prior_batches", [])
                         if isinstance(row, dict) and
                         (dataset_id == "*" or row.get("dataset_id") == dataset_id)]
        mechanism = proposal.get("mechanism_tags") or context.get("mechanism_tags") or []
        if not mechanism:
            mechanism = sorted({tag for row in prior_batches
                                for tag in (row.get("mechanism_tags") or [])})
        if isinstance(mechanism, str):
            mechanism = [mechanism]
        generation = proposal.get("field_generation_tags") or context.get("field_generation_tags") or []
        if not generation:
            generation = sorted({tag for row in prior_batches
                                 for tag in (row.get("field_generation_tags") or [])})
        if isinstance(generation, str):
            generation = [generation]
        failures = context.get("failure_signatures") or []
        if not failures:
            failures = sorted({tag for row in prior_batches
                               for tag in (row.get("failure_signatures") or [])})
        if isinstance(failures, str):
            failures = [failures]
        query_tags = {"dataset_id": dataset_id,
                      "mechanism": mechanism,
                      "failure_signature": failures,
                      "field_generation": generation,
                      "scope": {key: scope.get(key) for key in
                                ("region", "delay", "universe")}}
        query_text = " ".join([*(str(context.get(key) or "") for key in
                                 ("objective", "hypothesis", "query_text")),
                                *(str(row.get("hypothesis") or "") for row in prior_batches)])
        evidence = []
        if task == "select_dataset":
            indexed_docs = self._index_documents()
            for row in context.get("datasets") or []:
                candidate = row.get("id") if isinstance(row, dict) else None
                if not candidate:
                    continue
                tags = {**query_tags, "dataset_id": candidate}
                evidence.extend(self.search_evidence(tags, k=1, query_text=query_text,
                                                     _documents=indexed_docs))
        elif task in {"plan_probes", "construct_batch", "attribute_results",
                      "diagnose_failures", "decide_next"}:
            evidence = self.search_evidence(query_tags, k=5, query_text=query_text)
        unique = {item["id"]: item for item in evidence}
        l3_rows = list(unique.values())
        if task == "select_dataset":
            l3_rows = [{key: item[key] for key in
                        ("id", "source", "date", "validity", "validity_reason",
                         "tags", "scope", "score", "history_only") if key in item} | {
                            "summary": item["content"][:800]}
                        for item in l3_rows]
        return {"schema_version": 1, "task": task,
                "l1_rules": fragments, "l2_field_notes": field_notes,
                "l2_operator_notes": operator_notes,
                "l2_lookup_gaps": {"field_ids": sorted(field_ids -
                                                        {row["field_id"] for row in field_notes
                                                         if not row.get("reference_only")}),
                                   "operator_names": sorted(operator_names -
                                                            {row["name"] for row in operator_notes})},
                "l3_evidence": sorted(l3_rows,
                                      key=lambda row: (-row.get("score", 0),
                                                       row["source"], row["id"]))}

    def _index_documents(self):
        overrides = {}
        if self.validity_path.exists():
            try:
                overrides = json.loads(self.validity_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise KnowledgeError("evidence_validity.json is unreadable") from exc
            if not isinstance(overrides, dict):
                raise KnowledgeError("evidence_validity.json schema is unsupported")
        docs = self._prior_knowledge_documents(overrides)
        indexed_batches = set()
        for prereg_path in sorted(self.runs_root.rglob("prereg.json")) if self.runs_root.exists() else []:
            batch_dir = prereg_path.parent
            indexed_batches.add(str(batch_dir.resolve()))
            results_path = batch_dir / "results.json"
            void_path = batch_dir / "void.json"
            if not results_path.exists() and not void_path.exists():
                continue
            intent_path = batch_dir / "simulation_intent.json"
            if results_path.exists():
                try:
                    if json.loads(intent_path.read_text(encoding="utf-8")).get("status") != "READ_BACK":
                        continue
                except (OSError, ValueError):
                    continue
            diagnosis_path = batch_dir / "diagnosis.json"
            context_path = batch_dir / "batch_context.json"
            try:
                prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
                results = (json.loads(results_path.read_text(encoding="utf-8"))
                           if results_path.exists() else {})
                diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8")) if diagnosis_path.exists() else {}
                context = json.loads(context_path.read_text(encoding="utf-8")) if context_path.exists() else {}
                void_record = (json.loads(void_path.read_text(encoding="utf-8"))
                               if void_path.exists() else {})
            except (OSError, ValueError):
                continue
            proposal = prereg.get("proposal") or {}
            expression_by_id = {}
            variants = prereg.get("variants") or []
            for alpha_id, row in (results.get("readback") or {}).items():
                details = row.get("details") or {}
                code = (((details.get("regular") or {}).get("code")
                         if isinstance(details.get("regular"), dict) else None))
                expression_by_id[alpha_id] = code
            all_expressions = [v.get("expression") for v in variants if isinstance(v, dict)]
            gate = results.get("gate") or {}
            dia_by_alpha = diagnosis.get("alphas") or {}
            source_date = results_path if results_path.exists() else void_path
            date = (context.get("started_at") or "")[:10] or _document_date("", source_date)
            rows = list(gate.items())
            if not rows and void_record:
                rows = [("void", {"status": "VOID", "failures": [], "pending": []})]
            for i, (alpha_id, verdict) in enumerate(rows):
                dia = dia_by_alpha.get(alpha_id) or {}
                signatures = dia.get("signatures") or []
                mechanism = proposal.get("mechanism_tags") or ["unclassified"]
                generation = proposal.get("field_generation_tags") or sorted({
                    row.get("field_id") for row in proposal.get("field_report", [])
                    if isinstance(row, dict) and row.get("field_id")})
                tags = {
                    "dataset_id": prereg.get("dataset_id", "unknown"),
                    "mechanism": mechanism,
                    "failure_signature": signatures or ["none"],
                    "field_generation": generation or ["unclassified"],
                }
                readback = (results.get("readback") or {}).get(alpha_id) or {}
                metric = (readback.get("details") or {}).get("is") or {}
                attribution = results.get("attribution", "")
                if not isinstance(attribution, str):
                    attribution = json.dumps(attribution, ensure_ascii=False, sort_keys=True)
                content = "\n".join(str(part) for part in (
                    proposal.get("hypothesis", ""), proposal.get("expected_sign", ""),
                    all_expressions[i] if i < len(all_expressions) else expression_by_id.get(alpha_id, ""),
                    attribution, json.dumps(metric, ensure_ascii=False),
                    json.dumps(verdict, ensure_ascii=False),
                    json.dumps(dia, ensure_ascii=False), json.dumps(void_record, ensure_ascii=False),
                ) if part)
                pointer = f"#/gate/{alpha_id}" if gate else "#/void"
                source = f"{source_date.resolve()}{pointer}"
                digest = _sha256((content + source).encode("utf-8"))
                evidence_id = digest[:20]
                validity = "void" if void_record else "valid"
                override = overrides.get(evidence_id) or overrides.get(source) or {}
                if override:
                    validity = override.get("validity", validity)
                if validity not in VALIDITY:
                    raise KnowledgeError(f"invalid validity for evidence {evidence_id}")
                docs.append({"id": evidence_id, "content": content, "source": source,
                             "date": date, "validity": validity,
                             "validity_reason": (override.get("reason") if override else
                                                 void_record.get("reason") if void_record else None),
                             "source_kind": "batch_evidence", "tags": tags,
                             "scope": {key: (prereg.get("scope") or {}).get(key)
                                       for key in ("region", "delay", "universe")},
                             "batch": str(batch_dir.resolve())})
            probe_path = batch_dir / "probe_evidence.json"
            if results_path.exists() and probe_path.exists():
                try:
                    probe_payload = json.loads(probe_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    probe_payload = {}
                for index, observation in enumerate(probe_payload.get("observations", [])):
                    if not isinstance(observation, dict):
                        continue
                    content = json.dumps(observation, ensure_ascii=False, sort_keys=True)
                    source = f"{probe_path.resolve()}#/observations/{index}"
                    evidence_id = _sha256((content + source).encode("utf-8"))[:20]
                    override = overrides.get(evidence_id) or overrides.get(source) or {}
                    validity = override.get("validity", "valid")
                    if validity not in VALIDITY:
                        raise KnowledgeError(f"invalid validity for probe evidence {evidence_id}")
                    field_id = observation.get("field_id")
                    field_type = observation.get("field_type") or "unknown"
                    docs.append({"id": evidence_id, "content": content, "source": source,
                                 "date": date, "validity": validity,
                                 "validity_reason": override.get("reason"),
                                 "source_kind": "probe_evidence",
                                 "tags": {"dataset_id": prereg.get("dataset_id", "unknown"),
                                          "mechanism": ["field_probe"],
                                          "failure_signature": ["none"],
                                          "field_generation": [f"{field_id}:{field_type}"]},
                                 "scope": {key: (prereg.get("scope") or {}).get(key)
                                           for key in ("region", "delay", "universe")},
                                 "batch": str(batch_dir.resolve())})
        for void_path in sorted(self.runs_root.rglob("void.json")) if self.runs_root.exists() else []:
            batch_dir = void_path.parent
            if str(batch_dir.resolve()) in indexed_batches:
                continue
            context_path = batch_dir / "batch_context.json"
            try:
                void_record = json.loads(void_path.read_text(encoding="utf-8"))
                context = json.loads(context_path.read_text(encoding="utf-8")) if context_path.exists() else {}
            except (OSError, ValueError):
                continue
            source = f"{void_path.resolve()}#/void"
            content = json.dumps({"context": context, "void": void_record},
                                 ensure_ascii=False, sort_keys=True)
            digest = _sha256((content + source).encode("utf-8"))
            evidence_id = digest[:20]
            override = overrides.get(evidence_id) or overrides.get(source) or {}
            validity = override.get("validity", "void")
            if validity not in VALIDITY:
                raise KnowledgeError(f"invalid void evidence validity for {source}")
            scope = context.get("scope") or {}
            docs.append({"id": evidence_id, "content": content, "source": source,
                         "date": (context.get("started_at") or "")[:10] or
                                 _document_date("", void_path),
                         "validity": validity,
                         "validity_reason": override.get("reason") or void_record.get("reason"),
                         "source_kind": "void_batch",
                         "tags": {"dataset_id": context.get("dataset_id", "unknown"),
                                  "mechanism": ["unclassified"],
                                  "failure_signature": ["none"],
                                  "field_generation": ["unclassified"]},
                         "scope": {key: scope.get(key) for key in
                                   ("region", "delay", "universe")},
                         "batch": str(batch_dir.resolve())})
        docs.sort(key=lambda item: (item["date"], item["source"], item["id"]))
        index = {"schema_version": 1, "method": "heading-tags-bm25",
                 "documents": docs}
        atomic_write_json(self.index_path, index)
        return docs

    def search_evidence(self, query_tags: dict, k: int = 5,
                        query_text: str = "", include_history: bool = False,
                        _documents=None):
        if not isinstance(query_tags, dict) or any(key not in query_tags for key in CORE_TAGS):
            raise KnowledgeError(f"L3 query requires structured tags: {', '.join(CORE_TAGS)}")
        if not isinstance(k, int) or k < 1:
            raise KnowledgeError("evidence result count must be positive")
        docs = self._index_documents() if _documents is None else _documents
        eligible = []
        for doc in docs:
            if doc["validity"] != "valid" and not include_history:
                continue
            wanted_scope = query_tags.get("scope") or {}
            if any(value is not None and doc["scope"].get(key) not in {None, value}
                   for key, value in wanted_scope.items()):
                continue
            dataset = query_tags.get("dataset_id")
            if dataset and dataset != "*":
                observed_dataset = doc["tags"]["dataset_id"]
                observed_datasets = ([observed_dataset] if isinstance(observed_dataset, str)
                                     else observed_dataset)
                if dataset not in observed_datasets and "*" not in observed_datasets:
                    continue
            eligible.append(doc)
        corpus_tokens = [Counter(_tokens(doc["content"])) for doc in eligible]
        n_docs = len(eligible)
        avg_len = (sum(sum(counter.values()) for counter in corpus_tokens) / n_docs
                   if n_docs else 0.0)
        df = Counter(term for counter in corpus_tokens for term in counter)
        query_terms = _tokens(query_text)
        for key in ("mechanism", "failure_signature", "field_generation"):
            values = query_tags.get(key) or []
            if isinstance(values, str):
                values = [values]
            query_terms.extend(term for value in values for term in _tokens(value))
        q = Counter(query_terms)
        scored = []
        for doc, tf in zip(eligible, corpus_tokens):
            length = sum(tf.values())
            score = 0.0
            for term, qf in q.items():
                frequency = tf.get(term, 0)
                if not frequency:
                    continue
                idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
                denom = frequency + 1.5 * (1 - 0.75 + 0.75 * length / (avg_len or 1))
                score += idf * frequency * 2.5 / denom * qf
            for tag_key in ("mechanism", "failure_signature", "field_generation"):
                values = query_tags.get(tag_key) or []
                if isinstance(values, str):
                    values = [values]
                observed = doc["tags"].get(tag_key) or []
                if isinstance(observed, str):
                    observed = [observed]
                score += 2.0 * len(set(values).intersection(observed))
            scored.append((score, doc["source"], doc))
        scored.sort(key=lambda row: (-row[0], row[1], row[2]["id"]))
        valid = [{**doc, "score": round(score, 8)} for score, _, doc in scored
                 if score > 0 and doc["validity"] == "valid"]
        old = [{**doc, "score": round(score, 8), "history_only": True}
               for score, _, doc in scored
               if score > 0 and doc["validity"] != "valid"]
        return old[:k] if include_history else valid[:k]
