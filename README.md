# Codex Hermes A2A Bridge

Bridge local để Codex làm “lễ tân”: Codex gọi MCP tools qua stdio, bridge chuyển yêu cầu thành A2A v1.0/JSON-RPC tới Hermes profile `default`, rồi giữ mapping hội thoại/task trong SQLite. Hermes vẫn là “bộ não” thực hiện agent loop, memory, skills, tools và điều phối nội bộ.

Phiên bản hiện tại: **v0.1.1**. Chỉ bind/call endpoint loopback; không có tool đổi model, plugin, cấu hình, update, shell hoặc điều khiển service Hermes.

> **Independent project:** đây là phần mềm cộng đồng độc lập, không phải sản phẩm chính thức, không được bảo trợ và không đại diện cho Nous Research/Hermes Agent hay OpenAI/Codex. Tên thương hiệu chỉ dùng để mô tả khả năng tương tác.

## Kiến trúc

```text
Codex client --MCP stdio--> MCP server --> bridge core --> Hermes A2A :9900
                                      \--> SQLite context/task mapping
```

- Python 3.11 và venv riêng, không dùng venv Hermes.
- MCP SDK Python chính thức, `httpx` async, Pydantic và SQLite stdlib.
- Mỗi `conversation_key` mở được ánh xạ tới Hermes `contextId`; các lượt sau dùng lại ánh xạ đó.
- Prompt gốc không được persist; bridge lưu fingerprint, route, trạng thái, kết quả và lỗi tối thiểu.

## Yêu cầu và cài đặt nhanh

- Python 3.11.
- Hermes Agent 0.20.5 với A2A gateway chạy trên loopback.
- Codex client có hỗ trợ MCP stdio.

```bash
cd /absolute/path/to/codex-hermes-a2a-bridge
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/codex-hermes-a2a-bridge doctor
```

Contributor có thể cài thêm tool kiểm thử bằng `python -m pip install -e '.[dev]'`. Xem [.env.example](.env.example) để biết các override; không commit file `.env` thật.

Cấu hình an toàn mặc định:

| Biến môi trường | Mặc định | Ý nghĩa |
|---|---:|---|
| `HERMES_A2A_ENDPOINT` | `http://127.0.0.1:9900` | A2A root; chỉ URL loopback được chấp nhận. |
| `HERMES_A2A_TOKEN` | rỗng | Bearer token đọc từ env, không nhận qua tool args. |
| `HERMES_BRIDGE_STATE_PATH` | `~/.local/state/codex-hermes-a2a-bridge/state.sqlite3` | SQLite mode `0600`. |
| `HERMES_BRIDGE_DEFAULT_TIMEOUT` | `60` | Timeout mặc định, clamp tối đa 300 giây. |
| `HERMES_BRIDGE_AUTO_WAIT` | `15` | Thời gian `auto` chờ trước khi trả task handle. |
| `HERMES_BRIDGE_SYNC_WAIT` | `30` | Giới hạn chờ inline của `sync`; sau đó trả handle nhưng correlation tiếp tục. |
| `HERMES_BRIDGE_CORRELATION_TIMEOUT` | `300` | Tuổi thọ SSE worker để giữ A2A task ID/kết quả sau initial timeout. |
| `HERMES_A2A_CONVERSATION_DIR` | `~/.hermes/a2a_conversations` | Fallback read-only khi TaskStore in-memory không còn. |
| `HERMES_BRIDGE_MAX_TURNS` | `5` | Turn budget/context chống agent loop. |
| `HERMES_BRIDGE_MAX_CONCURRENCY` | `4` | Số outbound call đồng thời. |

## Bật Hermes A2A và đăng ký Codex

Trên Hermes 0.20.5 đã cài local:

```bash
hermes plugins enable a2a-platform --no-allow-tool-override
hermes config set gateway.platforms.a2a.enabled true
hermes gateway run --no-supervise
```

Khi foreground pass, có thể cài user service (không `sudo`):

```bash
hermes gateway install --start-now --start-on-login
```

Đăng ký bridge trong cấu hình MCP dùng chung của Codex:

