# Kế hoạch triển khai v0.1

## 1. Mục tiêu đã chốt

`codex-hermes-a2a-bridge` là MCP server local để Codex làm “lễ tân” và giao hội thoại/task cho Hermes profile `default` qua A2A v1.0. Bridge không sao chép agent loop, memory, skills, tools hoặc orchestration của Hermes. V0.1 chỉ có đúng bảy high-level tools đã chỉ định; không có tool quản trị model, plugin, update, shell hay service.

Chuỗi dữ liệu:

```text
Codex client --MCP stdio--> MCP server --bridge core--> A2A client
                                                      |
                                                      +--HTTP JSON-RPC/SSE--> Hermes A2A :9900
                                  SQLite <------------+
```

## 2. Kiến trúc ba lớp

### 2.1 MCP server

- Official MCP Python SDK v2, `MCPServer`, transport stdio.
- Khai báo đúng 7 tools, Pydantic/type-hint schemas và structured JSON results.
- `instructions` mở đầu bằng workflow tự đủ nghĩa trong 512 ký tự: dùng `hermes_chat`, giữ `conversation_key`, xử lý `input_required`, dùng get/wait cho async, cancel không bảo đảm dừng computation.
- Tuyệt đối không log thường ra stdout; stdout chỉ dành cho MCP frames. Diagnostics đi stderr.
- MCP server được Codex khởi động on-demand; không cần daemon riêng cho bridge.

### 2.2 Bridge core

- Validate policy, resolve/create context, serialize mỗi context, enforce concurrency/turn budget.
- SQLite giữ mapping bền vững và kết quả tối thiểu.
- Quản lý background jobs trong lifetime của MCP process; sau restart reconcile từ SQLite và Hermes khi có A2A task ID.
- Normalize A2A state/error thành payload ngắn, ổn định cho model.
- Không tự resend `SendMessage`/`SendStreamingMessage` sau timeout hoặc connection ambiguity.

### 2.3 A2A client

- `httpx.AsyncClient`, chỉ loopback HTTP(S) endpoint cấu hình từ env.
- Agent Card canonical trước, fallback legacy card chỉ khi canonical 404.
- Canonical PascalCase operations: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `SubscribeToTask`, `CancelTask`.
- Parse cả canonical `result.task` và bare legacy Task response vì Hermes giữ compatibility.
- SSE parser hiểu JSON-RPC-enveloped `task`, `statusUpdate`, `artifactUpdate`; comment keepalive không thành model output.
- Không follow redirect sang host khác; card-advertised RPC URL phải vẫn là loopback.

## 3. ADR

### ADR-001 — Python 3.11

Chọn Python `3.11.16` có sẵn trên máy. Tạo `.venv` riêng trong project; không dùng venv hoặc Python site-packages của Hermes. Lý do: yêu cầu sản phẩm, async/httpx tốt, SDK MCP hỗ trợ Python 3.10+.

### ADR-002 — MCP stdio

Chọn stdio vì Codex hỗ trợ native local process, không mở thêm port/auth surface và dùng chung config ở `~/.codex/config.toml`. Official OpenAI documentation yêu cầu restart/new client để nạp cấu hình và cho biết Codex đọc server `instructions`.

### ADR-003 — SQLite stdlib

Chọn `sqlite3` stdlib với WAL, busy timeout, foreign keys và transaction ngắn. Mỗi operation mở connection mới; không cần `aiosqlite` ở v0.1 vì workload nhỏ và tránh thêm dependency. I/O được gọi qua core methods ngắn, có lock khi cần.

### ADR-004 — localhost-only

Endpoint mặc định `http://127.0.0.1:9900`. V0.1 từ chối hostname/IP không phải loopback ở settings và Agent Card. Token chỉ đọc từ `HERMES_A2A_TOKEN`, không có tool argument và không persist. Không expose remote endpoint hoặc arbitrary URL.

### ADR-005 — stable bridge task ID

