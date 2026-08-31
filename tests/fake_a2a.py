from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeA2AServer:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.turns: dict[str, int] = {}
        self.method_counts: dict[str, int] = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def start(self) -> FakeA2AServer:
        self.thread.start()
        return self

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _make_task(self, message: dict[str, Any], *, working: bool = False) -> dict[str, Any]:
        context = message.get("contextId") or f"ctx-{uuid.uuid4().hex}"
        task_id = f"task-{uuid.uuid4().hex}"
        self.turns[context] = self.turns.get(context, 0) + 1
        text = "\n".join(str(p.get("text", "")) for p in message.get("parts") or [])
        state = "TASK_STATE_WORKING" if working else "TASK_STATE_COMPLETED"
        answer = f"fake-marker turn={self.turns[context]} input={text}"
        if "need input" in text:
            state = "TASK_STATE_INPUT_REQUIRED"
            answer = "Which value should I use?"
        task = {
            "id": task_id,
            "contextId": context,
            "status": {"state": state},
            "artifacts": [{"artifactId": "a1", "parts": [{"text": answer}]}],
        }
        self.tasks[task_id] = task
        return task

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _json(self, value: dict[str, Any], status: int = 200) -> None:
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/.well-known/agent-card.json":
                    self._json(
                        {
                            "name": "Fake Hermes",
                            "version": "0.20.5-fake",
                            "description": "test server",
                            "url": outer.endpoint + "/",
                            "supportedInterfaces": [{"protocolBinding": "JSONRPC", "url": outer.endpoint + "/"}],
                            "capabilities": {"streaming": True, "pushNotifications": False},
                        }
                    )
                    return
                if self.path == "/health":
                    self._json({"status": "ok", "version": "0.20.5-fake"})
                    return
                self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                method = body.get("method")
                outer.method_counts[method] = outer.method_counts.get(method, 0) + 1
                params = body.get("params") or {}
                rpc_id = body.get("id")
                if method in {"SendMessage", "SendStreamingMessage"}:
                    message = params.get("message") or {}
                    text = " ".join(str(p.get("text", "")) for p in message.get("parts") or [])
                    task = outer._make_task(message, working="long" in text or "delayed result" in text)
                    if method == "SendMessage":
                        self._json({"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task}})
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    first = {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task}}
                    self.wfile.write(f"data: {json.dumps(first)}\n\n".encode())
                    self.wfile.flush()
                    if "delayed result" in text:
                        time.sleep(1.2)
                    elif "long operation" in text:
                        time.sleep(2)
                    terminal = json.loads(json.dumps(outer.tasks[task["id"]]))
                    if terminal["status"]["state"] == "TASK_STATE_WORKING":
                        terminal["status"]["state"] = "TASK_STATE_COMPLETED"
                        outer.tasks[task["id"]] = terminal
                    final = {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": terminal}}
                    try:
                        self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if method == "GetTask":
                    task_id = params.get("id")
                    lookup_task = outer.tasks.get(task_id) if isinstance(task_id, str) else None
                    if not lookup_task:
                        self._json({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": "not found"}})
                    else:
                        self._json({"jsonrpc": "2.0", "id": rpc_id, "result": {"task": lookup_task}})
                    return
                if method == "ListTasks":
                    self._json({"jsonrpc": "2.0", "id": rpc_id, "result": {"tasks": list(outer.tasks.values())}})
                    return
                if method == "CancelTask":
                    task_id = params.get("id")
                    cancel_task = outer.tasks.get(task_id) if isinstance(task_id, str) else None
                    if not cancel_task:
                        self._json({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": "not found"}})
                    else:
                        cancel_task["status"]["state"] = "TASK_STATE_CANCELED"
                        self._json({"jsonrpc": "2.0", "id": rpc_id, "result": {"task": cancel_task}})
                    return
                if method == "SubscribeToTask":
                    task_id = params.get("id")
                    subscribed_task = outer.tasks.get(task_id) if isinstance(task_id, str) else None
                    if not subscribed_task:
                        self._json({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": "not found"}})
                        return
                    event = {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": subscribed_task}}
                    raw = f"data: {json.dumps(event)}\n\n".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self._json({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": "unknown"}})

        return Handler
