# Hermes A2A Gateway

[![CI](https://github.com/phamviet86/hermes-a2a-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/phamviet86/hermes-a2a-gateway/actions/workflows/ci.yml)

English | [Tiếng Việt](README.vi.md)

Hermes A2A Gateway connects Codex Desktop to Hermes on a private server. The client
owns a durable local inbox and optional SSH tunnel; the broker owns a PostgreSQL
ledger and dispatches work to the server's loopback Hermes A2A endpoint.

**Release: v0.7.0.** Package, command and MCP registration are now
`hermes-a2a-gateway`. The Python namespace is `hermes_a2a_gateway` and configuration
uses `HERMES_A2A_GATEWAY_*`. This release removes the old local gateway modes,
legacy executable aliases, setup skills and Hermes → Codex plugin.

```text
Codex Desktop -- MCP --> client daemon + SQLite inbox
                            | direct HTTPS or HTTPS inside owned SSH tunnel
                       TLS proxy --> broker + PostgreSQL
                                         | loopback A2A
                                       Hermes
```

This is an independent community project, not an official OpenAI or Nous Research
product. Codex Desktop is the first verified integration; other runtime adapters
and arbitrary Hermes-initiated Desktop jobs are not implemented.

## Install and use

Install the same release wheel on both machines; a Git clone is unnecessary.
Start with the [release download/checksum instructions](docs/deployment.md#install-the-same-release-on-both-machines).

| Machine or task | Guide |
| --- | --- |
| Private server running Hermes | [Server installation and operation (Vietnamese)](docs/server-hermes.vi.md) |
| Workstation running Codex Desktop | [Client installation and five-tool usage (Vietnamese)](docs/client-codex.vi.md) |
| Direct LAN/VPN or managed SSH connection | [SSH configuration, retries and diagnostics (Vietnamese)](docs/ssh-tunnel.vi.md) |
| Existing v0.6 or legacy installation | [Migration, removal and rollback](docs/migration-v0.7.md) |

Only four commands are installed:

```text
hermes-a2a-gateway broker
hermes-a2a-gateway client
hermes-a2a-gateway client-mcp
hermes-a2a-gateway client-doctor
```

Configure the private client launcher as documented, then register its MCP facade:

```bash
codex mcp add hermes-a2a-gateway -- \
  "$HOME/.config/hermes-a2a-gateway/launch" client-mcp
codex mcp get hermes-a2a-gateway
```

Set `tool_timeout_sec = 90` in the existing `[mcp_servers.hermes-a2a-gateway]`
Codex configuration table, as shown in the [client guide](docs/client-codex.vi.md).

The daemon must be running before using tools. SSH belongs to the daemon, not to
individual MCP calls or Desktop tasks. Server credentials and encryption keys stay
in protected configuration, outside MCP arguments.

| MCP tool | Purpose |
| --- | --- |
| `gateway_submit` | Submit once and return an operation handle; explicit wait 0–60 seconds |
| `gateway_get` | Retrieve an existing operation/result in its originating Desktop task |
| `gateway_wait` | Wait up to 60 seconds on the same operation without resubmitting |
| `gateway_cancel` | Request best-effort cancellation |
| `gateway_upload_artifact` | Upload an explicitly selected regular file |

For long work, submit with `wait_seconds: 0`, keep `operation_id`, and get/wait on
that handle. Explicit waits such as `20` are valid even when the configured default
is `15`. Only UTF-8 `text/plain` attachments are passed to Hermes in this profile;
local repository files are not automatically synchronized.

## Durability and limits

- TLS verification, device bearer authentication and loopback Hermes are required
  for remote deployment. SSH mode additionally verifies host keys and binds its
  local forward to loopback. It never disables HTTPS to recover from an error.
- Network reconnect uses bounded retries. SSE resumes from a persisted cursor and
  operations are reconciled by exact identities. An ambiguous Hermes mutation or
  native queue insertion is never blindly repeated.
- A late result may queue a reference in its original Desktop task. Queue ACK is
  not proof of consumption. Offline/unloaded-host wake is not guaranteed; use
  `gateway_get`/`gateway_wait` when necessary.
- One owner, separately authorized devices, one broker dispatcher, one daemon per
  inbox. This is not a public multi-tenant service or an HA cluster.
- Payloads are encrypted with external keys and expire. Default payload/artifact TTL
  is one day; result/event retention is seven days. Keep backups and keys together
  under a separate retention policy. Do not replace an existing encryption key.
- Cancellation is best-effort. Human-input continuation through the broker is not
  implemented. Native delivery is version/schema gated.
- CPython 3.11 and clean wheels are tested on macOS/Linux. The Unix-socket client
  does not support Windows. Native Desktop behavior must be verified separately
  from package installation or simulated reconnect tests.

See the [wire contract](docs/client-server-contract.md), [deployment reference](docs/deployment.md),
[release notes](docs/release-notes.md), [v0.7 verification](docs/testing-report-v0.7.0.md),
and [historical evidence](docs/history/README.md).

## Development

```bash
git clone https://github.com/phamviet86/hermes-a2a-gateway.git
cd hermes-a2a-gateway
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
# Set HERMES_A2A_GATEWAY_TEST_POSTGRES_DSN to a disposable PostgreSQL 16 database.
.venv/bin/pytest --cov=hermes_a2a_gateway --cov-report=term-missing
```

Tests use fake peers; live model tasks are opt-in and never run in CI. See
[AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md),
and [SECURITY.md](SECURITY.md). Licensed under [Apache-2.0](LICENSE).
