# Client/server setup

Use the exact installed version's
[deployment guide](https://github.com/phamviet86/hermes-a2a-gateway/blob/v0.6.0b1/docs/client-server-deployment.md).
Its topology is Desktop MCP → local daemon/SQLite → verified HTTPS/SSE → private
broker/PostgreSQL → loopback Hermes A2A. The legacy inbound gateway is separate.

1. Reuse the user's selected server and device identity. Inspect existing state,
   credentials and TLS trust without printing secrets. Keep legacy environments,
   registrations and databases intact.
2. Install the same verified v0.6 wheel in separate Python3.11 environments.
   Server prerequisites are PostgreSQL and a trusted TLS reverse proxy on a
   private interface. Client prerequisites are Unix sockets and a tested Codex
   native queue schema/version. Do not expose Hermes or PostgreSQL remotely.
3. Store device tokens and independent server/client encryption keys outside the
   repository in restricted environment files. Use persistent launchers for GUI
   execution. New CLI commands are `broker`, `client`, `client-mcp`, and
   `client-doctor`; run `--help` against the installed runtime.
4. Use one broker and one client daemon per state directory. Register only
   `client-mcp` under `codex-a2a-client`, with the absolute launcher. Verify HTTPS,
   missing-token denial and client doctor, then tool discovery in a fresh task.
5. With existing live-test authorization, submit a uniquely marked harmless
   request and retrieve the exact handle. Separately test a delayed result's
   native queue reference in an idle disposable Desktop task. Acknowledgement
   alone is not proof that the assistant consumed it. Do not bypass a blocked UI
   or take over Desktop's writer lock.

Never retry a Hermes mutation or native queue insertion after an ambiguous ACK.
Keep `outcome_unknown` / `delivery_outcome_unknown` and use exact retrieval.
Offline/unloaded wake, exactly-once consumption and guaranteed cancellation are
not promised. Payloads/artifacts expire; preserve keys with backups under a
matching retention policy. Uploaded files are inert and not virus-scanned.

Rollback stops only the new units and removes only their MCP registration. Keep
new stores and key files; do not point an older executable at them.
