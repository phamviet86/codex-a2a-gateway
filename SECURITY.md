# Security Policy

## Supported versions

| Version | Security fixes |
|---|---|
| 0.6.0b1 | Bản prerelease hiện tại; báo lỗi kèm đúng phiên bản |
| 0.5.1 | Bản local ổn định gần nhất trong giai đoạn chuyển đổi |
| < 0.5.1 | Nâng cấp trước khi đối chiếu lỗi đã sửa |

## Báo cáo lỗ hổng

Không đăng token, prompt riêng tư, transcript hoặc chi tiết khai thác nhạy cảm trong issue công khai. Hãy dùng GitHub **Report a vulnerability** của repository; private vulnerability reporting đã được bật cho project này.

## Security boundary

Outbound vẫn chỉ chấp nhận Hermes endpoint loopback, không follow redirect và không nhận bearer token qua tool arguments. Inbound gateway mặc định bind `127.0.0.1`; bind non-loopback bị từ chối nếu không có `CODEX_A2A_BEARER_TOKEN`. RPC bắt buộc JSON, kiểm tra Host/Origin/Sec-Fetch-Site để giảm DNS-rebinding/cross-site localhost abuse, giới hạn body khi đọc từng chunk và chặn admission trước khi tạo task. Token chỉ đọc từ env, được compare constant-time và không ghi log.

Adapter local cũ không persist prompt gốc nhưng lưu fingerprint, mapping, trạng thái, kết quả/artifact và lỗi tối thiểu trong SQLite local; các nội dung kết quả có thể nhạy cảm. Codex và Hermes cũng có thể ghi session/conversation riêng. Người vận hành chịu trách nhiệm về quyền file, retention và backup.

Client/server v0.6 dùng PostgreSQL riêng trên server và SQLite riêng trên client.
Prompt chờ dispatch được mã hóa bằng khóa môi trường và xóa theo TTL; đây là thay
đổi có chủ đích so với adapter local cũ. HTTPS phải được xác minh qua CA, mỗi thiết
bị có token riêng, Hermes và PostgreSQL vẫn nằm trong boundary nội bộ. Unix socket
và state client chỉ dành cho tài khoản hệ điều hành sở hữu; đây không phải sandbox
chống tiến trình độc hại chạy cùng tài khoản. MCP metadata xác định task gốc nhưng
không thay thế xác thực thiết bị trên server.

Native queue không đảm bảo idempotency. ACK không rõ phải giữ
`delivery_outcome_unknown`; không tự gửi lại để che mất trạng thái mơ hồ. Chỉ bản
Codex/schema đã xác minh mới bật adapter. File upload có giới hạn kích thước,
digest, phạm vi thiết bị và TTL; bản beta chưa quét virus. Chỉ UTF-8 text/plain được
dùng làm reference text cho Hermes, không thực thi nội dung upload.

Đây là project độc lập, không phải sản phẩm chính thức hay được hỗ trợ bởi Nous Research/Hermes Agent hoặc OpenAI/Codex.
