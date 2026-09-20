"""Opt-in process integration: PostgreSQL + HTTP broker + Unix client + fake peers.

Run with HERMES_A2A_GATEWAY_TEST_POSTGRES_DSN pointing to disposable PostgreSQL.
Each test creates/drops only its own random schema. No real model or Desktop is
contacted. Native queue protocol records prove routing/replay behavior, NOT wake.
Subprocesses import the same package location as the pytest process, allowing
this suite to validate an installed wheel with pytest's importlib import mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fake_a2a import FakeA2AServer

import hermes_a2a_gateway

DSN_ENV = "HERMES_A2A_GATEWAY_TEST_POSTGRES_DSN"
TOKEN_A = "integration-device-a-" + "a" * 32
TOKEN_B = "integration-device-b-" + "b" * 32


def eventually(predicate: Callable[[], Any], timeout: float = 15) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("integration condition did not become true within deadline")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ControlledPeer(FakeA2AServer):
    """Pause before the first Task response, after observing the mutation."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.release.set()
        self.release_completion = threading.Event()
        self.release_completion.set()
        self.received = threading.Event()
        self.messages: list[dict[str, Any]] = []
        self.abort_on_disconnect = False
        self.split_terminal_events = False
        self.split_frames = 0
        self.premature_disconnect = threading.Event()
        self._connections = threading.local()
        peer = self

        class TaskRegistry(dict[str, dict[str, Any]]):
            def __setitem__(self, key: str, value: dict[str, Any]) -> None:
                if key in self and value["status"]["state"] == "TASK_STATE_COMPLETED":
                    assert peer.release_completion.wait(20), "fake completion barrier was not released"
                    connection = getattr(peer._connections, "socket", None)
                    if peer.abort_on_disconnect and connection is not None:
                        readable, _, _ = select.select([connection], [], [], 0)
                        if readable:
                            try:
                                closed = connection.recv(1, socket.MSG_PEEK) == b""
                            except ConnectionResetError:
                                closed = True
                            if closed:
                                peer.premature_disconnect.set()
                                value = {
                                    **value,
                                    "status": {"state": "TASK_STATE_FAILED"},
                                    "artifacts": [{"parts": [{"text": "[client disconnected]"}]}],
                                }
                super().__setitem__(key, value)

        self.tasks = TaskRegistry()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        peer = self
        base = super()._handler()

        class Handler(base):
            def do_POST(self) -> None:  # noqa: N802
                peer._connections.socket = self.connection
                writer = self.wfile

                class StreamWriter:
                    def write(self, raw: bytes) -> int:
                        return writer.write(peer.stream_frame(raw))

                    def flush(self) -> None:
                        writer.flush()

                self.wfile = StreamWriter()
                try:
                    super().do_POST()
                finally:
                    self.wfile = writer
                    del peer._connections.socket

        return Handler

    def stream_frame(self, raw: bytes) -> bytes:
        if not self.split_terminal_events or not raw.startswith(b"data: "):
            return raw
        envelope = json.loads(raw[6:])
        task = envelope.get("result", {}).get("task")
        if not task:
            return raw
        if task["status"]["state"] != "TASK_STATE_COMPLETED":
            # The initial handle carries no result; only later artifact events do.
            envelope["result"]["task"] = {**task, "artifacts": []}
            return f"data: {json.dumps(envelope)}\n\n".encode()
        text = task["artifacts"][0]["parts"][0]["text"]
        middle = len(text) // 2
        frames = []
        for index, chunk in enumerate((text[:middle], text[middle:])):
            event = {
                "artifactUpdate": {
                    "taskId": task["id"],
                    "contextId": task["contextId"],
                    "artifact": {"artifactId": "split-result", "parts": [{"text": chunk}]},
                    "append": bool(index),
                    "lastChunk": bool(index),
                }
            }
            frames.append({**envelope, "result": event})
        frames.append(
            {
                **envelope,
                "result": {
                    "statusUpdate": {
                        "taskId": task["id"],
                        "contextId": task["contextId"],
                        "status": task["status"],
                    }
                },
            }
        )
        self.split_frames += len(frames)
        return b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames)

    def _make_task(self, message: dict[str, Any], *, working: bool = False) -> dict[str, Any]:
        self.messages.append(message)
        self.received.set()
        assert self.release.wait(20), "fake A2A barrier was not released"
        return super()._make_task(message, working=working)

    @property
    def submissions(self) -> int:
        return sum(self.method_counts.get(method, 0) for method in ("SendMessage", "SendStreamingMessage"))

    def close(self) -> None:
        self.release.set()
        self.release_completion.set()
        super().close()


