"""Immutable orchestration wire-policy, shared by Gateway and driver.

This policy is purely negative: it never issues or consumes live grants.
Both the client and the driver MUST apply it independently; importing the
same constants prevents an accidental difference in their allowed commands.
Legacy non-guarded clients remain outside this boundary.
"""

GUARDED_ORCHESTRATION_READS = frozenset({
    "get_platform_setting_options",
    "get_simulation_settings_schema",
    "readonly_probe_datasets",
    "get_dataset_details",
    "get_alpha_details",
    "get_alpha_pnl",
    "get_alpha_yearly_stats",
    "get_user_alphas",
})

# Executable claims cannot be smuggled through read requests or future
# expansions of the command table.
GUARDED_FORBIDDEN_CLAIMS = frozenset({
    "allow", "live", "authorization_id", "operator_approval_id",
    "grant_id", "attempt_id", "permission_to_dispatch",
    "dispatch_authorized",
})


def guarded_request_allowed(command, params):
    """Positive command/negative param gate. No authorization capability."""
    return (
        isinstance(command, str)
        and command in GUARDED_ORCHESTRATION_READS
        and isinstance(params, dict)
        and not bool(GUARDED_FORBIDDEN_CLAIMS.intersection(params))
    )
