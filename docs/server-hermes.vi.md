# Server: cài đặt và sử dụng Hermes A2A Gateway

Tài liệu này dành cho người vận hành một máy chủ riêng chạy Hermes và một hoặc
nhiều máy Codex Desktop. Nó triển khai nhánh **Codex → Hermes → trả kết quả về
Codex** của `v0.8.0`:

```text
Codex Desktop → client daemon → HTTPS riêng → broker → Hermes A2A loopback
                                      PostgreSQL 16
```

Tên package/lệnh là **`hermes-a2a-gateway`**, biến môi trường dùng
`HERMES_A2A_GATEWAY_*`. Đây là một broker cho một người sở hữu và các thiết bị
được cấp quyền. Làm [hướng dẫn client](client-codex.vi.md) trên máy Codex sau
khi server sẵn sàng; hai phía cài cùng wheel theo [hướng dẫn cài chung](deployment.md#install-the-same-release-on-both-machines).

Bản v0.7 chỉ giữ client/server. Gateway inbound Codex, bảy tool `hermes_*`, plugin
Hermes `codex_a2a` và bộ cài legacy đã được gỡ khỏi sản phẩm. Khi nâng cấp, làm
[di trú và dọn cài đặt cũ](migration-v0.7.md), giữ dữ liệu/khóa của broker hiện tại.
SSH tunnel là tùy chọn phía client; broker và Hermes tiếp tục dùng cùng HTTPS/A2A.

## Điều kiện trước khi thay đổi

Chuẩn bị trước:

- Một Hermes đang hoạt động dưới một tài khoản Unix riêng, với A2A nội bộ đã xác
  thực và chỉ nghe `127.0.0.1:9900`.
- PostgreSQL 16 trên cùng máy, một database riêng cho broker.
- CPython 3.11 và wheel `v0.8.0` đã được xác minh theo
  [hướng dẫn cài chung](deployment.md#install-the-same-release-on-both-machines).
- DNS/private VPN và một reverse proxy TLS có CA được máy Codex tin cậy.

### Nếu đây là máy Hermes mới

Hoàn tất Hermes trước khi cài gateway. Dùng đúng phương thức cài và provider
được Hermes upstream hỗ trợ trong [Installation](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/getting-started/installation.md)
và [Quickstart](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/getting-started/quickstart.md): trên Linux/macOS, installer CLI chính thức là
`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`; sau đó
`hermes setup` (hoặc `hermes model` khi đã biết provider) hướng dẫn cấu hình
provider/model. Có thể dùng `hermes setup --portal` khi người vận hành chọn
Nous Portal. Không tự tạo API key, không chép credential vào broker, và không
thay phương thức cài của một Hermes đã tồn tại.

Trước khi bật A2A, hoàn thành một chat bình thường theo Quickstart bằng tài khoản
Hermes đó. A2A không sửa được Hermes chưa có provider/model hoạt động. Giữ lại
lifecycle owner mà Hermes installer hoặc deployment hiện hữu đã tạo; broker bên
dưới là user service riêng và không thay nó.

Các kiểm tra này chỉ đọc trạng thái, không gửi prompt tới model:

```bash
hermes --version
ss -ltn 'sport = :9900'
curl --fail --silent --show-error \
  http://127.0.0.1:9900/.well-known/agent-card.json >/dev/null
psql --version
```

Nếu cổng `9900`, dịch vụ Hermes, database hoặc cấu hình đã thuộc một deployment
khác, dừng lại và ghi nhận chủ sở hữu/lifecycle của nó. Không chạy thêm gateway,
không thay khóa, không xóa state, và không restart Hermes chỉ để làm theo ví dụ.

## 1. Giữ Hermes A2A ở loopback và xác thực broker

Hermes upstream dùng plugin `a2a-platform`. Theo tài liệu upstream, platform
được bật ở `gateway.platforms.a2a`, mặc định bind `127.0.0.1`, cổng mặc định là
`9900`, và bind rộng chỉ được phép khi có token cùng `A2A_HOST` rõ ràng.
Xem [README của plugin A2A](https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/a2a/README.md)
và [tài liệu cấu hình A2A chính thức](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/a2a.md).

Broker ở cùng máy nên không cần và không nên mở Hermes A2A ra mạng. Trong cấu
hình/profile Hermes hiện có, giữ platform như sau; đặt secret bằng cơ chế env
riêng tư mà dịch vụ Hermes hiện đang dùng, thường là tệp `~/.hermes/.env` mode
`0600`, không phải YAML hay command line.

```yaml
# ~/.hermes/config.yaml — hợp nhất vào profile hiện có, không thay toàn bộ file
gateway:
  platforms:
    a2a:
      enabled: true
      extra:
        port: 9900
```

Trong tệp môi trường riêng của Hermes, tạo **một token chỉ dành cho broker** và
gán một identity ổn định. Các giá trị dưới đây là tên minh họa, không phải secret
để sao chép:

```dotenv
A2A_HOST=127.0.0.1
A2A_PORT=9900
A2A_PEER_TOKENS=codex-broker:GIU_TOKEN_RIENG_TU_O_DAY
A2A_TRUSTED_PEERS=codex-broker
```

`A2A_PEER_TOKENS` gắn credential với identity của broker. Giữ allow-list
`A2A_TRUSTED_PEERS`, không bật `A2A_ALLOW_ALL_USERS` trên server. Các giới hạn
request, số lượt và timeout của Hermes độc lập với broker; xem
[bảng biến môi trường upstream](https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/a2a/README.md#env-vars).

Với máy mới, có thể bật A2A qua wizard thay cho sửa YAML:

```bash
hermes gateway setup  # chọn A2A; giữ bind loopback và credential đã cấu hình
hermes gateway        # foreground, chỉ dùng nếu chưa có service Hermes chạy
```

Giữ terminal foreground mở trong lúc kiểm tra, hoặc chuyển sang cách chạy nền
trong [hướng dẫn gateway chính thức](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/).
Với máy đã có service, dùng service đó, không chạy thêm process foreground.
Sau khi dùng **đúng lifecycle owner hiện có** để reload/restart Hermes, kiểm tra
lại Agent Card loopback ở phần trước. Không thực hiện `message/send` để “test”:
đó là một model task thật. Token Hermes này sẽ đi vào
`HERMES_A2A_TOKEN` của broker ở bước 3 và không được đưa cho máy Codex.

## 2. PostgreSQL 16: database và role riêng

Chọn trước tên tài khoản Unix chạy Hermes/broker, ví dụ `hermes`. Với peer auth
qua Unix socket, role PostgreSQL phải trùng tên tài khoản Unix đó. Kiểm tra
`pg_hba.conf` trước: chỉ dùng DSN socket nếu policy của máy thực sự cho `peer`;
nếu không, dùng DSN có mật khẩu trong tệp môi trường bảo vệ hoặc cơ chế secret
manager của hệ thống. Không mở PostgreSQL cho mạng workstation.

Lệnh sau chỉ là thủ tục tạo ban đầu do quản trị viên PostgreSQL chạy. Nó kiểm tra
tên trước rồi dừng khi role/database đã tồn tại, nên không thay quyền sở hữu hay
ghi đè một database đang dùng. Thay `hermes` và `hermes_a2a_broker` bằng các tên
đã chọn cho deployment trước khi chạy.

```bash
(
set -eu
broker_os_user=hermes
broker_database=hermes_a2a_broker
# Chỉ dùng tên ASCII gồm chữ thường, chữ số và dấu gạch dưới.
case "$broker_os_user$broker_database" in
  *[!a-z0-9_]*) echo "Invalid role/database name" >&2; exit 1 ;;
esac
role_exists=$(sudo -u postgres psql -v ON_ERROR_STOP=1 -tAc \
  "SELECT 1 FROM pg_roles WHERE rolname = '$broker_os_user'")
db_exists=$(sudo -u postgres psql -v ON_ERROR_STOP=1 -tAc \
  "SELECT 1 FROM pg_database WHERE datname = '$broker_database'")
if [ -n "$role_exists$db_exists" ]; then
  echo "Role/database exists; inspect ownership and reuse intentionally." >&2
  exit 1
fi
sudo -u postgres createuser --login "$broker_os_user"
sudo -u postgres createdb --owner="$broker_os_user" "$broker_database"
)
```

Với ví dụ peer socket, DSN private của broker là:

```text
postgresql:///hermes_a2a_broker?host=/var/run/postgresql
```

Kết nối dưới tài khoản service thực tế để xác nhận quyền tối thiểu, sau đó broker
tự tạo các bảng `broker_v06_*` additive trong database riêng:

```bash
sudo -u hermes psql 'postgresql:///hermes_a2a_broker?host=/var/run/postgresql' \
  -c 'SELECT current_user, current_database();'
```

Nếu dùng DSN mật khẩu, không đặt mật khẩu trong lệnh, shell history, unit file
hay Git. Lưu full DSN chỉ trong `broker.env` mode `0600` ở bước sau (hoặc để
systemd nhận nó từ secret manager). Role broker chỉ cần quyền trên database riêng
của chính nó; không dùng superuser, không dùng database Hermes, và không cấp
quyền PostgreSQL cho client Codex.

## 3. Tạo khóa và environment broker mà không in secret

Các secret cần tách biệt:

| Secret | Chủ sở hữu | Mục đích |
| --- | --- | --- |
| Token Hermes A2A | Hermes + broker | Hermes xác thực broker loopback |
| Fernet broker | Broker | Mã hóa prompt/artifact trong PostgreSQL |
| Token từng device | Broker + đúng client đó | Xác thực HTTPS của workstation |

Tạo thư mục riêng tư và sinh Fernet key cùng token cho một device trực tiếp vào
tệp, không in chúng ra terminal. Đoạn này dừng nếu tệp đã có để không vô tình
thay khóa/token đang dùng. Đổi `workstation-01` thành device ID duy nhất, ngắn
và ổn định.

```bash
umask 077
install -d -m 0700 "$HOME/.config/hermes-a2a-gateway"
"$HOME/.local/share/hermes-a2a-gateway/venv/bin/python" - <<'PYTHON'
import os
import secrets
from pathlib import Path
from cryptography.fernet import Fernet

os.umask(0o077)
base = Path.home() / ".config/hermes-a2a-gateway"
device_id = "workstation-01"
key_file = base / "broker-fernet.key"
token_file = base / f"{device_id}.device.token"
for path in (base / "broker.env", key_file, token_file):
    if path.exists():
        raise SystemExit("Đã có cấu hình/key/token; kiểm tra và giữ bản hiện tại.")
with key_file.open("x") as output:
    output.write(Fernet.generate_key().decode() + "\n")
with token_file.open("x") as output:
    output.write(secrets.token_urlsafe(48) + "\n")
print("Đã tạo key/token trong thư mục riêng tư, không xuất nội dung.")
PYTHON
```

Tạo `broker.env` bằng editor bảo mật hoặc secret manager; đặt file mode `0600`
và thư mục cha mode `0700`. Nó phải chứa các biến sau. Dòng token JSON là một
đối tượng JSON hợp lệ, với đúng `device_id` và nội dung của tệp token; không
dùng placeholder trong deployment thật. Mỗi token phải khác nhau và ít nhất 32
ký tự.

```dotenv
HERMES_A2A_GATEWAY_BROKER_DATABASE_URL=postgresql:///hermes_a2a_broker?host=/var/run/postgresql
HERMES_A2A_GATEWAY_BROKER_DEVICE_TOKENS='{"workstation-01":"TOKEN_RIENG_CUA_DEVICE"}'
HERMES_A2A_GATEWAY_BROKER_ENCRYPTION_KEY=FERNET_KEY_RIENG_CUA_BROKER
HERMES_A2A_GATEWAY_BROKER_PUBLIC_URL=https://gateway.example.internal:9443
HERMES_A2A_GATEWAY_BROKER_HOST=127.0.0.1
HERMES_A2A_GATEWAY_BROKER_PORT=8790
HERMES_A2A_ENDPOINT=http://127.0.0.1:9900
HERMES_A2A_TOKEN=TOKEN_CUA_IDENTITY_codex-broker

# Chọn có chủ đích cho tải công việc; các giá trị dưới đây là mặc định beta.
HERMES_A2A_GATEWAY_BROKER_PEER_TIMEOUT_SECONDS=300
HERMES_A2A_GATEWAY_BROKER_MAX_CONCURRENT_STREAMS=4
```

`BROKER_PUBLIC_URL` phải là HTTPS origin không có path/query/credential. Chỉ
`ALLOW_LOOPBACK_HTTP` mới cho phép HTTP và chỉ cho phát triển loopback; không đặt
biến đó trên máy chủ. Broker chấp nhận tối đa 32 streams, mặc định 4, trong khi
một flow vẫn được tuần tự hóa. `PEER_TIMEOUT_SECONDS` là deadline stream Hermes
tuyệt đối, mặc định 300 giây và tối đa 3600; tăng nó chỉ khi công việc cần thiết
và giới hạn Hermes/proxy cũng phù hợp.

Không xoay Fernet key bằng cách ghi đè tệp. Prompt/artifact chưa hết TTL và kết
quả giữ lại cần key cũ để giải mã. Chỉ xoay sau một quy trình di trú có chủ đích
hoặc sau khi dữ liệu liên quan đã hết hạn. Khi thu hồi device, xóa riêng mapping
của device đó, khởi động lại một broker sau khi đã đối soát operation đang chạy,
rồi xóa token file đã được bàn giao theo policy của tổ chức.

## 4. Chạy một broker bằng user systemd

Tạo thư mục `~/.config/systemd/user` nếu chưa có và lưu unit dưới đây vào file mới.
Thực hiện dưới **cùng user Unix** sở hữu database peer/socket và thư mục
config. Giữ một process broker duy nhất cho mỗi PostgreSQL ledger; advisory lock
cũng sẽ từ chối dispatcher thứ hai. Ví dụ này không chỉnh service Hermes hay
dịch vụ cũ.

```ini
# ~/.config/systemd/user/hermes-a2a-gateway-broker.service
[Unit]
Description=Hermes A2A Gateway v0.7 broker
After=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/hermes-a2a-gateway/broker.env
ExecStart=%h/.local/share/hermes-a2a-gateway/venv/bin/hermes-a2a-gateway broker
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

Chỉ khi đường dẫn, user và `broker.env` đã được kiểm tra, nạp unit và bật nó:

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-a2a-gateway-broker.service
systemctl --user status --no-pager hermes-a2a-gateway-broker.service
curl --fail --silent --show-error http://127.0.0.1:8790/healthz
```

Kết quả health đúng là `{"ok":true}`. `/healthz` cố ý không cần bearer token;
tất cả route operation, event, receipt và artifact đều yêu cầu token device.
Nếu máy chủ phải chạy sau logout, quản trị viên cần bật lingering cho chính user
service đó theo policy hệ điều hành, ví dụ `loginctl enable-linger hermes`. Không
chạy `broker` bằng `sudo` hoặc tạo system service thứ hai trỏ cùng ledger.

## 5. Reverse proxy TLS riêng và SSE

Broker chỉ bind loopback. Reverse proxy là điểm duy nhất lắng nghe HTTPS trên
VPN/private network, với chứng chỉ từ CA mà workstation tin cậy. Giới
hạn firewall/proxy theo subnet VPN hoặc identity của tổ chức; không expose port
`8790`, PostgreSQL, hay `9900` ra ngoài.

Ví dụ Nginx dưới đây được ghép vào cấu hình reverse proxy hiện có. Thay
**192.0.2.10** (IP chỉ dùng trong tài liệu) bằng IP private/VPN của server,
đặt DNS `gateway.example.internal` trỏ đến IP đó và thay hostname/chứng chỉ bằng
giá trị thật. Giữ các policy TLS hiện có; không tạo thêm listener công cộng.
Các dòng tắt buffer/cache cần thiết để chuyển tiếp SSE `/v1/events`.

```nginx
server {
    listen 192.0.2.10:9443 ssl;
    server_name gateway.example.internal;

    ssl_certificate     /etc/nginx/private-ca/gateway.fullchain.pem;
    ssl_certificate_key /etc/nginx/private-ca/gateway.key;

    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        client_max_body_size 10m;
    }
}
```

Sau `nginx -t`, dùng cơ chế reload quen thuộc của proxy; không restart nếu kiểm
tra cú pháp thất bại. Từ mạng client, chỉ kiểm tra readiness TLS, không truyền
token hoặc gửi operation:

```bash
curl --fail --silent --show-error \
  --cacert /duong/dan/ca-rieng.pem \
  https://gateway.example.internal:9443/healthz
```

Có thể dùng CA công khai nếu chứng chỉ hợp lệ với hostname đã chọn; khi đó
bỏ `--cacert` nếu trust store đã tin cậy. Không tắt TLS verification hoặc dùng
HTTP từ workstation. Nếu chứng chỉ thay đổi, cấp CA/cert chain
đã kiểm tra cho client trước khi đổi proxy để tránh client rơi vào trạng thái
không thể kết nối.

## 6. Bàn giao đúng ba thứ cho mỗi máy Codex

Tạo một token khác cho từng device và thêm một entry mới vào JSON
`BROKER_DEVICE_TOKENS`; sau đó restart broker trong cửa sổ bảo trì ngắn và xác
nhận `/healthz`. Chỉ gửi qua kênh quản trị đã xác thực:

1. `HERMES_A2A_GATEWAY_CLIENT_BROKER_URL` — HTTPS origin, ví dụ
   `https://gateway.example.internal:9443`.
2. CA certificate/chain để client đặt vào
   `HERMES_A2A_GATEWAY_CLIENT_CA_FILE`, nếu CA không có sẵn trong trust store.
3. `HERMES_A2A_GATEWAY_CLIENT_TOKEN` của **chính device đó**.

Token là đúng một dòng; khi bàn giao, người vận hành phía client lưu nó thành
`~/.config/hermes-a2a-gateway/device.token` mode `0600`, đúng tên mà hướng dẫn client
sử dụng. Tệp nguồn trên server, ví dụ
`$HOME/.config/hermes-a2a-gateway/workstation-01.device.token`, chỉ là bản private
để quản trị/revoke và không được mount hoặc đồng bộ sang client.

Không gửi PostgreSQL DSN, Fernet key broker, `HERMES_A2A_TOKEN`, tệp
`broker.env`, token của device khác, hay config Hermes. Người dùng desktop tiếp
tục ở [Cài đặt Codex client](client-codex.vi.md), nơi client tạo payload key và
SQLite state riêng. Token client không thay thế token giữa broker và Hermes.

## Vận hành hằng ngày và xử lý sự cố

Theo dõi service và readiness mà không in environment hoặc request body:

```bash
systemctl --user is-active hermes-a2a-gateway-broker.service
systemctl --user status --no-pager hermes-a2a-gateway-broker.service
journalctl --user -u hermes-a2a-gateway-broker.service --since '1 hour ago' --no-pager
curl --fail --silent http://127.0.0.1:8790/healthz
```

Giữ log proxy không ghi `Authorization`, query sensitive, request body, prompt,
kết quả, Fernet key hoặc DSN. Khi cần chia sẻ log để điều tra, lọc các dữ liệu đó
trước. PostgreSQL backup và thư mục state/key cần backup nhất quán, với key được
quản lý riêng và retention phù hợp. Mặc định beta là TTL một ngày cho prompt mã
hóa và upload, bảy ngày cho event/kết quả; mặc định 100 pending operation/device,
10 MiB/upload và 100 MiB artifact/device. Dữ liệu hết TTL có thể không lấy lại
được.

Khi cập nhật broker: lưu version/digest wheel hiện tại, dừng **chỉ** unit broker
mới, giữ PostgreSQL cùng key file, đổi venv đã kiểm tra, khởi động lại và kiểm tra
`/healthz` cùng một operation đã biết bằng client. Không xóa ledger hoặc "reset"
record `outcome_unknown` để ép chạy lại. Nếu broker bị ngắt sau submit mà không
có remote task ID chính xác, trạng thái phải giữ `outcome_unknown`; không resend
prompt. Với remote task ID đã lưu, broker chỉ đối soát bằng exact GET.

`gateway_cancel` là best effort. Nó yêu cầu hủy ở Hermes khi có handle, nhưng
không chứng minh model đã dừng. Broker shutdown có ngân sách năm giây để drain
HTTP/SSE rồi hủy connections; điều đó không phải hủy upstream. Lên lịch nâng cấp
vào lúc không có job dài, đặc biệt khi tăng/giảm `PEER_TIMEOUT_SECONDS`.

Nếu client không thấy kết quả, trước hết dùng `gateway_get` hoặc `gateway_wait`
với operation handle chính xác. Native queue có thể là `delivery_outcome_unknown`
và host Desktop offline/chưa load không được hứa tự đánh thức; client vẫn có pull
tường minh. Nếu Hermes cần human input, beta báo operation failed cần người vận
hành xem xét; nó không có protocol tiếp tục input qua broker. Đầu vào đính kèm của beta chỉ hỗ trợ UTF-8 `text/plain`;
xem ví dụ upload và bài kiểm tra đầu cuối trong [hướng dẫn client](client-codex.vi.md#6-sử-dụng-hằng-ngày).

## Nguồn và giới hạn đã xác minh

- [Contract client/server v0.7](client-server-contract.md) định nghĩa API,
  operation states, recovery và native delivery; `outcome_unknown` không phải
  kết quả cuối cùng.
- [Triển khai v0.7](deployment.md) là nguồn cài wheel, retention,
  systemd, TLS và giới hạn vận hành của release này.
- [Hermes A2A plugin README](https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/a2a/README.md)
  và [hướng dẫn A2A của Hermes](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/a2a.md)
  là nguồn upstream cho `gateway.platforms.a2a`, cổng `9900`, authentication,
  Agent Card, rate limit và timeout.

## Hội thoại dài và thông báo lỗi trong v0.8

Đọc [hướng dẫn tiếng Anh dành cho AI](ai-operations.md) và
[cài đặt/rollback patch Hermes](hermes-compatibility.md).
Chỉ cài wheel gateway không thay đổi giới hạn hội thoại của Hermes. Kiểm tra
`client-doctor.peer_policy`: cấu hình đúng trả về `sliding_window`, 5 yêu cầu
trong 60 giây. Những peer khác giữ chính sách cũ.

Giữ cùng context để tiếp tục hội thoại. Gửi công việc mới bằng `gateway_submit`
một lần; đọc operation đã gửi bằng `gateway_get` hoặc `gateway_wait`.
Hỏi Helen tiến độ của task chuyên gia là một yêu cầu mới và vẫn tính vào giới hạn.
Khi bị giới hạn, đọc `error.details.retry_at`; không tự gửi lại hoặc đổi context
để né bảo vệ. Kết quả chưa rõ phải được đối chiếu theo operation cũ.

### Nâng cấp Hermes để dùng chính sách mới

Release cung cấp patch, manifest và utility riêng; chỉ hỗ trợ đúng commit Hermes
được ghi trong manifest. Tải và xác minh tất cả asset trước khi chạy. Utility
`hermes_patch.py check --root "$HERMES_CHECKOUT"` phải đạt trước bước apply.
Dừng nhận việc mới, đợi công việc đang chạy hoàn tất và sao lưu nhất quán trước
khi dừng service để áp dụng patch. Giữ nguyên context/session, khóa và database.

Cấu hình riêng phía Hermes `A2A_GATEWAY_TOKEN` khớp credential broker dùng ở
`HERMES_A2A_TOKEN`. Với cài đặt localhost-only chưa có token, cấp một secret mới
cho cặp này, giữ nguyên chế độ truy cập cũ của những caller local khác. Nếu đã có
xác thực Hermes, credential vẫn phải vượt xác thực và trust policy hiện có.
Không đưa secret vào tài liệu, lệnh mẫu chứa giá trị thật hoặc lời nhắc cho AI.

Khởi động Hermes trước, rồi broker và client. Kiểm tra doctor và mapping session
trước khi chạy smoke test. Đây là giới hạn tốc độ, không phải cơ chế phát hiện
mọi vòng lặp giữa các agent; không vô hiệu hóa các phê duyệt công cụ của Hermes.