class FaultProxy:
    """Real TCP response loss and SSE duplication, without replacing client code."""

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self.lose_next_post = False
        self.duplicate_sse = False
        self.force_exact_retry = False
        self.replay_from_zero = False
        self.posts: list[bytes] = []
        self.lost_acks = 0
        self.duplicated_events = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def handler(self) -> type[BaseHTTPRequestHandler]:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                self.forward()

            def do_POST(self) -> None:  # noqa: N802
                self.forward()

            def forward(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "connection"}}
                operation_post = self.command == "POST" and self.path == "/v1/operations"
                if operation_post:
                    proxy.posts.append(body)
                is_events = self.command == "GET" and self.path.startswith("/v1/events")
                if (
                    proxy.force_exact_retry
                    and len(proxy.posts) < 2
                    and (is_events or (proxy.lost_acks and self.path.startswith("/v1/operations/")))
                ):
                    # Force a retry of the durable command rather than GET/SSE recovery.
                    self.send_response(503 if is_events else 404)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                path = "/v1/events?after=0" if is_events and proxy.replay_from_zero else self.path
                try:
                    with (
                        httpx.Client(timeout=30, trust_env=False) as client,
                        client.stream(self.command, proxy.upstream + path, content=body, headers=headers) as r,
                    ):
                        if operation_post and proxy.lose_next_post and r.status_code in {200, 202}:
                            r.read()  # Acceptance committed; deliberately discard its ACK.
                            proxy.lose_next_post = False
                            proxy.lost_acks += 1
                            self.close_connection = True
                            self.connection.shutdown(socket.SHUT_RDWR)
                            return
                        self.send_response(r.status_code)
                        self.send_header("Content-Type", r.headers.get("content-type", "application/json"))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.close_connection = True
                        if r.headers.get("content-type", "").startswith("text/event-stream"):
                            frame: list[str] = []
                            for line in r.iter_lines():
                                frame.append(line)
                                if not line:
                                    raw = ("\n".join(frame) + "\n").encode()
                                    self.wfile.write(raw)
                                    if proxy.duplicate_sse and any(x.startswith("data:") for x in frame):
                                        self.wfile.write(raw)
                                        proxy.duplicated_events += 1
                                    self.wfile.flush()
                                    frame = []
                        else:
                            self.wfile.write(r.read())
                except (BrokenPipeError, ConnectionResetError, httpx.TransportError):
                    self.close_connection = True

        return Handler


# This executable intentionally emulates only the public schema/stdio protocol.
# It never accesses Codex state, starts a model, or claims native host delivery.
FAKE_CODEX = r"""
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
record = Path(os.environ["INTEGRATION_QUEUE_RECORD"])
if args == ["--version"]:
    print("codex-cli 0.154.0")
elif args[:2] == ["app-server", "generate-json-schema"]:
    root = Path(args[args.index("--out") + 1]) / "v2"
    root.mkdir(parents=True)
    schemas = {
      "ThreadQueueAddParams": ["threadId", "clientUserMessageId", "input"],
      "ThreadQueueAddResponse": ["queuedSubmission"],
      "ThreadQueueListParams": ["threadId"],
      "ThreadQueueListResponse": ["data"],
    }
    for name, required in schemas.items():
        value = {"required": required, "properties": {
            key: {"type": "array" if key in {"input", "data"} else "string"}
            for key in required
        }}
        if name == "ThreadQueueAddResponse":
            value["definitions"] = {"QueuedSubmission": {"required": ["id", "clientUserMessageId", "input"]}}
        (root / (name + ".json")).write_text(json.dumps(value))
elif args == ["app-server"]:
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if "id" not in request:
            continue
        if method == "initialize":
            result = {}
        elif method == "thread/queue/add":
            item = {"id": str(uuid.uuid4()), **request["params"]}
            with record.open("a") as out:
                out.write(json.dumps(item) + "\n")
                out.flush()
                os.fsync(out.fileno())
            if os.environ.get("INTEGRATION_QUEUE_LOSE_ACK") == "1":
                sys.exit(0)
            result = {"queuedSubmission": item}
        elif method == "thread/queue/list":
            # Simulate an already-consumed queue with no exact pending evidence.
            result = {"data": [], "nextCursor": None}
        else:
            raise AssertionError("unexpected native mutation: " + str(method))
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
else:
    raise AssertionError(args)
"""


