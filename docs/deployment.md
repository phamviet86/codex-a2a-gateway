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

## 2. Install release v0.2.2

Use a dedicated virtual environment so the gateway does not modify the system Python:

```bash
python3.11 -m venv "$HOME/.local/share/codex-a2a-gateway/venv"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install \
  "https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.2.2/codex_a2a_gateway-0.2.2-py3-none-any.whl"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/codex-a2a-gateway" --version
```

Expected version output:

```text
codex-a2a-gateway 0.2.2
```

For a higher-assurance installation, download the wheel, source distribution, and `SHA256SUMS` release asset, verify the downloaded files against that manifest, then install the verified wheel. This works on macOS (`shasum`) and Linux (`sha256sum`):

```bash
release_dir=$(mktemp -d)
release_url="https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.2.2"
wheel="codex_a2a_gateway-0.2.2-py3-none-any.whl"
sdist="codex_a2a_gateway-0.2.2.tar.gz"

curl --fail --location --output "$release_dir/$wheel" "$release_url/$wheel"
curl --fail --location --output "$release_dir/$sdist" "$release_url/$sdist"
curl --fail --location --output "$release_dir/SHA256SUMS" "$release_url/SHA256SUMS"
if command -v shasum >/dev/null 2>&1; then
  (cd "$release_dir" && shasum -a 256 -c SHA256SUMS)
else
  (cd "$release_dir" && sha256sum --check SHA256SUMS)
fi
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install "$release_dir/$wheel"
```

If verification fails, do not install the file. Delete the temporary directory and download again from the release page; do not substitute a manifest or digest from an untrusted source.

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

## 5. Generic A2A client lifecycle

This is an intentional live Codex task, not a read-only health check. With the gateway already running on loopback, submit a harmless request, save its identifiers, and poll it through the canonical A2A operation. `returnImmediately` makes submission return the task handle instead of waiting for Codex to finish:

```bash
gateway_venv="$HOME/.local/share/codex-a2a-gateway/venv"
gateway_python="$gateway_venv/bin/python"
run_id=$(date +%s)
context_id="quickstart-context-$run_id"
message_id="quickstart-message-$run_id"

response=$(curl --fail-with-body http://127.0.0.1:9910/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"SendMessage\",\"params\":{\"message\":{\"messageId\":\"$message_id\",\"role\":\"ROLE_USER\",\"contextId\":\"$context_id\",\"parts\":[{\"text\":\"Reply with exactly CODEX_A2A_OK\"}]},\"configuration\":{\"returnImmediately\":true}}}")
printf '%s\n' "$response"
task_id=$(printf '%s' "$response" | "$gateway_python" -c 'import json, sys; print(json.load(sys.stdin)["result"]["task"]["id"])')
printf 'contextId=%s\ntaskId=%s\n' "$context_id" "$task_id"

while :; do
  task=$(curl --fail-with-body http://127.0.0.1:9910/ \
    -H 'Content-Type: application/json' \
    -H 'A2A-Version: 1.0' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"GetTask\",\"params\":{\"id\":\"$task_id\"}}")
  printf '%s\n' "$task"
  task_state=$(printf '%s' "$task" | "$gateway_python" -c 'import json, sys; print(json.load(sys.stdin)["result"]["status"]["state"])')
  case "$task_state" in
    TASK_STATE_COMPLETED|TASK_STATE_FAILED|TASK_STATE_CANCELED|TASK_STATE_REJECTED|TASK_STATE_INPUT_REQUIRED) break ;;
  esac
  sleep 1
done
```

After the task reaches `TASK_STATE_COMPLETED`, send a normal follow-up with a **new** `messageId` and the same `contextId`; do not resend the original message after an ambiguous result:

```bash
curl --fail-with-body http://127.0.0.1:9910/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"SendMessage\",\"params\":{\"message\":{\"messageId\":\"$message_id-follow-up\",\"role\":\"ROLE_USER\",\"contextId\":\"$context_id\",\"parts\":[{\"text\":\"Reply with exactly CODEX_A2A_FOLLOW_UP_OK\"}]}}}"
```

If the first task returns `TASK_STATE_INPUT_REQUIRED`, answer that task with a new `messageId` and `message.taskId`; omit `contextId` because the gateway derives and validates it from the task. For a normal completed task, use `contextId` as above instead.

