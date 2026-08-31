# Tham chiếu Hermes A2A v1.0

> **Snapshot reference:** phần phân tích chi tiết dưới đây ghi lại source Hermes `0.20.5` tại thời điểm thiết kế v0.1. Hướng dẫn vận hành hiện tại dùng `hermes gateway setup`, `hermes gateway run` và `hermes tools enable a2a --platform <platform>` theo [Hermes A2A guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a). Kiểm thử release v0.2 được thực hiện riêng với Hermes `0.20.6`; xem [testing report v0.2](testing-report-v0.2.md).

## Phạm vi và thứ tự ưu tiên nguồn

Tài liệu này mô tả **những gì Hermes Agent thực sự hỗ trợ**, không phải toàn bộ chuẩn A2A. Cơ sở kiểm chứng:

1. Source cài local tại `<HERMES_INSTALL>`, phiên bản `0.20.5` trong `pyproject.toml`, commit `d736f5d53f1d33fabad5a17cb070eb138b618fb8` tại thời điểm khảo sát 2026-08-25.
2. [Tài liệu A2A chính thức của Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a) và [repo NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/tree/main/plugins/platforms/a2a).
3. [Đặc tả A2A v1.0](https://a2a-protocol.org/latest/specification/) để đối chiếu tên operation và wire format.

Khi tài liệu và code khác nhau, phần dưới ưu tiên hành vi của source local `0.20.5`. Những nhận định gắn nhãn **Local 0.20.5** đã được xác minh trực tiếp từ `<HERMES_INSTALL>/plugins/platforms/a2a/{adapter.py,protocol.py,security.py,tools.py,plugin.yaml}`. Đây là mô tả upstream tại snapshot đó, không phải guarantee do bridge cung cấp.

## Tổng quan kiến trúc

Plugin `a2a-platform` có hai chiều độc lập:

- **Inbound:** một HTTP server dùng Python stdlib (`ThreadingHTTPServer`) công bố Hermes như một A2A agent. Task cho agent đang chạy được đưa qua `MessageEvent` vào live gateway session. Task cho named profile khác được chuyển qua CLI `hermes chat` với `HERMES_HOME` của profile đó.
- **Outbound:** năm Hermes model tools dùng `urllib` để khám phá và gọi A2A peer. Chúng không phải API bắt buộc cho bridge Codex; bridge có thể nói JSON-RPC trực tiếp với inbound server.

Không có dependency `a2a-sdk`; binding duy nhất được quảng bá là `JSONRPC`, protocol version `1.0`.

## HTTP endpoints và routing

Với agent mặc định, base URL thường là `http://127.0.0.1:9900/`. Với served agent có path `research`, base URL là `http://127.0.0.1:9900/research/`. Mọi endpoint trong bảng áp dụng sau base URL tương ứng.

| HTTP | Path | Auth | Hành vi Local 0.20.5 |
|---|---|---|---|
| GET | `/.well-known/agent-card.json` | Không | Agent Card canonical v1.0. |
| GET | `/.well-known/agent.json` | Không | Alias cũ để tương thích client trước v1.0. |
| GET | `/` hoặc `/health` | Không bắt buộc | Trả `status: ok` và tên agent. `served_agents` chỉ hiện khi localhost-only hoặc request có bearer hợp lệ. |
| GET | `/metrics` | Không | Snapshot JSON các counter trong tiến trình; không phải Prometheus text format. |
| POST | `/` hoặc URL interface/profile prefix | Có đối với remote mode | JSON-RPC 2.0 cho tất cả operation. |

Lưu ý:

- Agent Card là public theo chủ đích. `/metrics` cũng không kiểm tra auth trong source hiện tại.
- `A2A_PUBLIC_URL` được ưu tiên khi tạo URL trên card; sau đó là `X-Forwarded-Host`/`Host` và `X-Forwarded-Proto`; cuối cùng mới dùng bind host/port.
- POST giới hạn body 1 MiB. Header `A2A-Version` là tùy chọn; nếu có thì chỉ nhận `1.0` hoặc `1.0.0`.
- Canonical endpoint nên lấy từ `supportedInterfaces[].url`, không tự nối từ URL discovery.

## Agent Card thực tế

Card do `protocol.build_agent_card()` tạo có các trường chính:

- `name`, `description`, `version: "1.0.0"`;
- `provider.organization` (mặc định `Hermes Agent`) và `provider.url`;
- `supportedInterfaces`: một interface có `url`, `protocolBinding: "JSONRPC"`, `protocolVersion: "1.0"`, và `tenant` nếu agent được route theo tenant;
- `capabilities.streaming`, `pushNotifications`, `stateTransitionHistory: false`, `extendedAgentCard: false`;
- `defaultInputModes` và `defaultOutputModes` chỉ gồm `text/plain`;
- `skills` sinh động từ live tool registry; có thể giới hạn bằng `advertised_toolsets` hoặc `A2A_ADVERTISED_TOOLSETS`;
- `securitySchemes`/`security` kiểu HTTP bearer khi server không ở chế độ localhost-only;
- top-level `url` vẫn được giữ để tương thích client cũ.

**Local 0.20.5:** root/live agent quảng bá `streaming: true`; named profile được forward bằng subprocess quảng bá `streaming: false`. Push notification luôn được quảng bá là có hỗ trợ. Không có extended Agent Card operation.

## Wire format

- JSON-RPC envelope: `{"jsonrpc":"2.0","id":...,"method":...,"params":...}`.
- Canonical A2A v1.0 dùng method PascalCase. Hermes vẫn nhận alias path-style cũ.
- Role: `ROLE_USER`, `ROLE_AGENT`.
- Part phân biệt bằng sự hiện diện của member, không có `kind`: text (`text`, `mediaType`), file (`url` hoặc `raw`, tùy chọn `filename`, `mediaType`), data (`data`, `mediaType`).
- Inbound file URL/data được render thành text cho agent. Raw base64 chỉ được mô tả, không decode. Outbound Hermes hiện chỉ tạo text Part.
- `contextId` canonical nằm trong `params.message`; Hermes vẫn nhận top-level `params.contextId` kiểu cũ.
- `SendMessage` trả `result: {task: Task}` hoặc `{message: Message}`. Alias `message/send` trả bare Task theo đường tương thích cũ.
- Task của Local 0.20.5 không serialize `createdAt`/`lastModified`, vì code ưu tiên tương thích ProtoJSON chặt của SDK chính thức. `TaskStatus.timestamp` có UTC ISO-8601 tới mili giây.

Ví dụ request canonical tối thiểu:

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-1",
      "role": "ROLE_USER",
      "contextId": "ctx-codex-1",
      "parts": [{"text": "Hãy phân tích yêu cầu này", "mediaType": "text/plain"}
    }
  }
}
```

## JSON-RPC methods được hỗ trợ

| Canonical v1.0 | Alias được nhận | Input chính | Result/hành vi thực tế |
|---|---|---|---|
| `SendMessage` | `message/send` | `message`; tùy chọn `tenant`, `configuration.taskPushNotificationConfig` | Chờ agent kết thúc hoặc timeout. Canonical bọc Task trong `result.task`; alias trả bare Task. |
| `SendStreamingMessage` | `message/stream` | Như `SendMessage` | HTTP SSE: Task submitted, status working, rồi terminal status/artifact. |
| `GetTask` | `tasks/get` | `id` hoặc `taskId`; tùy chọn `historyLength` | Task hiện tại. Sai scope cũng trả not found. `historyLength` được parse nhưng store không tạo Task history. |
| `ListTasks` | `tasks/list` | `contextId`, `status`/`state`, `pageSize`, `pageToken`, `includeArtifacts`, `historyLength` | Newest-first; `pageSize` 1–100, mặc định 50; `pageToken` thực chất là offset số; artifacts mặc định bị bỏ; trả `tasks`, `nextPageToken`, `pageSize`, `totalSize`. |
| `CancelTask` | `tasks/cancel` | `id` hoặc `taskId` | Đánh dấu canceled, resolve waiter và reset anti-loop counter của context. Không dừng computation Hermes đang chạy. |
| `SubscribeToTask` | `tasks/subscribe` | `id` hoặc `taskId` | SSE chờ terminal của task đã biết; keepalive mỗi 5 giây. |
| `CreateTaskPushNotificationConfig` | `tasks/pushNotificationConfig/create`, `tasks/pushNotificationConfig/set`, `tasks/pushNotification/set` | `taskId`, `pushNotificationConfig.url` | Lưu một callback URL, trả `configId`, `taskId`, `createdAt`, config. |
| `GetTaskPushNotificationConfig` | `tasks/pushNotificationConfig/get` | `taskId`, tùy chọn `id`/`configId` | Trả config duy nhất hoặc not found. |
| `ListTaskPushNotificationConfigs` | `tasks/pushNotificationConfig/list` | `taskId` | Trả `configs` (0 hoặc 1) và `nextPageToken: ""`. |
| `DeleteTaskPushNotificationConfig` | `tasks/pushNotificationConfig/delete` | `taskId`, tùy chọn `id`/`configId` | Trả `{deleted: true}` hoặc not found. |

Không hỗ trợ `GetExtendedAgentCard`, gRPC hay HTTP+JSON/REST binding.

## Error codes và HTTP status

| Code | Ý nghĩa |
|---:|---|
| `-32700` | Parse error; cũng dùng cho payload quá lớn. |
| `-32602` | Invalid params hoặc `A2A-Version` không hỗ trợ. |
| `-32601` | Method không tồn tại. |
| `-32001` | Task/config không tìm thấy, gồm cả truy cập sai profile/tenant scope. |
| `-32002` | Task không thể cancel vì đã terminal. |
| `-32003` | Push notification không hỗ trợ; constant có trong code nhưng đường hiện tại không phát ra. |
| `-32050` | Unauthorized. |
| `-32051` | Rate limited. |
| `-32052` | Authenticated peer không nằm trong trust gate. |

Hermes thường trả JSON-RPC error với HTTP 200 cho lỗi method/params/task. Auth dùng HTTP 401, trust dùng 403, rate limit dùng 429, payload quá lớn dùng 413.

## Task lifecycle

Các state được định nghĩa:

| State | Có phát sinh thực tế? | Ý nghĩa |
|---|---|---|
| `TASK_STATE_SUBMITTED` | Có | Task mới; thấy trong stream/store. |
| `TASK_STATE_WORKING` | Có | Đã dispatch tới live gateway. |
| `TASK_STATE_INPUT_REQUIRED` | Có | Reply bắt đầu bằng `[INPUT_REQUIRED]`; marker bị bỏ và câu hỏi nằm trong `status.message`. |
| `TASK_STATE_AUTH_REQUIRED` | Chưa có đường phát sinh | Constant tồn tại nhưng adapter không chuyển reply thành state này. |
| `TASK_STATE_COMPLETED` | Có | Reply hoàn tất; text nằm ở artifact và status message. |
| `TASK_STATE_FAILED` | Có | Dispatch/reply timeout, gateway/profile lỗi, orphan, hoặc client stream rời trong lúc chờ. |
| `TASK_STATE_CANCELED` | Có | Cancel ở protocol level; computation nền có thể vẫn chạy. |
| `TASK_STATE_REJECTED` | Có | Empty task hoặc vượt anti-loop turn cap. |

Terminal set nội bộ chỉ gồm completed, failed, canceled, rejected. `INPUT_REQUIRED` không được coi terminal trong `TERMINAL_STATES`, dù request hiện tại đã trả về; lượt tiếp theo tạo **task ID mới** nhưng dùng lại `contextId`.

Task store:

- nằm trong RAM của adapter, mất khi gateway restart;
- giữ tối đa 500 task terminal gần nhất;
- watchdog kiểm tra mỗi 60 giây và fail task non-terminal cũ hơn 300 giây;
- task được scope theo served-agent slug và tenant; cross-tenant lookup bị che thành not-found;
- không có idempotency/dedup theo `messageId` hay JSON-RPC id.

## `contextId`, multi-turn và profile routing

### Agent mặc định

- Root path `/` luôn trỏ vào live gateway session của **active profile** tại lúc adapter khởi tạo.
- Nếu request không có `contextId`, Hermes sinh `ctx-<16 hex>`.
- Với live agent, `contextId` được dùng làm `chat_id`, nên các lượt cùng ID vào cùng gateway conversation context.

### Named served agent/profile

**Local 0.20.5** có hỗ trợ mà `DESIGN.md` cũ vẫn ghi là out-of-scope. Cấu hình được đọc ưu tiên từ `gateway.platforms.a2a.extra.agents` (hoặc `extra.served_agents`), với fallback `a2a_served_agents` hay `a2a.served_agents` top-level.

Ví dụ cấu trúc tham chiếu, không phải thay đổi cấu hình trong task này:

```yaml
gateway:
  platforms:
    a2a:
      enabled: true
      extra:
        agents:
          research:
            path: research
            profile: research
            tenant: research-team
            name: Research Agent
            description: Research specialist
            advertised_toolsets: [web, research]
            timeout: 300
