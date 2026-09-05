# Job bền vững và trả kết quả (bản chưa phát hành)

Tài liệu này thay thế mô tả recovery của v0.3.0 đối với checkout có bản sửa mới.
Kiểm chứng hiện tại dùng peer/backend giả lập tại máy phát triển, chưa phải live.

## Mô hình đã chốt

Một execution host/VPS chạy Codex, Hermes Agent và gateway. A2A chỉ ở loopback.
Máy cá nhân chạy Codex Desktop và dùng Codex Remote để giao việc lên VPS. Cuộc
trò chuyện Desktop khác với thread App Server mà gateway tạo cho Codex worker.
Không cần VPN, mở cổng A2A công khai hay endpoint Hermes remote trực tiếp.

Codex tự làm việc đơn giản và tham vấn Hermes khi cần. Hiện chỉ định tuyến profile
Hermes `default`; chuyên môn yêu cầu trong prompt không đồng nghĩa đã chọn được
profile riêng. Hermes có thể giao Codex tìm kiếm web, đọc/trích tài liệu, lấy dữ
liệu, dùng browser, sửa file hoặc thao tác hệ thống trong quyền thực tế của worker.
Health ghi rõ công cụ worker là `unverified`, không kế thừa plugin/tool trên Desktop.
Worker App Server được hướng dẫn báo rõ việc đã làm, artifact và công cụ còn thiếu.

Process Codex do gateway tạo mang biến `CODEX_A2A_GATEWAY_WORKER_TASK_ID`; MCP
outbound kế thừa biến này sẽ từ chối giao ngược về Hermes. Điều này chặn vòng lặp
trong đường tích hợp đi kèm, không bảo đảm cho client ngoài tự bỏ dấu provenance.

## Định danh và handle

`hermes_chat(mode="async")` lưu handle, message ID, fingerprint và nguồn yêu cầu
trước discovery/gọi mạng, rồi trả sớm. Caller giữ `bridge_task_id` và tự đặt
`idempotency_key`. Gọi lại đúng key và nội dung trả về job cũ, kể cả lần đầu không
truyền context. Không lưu prompt gốc; kết quả/artifact có thể chứa dữ liệu nhạy cảm.

Trường `origin` nhận `conversation_id`, `question_id`, `parent_job_id`, mỗi giá trị
là chuỗi định danh tối đa 256 ký tự. Không đưa nội dung câu hỏi hoặc bí mật vào đây.
Đây là thông tin nguồn do caller cung cấp, không phải endpoint giao kết quả đã xác
thực. MCP stdio không tự xác định cuộc trò chuyện Desktop gốc. Continuation giữ
nguyên nguồn của job; các job độc lập có thể cùng origin nhưng phải khác context.

Plugin Hermes lưu handle vào `ctx.state` trước `SendMessage(returnImmediately=true)`.
Nên truyền `message_id` ổn định cho từng yêu cầu để chống gọi trùng ngay tại plugin.
Continuation giữ handle nhưng có message ID mới; các fingerprint cũ còn được giữ.
Plugin không lưu nội dung result/artifact. Khi đủ 200 handle, nó từ chối job mới để
không làm mất handle cũ. Độ bền thực tế phụ thuộc Hermes ghi `ctx.state` đúng hợp đồng.

## Timeout, restart, recovery

Hết thời gian **chờ** trả `timed_out` và giữ trạng thái đã biết. Lỗi vận chuyển không
rõ kết quả là `outcome_unknown`, không được diễn giải là thất bại hoặc cho phép lặp
mutation. Deadline thực thi backend/stream khác lượt chờ caller; deadline backend
có thể dừng subprocess, trong khi tác động bên ngoài vẫn chưa xác định.

Outbound sau restart chuyển job đang dở sang unknown và không tự gửi lại prompt.
Có A2A task ID thì đọc đúng ID. Peer không còn task đó: giữ binding và trạng thái
unknown. Nếu mất ACK, chỉ recovery khi context và `metadata.requestMessageId` khớp
chính xác message đã lưu. Kiểm tra các trang ListTasks, tối đa 100 trang; candidate
mơ hồ, cursor lặp hoặc chưa đọc hết không đủ để ghép. Không ghép theo context +
unique candidate, nội dung tương tự hay thời gian gần nhau; không lấy task của job khác.

Fallback JSONL Hermes chỉ dùng khi user record có đúng `message_id`/`messageId` và
agent record có `task_state` rõ ràng. File legacy chỉ có time/role/text/task ID
không đáp ứng. Peer chưa hỗ trợ các trường này có thể giữ unknown sau mất ACK hoặc
mất task trong RAM. Bản sửa không thay installation/service Hermes đang chạy.

