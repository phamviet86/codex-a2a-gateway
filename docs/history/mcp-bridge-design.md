# Thiết kế Codex MCP → Hermes A2A bridge

> **Research record:** tài liệu này mô tả không gian thiết kế và snapshot Hermes v0.1. Contract đang triển khai nằm trong README, `architecture-v0.2.md` và `inbound-gateway.md`.

> Cập nhật triển khai 2026-08-25: v0.1.1 dùng stdio MCP, Python 3.11, SQLite, localhost-only, profile `default` và đúng bảy high-level tools hội thoại/task. Mọi send dùng lifecycle SSE để nhận task ID sớm; `outcome_unknown` được reconcile bằng context-scoped task list/conversation persistence mà không resend. Phần còn lại của tài liệu vẫn là tham chiếu capability đầy đủ; không phải mọi A2A operation bên dưới đều được expose trong v0.1.

## Trạng thái tài liệu và mục tiêu nghiên cứu

Tài liệu này giữ lại **không gian thiết kế** rộng đã dùng trước khi chốt v0.1. Surface đã triển khai được ghi rõ ở phần mở đầu và README; các surface ứng viên bên dưới không phải commitment hay roadmap. Tài liệu khảo sát cách một MCP server có thể cho Codex nhìn thấy bộ tools ổn định, trong khi bridge làm A2A v1.0/JSON-RPC client tới Hermes:

1. người dùng nói tự nhiên với Codex;
2. Codex chọn Hermes profile/served agent và gọi MCP tool;
3. bridge discover Agent Card, gửi task, relay state/progress/kết quả;
4. lượt sau tiếp tục đúng `contextId`, endpoint/profile và tenant;
5. task dài có thể theo dõi, hủy và khôi phục sau timeout/restart bridge trong giới hạn Hermes.

Thiết kế dựa trên hành vi Hermes local `0.20.5` mô tả tại [Hermes A2A reference](./hermes-a2a-reference.md). V0.1.1 đã chốt đúng bảy tool hội thoại/task, localhost-only và không có administration tools; những capability còn lại chỉ là research reference cho thảo luận phiên bản sau.

## Ranh giới capability đã xác minh, chưa phải product scope

A2A adapter Local 0.20.5 cung cấp discovery, gửi/stream message, task get/list/cancel/subscribe, push-config CRUD, profile/tenant routing, health và metrics. Nó **không cung cấp** operation để cài Hermes, sửa cấu hình, đổi model, quản lý plugin, update, hoặc start/stop dịch vụ.

Vì vậy có hai lớp cần phân biệt:

- **A2A-native:** mọi capability được kiểm chứng và mapping trong tài liệu này; đây là dữ liệu đầu vào cho quyết định MCP surface.
- **Hermes administration:** CLI/dashboard hoặc API khác ngoài A2A. Đưa lớp này vào cùng MCP server sẽ cần một thiết kế và threat model riêng, không thể suy ra từ A2A.

Ý tưởng “bridge chỉ trò chuyện/giao việc, còn quản trị giữ ở CLI/dashboard” là một phương án an toàn và hẹp, **chưa phải quyết định đã chốt**. Tương tự, tài liệu không kết luận bridge có hay không thay thế CLI. Với capability hiện tại, riêng A2A bridge chắc chắn không thể thay thế toàn bộ CLI/dashboard.

## Ranh giới trách nhiệm

```text
User ↔ Codex/MCP client ↔ MCP gateway ↔ HTTP JSON-RPC/SSE ↔ Hermes A2A adapter
                              │
                              └─ mapping store + job/event store + auth refs
```

- **Codex:** quyết định khi nào giao việc, cung cấp `conversation_key` ổn định nếu host cho phép, hiển thị câu hỏi/kết quả.
- **Bridge:** resolve profile, quản lý Agent Card cache, A2A envelope, state machine, context mapping, timeout/retry, SSE → MCP progress, chống loop và redaction log.
- **Hermes:** xử lý agentic task, giữ live/profile session, phát Task state/artifact và thực thi auth/rate limit riêng.

