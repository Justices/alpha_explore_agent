"""Process-wide writer lock for the research state directory."""
from datetime import datetime
import json
import os
from pathlib import Path


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
