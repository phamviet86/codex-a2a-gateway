# Deploy on another computer

This guide installs `codex-a2a-gateway` from a versioned GitHub release asset without cloning the source repository. The supported beta topology is one local user running Codex, Hermes, and the gateway on the same macOS or Linux computer.

## Support matrix

| Target | Release status |
|---|---|
| macOS, CPython 3.11 | Clean-wheel install and CI tested |
| Linux, CPython 3.11 | Clean-wheel install and CI tested |
| Windows / WSL | Not yet verified; do not treat the POSIX paths below as supported instructions |
| Docker or a remote multi-user service | Not supported by this beta deployment profile |

The wheel is pure Python (`py3-none-any`), but a complete deployment also depends on host-installed Codex and Hermes executables, their authentication state, workspace access, and local state directories. A portable wheel therefore does not by itself prove every host topology.

## 1. Prerequisites

- CPython `>=3.11,<3.12` with `venv` support.
- Codex CLI installed and signed in for the inbound A2A → Codex direction.
- Hermes Agent installed for the Codex → Hermes direction and for Hermes-native calls into Codex.
- `curl` for the discovery checks below.

Install Codex using the current official [Codex CLI guide](https://learn.chatgpt.com/docs/codex/cli), then run `codex` once and complete sign-in. Check all prerequisites:

```bash
python3.11 --version
codex --version
hermes --version
```

Hermes is optional if the machine only exposes Codex to a different A2A client.

## 2. Install release v0.2.1

Use a dedicated virtual environment so the gateway does not modify the system Python:

```bash
python3.11 -m venv "$HOME/.local/share/codex-a2a-gateway/venv"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install \
  "https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.2.1/codex_a2a_gateway-0.2.1-py3-none-any.whl"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/codex-a2a-gateway" --version
```

Expected version output:

```text
codex-a2a-gateway 0.2.1
```

The release page publishes the SHA-256 digest for each asset. For higher-assurance installation, download the wheel, compare its digest with the release notes, and install that local file.

## 3. Register Codex → Hermes MCP

Start Hermes' native A2A platform in a terminal:

```bash
hermes gateway setup
hermes gateway run
```

Select A2A during setup. In another terminal, check discovery and register the installed executable by absolute path:

```bash
"$HOME/.local/share/codex-a2a-gateway/venv/bin/codex-a2a-gateway" doctor
codex mcp add codex-a2a-gateway -- \
  "$HOME/.local/share/codex-a2a-gateway/venv/bin/codex-a2a-gateway" serve
codex mcp get codex-a2a-gateway
codex mcp list
```

This follows Codex's documented stdio MCP registration form. Restart or open a new Codex client after registration. The MCP process is launched by Codex; do not run `serve` separately.

## 4. Expose Codex as an A2A peer

Choose the exact workspace Codex may operate on, then start one foreground gateway process:

```bash
CODEX_WORKSPACE_ROOT=/absolute/path/to/workspace \
  "$HOME/.local/share/codex-a2a-gateway/venv/bin/codex-a2a-gateway" gateway
```

From another terminal:

```bash
curl --fail http://127.0.0.1:9910/.well-known/agent-card.json
```

To let Hermes call this endpoint through its native A2A tools:

```bash
hermes tools enable a2a --platform cli
```

Add the peer to `~/.hermes/config.yaml`:

```yaml
a2a_agents:
  codex:
    url: "http://127.0.0.1:9910"
    timeout: 300
```

The current [Hermes A2A guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a) documents the platform-specific tool enablement and `a2a_agents` peer map. Keep `--platform a2a` disabled unless intentional agent chaining requires an inbound Hermes task to call another peer.

## 5. State and process ownership

- The default database is `$XDG_STATE_HOME/codex-a2a-gateway/state.sqlite3` when `XDG_STATE_HOME` is set, otherwise `~/.local/state/codex-a2a-gateway/state.sqlite3`.
- Run only one active MCP/gateway writer set against a state file.
- Do not copy a live SQLite file while its processes are writing. Stop the gateway and close Codex before backup or migration.
- Treat the database as sensitive: it can contain results, artifacts, mappings, statuses, and minimal error information.
- A machine migration may copy the stopped database to the same path, but authentication and workspace paths must be configured again on the destination.

## 6. Upgrade, rollback, and uninstall

Stop the foreground gateway and close clients using its MCP process before changing versions.

Upgrade or reinstall v0.2.1:

```bash
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install --upgrade --force-reinstall \
  "https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.2.1/codex_a2a_gateway-0.2.1-py3-none-any.whl"
```

Rollback to v0.2.0 without deleting state:

```bash
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install --force-reinstall \
  "https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.2.0/codex_a2a_gateway-0.2.0-py3-none-any.whl"
```

After either operation, run `--version`, `doctor`, and `codex mcp get codex-a2a-gateway` before resuming work.

To uninstall the executable while preserving state:

```bash
codex mcp remove codex-a2a-gateway
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip uninstall codex-a2a-gateway
```

The commands above do not delete the SQLite state database. Remove state only after making a separate, explicit retention decision.

## Why this beta uses host-native processes

Codex App Server and the Hermes peer are local CLI processes with user authentication, workspace permissions, and persistent host state. Containerizing only the Python gateway would still require mounting credentials, workspaces, sockets or executables, and state, while weakening the simple loopback trust boundary. A container image may be added later for a separately designed remote-service topology; it is not the recommended local deployment for v0.2.x.
