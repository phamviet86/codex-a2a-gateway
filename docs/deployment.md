# Deploy the v0.7 client/server gateway

> Step-by-step Vietnamese guides: [server installation and Hermes usage](server-hermes.vi.md) → [client installation and Codex usage](client-codex.vi.md). This page remains the shared release-installation and operations reference.

This is the only supported runtime profile: a Desktop workstation connects to one
private server running Hermes. Install the canonical `hermes-a2a-gateway` package
on each host. The four commands are `broker`, `client`, `client-mcp`, and
`client-doctor`. See [v0.6 migration and legacy removal](migration-v0.7.md) before
replacing an existing installation.

```text
Desktop -- MCP stdio --> client daemon -- verified HTTPS --> TLS proxy
                         SQLite inbox       commands/SSE       |
                                                        broker :8790
                                                        PostgreSQL
                                                             |
                                                     Hermes A2A :9900
```

The initial profile is one owner, several separately authorized devices, one
broker process and one daemon per client state directory. It is not a public
multi-tenant service or an HA cluster. Use the [wire contract](client-server-contract.md)
and the dated release evidence to distinguish implemented behavior from live
observations. macOS/Linux wheel installation is checked separately from native
Desktop integration; Windows is not supported by this Unix-socket client.

## Install the same release on both machines

Prerequisites: CPython 3.11 with venv; PostgreSQL 16 on the server; an existing
authenticated Hermes installation with A2A on loopback; and Codex Desktop/CLI
on the workstation. Keep server and client state separate. Preserve existing modern inbox/ledger keys and records when upgrading.

For a new installation, download the wheel and checksums from the versioned GitHub release, without a clone. The subshell stops on any failed download or checksum. If the venv already exists, verify its version and use the upgrade procedure instead of overwriting a running installation:

```bash
(
set -eu
gateway_venv="$HOME/.local/share/hermes-a2a-gateway/venv"
if [ -e "$gateway_venv" ]; then
  echo "Existing venv: inspect the installed version and follow Upgrade and rollback." >&2
  exit 1
fi
release_dir=$(mktemp -d)
release_url=https://github.com/phamviet86/hermes-a2a-gateway/releases/download/v0.7.0
wheel=hermes_a2a_gateway-0.7.0-py3-none-any.whl
sdist=hermes_a2a_gateway-0.7.0.tar.gz
curl --fail --location --output "$release_dir/$wheel" "$release_url/$wheel"
curl --fail --location --output "$release_dir/$sdist" "$release_url/$sdist"
curl --fail --location --output "$release_dir/SHA256SUMS" "$release_url/SHA256SUMS"
if command -v shasum >/dev/null 2>&1; then
  (cd "$release_dir" && shasum -a 256 -c SHA256SUMS)
else
  (cd "$release_dir" && sha256sum --check SHA256SUMS)
fi
python3.11 -m venv "$gateway_venv"
"$gateway_venv/bin/python" -m pip install "$release_dir/$wheel"
"$gateway_venv/bin/hermes-a2a-gateway" --version
)
```

Stop if checksum verification fails. Keep the verified wheel for rollback and
record its digest with the deployed source revision. Do not install into Hermes's own Python environment. Use an independent Python 3.11 installation; do not remove an older venv's interpreter until its dependents have been replaced.

## Server configuration

Create a dedicated PostgreSQL database and an unprivileged role owning only that
database. Prefer Unix socket peer authentication under the broker service user;
otherwise inject the database credential through a protected environment file.
Do not expose PostgreSQL to client workstations. The broker creates additive
tables in its own database; it never imports legacy SQLite records.

Create a private configuration directory (0700) and broker environment file
(0600). Required values:

| Environment variable | Meaning |
| --- | --- |
| `HERMES_A2A_GATEWAY_BROKER_DATABASE_URL` | Dedicated PostgreSQL DSN |
| `HERMES_A2A_GATEWAY_BROKER_DEVICE_TOKENS` | JSON mapping device IDs to distinct long random tokens |
| `HERMES_A2A_GATEWAY_BROKER_ENCRYPTION_KEY` | Fernet key kept outside PostgreSQL |
| `HERMES_A2A_GATEWAY_BROKER_PUBLIC_URL` | Verified private HTTPS URL |
| `HERMES_A2A_GATEWAY_BROKER_HOST` | `127.0.0.1` behind the TLS proxy |
| `HERMES_A2A_GATEWAY_BROKER_PORT` | `8790`, or an unused loopback port |
| `HERMES_A2A_ENDPOINT` | Existing loopback A2A endpoint, usually `http://127.0.0.1:9900` |
| `HERMES_A2A_TOKEN` | Existing peer token, only when the peer requires it |

