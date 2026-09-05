# Thiết lập Codex + Hermes hai chiều (macOS/Linux)

> Hợp đồng recovery/delivery mới: [job bền vững](durable-jobs.vi.md). Hết lượt wait không phải thất bại; không tự đưa kết quả vào Desktop conversation.

Hướng dẫn này dành cho một người dùng cài `codex-a2a-gateway` `v0.3.0` trên **một máy local** rồi kiểm tra cả hai chiều. Release đã gồm plugin durable, recovery timeout, tiếp tục `INPUT_REQUIRED` và extension model/reasoning tùy chọn. Mỗi chiều có một vai trò riêng:

```text
Codex task --MCP stdio--> gateway serve --> Hermes A2A, 127.0.0.1:9900
Hermes CLI --A2A tools--> gateway HTTP --> Codex App Server, 127.0.0.1:9910
```

- `9900` là A2A inbound của **Hermes**; gateway MCP của Codex gọi vào đây.
- `9910` là A2A inbound của **Codex gateway**; Hermes gọi vào đây qua peer `codex`.
- MCP là một process stdio, **không phải** một HTTP port.

Đây là topology local single-user. Giữ cả hai endpoint ở `127.0.0.1`; không mở chúng ra Internet. Gateway lưu mapping, trạng thái và kết quả tối thiểu trong SQLite, vì vậy chỉ chạy một bộ process writer trên cùng state file.

## 1. Điều kiện và biến dùng chung

