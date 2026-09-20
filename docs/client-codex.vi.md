# Client: cài đặt và sử dụng với Codex Desktop

Hướng dẫn này dành cho **v0.7.0 client/server** của Hermes A2A Gateway. Codex trên máy cá nhân giao việc cho Hermes trên server; daemon giữ inbox local và lấy kết quả qua HTTPS/SSE. Package, lệnh và đăng ký MCP thống nhất tên `hermes-a2a-gateway`.

```text
Codex Desktop → MCP client-mcp → daemon + SQLite → HTTPS/SSE → server → Hermes
      ↑                            |
      └── tham chiếu kết quả muộn ──┘
```

Làm [hướng dẫn server](server-hermes.vi.md) trước, sau đó làm các bước bên dưới trên **máy chạy Codex**. Không cần cài Hermes hoặc PostgreSQL trên máy client. Đây là hướng dẫn cho cài mới; nếu máy đã có client đang hoạt động, làm [di trú v0.7](migration-v0.7.md) trước, giữ key, token và dữ liệu; không chạy bước tạo mới lên inbox đã có.

## 1. Chuẩn bị

- CPython **3.11**, có `venv`, `curl` và một tài khoản hệ điều hành dùng riêng cho bạn.
- Codex Desktop đã đăng nhập và dùng được; xác định đường dẫn tuyệt đối tới Codex CLI của cùng môi trường/tài khoản.
- Server đã sẵn sàng. Nhận ba thông tin qua kênh quản trị được xác thực: **HTTPS origin** (ví dụ `https://gateway.example.internal:9443`, không thêm `/v1`), **device token riêng cho máy này**, và **CA certificate** nếu server dùng CA riêng. Không nhận private key TLS hoặc encryption key của broker.
- Máy client phải truy cập được mạng riêng/VPN của server, và hostname/IP trong URL phải khớp SAN của chứng chỉ. Không dùng `curl -k` hoặc tắt kiểm tra TLS.

Wheel v0.7 đã qua cài sạch trên macOS và Linux trong CI. Candidate được cài từ GitHub và kiểm chứng thêm trên Mac/VPS. Luồng kết quả muộn đã được kiểm chứng trên macOS; không suy ra mọi bản Desktop hoặc Linux có hành vi wake giống nhau. Client dùng Unix socket, chưa hỗ trợ Windows. Adapter v0.7.0 chỉ cho phép CLI `0.154.0` và `0.155.0-alpha.9.2`, đồng thời kiểm tra schema queue; ngoài danh sách đó sẽ dùng cơ chế lấy kết quả chủ động nếu host vẫn cung cấp native MCP metadata. Xem [bằng chứng v0.7](testing-report-v0.7.0.md) và [các phiên bản đã kiểm chứng trước đó](history/testing-report-v0.6.0b1.md).

Tìm CLI trong terminal. Đường dẫn bundle phụ thuộc tên ứng dụng thực tế:

```bash
command -v codex || true
# Chọn đường dẫn có thật trên máy; đây là ví dụ cho bundle ChatGPT:
codex_bin="/Applications/ChatGPT.app/Contents/Resources/codex"
# Hoặc: codex_bin="/Applications/Codex.app/Contents/Resources/codex"
# Hoặc đặt codex_bin bằng đường dẫn tuyệt đối do command -v trả về.
test -x "$codex_bin" && "$codex_bin" --version
```

Giữ giá trị `codex_bin` cho bước đăng ký MCP. Không đổi `CODEX_HOME` sang một tài khoản/thư mục khác: adapter cần thấy task của Desktop hiện tại. Trên host dùng `CODEX_HOME` tùy chỉnh, launcher và service cũng phải nhận đúng giá trị đó từ cấu hình riêng của bạn.

## 2. Cài package và nhận credential

