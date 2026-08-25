# Báo cáo kiểm thử v0.1

Ngày kiểm thử: 2026-08-25 (Asia/Ho_Chi_Minh)  
Bridge: `0.1.1`, Python `3.11.16`, MCP SDK `2.1.0`  
Hermes local: `0.20.5`, source commit `d736f5d53f1d33fabad5a17cb070eb138b618fb8`

## Regression 0.1.1 — late response recovery

Một ca live trước fix cho thấy bridge `ReadTimeout` trong khi Hermes hoàn tất sau đó. Hermes pending-future correlation đã hoạt động và `ListTasks(contextId)` trả Task completed; lỗi nằm ở bridge dùng `SendMessage`: task ID chỉ xuất hiện trong HTTP response cuối, nên timeout làm mất correlation ID. Báo cáo public không giữ task/context ID, timestamp chi tiết, prompt hay output của máy kiểm thử.

Sau fix, `hermes_task_get` recover read-only task cũ qua `ListTasks(contextId)`, cập nhật A2A ID, state `completed` và kết quả đầy đủ; không có send mới. Nếu Hermes đã restart và TaskStore mất, bridge có fallback read-only tới conversation JSONL mà chính source Hermes persist.

Live Codex process mới gọi `hermes_chat(mode=sync, timeout=1)` đúng một lần. Bridge nhận `submitted`/`working` và A2A ID trước initial timeout, Codex gọi `hermes_task_wait`, rồi nhận đúng marker vô hại sau khi completed. SQLite ghi đúng một bridge/A2A task; không có duplicate send.

## Kết luận

V0.1 đạt luồng chính Codex → MCP stdio → bridge → Hermes A2A → bridge → Codex, bao gồm cùng-context multi-turn. Tất cả automated tests pass. Hermes A2A đang chạy loopback và Codex MCP entry đã được process Codex mới nạp thành công.

Giới hạn deploy duy nhất: `launchctl bootstrap` trên máy trả exit 5. Hermes CLI tự chạy detached fallback process, nên gateway hoạt động nhưng không có auto-start/auto-restart của launchd.

## Public-readiness verification

Lượt rà soát repository ngày 2026-08-25 xác minh:

- Ruff lint và format check: pass trên 27 Python files.
- Mypy: pass trên 9 source files; `compileall` và `pip check`: pass.
- Pytest: 12 pass; branch-aware coverage 67,51%, vượt gate 65%.
- MCP stdio integration vẫn xác nhận đúng bảy tool.
- Wheel và sdist build trong isolated environment, `twine check` pass; wheel có 14 members, sdist có 33 members.
- Distribution scan không thấy `.env`, SQLite, log, backup, cache, bytecode hay dữ liệu máy local. Wheel được cài `--no-deps` vào thư mục tạm và import đúng version `0.1.1`.
- `doctor` read-only xác nhận Hermes health/Agent Card ở loopback; không gửi task live mới trong lượt chuẩn bị public.

## Automated test matrix

Lệnh cuối:

```bash
.venv/bin/pytest --cov=codex_hermes_a2a_bridge --cov-report=term-missing
```

Kết quả public-readiness: **12 passed**, branch-aware coverage tổng xấp xỉ **68%** với gate **65%**. MCP handler chạy trong subprocess nên coverage process chính báo thấp hơn phần hành vi thực tế đã kiểm.

| Nhóm | Phạm vi | Kết quả |
|---|---|---|
| Schema/settings | loopback-only URL, defaults và validation | Pass |
| Persistence | migration, mapping uniqueness, close, turn budget, lookup bridge/A2A ID | Pass |
| State machine | working→completed, terminal overwrite protection, input-required | Pass |
| Idempotency | same key+fingerprint dedup; different payload conflict | Pass |
| Ambiguous send | mutating call chỉ gửi một lần; local state `outcome_unknown` | Pass |
| Late result | sync trả working sau initial timeout; cùng SSE worker nhận completed | Pass |
| Context recovery | A2A `ListTasks(contextId)` gắn đúng unmatched task, không resend | Pass |
| Restart fallback | official conversation JSONL thu hồi kết quả khi TaskStore rỗng | Pass |
| Ambiguous recovery | nhiều unresolved local tasks thì từ chối correlate | Pass |
| Cancel | active fake task nhận cancel; response giữ `cancel_requested` và cảnh báo computation | Pass |
| Agent Card | canonical path; legacy chỉ fallback khi canonical 404 | Pass |
| Fake A2A | health/card, SendMessage, SendStreamingMessage SSE, Get/List/Subscribe/Cancel | Pass |
| Multi-turn/input-required | cùng context qua bốn lượt, gồm hỏi thêm input và continuation | Pass |
| MCP stdio | initialize, server instructions, exact `tools/list`, gọi cả bảy tools | Pass |

