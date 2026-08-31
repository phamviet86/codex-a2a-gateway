# Contributing

Cảm ơn bạn muốn đóng góp. Project đang ở trạng thái public beta, dùng Python 3.11, MCP stdio cho outbound Hermes adapter và A2A v1.0 HTTP/SSE cho inbound Codex gateway.

## Thiết lập phát triển

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest --cov=codex_a2a_gateway
```

Không cần Hermes thật để chạy test mặc định: integration suite dùng fake A2A server ở ephemeral loopback port. Live test phải là thao tác chủ động, dùng prompt vô hại và không được chạy trong CI.

## Pull request

- Giữ đúng ranh giới loopback-only và không thêm URL do model cung cấp.
- Không log lên stdout của MCP stdio server.
- Không commit token, file `.env`, SQLite, log, transcript, backup hay đường dẫn máy cá nhân.
- Thêm test cho thay đổi state, retry, timeout, idempotency hoặc cancel.
- Cập nhật README/CHANGELOG khi thay đổi contract công khai.
- PR nên nhỏ, có mô tả rủi ro và cách rollback nếu thay đổi persistence/protocol.

Bằng việc gửi contribution, bạn đồng ý cấp phép nó theo [Apache License 2.0](LICENSE).
