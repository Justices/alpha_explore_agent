# P3: model connectivity and BRAIN tool boundary

## Merge baseline

Feature branch contains the latest main commit c1512e64 as a second parent of
merge commit 6469ceb4. This incorporates the new Dataset review and one-target
Optimization policy, plus field-generation, recovery and Workbench changes.
The latest main campaign imported an absent dataset_review module; this branch
adds it with saved-receipt attribution, recorded selection and intent-only
optimization recovery. No simulation is automatically reposted.

## Model-only smoke

By default these commands do NOT contact models or BRAIN:

```powershell
python -m alpha_research --lead codex --executor glm models inspect
python -m alpha_research --lead codex --executor glm models doctor
python -m alpha_research --lead codex --executor glm models smoke --role research_lead
```

An operator can explicitly authorize ONE model request:

```powershell
python -m alpha_research --lead deepseek --executor glm models smoke --role research_lead --allow-model-call
```

This last command may consume paid model tokens; it does NOT run Simulation
or connect to BRAIN. The model receives a fixed JSON contract probe,
must return exactly {ready:true, contract:'model_probe_v1'}, and cannot
choose arbitrary executable tools. One model attempt is recorded in a
fresh one-call ledger with model identity and raw response in LOCAL audit
files. The printed result contains only status, hashes and budget summary.

Model smoke success confirms one actual model request and response format;
it does NOT validate research quality, tool permissions of a separately
installed CLI beyond its adapter, or the platform's availability.

## Restricted BRAIN evidence bridge

alpha_research/orchestration/model_evidence_gateway.py exposes only
get_platform_setting_options, get_simulation_settings_schema, get_datasets,
get_datafields and get_operators. It validates the fixed Region/Delay/
Universe, checks Dataset IDs, records local request+response digest evidence
and never provides Simulation/Alpha mutation methods. It delegates only
approved read operations to the existing Gateway driver.

Only the orchestrator may instantiate this interface. Agent prompts never
receive the Gateway object. Direct actions to simulate, resume, tag,
submit or change a case are denied before any underlying call.

**Production action routing remains under the existing workflow's durable
intent and BRAIN recovery controls.** This module is an additive read-only
bridge and is NOT a production replacement for the simulation executor.
Real end-to-end BRAIN integration and paid model smoke require explicit
authorization, credentials, and separate validation. None were performed
by this P3 work or CI tests.

## New Dataset closeout boundary

- Completion relies on matching saved child simulation READ_BACK receipts.
- Pending attribution retries only missing model reports, no simulation.
- The lead's Dataset decision must select a measured Alpha ID and reason.
- One optimization run ID and intent reference are saved before any launch.
- An interrupted launch never calls run_optimization again.
- The saved optimization intent is the only recovery target.
- Empty selection is permanently recorded as no_selection.
- No record is promoted to Candidate without the deterministic RA/PC/SC gates.

## Verification

tests/test_dataset_review_safety.py and tests/test_p3_model_bridge.py
cover malformed selections, missing intents, crash recovery, mock-only
contract probes, forbidden BRAIN operations and local evidence digests.
Cross-platform GitHub Actions remains mandatory before PR merge.
