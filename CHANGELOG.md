# Changelog

## Unreleased

- Đồng bộ metadata runtime với version package thay vì hard-code `0.2.0`, gồm MCP server, Agent Card,
  `doctor`, Codex App Server client metadata và outbound User-Agent.
- Sửa quickstart release để luôn dùng executable đã cài, thêm checksum verification, lifecycle A2A
  generic có submit/poll/continue/cancel và bảng troubleshooting tập trung.

## 0.2.1 — 2026-09-01

- Thêm hướng dẫn triển khai từ GitHub Release trên máy sạch, gồm prerequisite, đăng ký MCP, cấu hình Hermes, nâng cấp, rollback và gỡ cài đặt không xóa state.
- Mở rộng CI sang macOS và Linux với CPython 3.11; mỗi job tự build rồi cài wheel vào virtualenv mới và kiểm tra cả hai executable.
- Thêm `--version` cho CLI và kiểm tra tự động rằng metadata package, import và executable hoạt động sau khi cài wheel.
- Ghi rõ Windows chưa được xác minh và deployment qua container chưa phải topology được hỗ trợ cho tích hợp local có Codex/Hermes đăng nhập.

## 0.2.0 — 2026-09-01

- Đổi tên và định vị project thành `codex-a2a-gateway`: inbound A2A generic cho Codex, outbound MCP adapter đã kiểm thử với Hermes.
- Đổi distribution/executable/namespace thành `codex-a2a-gateway` / `codex-a2a-gateway` / `codex_a2a_gateway`; giữ alias executable, env và state-path cũ trong v0.2 để migration an toàn.
- Thêm README tiếng Anh làm bản chuẩn, README tiếng Việt, quickstart hai chiều, `AGENTS.md` và báo cáo kiểm thử live v0.2.
- Xác minh live cả Codex → Hermes và Hermes native `a2a_call` → Codex App Server trên Hermes 0.20.6; công bố v0.2.0 ở mức public beta.
- Giữ nguyên MCP façade outbound tới Hermes và thêm A2A v1.0 HTTP gateway inbound generic tới Codex.
- Thêm Agent Card, `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask` và alias pre-1.0 tương ứng; không quảng cáo push notification.
- Chuẩn hóa response/event theo A2A v1, bearer security objects, task continuation qua `message.taskId`, ListTasks filter/cursor và TaskNotFound.
- Siết ingress localhost bằng JSON Content-Type cùng Host/Origin/Sec-Fetch-Site checks; giới hạn concurrency/stream queue và đóng cancellation race.
- Recovery inbound xử lý toàn bộ active task; App Server dùng đúng `item/tool/requestUserInput` và schema probe kiểm tra method names.
- Broadcast stream theo từng subscriber, reject mixed/non-text Part, chunked early body limit và bearer scheme case-insensitive.
- Thêm total inbound admission cap và transaction atomic cho message/task/turn; replay phục hồi an toàn khoảng trống commit-to-worker kể cả qua restart.
- Dùng Codex App Server JSON-RPC/JSONL qua stdio làm backend mặc định; WebSocket không được dùng ở production path.
- Thêm backend tương thích `codex exec --json`, chỉ bật bằng cấu hình rõ; không quảng cáo `input-required` khi fallback có thể hoạt động.
- Migrate SQLite để giữ `contextId ↔ threadId`, `taskId ↔ turnId`, direction/backend và idempotency theo `messageId` qua restart.
- Thêm per-context lock, restart reconciliation, best-effort interrupt/cancel, loopback default, bearer auth tùy chọn và request/message limits.
- Thêm daemon `gateway`, tài liệu kiến trúc/vận hành và regression tests cho protocol adapter, fallback, persistence và lifecycle inbound.

## 0.1.1 — 2026-08-25

- Chuẩn bị public repository: Apache-2.0, metadata/build exclusions, CI, lint/type/coverage, contribution và security policy, issue/PR templates, sample env và kiểm tra distribution artifacts.
- Dùng các major release GitHub Actions hiện hành chạy trên Node.js 24 để CI không phụ thuộc runtime Node.js 20 đã deprecated.
- Loại đường dẫn, task/context ID và operational evidence riêng của máy khỏi tài liệu; làm live-check read-only và rollback không đụng gateway service mặc định.
- Sửa mất kết quả khi Hermes hoàn tất sau HTTP `ReadTimeout`: `sync` chuyển sang lifecycle SSE và giữ correlation worker lâu hơn initial wait.
- Thêm recovery `outcome_unknown` bằng A2A `ListTasks(contextId)` mà không resend.
- Thêm fallback read-only tới `~/.hermes/a2a_conversations/<context>.jsonl` khi TaskStore in-memory đã mất.
- Chỉ correlate khi đúng một unresolved local task và một unmatched candidate; từ chối trường hợp mơ hồ.
- Xác minh read-only rằng một task timeout thực tế có thể được thu hồi đầy đủ mà không gửi lại.
- Thêm regression tests delayed completion, no-resend, A2A-list recovery, disk fallback và ambiguous refusal.

## 0.1.0 — 2026-08-25

- Thêm MCP stdio server với đúng bảy tool hội thoại/task cho Hermes.
- Thêm A2A v1.0 JSON-RPC/SSE client, canonical Agent Card và legacy 404 fallback.
- Thêm SQLite mapping Codex conversation ↔ Hermes context/task, idempotency local và state validation.
- Thêm loopback-only policy, env-only token, timeout/concurrency/turn budget và cancel semantics trung thực.
- Thêm CLI `serve`, `doctor`, `smoke`, test unit/fake/MCP stdio/live và rollback có guard.
- Thêm tài liệu triển khai, vận hành và báo cáo kiểm thử v0.1.
