"""One canonical admission predicate for a never-executed research Task.

Used by Manifest handoff, offline preparation and model-attempt reservation.
It is deliberately a pure read-only predicate: no state changes, network,
model client, or platform operations.
"""

_RESEARCH_WORKFLOWS = frozenset({"super", "optimization"})
_UNSTARTED_STEPS = {
    "validate": "PENDING",
    "execute": "PENDING",
    "qualify": "PENDING",
    "collect": "PENDING",
}
_UNUSED_BUDGET = {"limit": 1, "used": 0, "reserved": 0}


def is_fresh_research_task(task):
    """Reject Tasks with any consumed step, budget or recovery evidence."""
    return (
        isinstance(task, dict)
        and task.get("workflow") in _RESEARCH_WORKFLOWS
        and task.get("status") == "CREATED"
        and task.get("steps") == _UNSTARTED_STEPS
        and task.get("budget") == _UNUSED_BUDGET
        and task.get("checkpoints") == {}
        and task.get("research_identity") is None
        and task.get("recovery_target") is None
        and task.get("result") is None
        and task.get("failure") is None
    )