Inbound ghi task/message trước khi schedule. Job đã vào backend sau restart trở
thành unknown. GetTask/wait có thể đọc `thread/read(includeTurns=true)` bằng **đúng
thread ID và turn ID đã ACK**, nhận đúng turn kết thúc có đầy đủ items. Không gọi
thread/resume hoặc turn/start để recovery. Thiếu turn ID, lịch sử thiếu, CLI hoặc
turn còn chạy thì giữ unknown. Đường này đã đối chiếu schema và test giả lập, chưa
live; không suy diễn rằng clientUserMessageId tự giải quyết được mất ACK trước turn ID.

Job inbound có bằng chứng chưa vào backend được replay tường minh bằng đúng message
cũ. Với plugin, lặp nguyên tham số gồm `message_id`, thêm `resume: true`. Plugin đọc
peer trước, yêu cầu đúng request ID và mã `gateway_restarted_before_start` hoặc
`context_blocked_before_start`; thiếu bằng chứng thì không gửi lại. Get/wait, retry
thông thường và xác nhận nhận kết quả không chạy lại mutation.

## Context và yêu cầu thêm input

Job độc lập dùng context riêng. Outbound từ chối lượt mới trên context chưa xong
bằng `context_busy`. Inbound trả handle queued ngay cả khi lượt trước cùng context
còn chạy, nhưng serialize toàn bộ lượt thực thi. Kết quả chưa rõ của lượt trước
chặn lượt sau trong context đó; các context khác có thể chạy song song.

Outbound cần input: gọi `hermes_chat(task_id=<bridge handle>, message=<câu trả lời>,
idempotency_key=<key mới>)`. Giữ nguyên local handle và A2A taskId. Follow-up sau
khi hoàn tất vẫn có thể chỉ truyền context; không dùng cách đó để thay job cần input.
Hermes dùng `codex_a2a_call(task_id=<local handle>, message=<trả lời>, message_id=<ID mới>)`.

Inbound giữ A2A job và Codex thread nhưng tạo **turn mới** với câu trả lời. RPC
approval/input của App Server hiện được cancel/trả answers rỗng trước khi đóng
process. Đây không phải gắn lại RPC đang treo. Câu hỏi được trả về caller; worker phải
xem ngữ cảnh trước và không lặp mutation đã làm. Chưa hỗ trợ phục hồi nguyên native
tool call đang treo qua restart.

## Trả kết quả và xác nhận đã nhận

Kết quả đi vào tool call hiện tại hoặc lần get/wait sau bằng handle. Mỗi kết quả
kết thúc/cần input có `result_id` ổn định; A2A dùng metadata `resultId`. Caller kiểm
tra origin, dùng kết quả để tiếp tục đúng việc cũ rồi xác nhận đã nhận bằng
`hermes_task_get(acknowledge_result_id=..., expected_origin=...)` hoặc các trường
tương tự trên `codex_a2a_get`. Xác nhận lặp không chạy worker; mã của job/kết quả khác
bị từ chối. Continuation mới tạo mã kết quả mới.

Đây là **lấy kết quả theo handle và ghi nhận đã sử dụng**. Chưa tự đánh thức Hermes
hoặc chèn thông điệp vào cuộc trò chuyện Desktop gốc. Caller/orchestrator phải giữ
handle và sắp xếp get/wait bằng API runtime/scheduler thực sự có. Không có API Codex
Remote tự đặt, auto-attach turn/start, webhook hay watcher tự chạy trong bản sửa.
Receipt là xác nhận của caller, không chứng minh atomic write vào cuộc trò chuyện
ngoài. Muốn giao tự động đúng một lần cần receiver có durable dedup và API wake-up
được hỗ trợ/xác thực; đây còn là phần tích hợp peer/runtime cần làm tiếp.

## Dữ liệu và rollback

Schema 5 thêm origin, danh tính các attempt outbound và receipt; giữ các bảng/dữ
liệu cũ. Dừng writer và sao lưu SQLite cùng WAL trước khi đổi bản. Mỗi state file
chỉ có một writer đang hoạt động; MCP và daemon inbound chạy riêng cần state file
riêng. Không chạy registration cũ/mới đồng thời trên cùng file. Bản cũ bỏ qua trường
mới nhưng không áp dụng hợp đồng correlation mới; giữ backup/database mới, không
xóa job unknown để ép chạy lại. Xem [hợp đồng EN](durable-jobs.md) để đối chiếu chi tiết.
