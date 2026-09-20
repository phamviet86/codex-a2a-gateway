# v0.6.0b1 validation — 2026-09-21

Status: installed-host testing found a stream-lifetime defect; correction and
reverification are required before publication.
This report is updated with observed evidence before publication. Historical
[native probes](native-integration-probe-2026-09-20.md) establish feasibility only,
not validation of this implementation.

## Combined source and package

Candidate source: `5ab6175625de1540133187ba42d02722fc733cea`.

- CPython 3.11.16 on macOS; PostgreSQL 16 in a dedicated disposable cluster.
- Compileall, Ruff check, Ruff format check, mypy and installed Codex App Server
  schema check passed.
- Full legacy plus broker/client suite: 260 passed; 76.49% combined coverage.
- Additional process integration suite: eight passed using actual daemon/broker
  processes, TCP transport, PostgreSQL and controlled fake Hermes/native peers.
- Fresh sdist/wheel build, twine, distribution-content audit, macOS clean-wheel
  installation and checksum verification passed. Clean-wheel verification starts
  the installed MCP facade, discovers its five tools and confirms missing native
  metadata is rejected without a model call.

Candidate wheel SHA256:
`ef249e1c021b61dd72481b8b1e750136b8fd48facc78f2745f0ff36a6fb3575b`.
This identifies the prepublication validation artifact; the release has its own
published checksum manifest.

## Process and persistence evidence

The integration suite checks exact-ID command retry after a lost broker ACK,
event replay and client restart without duplicate queue insertion, cross-device
and cross-native-thread refusal, broker crash before receiving a remote handle,
lost native queue ACK without automatic resend, inline response/late delivery
ownership, SSE retention-gap recovery, and invalid attachment rejection before
acceptance. A saved-handle recovery case reads `outcome_unknown`, then observes
the same remote task complete under the same result identity and queues once.

Broker PostgreSQL tests additionally cover concurrent duplicate acceptance and
quota enforcement, encrypted payload tampering, TTL expiry, serialization and
retention, stable result IDs, and a completed first streamed Task that must not
be discarded when later GetTask is unavailable.

Fake native queue tests prove application routing and recovery rules. They do
not prove Desktop wake or model consumption. CI runs fake peers only.

## Installed-host gates

Live testing exposed a behavior absent from the original fake peer: this Hermes
build may finalize a task as failed with `[client disconnected]` when the broker
closes its stream immediately after saving the first remote task ID. One quick
Desktop inline request passed, while slower probes failed. The native queue
correctly returned that failure to the originating task, which proves delivery
routing but does not prove a successful late-result workflow. Failed operations
were retained and not replayed. The release must retain/drain live streams and
pass a new delayed-result test before publication.

| Gate | Current evidence |
| --- | --- |
| Mac → private VPS TLS | PASS: private CA and hostname validation, health 200 |
| Linux clean-wheel install | PASS: same candidate wheel in a fresh Python 3.11 environment |
| Device auth and actual PostgreSQL broker | PASS: missing/invalid token 401, authenticated missing operation 404 |
| Real Hermes execution over HTTPS broker | Inline PASS; delayed execution FAIL due to early stream close |
| UTF-8 reference file consumed by Hermes | Pending live application check |
| Current Desktop MCP and native late result | Five tools discovered; inline result PASS; native queue delivered actual failure, successful late result pending fix |
| Independent verification of combined release | In progress |
| GitHub CI and release assets | Candidate CI PASS; release blocked on live stream correction |

## Limits

This beta does not claim exactly-once execution/consumption, automatic offline or
unloaded-host wake, guaranteed cancellation, human-input continuation in the new
broker, generic multimodal interpretation, antivirus scanning, multi-tenant
isolation, or high availability. Definitive terminal results are immutable;
uncertainty resolves only through exact saved identity. An unknown native enqueue
is not automatically retried. The legacy local profile remains independently
available with its own stores and contracts.