```

Hai cách chọn agent:

1. **Path:** discover `GET /research/.well-known/agent-card.json`, rồi POST vào URL interface `/research/`.
2. **Tenant:** POST root với `params.tenant: "research-team"`. Card của named agent quảng bá tenant; client phải echo lại. Nếu path và tenant mâu thuẫn, Hermes trả invalid params.

Path `health`, `metrics`, `.well-known` bị dành riêng; duplicate tenant bị bỏ qua. Root/default agent không có tenant.

Named profile không phải active/default được gọi qua subprocess `hermes chat -q ... -Q --source a2a` với `HERMES_HOME` của profile. Lượt đầu tạo session, sau đó adapter tìm session ID trong `state.db`, đặt title xác định `a2a-<slug>-<safe-context>` và dùng `--resume <session-id>` cho các lượt sau. Lock theo `(profile, slug, context)` giữ thứ tự.

Hệ quả thiết kế:

- Để gọi profile `default` một cách chắc chắn, chạy A2A gateway với `default` là active profile và dùng root card/path.
- Một named entry có `profile: default` bị xem là `local`; nếu gateway đang chạy profile khác, code vẫn đi vào live gateway hiện tại. Không nên dùng alias này để “nhảy” về default từ một gateway active-profile khác.
- Mapping session subprocess nằm trong RAM nhưng có thể khôi phục qua deterministic title trong profile `state.db`.
- Conversation JSONL chỉ đặt tên theo `contextId`, không namespace theo peer/profile/tenant; bridge phải tạo context ID globally unique để tránh trộn log.
- Forwarded named profile coi mọi CLI exit code 0 là `COMPLETED`; đường này không parse marker `[INPUT_REQUIRED]`. Vì vậy input-required mapping đáng tin cậy chỉ có ở live/local agent path trong source hiện tại.

## Streaming và tiến trình dài

`SendStreamingMessage` trả `Content-Type: text/event-stream`. Mỗi `data:` frame là một JSON-RPC response đầy đủ, có cùng request id và `result` là một `StreamResponse` phân biệt bằng member:

- `task`: snapshot ban đầu ở submitted;
- `statusUpdate`: working hoặc trạng thái cuối;
- `artifactUpdate`: text artifact khi completed.

Stream gửi comment `: keepalive` mỗi 5 giây lúc chờ và `: done` trước khi đóng. Chính việc đóng stream biểu thị kết thúc; không có trường `final`.

**Giới hạn Local 0.20.5:** đây là lifecycle streaming, chưa phải token/chunk streaming. `adapter.send()` bỏ qua preview/progress không có `metadata.notify`; kết quả nội dung chỉ được phát khi Hermes có final reply. `SubscribeToTask` cũng chủ yếu đợi terminal, không replay chuỗi state trước đó.

Không có operation “submit nhưng trả task ID ngay” cho non-streaming. Để hỗ trợ task dài, client có ba lựa chọn:

- giữ `SendStreamingMessage` mở, lấy task ID từ event đầu;
- gửi push config inline trong `SendMessage`/`SendStreamingMessage`;
- bridge tự chạy lời gọi A2A trong background và trả bridge job ID, sau đó poll/subscribe.

Timeout reply mặc định 300 giây. Named forwarded profile dùng timeout riêng của served agent nếu cấu hình. Timeout không bảo đảm computation đã dừng.

## Push notification

- Config có thể gửi inline tại `params.configuration.taskPushNotificationConfig`, hoặc CRUD bằng methods riêng.
- Local 0.20.5 chỉ giữ **một** URL cho mỗi task, dù spec cho phép nhiều config.
- Trên đường finalize reply bình thường (kể cả `INPUT_REQUIRED`) hoặc forwarded-profile reply, Hermes POST một `statusUpdate` StreamResponse; reply bị cắt ở 2.000 ký tự.
- Callback là one-shot: URL bị pop trước khi gửi, nên thất bại không tự retry và config không còn sau lần phát.
- Header `X-A2A-Signature` là HMAC-SHA256 hex. Secret lấy từ `A2A_PUSH_SECRET`, fallback shared `A2A_BEARER_TOKEN`; localhost không secret thì push không ký.
- Hàm ký canonicalize bằng `json.dumps(..., sort_keys=True)`, trong khi HTTP body được serialize bình thường. Receiver nên parse JSON rồi reserialize sorted keys trước khi verify, không HMAC raw body.
- Chỉ cho `http`/`https`; literal loopback/private/link-local/reserved bị chặn, ngoại trừ loopback khi server localhost-only. Hostname không được resolve để kiểm tra IP đích, nên DNS rebinding/private-DNS vẫn là rủi ro cần chặn thêm ở bridge/firewall.
- Các trường auth nâng cao của push config không được giữ; chỉ URL được dùng.
- Local 0.20.5 không gọi push sender trên mọi transition: explicit cancel, watchdog orphan, empty/anti-loop rejection và một số failure sớm không phát callback. Push phải được coi là best-effort và luôn cần poll/reconcile.

## Outbound Hermes tools

`plugin.yaml` khai báo năm tool trong toolset `a2a`:

Named peers được đọc từ `config.yaml` → `a2a_agents.<name>` với các field thực tế `url`, `auth` (bearer), `timeout`, `capabilities`, `tenant`; cũng có thể truyền direct HTTP(S) URL cho `a2a_call`. Client thử card canonical rồi legacy, ưu tiên JSONRPC interface URL trên card và echo `interface.tenant`; nếu discovery lỗi, nó vẫn thử POST vào configured base URL. Mỗi send tự tạo context nếu caller không truyền và dùng canonical `SendMessage` với header `A2A-Version: 1.0`.

| Tool | Hành vi Local 0.20.5 | Giới hạn liên quan bridge |
|---|---|---|
| `a2a_discover(url)` | Thử canonical card, fallback legacy path; tóm tắt card. | Không nhận auth argument, nên card cần public. |
| `a2a_call(agent,message,context_id?)` | Discover best-effort, chọn JSONRPC interface/tenant, gọi canonical `SendMessage` đồng bộ. | Không stream, poll, cancel hay retry. Direct URL không mang auth; named peer mới đọc bearer config. |
| `a2a_list()` | Liệt kê peers, persisted contexts và in-process metrics. | Không phải `ListTasks`. |
| `a2a_history(context_id)` | Đọc JSONL conversation, tối đa 200 messages. | Không trả A2A Task history chuẩn. |
| `a2a_orchestrate(...)` | Fan-out tối đa 6 workers, mode all/first/best. | `best` chỉ chọn reply dài nhất; cancel future không dừng HTTP call đã chạy; shared context qua nhiều peer có thể trộn persistence. |

Bridge Codex nên gọi A2A wire protocol trực tiếp thay vì gọi vòng qua các Hermes outbound model tools.

## Health, metrics, audit và persistence

`GET /metrics` trả:

- `uptime_seconds`, `inbound_total`, `outbound_total`;
- `streams_started`, `push_sent`, `push_failed`;
- `tasks_completed`, `tasks_failed`;
- `anti_loop_triggers`, `rate_limit_triggers`, `avg_latency_ms`.

Metrics là singleton trong tiến trình, dùng chung inbound/outbound, không persist và không gắn profile/tenant.

Audit ghi append-only best-effort vào `~/.hermes/a2a_audit.jsonl`: timestamp epoch, direction, peer, task ID, và tối đa 500 ký tự summary. Source không có rotation, retention, file locking hay mã hóa.

Conversation ghi `~/.hermes/a2a_conversations/<safe-context>.jsonl`, gồm timestamp, role, text, task ID. Nó sống ngoài context compaction và tồn tại qua restart. Tuy nhiên đây không phải TaskStore: sau restart có transcript nhưng `GetTask` không còn task.

**Local 0.20.5:** inbound audit summary và conversation log ghi text đã extract **trước** khi prompt-injection filter/framing; outbound text được redaction trước khi persist/audit. Vì vậy không coi audit/conversation files là nơi an toàn để gửi secret. Hàm tạo filename bỏ mọi ký tự ngoài chữ/số/`-_`, nên hai context ID khác nhau có thể va chạm sau sanitize nếu client không dùng ID sạch và unique.

## Auth, trust, rate limit và chống loop

- Không có token: bind bị ép về loopback, mọi POST từ socket loopback nhận identity `ip:<addr>`.
- Có `A2A_PEER_TOKENS`: token map tới tên peer xác thực; so sánh constant-time. Đây là lựa chọn tốt nhất để audit/rate limit có identity ổn định.
- Shared `A2A_BEARER_TOKEN`: identity là `ip:<addr>`.
- Remote bind chỉ xảy ra khi vừa có token vừa đặt `A2A_HOST` ngoài loopback.
- `A2A_TRUSTED_PEERS`/`a2a.trusted_peers` là allow-list bổ sung. `A2A_ALLOW_ALL_USERS=true` bypass trust list. Khi không có trust list, mọi identity đã auth đều được phép.
- Sliding-window rate limit mặc định 60 POST/phút/identity, áp dụng cả send, query, cancel và push CRUD.
- Mọi inbound text được lọc một số prompt-injection marker và luôn thêm privacy prefix coi peer là untrusted. Bearer/trusted-peer không biến peer thành operator-equivalent.
- Outbound redaction lọc một số API key, JWT, bearer token **và email address**, nên có thể làm biến dạng output hợp lệ.
- Anti-loop đếm mỗi inbound send theo `contextId`: mặc định 5, cấu hình tối đa 20; context idle hơn một giờ mới được prune. `CancelTask` reset counter. Bắt đầu context mới cũng reset thực tế.

## Biến cấu hình được source Local 0.20.5 đọc

| Biến | Mặc định/hành vi |
|---|---|
| `A2A_PEER_TOKENS` | Rỗng; chuỗi `name:token,...` cho inbound peer identity. |
| `A2A_BEARER_TOKEN` | Rỗng; shared inbound bearer. |
| `A2A_HOST` | `127.0.0.1`; chỉ được mở rộng khi đã có token. |
| `A2A_PORT` | `9900`. |
| `A2A_AGENT_NAME` | `hermes-<hostname>`. |
| `A2A_AGENT_DESCRIPTION` | Mô tả general-purpose mặc định cho root card. |
| `A2A_PUBLIC_URL` | Rỗng; override URL được quảng bá. |
| `A2A_PROVIDER_ORG` | `Hermes Agent`. |
| `A2A_PROVIDER_URL` | Interface URL hiện tại. |
| `A2A_ADVERTISED_TOOLSETS` | Rỗng nghĩa là dùng mọi registered toolset; extra config có thể override/restrict. |
| `A2A_TRUSTED_PEERS` | Rỗng; optional authenticated-identity allow-list. |
| `A2A_ALLOW_ALL_USERS` | False; bypass trust list khi bật. |
| `A2A_RATE_LIMIT` | 60 POST/phút/identity, tối thiểu 1. |
| `A2A_MAX_PINGPONG_TURNS` | 5, clamp 1–20. |
| `A2A_REPLY_TIMEOUT` | 300 giây, tối thiểu 1 giây. |
| `A2A_PUSH_SECRET` | Fallback shared bearer; rỗng ở localhost-only thì push unsigned. |

`HERMES_A2A_PEER` là biến nội bộ adapter đặt cho subprocess named-profile, không phải public operator setting. `A2A_HOME_CHANNEL` xuất hiện trong plugin manifest cho cron/notification delivery nhưng không được các file protocol/adapter/security/tools dùng để xử lý JSON-RPC.

## Các giới hạn/điểm cần nhớ của v0.20.5

1. `DESIGN.md` nói tenant chưa hỗ trợ nhưng `adapter.py`/`protocol.py` và tests hiện đã hỗ trợ path + tenant + named profile routing. Dùng code local làm chuẩn.
2. Streaming chỉ báo lifecycle, không stream token/progress thật.
3. `CancelTask` không abort turn đang chạy; chỉ đổi trạng thái protocol và bỏ reply khỏi waiter.
4. TaskStore in-memory; restart mất task/status/subscription. Chỉ transcript và profile session còn.
5. Không có non-blocking submit primitive, idempotency key hay retry contract.
6. `INPUT_REQUIRED` là state thực tế nhưng không nằm trong internal terminal set; continuation tạo task mới cùng context.
7. `historyLength` được nhận nhưng Task history không được dựng; `stateTransitionHistory` quảng bá false.
8. Một push config/task, one-shot, không retry, callback text cắt 2.000 ký tự, auth fields ngoài URL không được dùng; không phải mọi terminal transition đều phát push.
9. Metrics public và không phân tenant; Agent Card public; health chỉ che topology khi remote chưa auth.
10. Root trỏ active profile, không nhất thiết luôn là profile tên `default`.
11. Context persistence không namespace theo profile/peer; context ID phải globally unique.
12. Inbound mọi peer vẫn bị privacy framing; không có trusted-operator tier.
13. Outbound Hermes tool surface chỉ bao phủ discover/call/list/history/orchestrate, không phải full inbound A2A surface.
14. Một số biến runtime được source hỗ trợ nhưng chưa xuất hiện đầy đủ trong `plugin.yaml` setup UI; cần kiểm tra cả source/README khi cấu hình ở bước sau.
15. Adapter không kiểm tra chặt trường `jsonrpc: "2.0"`, không hỗ trợ JSON-RPC batch đúng nghĩa, và vẫn trả response `id: null` cho request không có id thay vì xử lý như notification fire-and-forget.
16. Named-profile forwarding là lời gọi CLI đồng bộ: stream không phát submitted/working cho tới khi subprocess xong, task ID khó dùng để điều khiển trong lúc chạy, và input-required marker không được map.
17. `tasks_completed` metrics tăng cho cả `INPUT_REQUIRED`, nên counter này không đồng nghĩa mọi task đã ở terminal completed.

## Nguồn

- [Hermes A2A user guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a)
- [NousResearch/hermes-agent – A2A plugin](https://github.com/NousResearch/hermes-agent/tree/main/plugins/platforms/a2a)
- [A2A protocol specification](https://a2a-protocol.org/latest/specification/)
- Source local `0.20.5`: `plugins/platforms/a2a/{README.md,DESIGN.md,adapter.py,protocol.py,security.py,tools.py,plugin.yaml}` tại commit nêu ở đầu tài liệu.
