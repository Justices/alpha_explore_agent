"""Stable, redacted diagnosis codes for full BRAIN Dataset catalog reads.

Codes are observations, not retry instructions or execution permissions.
No provider response text, credentials, URLs or arbitrary exception strings
cross the driver/Gateway boundary.
"""

from contextlib import contextmanager

CATALOG_FAILURE_CODES = frozenset({
    "CATALOG_PAGE_COUNT_CHANGED", "CATALOG_DUPLICATE_IDS",
    "CATALOG_PAGE_INCOMPLETE", "CATALOG_INVALID_RECORDS",
    "CATALOG_INVALID_PAGE", "CATALOG_INVALID_COUNT",
    "CATALOG_TOO_LARGE", "CATALOG_COUNT_EXCEEDED",
    "CATALOG_INVALID_JSON", "CATALOG_REQUEST_REJECTED",
    "CATALOG_AUTHORIZATION_REJECTED", "CATALOG_RATE_LIMITED",
    "CATALOG_UPSTREAM_ERROR", "CATALOG_HTTP_ERROR",
    "CATALOG_NETWORK_ERROR", "CATALOG_LOCAL_IO_ERROR",
    "CATALOG_UNKNOWN", "CATALOG_LOCK_UNAVAILABLE",
})

_MESSAGES = {
    "platform catalog changed during pagination": "CATALOG_PAGE_COUNT_CHANGED",
    "platform catalog contains duplicate records": "CATALOG_DUPLICATE_IDS",
    "platform catalog is incomplete": "CATALOG_PAGE_INCOMPLETE",
    "platform catalog contains invalid records": "CATALOG_INVALID_RECORDS",
    "platform catalog page is unreadable": "CATALOG_INVALID_PAGE",
    "platform catalog count is unavailable": "CATALOG_INVALID_COUNT",
    "platform catalog exceeds pagination limit": "CATALOG_TOO_LARGE",
    "platform catalog contains too many rows": "CATALOG_COUNT_EXCEEDED",
    "platform response is not readable JSON": "CATALOG_INVALID_JSON",
}


def classify_catalog_failure(exc):
    """Map trusted exception *types* and expected messages to fixed codes."""
    from alpha_research.integrations.brain.runtime import PlatformError
    from alpha_research.state.writer_lock import WriterLockError
    if isinstance(exc, WriterLockError):
        return "CATALOG_LOCK_UNAVAILABLE"
    if isinstance(exc, OSError):
        return "CATALOG_LOCAL_IO_ERROR"
    if not isinstance(exc, PlatformError):
        return "CATALOG_UNKNOWN"
    message = str(exc)
    if message in _MESSAGES:
        return _MESSAGES[message]
    prefix = "platform request returned HTTP "
    if message.startswith(prefix) and message[len(prefix):].isdigit():
        code = int(message[len(prefix):])
        if code in (401, 403):
            return "CATALOG_AUTHORIZATION_REJECTED"
        if code == 400:
            return "CATALOG_REQUEST_REJECTED"
        if code == 429:
            return "CATALOG_RATE_LIMITED"
        if 500 <= code <= 599:
            return "CATALOG_UPSTREAM_ERROR"
        return "CATALOG_HTTP_ERROR"
    if message.startswith("platform request failed:"):
        return "CATALOG_NETWORK_ERROR"
    if "snapshot identity key" in message:
        return "CATALOG_LOCAL_IO_ERROR"
    return "CATALOG_UNKNOWN"

# Stage names are fixed program identifiers, never filesystem paths, URLs, or
# untrusted exception strings. They may be safely copied to local plan records.
CATALOG_FAILURE_STAGES = frozenset({
    "CATALOG_CASE", "CATALOG_SCOPE", "CATALOG_LOCK",
    "CATALOG_ACCOUNT_IDENTITY", "CATALOG_SNAPSHOT_READ",
    "CATALOG_REMOTE_PAGINATION", "CATALOG_REMOTE_OPERATORS",
    "CATALOG_SNAPSHOT_WRITE", "CATALOG_SNAPSHOT_CHECKSUM",
    "CATALOG_REFERENCE_WRITE",
})


@contextmanager
def catalog_stage(stage):
    """Annotate original exception without changing its class or message.

    Preserve the innermost stage when an outer lock boundary also catches it.
    This retains the existing fail-closed / non-retry semantics.
    """
    if stage not in CATALOG_FAILURE_STAGES:
        raise ValueError("invalid catalog diagnostic stage")
    try:
        yield
    except Exception as exc:
        if getattr(exc, "catalog_failure_stage", None) not in CATALOG_FAILURE_STAGES:
            try:
                exc.catalog_failure_stage = stage
            except (AttributeError, TypeError):
                pass
        raise


def catalog_failure_stage(exc):
    stage = getattr(exc, "catalog_failure_stage", None)
    return stage if stage in CATALOG_FAILURE_STAGES else None


def catalog_exception_kind(exc):
    """Optional fixed code for unknown failures, never raw exception text."""
    from alpha_research.integrations.brain.runtime import PlatformError
    from alpha_research.state.writer_lock import WriterLockError
    if isinstance(exc, WriterLockError):
        return "LOCK"
    if isinstance(exc, PermissionError):
        return "PERMISSION"
    if isinstance(exc, OSError):
        return "OS"
    if isinstance(exc, PlatformError):
        return "PLATFORM"
    if isinstance(exc, (TypeError, ValueError, KeyError, IndexError)):
        return "DATA_SHAPE"
    return "UNEXPECTED"
