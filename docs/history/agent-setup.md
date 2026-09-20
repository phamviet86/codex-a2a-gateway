# Agent-led workstation setup — v0.5.1

> For the v0.6 remote server/client topology, use the [Hermes server guide](server-hermes.vi.md) followed by the [Codex client guide](client-codex.vi.md). The setup skill described below configures the retained local modes; it does not provision the PostgreSQL broker or client daemon.

Install the versioned `v0.5.1` release wheel without cloning. It includes `install-skills`, setup/usage guidance and direction-specific `doctor`. The earlier `v0.4.0` wheel does not include these additions. Full deployment operations are documented in [deployment.md](deployment.md).

Give the agent this request:

> Install Codex A2A Gateway v0.5.1 from its release wheel into a dedicated Python 3.11 environment and install its setup and usage skills. Then use `codex-a2a-setup` to configure the requested direction on this machine. Reuse existing credentials and settings; ask only for required missing values, including the exact inbound workspace. Preserve other MCP entries and state. Verify the selected transport and client tool discovery. Do not run a live model task or enable automatic startup unless I authorize those steps.

Supply `outbound`, `inbound`, or `both` when known. Inbound also needs the workspace and intended A2A caller; generic clients do not need Hermes. Outbound needs a configured Hermes default agent and local A2A platform. Codex and Hermes retain their provider authentication; this project has no universal API key.

## Install the release

The agent runs these commands (replace `python3.11` with the selected compatible interpreter if necessary):

```bash
python3.11 -m venv "$HOME/.local/share/codex-a2a-gateway/venv"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install \
  "https://github.com/phamviet86/hermes-a2a-gateway/releases/download/v0.5.1/codex_a2a_gateway-0.5.1-py3-none-any.whl"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/codex-a2a-gateway" install-skills
```

This is a regular installed package, not an editable checkout dependency. Verify the release SHA256SUMS as shown in the deployment guide when downloading assets. For development from a reviewed checkout, replace the wheel URL with `.` while in that checkout, or use an absolute path to a freshly built wheel.

The install command puts `codex-a2a-setup` and `codex-a2a` in `$CODEX_HOME/skills` when set, otherwise `~/.agents/skills`. `--dest /absolute/skills-root` overrides the destination, including for other agents; the selected client must actually discover that directory. Existing Codex sessions may need reopening for skills/tool changes. The setup skill drives normal CLI/configuration operations; there is no separate interactive setup wizard.

## Installer contract

- `install-skills --dry-run`: read-only plan; creates no directory.
- `install-skills --check`: read-only verification; exits 2 for missing/different/conflicting installations, 0 when current.
- Identical install: no rewritten files.
- Differing managed skill: review changes, then `--replace`. An unmanaged directory or symlink is never replaced by this command. Other skills and unrelated additions within an installed skill are preserved.
- `references/runtime.json`: generated nonsecret absolute Python command and distribution/version. It lets the skill run the installed CLI from any working directory without PATH assumptions. It preserves the virtual environment's Python path rather than resolving it to system Python. Reinstall skills after moving the runtime.

The installer changes skills only: it does not register MCP, start services, modify Hermes/Codex configuration, or store credentials. Those are explicit setup-skill actions within the user's selected deployment scope.

## Configure and verify

The setup skill chooses direction, inspects prerequisites, reuses authentication, and writes only selected configuration. It can author a private persistent launcher for GUI startup using absolute paths, nonsecret environment settings, and a reference to an existing protected secret source. This is a setup workflow, not a bundled credential manager. Gateway secrets still come only from environment variables. Never paste keys into chat or put them in skills, client command arguments or logs.

For inbound, persist the exact workspace and absolute Codex executable. For new `both` installations, use separate MCP and inbound state files/launchers; preserve existing state and require a deliberate migration decision before changing its ownership. MCP `serve` is started by its client. Foreground inbound/Hermes gateways use the existing supervisor; auto-start service creation is optional, not part of package installation.

`doctor` retains its outbound default. `doctor --mode inbound` checks local gateway health/discovery, workspace and executable presence without Hermes or a model turn. `doctor --mode both` checks both transports using the current environment; when using separate launchers, run each direction's check through its own launcher. Read-only readiness explicitly leaves sign-in, model execution and worker tools unverified. Live verification is opt-in.

A gateway-created Codex App Server worker uses its execution host/Codex configuration. A same-host MCP registration may already be available; Desktop-only injected tools/plugins are not inherited. If the requested workflow includes `local-rag-mcp`, verify discovery and an authorized representative retrieval in the actual worker context before claiming integration.

## MCP compatibility

The outbound server uses the maintained MCP Python SDK (`>=2.0,<3`) for negotiation and stdio framing. Tests exercise modern `2026-07-28` and legacy `2025-11-25` client flows, discovery, seven tool schemas, structured results with matching text fallback, invocation failures marked `isError`, and JSON-RPC invalid-parameter errors for unknown tools. Stdout must contain protocol frames only. This is scoped stdio compatibility evidence, not a claim of every optional MCP feature or every client being certified.
