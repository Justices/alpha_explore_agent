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
        # Avoid O_APPEND: writing metadata must not append to the lock byte.
        # os.open creates the file atomically without truncating another owner.
        self.handle = os.fdopen(
            os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
        try:
            if self.path.stat().st_size == 0:
                self.handle.write(b"\0")
                self.handle.flush()
            self.handle.seek(0)
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

        try:
            metadata = {"pid": os.getpid(),
                        "acquired_at": datetime.now().astimezone().isoformat()}
            self.handle.seek(0)
            self.handle.write(json.dumps(metadata).encode("utf-8"))
            self.handle.truncate()
            self.handle.flush()
            os.fsync(self.handle.fileno())
        except BaseException:
            # A metadata failure after OS acquisition must release its lock.
            if self._windows:
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return False
        # Never temporarily expose a zero-byte lockfile to contenders.
        self.handle.seek(0)
        self.handle.write(b"\0")
        self.handle.truncate(1)
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
