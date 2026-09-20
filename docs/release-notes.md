# v0.7.0 — Hermes client/server and managed SSH tunnel

This release fixes `gateway_submit(wait_seconds=20)` being rejected
when the configured default wait is 15 seconds. Explicit waits from 0 to 60 are
accepted; malformed waits fail before an operation is persisted.

The client daemon can maintain a private SSH forward alongside its MCP service.
HTTPS certificate verification and device authentication remain required. Network
failures reconnect with backoff; an ambiguous agent submission is reconciled by
its saved identity rather than blindly replayed. `client-doctor` separates SSH,
TLS and broker authentication diagnostics.

This is a breaking installation update: package, executable and MCP registration
are now `hermes-a2a-gateway`, the Python namespace is `hermes_a2a_gateway`, and
configuration uses `HERMES_A2A_GATEWAY_*`. Only `broker`, `client`, `client-mcp`
and `client-doctor` remain. The old inbound gateway, seven `hermes_*` tools,
Hermes-to-Codex plugin, setup skills and compatibility aliases are removed.

Follow [migration and rollback](migration-v0.7.md), then the
[server](server-hermes.vi.md), [client](client-codex.vi.md) and
[SSH transport](ssh-tunnel.vi.md) guides. Preserve existing v0.6 database records,
encryption keys, operation IDs, result IDs and device identities. Stop the old
writer before moving the inbox; remove obsolete operational installations only
after the replacement passes verification. The release contains a wheel, source
archive and `SHA256SUMS`; installation requires no Git clone.

The release candidate passed installed Mac/VPS testing: an explicit 20-second
submit returned a durable operation, the daemon recovered after its own SSH child
was terminated, and the result returned through native queue to the originating
Desktop task. The same operation/result was retrieved without a second submit.
See the [dated verification report](testing-report-v0.7.0.md) for exact artifacts,
identities, CI and limits. Release publication also tolerates delayed GitHub draft
visibility through read retries without creating duplicate releases.

Native queue delivery remains version/schema gated; queue acknowledgement is
not a user-consumption guarantee. Cancellation is best effort, offline host wake
is not guaranteed, and an interrupted broker-to-Hermes stream may stop computation.
Custom SSH helpers that deliberately detach or ignore SIGTERM are outside the
macOS forced-daemon-exit cleanup guarantee.
