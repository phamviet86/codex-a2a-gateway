# Repository guidance

These instructions apply to the entire repository. Codex and other coding agents should read this file before changing code, tests, documentation, packaging, or release metadata.

## Mission and scope

`codex-a2a-gateway` gives Codex a bidirectional A2A v1.0 integration:

- Outbound: Codex calls the local MCP stdio server, which delegates to the configured loopback Hermes A2A peer.
- Inbound: an A2A client calls the HTTP/SSE gateway, which maps the task to a Codex App Server thread and turn.

Hermes is the first verified outbound peer, not the product boundary. Do not turn this project into Hermes administration, a general arbitrary-URL proxy, or a multi-tenant service without an explicit architecture and security decision.

## Architecture ownership

- `src/codex_a2a_gateway/server.py`, `core.py`, and `a2a.py`: outbound MCP-to-A2A adapter.
- `src/codex_a2a_gateway/gateway.py` and `inbound.py`: inbound A2A transport and task lifecycle.
- `src/codex_a2a_gateway/codex_backend.py`: Codex App Server and CLI compatibility adapters.
- `src/codex_a2a_gateway/store.py`: SQLite schema, mappings, tasks, messages, events, and migrations.
- `src/codex_a2a_gateway/settings.py`: environment contract and network policy.
- `tests/`: mirrors these boundaries with fake-server and protocol regression coverage.

Treat `docs/durable-jobs.md`, `docs/architecture-v0.2.md`, `docs/inbound-gateway.md`, and the current README as the implemented contract. Files labelled as v0.1 or research are historical evidence, not the current specification.

## Non-negotiable invariants

- MCP stdout contains protocol frames only. Send diagnostics to stderr.
- Outbound endpoints and discovered interfaces remain loopback-only. Never accept a model-supplied URL or credential.
- Inbound non-loopback bind or public URL requires bearer authentication. Remote deployment also requires TLS at a trusted proxy.
- Read secrets from environment variables. Never log, persist, echo, or commit tokens.
- Do not persist the original outbound prompt. Results and artifacts can still be sensitive and require restrictive file permissions and retention.
- Never automatically resend a mutating A2A request after an ambiguous transport outcome. Reconcile only by saved task ID or exact request message identity, never context/unique-candidate inference; preserve `outcome_unknown` when evidence is ambiguous.
- Cancellation remains best-effort. Never claim that the underlying agent computation stopped unless an upstream protocol proves it.
- Preserve A2A v1 task and event shapes. Do not reintroduce the legacy stream field `final`.
- Codex App Server over stdio is the default inbound backend. CLI mode is an explicit compatibility path and may not take over a context that already owns an App Server thread.
- Preserve per-context serialization, bounded admission, and the one-active-writer assumption for each SQLite state file.
- SQLite migrations must be additive and backward-compatible. Never discard existing context, task, message, or event records.
- Live tests and `smoke` are opt-in, use harmless prompts, and never run in CI.
- The bundled Hermes `codex_a2a` plugin is the reliable Hermes → Codex client path. Keep its endpoint loopback-only, persist only task/context handles in `ctx.state`, submit with `returnImmediately`, and never resend after an ambiguous result. The built-in Hermes `a2a_call` remains synchronous.
- Execution preferences are inbound-only and require the negotiated Agent Card extension (`A2A-Extensions`, `message.extensions`, and `message.metadata.executionPreferences`). Query App Server `model/list`; receiver policy may narrow that catalog but must not invent support. Persist requested/effective decisions, send only `model` and `effort` to `turn/start`, and reject the extension in CLI mode.

## Compatibility policy

- Canonical distribution, executable, and Python namespace: `codex-a2a-gateway`, `codex-a2a-gateway`, and `codex_a2a_gateway`.
- The `codex-hermes-a2a-bridge` executable and legacy `HERMES_BRIDGE_*` / `CODEX_BRIDGE_*` environment variables are temporary v0.2 compatibility aliases. Canonical `CODEX_A2A_GATEWAY_*` values take precedence.
- Keep `HERMES_A2A_ENDPOINT`, `HERMES_A2A_TOKEN`, and `HERMES_A2A_CONVERSATION_DIR`; they describe the outbound Hermes adapter.
- Keep persisted and wire identifiers such as `bridge_task_id`, `BridgeService`, SQLite column names, and the seven `hermes_*` MCP tools unless a versioned migration is designed.
- If the new default state file is absent and the legacy state file exists, continue using the legacy file. Do not silently move or delete a live database.

## Development workflow

1. Inspect `git status` and preserve unrelated user changes.
2. Use CPython 3.11 and the project virtual environment.
3. Keep changes scoped to the requested behavior; avoid speculative protocol expansion.
4. Add tests for changes involving protocol shape, persistence, retry, timeout, idempotency, cancellation, restart, authentication, or network policy.
5. Update README, `.env.example`, Agent Card claims, relevant docs, and `CHANGELOG.md` whenever a public contract changes.
6. Do not rewrite historical test evidence to look current; add a new dated report instead.

## Required validation

Run from the repository root:

```bash
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest --cov=codex_a2a_gateway --cov-report=term-missing
```

Validate a release build in a fresh output directory:

```bash
build_dir=$(mktemp -d)
.venv/bin/python -m build --outdir "$build_dir"
.venv/bin/python -m twine check "$build_dir"/*
.venv/bin/python scripts/check_dist.py "$build_dir"
.venv/bin/python scripts/check_wheel_install.py "$build_dir"/*.whl
.venv/bin/python scripts/write_sha256sums.py "$build_dir"
.venv/bin/python scripts/write_sha256sums.py --check "$build_dir"
```

The clean-wheel check is required on both macOS and Linux CI. Keep deployment commands in `docs/deployment.md` installable without a Git clone, and never claim an operating system as supported until that path has passed CI or an equivalent clean-host test.

For App Server protocol changes, also run `scripts/check_app_server_schema.py` against the supported local Codex CLI. A live Hermes or Codex model task requires explicit operator authorization and must use a harmless, uniquely identifiable prompt.

## Definition of done

- Relevant tests and compatibility checks pass.
- The worktree diff contains no secrets, local paths, runtime databases, logs, transcripts, or stale build artifacts.
- Documentation and release notes match the implemented behavior and do not overstate conformance, cancellation, streaming, durability, or isolation.
- Rollback and migration instructions preserve user data and avoid running old and new MCP registrations against the same SQLite file concurrently.