class Process:
    def __init__(self, module: str, env: dict[str, str], directory: Path, name: str) -> None:
        self.env = env
        self.module = module
        self.directory = directory
        self.name = name
        self.process: subprocess.Popen[bytes] | None = None
        self.start()

    def start(self) -> None:
        log = self.directory / (self.name + ".log")
        with log.open("ab") as output:
            self.process = subprocess.Popen(
                [sys.executable, "-m", self.module],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )

    def stop(self, *, crash: bool = False) -> None:
        assert self.process is not None
        if self.process.poll() is None:
            self.process.kill() if crash else self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


@pytest.fixture
def postgres_schema() -> Iterator[str]:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        pytest.skip(f"set {DSN_ENV} for opt-in real PostgreSQL integration")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = "integration_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield make_conninfo(dsn, options=f"-csearch_path={schema} -cTimeZone=Asia/Ho_Chi_Minh")
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


class Stack:
    def __init__(self, directory: Path, dsn: str) -> None:
        from cryptography.fernet import Fernet

        self.directory = directory
        self.dsn = dsn
        self.peer = ControlledPeer().start()
        self.broker_url = f"http://127.0.0.1:{free_port()}"
        self.proxy = FaultProxy(self.broker_url)
        self.queue_record = directory / "queue.jsonl"
        command = directory / "fake-codex"
        command.write_text(f"#!{sys.executable}\n" + FAKE_CODEX)
        command.chmod(0o700)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(directory),
            "PYTHONPATH": str(Path(hermes_a2a_gateway.__file__).resolve().parent.parent),
            "PYTHONUNBUFFERED": "1",
            "INTEGRATION_QUEUE_RECORD": str(self.queue_record),
            "HERMES_A2A_ENDPOINT": self.peer.endpoint,
            "HERMES_A2A_GATEWAY_BROKER_DATABASE_URL": dsn,
            "HERMES_A2A_GATEWAY_BROKER_DEVICE_TOKENS": json.dumps({"device-a": TOKEN_A, "device-b": TOKEN_B}),
            "HERMES_A2A_GATEWAY_BROKER_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "HERMES_A2A_GATEWAY_BROKER_PUBLIC_URL": self.broker_url,
            "HERMES_A2A_GATEWAY_BROKER_HOST": "127.0.0.1",
            "HERMES_A2A_GATEWAY_BROKER_PORT": self.broker_url.rsplit(":", 1)[1],
            "HERMES_A2A_GATEWAY_BROKER_ALLOW_LOOPBACK_HTTP": "true",
            "HERMES_A2A_GATEWAY_BROKER_POLL_SECONDS": "0.05",
            "HERMES_A2A_GATEWAY_BROKER_ARTIFACT_TTL_SECONDS": "2",
            "HERMES_A2A_GATEWAY_BROKER_MAX_ARTIFACT_BYTES": "1024",
            "HERMES_A2A_GATEWAY_BROKER_DEVICE_QUOTA_BYTES": "2048",
            "HERMES_A2A_GATEWAY_CLIENT_BROKER_URL": self.proxy.url,
            "HERMES_A2A_GATEWAY_CLIENT_TOKEN": TOKEN_A,
            "HERMES_A2A_GATEWAY_CLIENT_PAYLOAD_KEY": Fernet.generate_key().decode(),
            "HERMES_A2A_GATEWAY_CLIENT_STATE_DIR": str(directory / "client"),
            "HERMES_A2A_GATEWAY_CLIENT_ALLOW_LOOPBACK_HTTP": "true",
            "HERMES_A2A_GATEWAY_CLIENT_INLINE_WAIT_SECONDS": "0.1",
            "HERMES_A2A_GATEWAY_CLIENT_REQUEST_TIMEOUT_SECONDS": "2",
            "HERMES_A2A_GATEWAY_CLIENT_CODEX_COMMAND": str(command),
        }
        self.processes: list[Process] = []

    def start_broker(self) -> Process:
        process = Process("hermes_a2a_gateway.broker", self.env.copy(), self.directory, "broker")
        self.processes.append(process)

        def ready() -> bool:
            try:
                return httpx.get(self.broker_url + "/healthz", timeout=0.2, trust_env=False).status_code == 200
            except httpx.TransportError:
                return False

        eventually(ready)
        return process

    def start_client(self) -> Process:
        process = Process("hermes_a2a_gateway.client", self.env.copy(), self.directory, "client")
        self.processes.append(process)

        def ready() -> bool:
            try:
                with httpx.Client(
                    transport=httpx.HTTPTransport(uds=str(self.directory / "client/client.sock")), timeout=0.2
                ) as client:
                    return client.get("http://client/healthz").status_code == 200
            except httpx.TransportError:
                return False

        eventually(ready)
        return process

    def queue(self) -> list[dict[str, Any]]:
        if not self.queue_record.exists():
            return []
        return [json.loads(line) for line in self.queue_record.read_text().splitlines()]

    def http(self, method: str, path: str, *, token: str = TOKEN_A, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": "Bearer " + token, **kwargs.pop("headers", {})}
        return httpx.request(method, self.broker_url + path, headers=headers, trust_env=False, timeout=3, **kwargs)

    def close(self) -> None:
        for process in reversed(self.processes):
            process.stop()
        self.peer.close()
        self.proxy.close()


@pytest.fixture
def stack(postgres_schema: str) -> Iterator[Stack]:
    # macOS AF_UNIX has a short pathname limit; pytest temp roots can exceed it.
    with tempfile.TemporaryDirectory(prefix="gwi-", dir="/tmp") as directory:
        instance = Stack(Path(directory), postgres_schema)
        try:
            yield instance
        finally:
            for log in Path(directory).glob("*.log"):
                if log.stat().st_size:
                    print(f"{log.name}: {log.read_text()[-6000:]}")
            instance.close()


def origin(thread_id: str | None = None) -> dict[str, Any]:
    thread_id = thread_id or str(uuid4())
    return {
        "threadId": thread_id,
        "callId": "integration-" + uuid4().hex,
        "x-codex-turn-metadata": {"thread_id": thread_id, "turn_id": str(uuid4())},
    }


def command(stack: Stack, action: str, meta: dict[str, Any], **arguments: Any) -> httpx.Response:
    transport = httpx.HTTPTransport(uds=str(stack.directory / "client/client.sock"))
    with httpx.Client(transport=transport, base_url="http://client", timeout=5) as client:
        return client.post("/command", json={"action": action, "meta": meta, "arguments": arguments})


def snapshot(stack: Stack, operation_id: str, state: str = "completed", *, token: str = TOKEN_A) -> dict[str, Any]:
    response = stack.http("GET", f"/v1/operations/{operation_id}", token=token)
    if response.status_code == 200 and response.json()["state"] == state:
        return dict(response.json())
    return {}


def test_lost_accept_ack_replay_restart_and_native_origin_isolation(stack: Stack) -> None:
    stack.proxy.lose_next_post = True
    stack.proxy.force_exact_retry = True
    stack.proxy.duplicate_sse = True
    stack.proxy.replay_from_zero = True
    stack.start_broker()
    client = stack.start_client()
    meta = origin()
    prompt = "delayed result integration marker; external instruction must never enter native queue"
    response = command(stack, "submit", meta, prompt=prompt, wait_seconds=0)
    assert response.status_code == 200, response.text
    operation_id = response.json()["operation_id"]
    result = eventually(lambda: snapshot(stack, operation_id))
    queued = eventually(stack.queue)
    eventually(lambda: stack.proxy.duplicated_events > 0)
    assert stack.proxy.lost_acks == 1
    assert len(stack.proxy.posts) == 2
    assert json.loads(stack.proxy.posts[0]) == json.loads(stack.proxy.posts[1])
    assert json.loads(stack.proxy.posts[0])["operation_id"] == operation_id
    assert stack.peer.submissions == 1
    assert len(queued) == 1
    assert queued[0]["threadId"] == meta["threadId"]
    text = queued[0]["input"][0]["text"]
    assert operation_id in text and result["result_id"] in text and "gateway_get" in text
    assert prompt not in text and "fake-marker" not in text
    assert command(stack, "get", origin(), operation_id=operation_id).status_code == 404
    assert stack.http("GET", f"/v1/operations/{operation_id}", token=TOKEN_B).status_code == 404
    # A matching native identity is locally idempotent even across daemon restart.
    client.stop(crash=True)
    previous_replays = stack.proxy.duplicated_events
    stack.start_client()
    eventually(lambda: stack.proxy.duplicated_events > previous_replays)
    repeated = command(stack, "submit", meta, prompt=prompt, wait_seconds=0)
    assert repeated.status_code == 200
    assert repeated.json()["operation_id"] == operation_id
    pulled = command(stack, "get", meta, operation_id=operation_id)
    assert pulled.json()["result_id"] == result["result_id"]
    assert prompt in pulled.json()["result"]["text"]
    assert len(stack.queue()) == 1
    assert stack.peer.submissions == 1
    conflict = command(stack, "submit", meta, prompt="changed command", wait_seconds=0)
    assert conflict.status_code == 400
    request = json.loads(stack.proxy.posts[0])
    assert stack.http("POST", "/v1/operations", json={**request, "prompt": "changed"}).status_code == 409
    assert stack.peer.submissions == 1
    for name in ("client.sqlite3", "client.sock", "writer.lock"):
        assert (stack.directory / "client" / name).stat().st_mode & 0o777 == 0o600


def test_broker_crash_after_claim_without_task_handle_is_never_replayed(stack: Stack) -> None:
    stack.peer.release.clear()
    broker = stack.start_broker()
    request = {"operation_id": str(uuid4()), "flow_id": str(uuid4()), "prompt": "crash marker", "artifact_ids": []}
    assert stack.http("POST", "/v1/operations", json=request).status_code == 202
    assert stack.peer.received.wait(10)
    operation_id = request["operation_id"]
    running = stack.http("GET", f"/v1/operations/{operation_id}").json()
    assert running["state"] == "running" and running["remote_task_id"] is None
    broker.stop(crash=True)
    stack.peer.release.set()
    stack.start_broker()
    unknown = eventually(lambda: snapshot(stack, operation_id, "outcome_unknown"))
    assert unknown["remote_task_id"] is None and unknown["result_id"]
    for _ in range(3):
        duplicate = stack.http("POST", "/v1/operations", json=request)
        assert duplicate.status_code == 200
        assert duplicate.json()["state"] == "outcome_unknown"
        assert duplicate.json()["result_id"] == unknown["result_id"]
    # A new operation in another flow proves the restarted dispatcher is running.
    next_request = {**request, "operation_id": str(uuid4()), "flow_id": str(uuid4()), "prompt": "healthy marker"}
    assert stack.http("POST", "/v1/operations", json=next_request).status_code == 202
    eventually(lambda: snapshot(stack, next_request["operation_id"]))
    assert stack.peer.submissions == 2
    assert sum(message["messageId"] == operation_id for message in stack.peer.messages) == 1
    assert stack.peer.method_counts.get("ListTasks", 0) == 0


def test_native_lost_ack_stays_unknown_without_repeat_after_client_restart(stack: Stack) -> None:
    stack.env["INTEGRATION_QUEUE_LOSE_ACK"] = "1"
    stack.proxy.duplicate_sse = True
    stack.proxy.replay_from_zero = True
    stack.start_broker()
    client = stack.start_client()
    meta = origin()
    response = command(stack, "submit", meta, prompt="delayed result ambiguous native ACK", wait_seconds=0)
    assert response.status_code == 200
    operation_id = response.json()["operation_id"]
    eventually(lambda: snapshot(stack, operation_id))
    eventually(stack.queue)

    def unknown() -> dict[str, Any]:
        value = command(stack, "get", meta, operation_id=operation_id).json()
        return value if value.get("delivery_state") == "delivery_outcome_unknown" else {}

    before = eventually(unknown)
    assert before["result"]["text"]
    client.stop(crash=True)
    previous_replays = stack.proxy.duplicated_events
    stack.start_client()
    eventually(lambda: stack.proxy.duplicated_events > previous_replays)
    after = eventually(unknown)
    assert after["result_id"] == before["result_id"]
    assert len(stack.queue()) == 1
    assert stack.peer.submissions == 1


def test_device_flow_isolation_and_bounded_artifact_retention(stack: Stack) -> None:
    stack.env["HERMES_A2A_GATEWAY_BROKER_MAX_ARTIFACT_BYTES"] = "32"
    stack.env["HERMES_A2A_GATEWAY_BROKER_DEVICE_QUOTA_BYTES"] = "48"
    stack.start_broker()
    request = {
        "operation_id": str(uuid4()),
        "flow_id": str(uuid4()),
        "prompt": "same ID different device",
        "artifact_ids": [],
    }
    for token in (TOKEN_A, TOKEN_B):
        assert stack.http("POST", "/v1/operations", token=token, json=request).status_code == 202
        eventually(lambda token=token: snapshot(stack, request["operation_id"], token=token))
    assert stack.peer.submissions == 2
    assert len({message["contextId"] for message in stack.peer.messages}) == 2
    a_result = snapshot(stack, request["operation_id"])
    b_result = snapshot(stack, request["operation_id"], token=TOKEN_B)
    assert a_result["result_id"] != b_result["result_id"]
    receipt = {"operation_id": request["operation_id"], "result_id": a_result["result_id"], "status": "stored"}
    assert stack.http("POST", "/v1/receipts", token=TOKEN_B, json=receipt).status_code in {404, 409}
    for _ in range(2):
        assert stack.http("POST", "/v1/receipts", json=receipt).status_code == 200

    def upload(data: bytes, *, digest: str | None = None) -> httpx.Response:
        return stack.http(
            "POST",
            "/v1/artifacts",
            content=data,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Content-SHA256": digest or hashlib.sha256(data).hexdigest(),
            },
        )

    assert upload(b"x", digest="0" * 64).status_code == 422
    assert upload(b"x" * 33).status_code == 413
    data = b"inert integration attachment"
    artifact_response = upload(data)
    assert artifact_response.status_code in {200, 201}
    artifact = artifact_response.json()
    assert artifact["size"] == len(data)
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
    path = f"/v1/artifacts/{artifact['artifact_id']}"
    assert stack.http("GET", path).content == data
    assert stack.http("GET", path, token=TOKEN_B).status_code == 404
    assert upload(b"y" * 32).status_code in {413, 429}
    eventually(lambda: stack.http("GET", path).status_code in {404, 410}, timeout=5)
    assert upload(b"z" * 32).status_code in {200, 201}


