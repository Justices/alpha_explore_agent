"""Persist Regular Campaign pointers."""
from datetime import datetime
import json
from pathlib import Path

from alpha_research.state.store import atomic_write_json


class CampaignStore:
    """Persist campaign state separately from the per-case candidate ledger."""

    def __init__(self, state_root: Path):
        self.root = Path(state_root).resolve() / "campaigns"

    def path_for(self, campaign_id: str) -> Path:
        if not campaign_id or any(char not in "0123456789abcdef" for char in campaign_id.lower()):
            raise ValueError("campaign id must be a hexadecimal case key")
        return self.root / f"{campaign_id}.json"

    def get(self, campaign_id: str) -> dict:
        path = self.path_for(campaign_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise ValueError(f"campaign state is unreadable: {path}") from exc

    def update(self, campaign_id: str, **changes) -> dict:
        path = self.path_for(campaign_id)
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            current = {}
        except (OSError, ValueError) as exc:
            raise ValueError(f"campaign state is unreadable: {path}") from exc
        if not current:
            current = {"schema_version": 1, "campaign_id": campaign_id,
                       "created_at": datetime.now().astimezone().isoformat(),
                       "candidate_ledger_ref": None, "current_batch": None,
                       "current_dataset": None, "dataset_state": None,
                       "selection_mode": None, "locked": False,
                       "auth_mode": "auto", "status": "new"}
        if current.get("schema_version") != 1:
            raise ValueError("unsupported campaign state schema")
        current.update(changes)
        current["updated_at"] = datetime.now().astimezone().isoformat()
        atomic_write_json(path, current)
        return current
