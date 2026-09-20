# Roadmap và tính khả thi

**Sự thật phiên bản:** release `v0.4.0` đã gồm plugin durable `codex_a2a`, recovery timeout hai chiều, tiếp tục `INPUT_REQUIRED` và extension model/reasoning cho chiều Hermes/A2A → Codex. Các mục roadmap bên dưới vẫn không phải cam kết release.

- **Có thể triển khai tiếp ở gateway:** preflight, template `systemd`/`launchd`, one-command two-way verify và reconciliation tùy chọn.
- **Cần Hermes thay đổi:** async task lifecycle/recovery đối xứng ở tool native của Hermes.
- **Experimental/later:** chuẩn hóa model/reasoning liên peer và push notification.

## Có trong release v0.4.0

- Gateway foreground với MCP stdio (Codex → Hermes) và A2A HTTP/SSE (A2A → Codex).
- `doctor`, Agent Card, `/health`, lifecycle A2A (`GetTask`, `ListTasks`, `CancelTask`), SQLite state và recovery bảo thủ ở chiều Codex → Hermes.
- `configuration.returnImmediately=true` cho A2A client generic; client lưu `taskId`/`contextId` rồi poll. Xem [deployment guide](deployment.md#5-generic-a2a-client-lifecycle).
- Loopback mặc định, bearer token trước non-loopback bind, và concurrency theo từng process. Đây không phải deployment multi-tenant.
- Plugin Hermes `codex_a2a`: submit sớm, handle bền, poll/cancel, recovery không blind resend và tiếp tục `INPUT_REQUIRED` bằng cùng handle. Built-in `a2a_call` vẫn synchronous.
- Extension inbound opt-in cho preference model/reasoning: App Server receiver và sender cùng dùng URI/envelope **A2A-Extensions**; receiver quyết định model/effort thực tế. Nó chưa phải field chuẩn A2A liên peer.

## Khả thi bằng công việc tiếp theo ở gateway, nhưng chưa có

| Hạng mục đề xuất | Đánh giá hiện tại |
|---|---|
| Preflight cài đặt | Khả thi, nhưng chưa có command/script chính thức. Hiện dùng `doctor`, Agent Card và `/health` thủ công. |
| Template service `systemd`/`launchd` | Khả thi, nhưng chưa đóng gói/kiểm thử. Hiện chỉ hỗ trợ foreground. |
| One-command two-way verify | Khả thi nếu operator opt-in vì tạo hai live task. Chưa có verifier chính thức. |
| Reconciliation `outcome_unknown` | Có recovery bảo thủ theo state hiện có; job/UI tự động, retry policy và audit report vẫn chưa có. Không tự resend side effect. |

## Cần Hermes upstream hoặc thay đổi phía peer

| Hạng mục đề xuất | Lý do |
|---|---|
| Hermes native async task tool đầy đủ | `a2a_call` native hiện là call HTTP đồng bộ. Để đối xứng lifecycle cần `returnImmediately`, giữ handle, poll, tiếp tục và cancel cùng task; gateway không tự thay đổi native tool đó. |
| Recovery native sau timeout | Khi native client timeout trước task ID, nó không có handle để refresh request cũ. Plugin `codex_a2a` v0.4.0 giảm vấn đề này cho đường riêng của nó, nhưng không sửa native behavior/upstream. |
| Model/reasoning với peer khác | Peer phải cùng hỗ trợ URI/envelope extension; A2A core không chuẩn hóa field này. Receiver luôn có quyền policy/capability cuối cùng. |

## Later / experimental

- Push notification/webhook: Agent Card hiện `pushNotifications: false`; CRUD/delivery chưa có.
- Preflight/service/two-way verify đều khả thi tiếp theo nhưng phải được kiểm thử macOS/Linux trước khi phát hành.
- Relay A2A qua Hermes sang peer khác cần trusted peers, rate limit và anti-loop policy; không cần cho topology hai chiều cơ bản.

Để vận hành release, theo [hướng dẫn triển khai](deployment.md) và [hướng dẫn thiết lập Codex + Hermes](setup-codex-hermes.vi.md), không theo roadmap này.