def test_inline_deadline_and_terminal_sse_do_not_double_deliver(stack: Stack) -> None:
    stack.env["HERMES_A2A_GATEWAY_CLIENT_INLINE_WAIT_SECONDS"] = "3"
    stack.proxy.duplicate_sse = True
    stack.proxy.replay_from_zero = True
    stack.start_broker()
    client = stack.start_client()
    queued_operations: set[str] = set()
    inline_operations: set[str] = set()
    # Fake peer completes after 1.2 seconds; the middle case probes the boundary.
    for wait in (0.01, 1.2, 3.0):
        meta = origin()
        response = command(stack, "submit", meta, prompt="delayed result deadline marker", wait_seconds=wait)
        assert response.status_code == 200
        value = response.json()
        operation_id = value["operation_id"]
        if value["result_id"]:
            assert value["delivery_state"] == "inline_returned"
            inline_operations.add(operation_id)
        else:
            eventually(
                lambda operation_id=operation_id: any(
                    operation_id in item["input"][0]["text"] for item in stack.queue()
                )
            )
            queued_operations.add(operation_id)
        result = command(stack, "get", meta, operation_id=operation_id).json()
        assert result["state"] == "completed"
        assert result["result_id"]
    assert inline_operations and queued_operations
    client.stop(crash=True)
    previous_replays = stack.proxy.duplicated_events
    stack.start_client()
    eventually(lambda: stack.proxy.duplicated_events > previous_replays)
    queue = stack.queue()
    assert len(queue) == len(queued_operations)
    for operation_id in inline_operations:
        assert all(operation_id not in item["input"][0]["text"] for item in queue)
    for operation_id in queued_operations:
        assert sum(operation_id in item["input"][0]["text"] for item in queue) == 1
    assert stack.peer.submissions == 3


