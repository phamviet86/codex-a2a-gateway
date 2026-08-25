# Changelog

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
