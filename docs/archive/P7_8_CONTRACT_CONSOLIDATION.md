> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# P7.8 — Shared negative status contracts

P7.2–P7.5 use a single Provider hostname catalog and closed Billing/BRAIN/Canary status vocabularies in `research_p7_contracts.py`. The offline BRAIN producer and integrated release verifier share one decision table; Canary producer and verifier share a single Stop-first status function.

P7 BRAIN caller readback now checks the exact scope shape and types, all bounded frame fingerprints, and immutable no-live flags before correlating a response. The caller's supplied digest remains **unattested**.

The new no-network tests cover BRAIN's six possible policy/monitor combinations, Canary priority and allowed Provider/Billing identities. The code is committed; no successful test result is claimed until final CI evidence is reviewed.

This refactor never grants live Provider/BRAIN access, Simulation, Alpha submission or automatic retry. Production is still `P7_PRODUCTION_BLOCKED`.