def test_sse_retention_gap_recovers_exact_known_operation_after_restart(stack: Stack) -> None:
    stack.env["HERMES_A2A_GATEWAY_BROKER_EVENT_TTL_SECONDS"] = "1"
    stack.peer.release.clear()
    stack.start_broker()
    client = stack.start_client()
    meta = origin()
    response = command(stack, "submit", meta, prompt="retention gap harmless marker", wait_seconds=0)
    assert response.status_code == 200
    operation_id = response.json()["operation_id"]
    assert stack.peer.received.wait(10)
    client.stop(crash=True)
    stack.peer.release.set()
    result = eventually(lambda: snapshot(stack, operation_id))

    def gap() -> dict[str, Any]:
        with httpx.stream(
            "GET",
            stack.broker_url + "/v1/events?after=0",
            headers={"Authorization": "Bearer " + TOKEN_A},
            timeout=1,
            trust_env=False,
        ) as response:
            if response.status_code == 410:
                response.read()
                return dict(response.json())
        return {}

    error = eventually(gap, timeout=5)
    assert error["error"]["code"] == "retention_gap"
    assert error["error"]["details"]["resume_after"] > 0
    stack.start_client()
    eventually(stack.queue)
    recovered = command(stack, "get", meta, operation_id=operation_id).json()
    assert recovered["result_id"] == result["result_id"]
    assert recovered["state"] == "completed"
    assert len(stack.queue()) == 1
    assert stack.peer.submissions == 1
    assert stack.peer.method_counts.get("ListTasks", 0) == 0


