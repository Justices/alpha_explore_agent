# P4.4 — BRAIN read-only connection and scoped catalog probe

The P4.4 probe is deliberately separate from the `run-*` research paths.
It is an **explicitly authorized platform read**, not an Alpha research run.

## CLI

```sh
# In your local environment; do NOT commit the credential file or print it.
export BRAIN_CREDENTIALS_FILE=/private/secure/path/brain-credentials.json

python3 -m alpha_research brain-readonly-check \
  --region CHN --delay 1 --universe TOP3000 \
  --allow-brain-read
```

The CLI refuses to start without `--allow-brain-read`. This flag authorizes
only the connection check in this command; it never grants model, Simulation,
Alpha tag, readback/recovery or Alpha submission permission. The CLI refuses
execution within a marked offline Mock workspace.

`--config` can alternatively name an explicit configuration file containing
`auth.credential_env`; its environment variable must point to a credential
file. Credential contents are **never** included in the report.

## Strict command boundary

The Gateway starts its own driver with `--read-only`; both independently
allow **only**:

- `get_platform_setting_options`: platform simulation settings availability
  via OPTIONS; there is no simulation creation
- `readonly_probe_datasets`: exactly one scoped GET page of /data-sets with
  `limit=1` and `offset=0`, returning only `dataset_count`, not data rows

No call to `set_case`, `get_datasets` (which manages cached snapshots),
`simulate_batch`, `simulate_super`, `resume_simulation_batch`,
`tag_alpha`, `sync_backtest_cache`, model APIs or Alpha submission is
permitted. The driver also rejects mutating command JSON directly sent to
stdin, independently of the Gateway.

**Exception:** BRAIN authentication itself requires a POST to the platform
`/authentication` endpoint. That login POST does not create a Simulation,
but the check is **not** network-free. It can reveal to the platform that the
account connected; run it only with authorization to authenticate.

## Results

`READ_ONLY_SCOPE_VERIFIED` means the server returned platform settings
listing the exact Region/Delay/Universe combination and the scoped dataset GET
returned a readable count. `SCOPE_NOT_AVAILABLE` means the selected
combination was not in the server's offered settings. The response never
includes credentials or Alpha details. Both statuses report
`simulation_invoked=false` and `model_invoked=false`.

The check does **not** establish eligibility to submit Alpha, access arbitrary
datafields, run Simulations, or prove end-to-end production readiness.

## Verification and security limitations

- Offline tests in `tests/test_p4_readonly_brain_probe.py` verify the CLI
  opt-in flag, Gateway restriction, driver-side restriction, response hygiene,
  scope matching, and absence of a model client.
- Existing cross-platform CI must pass before the feature is accepted.
- The remote session cannot be verified in GitHub CI without operator-owned
  credentials. Do not substitute synthetic fixture success for remote success.
- A credential-safe console output does not rule out logs emitted by a
  separately configured external process or proxy. Keep operational logs
  access controlled.
- Remote platform permissions and API semantics can change independently;
  a success at one Region/Delay/Universe is not blanket authorization.

## Production admission

The P4.3 `NOT_AUTHORIZED` verdict remains in effect. The P4.4 check
cannot issue or consume a live execution grant. A dedicated trusted issuer,
live attempt journal, operation-specific Gateway enforcement and a separate
Simulation/Alpha submission approval are still required.
