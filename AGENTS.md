# Repository guidance

Read this file before changing code, tests, documentation, packaging or release metadata.

## Mission and current contract

`hermes-a2a-gateway` connects Codex Desktop to a private Hermes server using MCP,
a local SQLite client daemon, authenticated HTTPS/SSE, a PostgreSQL broker and
loopback Hermes A2A. Direct and daemon-managed SSH tunnel transports are supported.
The only public commands are `broker`, `client`, `client-mcp`, and `client-doctor`.

The operator explicitly authorized the v0.7 breaking rename and removal of all
legacy gateway modes, executable aliases, setup skills and Hermes client plugin.
Do not restore `serve`, the inbound Codex gateway, CLI backend, seven `hermes_*`
tools or old configuration aliases. Historical evidence is not current setup guidance.

Canonical distribution, executable and MCP registration: `hermes-a2a-gateway`.
Python namespace: `hermes_a2a_gateway`. Configuration uses `HERMES_A2A_GATEWAY_*`;
`HERMES_A2A_ENDPOINT` and `HERMES_A2A_TOKEN` configure the server's Hermes peer.
See `docs/client-server-contract.md`, `docs/deployment.md`, and the current README.

## Ownership and invariants

- `broker*.py`: PostgreSQL ledger, authenticated API, loopback Hermes dispatch.
- `client*.py`: single-writer SQLite inbox, MCP facade and transport lifecycle.
- `ssh_tunnel.py`: client-owned SSH process, strict authentication, retry and cleanup.
- `native_delivery.py`: version/schema-gated native queue delivery.
- `a2a.py`, `settings.py`, `models.py`: internal loopback peer protocol transport.
- MCP stdout contains protocol frames only; diagnostics go to stderr.
- No model-supplied URL, SSH target, credential or execution identity. Native origin
  comes from trusted host MCP metadata; reject missing or inconsistent identity.
- Verify TLS and SSH host keys, retain device bearer authentication inside tunnels,
  bind local forwards to loopback, and never invoke SSH through shell interpolation.
  Only the daemon owns its tunnel; never kill unrelated SSH processes or services.
- Broker requires HTTPS at a trusted proxy for remote deployment. Hermes remains
  loopback-only. Development loopback HTTP is explicit and never a remote fallback.
- Secret inputs come from protected environment injection. Never log, echo or commit
  tokens, keys, prompt payloads, private configuration or raw authentication errors.
  Native/SSH child environments must scrub both new and historical secret prefixes.
- Payload encryption keys stay outside stores. Enforce retention, restrictive modes
  and quotas. Results/artifacts/backups can also be sensitive.
- Retry network connectivity with bounds and backoff, not ambiguous agent work.
  Reconcile Hermes mutations only by exact saved task identity. Preserve
  `outcome_unknown` when evidence is insufficient. Native queue insertion is not
  idempotent; uncertain acknowledgement remains `delivery_outcome_unknown` and
  must not be blindly re-enqueued. Queue ACK does not prove Desktop consumption.
- Cancellation is best-effort; never claim upstream computation stopped without proof.
- Preserve A2A v1 shapes; do not add the legacy stream `final` field.
- Preserve per-flow serialization, bounded admission, one broker dispatcher and one
  active client writer per SQLite file. Do not run old/new daemons on the same inbox.
- v0.6 client and broker data survive the rename. Preserve `broker_v06_*` schema,
  the peer context UUID namespace, device/flow/native origin IDs, operation/result/
  delivery IDs, receipts, cursor, ciphertext and keys. Additive migrations only.
  Legacy SQLite data is archived, not interpreted as a modern inbox or discarded.
- Explicit submit waits support 0–60 seconds; the configured inline wait is the
  default, not an undisclosed smaller ceiling. Reject invalid arguments clearly
  before persistence or dispatch, without exposing submitted content.

## Development and validation

1. Inspect `git status` and preserve unrelated user changes.
2. Use CPython 3.11 and the project virtual environment.
3. Keep work scoped. Add regression tests for protocol, persistence, authentication,
   timeouts, idempotence, cancellation, restart, tunnel lifecycle and network policy.
4. Update README, `.env.example`, relevant docs and CHANGELOG for public changes.
5. Historical reports are immutable evidence; add dated reports for new live results.

Run from the repository root:

```bash
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest --cov=hermes_a2a_gateway --cov-report=term-missing
```

The full coverage gate requires `HERMES_A2A_GATEWAY_TEST_POSTGRES_DSN` pointing to
an isolated disposable PostgreSQL 16 database. CI runs the full suite with PostgreSQL;
macOS/Linux also run portable tests and clean-wheel installation. Do not lower the
full-suite coverage gate merely because PostgreSQL is absent locally.

```bash
build_dir=$(mktemp -d)
.venv/bin/python -m build --outdir "$build_dir"
.venv/bin/python -m twine check "$build_dir"/*
.venv/bin/python scripts/check_dist.py "$build_dir"
.venv/bin/python scripts/check_wheel_install.py "$build_dir"/*.whl
.venv/bin/python scripts/write_sha256sums.py "$build_dir"
.venv/bin/python scripts/write_sha256sums.py --check "$build_dir"
```

Native protocol changes also require `scripts/check_app_server_schema.py` against
the supported local Codex CLI. Live model tasks are opt-in, uniquely identified,
harmless, authorized by the operator, and never run in CI. Simulated reconnect tests
do not prove real machine sleep/wake or native Desktop consumption.

## Completion and release

Relevant tests, clean wheel and CI pass. The diff contains no private files, runtime
stores, logs, credentials or stale artifacts. Documentation matches implemented limits.
Publish immutable release assets from the exact successful main/push CI commit.
Install and verify downloaded release artifacts on both hosts; report provenance and
actual end-to-end evidence separately from package/transport checks.

Removal/upgrade plans identify exact owned installations, back up data and keys,
check interpreter dependencies, stop writers before consistent copy, and preserve a
rollback path. Do not remove Hermes, Codex, PostgreSQL, provider authentication,
unrelated SSH tunnels, user workspaces or shared runtimes as gateway cleanup.