def test_client_upload_and_text_only_peer_input_contract(stack: Stack) -> None:
    import base64

    stack.env["HERMES_A2A_GATEWAY_BROKER_ARTIFACT_TTL_SECONDS"] = "10"
    stack.start_broker()
    stack.start_client()
    meta = origin()
    text = "harmless UTF-8 reference: café"
    valid = command(stack, "upload", meta, data=base64.b64encode(text.encode()).decode(), content_type="text/plain")
    assert valid.status_code == 200
    artifact = valid.json()
    response = command(
        stack, "submit", meta, prompt="Read reference", artifact_ids=[artifact["artifact_id"]], wait_seconds=0
    )
    assert response.status_code == 200
    result = eventually(lambda: snapshot(stack, response.json()["operation_id"]))
    assert result["state"] == "completed"
    assert stack.peer.submissions == 1
    outbound = "\n".join(part.get("text", "") for part in stack.peer.messages[0]["parts"])
    assert text in outbound and "untrusted" in outbound.lower()
    assert all("raw" not in part for part in stack.peer.messages[0]["parts"])
    for data, content_type in ((b"inert binary", "application/octet-stream"), (b"\xff", "text/plain")):
        uploaded = command(stack, "upload", meta, data=base64.b64encode(data).decode(), content_type=content_type)
        assert uploaded.status_code == 200
        request = {
            "operation_id": str(uuid4()),
            "flow_id": str(uuid4()),
            "prompt": "Reject unsupported reference",
            "artifact_ids": [uploaded.json()["artifact_id"]],
        }
        denied = stack.http("POST", "/v1/operations", json=request)
        assert denied.status_code == 422
        assert stack.http("GET", f"/v1/operations/{request['operation_id']}").status_code == 404
    assert stack.peer.submissions == 1


