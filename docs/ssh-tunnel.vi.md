# Kết nối trực tiếp hoặc SSH tunnel

Bản v0.7 có hai chế độ ở **daemon client**: `direct` (mặc định) và `ssh-tunnel`.
MCP chỉ gọi daemon qua Unix socket, không sở hữu SSH. Nhiều task Codex dùng chung
một tunnel; restart một facade MCP không làm tạo thêm tunnel.

## Chọn đường kết nối

| Chế độ | Dữ liệu người vận hành cung cấp |
| --- | --- |
| `direct` qua LAN/VPN | HTTPS origin của broker, token thiết bị, CA nếu cần |
| `ssh-tunnel` | SSH alias/host; HTTPS proxy đích nhìn từ SSH server; token/CA; tùy chọn user, port, khóa và TLS server name |

LAN/VPN không thay xác minh TLS hoặc bearer token. SSH host và broker đích có thể
khác nhau: SSH tới domain public, rồi forward tới IP private của TLS proxy trên VPS.
Không forward trực tiếp vào HTTP broker `8790` rồi tắt TLS để tránh cấu hình chứng chỉ.

Trước tiên hoàn thành [server](server-hermes.vi.md) và tạo private launcher/config
ở bước 3 của [client](client-codex.vi.md). Nếu đang nâng cấp, làm [di trú](migration-v0.7.md).

## Direct: mặc định

Trong `client.json` riêng tư, giữ token/key hiện có và đặt:

```json
{
  "HERMES_A2A_GATEWAY_CLIENT_TRANSPORT_MODE": "direct",
  "HERMES_A2A_GATEWAY_CLIENT_BROKER_URL": "https://gateway.example.internal:9443"
}
```

Đây là **các trường cần ghép vào cấu hình**, không thay toàn bộ file JSON chứa
payload key, token, state directory và đường dẫn Codex.

## SSH tunnel do daemon quản lý

Ưu tiên một alias đã hoạt động trong `~/.ssh/config`. Xác minh host key bằng kênh
quản trị tin cậy và hoàn tất SSH key/agent trước; service không hỏi mật khẩu hay tự
chấp nhận key mới. Daemon đọc config SSH của đúng user chạy service. Alias này chỉ chứa kết nối/xác
thực; nếu có `LocalForward`, `RemoteForward` hoặc `DynamicForward` kế thừa, daemon
sẽ từ chối. Tạo alias riêng trỏ cùng VPS nếu alias hiện có đang phục vụ tunnel khác.

```sshconfig
Host my-hermes-server
  HostName ssh.example.com
  User operator
  Port 2222
  IdentityFile ~/.ssh/hermes-server
  IdentitiesOnly yes
  StrictHostKeyChecking yes
```

Ghép các trường sau vào `client.json`, thay alias và đích TLS bằng cấu hình thật:

```json
{
  "HERMES_A2A_GATEWAY_CLIENT_TRANSPORT_MODE": "ssh-tunnel",
  "HERMES_A2A_GATEWAY_CLIENT_BROKER_URL": "https://127.0.0.1:18790",
  "HERMES_A2A_GATEWAY_CLIENT_SSH_HOST": "my-hermes-server",
  "HERMES_A2A_GATEWAY_CLIENT_SSH_LOCAL_PORT": "18790",
  "HERMES_A2A_GATEWAY_CLIENT_SSH_REMOTE_HOST": "192.0.2.10",
  "HERMES_A2A_GATEWAY_CLIENT_SSH_REMOTE_PORT": "9443",
  "HERMES_A2A_GATEWAY_CLIENT_SSH_TLS_SERVER_NAME": "gateway.example.internal"
}
```

`192.0.2.10` chỉ là IP minh họa; thay bằng địa chỉ TLS proxy mà SSH server truy cập
được. Nếu proxy thực sự bind `127.0.0.1:9443` trên VPS thì dùng `127.0.0.1`; nếu chỉ
bind IP LAN, phải dùng IP LAN đó. Cổng local phải còn trống và trùng cổng trong
`BROKER_URL`. Nó chỉ lắng nghe loopback trên client, không mở cho máy khác.

`SSH_TLS_SERVER_NAME` là hostname/IP có trong SAN của chứng chỉ broker. Client dùng
nó để xác minh TLS qua tunnel và gửi Host phù hợp, không bỏ hostname verification.
Nếu chứng chỉ đã chứa `127.0.0.1`, có thể bỏ trường này. `CA_FILE` và device token
vẫn dùng như direct. Private key của CA/TLS và encryption key broker ở lại VPS.

Các override tùy chọn dùng khi không đặt trong SSH alias:

