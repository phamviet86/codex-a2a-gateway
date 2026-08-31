# Kiến trúc bidirectional v0.2

## Boundary

V0.2 bổ sung inbound mà không rewrite outbound v0.1.1:

```text
MCP stdio -> server.py -> BridgeService/core.py -> A2AClient -> configured peer (Hermes default)

A2A HTTP/SSE -> gateway.py -> InboundService -> CodexBackend
                                      |            |- AppServerBackend (default, stdio JSONL)
                                      |            `- CLIBackend (explicit fallback, JSONL)
                                      `-> Store/SQLite
```

Transport chỉ parse/format A2A. `InboundService` sở hữu lifecycle, lock và persistence. `CodexBackend` cô lập protocol Codex. Store dùng migration additive để bảo toàn table/record v0.1.1.

## Durable identity

- `contexts.context_id` là A2A `contextId`; `contexts.codex_thread_id` là Codex thread/session id.
- `tasks.a2a_task_id` là A2A `taskId`; `tasks.codex_turn_id` là App Server turn id khi backend có khái niệm này.
- `inbound_messages.message_id` chống submit lại cho mọi lượt, kể cả continuation trên cùng task. Cùng message id nhưng khác fingerprint/context/task bị từ chối.
- Admission, turn increment, task/message/event mutation được serialize và commit transactionally. Worker chỉ được schedule sau commit; idempotent replay sửa được khoảng trống post-commit/pre-schedule.
- Prompt gốc không persist. Fingerprint, route, state, kết quả, lỗi tối thiểu và lifecycle event được persist.

Sau restart, task đã vào backend được kết thúc trung thực bằng `gateway_restarted`; context/thread mapping vẫn giữ. Task còn `queued` được đánh dấu `gateway_restarted_before_start`, rồi chỉ requeue khi client replay đúng original `messageId`, vì prompt không được persist.

## App Server protocol

Adapter dùng schema sinh từ binary hiện hành bằng:

```bash
codex app-server generate-json-schema --out /tmp/codex-app-server-schema
```

Handshake: `initialize` -> `initialized`; session: `thread/start` hoặc `thread/resume`; turn: `turn/start`; output lấy từ `item/agentMessage/delta`, `item/completed`, `turn/completed`; cancel dùng `turn/interrupt`.

Server-initiated approval/request-user-input không bị auto-approve. Gateway trả quyết định cancel/empty answer để App Server không treo, rồi biểu diễn A2A `TASK_STATE_INPUT_REQUIRED`. Không ghi command/prompt approval đầy đủ vào event log.

## Concurrency và recovery

Mỗi `contextId` có một asyncio lock trong daemon, nên hai lượt cùng context không khởi tạo đồng thời. Global admission cap giới hạn tổng queued + running trước khi tạo context/task; semaphore nhỏ hơn giới hạn số backend thực sự chạy. Mỗi stream subscriber có bounded queue riêng. Các context khác có thể chạy song song. Daemon độc lập với MCP stdio; process manager bên ngoài chịu trách nhiệm restart daemon.

SQLite WAL, schema version 3 và additive columns cho phép restart/recovery. In-flight subprocess không thể sống qua process restart; gateway không giả vờ resume đúng turn đó, chỉ giữ thread để lượt mới tiếp tục.

## Capability boundary

Implemented: Agent Card, send, stream, get, list, cancel. Not implemented: push notification config/webhooks, authenticated extended card và non-text input. Agent Card chỉ quảng cáo streaming; `pushNotifications` luôn false.

Thiết kế tham khảo ý tưởng adapter/domain/transport và dual-role từ Apache-2.0 project [codex-a2a](https://github.com/liujuanjuan1984/codex-a2a). Không copy source code từ project đó.
