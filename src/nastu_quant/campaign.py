"""Campaign pointers and a process-wide, non-stealing writer lock."""
from datetime import datetime
import json
import os
from pathlib import Path

from .state_store import atomic_write_json


class WriterLockError(RuntimeError):
    pass


class SingleWriterLock:
    """Hold an OS lock for a mutating CLI operation; stale files are harmless."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None
        self._windows = os.name == "nt"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if self._windows:
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise WriterLockError("another research writer holds the state lock") from exc

        metadata = {"pid": os.getpid(),
                    "acquired_at": datetime.now().astimezone().isoformat()}
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps(metadata).encode("utf-8"))
        self.handle.flush()
        os.fsync(self.handle.fileno())
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return False
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(b"\0")
        self.handle.flush()
        if self._windows:
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
        return False


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
