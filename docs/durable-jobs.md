# Durable jobs and return delivery (unreleased)

This contract supersedes the v0.3.0 recovery descriptions for a checkout containing
these changes. It has local fake-peer coverage; it is not a live deployment claim.

## Deployment and roles

Run Codex, Hermes Agent and the gateway on one execution host/VPS. A2A stays on
loopback. Codex Desktop on a personal computer connects to that host through Codex
Remote. The Desktop conversation is not the gateway's worker App Server thread.
No VPN, public A2A port or direct remote Hermes endpoint is needed.

Codex can do everyday work itself and consult Hermes when expert advice is useful.
Only the active Hermes `default` profile is routable; a requested specialty is an
instruction to that agent, not evidence that a named profile was selected. Hermes
can independently ask Codex to search, extract documents, use a browser, retrieve
data, edit files or run system operations within the worker's actual permissions.
The health response reports worker tool availability as `unverified`; it does not
inherit the Desktop's tools or plugins. The App Server worker receives instructions
to report unavailable tools, completed actions and artifact paths honestly.

Gateway-created Codex processes inherit `CODEX_A2A_GATEWAY_WORKER_TASK_ID`. An
outbound MCP instance inheriting that marker refuses `hermes_chat`, preventing the
worker from bouncing its job back to Hermes. This covers the bundled path, not
arbitrary third-party clients that discard the marker or provenance.

## Identity before sending

Outbound `hermes_chat(mode="async")` commits a local handle, message identity,
fingerprint and optional `origin` before discovery/network I/O. Async mode does
not wait for discovery or a remote ACK. The worker continues independently of the
caller wait. Keep `bridge_task_id` and a caller-chosen `idempotency_key`; retrying
an identical key returns the saved job, including when no context was supplied.
Prompts are not stored. Results, artifacts and opaque origin handles are stored.

`origin` accepts only `conversation_id`, `question_id` and `parent_job_id` strings
(up to 256 characters each). Supply identifiers, not question text or secrets.
They are attribution supplied by the caller, not authenticated delivery endpoints.
They are returned with results and must remain unchanged during a continuation.
The gateway cannot infer an originating Desktop conversation from MCP stdio.

The bundled Hermes plugin records a local handle and stable message identity in
`ctx.state` before `SendMessage(returnImmediately=true)`. Supply `message_id` to
deduplicate exact tool invocations locally. Reusing it for another request is an
error. Each continuation has a new message identity but keeps its job handle;
prior attempt fingerprints remain available for duplicate suppression. The plugin
stores handles, origins and receipt IDs, never result/artifact contents. At 200
handles it refuses new jobs instead of silently evicting recovery information.
Durability depends on the Hermes implementation persisting `ctx.state` as promised.

## Wait, restart and exact recovery

A caller wait expiring returns `timed_out` and preserves the last known task state.
Transport uncertainty is `outcome_unknown`; it is not proof of failure or permission
to repeat a mutation. Configured backend/stream execution deadlines are separate
from caller waits. A backend deadline may terminate the local subprocess; the
side-effect outcome can still be unknown.

Outbound restart converts interrupted queued/submitted/working jobs to unknown.
No prompt is replayed automatically. A known A2A task ID is read directly. If it is
missing at the peer, the saved binding remains and the result is unknown. Recovery
without an ACK requires matching context **and** `metadata.requestMessageId` equal
to the saved outbound message ID. All returned pages are checked (bounded at 100
pages); duplicate candidates, repeated cursors or an incomplete scan do not establish
uniqueness. A task already bound to another job is never reassigned. A sole task in
a context, similar prompt, nearby timestamp or shared conversation is insufficient.

The local Hermes JSONL fallback also requires the user record's exact `message_id`
(or `messageId`) and an agent record's explicit `task_state`. Legacy records with
only time/role/text/task ID cannot provide this guarantee. Unmodified peers lacking
these fields may remain unknown after losing an ACK or their in-memory task. This
change does not patch or restart Hermes to manufacture peer support.

Inbound persistence commits each message and job before scheduling its worker.
Tasks that entered the backend become unknown after restart. `GetTask` and inbound
wait can read App Server `thread/read(includeTurns=true)` using the **saved thread
and acknowledged turn ID**, accepting only that exact terminal turn with full items.
Recovery does not call `thread/resume` or `turn/start`. Missing turn ID, unavailable
history, partial history, CLI history or an in-progress turn remain unknown. This
is schema-checked and fake-tested, not live verified. The pre-turn-ACK gap is not
claimed solved by the existence of `clientUserMessageId` in a schema.

An inbound job proven queued before backend entry can be restarted only by an
explicit replay of its identical message. In the Hermes plugin, repeat the original
arguments including `message_id`, adding `resume: true`. The plugin first checks the
receiver's exact request identity and `gateway_restarted_before_start` or
`context_blocked_before_start` evidence; otherwise it refuses to resend. Ordinary
retries, get, wait and result acknowledgements do not execute the request again.

## Independent jobs and input

Use separate contexts for independent jobs, even when their origin conversation is
the same. Outbound rejects a new turn on an unresolved context with `context_busy`.
Inbound submission returns a queued handle while another turn owns that context;
execution is serialized across the whole turn. A prior uncertain execution blocks
later work in that context. Independent contexts can execute concurrently.

Continue an outbound input-required job using `hermes_chat(task_id=<bridge handle>,
message=<answer>, idempotency_key=<new attempt>)`. The same A2A taskId is sent and
the same local job is retained. Context-only calls remain valid for follow-ups after
a completed turn, but cannot silently replace an input-required job.

Hermes uses `codex_a2a_call(task_id=<local handle>, message=<answer>,
message_id=<new attempt>)`. The gateway retains the A2A job and Codex thread and
starts a new Codex turn with the answer. App Server approval/input RPCs are currently
cancelled/answered empty before that process closes; this is **not** reattachment to
the original suspended RPC. Questions are returned to the caller. The worker must
inspect its prior context and avoid repeating completed mutations. Exact continuation
of a suspended native tool call across process restart is not implemented.

## Return delivery and consumption

Results return to the invoking tool call or a later get/wait using the durable
handle. A terminal/input-required result has a stable `result_id` (A2A metadata
`resultId`), derived from job, attempt and result contents, not poll timestamps.
The caller should check origin, consume the result in its original work, then
acknowledge it. Outbound uses `hermes_task_get(acknowledge_result_id=...,
expected_origin=...)`. Hermes uses the same optional fields on `codex_a2a_get`.
Repeated acknowledgements are idempotent and never start a worker. A receipt for
another job/result is rejected; a new continuation produces a different receipt.

This is durable **pull delivery with explicit consumption receipts**. It does not
automatically wake Hermes or inject a message into an originating Codex Desktop
conversation. An external caller/orchestrator must retain the handle and arrange
get/wait when its supported scheduling/runtime API allows. There is no invented
Codex Remote delivery API, auto-attached `turn/start`, push webhook or unattended
watcher in this patch. A receipt records caller acknowledgement, not proof of an
atomic external conversation write. Exactly-once external delivery requires a
receiver with durable deduplication and an authenticated, supported wake-up API.

## Migration and rollback

Schema 5 adds origin metadata, outbound attempt identities and result receipts.
Existing records and tables are retained. Stop writers and back up SQLite with its
WAL before switching versions. Keep one active writer process per state file; use
separate MCP and inbound state files when deploying separate processes. Do not run
old and new MCP registrations against the same file. Older code will ignore additive
fields but does not enforce the new exact-correlation/receipt contract; retain the
backup and new-version database rather than deleting unknown jobs to force retry.
