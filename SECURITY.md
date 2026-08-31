# Security Policy

## Supported versions

| Version | Security fixes |
|---|---|
| 0.2.x | Có |
| 0.1.x | Có |
| < 0.1.0 | Không |

## Báo cáo lỗ hổng

Không đăng token, prompt riêng tư, transcript hoặc chi tiết khai thác nhạy cảm trong issue công khai. Hãy dùng GitHub **Report a vulnerability** (private security advisory) của repository. Maintainer nên bật tính năng này trước khi public repository.

Nếu private reporting chưa được bật, liên hệ riêng repository owner qua kênh được ghi trên GitHub profile và chỉ cung cấp thông tin tối thiểu cho tới khi có kênh mã hóa phù hợp.

## Security boundary

Outbound vẫn chỉ chấp nhận Hermes endpoint loopback, không follow redirect và không nhận bearer token qua tool arguments. Inbound gateway mặc định bind `127.0.0.1`; bind non-loopback bị từ chối nếu không có `CODEX_A2A_BEARER_TOKEN`. RPC bắt buộc JSON, kiểm tra Host/Origin/Sec-Fetch-Site để giảm DNS-rebinding/cross-site localhost abuse, giới hạn body khi đọc từng chunk và chặn admission trước khi tạo task. Token chỉ đọc từ env, được compare constant-time và không ghi log.

Bridge không persist prompt gốc nhưng lưu fingerprint, mapping, trạng thái, kết quả/artifact và lỗi tối thiểu trong SQLite local; các nội dung kết quả có thể nhạy cảm. Codex và Hermes cũng có thể ghi session/conversation riêng. Người vận hành chịu trách nhiệm về quyền file, retention và backup.

Đây là project độc lập, không phải sản phẩm chính thức hay được hỗ trợ bởi Nous Research/Hermes Agent hoặc OpenAI/Codex.