Không gọi vòng qua Hermes tool `a2a_call`; bridge phải POST trực tiếp vào URL JSONRPC trên Agent Card.

## Bốn tool MVP trước đây

Bộ bốn tool tối thiểu có thể là:

| MCP tool MVP | Mục đích | A2A mapping | Thiếu so với full A2A |
|---|---|---|---|
| `hermes_discover` | Khám phá Hermes/Agent Card | GET Agent Card | Chưa health/metrics, cache policy, profile catalog rõ ràng. |
| `hermes_send_task` | Gửi yêu cầu mới | `SendMessage` | Đồng bộ; chưa stream/background/push. |
| `hermes_get_task` | Lấy trạng thái/kết quả | `GetTask` | Chưa list/cancel/subscribe. |
| `hermes_continue` | Gửi lượt tiếp theo | `SendMessage` với `contextId` cũ | Đây không phải A2A operation riêng; dễ lệch endpoint/profile nếu mapping sơ sài. |

Bốn tool này đủ cho demo happy-path nhưng **không phải full A2A**. Đặc biệt, continue chỉ là send với cùng context; task dài, `INPUT_REQUIRED`, cancel, SSE, push config, task list, tenant isolation và bridge observability vẫn thiếu.

## MCP surface ứng viên để thảo luận

Không cần ánh xạ 1:1 mọi A2A method. Surface ứng viên sau minh họa cách giữ số tool vừa phải mà vẫn bao phủ đầy đủ chức năng A2A Hermes đang có; chưa có tool nào trong danh sách được chốt để triển khai.

### 1. `hermes_agents`

Action: `discover | list | health | metrics`.

Input chính:

- `action`;
- `profile` hoặc `endpoint` (endpoint trực tiếp chỉ cho phép khi policy cho phép);
- `refresh_card?: boolean`.

Output có cấu trúc:

- resolved profile, card URL, RPC URL, tenant;
- name/description/skills/capabilities/security;
- health hoặc metrics snapshot;
- `verified_at`, cache status và warning nếu card/interface đổi.

### 2. `hermes_send`

Gửi message mới hoặc tiếp tục hội thoại trong cùng một tool.

Input chính:

- `message` bắt buộc;
- `profile?: string` mặc định `default`;
- `conversation_key?: string` để tra mapping tự động;
- `context_id?: string` override tường minh;
- `mode?: "wait" | "stream" | "background"`;
- `timeout_seconds?`, `push_callback?` chỉ nếu bridge vận hành callback an toàn;
- optional text/file/data parts nếu quyết định sản phẩm đưa rich input vào scope.

Output chuẩn hóa:

- `bridge_job_id`, `a2a_task_id`, `context_id`;
- resolved `profile`, `rpc_url`, `tenant`;
- `state`, `message`, `artifacts`;
- `needs_input`, `retryable`, timestamps/warnings.

`context_id` có trước nếu bridge tự sinh globally unique. Với `background`, tool trả job handle sớm dù A2A server không có primitive submit-only; bridge worker giữ HTTP/SSE call.

### 3. `hermes_task`

Action: `get | list | cancel | subscribe`.

Input theo action:

- `task_id` cho get/cancel/subscribe;
- `profile` hoặc `bridge_job_id` để resolve scope;
- list: `context_id`, `state`, `page_size`, opaque `cursor`, `include_artifacts`;
- subscribe: `timeout_seconds` và có thể `return_on_state_change`.

Bridge phải giữ A2A `pageToken` như opaque string dù Hermes hiện dùng numeric offset.

### 4. `hermes_push_config`

Action: `create | get | list | delete`.

Tool này chỉ khả dụng nếu phương án được chọn có callback receiver. Trong một surface local hẹp, có thể không expose tool này hoặc trả capability-disabled rõ ràng cho đến khi callback listener được cấu hình an toàn.

