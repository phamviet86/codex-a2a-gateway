# v0.4.0 release validation

Date: 2026-09-05. This report supplements the earlier local durability report;
it does not rewrite historical validation or claim a production rollout.

## Source and scope

The durability implementation was merged in PR #1 as
`eb1ab8b50e863b0fdd297b286028ecab01eb9afd`, containing tested head
`7e80aaea94e19eccb1d56727dd0fc5a6331e02e3` and tree
`fd9d15a89915f359e367829e9e8678a32ff66c07`. Release preparation changes version
metadata, its assertion, and documentation only; the job execution logic is unchanged.

## Automated and isolated live evidence

- CPython 3.11 on macOS and Ubuntu passed compile, Ruff lint/format, Mypy,
  88 tests (73.63% coverage), build, Twine, distribution-content validation,
  clean-wheel installation, CLI entrypoints and checksum validation.
- PR #1 CI: [run 33969487982](https://github.com/phamviet86/codex-a2a-gateway/actions/runs/33969487982).
- App Server schema checks passed with local Codex 0.153.4 and VPS Codex
  0.151.0-alpha.7.2. The older VPS CLI could not run Astra; the test receiver used
  its compatible `gpt-5.6-sol` model without upgrading Codex or enabling fallback.
- Isolated live Codex → Hermes and Hermes → Codex calls returned their exact
  expected markers, retaining origin and result identity.
- Both directions expired a one-second caller wait while work continued. Later
  retrieval on the same handle returned the expected result without cancel/resend.
  The outbound result arrived after about 15 seconds; read-only SQLite inspection
  confirmed one task, one context, one attempt and queued/submitted/working/completed
  events. An exact duplicate returned the same identifiers with deduplication.
- Four concurrent independent contexts returned their own markers and origins.
  Repeated completed requests retained their existing task IDs.
- Restarting only the test gateway after a Codex turn ACK recovered that exact
  turn as terminal `CANCELED`; replay retained the same task/origin/result ID and
  did not execute it again. This is not evidence that every interrupted turn succeeds.
- Web access and local file read were live verified. A scratch-file mutation was
  refused by the worker's read-only sandbox, so successful file-write execution
  was not established. Its duplicate was suppressed with one task/message binding.
- Test processes and cloned Hermes profile were removed; test ports were closed.
  Production service PID/start times and health stayed unchanged.

## Limits

Transport fault injection, pagination ambiguity, continuation and exact history
edge cases are covered by deterministic fake-peer/backend tests. Native input was
unavailable in the live worker mode; browser control/authentication was not proven.
Do not infer Desktop plugin/tool inheritance or successful mutation permissions.

Delivery remains durable pull plus explicit consumption receipts. There is no
automatic wake-up or injection into the originating conversation and no atomic
external-delivery guarantee. Missing exact peer correlation or a pre-turn ACK can
remain `outcome_unknown`; it is not permission to resend. Suspended native approval
or input RPCs are not reattached. Same-job continuation starts a new turn.

Release assets are the universal Python wheel, source distribution and
`SHA256SUMS`. Verify the manifest before installation. The GitHub release identifies
the release tag and current packaging CI; live evidence above applies to the
unchanged implementation inherited from PR #1. No PyPI publication or production
deployment is part of this release validation.
