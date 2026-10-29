"""Serialize stop/resume with the start of a BRAIN simulation POST."""

import errno
import os
from pathlib import Path
import time


class SubmissionGateError(RuntimeError):
    pass


class SubmissionGate:
    """A short-lived, cross-process lock shared by submission and emergency stop."""

    def __init__(self, state_root, timeout=60.0):
        self.path = Path(state_root) / "simulation-submit.lock"
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    self.handle.close()
                    self.handle = None
                    raise SubmissionGateError("simulation lock is unavailable") from exc
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise SubmissionGateError("timed out waiting for simulation submission") from exc
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        if self.handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None
        return False