### 5. `hermes_context`

Bridge-only action: `get | list | bind | reset`.

- inspect mapping Codex conversation ↔ profile/endpoint/tenant/context;
- bind một Codex conversation vào context ID đã biết sau khi bridge restart/migrate;
- reset tạo context mới mà không xóa Hermes transcript;
- không cho đổi profile của context đang tồn tại; phải reset/fork context.

### 6. `hermes_job`

Bridge-only action: `get | list | cancel | events | result` cho background workers. Nó tách bridge job lifetime khỏi A2A TaskStore vốn mất khi Hermes restart.

Nếu MCP client đã negotiate **MCP task-augmented tools**, `hermes_send(mode=background)` có thể dùng MCP task native. Nếu không, vẫn trả `bridge_job_id` và dùng `hermes_job`. MCP tasks còn là phần protocol cần capability negotiation; không giả định mọi bản Codex host đều hỗ trợ. Progress notifications tiêu chuẩn chỉ nên phát khi request có progress token.

## Bảng mapping MCP → A2A

| MCP tool/action | HTTP/A2A operation | Sync/async phía bridge | Context/profile routing | Input/output quan trọng |
|---|---|---|---|---|
| `hermes_agents.discover` | GET `/.well-known/agent-card.json` | Sync, cacheable | Chọn card root hoặc `/<path>/...`; lấy tenant/interface từ card | URL/profile → normalized card. |
| `hermes_agents.list` | Bridge registry; có thể GET root `/health` khi local/authed | Sync | Không dùng context | Danh mục cấu hình + served agents đã biết. |
| `hermes_agents.health` | GET `/health` | Sync | URL prefix của agent | Status, agent, served_agents nếu được phép. |
| `hermes_agents.metrics` | GET `/metrics` | Sync | Metrics không tenant-scoped | Counter snapshot. |
| `hermes_send(wait)` | `SendMessage` | Sync, chờ terminal/timeout | Message mang context; params mang tenant; URL chọn profile | Task/message response chuẩn hóa. |
| `hermes_send(stream)` | `SendStreamingMessage` | Sync streaming; MCP progress out-of-band nếu supported | Như trên | SSE events → progress + final result. |
| `hermes_send(background)` | `SendStreamingMessage` ưu tiên, fallback `SendMessage` trong worker | Async bridge job | Mapping được commit trước khi dispatch | Trả job/context sớm; worker giữ A2A call. |
| `hermes_task.get` | `GetTask` | Sync | Bắt buộc endpoint+tenant đúng với task | A2A Task chuẩn hóa. |
| `hermes_task.list` | `ListTasks` | Sync paginated | Scope theo endpoint/profile/tenant | Filters, cursor, totals. |
| `hermes_task.cancel` | `CancelTask` | Sync protocol cancel | Scope từ task mapping | Canceled không đồng nghĩa computation đã abort. |
| `hermes_task.subscribe` | `SubscribeToTask` | SSE wait | Scope từ task mapping | Terminal update hoặc timeout. |
| `hermes_push_config.create` | `CreateTaskPushNotificationConfig` hoặc inline config khi send | Sync | Scope task | URL → configId. |
| `hermes_push_config.get/list/delete` | Push config CRUD tương ứng | Sync | Scope task | Config/result chuẩn hóa. |
| `hermes_context.*` | Không có A2A operation | Local bridge state | Quản lý mapping bất biến | Context record/version. |
| `hermes_job.*` | Local worker; có thể gọi Get/Cancel/Subscribe | Async bridge state | Job giữ task/profile/context triple | Event log/result/recovery. |

## Profile resolution và routing contract

### Registry đề xuất

Bridge có cấu hình logic (không tạo ở bước tài liệu này) dạng:

```yaml
profiles:
  default:
    card_url: http://127.0.0.1:9900/.well-known/agent-card.json
    auth_ref: hermes-local-default
  research:
    card_url: http://127.0.0.1:9900/research/.well-known/agent-card.json
    auth_ref: hermes-local-default
```

