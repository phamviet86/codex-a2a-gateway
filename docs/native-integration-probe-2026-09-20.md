# Native Desktop integration probe — 2026-09-20

> Implementation follow-up on 2026-09-21 found that the deployed Hermes stream
> handler can fail tasks with `[client disconnected]` when the client closes SSE
> early. The successful early-close experiment below was timing dependent and
> must not be generalized into a transport contract. See the
> [v0.6 live validation report](testing-report-v0.6.0b1.md).

> Updated by the VPS/queue follow-up below: the earlier external-Desktop-wake
> NO-GO finding is superseded. Native `codex queue` successfully woke the local
> Desktop test task. Queue deduplication and ambiguous ACK recovery remain gaps.

## Scope and environment

Operator-authorized live test using one disposable, projectless Desktop task and
harmless fixed-text prompts. Hermes and gateway transport were simulated by a late
result message; no Hermes request, network configuration change, daemon restart,
or production-task mutation was performed.

Tested installed Codex CLI: `0.155.0-alpha.9.2` on macOS. The project virtual
environment was absent; the schema check used available CPython 3.11. No claim is
made that the repository's full validation suite ran.

## Observed results

| Probe | Actual result | Interpretation |
| --- | --- | --- |
| Create a task through Desktop host tools; wait for completion | PASS: initial turn completed | Establishes a Desktop-owned test origin |
| Start independent App Server stdio and read exact test thread with `includeTurns` | PASS: exact initial completed turn returned; status `notLoaded` in independent process | Persisted history is readable; this is not attachment to the owning runtime |
| Deliver a simulated late result through Desktop `send_message_to_thread` after initial completion | PASS: a new completed turn in the same thread returned the expected history-dependent marker | Host-mediated continuation works for this installed app and exposed tool |
| Independent App Server `thread/resume` of the same idle Desktop thread | REJECTED: `-32600`, `already has an active writer` | The Desktop retains writer ownership even while the task is idle; external `turn/start` was not attempted after rejection |
| Default `codex app-server proxy` connection | UNAVAILABLE: default control socket absent | No attachment through the default documented proxy endpoint on this installation |
| Generate experimental schema from installed CLI | PASS | Version-specific schema available; does not prove runtime delivery behavior |
| `scripts/check_app_server_schema.py` | PASS | Existing adapter's minimal schema expectations satisfied |

The host late-result test used one flow reference and one result reference. It did
not test duplicate delivery or prove atomic consumption. No arbitrary IPC socket
was contacted, and no writer lock was bypassed. An idle thread is not evidence
that its writer ownership has been released.

## Architecture consequences

1. Keep the MCP request facade distinct from native result delivery.
2. Define a host delivery adapter: exact origin binding, authorized enqueue into
   the owning host, delivery identity, read-only reconciliation, and explicit
   unsupported/blocked states.
3. Do not implement Desktop continuation by launching an independent App Server
   against the same thread. Keep that adapter for threads it owns.
4. Treat the successful Desktop tool as host-internal capability evidence, not
   proof that a standalone gateway daemon has a supported callable endpoint.
5. Require separate validation of the external gateway-to-host entry point before
   claiming automatic late-result delivery as a shipped feature.

## Still unverified

- Obtaining trusted thread/turn/tool-call identity from an actual MCP request.
- Standalone daemon access to a supported host continuation interface.
- Delayed response to a still-pending native tool call.
- Duplicate result suppression, lost delivery ACK, and durable consumption proof.
- Active-turn routing, concurrent flows, and wrong-origin rejection.
- Desktop exit/restart, sleep/reconnect, approvals, and process-epoch changes.
- Real Hermes execution and end-to-end gateway transport.

Do not generalize these observations to other Codex releases or operating systems.
The test task is retained in Desktop for inspection. No production registration
or runtime configuration was modified.

## References

