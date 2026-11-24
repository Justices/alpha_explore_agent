"""Stable, redacted diagnosis codes for full BRAIN Dataset catalog reads.

Codes are observations, not retry instructions or execution permissions.
No provider response text, credentials, URLs or arbitrary exception strings
cross the driver/Gateway boundary.
"""

CATALOG_FAILURE_CODES = frozenset({
    "CATALOG_PAGE_COUNT_CHANGED", "CATALOG_DUPLICATE_IDS",
    "CATALOG_PAGE_INCOMPLETE", "CATALOG_INVALID_RECORDS",
    "CATALOG_INVALID_PAGE", "CATALOG_INVALID_COUNT",
    "CATALOG_TOO_LARGE", "CATALOG_COUNT_EXCEEDED",
    "CATALOG_INVALID_JSON", "CATALOG_REQUEST_REJECTED",
    "CATALOG_AUTHORIZATION_REJECTED", "CATALOG_RATE_LIMITED",
    "CATALOG_UPSTREAM_ERROR", "CATALOG_HTTP_ERROR",
    "CATALOG_NETWORK_ERROR", "CATALOG_LOCAL_IO_ERROR",
    "CATALOG_UNKNOWN",
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
