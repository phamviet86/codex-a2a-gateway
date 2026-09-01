# Codex A2A Gateway

[English](README.md) | **Tiếng Việt**

**Public beta · v0.2.1**

`codex-a2a-gateway` giúp Codex giao tiếp hai chiều theo chuẩn A2A v1.0:

- **Codex → Hermes/A2A:** Codex gọi bảy MCP tool qua stdio để giao việc cho Hermes Agent local.
- **Hermes/A2A → Codex:** Hermes hoặc A2A client gọi HTTP/SSE gateway; gateway chuyển task vào Codex App Server.

Hermes là peer đầu tiên đã được kiểm thử, không phải giới hạn của sản phẩm. Inbound gateway dùng các operation A2A v1.0 phổ biến nên các A2A client tương thích khác cũng có thể kết nối.

> Đây là dự án cộng đồng độc lập, không phải sản phẩm chính thức hay được bảo trợ bởi OpenAI/Codex hoặc Nous Research/Hermes Agent.

## Cài đặt

Yêu cầu CPython `>=3.11,<3.12`, Codex CLI đã đăng nhập và Hermes Agent nếu cần chiều Codex → Hermes.

Trên máy vận hành, cài trực tiếp release wheel vào virtualenv riêng, không cần clone source:

```bash
python3.11 -m venv "$HOME/.local/share/codex-a2a-gateway/venv"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/python" -m pip install \
  "https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.2.1/codex_a2a_gateway-0.2.1-py3-none-any.whl"
"$HOME/.local/share/codex-a2a-gateway/venv/bin/codex-a2a-gateway" --version
```

Release wheel và quy trình cài sạch được kiểm tra trên macOS/Linux. Windows/WSL chưa được xác minh. Xem [hướng dẫn triển khai trên máy khác](docs/deployment.md) để cài Codex/Hermes, đăng ký MCP, migrate state, nâng cấp, rollback hoặc gỡ cài đặt.

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

Thêm peer vào `~/.hermes/config.yaml`:

```yaml
a2a_agents:
  codex:
    url: "http://127.0.0.1:9910"
    timeout: 300
```

Chỉ bật `hermes tools enable a2a --platform a2a` khi thật sự cần agent chaining, để tránh vòng lặp Hermes ↔ Codex không chủ ý.

Với A2A client generic, xem flow gửi task → lưu `taskId`/`contextId` → `GetTask`/tiếp tục/hủy ở [deployment guide](docs/deployment.md#5-generic-a2a-client-lifecycle). Mỗi `messageId` phải mới; chỉ dùng `message.taskId` khi task trả `INPUT_REQUIRED`.

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

Xem thêm [kiến trúc v0.2](docs/architecture-v0.2.md), [vận hành inbound](docs/inbound-gateway.md), [báo cáo kiểm thử v0.2](docs/testing-report-v0.2.md), [AGENTS.md](AGENTS.md), [SECURITY.md](SECURITY.md) và [CHANGELOG.md](CHANGELOG.md).
