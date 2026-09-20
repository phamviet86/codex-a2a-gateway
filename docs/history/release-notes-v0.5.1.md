# v0.5.1 — Hermes continuation compatibility

This patch supports Hermes continuations that return a new remote task ID while retaining the conversation context. The local `bridge_task_id` and origin remain stable.

## Changes

- Bind a new remote task ID only from the current continuation's validated direct response. Persist the per-attempt binding and lineage atomically, then reject later rebinding, stale responses, conflicting message attribution, and IDs already owned elsewhere.
- Preserve support for peers that retain the remote task ID.
- Recover a uniquely bound current-attempt task after acknowledgement. Reused IDs require exact current-message attribution; a retrievable predecessor is not evidence that a continuation completed.
- Keep ambiguous outcomes as `outcome_unknown` and never automatically replay a mutating request. Existing unknown jobs require exact new evidence to recover.

## Upgrade and rollback

Stop the gateway and all MCP writers before backing up state or upgrading. Install the v0.5.1 wheel and retain the existing state files and configuration. The migration adds attempt provenance without deleting existing records. Run only one writer per state file.

Before rollback, stop v0.5.1 and retain a backup of its state. Older versions do not understand the new continuation provenance; do not use them to resume affected jobs. See [deployment](https://github.com/phamviet86/codex-a2a-gateway/blob/v0.5.1/docs/deployment.md) for installation and rollback procedures.

## Release verification

Publication is triggered only by successful CI for a main-branch push containing `[release v0.5.1]`. The publisher checks the exact tested SHA, current main, version metadata, annotated tag, asset set and downloaded checksums before publishing. Existing published artifacts are immutable. Linux and macOS clean-wheel checks run in CI; local deterministic results are recorded separately. No live model task runs in CI.