`auth_ref` trỏ tới secret store/env, không chứa token trong tool input, log hoặc mapping DB.

Resolution algorithm:

1. Nếu `profile` vắng: dùng `default`.
2. Lấy `card_url` từ allow-listed registry; không suy đoán profile path từ tên.
3. Fetch/cache Agent Card; chọn interface có `protocolBinding=JSONRPC` và version `1.0`.
4. Ghi nhận chính xác `interface.url` và `interface.tenant`.
5. Mọi request send/query/cancel/subscribe cho context/task đó phải dùng lại cùng `(profile, rpc_url, tenant)`.
6. Nếu card đổi URL/tenant trong lúc context đang hoạt động, không âm thầm migrate. Đánh dấu `routing_changed` và yêu cầu explicit reset/rebind.

Đối với profile `default`, cấu hình an toàn nhất là gateway Hermes đang chạy active profile `default` và registry trỏ root card. Không dùng named alias `profile: default` để vượt qua active profile khác vì Local 0.20.5 coi nó là local live agent.

## Conversation mapping

Bridge phải persist record tối thiểu:

| Field | Ý nghĩa |
|---|---|
| `conversation_key` | Stable ID của Codex task/chat nếu host cung cấp; nếu không, ID do gateway cấp và trả cho model. |
| `profile_key` | Tên logic (`default`, `research`, ...). |
| `card_url`, `rpc_url`, `tenant` | Route đã resolve, đóng băng cho context. |
| `context_id` | A2A context globally unique, ví dụ `codex-<random-uuid>`. |
| `last_task_id` | Task A2A gần nhất. |
| `active_job_id` | Job dài đang chạy, nếu có. |
| `state` | Last normalized state, gồm input-required. |
| `created_at`, `updated_at`, `mapping_version` | Audit/recovery/concurrency control. |
| `auth_ref` | Tên reference, không phải secret. |

Invariants:

- Một `(conversation_key, profile_key)` có tối đa một active context mặc định.
- Một context không bao giờ đổi profile, RPC URL hoặc tenant.
- Một task ID luôn gắn với đúng context và route.
- Context ID do bridge tạo phải globally unique để tránh Hermes JSONL collision giữa profile/peer.
- Khi hai tool call cùng context, serialize send theo context; Hermes live adapter có FIFO reply nhưng bridge không nên dựa vào concurrent same-context semantics.
- Mapping commit trước network dispatch để timeout mơ hồ vẫn có context cho recovery.

## Luồng chính

### Lượt đầu

1. Resolve `profile=default` → card → RPC URL/tenant.
2. Tạo và persist `context_id` mới.
3. Gửi `SendMessage` hoặc `SendStreamingMessage`, đặt context trong Message và tenant trong params.
4. Persist task/job mapping ngay khi biết task ID.
5. Trả kết quả chuẩn hóa cho Codex.

### Lượt tiếp theo

1. Lookup bằng `conversation_key` hoặc context override.
2. Kiểm tra profile yêu cầu khớp mapping.
3. Không gửi toàn bộ transcript Codex; chỉ gửi message mới với cùng `contextId`. Hermes giữ conversation session.
4. Task mới được tạo; update `last_task_id` nhưng giữ context.

### `INPUT_REQUIRED`

1. Map `TASK_STATE_INPUT_REQUIRED` thành `needs_input: true` và trả câu hỏi từ `status.message`.
2. Codex hỏi người dùng tự nhiên.
3. Câu trả lời kế tiếp gọi `hermes_send` với cùng conversation/context/profile.
4. Không poll task cũ để “resume”; continuation là `SendMessage` mới cùng context.

### Task dài

