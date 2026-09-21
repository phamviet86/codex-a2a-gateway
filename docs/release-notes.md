# v0.8.0rc1 — Durable conversations and actionable peer diagnostics

This candidate preserves rejection reasons through the broker and MCP using bounded,
validated, encrypted details. Existing error codes and the five MCP tools remain.
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

This is a release candidate. Installed-host long-conversation and parallel Desktop
acceptance will be recorded separately before the stable release. Historical v0.7
results are not evidence that this candidate has passed those new live scenarios.