Cần CPython 3.11, Codex CLI đã đăng nhập, Hermes Agent, `curl`, và hai cổng loopback `9900`/`9910` chưa bị process khác chiếm. Mở hai terminal riêng: một cho Hermes, một cho Codex gateway. Nếu `codex` không được tìm thấy, hoàn tất cài đặt/đăng nhập theo [Codex CLI guide](https://learn.chatgpt.com/docs/codex/cli) trước.

```bash
python3.11 --version
codex --version
hermes --version
curl --version

install_dir="$HOME/.local/share/codex-a2a-gateway"
gateway_venv="$install_dir/venv"
gateway_bin="$gateway_venv/bin/codex-a2a-gateway"
```

## 2. Tải và xác minh release rồi cài

Chỉ cài sau khi SHA-256 của **cả wheel và source archive** khớp `SHA256SUMS` tải từ cùng release. Lệnh dưới đây chọn `shasum` trên macOS và `sha256sum` trên Linux:

```bash
release_dir="$(mktemp -d)"
release_url="https://github.com/phamviet86/codex-a2a-gateway/releases/download/v0.3.0"
wheel="codex_a2a_gateway-0.3.0-py3-none-any.whl"
sdist="codex_a2a_gateway-0.3.0.tar.gz"

curl --fail --location --output "$release_dir/$wheel" "$release_url/$wheel"
curl --fail --location --output "$release_dir/$sdist" "$release_url/$sdist"
curl --fail --location --output "$release_dir/SHA256SUMS" "$release_url/SHA256SUMS"
if command -v shasum >/dev/null 2>&1; then
  (cd "$release_dir" && shasum -a 256 -c SHA256SUMS)
else
  (cd "$release_dir" && sha256sum --check SHA256SUMS)
fi

python3.11 -m venv "$gateway_venv"
"$gateway_venv/bin/python" -m pip install "$release_dir/$wheel"
"$gateway_bin" --version
```

Nếu kiểm tra checksum thất bại, không cài file đó. Xóa thư mục tạm và tải lại từ trang release; không dùng digest từ nguồn khác.

## 3. Bật Hermes inbound A2A (`:9900`)

`a2a` tool cho CLI và A2A **inbound platform** của Hermes là hai cấu hình khác nhau. Chạy setup nếu đây là lần đầu, sau đó bật rõ platform inbound và port:

```bash
hermes gateway setup
hermes config set gateway.platforms.a2a.enabled true
hermes config set gateway.platforms.a2a.extra.port 9900
hermes gateway run
```

Giữ `hermes gateway run` ở foreground trong terminal này. Trong terminal khác, endpoint phải trả Agent Card:

```bash
curl --fail http://127.0.0.1:9900/.well-known/agent-card.json
```

Nếu phiên bản Hermes của bạn dùng tên cấu hình khác hoặc `config set` báo lỗi, dừng tại đây và đối chiếu [Hermes A2A guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a); đừng chỉ bật tool CLI để thay thế inbound platform.

## 4. Đăng ký MCP cho Codex (Codex → Hermes)

Đầu tiên kiểm tra `doctor`, rồi đăng ký executable tuyệt đối từ virtualenv. Codex khởi chạy `serve` qua stdio; không tự chạy `serve` trong terminal.

```bash
"$gateway_bin" doctor
codex mcp add codex-a2a-gateway -- "$gateway_bin" serve
codex mcp get codex-a2a-gateway
```

Sau đó khởi động lại Codex Desktop hoặc mở **task Codex mới** để nạp MCP. Trong task mới, dùng `hermes_status` trước khi dùng `hermes_chat`. Giữ cùng `conversation_key` cho một hội thoại; khi task còn active hoặc sau timeout không rõ kết quả, dùng `hermes_task_get`/`hermes_task_wait`, không gửi lại yêu cầu có side effect.

## 5. Khởi chạy Codex inbound A2A (`:9910`)

Chọn thư mục mà Codex được phép thao tác. Không dùng một đường dẫn ví dụ chưa tồn tại. Cổng 9910 gateway này tách hoàn toàn với port 9900 của Hermes.

Trước tiên resolve binary Codex. Đoạn dưới ưu tiên `CODEX_CLI_BIN` đã có, rồi `codex` trong `PATH`, sau đó thử các vị trí bundle thông dụng. Không có path nào là bắt buộc; nếu tất cả đều không có, nó dừng và yêu cầu bạn đặt đường dẫn tuyệt đối của máy mình.

```bash
codex_bin=""
for candidate in \
  "${CODEX_CLI_BIN:-}" \
  "$(command -v codex 2>/dev/null || true)" \
  "/Applications/ChatGPT.app/Contents/Resources/codex" \
  "$HOME/Applications/ChatGPT.app/Contents/Resources/codex" \
  "/Applications/Codex.app/Contents/Resources/codex" \
  "$HOME/Applications/Codex.app/Contents/Resources/codex" \
  "/usr/lib/chatgpt/resources/codex"; do
  if test -n "$candidate" && test -x "$candidate"; then
    codex_bin="$candidate"
    break
  fi
done
test -n "$codex_bin" || { printf '%s\n' 'Không tìm thấy Codex CLI; đặt CODEX_CLI_BIN thành đường dẫn tuyệt đối tới executable Codex.'; false; }
export CODEX_CLI_BIN="$codex_bin"

workspace_root="/duong/dan/tuyet-doi/toi/workspace"  # thay bằng workspace thật
test -d "$workspace_root"
CODEX_WORKSPACE_ROOT="$workspace_root" CODEX_CLI_BIN="$codex_bin" \
  "$gateway_bin" gateway
```

Giữ lệnh `gateway` ở foreground trong terminal này. Bản beta hiện chưa cung cấp template `systemd` hoặc `launchd`; đừng tự suy ra một service unit từ hướng dẫn này.

## 6. Khai báo peer Codex trong Hermes (Hermes → Codex)

Trong một terminal khác, xác nhận gateway và thêm peer. Bật A2A tools **chỉ cho CLI**; không bật platform inbound `a2a` trừ khi bạn chủ động thiết kế relay/agent chaining.

```bash
curl --fail http://127.0.0.1:9910/health
curl --fail http://127.0.0.1:9910/.well-known/agent-card.json

hermes config set a2a_agents.codex.url http://127.0.0.1:9910
hermes config set a2a_agents.codex.timeout 300
hermes tools enable a2a --platform cli
```

### Plugin durable

`a2a_call` built-in của Hermes là call đồng bộ. Wheel `v0.3.0` đã có plugin self-contained; cài plugin và chỉ bật toolset riêng `codex_a2a` cho CLI:

```bash
gateway_bin="$gateway_venv/bin/codex-a2a-gateway"
test -x "$gateway_bin"
"$gateway_bin" install-hermes-plugin
hermes plugins enable codex-a2a-gateway
hermes tools enable codex_a2a --platform cli
hermes config set plugins.entries.codex-a2a-gateway.settings.endpoint http://127.0.0.1:9910
hermes config set plugins.entries.codex-a2a-gateway.settings.timeout 30
```

Plugin `codex_a2a_call` luôn submit với `returnImmediately: true`, chỉ lưu metadata handle (không lưu result/artifact Codex) và có `codex_a2a_get`, `codex_a2a_wait`, `codex_a2a_list`, `codex_a2a_cancel`. Timeout là `outcome_unknown`, không gửi lại request; chỉ recovery khi `requestMessageId` đã lưu khớp chính xác đúng một candidate chưa gắn handle từ `ListTasks(contextId)`. Endpoint plugin luôn phải loopback.

Plugin đọc `plugins.entries.codex-a2a-gateway.settings.endpoint` và `.timeout`, không đọc `a2a_agents`; đặt hai key plugin này nếu gateway chạy ở port loopback khác `9910`.

Installer ghi vào `$HERMES_HOME/plugins/codex-a2a-gateway`, mặc định là `~/.hermes/plugins/codex-a2a-gateway`. Khi `TASK_STATE_INPUT_REQUIRED`, gọi `codex_a2a_call` với local `task_id` trả về và câu trả lời mới; plugin gửi `message.taskId` cho cùng remote task và từ chối đổi model/reasoning. Không chạy song song nhiều operation trên cùng handle.

Các lệnh cấu hình Hermes có thể cảnh báo `a2a_agents` là khóa plugin. Xác nhận lại `~/.hermes/config.yaml` chứa peer `codex` với URL loopback và timeout `300`. Một session/CLI Hermes mới có thể cần được mở lại để thấy tool vừa bật.

## 7. Readiness và smoke test opt-in

Readiness chỉ xác minh listener/card; smoke gửi một task thật, dù prompt vô hại. Chạy các lệnh sau sau khi cả hai process foreground đang hoạt động:

```bash
"$gateway_bin" doctor
curl --fail http://127.0.0.1:9900/.well-known/agent-card.json
curl --fail http://127.0.0.1:9910/health
curl --fail http://127.0.0.1:9910/.well-known/agent-card.json
```

Nếu bạn đồng ý tạo một task vô hại, kiểm tra Codex → Hermes:

```bash
"$gateway_bin" smoke \
  --conversation-key readiness-codex-to-hermes \
  'Hãy chỉ trả lời đúng chuỗi HERMES_A2A_OK, không thêm nội dung khác.'
```

Để kiểm tra A2A → Codex trực tiếp, gửi một request với ID mới. Kết quả thành công là task có artifact/response `CODEX_A2A_OK` (task có thể mất một lúc để hoàn tất):

```bash
run_id="$(date +%s)"
curl --fail-with-body --silent --show-error http://127.0.0.1:9910/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":\"readiness-hermes-to-codex-$run_id\",\"method\":\"SendMessage\",\"params\":{\"message\":{\"messageId\":\"readiness-message-hermes-to-codex-$run_id\",\"role\":\"ROLE_USER\",\"parts\":[{\"text\":\"Reply with exactly CODEX_A2A_OK\"}]}}}"
```

Để gọi qua Hermes thay vì `curl`, mở một session Hermes CLI mới và dùng peer `codex`/A2A tool vừa cấu hình. Flow A2A đầy đủ (submit sớm, lưu `taskId`/`contextId`, poll, tiếp tục `INPUT_REQUIRED`, cancel) có trong [deployment guide](deployment.md#5-generic-a2a-client-lifecycle).

### Extension model/reasoning (chỉ Hermes/A2A → Codex)

Agent Card chỉ quảng bá extension opt-in này khi gateway dùng App Server. Plugin fetch card loopback trước và không gửi preference request nếu URI chính xác không có. Client phải đưa cùng URI vào HTTP header `A2A-Extensions` và `message.extensions`, rồi đặt `model`, `reasoning_effort`, `require_exact` tùy chọn tại `message.metadata.executionPreferences`. Gateway gọi `model/list`, áp policy receiver và lưu requested/effective decision vào task metadata; `turn/start` nhận `model`/`effort`. Giá trị exact không hỗ trợ bị reject, còn non-exact có thể fallback rõ ràng. CLI backend từ chối extension; Codex → Hermes không hỗ trợ các field này. Xem [contract versioned](execution-preferences-extension-v1.md).

## 8. Lỗi thường gặp

| Triệu chứng | Kiểm tra và xử lý an toàn |
|---|---|
| `doctor` không tới được Hermes | Kiểm tra terminal `hermes gateway run` còn chạy, platform inbound A2A đã bật và Agent Card ở `127.0.0.1:9900` trả được. Không đổi endpoint outbound sang URL remote tùy ý. |
| `9910` không có `/health` hoặc Agent Card | Kiểm tra terminal `gateway` còn chạy, workspace tồn tại, `CODEX_CLI_BIN` trỏ tới executable thật. Nếu port bị chiếm, chọn một `CODEX_A2A_PORT` loopback khác và cập nhật peer URL cùng lúc. |
| Codex không thấy MCP tools | Chạy `codex mcp get codex-a2a-gateway`, kiểm tra command là đường dẫn tuyệt đối `$gateway_bin`, sau đó mở task Codex mới. Không chạy `serve` thủ công. |
| Hermes thấy peer nhưng không gọi được Codex | Kiểm tra `a2a_agents.codex.url` là `http://127.0.0.1:9910`, `hermes tools enable a2a --platform cli` đã chạy, rồi mở session Hermes CLI mới. |
| Task timeout hoặc kết quả chưa rõ | Lưu ID và truy vấn trạng thái thay vì gửi lại yêu cầu thay đổi dữ liệu. Cancel chỉ là best-effort; response cancel không chứng minh Codex/Hermes đã dừng computation. |

Xem [deployment guide](deployment.md) để upgrade, rollback, gỡ cài đặt và các giới hạn vận hành; xem [roadmap tiếng Việt](roadmap.vi.md) để biết phần nào chưa có trong release hiện tại.