Mỗi call tạo `bridge_task_id`; `a2a_task_id` nullable cho tới khi Hermes trả/event stream cho biết ID. Tool task nhận bridge ID hoặc A2A ID. Cách này cho phép record `outcome_unknown` khi mutating send mất kết nối trước lúc biết remote task.

## 4. Package layout

```text
pyproject.toml
src/codex_hermes_a2a_bridge/
  __init__.py
  settings.py
  models.py
  store.py
  a2a.py
  core.py
  server.py
  cli.py
tests/
  fake_a2a.py
  test_models.py
  test_store.py
  test_core.py
  test_tools_integration.py
  test_mcp_stdio.py
scripts/rollback.sh
```

CLI entrypoint `codex-hermes-a2a-bridge` có subcommands `serve`, `doctor`, `smoke`. `serve` là command đăng ký với Codex.

## 5. Cấu hình

| Env | Default | Policy |
|---|---|---|
| `HERMES_A2A_ENDPOINT` | `http://127.0.0.1:9900` | Chỉ loopback. |
| `HERMES_A2A_TOKEN` | rỗng | Bearer secret; không log/persist. |
| `HERMES_BRIDGE_STATE_PATH` | project-independent user data path | SQLite file; tests override temp. |
| `HERMES_BRIDGE_DEFAULT_TIMEOUT` | `60` | Clamp 1–300 giây. |
| `HERMES_BRIDGE_AUTO_WAIT` | `15` | Auto mode chờ trước khi trả async handle. |
| `HERMES_BRIDGE_SYNC_WAIT` | `30` | Sync inline wait cap; correlation tiếp tục ở worker. |
| `HERMES_BRIDGE_CORRELATION_TIMEOUT` | `300` | Absolute SSE worker lifetime, khớp Hermes reply timeout mặc định. |
| `HERMES_A2A_CONVERSATION_DIR` | `~/.hermes/a2a_conversations` | Read-only recovery fallback theo source Hermes local. |
| `HERMES_BRIDGE_MAX_MESSAGE_CHARS` | `32768` | Reject trước network. |
| `HERMES_BRIDGE_MAX_TURNS` | `5` | Khớp Hermes default anti-loop. |
| `HERMES_BRIDGE_MAX_CONCURRENCY` | `4` | Global outbound semaphore. |

Profile v0.1 cố định là `default`; endpoint root phải là active Hermes profile `default`. Không có model-supplied URL hoặc named specialist routing.

## 6. SQLite schema

### `contexts`

- `context_id TEXT PRIMARY KEY`
- `conversation_key TEXT NOT NULL`
- `profile TEXT NOT NULL DEFAULT 'default'`
- `endpoint TEXT NOT NULL`
- `tenant TEXT NOT NULL DEFAULT ''`
- `status TEXT NOT NULL` (`open|closed`)
- `turn_count INTEGER NOT NULL DEFAULT 0`
- `last_task_id TEXT`
- `created_at`, `updated_at`, `closed_at` ISO UTC
- partial unique index: một context `open` cho mỗi `(conversation_key, profile)`

### `tasks`

- `bridge_task_id TEXT PRIMARY KEY`
- `a2a_task_id TEXT UNIQUE NULL`
- `context_id`, `conversation_key`, `profile`, `endpoint`
- `request_id`, `message_id`, `idempotency_key`, `request_fingerprint`
- `mode`, normalized `state`
- `result_text`, `artifacts_json`, `error_code`, `error_message`
- `cancel_requested INTEGER`, `hop_count INTEGER`
- `created_at`, `updated_at`, `completed_at`

Không persist prompt raw. Fingerprint SHA-256 đủ cho idempotency/loop check; result text được giữ để task get hoạt động sau bridge restart. Không có secret column.

### `events`

- autoincrement ID, bridge task ID, normalized event type/state, message ngắn, timestamp.
- Retain tối đa 100 events/task; keepalive không persist.

## 7. Tool contracts

Mọi tool trả object với `ok`, `state` hoặc `status`, IDs liên quan, message ngắn, `retryable`, `warnings`. Protocol errors không bị biến thành câu prose mơ hồ.