Fake integration dùng HTTP server thật trên ephemeral loopback port; không dùng mock cho đường MCP stdio.

## Live Hermes 0.20.5

| Kiểm tra | Bằng chứng | Kết quả |
|---|---|---|
| Health | `/health` trả `status=ok`, served agent slug/profile `default` | Pass |
| Agent Card | canonical card, JSONRPC 1.0, streaming và push advertised | Pass |
| Lượt 1 | conversation test trả đúng marker vô hại | Pass |
| Lượt 2 | cùng context nhớ và trả lại marker | Pass |
| Status/get/list/wait/contexts | `scripts/live_check.py <conversation-key>` trả health ok, get/wait completed và context nhất quán | Pass |
| Cancel live | gọi trên task terminal trả `cancel_sent=false`, không tuyên bố computation dừng | Pass |

Active cancel được kiểm đầy đủ với fake server. Live test cố ý dùng terminal no-op để không tạo computation dài/mồ côi; đây phù hợp giới hạn Hermes 0.20.5 là `CancelTask` không abort turn đang chạy.

Hermes `ListTasks`/`SubscribeToTask` wire operations được kiểm qua fake A2A. V0.1 tool `hermes_tasks_list` ưu tiên SQLite durable thay vì phụ thuộc TaskStore in-memory của Hermes; `hermes_task_wait` dùng active stream/subscribe rồi polling fallback.

## MCP stdio và Codex client mới

Test protocol riêng khởi động MCP server subprocess và thực hiện:

1. `initialize` và kiểm tra instructions bắt đầu bằng workflow `hermes_chat`/context/input-required;
2. `tools/list`, xác nhận đúng bảy tên;
3. `tools/call` cho cả bảy tools qua fake A2A.

Sau khi thêm entry bằng `codex mcp add`, một process `codex exec --ephemeral` mới đã nạp server, gọi `hermes_chat` và nhận đúng marker vô hại. Operational log và marker cụ thể không được giữ trong repository public.

Đây là bằng chứng end-to-end Codex mới → bridge → Hermes → bridge → Codex, không dựa vào hot-load của task điều phối.

Codex CLI phát warning `Transport closed` trong shutdown sau khi tool và final response đã hoàn tất; process exit code vẫn 0 và marker đúng. Không quan sát stdout protocol corruption.

## Deploy/config evidence

- Trước thay đổi đã tạo scoped backups ngoài repository cho đúng các file cấu hình liên quan.
- `a2a-platform` enabled, không cấp tool override.
- `gateway.platforms.a2a.enabled=true`; không token nên source Hermes ép bind `127.0.0.1`.
- Foreground gateway pass trước khi thử user-level install.
- `codex mcp get codex-hermes-a2a-bridge` báo enabled, stdio command trỏ đúng `.venv` riêng.
- SQLite nằm ngoài repo và được chmod `0600`; thư mục state `0700`.
- Không sửa source Hermes, không dùng `sudo`, không commit/push và không ghi secret vào repo/report.

## Giới hạn còn lại

- Gateway hiện là detached fallback process; không tự phục hồi sau logout/crash do launchd exit 5.
- Hermes TaskStore mất khi restart; bridge giữ local record/result nhưng remote refresh có thể không tìm thấy task.
- Streaming của Hermes là lifecycle SSE, không token streaming.
- Profile v0.1 cố định `default`; named profile/tenant không model-selectable.
- Push notification CRUD có trong A2A reference nhưng không nằm trong bảy tool v0.1.
- Idempotency chỉ local bridge; không có guarantee dedup từ Hermes.
- Conversation JSONL không lưu A2A state; disk fallback có warning và không thể phân biệt chính xác `input_required` với `completed`.
- Nếu toàn bộ MCP process chết khi SSE còn mở, Hermes 0.20.5 có thể đánh dấu client-disconnected và bỏ late reply khỏi A2A persistence; bridge không thể bảo đảm recovery hoàn toàn nếu không sửa Hermes thành durable task store/push delivery.
- Correlation worker mặc định 300 giây, bằng Hermes reply timeout mặc định; task dài hơn ngưỡng này vẫn có thể cần operator recovery.

## Rollback

Chạy `scripts/rollback.sh` để xem trước; `scripts/rollback.sh --apply` mới thực hiện. Rollback gỡ đúng MCP entry và cấu hình/plugin A2A nhưng giữ gateway service. Chỉ dùng thêm `--include-gateway-service` khi gateway được cài riêng cho bridge; source, `.venv`, SQLite và Hermes transcripts được giữ.