```bash
curl --fail-with-body http://127.0.0.1:9910/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"SendMessage\",\"params\":{\"message\":{\"messageId\":\"$message_id-input\",\"taskId\":\"$task_id\",\"role\":\"ROLE_USER\",\"parts\":[{\"text\":\"Your answer to the requested input\"}]}}}"
```

Before polling reaches a terminal or input-required state, use `CancelTask` to request cancellation of a still-active task. A successful protocol response never proves that the underlying Codex computation stopped; inspect the returned task status and treat `computationStopped: unknown` as authoritative.

```bash
curl --fail-with-body http://127.0.0.1:9910/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"CancelTask\",\"params\":{\"id\":\"$task_id\"}}"
```

For streaming instead of polling, call `SendStreamingMessage` with the same message shape and consume JSON-RPC-enveloped SSE frames. The first frame contains `task`; later frames contain `statusUpdate` or `artifactUpdate`, and token boundaries are not guaranteed.

## 6. State and process ownership

- The default database is `$XDG_STATE_HOME/codex-a2a-gateway/state.sqlite3` when `XDG_STATE_HOME` is set, otherwise `~/.local/state/codex-a2a-gateway/state.sqlite3`.
- Run only one active MCP/gateway writer set against a state file.
- Do not copy a live SQLite file while its processes are writing. Stop the gateway and close Codex before backup or migration.
- Treat the database as sensitive: it can contain results, artifacts, mappings, statuses, and minimal error information.
- A machine migration may copy the stopped database to the same path, but authentication and workspace paths must be configured again on the destination.

## 7. Upgrade, rollback, and uninstall

Stop the foreground gateway and close clients using its MCP process before changing versions.

Upgrade or reinstall v0.2.2:

```bash
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install --upgrade --force-reinstall \
  "https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.2.2/codex_a2a_gateway-0.2.2-py3-none-any.whl"
```

Rollback to v0.2.1 without deleting state:

```bash
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install --force-reinstall \
  "https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.2.1/codex_a2a_gateway-0.2.1-py3-none-any.whl"
```

After either operation, run `--version`, `doctor`, and `codex mcp get codex-a2a-gateway` before resuming work.

To uninstall the executable while preserving state:

```bash
codex mcp remove codex-a2a-gateway
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip uninstall codex-a2a-gateway
```

The commands above do not delete the SQLite state database. Remove state only after making a separate, explicit retention decision.

## 8. Troubleshooting

| Symptom | Safe check and response |
|---|---|
| `--version` is not `0.2.2`, or wheel installation fails | Repeat the manifest-verified install above. Do not use a cached or differently named wheel as a substitute. |
| `doctor` reports Hermes unreachable | Keep `HERMES_A2A_ENDPOINT` on loopback, confirm `hermes gateway run` is active, then rerun `doctor`. Do not weaken the loopback-only endpoint policy to reach an arbitrary remote URL. |
| Codex does not show the MCP server | Run `codex mcp get codex-a2a-gateway`, verify its command is the installed absolute `$HOME/.local/share/codex-a2a-gateway/venv/bin/codex-a2a-gateway` path, then restart or open a new Codex client. Do not start `serve` manually. |
| Agent Card or `/health` is unavailable | Confirm the foreground `gateway` process is still running and the selected `CODEX_WORKSPACE_ROOT` is accessible. If port 9910 is occupied, choose another loopback `CODEX_A2A_PORT` and update the A2A peer URL together. |
| An inbound task fails before a Codex reply | Confirm `codex --version`, sign-in, and workspace access. Keep `app-server` as the default backend; `cli` is only an explicit compatibility mode and does not support interactive input-required handling. |
| A task is ambiguous after a timeout or restart | Query `GetTask`/`ListTasks` with the saved IDs. Do not resend a mutating message; the gateway records conservative recovery state and may require replay of the original `messageId` only for a task that never reached the backend. |

## Why this beta uses host-native processes

Codex App Server and the Hermes peer are local CLI processes with user authentication, workspace permissions, and persistent host state. Containerizing only the Python gateway would still require mounting credentials, workspaces, sockets or executables, and state, while weakening the simple loopback trust boundary. A container image may be added later for a separately designed remote-service topology; it is not the recommended local deployment for v0.2.x.