```bash
codex mcp add codex-hermes-a2a-bridge -- \
  /absolute/path/to/codex-hermes-a2a-bridge/.venv/bin/codex-hermes-a2a-bridge serve
codex mcp get codex-hermes-a2a-bridge
```

Phải mở/restart một Codex client mới để đọc entry mới. MCP stdio chỉ ghi protocol frames ra stdout; diagnostics đi stderr.

## Bảy MCP tool v0.1

| Tool | Công dụng |
|---|---|
| `hermes_status` | Health, Agent Card tóm tắt, DB counts và kết nối. |
| `hermes_chat` | Tạo/tiếp tục hội thoại; `auto`, `sync` hoặc `async`; profile `default`. |
| `hermes_task_get` | Reconcile trạng thái, kết quả, lỗi hoặc `input_required`. |
| `hermes_tasks_list` | Liệt kê durable bridge tasks theo conversation/state. |
| `hermes_task_wait` | Chờ active stream, subscribe SSE, rồi polling fallback. |
| `hermes_task_cancel` | Gửi cancel best-effort; không tuyên bố computation đã dừng. |
| `hermes_contexts` | List/inspect/close mapping; close không xóa dữ liệu Hermes. |

Bộ bốn thao tác MVP từng nêu trong nghiên cứu (discover, send, get, continue) **không phải full A2A**. V0.1 gom chúng thành bảy high-level tools phục vụ hội thoại/task; các A2A operation thấp hơn như push notification CRUD và administration của Hermes không được expose trực tiếp.

## Workflow mẫu

1. Codex gọi `hermes_status`.
2. Codex gọi `hermes_chat(message=..., conversation_key=<ổn định>, mode="auto")`.
3. Nếu task còn chạy, dùng `hermes_task_wait` hoặc `hermes_task_get`; không resend mù sau timeout mơ hồ.
4. Nếu `needs_input=true`, hỏi người dùng rồi gọi `hermes_chat` với cùng `conversation_key`/`context_id`.
5. Lượt hội thoại tiếp theo tiếp tục cùng mapping; `hermes_contexts(action="close")` chỉ đóng mapping bridge.

Với tác vụ có side effect, cung cấp `idempotency_key`. Hermes 0.20.5 không có idempotency wire-level, nên bridge không retry mutating send khi kết quả truyền tải không rõ.

Từ v0.1.1, cả ba mode đều dùng `SendStreamingMessage` để nhận A2A task ID ngay ở event đầu. `sync` chỉ chờ inline tối đa 30 giây (hoặc `timeout` nếu nhỏ hơn); stream vẫn sống tới correlation timeout. Với record cũ ở `outcome_unknown` chưa có A2A ID, `hermes_task_get`/`hermes_task_wait` thử `ListTasks(contextId)` trước, rồi mới đọc conversation persistence chính thức của Hermes. Recovery chỉ gắn kết quả khi có đúng một local unresolved task và đúng một remote/disk candidate; trường hợp mơ hồ được giữ nguyên, không resend và không đoán. Disk fallback không có A2A state nên trả warning và coi agent reply đã persist là `completed`.

## Kiểm thử và vận hành

```bash
.venv/bin/pytest --cov=codex_hermes_a2a_bridge --cov-report=term-missing
.venv/bin/codex-hermes-a2a-bridge doctor
.venv/bin/codex-hermes-a2a-bridge smoke \
  'Reply with exactly MY_MARKER and nothing else.' \
  --conversation-key manual-smoke
.venv/bin/python scripts/live_check.py manual-smoke
```

`pytest` dùng fake A2A server trên ephemeral loopback port và không cần Hermes thật. `doctor` và `live_check.py` là read-only. Lệnh `smoke` gửi một task thật; chỉ chạy chủ động với nội dung vô hại.

## Bảo mật và quyền riêng tư

