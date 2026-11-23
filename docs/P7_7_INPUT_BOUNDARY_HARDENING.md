# P7.7 — Strict offline parser and temporal boundaries

This follow-up closes type-confusion and audit-input denial-of-service gaps in P7.2–P7.5.

- P7 invoice rows require string operation IDs before dictionary lookup. The release gate validates exact billing row fields, bounded IDs, and string enum types.
- P7 release checks reject non-string/unhashable status and Stop state values with an `OrchestrationError`, never interpreting malformed data as positive authority.
- P7 BRAIN canonical frame encoding now rejects excessively recursive frames as a domain error. Caller-reported readback scope uses canonical JSON byte equivalence, so `true` cannot masquerade as integer `1` in Delay.
- P7 Canary staleness uses the precise timestamp delta before integer presentation rounding; a 90.5 second old heartbeat is stale under a 90 second limit. Malformed report enums are rejected, and reason lists are bounded.

Targeted offline negative tests have been added; execution and full CI reconciliation remain queued. This batch does not enable any Provider request, BRAIN read, Simulation or Alpha submission. Production remains `P7_PRODUCTION_BLOCKED`.
