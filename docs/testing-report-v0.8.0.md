# v0.8 verification — 2026-09-21

## Candidate source validation

The candidate contains structured broker diagnostics, an authenticated peer-policy
probe and the versioned Hermes patch for commit
`490e6b5966de330285905f52ebaee23c842dc3ea`.

- CPython 3.11 compile, Ruff check/format and mypy: pass.
- Full suite with isolated PostgreSQL 16: **216 passed, 1 skipped**, 81.61% coverage.
  The skipped case is the opt-in destructive-to-disposable-job macOS LaunchAgent test;
  this release does not change tunnel process lifecycle.
- Build, wheel/sdist metadata validation, distribution inventory, clean macOS wheel
  install, MCP discovery/native-origin refusal and release checksums: pass.
- Independent source review: pass. The reviewer ran the 11 pinned-source patch tests
  and 31 diagnostic/client tests independently.

Patch tests execute the patched protocol/security and HTTP handling source against
licensed pinned fixtures, with only unrelated Hermes runtime/model execution stubbed.
They cover fake-clock 25-turn continuity, sliding-window boundaries, concurrent
admission, credential-selected policy, localhost legacy peers, forged metadata,
HTTP/SSE diagnostics, scoped GetTask, and strict patch apply/revert preconditions.
These are offline tests, not live model or deployment evidence.

## Candidate deployment acceptance

Pending installation of the published candidate: real same-context 25-turn recall,
four concurrent Desktop tasks with correct result routing, receiver rate rejection
and subsequent acceptance, ledger/session preservation, and installed asset provenance.
Do not infer these results from the offline suite or historical v0.7 report.

## Limits

Rate protection does not identify causal agent loops. Unknown mutation outcomes
are not automatically retried. Tool approval and Google authentication remain
Hermes responsibilities. Native TaskStore is memory-resident; historical broker
results and persisted Hermes conversation history are separate durability layers.
Broker diagnostics are encrypted; local SQLite snapshots retain their existing
private-file/retention model. Workstation sleep/wake remains outside this test scope.