| Biến (sau `HERMES_A2A_GATEWAY_CLIENT_`) | Ý nghĩa |
| --- | --- |
| `SSH_USER` / `SSH_PORT` | User và cổng SSH, khác cổng TLS đích |
| `SSH_IDENTITY_FILE` | Đường dẫn tuyệt đối SSH private key; không nhúng nội dung khóa |
| `SSH_COMMAND` | OpenSSH executable, mặc định `ssh` |
| `SSH_CONNECT_TIMEOUT_SECONDS` | Ngân sách thiết lập SSH, mặc định 10 giây |

Mật khẩu/khóa không được truyền trong MCP tool arguments. Với khóa có passphrase,
SSH agent phải sẵn sàng trong môi trường service; đăng nhập được từ terminal chưa
chứng minh LaunchAgent/systemd nhìn thấy cùng SSH agent. Dùng tài khoản/khóa theo
policy hiện có, không tự động nới xác minh để làm service chạy.

Sau khi lưu cấu hình, restart **daemon** theo hướng dẫn client rồi chạy:

```bash
"$HOME/.config/hermes-a2a-gateway/launch" client-doctor
```

Kiểm tra lần lượt SSH, TLS, broker và capability native. Doctor không gửi model task
và không chứng minh Hermes đã trả lời. Chỉ khi transport sẵn sàng, chạy bài kiểm tra
vô hại `wait_seconds: 20` trong task Desktop mới, giữ operation ID và chờ kết quả.

## Mất mạng và phục hồi

Daemon dùng keepalive và backoff có jitter, tăng thời gian chờ tới giới hạn thay vì
liên tục mở kết nối: keepalive mỗi 15 giây, tối đa ba lần không phản hồi; retry có
jitter từ khoảng 1 đến 30 giây. Network retry chỉ khôi phục tunnel/HTTPS/SSE. Khi mạng trở lại,
client đọc tiếp cursor đã lưu và GET operation cũ. Hermes có thể đã chạy xong lúc
client offline; không tạo task mới để “thử lại”.

Lỗi xác thực SSH, host key thay đổi, cấu hình sai hoặc cổng local bị chiếm cần xử lý
đúng nguyên nhân. Không xóa known_hosts, tắt TLS hay giết mọi process SSH để sửa lỗi.
Daemon chỉ dừng SSH và process con đã xác định thuộc SSH đó. Trên macOS, SSH giữ
nhóm tiến trình của LaunchAgent để launchd dọn listener khi daemon bị kill; trên
Linux, systemd quản lý cả cgroup. Với `ProxyCommand` hoặc helper tùy biến tự tách
khỏi nhóm hay cố tình bỏ qua SIGTERM, không đảm bảo dọn hết sau daemon SIGKILL.
Gateway không nhận nuôi hoặc tự giết listener không xác định đang chiếm cổng. Thay cấu hình có chủ đích rồi restart
service; giữ state, key và operation IDs.

`ExitOnForwardFailure` phát hiện lỗi thiết lập forward, không chứng minh dịch vụ
đích còn hoạt động; vì vậy client kiểm tra TLS/broker riêng. SSH keepalive phát hiện
kết nối mất phản hồi, còn daemon quản lý restart. [Tài liệu OpenSSH](https://man.openbsd.org/ssh_config).

## Kiểm chứng một deployment

1. Direct hoạt động và TLS/auth đúng trước khi chuyển sang SSH.
2. Qua SSH, `client-doctor` sẵn sàng; submit một yêu cầu có marker riêng với wait 20.
3. Giao một công việc đủ lâu; ngắt **đúng SSH child của daemon**, giữ daemon/broker/Hermes.
4. Xác nhận tunnel được tạo lại, cùng operation/remote task được lấy lại và chỉ có một
   kết quả; đối chiếu ledger/A2A dispatch để phát hiện submit trùng.
5. Kiểm tra restart daemon và native queue ACK/consumption riêng. Khi thử sleep/wake,
   chỉ ngủ máy nếu không ảnh hưởng công việc khác và có người vận hành phối hợp.

Không suy ra sleep/wake thực tế hoặc native auto-wake từ một test fake SSH. Ghi rõ
những trường hợp đã chạy và chưa chạy trong báo cáo triển khai.

## Long conversations and actionable errors (0.8)

See the [English AI operating guide](ai-operations.md) and [versioned Hermes compatibility patch](hermes-compatibility.md). The gateway wheel alone does not remove the receiver's legacy conversation limit. Verify the authenticated peer policy with `client-doctor`. Keep the same context, observe existing operations with get/wait, and never blindly resend ambiguous work.
