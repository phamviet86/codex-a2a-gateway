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

## Published candidate provenance

Candidate [v0.8.0rc1](https://github.com/phamviet86/hermes-a2a-gateway/releases/tag/v0.8.0rc1)
was published from main commit `68bbfb63522637e7c7ae3caccf310eaa8d45e98e`.
[Main CI](https://github.com/phamviet86/hermes-a2a-gateway/actions/runs/35565276341)
passed, including macOS/Linux clean-wheel installation; the
[release workflow](https://github.com/phamviet86/hermes-a2a-gateway/actions/runs/35565399882)
passed. An independent reviewer downloaded the public assets, checked all six
checksums, matched the wheel's 16 runtime source files and three compatibility
assets to the reviewed source tree, and verified the tag target.

The candidate wheel SHA256 is
`e42a2f74efbefdc3cf5b75350d073e6df695de3040a8898be5f00b9d9e3e390e`.
The compatibility patch SHA256 is
`0052dca113c8160dc7a300178172c9ee290c8b685e2898743cae3619cedaf555`.
These checks establish artifact provenance, not successful live acceptance.

## Candidate deployment acceptance

Installed candidate provenance was independently checked on both hosts: all 16
runtime sources, package metadata and entry points match the public wheel. The
three patched Hermes files match the published manifest. Eight GWS guide files
match their pre-upgrade preservation receipt.

The authenticated client doctor reports verified TLS and a selected
`sliding_window` policy with limit 5 and window 60 seconds. One canonical broker
and one Mac daemon own the stores. Deployment corrected prematurely reported
service stops and an out-of-service broker process before authorizing model tests;
no nonterminal gateway operations were present during those corrections.

### Four independent Desktop tasks

Four fresh Desktop tasks each submitted exactly once. All four native sessions
started between 06:11:57.516Z and 06:12:02.617Z. All four operations were observed
running together at 06:12:31.970Z and 06:12:35.171Z; all completed by 06:12:47.902Z.
The no-tool prompts produced 80 numbered weather lines and distinct final markers.
Each original Desktop task consumed its native result notification, read its exact
operation, and verified only its own marker. No operation was resubmitted.

| Task | Operation | Result |
| --- | --- | --- |
| A | `9d272bbd-8fbb-4f6a-827f-1527f009187c` | `35c2c34b-09b4-4c04-b255-661235cc842c` |
| B | `beb736c1-ea0e-4954-a2b5-b5a459e47842` | `d2524e27-4219-46a5-997e-910fb94e78f2` |
| C | `95d61cb6-b3f4-4036-b115-50171e14c93e` | `1283a45b-7f4c-47a6-8a4c-4df7d9961a22` |
| D | `4dc1bc2c-5d2a-4ccb-afd2-a7ad84a4b33e` | `dda6f129-5428-4406-b9fb-f1ee8bdd1b97` |

### Live rate boundary and per-flow ordering

A separate Desktop flow queued six distinct short requests in 25.955 seconds.
The first five completed; the sixth operation
`a0dda124-fd8e-4b69-88ab-33ab4c37571b` failed with `peer_rejected` and:

```json
{
  "reason": "context_rate_limited",
  "execution_started": false,
  "retry_after_seconds": 34,
  "retry_at": "2026-09-21T06:14:48.608028+00:00",
  "recommended_action": "wait_then_submit_new"
}
```

Native history contained only five request/response pairs before recovery, no
model execution for the rejected request. Each accepted request began after the
preceding response. At 06:15:04.940Z, after the stated window and safety margin,
one explicitly authorized new operation `a54f8518-c40a-4ffb-876e-397f723a2ef3`
was accepted in the same flow and completed with `RATE-RECOVERED`. The original
rejection snapshot was unchanged on reread. Exactly seven submissions occurred;
there was no automatic retry, context rotation, counter reset or service restart.

### Preservation and outstanding acceptance

All 25 baseline PostgreSQL operations retained their saved identity/state/result/
remote-task fields. Existing native session IDs were preserved across eight
profile database snapshots. The original conversation anchor retained its mapping.

### Same-context continuity and recall

All 25 distinct operations completed in one flow and one native session, with at
least 15.051 seconds between submission starts and each operation terminal before
the next. Turns 2–24 returned the exact requested acknowledgments. The first
request's suggestion to remember a synthetic fact was declined as peer-supplied
memory; the final request to recall that fact was declined as conversation data.
Therefore **25-turn transport/history continuity passed, but behavioral recall did
not pass**. No context rotation, repeated fact, retry or permission change was used.
Read-only inspection found no own-context public-fiction ban in the installed
platform hint, configured system prompt or shared skill. The first fact remained
in active persisted history and its API-content sidecar; source replays user and
assistant messages. The exact original model wire payload was not captured, so
this does not establish the precise reason for the refusals.

A second, separately recorded 25-exchange test continued in the **same flow and
native session**, with no policy change, reset or repeated operation. Its opening
request described a public fictional yellow paper kite with four purple stars.
After 23 short acknowledgments, the final request asked to continue the kite story
without repeating those properties. The actual final response was:

> It was bright yellow, decorated with four purple stars.

All three properties matched. All 25 new operations completed sequentially;
caller submission starts were at least 15.101 seconds apart. The session therefore
completed 50 exchanges in total. The final operation is
`00d77dbd-2af1-421b-a4cd-cf7624270f9f`, result
`44ef6e82-114d-4285-b691-945ba27123ac`. Public-fiction conversation recall passed;
the first test's privacy refusals remain recorded above and are not relabeled.

Final stable artifact installation and post-restart readback require separate
post-publication verification in the operator's deployment receipts. This candidate
acceptance section does not claim those later steps.

## Limits

Rate protection does not identify causal agent loops. Unknown mutation outcomes
are not automatically retried. Tool approval and Google authentication remain
Hermes responsibilities. Native TaskStore is memory-resident; historical broker
results and persisted Hermes conversation history are separate durability layers.
Broker diagnostics are encrypted; local SQLite snapshots retain their existing
private-file/retention model. Workstation sleep/wake remains outside this test scope.
