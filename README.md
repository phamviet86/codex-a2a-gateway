# Codex A2A Gateway

[![CI](https://github.com/phamviet86/codex-a2a-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/phamviet86/codex-a2a-gateway/actions/workflows/ci.yml)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/)
[![A2A 1.0](https://img.shields.io/badge/A2A-1.0-6f42c1.svg)](https://a2a-protocol.org/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)

**Public beta · v0.2.2**

English | [Tiếng Việt](README.vi.md)

`codex-a2a-gateway` is a local bidirectional gateway that lets Codex participate in A2A v1.0 workflows even though Codex does not expose a native A2A endpoint.

- **Codex → A2A:** Codex calls seven MCP stdio tools that delegate to the local Hermes A2A peer.
- **A2A → Codex:** Hermes or another A2A v1.0 client calls an HTTP/SSE gateway backed by Codex App Server.

Hermes Agent is the first verified peer, not the product boundary. The inbound endpoint uses portable A2A v1.0 operations and can be called by other compliant clients.

> **Independent community project:** this software is not an official OpenAI/Codex or Nous Research/Hermes Agent product and is not endorsed by either organization. Product names are used only to describe interoperability.

## Architecture

```text
Codex client --MCP stdio--> outbound adapter --> local Hermes A2A :9900
A2A v1 client --HTTP/SSE--> inbound gateway --> Codex App Server stdio
                    \--------------------------> SQLite mappings/tasks
```

The MCP server and inbound gateway are separate processes. They share durable SQLite mappings between A2A contexts/tasks and Codex conversations/threads.

## Current capabilities

- A2A v1 Agent Card and JSON-RPC `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, and `CancelTask`.
- SSE lifecycle streaming with task, status, and artifact updates.
- Durable context, task, message, thread, and turn correlation in local SQLite.
- Local idempotency and conservative recovery after ambiguous outbound timeouts.
- App Server approval and user-input requests mapped to `TASK_STATE_INPUT_REQUIRED`.
- Loopback defaults, bearer authentication for non-loopback inbound exposure, request limits, and bounded admission.
- Explicit CLI compatibility backend using `codex exec --json`.

Known limitations:

- Inbound requests support text parts only.
- No inbound push-notification CRUD or webhook delivery; the Agent Card advertises `pushNotifications: false`.
- Streaming uses A2A lifecycle/artifact events; inbound artifact deltas may be incremental, but token boundaries are not guaranteed.
- Cancellation is best-effort and never proves that upstream computation stopped.
- Hermes task storage is currently in memory; the gateway uses conservative local recovery after a Hermes restart.
- One active writer/process set should own a SQLite state file. This is a local single-user integration, not a multi-tenant isolation boundary.

## Compatibility

| Component | Supported/tested |
|---|---|
| Python | CPython `>=3.11,<3.12` |
| A2A | v1.0 JSON-RPC and SSE |
| Codex | App Server over stdio by default; CLI fallback is explicit |
| Hermes Agent | Live-tested with `0.20.6` on macOS |
| MCP | Local stdio server |

The release wheel and clean-install path are tested in CI on macOS and Linux. Windows/WSL is not yet verified. See [deploying on another computer](docs/deployment.md) for the exact support boundary.

The App Server backend follows the official [Codex App Server protocol](https://learn.chatgpt.com/docs/app-server): initialize once, start or resume a thread, start a turn, and consume streamed notifications. WebSocket App Server transport is not used by this project.

## Install

For an operator machine, install the release wheel into a dedicated virtual environment without cloning the repository:

```bash
python3.11 -m venv "$HOME/.local/share/codex-a2a-gateway/venv"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install \
  "https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.2.2/codex_a2a_gateway-0.2.2-py3-none-any.whl"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/codex-a2a-gateway" --version
```

See the complete [deployment guide](docs/deployment.md) for prerequisites, MCP registration, Hermes setup, state migration, upgrades, rollback, and uninstall.

For a source checkout or contributor environment:

```bash
git clone https://github.com/phamviet86/codex-a2a-gateway.git
cd codex-a2a-gateway
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/codex-a2a-gateway --help
```

Contributors should install development tools with:

```bash
.venv/bin/python -m pip install -e '.[dev]'
```

## Quickstart 1: Codex → Hermes

These quickstarts assume the release-wheel installation above. Define the installed paths once in each shell:

```bash
gateway_venv="$HOME/.local/share/codex-a2a-gateway/venv"
gateway_bin="$gateway_venv/bin/codex-a2a-gateway"
```

For a source checkout, use its `.venv/bin/codex-a2a-gateway` explicitly instead; do not mix the two installations against one state file.

Enable the native Hermes A2A platform using the current [Hermes A2A guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a):

```bash
hermes gateway setup   # select A2A when prompted
hermes gateway run     # foreground process on 127.0.0.1:9900
```

In another terminal, verify the peer and register the MCP server:

```bash
"$gateway_bin" doctor

codex mcp add codex-a2a-gateway -- \
  "$gateway_bin" serve
codex mcp get codex-a2a-gateway
```

Restart or open a new Codex client so it loads the MCP entry. A typical agent workflow is:

1. Call `hermes_status`.
2. Call `hermes_chat` with a stable `conversation_key`.
3. If the task is still active, call `hermes_task_wait` or `hermes_task_get` instead of resending it.
4. Continue the conversation with the same `conversation_key` or returned `context_id`.

The MCP stdio process writes protocol frames to stdout and diagnostics to stderr.

## Quickstart 2: Hermes or A2A → Codex

Start the inbound gateway against the workspace Codex should operate on:

```bash
CODEX_WORKSPACE_ROOT=/absolute/path/to/workspace \
  "$gateway_bin" gateway
```

Confirm discovery:

```bash
curl --fail http://127.0.0.1:9910/.well-known/agent-card.json
```

Send one harmless A2A task:

```bash
curl --fail-with-body http://127.0.0.1:9910/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"quickstart-1","method":"SendMessage","params":{"message":{"messageId":"quickstart-message-1","role":"ROLE_USER","parts":[{"text":"Reply with exactly CODEX_A2A_OK"}]}}}'
```

To let Hermes call Codex using Hermes' native outbound A2A tools:

```bash
hermes tools enable a2a --platform cli
```

Add a peer to `~/.hermes/config.yaml`:

```yaml
a2a_agents:
  codex:
    url: "http://127.0.0.1:9910"
    timeout: 300
```

Do not enable `a2a` tools on the inbound Hermes `a2a` platform unless agent chaining is intentional; both systems have anti-loop limits, but an explicit topology is safer.

## Sessions and tasks

Outbound calls map a stable Codex `conversation_key` to a Hermes A2A `contextId`. Inbound calls map an A2A `contextId` and task to a Codex App Server thread and turn. Follow-up messages reuse these mappings, and task status remains queryable through SQLite across gateway restarts.

For a mutating outbound request, provide an `idempotency_key`. If a network timeout leaves the result ambiguous, query or wait on the existing task. The gateway deliberately does not blindly resend.

See [architecture v0.2](docs/architecture-v0.2.md), the [inbound operations guide](docs/inbound-gateway.md), and the runnable generic-client lifecycle in the [deployment guide](docs/deployment.md#5-generic-a2a-client-lifecycle).

## Outbound MCP tools

| Tool | Purpose |
|---|---|
| `hermes_status` | Check persistence, Hermes health, and Agent Card discovery. |
| `hermes_chat` | Start or continue a conversation in `auto`, `sync`, or `async` mode. |
| `hermes_task_get` | Reconcile and return task state, result, error, or input request. |
| `hermes_tasks_list` | List durable tasks by conversation and state. |
| `hermes_task_wait` | Wait through active SSE, subscription, then polling fallback. |
| `hermes_task_cancel` | Request best-effort cancellation. |
| `hermes_contexts` | List, inspect, or close local context mappings. |

These tools expose conversation and task operations only. They do not expose Hermes administration, shell, plugin, model, or service controls.

## Configuration

Safety-critical settings:

| Variable | Default | Purpose |
|---|---:|---|
| `HERMES_A2A_ENDPOINT` | `http://127.0.0.1:9900` | Outbound Hermes A2A root; loopback only. |
| `HERMES_A2A_TOKEN` | empty | Optional outbound bearer token, read from env only. |
| `CODEX_A2A_GATEWAY_STATE_PATH` | platform state directory | SQLite file; mode `0600`. |
| `CODEX_A2A_GATEWAY_MAX_TURNS` | `5` | Per-context anti-loop budget. |
| `CODEX_A2A_GATEWAY_MAX_CONCURRENCY` | `4` | Per-process execution limit. The MCP adapter and inbound gateway are separate processes, so this is not a global cap across both. |
| `CODEX_A2A_HOST` / `CODEX_A2A_PORT` | `127.0.0.1` / `9910` | Inbound bind. |
| `CODEX_A2A_BEARER_TOKEN` | empty | Required before a non-loopback bind/public URL. |
| `CODEX_A2A_GATEWAY_BACKEND` | `app-server` | `app-server` or explicit `cli`. |
| `CODEX_A2A_GATEWAY_CLI_FALLBACK` | `false` | Limited fallback for a new context only. |
| `CODEX_WORKSPACE_ROOT` | current directory | Workspace used by Codex. |
| `CODEX_A2A_GATEWAY_APPROVAL_POLICY` | `never` | `never`, `untrusted`, or `on-request`. |

See [.env.example](.env.example) for the complete operator surface. Legacy `HERMES_BRIDGE_*` and `CODEX_BRIDGE_*` variables remain lower-priority compatibility aliases for v0.2. The legacy executable `codex-hermes-a2a-bridge` also remains as a temporary alias.

## Security and privacy

- Outbound URLs and discovered interfaces must remain loopback-only and redirects are not followed.
- A non-loopback inbound host or public URL requires a bearer token; use TLS at a trusted reverse proxy for any remote deployment.
- Tokens are read from environment variables and compared without writing them to logs.
- Original outbound prompts are not stored by the gateway. SQLite does store results, artifacts, mappings, status, and minimal error data, which may still be sensitive.
- Codex and Hermes can maintain their own session, audit, and conversation records.
- Never publish tokens, transcripts, SQLite files, or private workspace paths in issues.

Report vulnerabilities through GitHub private vulnerability reporting as described in [SECURITY.md](SECURITY.md).

## Development and verification

```bash
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest --cov=codex_a2a_gateway --cov-report=term-missing
```

Default tests use an ephemeral fake A2A server and do not require Hermes or a live model. `doctor` is read-only; `smoke` sends a real Hermes task and must be run intentionally with harmless content.

Current release evidence is in [testing report v0.2](docs/testing-report-v0.2.md). Coding agents must also follow [AGENTS.md](AGENTS.md).

## Migration from the old name

Install the renamed project, add the new MCP entry, verify it, then remove the old entry. Do not leave both active against the same state database.

```bash
.venv/bin/python -m pip install -e .
codex mcp add codex-a2a-gateway -- \
  /absolute/path/to/codex-a2a-gateway/.venv/bin/codex-a2a-gateway serve
codex mcp get codex-a2a-gateway
codex mcp remove codex-hermes-a2a-bridge
```

The gateway uses the old state file automatically when it exists and the new default file has not been created. It never silently moves or deletes that data.

## Documentation

- [Vietnamese README](README.vi.md)
- [Deploy on another computer](docs/deployment.md)
- [Architecture v0.2](docs/architecture-v0.2.md)
- [Inbound gateway operations](docs/inbound-gateway.md)
- [Hermes A2A reference](docs/hermes-a2a-reference.md)
- [Testing report v0.2](docs/testing-report-v0.2.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Apache License 2.0](LICENSE)

Primary references: [Codex App Server](https://learn.chatgpt.com/docs/app-server), [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [Hermes A2A](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a), and the [A2A protocol](https://a2a-protocol.org/).
