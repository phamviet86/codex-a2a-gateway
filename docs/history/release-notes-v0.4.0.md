# v0.4.0 — Durable bidirectional jobs

Codex and Hermes retain the same job across caller timeouts, late replies and
supported continuations. Handles are saved before discovery, recovery requires
exact request/task identity, and paginated reconciliation refuses ambiguous matches.
Independent contexts can run concurrently while each context serializes its turns.

Origin handles, attempt deduplication, stable result receipts and additive SQLite
schema 5 preserve attribution and consumption state. The bundled Hermes plugin
suppresses duplicate submissions and permits explicit replay only when the receiver
proves execution never started. Acknowledged Codex turns can be recovered through
read-only exact history lookup after a gateway restart.

## Validation

The implementation passed 88 tests (73.63% coverage), macOS/Ubuntu CI, App Server
schema checks, clean-wheel installation and isolated VPS live checks. Live calls in
both directions expired one-second waits and later returned the correct result on
the same handle without resending. Four concurrent contexts retained their mapping;
duplicate and restart checks preserved identity without executing the job again.
The release workflow waits for CI success on the exact merged main commit, builds
and validates distributions, then downloads and verifies every uploaded asset before
publishing. See [the validation report](https://github.com/phamviet86/codex-a2a-gateway/blob/v0.4.0/docs/testing-report-v0.4.0.md).

## Upgrade and limits

Download the wheel, source archive and SHA256SUMS; verify the manifest before
installation. Stop existing gateway/MCP writers and back up state before upgrading.
Run only one writer per SQLite file and replace the bundled Hermes plugin in its
intended profile. Follow the [upgrade guide](https://github.com/phamviet86/codex-a2a-gateway/blob/v0.4.0/docs/deployment.md).

Delivery remains durable pull with receipts: there is no automatic originating-thread
wake-up or result injection. Missing exact peer correlation or a pre-turn ACK stays
unknown; do not blindly resend. Native suspended input/approval RPCs are not reattached.
Worker tools do not inherit Desktop capabilities. Live web/file-read passed; file-write
was read-only, and browser-control/auth plus native input were not live established.
The older VPS Codex used compatible gpt-5.6-sol; Astra was not supported there.

This GitHub release does not publish to PyPI or deploy/update production services.
