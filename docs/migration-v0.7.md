# Migrate to v0.7 and remove retired installations

v0.7 is a breaking runtime cleanup. It retains only the modern client/server path
and adds managed SSH. Do not install over a running old venv or delete directories
by a wildcard. Identify exact owned components and their dependencies first.

| v0.6 or retired surface | v0.7 |
| --- | --- |
| Distribution/command `codex-a2a-gateway` | `hermes-a2a-gateway` |
| Python `codex_a2a_gateway` | `hermes_a2a_gateway` |
| MCP `codex-a2a-client` | MCP `hermes-a2a-gateway` |
| `CODEX_A2A_GATEWAY_*` | `HERMES_A2A_GATEWAY_*` |
| `serve`, inbound `gateway`, old `doctor`/`smoke`, plugin/skill installer | Removed |
| Seven `hermes_*` MCP tools / bundled `codex_a2a` Hermes plugin | Removed |
| `codex-hermes-a2a-bridge`, `HERMES_BRIDGE_*`, `CODEX_BRIDGE_*` | Removed |

`HERMES_A2A_ENDPOINT` and `HERMES_A2A_TOKEN` still configure the broker's loopback
Hermes peer. Native Hermes `platforms/a2a`, provider credentials and its built-in
A2A tools are separate dependencies and must not be removed as legacy gateway code.

## 1. Inventory and stage

Record only paths, installed versions, service/MCP names, owners and state identities;
do not print or hash secrets. Inspect macOS LaunchAgents, user systemd, `codex mcp
list/get`, installed distributions, external Hermes plugins and old generated skills.
Distinguish actual operational installs from source checkouts and active development
venvs. Preserve unrelated Codex parents, workspaces, profiles, SSH tunnels and apps.

Follow [release installation](deployment.md#install-the-same-release-on-both-machines)
in a new dedicated venv. Use an independent Python 3.11 interpreter outside any old
root scheduled for removal: a venv symlink may still depend on an old deployment's
bundled Python. Verify `sys.base_prefix` and the resolved interpreter before cleanup.

Canonical suggested paths are:

```text
~/.local/share/hermes-a2a-gateway/venv
~/.config/hermes-a2a-gateway/
~/.local/state/hermes-a2a-gateway/client/
```

## 2. Quiesce, back up and migrate

Reconcile existing operations and schedule a quiet cutover. Stop the old client
daemon, its cached MCP facades and any exact legacy `serve` writers; preserve Codex
itself. Stop only the old gateway broker/proxy services when replacing them. Hermes
A2A and PostgreSQL need not be uninstalled or reset.

Back up exact owned configs, credentials, CA files, service definitions, MCP entries,
legacy stores and the modern client inbox into a private directory (0700; secret
files 0600/0400). Keep a PostgreSQL backup and preserve its matching encryption key.
Do not commit backup material or print tokens/keys to the terminal.

Copy SQLite with its backup API while writers are stopped, or make a checkpointed
consistent copy; copying only an open main database can lose WAL records. Preserve
`client.sqlite3`, all operation/native IDs, receipts, cursor, ciphertext and payload
key. Recreate the socket/lock through the new daemon; do not migrate live socket files.
Legacy local-gateway SQLite is archived separately and must never become a modern inbox.

Keep the existing PostgreSQL database and `broker_v06_*` tables. Database/table names
are persisted identities, not obsolete installations. Preserve the peer context UUID
namespace, device/flow IDs, operations, results, pending/unknown state and keys. Do not
create an empty replacement ledger to make the new name look uniform.

Map environment names programmatically in protected configuration, retaining values.
Update executable/config/state/CA paths to their new actual locations. Rename the MCP
registration and services; do not run old/new registrations against the same inbox.
Suggested service names: `com.hermes.a2a.gateway.client` (macOS),
`hermes-a2a-gateway-client.service`, `hermes-a2a-gateway-broker.service`, and
`hermes-a2a-gateway-proxy.service` (Linux). The new Python package does not accept old
configuration aliases. Do not regenerate payload or broker keys during this mapping.

For SSH mode, keep the existing SSH alias/key, configure the actual VPS TLS destination
and local port, and follow [SSH setup](ssh-tunnel.vi.md). Strict host-key and TLS
verification remain enabled. Start one canonical broker/proxy and one client daemon,
then register the new MCP facade through its private launcher.

## 3. Verify, then remove exact old components

Check downloaded release checksums and installed package identity on both machines.
Verify HTTPS/auth, doctor, the five MCP tools and retained state/operation identities.
Perform an authorized harmless Desktop task with `wait_seconds: 20`; separately test
owned SSH interruption/recovery without duplicate remote work. Queue ACK alone does
not prove native result consumption in Desktop.

After the new installation passes and private backups exist, remove only the inventoried
old operational venvs, gateway config/state directories, service files, old MCP entries
and installed legacy plugin/skill artifacts. If no legacy plugin is installed, record a
no-op. Update obsolete gateway references in operator-managed skills without deleting
unrelated native Hermes instructions. Do not delete actual Hermes, Codex, PostgreSQL,
nginx/shared runtimes, provider authentication, source worktrees or unrelated SSH setup.

A candidate-to-final upgrade uses the same canonical paths/config/ledger. Stop its
writer before changing the venv, install the verified final GitHub release wheel, restart,
and confirm a new uniquely identified harmless operation using the final package.

## Rollback

Stop new services/facades before restoring old executable/config/MCP/service snapshots.
Never restore an older database snapshot over newer work without reconciling it first.
Retain the modern ledger and all keys; do not point legacy binaries at modern stores.
Keep old installation backups available until the operator's retention window expires.
No rollback command should stop native Hermes platforms or unrelated services.

## Ngân sách MCP

Trong table `[mcp_servers.hermes-a2a-gateway]` mới, đặt `tool_timeout_sec = 90` để
chờ tối đa 60 giây ở daemon còn có ngân sách IPC/trả lời. Giữ các table và policy
Codex khác; mở task mới sau khi thay cấu hình.
