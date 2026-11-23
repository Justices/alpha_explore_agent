"""Closed, negative-only shared research release audit vocabulary.

These constants and pure verdict functions cannot authorize live execution.
"""

PROVIDER_HOSTNAMES = frozenset({
    "api.deepseek.com", "open.bigmodel.cn", "api.moonshot.cn",
})
BILLING_STATUSES = frozenset({
    "P7_INVOICE_MATCHED_UNTRUSTED_INPUT", "P7_RECONCILIATION_REQUIRED",
})
BRAIN_STATUSES = frozenset({
    "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE",
    "P7_BRAIN_READ_SCOPE_DENIED",
    "P7_BRAIN_READ_STOP_LATCHED",
    "P7_BRAIN_READ_MONITOR_UNAVAILABLE",
})
CANARY_STATUSES = frozenset({
    "P7_CANARY_STOP_ALREADY_LATCHED",
    "P7_CANARY_STOP_REQUIRED",
    "P7_CANARY_OFFLINE_OBSERVATION_ONLY",
})


def brain_read_status(frames_valid, monitor_state):
    """A denied frame takes priority over the offline monitor verdict."""
    if not frames_valid:
        return "P7_BRAIN_READ_SCOPE_DENIED"
    if monitor_state == "STOPPED":
        return "P7_BRAIN_READ_STOP_LATCHED"
    if monitor_state == "NOT_SUPPLIED":
        return "P7_BRAIN_READ_MONITOR_UNAVAILABLE"
    return "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE"


def canary_status(stop_latched, stop_required):
    """An irreversible Stop always takes priority over local observation."""
    if stop_latched:
        return "P7_CANARY_STOP_ALREADY_LATCHED"
    if stop_required:
        return "P7_CANARY_STOP_REQUIRED"
    return "P7_CANARY_OFFLINE_OBSERVATION_ONLY"