- `stream`: relay submitted/working/terminal qua MCP progress notification nếu client cung cấp progress token; luôn trả final structured result.
- `background`: worker giữ SSE/HTTP call; tool trả `bridge_job_id` ngay. Codex dùng `hermes_job.get/events/result`.
- Có thể đăng ký push inline nếu callback receiver đã sẵn sàng trước send; không chờ biết task ID mới tạo config vì synchronous send có thể đã hoàn tất.
- Sau bridge restart, job record có thể còn nhưng Hermes TaskStore có thể mất nếu Hermes cũng restart. Khi `GetTask` trả not found, phân biệt `hermes_task_lost`; transcript/context vẫn có thể tiếp tục nhưng kết quả task cũ không khôi phục được qua A2A.

## Streaming/progress translation

| A2A event | Bridge state | MCP behavior |
|---|---|---|
| `task` submitted | `submitted` + capture task ID | Progress message “Hermes đã nhận task”. |
| `statusUpdate` working | `working` | Progress heartbeat; không tuyên bố phần trăm giả. |
| SSE comment keepalive | Không đổi | Refresh transport timeout nội bộ, không spam Codex. |
| `artifactUpdate` | Append artifact | Có thể emit progress summary nhỏ; final artifact giữ nguyên có cấu trúc. |
| `statusUpdate` input-required | `input_required` | Dừng wait, trả `needs_input`. |
| terminal update + close | terminal | Hoàn tất MCP tool/task. |
| stream đóng không terminal | `transport_interrupted` | Reconcile bằng `GetTask`; không tự coi completed. |

Vì Hermes 0.20.5 không token-stream, UI chỉ nên hứa state/progress streaming, không hứa từng token.

## Retry, timeout và idempotency

| Tình huống | Chính sách đề xuất |
|---|---|
| GET Agent Card/health/metrics lỗi tạm thời | Retry exponential backoff có jitter, tối đa nhỏ. |
| `GetTask`/`ListTasks` lỗi mạng | Retry an toàn vì read-only. |
| 401/403 | Không retry; báo auth/trust configuration. |
| 429 | Tôn trọng backoff; Hermes không trả `Retry-After` trong code hiện tại, dùng capped exponential delay. |
| JSON-RPC invalid params/method | Không retry; surface protocol mismatch. |
| Send chưa nhận HTTP response, connection timeout/reset | **Không tự resend**, vì Hermes không dedup theo request/message ID; có thể tạo task/side effect trùng. Đánh dấu `outcome_unknown`, thử reconcile nếu đã biết task ID từ SSE, nếu không yêu cầu người dùng quyết định. |
| Reply timeout nhưng có task ID | Poll `GetTask` hoặc subscribe trong giới hạn. |
| `GetTask` not found sau Hermes restart | Mark `task_lost`; không resend tự động. |
| Bridge cancel | Gọi `CancelTask`, dừng local waiter; cảnh báo Hermes computation có thể tiếp tục. |

Ba timeout riêng:

- connect timeout ngắn (ví dụ 2–5 giây local);
- idle/read timeout được refresh bởi SSE keepalive;
- absolute task deadline do caller chọn, luôn hữu hạn.

Bridge không nên mặc định deadline lớn hơn `A2A_REPLY_TIMEOUT` của Hermes mà không có background/push strategy.

## Auth và network cho localhost

Baseline local an toàn:

- MCP transport giữa Codex và gateway dùng stdio nếu có thể; không mở port mới.
- Hermes A2A bind `127.0.0.1`; token có thể không bắt buộc theo Hermes, nhưng bridge vẫn hỗ trợ bearer từ secret reference.
- Nếu dùng bearer, ưu tiên per-peer token định danh `codex-bridge`; trust list giới hạn đúng identity đó.
- Không nhận bearer token qua MCP tool argument. Secret chỉ vào HTTP header và bị redaction khỏi logs/errors.
- Registry mặc định chỉ cho `http://127.0.0.1`/`http://localhost` và known profile paths. Direct arbitrary endpoint là opt-in để tránh SSRF.
- Không forward headers do model cung cấp; không tin `Host`/redirect tới private/remote host ngoài allow-list.
- Card discovery public không có nghĩa RPC không auth; luôn áp auth policy của registry/card.
- `/metrics` chứa operational data và Hermes hiện để public; bridge không nên expose rộng hơn MCP client đã tin cậy.

