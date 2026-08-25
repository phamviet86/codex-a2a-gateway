# Hermes durable A2A task extension v1

Extension URI: `urn:hermes-agent:a2a:extension:durable-task-store:v1`

## Mục tiêu

Extension này cho phép A2A client phục hồi correlation sau transport failure mà không dispatch trùng agent. Nó dùng hai primitive mà A2A v1.0 cho phép nhưng không bắt buộc server triển khai:

1. terminal Task/result được persist và vẫn query được bằng `GetTask`/`ListTasks` sau gateway restart;
2. `SendMessage`/`SendStreamingMessage` với cùng `messageId` trong cùng security/routing scope trả lại Task gốc.

Hermes quảng bá capability trong `AgentCard.capabilities.extensions[]`:

```json
{
  "uri": "urn:hermes-agent:a2a:extension:durable-task-store:v1",
  "required": false,
  "params": {
    "terminalTaskPersistence": true,
    "messageIdDeduplication": true,
    "nonterminalRestartState": "TASK_STATE_FAILED"
  }
}
```

## Contract

- Scope dedup là `(authenticated peer, served-agent slug, tenant, messageId)`; peer hoặc tenant khác không nhìn thấy task của nhau.
- Record được commit SQLite **trước** khi Hermes dispatch `MessageEvent` hoặc forwarded profile command.
- Retry cùng scope/messageId không tăng turn counter, không ghi lại conversation và không gọi agent lần hai.
- Nếu payload mới reuse một messageId cũ, Hermes không dispatch payload mới và trả Task gốc; server ghi warning vận hành.
- Terminal state và result text được giữ tối đa 500 task như retention cũ.
- Watcher/SSE subscription đang chạy vẫn là process-local. Sau restart, terminal task resolve ngay; task từng nonterminal được đổi thành `TASK_STATE_FAILED` với lý do gateway restart.

## Persistence và privacy

Mặc định Hermes dùng `~/.hermes/a2a_tasks.sqlite3`; có thể override bằng `A2A_TASK_STORE_PATH`. Thư mục và database lần lượt dùng mode `0700` và `0600` khi filesystem hỗ trợ.

Database lưu task/context/message IDs, routing scope, request fingerprint, state, result và timestamps. Nó không lưu raw prompt hoặc push callback URL. Result có thể chứa dữ liệu nhạy cảm, vì vậy operator phải quản lý retention và backup giống conversation/audit store.

## Guarantee chính xác

Extension cung cấp durable correlation và **at-most-once dispatch cho retry cùng messageId**, không tuyên bố exactly-once end-to-end:

| Crash window | Sau restart |
|---|---|
| Trước durable commit | Không có task; client có thể retry để tạo task mới. |
| Sau commit, trước dispatch | Task trở thành failed; retry trả đúng task đó và không dispatch. |
| Sau dispatch, trước durable completion | Task trở thành failed; side effect của computation có thể đã xảy ra. |
| Sau durable completion | `GetTask`/retry trả terminal result đã persist. |

Exactly-once completion cần durable execution queue và tool transaction/idempotency xuyên suốt Hermes cùng các hệ thống bên ngoài. TaskStore không thể tự cung cấp guarantee đó.

## Bridge behavior

Bridge v0.2.0 chỉ safe-retry một lần khi đồng thời thỏa mãn:

- Agent Card quảng bá extension và `messageIdDeduplication=true`;
- lỗi là transport ambiguity hoặc absolute stream timeout;
- bridge chưa nhận A2A task ID;
- retry dùng nguyên context và `messageId` đã persist local.

Nếu thiếu bất kỳ điều kiện nào, bridge giữ `outcome_unknown` và không resend.
