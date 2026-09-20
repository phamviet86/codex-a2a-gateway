# v0.6.0b1 validation — 2026-09-21

Status: the corrected candidate passes installed VPS execution, text-reference
consumption and native late-result delivery on the current Codex Desktop.
Final release assets are published only after CI passes on the release commit.
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

### Corrected candidate

Source `0b328e420bbbcd11befd92e5380cac83cc76f7e0` includes retained bounded
submission streams, the broker-specific idle timeout override, UTC timestamp
normalization and exact appended artifact reconstruction.

- Full suite: **279 passed**, including eleven process integration cases;
  coverage **76.85%**.
- Compileall, Ruff, mypy, App Server schema, fresh build, twine, distribution audit,
  macOS clean-wheel installation and checksums all passed again.
- The independent verifier matched all 31 packaged files/assets against this
  source and the installed Mac environment.
- Corrected wheel SHA256:
  `5de0253fb943c97b9307614560ac325defa229f8b41bcc688eecc9192711eda3`.

The added real-process tests first failed on the earlier implementation, then
passed after correction: a peer that fails on disconnect, eleven seconds of
silent streaming while another flow completes, and split artifact chunks followed
by a status-only terminal event. The latter confirms exact content without an
extra newline and without relying on GetTask to repair an empty result.

### Final shutdown correction

Source `01cef3acf518bd5a7388c718e329bdca88a64947` additionally bounds HTTP/SSE
shutdown draining to five seconds. This addresses an installed maintenance
observation: an earlier broker waited for its service manager's 90-second stop
limit while a client SSE connection remained open.

- Final complete suite: **280 passed**, coverage **76.85%**.
- Compileall, Ruff check/format, mypy and App Server schema checks passed.
- A real broker subprocess with an active operation and connected SSE client
  exited about 5.62 seconds after SIGTERM. Its saved remote handle remained
  `outcome_unknown`, and a new store acquired the released dispatcher lock.
- The successful live candidate above differs in packaged runtime only by this
  shutdown deadline. Final release wheel identity and clean installation are
  checked separately; consult the release's `SHA256SUMS` for published bytes.

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
were retained and not replayed. The corrected implementation retains/drains live
streams and passes the new process regressions. New installed-host probes then
passed on the corrected wheel above; the original failed operations remain
unchanged. A fresh delayed Desktop request returned immediately, its originating
turn ended, and the native queue started a later turn in the same task after a
28-second idle gap. That turn read the exact completed operation and expected
unique marker. The verifier used read-only host state, SQLite and authenticated
broker GET, without injecting a result or queue entry.

The real text-reference probe placed its expected marker only inside a UTF-8
artifact uploaded before the broker upgrade. Hermes returned that marker after
the upgrade, demonstrating both reference consumption and retained artifact
identity. Linux clean-wheel checks passed for the corrected wheel. TLS used a
private CA with certificate and hostname validation enabled. The installed
Desktop native adapter was Codex CLI `0.155.0-alpha.9.2`; this is version-specific
evidence, not a guarantee for every future Desktop build.

| Gate | Current evidence |
| --- | --- |
| Mac → private VPS TLS | PASS: private CA and hostname validation, health 200 |
| Linux clean-wheel install | PASS: same candidate wheel in a fresh Python 3.11 environment |
| Device auth and actual PostgreSQL broker | PASS: missing/invalid token 401, authenticated missing operation 404 |
| Real Hermes execution over HTTPS broker | PASS on new corrected-candidate probes; historical failures retained |
| UTF-8 reference file consumed by Hermes | PASS: expected marker existed only in the retained uploaded artifact |
| Current Desktop MCP and native late result | PASS: five tools, inline result, and successful native return to the same idle task |
| Independent verification of combined release | Functional PASS: final shutdown test, exact live GETs, reference bytes, native late result, and 31 installed package files; published artifact rollout checked separately |
| GitHub CI and release assets | Corrected candidate CI PASS on macOS, Linux and PostgreSQL; final publication follows exact-commit CI |

## Limits

This beta does not claim exactly-once execution/consumption, automatic offline or
unloaded-host wake, guaranteed cancellation, human-input continuation in the new
broker, generic multimodal interpretation, antivirus scanning, multi-tenant
isolation, or high availability. Definitive terminal results are immutable;
uncertainty resolves only through exact saved identity. An unknown native enqueue
is not automatically retried. The legacy local profile remains independently
available with its own stores and contracts.
