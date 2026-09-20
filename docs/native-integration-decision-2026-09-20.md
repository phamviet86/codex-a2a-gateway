# Client–server feasibility decision — 2026-09-20

Status: proposed implementation scope based on
[live probe evidence](native-integration-probe-2026-09-20.md).
This document does not change the existing release contract or authorize a
production rollout. Updated after VPS CLI tests: native queue enables external
idle Desktop wake on the tested version; restart and ambiguous ACK guarantees
remain unverified.

## Decision

Proceed with a Desktop consultation client, durable Hermes broker, and a
version-gated native queue delivery adapter. An external `codex queue` invocation
successfully woke the idle local Desktop probe task. This supersedes the initial
NO-GO assessment, which overlooked the CLI queue command. Do not promise automatic
delivery to an unloaded/offline host or exactly-once native consumption.

## Concrete architecture

```text
Desktop -- MCP stdio --> local client daemon/inbox
                              |
                       HTTPS commands + SSE
                              |
                     server ledger + dispatcher
                              |
                         Hermes A2A
```

Use a local IPC boundary between MCP processes and the single client database
writer. Keep the server's Hermes adapter on its configured loopback endpoint.
LAN connectivity is a new authenticated transport, not relaxation of the legacy
model-facing endpoint policy. Keep App Server and client listeners private.

The initial deployment is one owner, multiple devices, one server instance,
SQLite on each client and a transactional server store. PostgreSQL is a reasonable
server target from the proposal, but was not benchmarked by these probes.
Preserve Python and existing protocol/recovery code where their contracts fit.

## Delivery behavior

1. Capture native identity from actual MCP request metadata, never model arguments.
   Pin supported binary/schema versions and reject missing/inconsistent metadata.
   Treat local metadata as host provenance, not remote device authentication.
2. Persist flow/operation identity before network dispatch. Server acceptance must
   atomically commit the accepted operation and dispatch outbox. Explicitly decide
   payload persistence: existing no-prompt-storage policy does not permit quietly
   adding a durable encrypted prompt queue.
3. Return within a configured bounded tool wait when possible. This preserves the
   exact pending tool consumer and was demonstrated end-to-end with Hermes.
4. If the result arrives after the tool wait, persist it independently and enqueue
   an authorized result reference to the originating thread through native queue.
   Local CLI queue and `thread/queue/add` both accepted submissions to a
   Desktop-owned thread without taking its writer lock. Use get/wait as an explicit
   fallback when the host is unavailable; never call that fallback idle-wake PASS.
5. Keep a separate `DesktopHostDelivery` interface implemented by a capability-
   checked native queue adapter. Capture returned queue IDs and delivery state.
   Same `clientUserMessageId` did not deduplicate two adds in the live probe.
   Do not use private IPC imitation, database edits, writer lock removal, or UI
   automation as the shipping delivery contract.
6. Optionally implement an `OwnedAppServer` execution adapter for gateway-owned
   tasks. Its native IDs and delayed tool completion were verified. It is a
   separate execution mode, not the user's originating Desktop conversation.

## Persistence and compatibility rules

- Separate execution state, delivery state, and reconciliation certainty.
- Device authorization scopes every flow/result/receipt; native IDs are local to
  the client and do not authorize server operations by themselves.
- Retain a stable gateway result ID for the exact operation. Store Hermes artifact
  IDs as snapshot provenance. The tested Hermes version regenerates them on reads.
- Redeliver stored events/results with the same identities. Do not resubmit A2A
  work to repair a lost receipt or transport ACK.
- Treat a changed terminal result snapshot as a conflict/versioned observation,
  not a reason to silently overwrite a previously consumed result.
- A local inbox dedup record cannot make an external Desktop queue insertion atomic.
  If host submission loses its ACK and no exact durable delivery lookup exists,
  retain `delivery_outcome_unknown`; do not automatically send the result again.
- Use streaming to obtain the remote handle promptly where verified. A completed
  response to one `returnImmediately` test alone does not establish nonblocking
  behavior; the streamed first-task event was actually observed.

## Implementation order

1. Versioned contracts and recorded capability fixtures, including MCP metadata.
2. Client daemon and broker with fake peers: accept/outbox/inbox/receipt atomicity,
   payload decision, authorization, idempotency and unknown-state handling.
3. Desktop MCP consultation, native queue delivery, and Hermes streaming/get
   adapter; test same-turn delivery, idle queue wake, and explicit pull fallback.
4. LAN TLS/pairing and two-device isolation tests, then file ingestion and retention.
5. Harden queue delivery with exact ACK lookup, active/idle/unloaded semantics and
   restart tests. Unknown enqueue outcomes must remain non-replayable unless exact
   native evidence supports reconciliation; a shared message ID alone is insufficient.

Implementation follow-up: retain and drain the Hermes submission stream while
execution is active. Live rollout testing on 2026-09-21 found cancellation/failure
on early disconnect despite the earlier timing-specific probe success. Saving
a remote handle permits exact reconciliation; it does not guarantee that the peer
keeps executing after its stream closes. See the [live report](testing-report-v0.6.0b1.md).

Preserve the legacy distribution, tools, state files, and safety boundaries until
a versioned migration is implemented. No existing database is rewritten by this
decision. Full release validation and clean-install CI remain required when code
changes ship.
