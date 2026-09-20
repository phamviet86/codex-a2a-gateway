# Client/server v0.7 implementation contract

Status: v0.7 release, 2026-09-21. This version retains the v0.6
client/server ledger contract and removes legacy commands and adapters. It does not claim exactly-once execution or
delivery. Initial topology: one owner, multiple devices, one broker process,
PostgreSQL ledger, client SQLite and private HTTPS transport.

## Ownership and boundaries

The broker owns `broker*.py` and broker tests. The client owns `client*.py`,
`native_delivery.py` and client/native tests. Release integration owns CLI,
packaging, shared documentation and installation scripts. The only commands are `broker`, `client`, `client-mcp` and
`client-doctor`; module entry points also work for deployments.

The client daemon is the only SQLite writer, protected by an exclusive process
lock. MCP processes communicate over a permission-restricted Unix socket. Native
thread IDs stay on the client; they never authorize broker access. Each configured
device has a different environment-provided bearer token. Broker authentication
maps tokens to device IDs before accessing any operation, event or artifact.
The production broker uses PostgreSQL and a single dispatcher. Test doubles may
use SQLite only if explicitly selected and never advertised as PostgreSQL proof.

## Broker HTTP v1

All endpoints except `/healthz` require device bearer authentication. JSON uses
snake_case. HTTPS is required except explicitly configured loopback development.
API prefix `/v1`. The broker generates no native Desktop messages.

- `POST /v1/operations`: `{operation_id, flow_id, prompt, artifact_ids: []}`.
  At most 16 artifact UUIDs may be attached. Client-generated UUIDs identify operation and flow. An accepted response is an
  operation snapshot (202; exact duplicate may be 200). Acceptance atomically
  persists payload and dispatch intent. Same device/operation_id with a different
  canonical request digest returns 409. A duplicate never resubmits Hermes work.
- `GET /v1/operations/{operation_id}` returns the same snapshot shape.
- `POST /v1/operations/{operation_id}/cancel`: best effort, no stop guarantee.
- `GET /v1/events?after=<sequence>`: resumable SSE with integer `id`, event
  `operation`, and `data` equal to the complete snapshot. `Last-Event-ID` is also
  accepted. Events are device scoped, durably ordered, and replayable. A retention
  gap returns an explicit error; clients recover via exact known operation GETs.
- `POST /v1/receipts`: `{operation_id, result_id, status}` with status
  `stored`, `delivered`, or `delivery_outcome_unknown`; idempotent. A stored receipt
  does not mean the Desktop consumed the result. Returns `{ok: true}`.
- `POST /v1/artifacts` uploads raw bytes with Content-Type and
  `X-Content-SHA256`, bounded size, authenticated scope, and safe inert storage.
  Returns `{artifact_id, sha256, size, content_type, expires_at}`. Download is
  `GET /v1/artifacts/{artifact_id}`. Never execute/render uploads. No arbitrary
  filesystem paths or URLs may be supplied to the broker. Enforce retention and
  device quotas; do not advertise antivirus scanning without a scanner.

Only validated UTF-8 `text/plain` uploads can be operation inputs in this beta.
The Hermes adapter includes their bounded content as explicitly delimited untrusted
reference text. Other MIME types remain inert upload/download objects and are
rejected as operation attachments before acceptance; general multimodal agent
interpretation is not supported.

An operation snapshot always includes `operation_id`, `flow_id`, `state`,
`result_id` (nullable), `result` (nullable), `error` (nullable), `remote_task_id`
(nullable), `created_at`, `updated_at`. States: `accepted`, `running`,
`completed`, `failed`, `canceled`, `outcome_unknown`. Result shape:
`{text, artifacts: []}`; each artifact descriptor uses the upload response shape.
Errors use `{code, message}`. Timestamps are UTC ISO8601. Result IDs are allocated
once per terminal operation and remain stable even if Hermes regenerates IDs.

`outcome_unknown` is an uncertainty state, not a definitive execution result.
It may reserve a result ID, then resolve through GET of the same saved nonempty
remote task ID to `completed`, `failed`, or `canceled`, preserving that result ID.
Only these definitive states trigger native delivery. Reading an unknown status
must not consume the eventual result. Definitive terminal snapshots are immutable;
other changed snapshots are conflicts. A missing remote handle is never inferred
from flow/context or resent to manufacture a result.

## Durability and native delivery

The approved new broker path may persist encrypted prompt payloads with an
environment-provided encryption key and explicit TTL. Digests support idempotency, but plaintext must not enter logs,
database columns or exception messages. Purge expired payloads and artifacts.

Claim dispatch before a mutating A2A call. Any crash/timeout after submission
may have started execution: recover by saved remote task ID only, otherwise keep
`outcome_unknown` and never replay. Stream the initial Task event to save the
handle early. Preserve gateway snapshots and IDs; no global content dedup.

Client persists an operation before POST and may retry the exact broker command
with its original ID. Inbox commit and SSE cursor advance are atomic. Result
delivery state is independent of execution state. Inline bounded MCP wait may
return a result. Otherwise queue only a result reference to the originating native
thread, directing the assistant to the explicit result tool. Keep queued text
free of untrusted result instructions.

Native identity comes from MCP request `_meta` with consistency checks between
`threadId` and `x-codex-turn-metadata.thread_id`, and corresponding turn identity.
Never accept a model-supplied target thread. Use a version/schema-checked
`thread/queue/add` adapter through an independent App Server process. Do not
resume the Desktop-owned thread. Persist queue intent before enqueue, returned
submission ID afterwards. A lost queue ACK becomes `delivery_outcome_unknown`;
same clientUserMessageId is NOT idempotent. Exact pending queue evidence may
reconcile, otherwise explicit pull remains available. No private IPC or desktop
database edits, transcript-based production lookup, UI workarounds or writer-lock
removal. Unloaded/offline host auto-wake is not guaranteed.

## Acceptance and release

Tests cover duplicate/conflicting IDs, cross-device denial, lost ACK, process
restart, replay cursor, TTL, quota, no blind resend, inline result and late queue
delivery. Release requires the repository validation suite, clean wheels on macOS
and Linux, live PostgreSQL, actual Hermes via loopback, authenticated HTTPS from
this Mac to hermes-local, and the installed Desktop MCP plus native queue proof.
Migration stops and removes obsolete operational services after backing up their
state. Never open archived legacy stores with the new runtime. Rollback preserves
all subsequent ledger/inbox records. Deployment must not restart unrelated tasks.

## v0.7 client transport and validation

The broker HTTP contract and `broker_v06_*` persisted identities remain unchanged.
The canonical package/CLI/MCP name is `hermes-a2a-gateway`; the Python namespace is
`hermes_a2a_gateway`. Only `HERMES_A2A_GATEWAY_*` configuration is accepted.
Legacy gateway modes and aliases are removed; old stores are archived separately.

`gateway_submit.wait_seconds` accepts finite values in [0, 60]. When omitted,
`CLIENT_INLINE_WAIT_SECONDS` supplies the default (15), not a smaller validation
ceiling. Invalid wait arguments are rejected before creating an operation and return
a specific safe validation error. `gateway_wait.timeout` also accepts [0, 60].

SSH transport is a daemon lifecycle concern, not a new A2A operation. It retains TLS,
device authentication, exact native origin binding, persisted cursors and operation
identity. SSH reconnect must not imply resending ambiguous Hermes work or native
queue insertion. Direct mode remains the default. See [SSH transport](ssh-tunnel.vi.md).
