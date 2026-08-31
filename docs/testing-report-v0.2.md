# Testing report v0.2.0

Test date: 2026-09-01 (Asia/Ho_Chi_Minh)

Release source: `v0.2.0`

Result: **public-beta candidate passed**

## Environment

| Component | Version |
|---|---|
| macOS | 26.6.2 (25G83) |
| Python | 3.11.16 |
| MCP Python SDK | 2.1.1 |
| Codex CLI | 0.151.0-alpha.7.2 |
| Hermes Agent | 0.20.6 (2026.8.27) |
| Hermes local source | `4f225435`, upstream marker `d10ef89e` |
| Gateway | 0.2.0 |

Hermes was installed from a local Git checkout and reported that it was behind current upstream. The claims below therefore cover the installed 0.20.6 snapshot and the live wire results, not every newer Hermes commit.

## Automated verification

The release candidate was verified with:

```bash
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest --cov=codex_a2a_gateway --cov-report=term-missing
```

The suite completed with **34 passed** and **71.75% branch-aware coverage**, above the 65% gate. It includes fake-server, MCP stdio, A2A protocol, persistence, restart recovery, idempotency, timeout, cancellation, input-required, host/origin/media-type, request-limit, admission-limit, and backend regression coverage.

Release packaging was also built in a fresh temporary output directory and checked with Twine plus `scripts/check_dist.py`. Both the canonical `codex-a2a-gateway` executable and temporary `codex-hermes-a2a-bridge` compatibility alias were exercised with `--help`.

`scripts/check_app_server_schema.py` passed against the installed Codex CLI and confirmed that the App Server methods and notification shapes used by the adapter were present.

## Live direction 1: Codex/MCP adapter → Hermes

Hermes ran in the foreground with its native A2A platform on `127.0.0.1:9900`.

Read-only checks passed:

- `hermes gateway status` reported a running manual gateway.
- The canonical Agent Card advertised A2A 1.0 JSON-RPC, streaming, and the local default profile.
- `codex-a2a-gateway doctor` reported `ok: true`, a reachable Hermes health endpoint, and a parsed Agent Card.

One explicit harmless smoke request asked Hermes to return a fixed marker. The gateway returned:

```text
state: completed
result: V020_CODEX_TO_HERMES_OK
```

The response included durable local/A2A correlation identifiers. Those machine-specific identifiers are intentionally omitted from this public report.

## Live direction 2: A2A client → Codex App Server

The inbound gateway ran on `127.0.0.1:9910` with:

- Codex App Server over stdio;
- a temporary SQLite state file;
- a local test workspace;
- no CLI fallback.

Discovery and health passed. The Agent Card advertised A2A 1.0 JSON-RPC, streaming, text input/output, and `pushNotifications: false`.

### Synchronous task

A canonical `SendMessage` request completed with:

```text
TASK_STATE_COMPLETED
V020_HERMES_TO_CODEX_OK
```

### Same-context continuation

A second `SendMessage` using the same A2A `contextId` created another task while resuming the same Codex App Server thread. It completed with:

```text
TASK_STATE_COMPLETED
V020_CONTEXT_CONTINUED_OK
```

Passing `message.taskId` for an already completed task was rejected with JSON-RPC `-32602` and `INVALID_TASK_STATE`, as designed. `taskId` continuation is reserved for an input-required task; ordinary multi-turn conversation reuses `contextId` without reusing a completed task.

### Streaming task

`SendStreamingMessage` produced JSON-RPC-enveloped SSE frames in this order:

1. submitted task;
2. working status;
3. appendable artifact updates;
4. completed status.

The concatenated artifact text was:

```text
V020_STREAM_OK
```

Inbound App Server output may arrive as incremental artifact deltas. Clients must still treat this as A2A artifact/lifecycle streaming rather than depend on model-token boundaries.

## Live Hermes-native call → Codex

The configured Hermes peer `codex-bridge` already pointed to the inbound gateway. The Hermes `a2a` toolset was enabled for the CLI platform with:

```bash
hermes tools enable a2a --platform cli
```

A Hermes one-shot agent turn then used its native `a2a_call` tool to call the configured Codex peer. The final response was:

```text
V020_HERMES_NATIVE_TO_CODEX_OK
```

This verifies the intended real path Hermes Agent → native A2A client tool → `codex-a2a-gateway` → Codex App Server.

The Hermes inbound `a2a` platform was not granted the outbound `a2a` toolset, because automatic agent chaining was not required for this release test and can create unintended ping-pong loops.

## Not claimed by this report

- No remote/non-loopback deployment or TLS reverse proxy was exercised.
- No push-notification CRUD/webhook test was run; the Codex gateway does not advertise that capability.
- No live long-running cancellation job was created. Fake-server coverage verifies the gateway response while public semantics remain best-effort with `computationStopped: unknown`.
- No live approval or user-input interruption was intentionally triggered; the App Server request/response mapping is covered by deterministic tests.
- The official Python `a2a-sdk` was not installed in the release virtual environment. Wire interoperability was verified through canonical JSON-RPC/Agent Card requests and Hermes' native A2A client.
- No PyPI publication is claimed by this GitHub release.

## Conclusion

Both required directions passed on the same macOS host:

```text
Codex → MCP adapter → Hermes A2A
Hermes/native A2A client → gateway → Codex App Server
```

The result supports a **public beta** designation. The documented text-only input, push-notification, cancellation, upstream task-durability, one-writer SQLite, and single-user isolation limitations remain release constraints rather than future guarantees.
