# Hermes A2A Gateway

[English](README.md) | **Tiếng Việt**

**Bản phát hành v0.8.0rc1** kết nối Codex Desktop với Hermes trên server riêng,
qua MCP, daemon/inbox local và broker PostgreSQL. Client hỗ trợ HTTPS trực tiếp
qua LAN/VPN hoặc HTTPS bên trong SSH tunnel do daemon quản lý.

Tên package, lệnh và đăng ký MCP thống nhất **`hermes-a2a-gateway`**. Namespace
Python là `hermes_a2a_gateway`, biến môi trường dùng `HERMES_A2A_GATEWAY_*`.
Bản này đã bỏ các chế độ gateway local cũ, alias executable, setup skill cũ và
plugin Hermes gọi Codex. Dữ liệu v0.6 được giữ khi di trú; không tự xóa ledger/inbox.

```text
Codex Desktop → MCP → client daemon + SQLite
                         ↓ HTTPS trực tiếp hoặc qua SSH tunnel
                    TLS proxy → broker PostgreSQL → Hermes A2A loopback
```

## Cài đặt

| Nơi thực hiện | Hướng dẫn |
| --- | --- |
| VPS/server chạy Hermes | [Cài và sử dụng server](docs/server-hermes.vi.md) |
| Máy cá nhân chạy Codex | [Cài và sử dụng client](docs/client-codex.vi.md) |
| Chọn kết nối trực tiếp/SSH | [SSH tunnel và phục hồi mất mạng](docs/ssh-tunnel.vi.md) |
| Máy đang có bản cũ | [Di trú, gỡ cài đặt cũ và rollback](docs/migration-v0.7.md) |

Hai máy cài cùng release wheel từ GitHub, có kiểm tra SHA256, không cần clone repo.
Đọc [lệnh tải và cài release](docs/deployment.md#install-the-same-release-on-both-machines).
Server cấp HTTPS origin, token riêng cho thiết bị và CA nếu cần; khóa mã hóa broker
không chuyển sang client.

Chỉ có bốn lệnh: `broker`, `client`, `client-mcp`, `client-doctor`. Tunnel thuộc
vòng đời daemon; nhiều task Desktop dùng chung daemon, không mở SSH riêng mỗi lượt.

## Sử dụng trong Codex

Sau khi cấu hình daemon và đăng ký MCP theo hướng dẫn client, Codex có năm tool:

| Tool | Công dụng |
| --- | --- |
| `gateway_submit` | Giao một yêu cầu mới, nhận `operation_id`; chờ tường minh 0–60 giây |
| `gateway_get` | Lấy trạng thái/kết quả theo handle trong task gốc |
| `gateway_wait` | Chờ tối đa 60 giây trên yêu cầu cũ |
| `gateway_cancel` | Yêu cầu hủy best-effort |
| `gateway_upload_artifact` | Upload file local được người dùng chọn |

Ví dụ yêu cầu: “Dùng gateway_submit giao Hermes xử lý việc này, wait_seconds: 0.
Giữ operation_id và dùng gateway_get/gateway_wait nếu chưa hoàn thành; không gửi lại.”

Lỗi `invalid_client_request` do `wait_seconds: 20` vượt mặc định 15 đã được sửa:
15 giây là thời gian chờ mặc định; giá trị tường minh 0–60 hợp lệ. Đầu vào sai
được báo lỗi cụ thể trước khi tạo operation. File đính kèm đầu vào hiện chỉ hỗ trợ
UTF-8 `text/plain`; repo trên máy không tự đồng bộ lên VPS.

## Mất mạng và giới hạn

Daemon tự khôi phục kết nối SSH với thời gian chờ tăng dần, tiếp tục SSE từ cursor
đã lưu và tra cứu công việc cũ. Không tự chạy lại tác vụ Hermes hoặc gửi lại native
queue khi chưa rõ kết quả lần trước. `outcome_unknown` phải được giữ nếu thiếu bằng chứng.

Kết quả muộn có thể được đưa vào task Desktop gốc dưới dạng tham chiếu; queue ACK
không chứng minh model đã đọc kết quả. Không cam kết wake khi Desktop offline/task
chưa được load. `gateway_get`/`gateway_wait` là cách lấy kết quả chủ động.

Bản này dành cho một chủ sở hữu, một broker và mỗi inbox chỉ một daemon. Hủy là
best-effort; vòng hỏi/đáp human input qua broker chưa được hỗ trợ. Payload/file mặc
định giữ một ngày, kết quả/event bảy ngày. Sao lưu cả dữ liệu và đúng khóa mã hóa.

Cài wheel được kiểm tra trên macOS/Linux với Python 3.11; Windows chưa hỗ trợ.
Khả năng native Desktop và sleep/wake phải có bằng chứng riêng, không suy ra từ
việc cài package thành công. Xem [hợp đồng](docs/client-server-contract.md),
[release notes](docs/release-notes.md) và [báo cáo lịch sử](docs/history/README.md).

Đây là dự án cộng đồng độc lập, không phải sản phẩm chính thức của OpenAI hoặc
Nous Research. Codex Desktop là tích hợp đầu tiên; chưa có adapter cho mọi AI agent
hay API cho Hermes tự tạo task Desktop.

Xem [bằng chứng kiểm thử và triển khai](docs/testing-report-v0.7.0.md).

## Long conversations and actionable errors (0.8)

See the [English AI operating guide](docs/ai-operations.md) and [versioned Hermes compatibility patch](docs/hermes-compatibility.md). The gateway wheel alone does not remove the receiver's legacy conversation limit. Verify the authenticated peer policy with `client-doctor`. Keep the same context, observe existing operations with get/wait, and never blindly resend ambiguous work.
