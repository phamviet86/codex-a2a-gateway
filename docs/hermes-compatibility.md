# Hermes compatibility for long conversations

Gateway 0.8 ships a narrow, optional-to-install but required-for-long-conversations
patch for Hermes commit `490e6b5966de330285905f52ebaee23c842dc3ea`.
Installing the gateway wheel alone does not change Hermes's conversation policy.
The patch is an integration maintained by this repository, not an upstream Hermes
release. Do not apply it to an unverified Hermes version.

The authenticated broker can select a five-accepted-submissions/60-second sliding
window per peer, target agent and context. Other peers retain the legacy total-turn
guard; the existing global request limiter and tool approval rules remain active.
No prompt or model-supplied metadata can select the new policy. Contexts and session
identity remain unchanged; the guard is not a general detector of agent call loops.

## Release assets and upgrade safety

Alongside the wheel and source archive, download `hermes-a2a-compat.patch`,
`hermes-a2a-compat.json`, `hermes_patch.py` and `SHA256SUMS` from the same release.
Verify the downloaded assets against `SHA256SUMS` before execution. The JSON manifest pins
the upstream commit and the original/patched file hashes. The installer must
refuse unknown source, conflicting edits and partial application; an already
matching installation is reported without applying it twice.

Before applying: stop new gateway submissions, drain active work, and preserve
consistent private backups of both ledgers, credentials/configuration and the
affected Hermes files. Do not stop native Hermes while unrelated jobs are active.
Run the downloaded utility with the actual installed Hermes checkout path:

```bash
python3 hermes_patch.py check --root "$HERMES_CHECKOUT"
python3 hermes_patch.py apply --root "$HERMES_CHECKOUT"
# Rollback, only after stopping affected work and verifying the same checkout:
# python3 hermes_patch.py revert --root "$HERMES_CHECKOUT"
```

Keep the patch, JSON manifest and utility together. `HERMES_CHECKOUT` is an
operator-resolved path, not a model-supplied target. Apply only after check succeeds.
Restore services in server-first order and
verify `client-doctor` reports the selected credential's active peer policy.

## Credential selection

Set server-only `A2A_GATEWAY_TOKEN` to a nonempty private credential and use the
same value as the broker's `HERMES_A2A_TOKEN`, through protected environment
injection. The selector is checked in constant time and only on loopback requests.
Existing Hermes authentication and trust checks still apply. If Hermes already
requires a bearer credential, reuse that authorized broker credential.

On an existing localhost-only deployment without tokens, provision a dedicated
secret for the selector and broker; keep the underlying localhost-only policy
unchanged. Local callers without that secret retain the legacy policy; they do not
receive sliding-window privileges. This does not enable unauthenticated remote
access. Keep existing peer/context/session identities unchanged. Never send the
selector in prompts or MCP arguments.

The patched Agent Card advertises support. The authenticated
`/gateway-capabilities` endpoint reports whether the supplied credential actually
selects it; the broker exposes a sanitized view through `/v1/peer-policy` and the
client doctor. An unavailable or older peer is reported as unknown/legacy, not
as verified long-conversation support.

## Errors and rollback

Structured rejection diagnostics use the versioned metadata key
`https://github.com/phamviet86/hermes-a2a-gateway/extensions/diagnostics/v1`.
The broker validates an allowlist and persists details encrypted under its normal
result retention. Get/wait and event replay use the same stored snapshot. Raw
peer text is not persisted in plaintext error columns or treated as instructions.
Hermes's own task store is memory-resident; broker evidence and historical
conversations are separate from that receiver task-cache lifetime.

If acceptance fails, quiesce affected writers, revert only the verified patch and
the selected configuration delta, and reinstall the previous wheel. Preserve new
ledger records; do not overwrite a live database with its pre-upgrade backup.
Keep backups private and record exact versions and checksums. A future Hermes
upgrade requires a newly verified patch or equivalent upstream implementation.

Local client snapshots keep the existing SQLite result model: restrictive file permissions and retention, not new at-rest snapshot encryption. Broker diagnostic details are encrypted; local snapshots contain only the bounded allowlisted fields, never a newly copied raw peer rejection message.
