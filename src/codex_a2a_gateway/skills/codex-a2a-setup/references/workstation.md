# Workstation configuration

## Persistent runtime and secrets

The runtime command in `runtime.json` is `[absolute_python, "-m", "codex_a2a_gateway.cli"]`. Append `serve`, `gateway`, `doctor`, etc. without requiring a `PATH` lookup. Discover the intended host's absolute Codex/Hermes executables once and preserve them.

For nondefault endpoint/workspace/token settings, create a user-owned launcher in a private application configuration directory, separate from the installed skill. Generate its literal paths with a proper shell-quoting function such as Python `shlex.quote`; do not interpolate user text into shell source. A small `#!/bin/sh` launcher can export nonsecret selected values, load an existing protected environment source when needed, then `exec` the runtime command followed by `"$@"`. Use mode 0700 for its directory and launcher. Do not overwrite an existing custom launcher without reviewing its contents and retaining unrelated configuration.

Store `CODEX_WORKSPACE_ROOT` explicitly for inbound; a GUI process's current directory is not a workspace choice. Store `CODEX_CLI_BIN` as the discovered absolute executable path. Optional nonsecrets include `HERMES_A2A_ENDPOINT`, `CODEX_A2A_HOST`, `CODEX_A2A_PORT`, and a dedicated `CODEX_A2A_GATEWAY_STATE_PATH`. Run one active writer per state file. For a new `both` installation, give outbound MCP and inbound gateway separate launchers with separate persistent `CODEX_A2A_GATEWAY_STATE_PATH` values (for example `outbound.sqlite3` and `inbound.sqlite3`). Run each direction's doctor through its matching launcher. Preserve existing databases and registrations; changing an existing topology requires an explicit migration decision, not an automatic move or copy of live state.

Gateway secrets are read from the process environment only. Prefer a pre-existing OS credential loader or operator-managed environment source. If a protected source is required, have the operator provision it through a private input channel; use 0600 permissions and a 0700 parent, and have the launcher load it without printing it. Reference its absolute path; do not copy tokens into skills, client arguments, README, logs, shell history, or generated runtime.json. The source is shell code only when deliberately managed as such; never `source` an untrusted downloaded `.env` file. Preserve existing credentials. Optional tokens: `HERMES_A2A_TOKEN` for authenticated Hermes; `CODEX_A2A_BEARER_TOKEN` for inbound gateway authentication. The current bundled Hermes plugin has no token setting, so use its default unauthed loopback deployment or an explicitly compatible client for a token-protected endpoint.

No service is needed for MCP `serve`: the MCP client starts it. For inbound `gateway` and the Hermes gateway, use the existing foreground/supervisor mechanism. Auto-start is optional user scope; only then create a platform-appropriate user service referencing the launcher, verify start/stop/restart, and keep credentials out of the service definition. This package does not ship service units. Remote/public or multi-user deployment is a separate architecture, not a setup default.

## Outbound: Codex → Hermes

Check the installed Hermes version and configuration. If the native A2A platform is absent, use that installed version's `hermes gateway setup` guidance to enable it and run `hermes gateway run`. Do not replace the user's provider configuration. Default root: `http://127.0.0.1:9900`; only loopback endpoints are accepted. Do not enable inbound Hermes `a2a` tool chaining merely to expose the platform.

Run the gateway command with `doctor --mode outbound`. Inspect `codex mcp get codex-a2a-gateway` before changing it. Register only this server using the selected Codex executable's documented `mcp add` command:

```text
codex mcp add codex-a2a-gateway -- /absolute/path/to/launcher serve
```

With default environment settings, register the absolute Python command from runtime.json followed by `serve` directly instead. Use subprocess argument arrays when paths contain spaces. If the entry is already correct, retain it; if different, change only this entry after preserving its prior value. Open a new client session and discover seven `hermes_*` tools, then call `hermes_status`. Never start `serve` separately or log setup messages to its stdout.

An authorized live smoke uses `smoke --conversation-key <unique-key> 'Reply with exactly HERMES_A2A_OK'`. This creates a model task; `doctor` does not.

## Inbound: A2A → Codex

Use the user's exact workspace; check the selected Codex executable and existing sign-in (for example, the installed CLI's `login status`, without exposing credential files). Keep the default `app-server` backend. Configure workspace and executable in the persistent launcher, then run it with `gateway`. Default endpoint: `http://127.0.0.1:9910`. Preserve existing approval policy; do not weaken it to make a readiness check pass.

Run the same launcher with `doctor --mode inbound`. It checks `/health` and `/.well-known/agent-card.json`, executable and workspace presence; authentication, model execution and worker tools remain unverified. A generic A2A client can use the advertised JSON-RPC interface directly and needs no Hermes installation.

Only when the user selected Hermes as the inbound caller, run `install-hermes-plugin`, then the installed Hermes CLI commands:

```text
hermes plugins enable codex-a2a-gateway
hermes tools enable codex_a2a --platform cli
hermes config set plugins.entries.codex-a2a-gateway.settings.endpoint http://127.0.0.1:9910
hermes config set plugins.entries.codex-a2a-gateway.settings.timeout 30
```

Use the selected port. The plugin reads these settings, not the native `a2a_agents` peer map. Do not enable the synchronous native `a2a_call` toolset as a substitute. Existing differing plugin files require review before `install-hermes-plugin --replace`.

An authorized generic-client test uses `SendMessage` with a unique `messageId`, text parts, and `configuration.returnImmediately: true`; retain the returned task/context IDs and poll `GetTask`. For `TASK_STATE_INPUT_REQUIRED`, answer with a new message ID and existing `taskId`. Do not reinterpret wait expiry as failure or resend an ambiguous request. Gateway results require pull retrieval; they are not automatically injected into a Desktop conversation.