def test_unknown_read_then_exact_handle_resolution_delivers_final_once(stack: Stack) -> None:
    stack.peer.release_completion.clear()
    stack.proxy.duplicate_sse = True
    broker = stack.start_broker()
    stack.start_client()
    meta = origin()
    response = command(stack, "submit", meta, prompt="delayed result handle recovery", wait_seconds=0)
    assert response.status_code == 200
    operation_id = response.json()["operation_id"]

    def saved_handle() -> dict[str, Any]:
        value = snapshot(stack, operation_id, "running")
        return value if value.get("remote_task_id") else {}

    running = eventually(saved_handle)
    broker.stop(crash=True)
    stack.start_broker()
    unknown = eventually(lambda: snapshot(stack, operation_id, "outcome_unknown"))
    assert unknown["remote_task_id"] == running["remote_task_id"]
    assert unknown["result_id"]
    pulled = command(stack, "get", meta, operation_id=operation_id).json()
    assert pulled["state"] == "outcome_unknown"
    assert pulled["delivery_state"] not in {"inline_returned", "queued", "enqueueing"}
    assert stack.queue() == []
    stack.peer.release_completion.set()
    resolved = eventually(lambda: snapshot(stack, operation_id))
    assert resolved["result_id"] == unknown["result_id"]
    assert resolved["remote_task_id"] == unknown["remote_task_id"]
    queue = eventually(stack.queue)
    assert len(queue) == 1
    final = command(stack, "get", meta, operation_id=operation_id).json()
    assert final["state"] == "completed"
    assert final["result_id"] == resolved["result_id"]
    assert not final["snapshot_conflict"]
    assert stack.peer.submissions == 1
    assert stack.peer.method_counts.get("GetTask", 0) > 0
    assert stack.peer.method_counts.get("ListTasks", 0) == 0


