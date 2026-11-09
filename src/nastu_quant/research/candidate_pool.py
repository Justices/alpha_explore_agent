"""Derived global Alpha pool index; evaluation lineage remains immutable by context.

Duplicate Alpha IDs across Campaigns or Workflow families are consolidated for
pool presentation, never used to bypass live RA / correlation verification.
"""
import hashlib
import json

from ..state_store import atomic_write_json


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        default=str).encode("utf-8")).hexdigest()


def evidence_hash(details, correlations, failures, pending):
    """Bind a verdict to exactly the details, correlations and gate findings."""
    return _hash({"details": details, "correlations": correlations,
                  "failures": sorted(failures), "pending": sorted(pending)})


def policy_hash(config):
    """Only the admission/selection policy participates, never credentials."""
    return _hash({"candidate": config.get("gate"),
                  "preferred": config.get("preferred")})


def _latest(candidate_rows, preferred_by_id):
    # Prefer the latest *assessment*, not the latest success: stale PASS
    # must not survive a newer failed or unresolved re-assessment.
    newest = sorted(candidate_rows,
                    key=lambda row: (row["updated_at"], row["candidate_id"]))[-1]
    preferred = preferred_by_id.get(newest["candidate_id"])
    sha = newest.get("evidence_sha256")
    policy = newest.get("policy_sha256")
    same_evidence = bool(preferred and
                         isinstance(sha, str) and len(sha) == 64 and
                         isinstance(policy, str) and len(policy) == 64 and
                         preferred.get("evidence_sha256") == sha and
                         preferred.get("policy_sha256") == policy)
    preferred_status = preferred["status"] if same_evidence else "NOT_CANDIDATE"
    return newest, preferred_status


class CandidatePool:
    """A derived index with one entry per Alpha ID and full source lineage."""

    def __init__(self, record_store):
        self.store = record_store
        self.index_path = record_store.root / "pool_index.json"

    def build_index(self):
        candidates = self.store.list("candidate")
        preferred = {row["candidate_id"]: row
                     for row in self.store.list("preferred")}
        grouped = {}
        for row in candidates:
            grouped.setdefault(row["alpha_id"], []).append(row)
        entries = []
        for alpha_id in sorted(grouped):
            rows = grouped[alpha_id]
            current, preferred_status = _latest(rows, preferred)
            sources = sorted(
                ({"candidate_id": row["candidate_id"],
                  "workflow": row["workflow"],
                  "candidate_ref": row["candidate_ref"],
                  "source_ref": row["source_ref"],
                  "status": row["status"],
                  "updated_at": row["updated_at"]}
                 for row in rows),
                key=lambda row: (row["workflow"], row["candidate_ref"],
                                 row["candidate_id"]))
            entries.append({
                "alpha_id": alpha_id, "candidate_id": current["candidate_id"],
                "candidate_status": current["status"],
                "preferred_status": (preferred_status
                                     if current["status"] == "CANDIDATE"
                                     else "NOT_CANDIDATE"),
                "evaluation_count": len(rows),
                "sources": sources,
                "latest_updated_at": current["updated_at"],
                "latest_evidence_sha256": current.get("evidence_sha256"),
                "latest_policy_sha256": current.get("policy_sha256"),
            })
        return {"schema_version": 1, "alpha_count": len(entries),
                "candidate_count": sum(e["candidate_status"] == "CANDIDATE"
                                       for e in entries),
                "preferred_count": sum(e["preferred_status"] == "PREFERRED"
                                       for e in entries),
                "entries": entries}

    def sync_index(self):
        """Idempotently reconstruct; no platform requests, safe after interruption."""
        state = self.build_index()
        if self.index_path.exists():
            current = json.loads(self.index_path.read_text(encoding="utf-8"))
            if current == state:
                return state
        atomic_write_json(self.index_path, state)
        return state

    def list_candidates(self):
        return [row for row in self.build_index()["entries"]
                if row["candidate_status"] == "CANDIDATE"]

    def list_preferred(self):
        return [row for row in self.build_index()["entries"]
                if row["candidate_status"] == "CANDIDATE"
                and row["preferred_status"] == "PREFERRED"]

    def find_reusable(self, alpha_id, *, evidence_sha256, policy_sha256):
        """Reuse already-evaluated, identical evidence; never assume freshness.

        Exact evidence and policy digests are mandatory. Callers must obtain
        current evidence independently and MUST NOT use this to skip RA/PC/SC.
        """
        if not all(isinstance(v, str) and len(v) == 64 and
                   all(ch in "0123456789abcdef" for ch in v)
                   for v in (evidence_sha256, policy_sha256)):
            return None
        for row in reversed(sorted(self.store.list("candidate"),
                                   key=lambda x: (x["updated_at"], x["candidate_id"]))):
            if (row["alpha_id"] == alpha_id
                    and row.get("evidence_sha256") == evidence_sha256
                    and row.get("policy_sha256") == policy_sha256):
                return row
        return None