### 7.1 `hermes_status`

Input: không có.

Output: bridge version/state path (không secret), endpoint, DB counts, health, Agent Card summary (`name`, protocol/interface, streaming, skills count), latency, `connected`.

Read-only; không auto-start Hermes.

### 7.2 `hermes_chat`

Input:

- `message` (1..max chars);
- `conversation_key?`, `context_id?`;
- `profile="default"` (literal/default);
- `mode="auto"|"sync"|"async"`;
- `timeout?` (1..300);
- `idempotency_key?`.

Semantics:

- Resolve context; nếu không có key/ID, bridge tạo cả hai và trả lại.
- Nếu idempotency key đã có cùng fingerprint: trả task hiện có, không resend. Key trùng nhưng fingerprint khác: validation conflict.
- Một send cùng context tại một thời điểm.
- `sync`: dùng `SendStreamingMessage`, đợi tối đa `min(timeout, sync_wait)`; worker tiếp tục giữ correlation sau khi trả working handle.
- `async`: tạo worker `SendStreamingMessage`, đợi tối đa startup window để lấy A2A task ID rồi trả handle.
- `auto`: dùng streaming worker; nếu hoàn tất trong `auto_wait` trả final, nếu chưa thì trả working handle.
- Network ambiguity trên send -> `outcome_unknown`, không retry.
- `TASK_STATE_INPUT_REQUIRED` -> normalized `input_required`, `needs_input=true`, câu hỏi ở `message`; lượt sau gọi cùng conversation/context.

### 7.3 `hermes_task_get`

Input `task_id`. Lookup bridge/A2A ID. Nếu nonterminal và có A2A ID, query `GetTask` rồi reconcile; nếu Hermes task mất sau restart, giữ local record và warning `remote_task_not_found`.

### 7.4 `hermes_tasks_list`

Input `conversation_key?`, `status?`, `limit=20` (1..100). Trả local durable tasks newest-first. Không gọi `ListTasks` cho mỗi list; status có thể stale và được ghi rõ. A2A list được cover trong client/integration tests và dùng bởi doctor/smoke diagnostics nếu cần.

### 7.5 `hermes_task_wait`

Input `task_id`, `timeout=30`. Nếu worker đang sống, chờ local completion event. Nếu không, thử `SubscribeToTask` khi có A2A ID; fallback poll `GetTask` với backoff. Timeout trả state hiện tại, không đổi thành failure giả.

### 7.6 `hermes_task_cancel`

Input `task_id`. Đặt `cancel_requested=true`, gọi `CancelTask` nếu có A2A ID, dừng local waiter/worker khi hợp lý. Output luôn tách `cancel_requested` và `computation_stopped=false|unknown`; không tuyên bố Hermes computation đã dừng. Task đã terminal trả no-op trung thực.

### 7.7 `hermes_contexts`

Input `action="list"|"inspect"|"close"`, `conversation_key?`, `context_id?`, `limit=20`. Close chỉ đổi bridge mapping thành closed; không xóa Hermes transcript/task/session và không cancel ngầm.

## 8. State machine

```text
queued -> submitted -> working -> completed
                            |--> input_required
                            |--> failed
                            |--> canceled
                            |--> rejected
queued/submitted/working -> outcome_unknown (ambiguous transport)
```

`input_required` là terminal cho **request task hiện tại** nhưng context vẫn open. Lượt trả lời tiếp theo tạo bridge/A2A task mới cùng context. `cancel_requested` là flag orthogonal vì Hermes cancel không abort live turn.

V0.1.1 recovery cho task `outcome_unknown` không có remote ID: query read-only `ListTasks(contextId)`, loại các ID đã gắn local và chỉ nhận kết quả khi mapping 1:1. Nếu Hermes restart đã xóa TaskStore, đọc conversation JSONL chính thức mà source local persist; vẫn yêu cầu đúng một unresolved task và một user/agent pair chưa gắn. Không có heuristic theo nội dung và không resend.