Use a cryptographically random token per device and a separately generated Fernet
key; write them directly to private files rather than terminal output/history.
Transport the device token through an authenticated administrative channel.
Do not place credentials in command arguments, MCP config, Git, or a shared log.
Revoking a device means removing its token from the broker mapping and restarting
the single broker after active work is reconciled.

Example user systemd unit, replacing the executable and environment-file paths
with the actual installation:

```ini
[Unit]
Description=Hermes A2A Gateway broker
After=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/hermes-a2a-gateway/broker.env
ExecStart=%h/.local/share/hermes-a2a-gateway/venv/bin/hermes-a2a-gateway broker
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

Run exactly one broker against this ledger. Use a dedicated TLS reverse proxy
bound to the private LAN/VPN interface. Its certificate must match the chosen
hostname or IP SAN. Trust its CA explicitly on clients; never use `verify=False`
or curl `-k`. Example nginx location inside that TLS server:

```nginx
location / {
    proxy_pass http://127.0.0.1:8790;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-Proto https;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    client_max_body_size 10m;
}
```

`/healthz` is public readiness only. All operation, receipt, event and artifact
routes require the device bearer token. Verify an unauthenticated operation
request is denied before registering the client. No model call is needed for
this check.

## Workstation client and Desktop MCP

Prepare a private client environment file and persistent launcher. Required
client values:

| Environment variable | Meaning |
| --- | --- |
| `HERMES_A2A_GATEWAY_CLIENT_BROKER_URL` | Same private HTTPS URL |
| `HERMES_A2A_GATEWAY_CLIENT_TOKEN` | This device's token only |
| `HERMES_A2A_GATEWAY_CLIENT_CA_FILE` | Absolute private CA certificate path when needed |
| `HERMES_A2A_GATEWAY_CLIENT_STATE_DIR` | Short absolute private directory for SQLite and Unix socket |
| `HERMES_A2A_GATEWAY_CLIENT_PAYLOAD_KEY` | Independent Fernet key for local queued payloads |
| `HERMES_A2A_GATEWAY_CLIENT_CODEX_COMMAND` | Absolute tested Codex executable |

The launcher loads the protected environment and runs the installed executable,
so a GUI-launched Desktop does not depend on interactive shell exports. For
example, a private launcher can use `set -a`, source its own trusted environment
file, `set +a`, then `exec` the installed gateway with `"$@"`. Never source
model-provided files. Start that launcher with `client` as a user service
(launchd on macOS or systemd on Linux). Use `RunAtLoad`/`KeepAlive` or
`Restart=on-failure` only for this new daemon, with a 0077 umask. Preserve unrelated
services and do not run two writers for the same state directory.

Register only the MCP facade under a distinct name:

```bash
codex mcp add hermes-a2a-gateway -- /absolute/path/to/private/launcher client-mcp
/absolute/path/to/private/launcher client-doctor
codex mcp get hermes-a2a-gateway
```

After `mcp add`, add `tool_timeout_sec = 90` to the existing
`[mcp_servers.hermes-a2a-gateway]` table in your Codex `config.toml`; preserve its
command, arguments and other settings. The gateway allows waits up to 60 seconds
and its local MCP request budget is 75 seconds. [Codex defaults to 60 seconds](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
so the host needs this larger budget to return a maximal wait reliably.

Open a fresh Desktop task to discover the new tools. A harmless explicit smoke
request should call `gateway_submit`, then retrieve its handle with `gateway_get`
or `gateway_wait`. Native thread and turn identity come from MCP metadata, never
from tool arguments. Calls without trustworthy native metadata fail closed.
Only the five `gateway_*` tools are supported. Remove obsolete MCP registrations
using the migration guide so agents cannot choose a retired tool.

For a late result, the daemon stores the result first and may queue a reference
into the originating native task. That reference asks the assistant to retrieve
the exact operation; result text is not inserted into native instructions.
`gateway_cancel` is best effort. `gateway_upload_artifact` transfers bounded file
bytes only when explicitly called; uploads are inert data, not executable content.
Only valid UTF-8 `text/plain` can be attached to a Hermes operation: the adapter
adds bounded, delimited reference text to the user message. Other MIME types can
be stored/downloaded but are rejected as operation inputs before acceptance.
This beta does not provide general multimodal attachment interpretation.

## Retention, failure states and operations

Broker defaults are one day for encrypted prompt payloads and uploaded artifacts,
seven days for replay events and terminal results, 10 MiB per upload, 100 MiB stored
artifacts per device, and 100 pending operations per device. See `.env.example`
and configuration validation for adjustable bounds. Expired data is unavailable;
backups need a matching retention policy and the encryption key. Do not rotate
an encryption key until pending work and retained data are deliberately migrated
or expired. Archived stores retain their original access and retention controls.

`HERMES_A2A_GATEWAY_BROKER_PEER_TIMEOUT_SECONDS` is the absolute submission-stream
deadline (default 300 seconds, configurable up to 3600). Set it deliberately for
long jobs; it is separate from the short MCP inline wait. This Hermes runtime may
stop execution when its stream disconnects, including broker shutdown or deadline
expiry. Persisted handles support exact result reconciliation, not a guarantee
that computation survives a lost connection. Never replay work to conceal that
failure. Prefer an idle maintenance window for upgrades. Broker shutdown bounds
HTTP/SSE graceful draining to five seconds before cancelling connections and
releasing its dispatcher; this bounds shutdown, not upstream cancellation.
`HERMES_A2A_GATEWAY_BROKER_MAX_CONCURRENT_STREAMS` defaults to four (range 1–32).
Independent flows may execute concurrently; one flow remains serialized. Reads
and best-effort cancellation have their own bounded work slots so a quiet
long-running stream does not block them.

- Exact duplicate operation IDs return the original operation. Conflicting
  content for the same ID is rejected. A new explicit request needs a new ID.
- An interrupted Hermes dispatch without an exact remote handle remains
  `outcome_unknown`. Do not resend it to repair delivery.
  With a saved handle, exact GET may resolve uncertainty under the same result
  identity. Only a definitive final state triggers native result delivery;
  reading the unknown status does not consume a later result.
- SSE reconnect replays persisted device events. If retention creates a gap,
  clients fetch their exact known operations and resume from the supplied cursor.
- An uncertain native queue insertion remains `delivery_outcome_unknown`.
  Reusing `clientUserMessageId` is not native deduplication. Explicit get/wait
  remains available; queue acknowledgement does not prove user consumption.
- Native queue delivery is gated on installed version/schema. Idle wake was
  observed on tested versions; unattended wake of offline/unloaded hosts is not
  guaranteed. Unsupported versions require explicit pull.
- Uploaded files are not virus-scanned by this beta. Do not claim scanning or
  trust untrusted attachments merely because they passed size/digest checks.
- The broker does not implement a human-input continuation protocol. A peer
  request for input is reported as a failed operation requiring operator review;
  legacy inbound/plugin continuation is not included in this release.

Back up PostgreSQL and the client directory consistently, with keys backed up
separately under appropriate access controls. Avoid logging Authorization headers,
request bodies, prompts, results or Fernet keys in the proxy or service manager.

## Upgrade and rollback

Record the current wheel digest and stop only the new daemon/broker units before
switching their venvs. Keep their stores and key files. Restart, run readiness and
retrieve a known operation before submitting new work. Never manually reset an
unknown record or delete the ledger to make a health check pass.

Follow [the migration and rollback guide](migration-v0.7.md) for exact cleanup
boundaries and data-preserving rollback. Keep private backups, but remove old
operational services and MCP registrations after the new path passes its checks.
An archived old binary must never run against a newer inbox or ledger.

## Managed SSH transport

Set `HERMES_A2A_GATEWAY_CLIENT_TRANSPORT_MODE=ssh-tunnel` and use the [SSH configuration and recovery guide](ssh-tunnel.vi.md). The daemon owns one loopback forward, retains verified HTTPS and device authentication, and reconnects connectivity without blindly replaying agent work. The MCP facade never owns an SSH process.
