"""Knowledge-enriched model decisions and immutable local evidence references.

ResearchAgent owns the workflow; this service owns model-session lifecycle,
knowledge-pack caching and the association with DecisionRecorder audit files.
It cannot call BRAIN, create Simulation intents or qualify candidates.
"""
import hashlib
import json
from pathlib import Path

from alpha_research.state.store import atomic_write_json
from alpha_research.workflows.regular.errors import ResearchError


class ResearchDecisionSession:
    def __init__(self, knowledge, recorder, state_root):
        self.knowledge = knowledge
        self.recorder = recorder
        self.state_root = Path(state_root)
        self.session_packs = []

    def begin_session(self, research_id=None):
        session_id = self.recorder.begin_session(research_id=research_id)
        self.session_packs = []
        return session_id

    def save_knowledge_pack(self, pack, batch_dir=None):
        raw = json.dumps(pack, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        pack_id = digest[:20]
        path = self.state_root / "logs" / "knowledge_packs" / f"{pack_id}.json"
        if not path.exists():
            atomic_write_json(path, pack)
        ref = {"pack_id": pack_id, "sha256": digest, "source_ref": str(path),
               "session_id": self.recorder.session_id}
        self.session_packs.append(ref)
        if batch_dir is not None:
            self.write_batch_knowledge_packs(batch_dir)
        return ref

    def write_batch_knowledge_packs(self, batch_dir):
        batch_dir = Path(batch_dir)
        batch_dir.mkdir(parents=True, exist_ok=True)
        existing = (json.loads((batch_dir / "knowledge_pack.json").read_text(encoding="utf-8"))
                    if (batch_dir / "knowledge_pack.json").exists() else {})
        packs_by_id = {row.get("pack_id"): row for row in existing.get("packs", [])
                       if isinstance(row, dict) and row.get("pack_id")}
        for ref in self.session_packs:
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

    def decide(self, task, context, batch_dir=None):
        pack = self.knowledge.build_pack(task, context)
        ref = self.save_knowledge_pack(pack, batch_dir)
        if batch_dir is not None:
            self.recorder.bind_batch(batch_dir)
        enriched = {**context, "knowledge_pack": pack}
        return self.recorder.complete_json(task, enriched,
                                           knowledge_pack_ref=ref["source_ref"])

