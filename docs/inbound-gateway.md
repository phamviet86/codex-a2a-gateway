# Vận hành inbound A2A gateway

> Hợp đồng recovery/delivery mới: [job bền vững](durable-jobs.vi.md). Hết lượt wait không phải thất bại; không tự đưa kết quả vào Desktop conversation.

## Khởi động an toàn

```bash
export CODEX_WORKSPACE_ROOT=/absolute/path/to/workspace
export CODEX_A2A_GATEWAY_BACKEND=app-server
.venv/bin/codex-a2a-gateway gateway
```

Mặc định chỉ listen `127.0.0.1:9910`. Muốn bind non-loopback phải đặt `CODEX_A2A_BEARER_TOKEN`; validation sẽ fail startup nếu thiếu. Đặt `CODEX_A2A_PUBLIC_URL` khi có reverse proxy. TLS phải terminate ở proxy; không dùng plain HTTP qua mạng không tin cậy.

RPC chỉ nhận `Content-Type: application/json`, kiểm tra `Host`, và từ chối browser request có `Origin` khi gateway không bật token. Khi có token, `Origin` phải đúng same-origin; `Sec-Fetch-Site` nếu xuất hiện cũng phải là `same-origin`. Đây là lớp bảo vệ localhost khỏi DNS rebinding/cross-site request, không thay thế bearer token khi bind ra mạng.

Body được đọc theo chunk và dừng ngay khi vượt `CODEX_A2A_MAX_REQUEST_BYTES`, kể cả request không có `Content-Length`. Tên auth scheme `Bearer` không phân biệt hoa/thường theo HTTP; giá trị token vẫn được so sánh constant-time và phân biệt hoa/thường.

Chạy một gateway writer cho mỗi SQLite state file. Per-context lock là lock trong daemon; không dùng nhiều gateway process cùng ghi và xử lý chung một context.

Token không truyền qua CLI args/A2A payload và không được log. Dùng secret manager/service environment; không commit `.env`.

## Backend

`app-server` là mặc định. Nó tạo stdio connection JSON-RPC cho mỗi active turn, resume durable thread id và hỗ trợ delta, turn id, input-required/approval và best-effort interrupt.

Khi Codex yêu cầu thêm dữ liệu, task kết thúc lượt ở `TASK_STATE_INPUT_REQUIRED`. Gửi message tiếp theo với `message.taskId` bằng task cũ; có thể bỏ `contextId` vì gateway suy ra và kiểm tra context từ task. Lượt mới tiếp tục **cùng task ID**. `messageId` vẫn phải mới cho mỗi message; retry cùng message là idempotent.

Việc tăng turn, ghi `messageId`, tạo/cập nhật task và lifecycle event nằm trong một SQLite transaction. Nếu process dừng sau commit nhưng trước khi worker được schedule, retry đúng `messageId` sẽ tạo lại worker. Sau restart, task chưa từng vào backend được đánh dấu riêng và chỉ được requeue khi original message được replay, tránh tự động chạy lại side effect.

`cli` là compatibility mode:

```bash
CODEX_A2A_GATEWAY_BACKEND=cli .venv/bin/codex-a2a-gateway gateway
```

CLI đọc `codex exec --json` JSONL và dùng `codex exec resume <SESSION_ID>` cho follow-up. Nó không hỗ trợ approval/input-required tương tác và chỉ cancel bằng terminate subprocess. `CODEX_A2A_GATEWAY_CLI_FALLBACK=true` cho phép App Server chuyển sang CLI chỉ khi context mới chưa nhận thread id; context App Server đã tồn tại sẽ fail closed để không trộn session semantics.

## Kiểm tra

```bash
curl http://127.0.0.1:9910/health
curl http://127.0.0.1:9910/.well-known/agent-card.json
curl -X POST http://127.0.0.1:9910/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"Summarize this workspace."}]}}}'
```

Đối chiếu adapter với schema của đúng binary đang cài:

```bash
.venv/bin/python scripts/check_app_server_schema.py --codex-bin codex
```

Khi bật token, thêm `Authorization: Bearer ...` mà không ghi token vào shell history/log dùng chung.

Contract A2A v1:

- `SendMessage.result` là union wrapper `{"task": Task}`.
- `SendStreamingMessage` phát task trước, sau đó chỉ phát `statusUpdate`/`artifactUpdate`; không dùng trường `final` đã bỏ ở v1.
- Mỗi subscriber SSE có queue riêng; duplicate live stream đều nhận cùng update thay vì chia nhau consume. Replay task đã kết thúc trả snapshot terminal rồi đóng ngay.
- `ListTasks` hỗ trợ `contextId`, `status`, `pageSize`, `pageToken`, `historyLength`, `statusTimestampAfter` và `includeArtifacts`; thứ tự ổn định theo thời điểm update mới nhất.
- Alias pre-1.0 vẫn được giữ để tương thích, nhưng trả legacy task snapshot.

## Giới hạn

- Text parts only, tối đa 32 parts và `CODEX_A2A_MAX_REQUEST_BYTES` mỗi body.
- Mọi part phải có đúng một content member là `text` và `mediaType` phải là `text/plain` hoặc bỏ trống. Nếu request trộn thêm data/raw/url/file, toàn request bị từ chối trước khi gọi Codex.
- Turn budget/context dùng `CODEX_A2A_GATEWAY_MAX_TURNS` để chặn ping-pong loop.
- `CODEX_A2A_GATEWAY_MAX_CONCURRENCY` giới hạn số turn Codex chạy đồng thời; stream queue được coalescing và có giới hạn.
- `CODEX_A2A_MAX_PENDING_TASKS` giới hạn tổng task inbound queued + running; effective cap không thấp hơn concurrency. Khi đầy, request mới nhận `SERVER_OVERLOADED` retryable và không tạo context/task.
- Push notification CRUD/webhook chưa triển khai.
- Cancel không chứng minh computation dừng; response metadata luôn `computationStopped: unknown`.
- Result/artifact được lưu SQLite và có thể nhạy cảm; áp dụng retention/backup phù hợp.

Nguồn protocol: [OpenAI Codex App Server](https://learn.chatgpt.com/docs/app-server), [OpenAI non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), [Hermes A2A guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/a2a.md).
