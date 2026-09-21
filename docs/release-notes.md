# v0.8.0 — Durable conversations and actionable peer diagnostics

This release preserves rejection reasons through the broker and MCP using bounded,
validated details encrypted in broker storage. Local SQLite snapshots retain their
existing private-file and retention model. Existing error codes and the five MCP tools remain.
The doctor reports whether the actual broker credential selects the patched Hermes policy.

A versioned compatibility patch targets Hermes commit
`490e6b5966de330285905f52ebaee23c842dc3ea`. It replaces the lifetime conversation
turn cap only for a configured authenticated gateway credential with five accepted
submissions per rolling 60 seconds, per peer/agent/context. Other peers retain
legacy protection. Rejected requests do not prolong the window. This is rate
protection, not causal-loop detection, and never grants additional tool permissions.

Download the wheel, source, compatibility patch, manifest, installer and SHA256SUMS
from this release. Follow [deployment](deployment.md) and
[compatibility upgrade/rollback](hermes-compatibility.md). Preserve ledgers, keys,
context/session identities and receipts. No operation is automatically resubmitted.
Read the [English AI operating guide](ai-operations.md) before delegating work.

The published candidate passed installed Mac/VPS acceptance: four simultaneous
Desktop tasks with correct native result consumption, a real pre-execution 5/60
rejection followed by authorized same-context recovery, and 25 public-fiction
exchanges with final recall in a context that completed 50 exchanges overall.
The earlier memory-framed test elicited privacy refusals; that evidence is retained,
not counted as successful recall. Permissions and memory policies were unchanged.
See the [dated verification report](testing-report-v0.8.0.md) for results and limits.

Stable promotion changes the version and documentation only; the tested runtime
and Hermes compatibility patch are otherwise unchanged. Install the stable assets
on both hosts and run doctor plus a harmless application smoke test.
