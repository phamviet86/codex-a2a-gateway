# Codex A2A Gateway

[English](README.md) | **Tiếng Việt**

**Public beta · v0.3.0**

`codex-a2a-gateway` giúp Codex giao tiếp hai chiều theo chuẩn A2A v1.0:

- **Codex → Hermes/A2A:** Codex gọi bảy MCP tool qua stdio để giao việc cho Hermes Agent local.
- **Hermes/A2A → Codex:** Hermes hoặc A2A client gọi HTTP/SSE gateway; gateway chuyển task vào Codex App Server.

Với task Hermes → Codex chạy lâu, `a2a_call` built-in vẫn là một lượt đồng bộ. Plugin `codex_a2a` đi kèm `v0.3.0` bổ sung submit sớm, handle bền, poll/cancel, tiếp tục `INPUT_REQUIRED` và không blind resend sau timeout.

Hermes là peer đầu tiên đã được kiểm thử, không phải giới hạn của sản phẩm. Inbound gateway dùng các operation A2A v1.0 phổ biến nên các A2A client tương thích khác cũng có thể kết nối.

> Đây là dự án cộng đồng độc lập, không phải sản phẩm chính thức hay được bảo trợ bởi OpenAI/Codex hoặc Nous Research/Hermes Agent.

> **Phạm vi phiên bản:** wheel `v0.3.0` đã phát hành có plugin durable Hermes, recovery timeout hai chiều, tiếp tục `INPUT_REQUIRED` và extension model/reasoning tùy chọn cho chiều Hermes/A2A → Codex.

## Cài đặt

Yêu cầu CPython `>=3.11,<3.12`, Codex CLI đã đăng nhập và Hermes Agent nếu cần chiều Codex → Hermes.

Trên máy vận hành, cài trực tiếp release wheel vào virtualenv riêng, không cần clone source:

```bash
python3.11 -m venv "$HOME/.local/share/codex-a2a-gateway/venv"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install \
  "https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.3.0/codex_a2a_gateway-0.3.0-py3-none-any.whl"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/codex-a2a-gateway" --version
```

Release wheel và quy trình cài sạch được kiểm tra trên macOS/Linux. Windows/WSL chưa được xác minh. Với luồng từng bước để cài, cấu hình và kiểm tra **cả hai chiều** Codex + Hermes, xem [hướng dẫn thiết lập tiếng Việt](docs/setup-codex-hermes.vi.md). Xem [hướng dẫn triển khai trên máy khác](docs/deployment.md) để migrate state, nâng cấp, rollback hoặc gỡ cài đặt.

Nếu phát triển từ source:

```bash
git clone https://github.com/phamviet86/codex-a2a-gateway.git
cd codex-a2a-gateway
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
```

## Quickstart: Codex → Hermes

Các quickstart này dùng bản cài release wheel ở trên. Khai báo đường dẫn đã cài một lần trong mỗi shell:

```bash
gateway_venv="$HOME/.local/share/codex-a2a-gateway/venv"
gateway_bin="$gateway_venv/bin/codex-a2a-gateway"
```

Nếu chạy từ source checkout thì dùng rõ `.venv/bin/codex-a2a-gateway` của checkout đó; không trộn hai bản cài với cùng một state file.

Bật A2A native của Hermes:

```bash
hermes gateway setup   # chọn A2A
hermes gateway run     # chạy foreground tại 127.0.0.1:9900
```

Trong terminal khác:

```bash
"$gateway_bin" doctor

codex mcp add codex-a2a-gateway -- \
  "$gateway_bin" serve
codex mcp get codex-a2a-gateway
```

Mở lại Codex client để nạp MCP mới. Agent nên gọi `hermes_status`, sau đó `hermes_chat`. Nếu task còn chạy, dùng `hermes_task_wait` hoặc `hermes_task_get`; không gửi lại task khi timeout chưa rõ kết quả.

## Quickstart: Hermes/A2A → Codex

Chạy gateway với workspace mà Codex được phép thao tác:

```bash
CODEX_WORKSPACE_ROOT=/duong-dan-tuyet-doi/toi/workspace \
  "$gateway_bin" gateway
```

Kiểm tra Agent Card:

```bash
curl --fail http://127.0.0.1:9910/.well-known/agent-card.json
```

Gửi một task thử nghiệm vô hại:

```bash
curl --fail-with-body http://127.0.0.1:9910/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"quickstart-1","method":"SendMessage","params":{"message":{"messageId":"quickstart-message-1","role":"ROLE_USER","parts":[{"text":"Reply with exactly CODEX_A2A_OK"}]}}}'
```

Để Hermes chủ động gọi Codex bằng native A2A tools:

```bash
hermes tools enable a2a --platform cli
```

### Plugin reliable Hermes

Wheel `v0.3.0` đã phát hành có plugin này; bật plugin và toolset riêng cho CLI:

```bash
gateway_venv="$HOME/.local/share/codex-a2a-gateway/venv"
gateway_bin="$gateway_venv/bin/codex-a2a-gateway"
test -x "$gateway_bin"
"$gateway_bin" install-hermes-plugin
hermes plugins enable codex-a2a-gateway
hermes tools enable codex_a2a --platform cli
hermes config set plugins.entries.codex-a2a-gateway.settings.endpoint http://127.0.0.1:9910
hermes config set plugins.entries.codex-a2a-gateway.settings.timeout 30
```

Plugin có `codex_a2a_call`, `codex_a2a_get`, `codex_a2a_wait`, `codex_a2a_list`, `codex_a2a_cancel`; nó chỉ lưu metadata handle, không lưu result/artifact Codex, luôn dùng `returnImmediately: true` và timeout thành `outcome_unknown`. Recovery cần `requestMessageId` đã lưu khớp chính xác và đúng một candidate chưa gắn handle từ `ListTasks(contextId)`; nếu không task vẫn unknown. Endpoint bị giới hạn loopback.

