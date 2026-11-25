"""Trusted local evidence paths for provider-free model-call recovery.

The batch manifest is untrusted input: it may describe a call, but it must
never choose which file the recovery process reads or rewrites. The expected
locations are derived exclusively from trusted roots and the call ID.
"""
import errno
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from alpha_research.integrations.model_errors import ModelError


_CALL_ID = re.compile(r"[0-9a-f]{32}\Z")


def check_trusted_path(path, *, label):
    """Reject symlinks in the final file or any parent, even when absent."""
    path = Path(path)
    if path.is_absolute():
        resolved = path.resolve()
        if resolved == path:
            return path
        if os.name == "nt" and not path.drive.startswith(("\\\\?\\", "\\\\.\\")):
            # Windows can retain the extended prefix while another writer
            # creates the leaf. Accept only an exact alias of the same path.
            name = str(path)
            extended = ("\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\")
                        else "\\\\?\\" + name)
            if resolved == Path(extended):
                return path
    raise ModelError(f"model {label} escapes trusted evidence path")


def _check_manifest_leaf(reference, expected, *, label):
    """Check one filename after its parent was checked exactly once."""
    if not isinstance(reference, str) or reference != str(expected):
        raise ModelError(f"model {label} is not the trusted evidence path")
    # Leaf symlinks are rejected even when this evidence is not opened,
    # e.g. an unrelated response in a shared batch manifest.
    if expected.is_symlink():
        raise ModelError(f"model {label} is not the trusted evidence path")
    return expected


def trusted_replay_paths(batch_dir, calls_root, row):
    """Return canonical local artifacts after validating *every* manifest ref."""
    if not isinstance(row, dict):
        raise ModelError("existing batch model manifest has invalid call entries")
    call_id = row.get("call_id")
    if not isinstance(call_id, str) or _CALL_ID.fullmatch(call_id) is None:
        raise ModelError("existing batch model manifest has invalid call entries")
    batch = check_trusted_path(batch_dir, label="batch directory")
    calls = check_trusted_path(calls_root, label="global calls directory")
    copied = check_trusted_path(
        batch / "model_calls", label="batch evidence directory")
    if not isinstance(row.get("session_id"), str) or _CALL_ID.fullmatch(row["session_id"]) is None:
        raise ModelError("existing batch model manifest has invalid session identity")
    paths = {
        "context_ref": copied / f"{call_id}.context.json",
        "record_ref": copied / f"{call_id}.record.json",
        "response_ref": copied / f"{call_id}.response.json",
        "global_record_ref": calls / f"{call_id}.record.json",
        "global_response_ref": calls / f"{call_id}.response.json",
    }
    # Directory ancestry was checked above; do not run Path.resolve()
    # again for each of the five generated child files. Safe descriptor
    # opens independently pin their parents at read/copy time.
    for key, expected in paths.items():
        _check_manifest_leaf(row.get(key), expected, label=key)
    # The global context path is authoritative, reconstructed from call ID.
    context = calls / f"{call_id}.context.json"
    if context.is_symlink():
        raise ModelError("model global_context_ref is not the trusted evidence path")
    paths["global_context_ref"] = context
    return paths

@contextmanager
def open_trusted_directory(path, *, label="evidence directory"):
    """Pin the destination directory against symlink replacement on POSIX."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ModelError(f"model {label} is not the trusted evidence path")
    if os.name != "posix":
        check_trusted_path(path, label=label)
        yield None
        return
    fd = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(path.anchor, flags)
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ModelError(
                f"model {label} is not the trusted evidence path") from exc
        raise
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def open_trusted_evidence(path, *, label="evidence"):
    """Open one checked regular file without a symlink-swap read on POSIX.

    POSIX: walk absolute path components via pinned directory descriptors,
    O_DIRECTORY and O_NOFOLLOW. The final descriptor is also O_NOFOLLOW.
    Windows: Python's portable API lacks openat/O_NOFOLLOW; compare the
    opened file ID to the path's no-follow stat and recheck the full path.
    Windows remains best-effort against an actively swapping adversary.
    """
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ModelError(f"model {label} is not the trusted evidence path")
    if os.name == "posix":
        with open_trusted_directory(path.parent, label=label) as directory_fd:
            try:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW,
                             dir_fd=directory_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ModelError(
                        f"model {label} is not the trusted evidence path") from exc
                raise
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ModelError(f"model {label} is not a regular evidence file")
                with os.fdopen(fd, "rb") as handle:
                    fd = None
                    yield handle
            finally:
                if fd is not None:
                    os.close(fd)
    else:
        # Detect reparse points swapped into any path component immediately
        # before or after opening. This is *not* equivalent to POSIX openat.
        check_trusted_path(path, label=label)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(fd)
            check_trusted_path(path, label=label)
            linked = os.stat(path, follow_symlinks=False)
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino)
                       != (linked.st_dev, linked.st_ino)):
                raise ModelError(f"model {label} changed during evidence open")
            with os.fdopen(fd, "rb") as handle:
                fd = None
                yield handle
        finally:
            if fd is not None:
                os.close(fd)


def read_trusted_json(path, *, label="evidence"):
    """Parse exactly the file opened by the trusted descriptor."""
    with open_trusted_evidence(path, label=label) as handle:
        return json.load(handle)