@pytest.mark.parametrize("silence_seconds", [0, 11])
def test_disconnect_sensitive_peer_stream_stays_open_and_other_flow_progresses(
    stack: Stack, silence_seconds: int
) -> None:
    stack.peer.abort_on_disconnect = True
    stack.peer.release_completion.clear()
    stack.start_broker()
    operation_id = str(uuid4())
    request = {
        "operation_id": operation_id,
        "flow_id": str(uuid4()),
        "prompt": "delayed result disconnect-sensitive peer",
        "artifact_ids": [],
    }
    assert stack.http("POST", "/v1/operations", json=request).status_code == 202

    def saved_handle() -> dict[str, Any]:
        value = snapshot(stack, operation_id, "running")
        return value if value.get("remote_task_id") else {}

    eventually(saved_handle)
    other_id = str(uuid4())
    other = {**request, "operation_id": other_id, "flow_id": str(uuid4()), "prompt": "independent-flow marker"}
    assert stack.http("POST", "/v1/operations", json=other).status_code == 202
    eventually(lambda: snapshot(stack, other_id))
    assert snapshot(stack, operation_id, "running"), "retained stream must leave other flows responsive"
    timer = threading.Timer(silence_seconds, stack.peer.release_completion.set)
    timer.start()

    def terminal() -> dict[str, Any]:
        value = stack.http("GET", f"/v1/operations/{operation_id}").json()
        return value if value["state"] not in {"accepted", "running"} else {}

    try:
        result = eventually(terminal)
    finally:
        timer.cancel()
    assert result["state"] == "completed", result
    assert result["created_at"].endswith("Z") and result["updated_at"].endswith("Z")
    assert not stack.peer.premature_disconnect.is_set(), "broker closed SSE before peer completion"
    assert "disconnect-sensitive peer" in result["result"]["text"]
    assert stack.peer.submissions == 2


def test_stream_artifact_chunks_survive_status_only_terminal_event(stack: Stack) -> None:
    stack.peer.split_terminal_events = True
    stack.start_broker()
    request = {
        "operation_id": str(uuid4()),
        "flow_id": str(uuid4()),
        "prompt": "delayed result split-chunk marker",
        "artifact_ids": [],
    }
    assert stack.http("POST", "/v1/operations", json=request).status_code == 202
    result = eventually(lambda: snapshot(stack, request["operation_id"]))
    assert stack.peer.split_frames == 3
    assert result["result"]["text"] == "fake-marker turn=1 input=" + request["prompt"]
    assert stack.peer.submissions == 1
    assert stack.peer.method_counts.get("GetTask", 0) == 0, "terminal stream itself must retain all result content"
