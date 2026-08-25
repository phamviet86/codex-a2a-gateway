# Báo cáo kiểm thử v0.2

Ngày kiểm thử: 2026-08-26 (Asia/Ho_Chi_Minh)  
Bridge: `0.2.0`; Hermes base: `0.20.5`

## Phạm vi

V0.2 thêm durable TaskStore/messageId dedup phía Hermes và capability-gated safe retry phía bridge. Báo cáo phân biệt automated/fake tests, Hermes local restart test và giới hạn không thể biến thành exactly-once end-to-end.

## Automated evidence

Chạy trong hai virtual environment tách biệt ngày 2026-08-26:

| Suite | Kết quả | Bao phủ chính |
|---|---:|---|
| Bridge `pytest -q` | 14 passed; coverage 67.95% (gate 65%) | 7 tools, persistence, recovery cũ, capability-gated retry |
| Bridge Ruff + format | pass | source và tests |
| Bridge mypy | pass | toàn bộ `src/` |
| Bridge compile + `pip check` | pass | import/bytecode và dependency consistency |
| Build sdist/wheel + Twine | pass | `0.2.0`, không có state/log/secret local trong artifact |
| Hermes A2A unit | 155 passed; 19 deselected | TaskStore, restart, dedup, scope và Agent Card |
| Hermes A2A integration | 12 passed; 106 deselected | HTTP JSON-RPC/SSE adapter |
| Hermes Ruff + compile | pass | các file A2A và regression tests đã sửa |

Regression transport dùng fake server chủ động nhận và hoàn tất một task, đóng socket trước SSE event đầu, rồi nhận retry cùng `messageId`. Một regression riêng mô phỏng server đã durable-accept trước khi client hết absolute timeout. Cả hai trường hợp đều retry đúng một lần, chỉ tạo một task và một agent turn. Test upstream không quảng bá extension tiếp tục xác nhận bridge không resend mutating request.

## Live local evidence

Hermes 0.20.5 trên localhost được chạy từ nhánh patch local `feature/a2a-durable-task-store`:

- health trả `ok`; Agent Card quảng bá durable extension cùng `messageIdDeduplication=true`;
- SQLite TaskStore được tạo mode `0600`;
- một prompt marker vô hại hoàn tất; gửi lại nguyên request/messageId trả cùng task ID;
- restart gateway, sau đó `GetTask` trả cùng task, trạng thái completed và đúng marker đã persist;
- MCP client stdio mới initialize protocol `2025-11-25`, thấy đúng 7 tools, gọi `hermes_status` thấy durable capability và gọi `hermes_chat` nhận đúng marker qua Hermes;
- sau kiểm thử gateway health vẫn `ok`, không có tiến trình test riêng bị bỏ lại.

Live test không cố tình cắt kết nối thật giữa bridge và Hermes vì cần proxy/fault injector trên đường local; crash window đó đã được kiểm deterministic bằng fake A2A server. Live test tập trung xác minh capability negotiation, dedup thực và recovery qua gateway restart.

## Deployment và rollback

- Bridge venv riêng đã được nâng editable từ `0.1.1` lên `0.2.0`; Codex MCP entry hiện hữu vẫn trỏ đúng entrypoint này.
- Gateway dùng service definition user-level có sẵn; không sửa Hermes/Codex config và không dùng `sudo`.
- Hermes patch là một commit local riêng trên base 0.20.5; chưa push/fork/open upstream PR.
- Rollback bridge: checkout release/commit trước, reinstall editable, rồi restart Codex client. Rollback Hermes: checkout base commit trước patch và restart gateway; database `~/.hermes/a2a_tasks.sqlite3` có thể giữ lại để quay lại patch mà không ảnh hưởng upstream cũ.

## Guarantee còn lại

Durable correlation và scoped message dedup đạt at-most-once dispatch cho retry cùng `messageId`. Không claim exactly-once completion: gateway có thể crash sau khi tool side effect xảy ra nhưng trước khi terminal result được commit.