Allowed transition validation ngăn terminal state bị overwrite bởi late stream output. `outcome_unknown` chỉ được reconcile sang state xác nhận được, không tự resend.

## 9. Retry, timeout, idempotency, cancel

- Retry GET card/health/GetTask/ListTasks tối đa 2 lần với jitter nhỏ cho connect/5xx; không retry 401/403/429 ngay lập tức.
- Không retry SendMessage/SendStreamingMessage sau khi bytes có thể đã tới Hermes.
- Connect timeout 3 giây; tool absolute timeout clamp 300 giây; SSE read timeout có keepalive nhưng absolute deadline vẫn hữu hạn.
- `sync` chỉ chờ inline tối đa 30 giây mặc định rồi trả handle; correlation SSE tiếp tục tối đa 300 giây. `async` vẫn là lựa chọn rõ ràng cho task dài.
- Idempotency là bridge-local theo key+fingerprint; Hermes không dedup.
- Cancel response ghi `cancel_requested`; test không assert process đã dừng.

## 10. Threat model tối thiểu

| Threat | Mitigation v0.1 |
|---|---|
| SSRF/model-chosen URL | Không có URL tool arg; validate endpoint và card RPC URL là loopback; không follow unsafe redirect. |
| Secret leak | Token env-only; HTTP auth header không log; DB/schema không chứa secret; test/report redaction scan. |
| Prompt injection từ Hermes/peer | Hermes inbound privacy framing; bridge không biến output thành commands, chỉ structured tool result. |
| Agent loop | Hop <= 1 mặc định, hard max 2; turn budget 5/context; same-context serialization; không auto-feed output lại Hermes. |
| Resource exhaustion | Message 32 KiB, concurrency 4, timeouts, SQLite event cap, task list cap. |
| Duplicate side effects | Bridge idempotency key; no mutating retry after ambiguity. |
| Cross-conversation mixup | Unique context ID, mapping invariants, task foreign key, context locks. |
| Stdout protocol corruption | Logging stderr only; MCP protocol test parses initialize/list/call. |

## 11. Test plan

### Unit

- Pydantic/settings validation, loopback URL, message/timeout/enum bounds.
- SQLite migrations, active mapping uniqueness, close semantics, lookup by both task IDs.
- State transition legality, input-required, terminal overwrite protection.
- Idempotency reuse/conflict, timeout -> outcome_unknown, cancel truthfulness.
- Context turn/hop/concurrency enforcement.

### Fake A2A integration

- Canonical and legacy Agent Card fallback.
- Canonical SendMessage response, bare legacy response, JSON-RPC errors.
- SSE submitted/working/artifact/terminal and subscribe fallback polling.
- Input-required then continuation same context.
- Get/List/Cancel and all seven MCP tool handlers.
- Timeout/disconnect/429/401/task-not-found.

### MCP stdio

- New subprocess; official SDK client initialize.
- Assert `instructions`, exact seven tool names, schemas.
- `tools/call` for status and fake-backed chat/get/list/wait/cancel/contexts.
- Assert stderr logging cannot corrupt stdout transport.

### Live Hermes 0.20.5

- Card/health and exact version evidence.
- Harmless marker prompt and response.
- Second turn with same context recalls first-turn codeword.
- Live status/get/list/wait/contexts.
- Bounded slow task + cancel; wait for bounded work to finish and verify no test process remains.
- Record tool-by-tool live/fake matrix. Push CRUD is outside seven-tool surface.

### New Codex client

- Register stdio server with `codex mcp add` or minimal TOML update after scoped backup.
- `codex mcp list/get` confirms exact command/env/cwd.
- Launch fresh `codex exec` process; prompt it to call bridge status and chat with unique harmless marker.
- Preserve output evidence with no token/raw secret.

## 12. Deploy