Thực hiện khối [cài cùng release trên hai máy](deployment.md#install-the-same-release-on-both-machines). Nó tải wheel từ GitHub, kiểm tra SHA256 và cài vào:

```text
~/.local/share/hermes-a2a-gateway/venv
```

Tạo thư mục riêng rồi chuyển token và CA từ server bằng kênh quản trị của bạn. Lưu token dạng một dòng vào `device.token`, CA PEM vào `ca.crt`. Với chứng chỉ CA công khai được hệ điều hành tin cậy, bỏ qua file CA.

```bash
umask 077
mkdir -p "$HOME/.config/hermes-a2a-gateway" "$HOME/.local/state/hermes-a2a-gateway/client"
chmod 700 "$HOME/.config/hermes-a2a-gateway" "$HOME/.local/state/hermes-a2a-gateway/client"
# Sau khi chuyển file credential vào đúng vị trí:
chmod 600 "$HOME/.config/hermes-a2a-gateway/device.token"
```

Không dán token vào lệnh, chat hoặc `~/.codex/config.toml`. Mỗi thiết bị dùng token và state directory riêng; không sao chép SQLite đang mở giữa hai máy.

## 3. Tạo cấu hình và launcher

Chạy khối sau bằng terminal tương tác. Nó hỏi URL, đường dẫn CLI và CA qua terminal; đọc token từ file, sinh **client payload key độc lập**, ghi file quyền riêng tư và từ chối ghi đè cấu hình cũ. Nếu cài đặt dở dang, kiểm tra file đã tạo trước khi tiếp tục; không xóa hay sinh lại key của inbox đã có dữ liệu.

```bash
"$HOME/.local/share/hermes-a2a-gateway/venv/bin/python" - <<'PY'
import json
import os
import shlex
from pathlib import Path
from cryptography.fernet import Fernet
from hermes_a2a_gateway.client_settings import ClientSettings

os.umask(0o077)
base = Path.home() / ".config/hermes-a2a-gateway"
venv = Path.home() / ".local/share/hermes-a2a-gateway/venv"
state = Path.home() / ".local/state/hermes-a2a-gateway/client"
for name in ("client.json", "run.py", "launch"):
    if (base / name).exists():
        raise SystemExit(f"Đã có {name}; giữ cấu hình hiện tại, không ghi đè.")
if any(state.iterdir()):
    raise SystemExit("State không rỗng; cần dùng lại cấu hình/key hiện có.")

def ask(label):
    with open("/dev/tty", "r+") as tty:
        tty.write(label + ": ")
        tty.flush()
        return tty.readline().strip()

url = ask("HTTPS origin của broker")
codex = str(Path(ask("Đường dẫn tuyệt đối Codex CLI")).resolve(strict=True))
if not os.access(codex, os.X_OK):
    raise SystemExit("Codex CLI không executable")
ca = ask("Đường dẫn tuyệt đối CA PEM, để trống nếu CA công khai")
if ca:
    ca = str(Path(ca).resolve(strict=True))
token = (base / "device.token").read_text().strip()
key = Fernet.generate_key().decode()
ClientSettings(broker_url=url, token=token, payload_key=key,
               state_dir=state, codex_command=codex, ca_file=ca or None)
values = {
    "HERMES_A2A_GATEWAY_CLIENT_BROKER_URL": url,
    "HERMES_A2A_GATEWAY_CLIENT_TOKEN": token,
    "HERMES_A2A_GATEWAY_CLIENT_PAYLOAD_KEY": key,
    "HERMES_A2A_GATEWAY_CLIENT_STATE_DIR": str(state),
    "HERMES_A2A_GATEWAY_CLIENT_CODEX_COMMAND": codex,
}
if ca:
    values["HERMES_A2A_GATEWAY_CLIENT_CA_FILE"] = ca
with (base / "client.json").open("x") as output:
    json.dump(values, output, indent=2)
runner = '''import json
import os
import sys
from pathlib import Path

os.umask(0o077)
mode = sys.argv[1] if len(sys.argv) == 2 else ""
if mode not in {"client", "client-mcp", "client-doctor"}:
    raise SystemExit("Expected client, client-mcp or client-doctor")
values = json.loads(Path(__file__).with_name("client.json").read_text())
env = {k: v for k, v in os.environ.items()
       if not k.startswith("HERMES_A2A_GATEWAY_")}
if mode == "client-mcp":
    # MCP chỉ cần socket và giới hạn upload, không cần credential của broker.
    values = {k: v for k, v in values.items() if k in {
        "HERMES_A2A_GATEWAY_CLIENT_STATE_DIR",
        "HERMES_A2A_GATEWAY_CLIENT_MAX_UPLOAD_BYTES"}}
env.update(values)
binary = str(Path(sys.executable).parent / "hermes-a2a-gateway")
os.execve(binary, [binary, mode], env)
'''
with (base / "run.py").open("x") as output:
    output.write(runner)
with (base / "launch").open("x") as output:
    output.write("#!/bin/sh\nexec " + shlex.quote(str(venv / "bin/python"))
                 + " " + shlex.quote(str(base / "run.py")) + ' "$@"\n')
(base / "launch").chmod(0o700)
print("Đã tạo cấu hình và launcher; không xuất credential.")
PY
```

`client.json` là nguồn cấu hình cho daemon. Khi đổi token về sau, cập nhật giá trị `HERMES_A2A_GATEWAY_CLIENT_TOKEN` trong file này bằng editor riêng tư; chỉ thay `device.token` sẽ **không** cập nhật daemon. Giữ payload key cùng bản sao lưu inbox; mất key sẽ không giải mã được dữ liệu đã lưu. Launcher giúp ứng dụng GUI chạy được mà không phụ thuộc `export` trong một terminal khác.

Muốn kết nối qua SSH, bổ sung [cấu hình SSH tunnel](ssh-tunnel.vi.md) vào `client.json` sau bước này và trước khi chạy daemon. Cả hai chế độ dùng cùng inbox và đăng ký MCP.

## 4. Chạy daemon nền

Chọn **một** cách. Chỉ có một daemon cho mỗi `STATE_DIR`. Để thử foreground, chạy `~/.config/hermes-a2a-gateway/launch client` và giữ terminal mở; dừng bằng Ctrl-C trước khi bật service.

### macOS: LaunchAgent

Tạo plist với đường dẫn tuyệt đối; không dùng `~` hoặc `$HOME` bên trong `ProgramArguments` của plist:

```bash
"$HOME/.local/share/hermes-a2a-gateway/venv/bin/python" - <<'PY'
import os
import plistlib
from pathlib import Path

os.umask(0o077)
root = Path.home()
folder = root / "Library/LaunchAgents"
folder.mkdir(parents=True, exist_ok=True)
state = root / ".local/state/hermes-a2a-gateway/client"
plist = {
    "Label": "com.hermes.a2a.gateway.client",
    "ProgramArguments": [str(root / ".config/hermes-a2a-gateway/launch"), "client"],
    "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "Umask": 0o077,
    "StandardOutPath": str(state / "daemon.stdout.log"),
    "StandardErrorPath": str(state / "daemon.stderr.log"),
}
with (folder / "com.hermes.a2a.gateway.client.plist").open("xb") as output:
    plistlib.dump(plist, output)
PY
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.hermes.a2a.gateway.client.plist"
launchctl print "gui/$(id -u)/com.hermes.a2a.gateway.client"
```

LaunchAgent chạy trong phiên đăng nhập của bạn, không đảm bảo hoạt động khi máy tắt hoặc đã logout. Nếu dùng `CODEX_HOME` tùy chỉnh, thêm `EnvironmentVariables` với đường dẫn tuyệt đối vào plist trước khi bootstrap và cấu hình Desktop cùng giá trị.

### Linux: user systemd

Đây là cách chạy daemon trên Linux; khả năng native queue phụ thuộc host/CLI và cần kiểm chứng riêng. Lưu file **mới** `~/.config/systemd/user/hermes-a2a-gateway-client.service` (tạo thư mục nếu chưa có):

```ini
[Unit]
Description=Hermes A2A Gateway client
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.config/hermes-a2a-gateway/launch client
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-a2a-gateway-client.service
systemctl --user status hermes-a2a-gateway-client.service --no-pager
```

## 5. Kiểm tra và đăng ký MCP

```bash
"$HOME/.config/hermes-a2a-gateway/launch" client-doctor
"$codex_bin" mcp get hermes-a2a-gateway
# Nếu chưa có entry này, thêm một lần:
"$codex_bin" mcp add hermes-a2a-gateway -- \
  "$HOME/.config/hermes-a2a-gateway/launch" client-mcp
"$codex_bin" mcp get hermes-a2a-gateway
"$codex_bin" mcp list
```

Sau lệnh `mcp add`, sửa **table vừa tạo** trong `~/.codex/config.toml` (hoặc
`$CODEX_HOME/config.toml` nếu dùng thư mục riêng), thêm `tool_timeout_sec = 90`.
Không tạo table thứ hai cùng tên; giữ launcher và các thiết lập khác:

```toml
[mcp_servers.hermes-a2a-gateway]
command = "/duong/dan/tuyet/doi/.config/hermes-a2a-gateway/launch"
args = ["client-mcp"]
tool_timeout_sec = 90
```

Thay `command` bằng đường dẫn thật do `mcp add` đã ghi. Ngân sách 90 giây bao gồm
thời gian chờ tối đa 60 giây ở daemon và overhead MCP; [mặc định Codex là 60 giây](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
Chạy lại `codex mcp get hermes-a2a-gateway` và mở task mới để cấu hình có hiệu lực.

Trong terminal mới, đặt lại `codex_bin` như bước 1. Nếu đã có entry, đối chiếu launcher/state trước khi thay; không tạo hai entry cùng trỏ vào cùng inbox. Cấu hình MCP được dùng chung giữa Desktop và CLI theo [tài liệu OpenAI MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Broker là API riêng của gateway, nên không đăng ký URL broker bằng `codex mcp add --url`.

`client-doctor` chỉ đọc cấu hình, daemon và capability native; nó không gửi task Hermes. Kiểm tra `ok: true`, `daemon.ok: true`, `daemon.transport_error: null`; `native_delivery.supported: true` là điều kiện capability cho kết quả muộn. `ok: true` một mình không chứng minh kết nối broker/Hermes hay native wake đã hoạt động. Nếu native không hỗ trợ, dùng `gateway_get`/`gateway_wait`, với điều kiện host vẫn cấp metadata.

Mở **task mới** trong Desktop để nạp đăng ký MCP. Nếu vẫn chưa thấy tools, khởi động lại Desktop khi công việc đang chạy đã kết thúc. Phải thấy năm tool bên dưới, có thể qua tool discovery của Codex; không yêu cầu tên có tiền tố hiển thị giống nhau ở mọi host.

## 6. Sử dụng hằng ngày

| Tool | Khi nào dùng | Tham số chính |
| --- | --- | --- |
| `gateway_submit` | Giao một yêu cầu mới cho Hermes | `prompt`, `wait_seconds` (0–60, mặc định 15), `artifact_ids` tùy chọn |
| `gateway_get` | Lấy trạng thái/kết quả của yêu cầu đã gửi | `operation_id` |
| `gateway_wait` | Chờ yêu cầu cũ, không gửi lại | `operation_id`, `timeout` (0–60) |
| `gateway_cancel` | Yêu cầu hủy best-effort | `operation_id` |
| `gateway_upload_artifact` | Gửi một file local đã chọn | `path` tuyệt đối, `content_type` |

Bạn có thể yêu cầu Codex bằng ngôn ngữ tự nhiên:

> Dùng gateway_submit giao Hermes kiểm tra và tóm tắt vấn đề sau: … Gửi một lần, giữ operation_id; nếu chưa xong thì dùng gateway_get hoặc gateway_wait cho đúng operation đó.

Để kiểm tra cài đặt, **chủ động cho phép một model task nhỏ** trong Desktop:

> Gửi đúng một yêu cầu qua gateway_submit: “Chỉ trả lời HERMES_GATEWAY_OK_20260921_TEST1”, wait_seconds = 0. Giữ operation_id và dùng gateway_get/gateway_wait để kiểm tra kết quả, không submit lại.

Đổi hậu tố marker cho mỗi lần kiểm tra. Thành công khi operation đạt `completed` và `result.text` chứa marker đúng. Ghi lại operation ID, result ID và phiên bản hai đầu; không công khai token hoặc nội dung công việc. Bài kiểm tra này có dùng model trên Hermes; nó là bước người vận hành thực hiện, không nằm trong CI.

**Task lâu:** `wait_seconds: 0` trả handle sớm; mặc định đợi inline tối đa 15 giây. `HERMES_A2A_GATEWAY_CLIENT_INLINE_WAIT_SECONDS` chỉ đặt thời gian chờ mặc định. Giá trị tường minh `wait_seconds: 20` hoặc `60` hợp lệ; `gateway_wait.timeout` cũng hỗ trợ 0–60 giây. Giữ nguyên task Desktop gốc. Khi kết quả đến, daemon có thể đưa một **tham chiếu** vào task đó, rồi Codex gọi `gateway_get` đọc nội dung. ACK queue chỉ xác nhận đã xếp hàng; không chứng minh Desktop đã thức dậy hoặc model đã đọc kết quả. Nếu không có thông báo, yêu cầu Codex lấy kết quả bằng operation ID cũ. Không có cam kết wake khi Desktop đóng/task chưa được load.

**File đầu vào:** v0.7 chỉ hỗ trợ đính kèm cho Hermes bằng file `text/plain` UTF-8, tối đa 8 MiB theo cấu hình client mặc định. Ví dụ tham số tool:

```json
{"path":"/absolute/path/to/requirements.txt","content_type":"text/plain"}
```

Lấy `artifact_id` từ kết quả upload và truyền vào `artifact_ids` của lần submit mới. Không tự động đồng bộ repo hay mount filesystem máy bạn lên VPS. Hermes sử dụng công cụ, model và filesystem đã cấu hình trên server; chỉ gửi những file bạn muốn chuyển sang đó. Mặc định `content_type` của tool là `application/octet-stream`, vì vậy cần ghi rõ `text/plain`; ảnh/PDF/binary chưa phải attachment đầu vào được hỗ trợ trong profile này.

**Phạm vi task:** handle gắn với native metadata của task Desktop và token thiết bị. Dùng `get`/`wait` trong task gốc; dán ID vào task hoặc máy khác không chuyển quyền sở hữu. Không tự tạo `threadId`/`turnId` trong prompt để thay thế metadata. `outcome_unknown` cần đối chiếu đúng handle đã lưu; không dùng một lần submit mới để “thử lại”. Hủy chỉ là yêu cầu best-effort, không khẳng định tính toán Hermes đã dừng.

Luồng này nhận lại kết quả của công việc **Codex đã giao Hermes**. Bản v0.7 đã loại bỏ gateway inbound cũ và plugin Hermes gọi Codex; không cung cấp API cho Hermes tự tạo task Desktop.

Tối đa 16 artifact ID cho một lần submit; vượt giới hạn sẽ bị từ chối trước khi
lưu operation ở client.

## 7. Vận hành và xử lý lỗi

| Hiện tượng | Kiểm tra / xử lý |
| --- | --- |
| `invalid_wait` | Giá trị chờ phải hữu hạn trong khoảng 0–60; bản mới nhận `20` ngay cả khi mặc định là `15`. |
| `native_metadata_required` | Dùng task Codex host có native MCP metadata; MCP client thông thường hoặc ID do model tự nhập không đủ. |
| `daemon.unavailable`, lỗi socket | Kiểm tra daemon, launcher và cùng `STATE_DIR`; xem log, chỉ chạy một daemon. Không xóa SQLite/lock để ép chạy process thứ hai. |
| `daemon.transport_error` khác `null` | Kiểm tra mạng/VPN, URL, CA, token và health server; doctor không tự sửa cấu hình. |
| TLS/certificate error | Dùng đúng CA và hostname có trong SAN; không bỏ xác minh TLS. |
| HTTP 401 | Đối chiếu token trong `client.json` với device token server; cập nhật rồi restart daemon. |
| Không thấy operation / HTTP 404 | Đối chiếu đúng task gốc, thiết bị, operation ID và thời hạn giữ dữ liệu. Không suy ra cần submit lại. |
| Native unsupported hoặc `delivery_outcome_unknown` | Dùng `gateway_get`/`gateway_wait`; không tự enqueue lại thông báo chưa rõ ACK. |
| `outcome_unknown` sau gián đoạn Hermes | Giữ handle, để hệ thống đối chiếu remote task đã lưu. Khi không có bằng chứng đủ chắc, trạng thái unknown là có chủ ý. |
| `peer_requires_input` | Beta chưa hỗ trợ vòng hỏi/đáp tương tác của Hermes trong luồng broker. Đưa đủ thông tin đầu vào cho một yêu cầu mới sau khi đã xác định trạng thái yêu cầu cũ. |
| Artifact bị từ chối | Kiểm tra file thường, không symlink, không rỗng, UTF-8 `text/plain`, hạn mức và TTL. |

Xem log macOS trong `~/.local/state/hermes-a2a-gateway/client/daemon.stderr.log`; Linux dùng `journalctl --user -u hermes-a2a-gateway-client.service -n 100 --no-pager`. Log và kết quả có thể nhạy cảm; che thông tin riêng trước khi chia sẻ.

Restart macOS (sau khi đối chiếu các operation đang chạy):

```bash
launchctl kickstart -k "gui/$(id -u)/com.hermes.a2a.gateway.client"
```

Linux: `systemctl --user restart hermes-a2a-gateway-client.service`. Khởi động lại không phải là gửi lại công việc; tiếp tục lấy kết quả bằng handle cũ. TTL mặc định của payload là một ngày, kết quả/event là bảy ngày; inbox không phải kho lưu trữ vĩnh viễn.

Để ngừng dùng, dừng service rồi gỡ đúng entry MCP:

```bash
# macOS:
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.hermes.a2a.gateway.client.plist"
# Linux thay lệnh trên bằng:
# systemctl --user disable --now hermes-a2a-gateway-client.service
"$codex_bin" mcp remove hermes-a2a-gateway
```

Giữ state, config/key và wheel đã kiểm tra checksum để phục hồi. Không chạy binary legacy trên store v0.7. Xem [nâng cấp, rollback và giới hạn](deployment.md#upgrade-and-rollback) trước khi thay phiên bản; không nâng cấp venv trong khi daemon đang sử dụng nó.