Nếu một phương án expose remote được chọn, nó cần TLS/reverse proxy, bearer per peer, explicit host allow-list, secret rotation và firewall; các yêu cầu này tách khỏi baseline localhost đang khảo sát.

## Tránh loop agent-to-agent

Hermes đã có cap 5 lượt/context mặc định, nhưng bridge cần lớp riêng:

1. Không bật Hermes outbound `a2a` toolset cho inbound A2A platform/profile trừ khi use case cần agent chaining.
2. Mỗi job giữ `origin=codex-mcp`, `hop_count`, `parent_job_id`, và fingerprint `(profile, context, normalized message)` trong bridge metadata.
3. Từ chối khi hop vượt policy (ví dụ giới hạn 2 trong một surface hẹp) hoặc cùng fingerprint quay lại trong một causal chain.
4. Không tự động gửi output Hermes trở lại Hermes. Chỉ Codex/user tạo lượt mới rõ ràng.
5. Serialize theo context và không fan-out cùng một context tới nhiều profile.
6. Khi Hermes trả anti-loop `TASK_STATE_REJECTED`, surface nguyên nhân; không tự tạo context mới để né guard.
7. `reset context` là explicit operation có audit trail.

Hermes không đọc bridge metadata như một authorization primitive, nên enforcement phải nằm ở bridge và cấu hình toolset.

## Error/result contract cho Codex

Mọi tool nên trả structured content ổn định, ví dụ các field:

```json
{
  "ok": true,
  "profile": "default",
  "context_id": "codex-...",
  "task_id": "task-...",
  "job_id": "job-...",
  "state": "completed",
  "needs_input": false,
  "message": "...",
  "artifacts": [],
  "retryable": false,
  "warnings": []
}
```

Phân biệt:

- **tool/protocol error:** không gọi được, auth, invalid params;
- **task failure:** A2A call thành công nhưng Task state failed/rejected/canceled;
- **input required:** không phải lỗi;
- **outcome unknown:** send có thể đã được Hermes nhận nhưng bridge không xác nhận được;
- **task lost:** bridge có record nhưng Hermes TaskStore không còn.

Không nhét lỗi vào chuỗi “Error: ...” duy nhất; Codex cần field để quyết định retry/hỏi người dùng.

## Persistence và recovery của bridge

Bridge store nên là local SQLite hoặc tương đương, atomic và có schema version. Cần ba nhóm bảng/record:

- `agent_profiles/cards`: registry, resolved interface, tenant, card hash, verified time;
- `conversations/tasks`: mapping invariant nêu trên;
- `jobs/events`: background lifecycle, SSE event log giới hạn, final result, error category.

Không copy Hermes bearer token vào DB; chỉ lưu `auth_ref`. Giới hạn retention cho artifacts/events và redact credentials/email theo policy riêng có thể cấu hình. Hermes redaction hiện có thể thay email hợp lệ; bridge không nên tự động “khôi phục” dữ liệu đã bị Hermes redacted.

Recovery sequence:

1. Load non-terminal jobs.
2. Với job có task ID, gọi `GetTask` đúng route; nếu working, có thể `SubscribeToTask` lại.
3. Với job chưa từng nhận task ID nhưng send đã bắt đầu, mark `outcome_unknown`; không resend. Query `ListTasks` theo context và chỉ correlate 1:1; nếu TaskStore đã mất, có thể đọc local A2A conversation persistence với cùng invariant.
4. Nếu Hermes not found, mark `task_lost` nhưng giữ context mapping cho lượt mới.
5. Reconcile card routing; nếu đổi tenant/URL, freeze context và yêu cầu reset/rebind.

