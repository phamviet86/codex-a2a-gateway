# v0.5.1 deterministic validation — 2026-09-15

## Scope and environment

Local macOS arm64, CPython 3.11.16, isolated project virtual environment. Baseline:
`cd11b74557209c7b279f65c72be0bc922e4862e4` (v0.5.0). This report covers the v0.5.1
source changes and deterministic fake-peer tests. It does not claim a published
release, Linux execution, live Hermes acceptance, or VPS deployment.

The source-derived receiver evidence supplied for Hermes 0.21.2/upstream 5eb99eb2
shows a new continuation task ID in an initial Task snapshot with explicit context
and the same non-null JSON-RPC request ID. Its task store is in memory and its JSONL
records lack exact request-message attribution. This was source inspection, not a
new live packet capture.

## Regression and checks

The new-ID continuation regression was run against extracted baseline source and
failed as expected: the gateway returned `outcome_unknown` instead of `completed`.
The same regression passes with the compatibility fix.

| Check | Result |
| --- | --- |
| `python -m compileall -q src tests scripts` | PASS |
| `ruff check .` | PASS |
| `ruff format --check .` | PASS |
| `mypy src` | PASS, 17 source files |
| Full pytest with branch coverage | PASS, 183 tests; 76.00% coverage |
| New continuation and transport suites | PASS, 47 cases |
| Fresh wheel/sdist build | PASS |
| Twine metadata, distribution contents | PASS |
| Fresh virtualenv clean-wheel install on macOS | PASS |
| SHA256SUMS generation and verification | PASS |

The test suite uses fake local listeners; local sandbox permissions were expanded
for those listeners. Build and clean-wheel validation downloaded dependencies.
No live Hermes or Codex model request was run. App Server wire behavior was not
changed, so its schema checker was not required for this patch.

## Cases covered

- Same-ID and rotating-ID continuations; stable bridge handle, context and origin;
  duplicate attempt keys; result acknowledgement and wrong-origin/receipt rejection.
- Wrong/missing JSON-RPC identity, explicit context and message attribution;
  status-only replacement rejection; occupied/historical IDs and repeated rebinding.
- Loss before and after acknowledgement; metadata-free retrieval of a uniquely
  bound ID; exact current-message requirements for reused IDs; restart with missing
  legacy provenance; exact recovery while the predecessor remains retrievable,
  including evidence arriving later during wait.
- Late old worker errors, get success/not-found and cancel responses; worker-map
  ownership; continuation transcript attribution; zero automatic mutation replay.
- Migrated predecessor reservation, cross-job ownership, and SQLite failures proving
  task/binding/reservation rollback occurs together.
- Release publisher and workflow guards for exact successful main/push CI SHA,
  immutable prior artifacts, tag identity and downloaded checksums.

## Limits and next gate

The existing failed VPS job was not accessed, replayed, rewritten or acknowledged.
Old unknown jobs do not acquire acknowledgement provenance through migration.
Missing ACK or receiver task loss can still leave a job unknown; context alone is
never sufficient. Cancellation remains best-effort.

The v0.5.1 workflow requires successful macOS and Linux CI on the exact release
commit before publication. Those remote results and any later authorized VPS
acceptance must be recorded separately; this local report is not evidence of them.
Historical release/testing reports remain unchanged.