- [Official App Server documentation](https://learn.chatgpt.com/docs/app-server)
- Installed CLI help for `app-server`, `app-server proxy`, and schema generation
- Repository `scripts/check_app_server_schema.py`

## Follow-up live probes

The operator subsequently authorized continued tests, disposable Desktop tasks,
Computer Use, and bounded experiments on `hermes-local`. The following evidence
extends, rather than replaces, the initial observations above.

Hermes reported `0.21.3`, upstream `490e6b59`, Python `3.12.14`. Its existing A2A
listener advertised protocol 1.0 and streaming. No VPS service was restarted,
upgraded, reconfigured, or exposed. SSH was an experimental access path to its
loopback listener, not validation of the proposed LAN HTTPS transport.

| Probe | Actual result |
| --- | --- |
| Desktop native exec result delayed 12 seconds | PASS: the same turn consumed the delayed result |
| App Server-owned dynamic tool, response delayed 8 seconds | PASS: server supplied matching `threadId`, `turnId`, `callId`; exactly one observed tool call completed |
| Restart the probe-owned App Server after completion | PASS: a new process read exactly the saved completed turn without `thread/resume` or `turn/start` |
| MCP metadata in an App Server-owned thread | PASS: request `_meta` included `threadId`, `callId`, and `x-codex-turn-metadata.turn_id`; these environment variables were absent |
| MCP metadata in a real Desktop-created task | PASS: all three IDs matched Desktop host readback exactly |
| Live Hermes `SendMessage` and subsequent `GetTask` via a new SSH connection | PASS: same task and expected harmless text; no resend |
| Close Hermes SSE after its first submitted-task event, then `GetTask` | PASS: exact task completed and expected result was recoverable without resend |
| Repeat `GetTask` on one completed Hermes task | Compatibility defect: text/task stayed equal, but artifact IDs changed on every observed snapshot |
| Deliver real Hermes result through Desktop host tools after idle | PASS: same Desktop task consumed the result |
| Repeat the same host delivery payload and `result_id` | A second completed turn was created; host tool does not deduplicate that result reference |
| Desktop → temporary MCP → SSH → Hermes A2A SSE → same Desktop turn | PASS: submitted, working, artifact, completed events observed; exact native IDs matched host readback; expected final marker returned |
| Computer Use access to Desktop UI | BLOCKED by the Computer Use tool's app safety policy; no alternate UI mechanism attempted |

The end-to-end test is a real model-to-model tool flow, but not an implementation
of the future gateway ledger, authentication, or LAN HTTPS services. It performed
one submission and no mutating retry. Completion took approximately 17 seconds
for the Desktop turn; this is a single functional observation, not a benchmark.

Temporary MCP registrations were added with `codex mcp add`, used only by probe
tasks, removed with `codex mcp remove`, and their absence checked afterward.
Existing MCP registrations were preserved. Test tasks remain available for review.
Probe files and synthetic wire evidence were staged outside the repository.

An initial owned-thread MCP harness stalled because it did not handle the native
MCP approval request. It timed out; exact readback showed an interrupted turn and
a cancelled tool call, with no probe execution evidence. A corrected harness
handled `mcpServer/elicitation/request` using a one-call `accept` response for the
operator-authorized harmless tool; it did not persist an approval or disable a
policy. The corrected MCP probe passed. This harness failure is not an MCP
transport failure.

Installed Hermes source corroborated the changing artifact IDs: `build_task`
creates new message/artifact UUIDs while rendering snapshots. Preserve peer IDs
as provenance, but use a separately persisted gateway result identity. Do not
globally deduplicate unrelated tasks by equal text or content digest.

## Feasibility gate disposition

- **PASS:** Desktop MCP request identity, pending tool completion, live Hermes
  response, exact-handle recovery after stream disconnect, and host-mediated
  late continuation.
- **NO-GO for the full standalone Desktop-wake claim:** no supported external
  daemon-to-owning-Desktop continuation endpoint was established. A separate
  App Server cannot acquire the Desktop thread writer. Host tools are not proof
  of an externally callable API.
- **Source-backed limitation:** official background-hook documentation says an
  idle session waits for the next user turn; hook completion does not start one.
  Therefore background hooks alone do not meet autonomous idle-wake acceptance.
- **Not production acceptance:** approval/restart recovery during execution,
  lost native delivery ACK, cross-device authorization, concurrent gateway flows,
  artifacts, TLS, and quotas require an implemented gateway and isolated fault
  environment. No live Desktop/Hermes service crash was induced. There is no
  claim that all roadmap tests passed.

These checks are sufficient for an architecture go/no-go decision, not for
declaring the original full product complete. See the concrete proposed scope in
[the feasibility decision](native-integration-decision-2026-09-20.md).

Additional primary reference: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
especially background delivery and native tool identity. Public pages may describe
newer behavior; observed MCP metadata is version-specific evidence, not a promise
of a stable cross-version API.

## VPS CLI and native queue follow-up

The operator explicitly requested testing the VPS Codex CLI. It was installed at
its recorded absolute executable path but absent from the non-login SSH PATH.
Version: `0.154.0`. `app-server daemon version` reported a running managed daemon
and control socket. No daemon restart, installation or authentication change was
performed.

Discovery of `codex queue --thread <UUID> --message <text>` corrected a material
omission in the earlier feasibility investigation:

- On the personal computer, an external shell invoked `codex queue` for the idle
  Desktop probe task. It returned a queue ID. Without a host send-message call,
  the same task started a new turn and returned the expected marker. **PASS:
  externally initiated idle Desktop wake on the installed version.**
- A VPS `codex exec` probe created a disposable session and completed normally.
  A queued late result for that initially `notLoaded` session was accepted, but
  autonomous execution before host attachment was not established. The host was
  then explicitly attached using its supported send-message tool.
- After host attachment and return to idle, a second VPS `codex queue` message
  started and completed a new turn in the same session. Exact test-session
  history contained the expected late-result and loaded-host markers. Remote
  host readback reported completed turns but omitted their items; targeted
  synthetic-session history inspection supplied the output evidence. Transcript
  parsing is diagnostic evidence only, not a proposed production delivery API.
- Local App Server `thread/queue/add` accepted an operation for the Desktop-owned
  task without `thread/resume`, despite the earlier writer-ownership rejection.
  Two identical adds with the same `clientUserMessageId` returned **different
  queued submission IDs**. This field must not be assumed to be an idempotency key.
- Generated VPS schemas expose `thread/queue/add`, list, update, delete, reorder,
  and start shapes. Their presence is not proof of stable semantics or successful
  reconnect/dedup after consumption.

Two direct VPS control-transport probes did not initialize: JSONL through proxy
timed out; generic Unix WebSocket handshake closed without an HTTP response.
Neither reached task creation. The interim assumption that this installed control
socket simply required a generic WebSocket handshake was not validated. Native
CLI queue was the successful version-compatible entry point; no custom transport
or security bypass was attempted.

Revised gate: **GO for a version-gated native queue delivery adapter.** Preserve
`delivery_outcome_unknown` after ambiguous enqueue; do not promise exactly-once
native consumption. Unloaded/offline host behavior, queue persistence across
host restart, active-turn ordering, and consumed-message ACK reconciliation still
require isolated implementation tests. A VPS CLI session is not the personal
Desktop conversation; the separate local Desktop queue test is what establishes
the relevant originating-Desktop behavior.
