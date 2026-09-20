# v0.6.0b1 — durable Desktop client and broker

An opt-in prerelease adds a local Desktop client daemon and PostgreSQL broker:
Desktop MCP → durable client inbox → verified HTTPS/SSE → broker → Hermes A2A.
Late results can notify the originating task through a version/schema-checked
native queue adapter. Exact get/wait remains available when native delivery is
unsupported or its acknowledgement is uncertain.

The new path has device-scoped authentication, replayable events, stable result
identities, encrypted expiring payloads, bounded artifacts and conservative
recovery. It does not promise exactly-once execution, native consumption,
unloaded-host wake, guaranteed cancellation, or antivirus scanning.

Legacy local modes, seven `hermes_*` tools, inbound Agent Card and existing
SQLite databases remain compatible. New services use separate stores and
registrations. See [client/server deployment](https://github.com/phamviet86/codex-a2a-gateway/blob/v0.6.0b1/docs/client-server-deployment.md) for
installation from release assets, TLS, retention and rollback.

Publication is restricted to the exact main commit with successful CI, including
macOS/Linux clean-wheel checks and real PostgreSQL transaction tests with a fake
A2A peer. Model execution remains opt-in and is never run in CI. The dated v0.6
evidence report records installed-host verification separately.