- V0.1.1 từ chối endpoint và Agent Card URL không phải loopback, không follow redirect và không nhận token qua MCP tool arguments.
- SQLite mặc định nằm ngoài source tree với quyền file `0600`; nó lưu mapping, fingerprint, trạng thái, **kết quả/artifact** và lỗi tối thiểu. Kết quả có thể chứa dữ liệu nhạy cảm, vì vậy cần áp dụng retention/backup phù hợp.
- Prompt gốc không được bridge persist, nhưng Hermes có thể ghi conversation/audit log riêng. Fallback recovery chỉ đọc thư mục conversation Hermes được cấu hình.
- MCP server phải được chạy bởi user tin cậy; bảy tool có thể kích hoạt Hermes dùng skills/tools với side effect. Dùng `idempotency_key` và không resend mù khi `outcome_unknown`.
- Báo cáo lỗ hổng theo [SECURITY.md](SECURITY.md). Không đăng token, transcript hoặc SQLite trong issue.

## Guarantees và giới hạn upstream

Bridge bảo đảm policy loopback, mapping local bền vững, không retry mutating send sau ambiguity và semantics cancel trung thực. Bridge **không** bảo đảm Hermes đã dừng computation, token-level streaming, wire-level idempotency hay task persistence qua Hermes restart.

Hermes 0.20.5 dùng TaskStore in-memory, lifecycle SSE và protocol cancel không abort live turn. Conversation-store recovery của bridge là fallback read-only có điều kiện, không thay thế durable task store của upstream. Các chi tiết đã xác minh nằm trong [Hermes A2A reference](docs/hermes-a2a-reference.md).

## Troubleshooting

- `a2a_unreachable`: chạy `hermes gateway status`, kiểm tra card tại `http://127.0.0.1:9900/.well-known/agent-card.json`.
- A2A plugin enabled nhưng không có port: kiểm tra `hermes config get gateway.platforms.a2a.enabled`, rồi restart gateway.
- Codex không thấy tool: chạy `codex mcp get codex-hermes-a2a-bridge`, sau đó dùng process/client Codex mới.
- `outcome_unknown`: gọi `hermes_task_get`/`hermes_task_wait` để bridge tự reconcile; nếu vẫn mơ hồ thì không resend tác vụ có side effect và hỏi người dùng.
- `turn_budget_exceeded`: đóng mapping và tạo conversation mới; không tăng budget chỉ để vòng agent tiếp tục vô hạn.
- Hermes 0.20.5 mất A2A TaskStore khi restart; bridge vẫn giữ local task/result nhưng remote refresh có thể báo task không còn.
- Trên macOS hiện tại, nếu `launchctl bootstrap` trả exit 5, Hermes dùng detached fallback: chạy được nhưng không auto-start/auto-restart. Dùng `hermes gateway status` để xác nhận.

## Rollback

Xem [scripts/rollback.sh](scripts/rollback.sh). Script mặc định chỉ in kế hoạch. `scripts/rollback.sh --apply` gỡ đúng MCP entry và cấu hình/plugin A2A, nhưng giữ gateway service vì service có thể phục vụ platform khác. Chỉ thêm `--include-gateway-service` nếu gateway được cài riêng cho rollout này. Source, `.venv`, SQLite và transcript Hermes được giữ nguyên.

Các backup scoped được tạo cạnh file cấu hình với hậu tố `.pre-codex-hermes-a2a-bridge-v0.1.bak`; không tự động restore toàn file vì có thể ghi đè thay đổi mới của người dùng.

## Tài liệu

- [Kế hoạch triển khai v0.1](docs/implementation-plan-v0.1.md)
- [Báo cáo kiểm thử v0.1](docs/testing-report-v0.1.md)
- [Tham chiếu Hermes A2A v1.0](docs/hermes-a2a-reference.md)
- [Thiết kế MCP bridge](docs/mcp-bridge-design.md)
- [CHANGELOG](CHANGELOG.md)
- [Hướng dẫn đóng góp](CONTRIBUTING.md)
- [Chính sách bảo mật](SECURITY.md)
- [Apache License 2.0](LICENSE)

Nguồn chuẩn: [OpenAI Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [Hermes A2A guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a), [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/tree/main/plugins/platforms/a2a). Khi khác biệt, source local Hermes 0.20.5 commit `d736f5d53f1d33fabad5a17cb070eb138b618fb8` được ưu tiên.
