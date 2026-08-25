# Security Policy

## Supported versions

| Version | Security fixes |
|---|---|
| 0.1.x | Có |
| < 0.1.0 | Không |

## Báo cáo lỗ hổng

Không đăng token, prompt riêng tư, transcript hoặc chi tiết khai thác nhạy cảm trong issue công khai. Hãy dùng GitHub **Report a vulnerability** (private security advisory) của repository. Maintainer nên bật tính năng này trước khi public repository.

Nếu private reporting chưa được bật, liên hệ riêng repository owner qua kênh được ghi trên GitHub profile và chỉ cung cấp thông tin tối thiểu cho tới khi có kênh mã hóa phù hợp.

## Security boundary

V0.1.1 chỉ chấp nhận Hermes endpoint loopback, không follow redirect và không nhận bearer token qua tool arguments. Tuy nhiên bridge lưu kết quả/artifact và một phần lỗi trong SQLite local; các nội dung đó có thể nhạy cảm. Hermes cũng có thể ghi conversation/audit log riêng. Người vận hành chịu trách nhiệm về quyền file, retention và backup.

Đây là project độc lập, không phải sản phẩm chính thức hay được hỗ trợ bởi Nous Research/Hermes Agent hoặc OpenAI/Codex.