1. Create `.venv` using Python 3.11 and install project editable with dev dependencies.
2. Run unit/fake/MCP tests before touching external config.
3. Back up exact Hermes config/env/plugin state files and `~/.codex/config.toml` to timestamped files with mode 600.
4. Enable only Hermes `a2a-platform` and `gateway.platforms.a2a` root/default on localhost:9900; do not enable outbound A2A toolset for inbound chaining.
5. Run gateway foreground/ephemeral, live smoke, stop cleanly.
6. If stable, install/start Hermes user-level launchd service; no sudo.
7. Register Codex MCP server `codex-hermes-a2a-bridge` pointing at project `.venv/bin/codex-hermes-a2a-bridge serve`, with cwd and state path env. Set safe tool timeout.
8. Restart/new Codex client and execute E2E.

Bridge itself remains on-demand stdio; “deploy bridge” means venv + Codex registration, không phải thêm daemon.

## 13. Rollback

- `codex mcp remove codex-hermes-a2a-bridge` hoặc restore scoped Codex backup.
- `hermes gateway stop`; disable/remove only A2A platform/config values added by this rollout, hoặc restore scoped Hermes backups.
- Không xóa project source hoặc SQLite state mặc định.
- `scripts/rollback.sh` mặc định dry-run/instructional; mutation cần `--apply`, chỉ target semantic MCP entry/A2A service/config đã ghi nhận.
- `.venv` có thể xóa thủ công sau khi MCP entry đã gỡ; không đụng Hermes venv/source.

## 14. Tiêu chí nghiệm thu

- Package v0.1.1 chạy trên Python 3.11 từ venv riêng.
- Exact seven tool names; instructions 512 ký tự đầu tự đủ nghĩa.
- Unit, fake integration và MCP stdio protocol tests pass.
- Hermes default live: card/health, marker reply, multi-turn same context, get/list/wait/status/contexts pass; cancel semantics có bằng chứng trung thực.
- Fresh Codex process thấy tools và hoàn tất ít nhất một Codex → MCP bridge → Hermes → bridge → Codex exchange.
- Hermes A2A và user service local hoạt động sau deploy; Codex config giữ nguyên entries khác.
- README, testing report, changelog, rollback đầy đủ; secret scan không phát hiện credential.
- Không sửa Hermes source, không commit/push.

## 15. Tự rà soát với source Hermes local 0.20.5

Plan đã được đối chiếu `plugins/platforms/a2a/{adapter.py,protocol.py,security.py,tools.py,plugin.yaml}` tại local commit `d736f5d53f1d33fabad5a17cb070eb138b618fb8`:

- Root URL thực sự route vào active profile; deploy phải xác minh active profile là `default`.
- Canonical method là PascalCase; card canonical `/.well-known/agent-card.json`; legacy fallback chỉ cần cho discovery/response parsing.
- Streaming là lifecycle, không token streaming. Auto/async không hứa token progress.
- `INPUT_REQUIRED` chỉ được map đáng tin cậy ở live/root agent, đúng target v0.1.
- Hermes TaskStore in-memory và tối đa 500 terminal tasks; bridge SQLite là nguồn lịch sử bền hơn.
- `CancelTask` không abort live turn; contract đã tách request khỏi actual stop.
- Hermes không idempotent; mutating retry đã bị cấm sau ambiguity.
- Anti-loop mặc định 5/context; bridge dùng cùng budget, không âm thầm tạo context mới để né reject.
- `SubscribeToTask` có thể chỉ phát terminal; wait có polling fallback.
- Server local không token sẽ bind loopback; bridge vẫn hỗ trợ token env-only nhưng không cần mở remote.
- A2A plugin hiện chưa enabled và gateway đang stopped, trái với giả định “configured”; deploy sẽ bật tối thiểu sau khi tests fake pass và có backup.

Không có lựa chọn nào trong plan làm thay đổi mục tiêu đã giao hoặc cần quyền ý định mới. Các write ngoài project vẫn phải xin filesystem permission của môi trường trước khi thực hiện.

## Nguồn

- [OpenAI Docs: Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)
- [Hermes A2A reference](./hermes-a2a-reference.md)
- [Bridge design research](./mcp-bridge-design.md)