## Push callback receiver

Chỉ triển khai push khi có nhu cầu thật; local SSE thường đơn giản hơn. Nếu triển khai:

- listener bind loopback hoặc một explicit callback URL;
- callback URL phải tồn tại trước send để dùng inline config;
- verify `X-A2A-Signature` bằng parse JSON rồi canonical sorted-key serialization đúng cách Hermes ký;
- correlate task/context/profile trước khi accept event;
- idempotent event ingestion dù Hermes hiện chỉ push một lần;
- không phụ thuộc push là delivery đảm bảo: không retry, config bị pop trước send;
- poll `GetTask` để reconcile khi push timeout/thất bại.

## Phân nhóm capability phục vụ quyết định sản phẩm sau nghiên cứu

| Nhóm | Capability đã xác minh | Câu hỏi product scope còn mở |
|---|---|---|
| Hội thoại trực tiếp | discover, send, continue bằng context, input-required, kết quả | Có phải surface mặc định duy nhất hay không? |
| Task lifecycle | get/list/cancel/subscribe, state/error normalization | Expose thành tool riêng hay ẩn sau `send`/job abstraction? |
| Long-running | SSE lifecycle, background worker ứng viên, push config | Cần stream, poll, push hay kết hợp nào trong Codex UX? |
| Profile routing | root active profile, named path, tenant | Cho model chọn tự do, allow-list, hay user pin profile? |
| Observability | health, metrics, bridge jobs/events | Đây là diagnostic surface hay model-callable tools? |
| Rich input | A2A text/file/data Parts inbound | MVP chỉ text hay có file/data ngay? |
| Administration ngoài A2A | install/config/model/plugin/update/service lifecycle | Giữ hoàn toàn ở CLI/dashboard hay thiết kế một surface khác sau này? |

## Các tiêu chí kỹ thuật dùng để đánh giá mọi phương án

Đây là tiêu chí so sánh, không phải acceptance criteria của một implementation đã chốt:

- Profile `default` và named profile phải resolve nhất quán nếu capability chọn profile được đưa vào scope.
- Multi-turn không được trộn `contextId`; `INPUT_REQUIRED` phải có đường quay lại người dùng tự nhiên.
- Streaming phải được mô tả đúng là lifecycle streaming, không token streaming.
- Retry không được tạo task/side effect trùng sau timeout mơ hồ.
- Cancel phải nói rõ giới hạn không abort computation của Hermes 0.20.5.
- Auth secret không đi qua model-visible tool input/output/log.
- Surface nào được chọn cũng phải có anti-loop và route/profile isolation.
- Capability ngoài A2A không được mô tả như thể Hermes A2A đã hỗ trợ.

## Kết luận mở về CLI/dashboard

A2A bridge có thể bao phủ đầy đủ **giao tiếp agent-to-agent** mà Hermes 0.20.5 công bố, gồm multi-turn, profile routing, task lifecycle, streaming và push. Nó không có wire operations cho Hermes administration, nên không thể tự thân thay thế CLI/dashboard ở các việc cài đặt, cấu hình, model, plugin, update hay quản lý service.

Có giữ ranh giới này cố định trong sản phẩm hay bổ sung một administrative surface riêng là quyết định cần thảo luận sau. Tài liệu hiện không khuyến nghị cũng không loại trừ phương án đó; nó chỉ tách rõ capability A2A đã xác minh khỏi capability ngoài A2A.

## Nguồn tham chiếu

- [Hermes A2A user guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a)
- [NousResearch/hermes-agent A2A source](https://github.com/NousResearch/hermes-agent/tree/main/plugins/platforms/a2a)
- [A2A v1.0 specification](https://a2a-protocol.org/latest/specification/)
- [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools), [MCP progress](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress), [MCP tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
- Source Hermes local `0.20.5`, commit `d736f5d53f1d33fabad5a17cb070eb138b618fb8`, là chuẩn khi có khác biệt.
