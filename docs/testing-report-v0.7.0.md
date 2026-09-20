# v0.7.0 verification — 2026-09-21

## Local implementation and migration checks

The combined candidate content was checked on macOS with CPython 3.11, a private,
disposable PostgreSQL 16 cluster, fake A2A peers and local TLS fixtures. The full
suite passed **189 tests with 81.35% coverage**, including the opt-in disposable
LaunchAgent test. Compile, Ruff lint/format, mypy and the installed Codex native
queue schema checks passed. A fresh wheel installation passed package metadata,
CLI/tool discovery, removed-command and checksum checks.

Regression coverage includes explicit waits 0/20/60 with configured default 15;
malformed/nonfinite waits rejected before persistence; 16/17 artifact boundaries;
loopback peer transport ignoring inherited proxy variables; authentication and
TLS failures; SSE replay and lost broker ACK recovery with one command POST;
ambiguous Hermes/native outcomes without blind replay; and copied SQLite state
created by the actual v0.6.0b1 implementation. The historical fixture preserves
ciphertext, operation/flow/result/delivery identities, receipts and cursor across
restart. The broker's PostgreSQL schema and peer-context UUID namespace are
unchanged.

The macOS process test kills a disposable daemon with SIGKILL, observes launchd
start a new daemon/SSH listener on the same port, then removes its synthetic job
and verifies all recorded processes exited. Controlled cleanup also kills an
owned helper ignoring SIGTERM while preserving an unrelated sibling. A later
CI-only failure exposed a test assumption about TCP TIME_WAIT; the final check
requires no accepting listener and successful SO_REUSEADDR bind/listen, without
SO_REUSEPORT. Ten repeated lifecycle tests and the actual LaunchAgent test passed
after this test-only correction.

## Candidate source and CI

[PR #8](https://github.com/phamviet86/hermes-a2a-gateway/pull/8) was merged as
`c2364682b881df12067c66a46d6c68f1165eb9bf`. Its content tree is
`b5ecbdb5e81c9d5fdafc1c62af25251f78780224`.
The [exact PR-head CI run](https://github.com/phamviet86/hermes-a2a-gateway/actions/runs/35543475511)
passed PostgreSQL transactions and macOS/Linux quality plus clean-wheel checks.

## Published release candidate

[Main CI](https://github.com/phamviet86/hermes-a2a-gateway/actions/runs/35543609699)
passed all three jobs. The [publisher](https://github.com/phamviet86/hermes-a2a-gateway/actions/runs/35543711771)
completed after resuming an existing draft whose first listing was temporarily
inconsistent. The annotated tag was never moved and published assets were not
overwritten. This is enforced by the publisher; GitHub platform-enforced release
immutability was not enabled.

The [v0.7.0rc1 release](https://github.com/phamviet86/hermes-a2a-gateway/releases/tag/v0.7.0rc1)
was downloaded again from GitHub and checked against both its manifest and API
asset digests. The annotated tag points to the merge commit above.

| Asset | SHA-256 |
| --- | --- |
| `hermes_a2a_gateway-0.7.0rc1-py3-none-any.whl` | `2fbec6d34bfe7569ffc31c159ef7d623a3cc4211ec4671750e067832584c78c2` |
| `hermes_a2a_gateway-0.7.0rc1.tar.gz` | `59e7804028f2d04f557517c9acb6db5eebacc34532e91707f2e2bc6082833171` |

## Installation migration evidence

Before cutover, all eight broker operations and all five workstation operations
were terminal. The workstation's old daemon and exact gateway MCP children were
stopped while preserving Codex. A private backup preceded configuration and
state migration. Equality checks preserved all rows across two flows, five
operations, one cursor record and ten receipts, and retained the existing token
and payload key without displaying their values.

The fresh workstation venv was installed from the verified GitHub wheel. A new
native Desktop task discovered exactly the five canonical `gateway_*` tools.
This metadata-only discovery did not submit a Hermes operation. After the live
proof passed, the obsolete workstation configuration, runtime and state roots
were removed while retaining private backups. The canonical daemon and its SSH
child stayed running; no unrelated Codex or SSH process was removed.

## Live SSH recovery and native Desktop consumption

The fresh source task `01a0c11c-cd80-7852-9fff-da91d1313671` submitted once with
`wait_seconds=20`. It received a running operation and ended its turn without
polling. The harmless Hermes request ran a local 35-second sleep, then returned
`HERMES_GATEWAY_RC1_SSH_RECOVERY_20260921_A71C`.

| Identity | Value |
| --- | --- |
| Operation | `e7e625fa-e3c6-4186-92c2-14ea03825374` |
| Flow | `d3ac9bc6-583d-4478-b844-64c4d6585cd0` |
| Hermes task | `task-2f491a9d1d0f44c8` |
| Result | `9845c858-d266-4e26-bb00-0c3c366198e6` |
| Native delivery intent | `49dcb3cd-96d6-4f34-919c-bfa5171463ae` |
| Native queue item | `01a0c12c-144a-7992-a451-e433cfb418a0` |

The observer waited until the remote handle was saved, then terminated only the
daemon-owned SSH child. The daemon PID stayed unchanged; SSH generation advanced
from 1 to 2 and verified/authenticated connectivity recovered in about 3.46 seconds.
The original PostgreSQL outbox claim remained unchanged before, after and at
completion. The operation, remote handle and result identity were preserved.

Without another orchestrator message, a native reference woke the same Desktop
task in turn `01a0c12c-2a80-75a1-a54b-acd65917b2ed`. It called `gateway_get` and
verified the exact marker. A subsequent `gateway_wait(timeout=60)` returned the
same completed result immediately. This proves the supported upper argument on
a completed operation; it is not a measurement of a full 60-second live wait.
There was one `gateway_submit` call in the source task. The native Hermes
`ListTasks` response for exact derived context
`4eb8a7e1-b7cc-5a1d-8667-d0461477d911` reported `totalSize=1` with the same remote
task. Exact `GetTask` returned the marker; broker receipts recorded `stored` and
`delivered` for the same result ID. No duplicate task or native queue item was
observed in this controlled fault test.

## Final release preparation

The `0.7.0` runtime differs from the installed candidate only in its version
constant. Release tooling adds cache-busted, bounded read retries for delayed
GitHub draft visibility; it does not repeat creation or overwrite published
assets. The final source passed compile, Ruff, mypy and **190 tests with one
opt-in LaunchAgent test skipped**, with 81.35% coverage. That unchanged lifecycle
test had already passed explicitly in the candidate verification above. A fresh
`0.7.0` wheel passed installation, metadata, CLI/MCP discovery, distribution and
checksum validation on macOS. The release workflow independently builds the
published assets from the exact successful main/push CI commit.

## Scope of the evidence

Local fixtures and CI establish protocol, packaging and failure handling. The
installed candidate and native task evidence above separately establish the
observed Desktop result consumption. These observations are not a general
exactly-once or offline-wake guarantee.

A custom operator SSH helper that detaches or deliberately ignores SIGTERM is
outside the macOS hard-kill cleanup guarantee. Native queue ACK does not imply
consumption, cancellation is best effort, and an offline/unloaded Desktop host
is not guaranteed to wake. Actual workstation sleep/wake is a separate test from
terminating a tunnel process. Broker-to-Hermes stream loss may stop computation;
reconciliation does not guarantee continued execution.