Plugin đọc `plugins.entries.codex-a2a-gateway.settings.endpoint` và `.timeout`, không đọc native peer map `a2a_agents`; cần đặt endpoint này khi dùng một port loopback khác `9910`.

Installer ghi vào `$HERMES_HOME/plugins/codex-a2a-gateway` (hoặc `~/.hermes/plugins/codex-a2a-gateway` khi chưa đặt `HERMES_HOME`). Để trả lời `TASK_STATE_INPUT_REQUIRED`, gọi lại `codex_a2a_call` với cùng local `task_id`/handle và message mới; plugin dùng lại remote task và từ chối đổi model/reasoning. Không gọi đồng thời hai plugin call cho cùng một handle.

Thêm peer vào `~/.hermes/config.yaml`:

```yaml
a2a_agents:
  codex:
    url: "http://127.0.0.1:9910"
    timeout: 300
```

Chỉ bật `hermes tools enable a2a --platform a2a` khi thật sự cần agent chaining, để tránh vòng lặp Hermes ↔ Codex không chủ ý.

Với A2A client generic, xem flow gửi task → lưu `taskId`/`contextId` → `GetTask`/tiếp tục/hủy ở [deployment guide](docs/deployment.md#5-generic-a2a-client-lifecycle). Mỗi `messageId` phải mới; chỉ dùng `message.taskId` khi task trả `INPUT_REQUIRED`.

### Model/reasoning cho chiều Hermes → Codex

Chỉ chiều Hermes/A2A → Codex có extension opt-in được Agent Card quảng bá. Plugin đi kèm fetch Agent Card loopback trước và không gửi preference request nếu URI chính xác không được quảng bá. Client phải khai báo URI extension ở HTTP header `A2A-Extensions` **và** `message.extensions`, rồi đặt `model`, `reasoning_effort`, `require_exact` tùy chọn ở `message.metadata.executionPreferences`. Gateway dùng `model/list` của Codex App Server, áp policy của receiver, lưu decision requested/effective vào task metadata và truyền App Server `model`/`effort`. `require_exact: true` sẽ reject giá trị không hỗ trợ; nếu không receiver có thể fallback có quyết định rõ. CLI backend từ chối extension, không giả vờ hỗ trợ. Chiều Codex → Hermes không có lựa chọn này. Xem [contract versioned](docs/execution-preferences-extension-v1.md).

## Cách quản lý phiên

- Chiều outbound: `conversation_key` của Codex được ánh xạ tới Hermes `contextId`.
- Chiều inbound: A2A `contextId`/task được ánh xạ tới Codex App Server thread/turn.
- SQLite giữ mapping, task, trạng thái, result/artifact và lỗi tối thiểu qua restart.
- Prompt outbound gốc không được gateway persist.
- Cancel là best-effort; kết quả luôn nói rõ việc computation đã dừng hay chưa vẫn là `unknown`.

## Giới hạn hiện tại

- Inbound chỉ nhận text parts.
- Không hỗ trợ push-notification CRUD/webhook ở inbound gateway.
- SSE dùng lifecycle/artifact events; inbound có thể phát delta nhưng không bảo đảm ranh giới token.
- Hermes TaskStore hiện là in-memory.
- Một SQLite state file chỉ nên có một bộ process writer đang hoạt động.
- Đây là local single-user integration, chưa phải ranh giới cách ly multi-tenant.

## Cấu hình quan trọng

| Biến | Mặc định | Ý nghĩa |
|---|---:|---|
| `HERMES_A2A_ENDPOINT` | `http://127.0.0.1:9900` | A2A root outbound; chỉ loopback. |
| `CODEX_A2A_GATEWAY_STATE_PATH` | thư mục state hệ thống | SQLite state. |
| `CODEX_A2A_HOST` / `CODEX_A2A_PORT` | `127.0.0.1` / `9910` | Inbound bind. |
| `CODEX_A2A_BEARER_TOKEN` | rỗng | Bắt buộc trước khi bind non-loopback. |
| `CODEX_A2A_GATEWAY_BACKEND` | `app-server` | Backend `app-server` hoặc `cli`. |
| `CODEX_WORKSPACE_ROOT` | cwd | Workspace Codex xử lý task. |

`CODEX_A2A_GATEWAY_MAX_CONCURRENCY=4` là giới hạn theo từng process, không phải global cap giữa MCP adapter và inbound gateway chạy tách biệt. Xem [.env.example](.env.example) để biết cấu hình đầy đủ. Tên executable cũ `codex-hermes-a2a-bridge` và các biến `HERMES_BRIDGE_*`/`CODEX_BRIDGE_*` được giữ làm alias tương thích trong v0.2.

## Kiểm thử

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest --cov=codex_a2a_gateway --cov-report=term-missing
```

Test mặc định dùng fake A2A server, không gọi model thật. `doctor` là read-only; `smoke` gửi task Hermes thật và chỉ nên dùng với prompt vô hại.

Xem thêm [hướng dẫn thiết lập Codex + Hermes](docs/setup-codex-hermes.vi.md), [roadmap và tính khả thi](docs/roadmap.vi.md), [kiến trúc v0.2](docs/architecture-v0.2.md), [vận hành inbound](docs/inbound-gateway.md), [báo cáo kiểm thử v0.2](docs/testing-report-v0.2.md), [AGENTS.md](AGENTS.md), [SECURITY.md](SECURITY.md) và [CHANGELOG.md](CHANGELOG.md).
