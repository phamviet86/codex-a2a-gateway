from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from codex_a2a_gateway.codex_backend import BackendResult
from codex_a2a_gateway.gateway import create_gateway_app
from codex_a2a_gateway.inbound import InboundService
from codex_a2a_gateway.models import BridgeError, TaskRecord, now_iso, request_fingerprint
from codex_a2a_gateway.settings import Settings
from codex_a2a_gateway.store import Store


class FakeBackend:
    name = "app-server"
    supports_input_required = True

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.thread_inputs: list[str | None] = []
        self.canceled = False

    async def run(self, **kwargs: Any) -> BackendResult:
        if self.fail:
            raise BridgeError("app_server_closed", "unavailable")
        self.thread_inputs.append(kwargs["thread_id"])
        await kwargs["on_started"]("codex-thread-1", "codex-turn-1")
        await kwargs["on_update"]("Codex reply", False)
        return BackendResult("completed", "Codex reply", "codex-thread-1", "codex-turn-1")

    async def cancel(self, task_id: str, thread_id: str | None, turn_id: str | None) -> bool:
        del task_id, thread_id, turn_id
        self.canceled = True
        return True


class FakeCLIBackend(FakeBackend):
    name = "cli"
    supports_input_required = False


class InputThenCompleteBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def run(self, **kwargs: Any) -> BackendResult:
        self.calls += 1
        self.thread_inputs.append(kwargs["thread_id"])
        await kwargs["on_started"]("codex-thread-input", f"turn-{self.calls}")
        if self.calls == 1:
            return BackendResult("input_required", "Which target?", "codex-thread-input", "turn-1")
        await kwargs["on_update"]("Finished", False)
        return BackendResult("completed", "Finished", "codex-thread-input", "turn-2")


class BlockingBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.messages: list[str] = []

    async def run(self, **kwargs: Any) -> BackendResult:
        self.messages.append(kwargs["prompt"])
        await kwargs["on_started"]("blocking-thread", "blocking-turn")
        self.started.set()
        await self.release.wait()
        return BackendResult("completed", "done", "blocking-thread", "blocking-turn")


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        state_path=tmp_path / "state.sqlite",
        codex_workspace=tmp_path,
        max_turns=10,
        codex_timeout=2,
        **overrides,
    )


@pytest.mark.asyncio
async def test_session_persistence_and_message_idempotency(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    first_backend = FakeBackend()
    first = InboundService(settings, app_backend=first_backend, cli_backend=FakeCLIBackend())
    task, deduplicated = await first.submit("hello", context_id="ctx-1", message_id="message-1")
    completed = await first.wait(task.bridge_task_id)
    assert not deduplicated and completed.codex_turn_id == "codex-turn-1"
    await first.aclose()

    second_backend = FakeBackend()
    second = InboundService(settings, app_backend=second_backend, cli_backend=FakeCLIBackend())
    repeated, deduplicated = await second.submit("hello", context_id="ctx-1", message_id="message-1")
    assert deduplicated and repeated.bridge_task_id == task.bridge_task_id
    follow_up, _ = await second.submit("continue", context_id="ctx-1", message_id="message-2")
    await second.wait(follow_up.bridge_task_id)
    assert second_backend.thread_inputs == ["codex-thread-1"]
    await second.aclose()


@pytest.mark.asyncio
async def test_explicit_cli_fallback_does_not_advertise_input_required(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, cli_fallback=True)
    service = InboundService(
        settings,
        app_backend=FakeBackend(fail=True),
        cli_backend=FakeCLIBackend(),
    )
    task, _ = await service.submit("fallback", context_id="ctx-fallback", message_id="message-fallback")
    completed = await service.wait(task.bridge_task_id)
    context = service.store.get_context(context_id="ctx-fallback")
    assert completed.state == "completed" and context and context.backend == "cli"
    assert service.backend_capabilities["inputRequired"] is False
    await service.aclose()


def test_restart_marks_active_task_failed_but_keeps_thread_mapping(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = Store(settings.state_path)
    context = store.get_or_create_context(
        conversation_key="inbound:ctx-restart",
        context_id="ctx-restart",
        endpoint="codex://local",
        profile="inbound",
        direction="inbound",
        backend="app-server",
    )
    store.set_codex_thread(context.context_id, "thread-persisted", "app-server")
    now = now_iso()
    store.create_task(
        TaskRecord(
            bridge_task_id="task-restart",
            a2a_task_id="task-restart",
            context_id=context.context_id,
            conversation_key="inbound:ctx-restart",
            profile="inbound",
            endpoint="codex://local",
            request_id="request-restart",
            message_id="message-restart",
            idempotency_key="inbound:message-restart",
            request_fingerprint=request_fingerprint("work", context.context_id, "inbound"),
            mode="async",
            state="working",
            direction="inbound",
            created_at=now,
            updated_at=now,
        )
    )
    service = InboundService(settings, store=store, app_backend=FakeBackend(), cli_backend=FakeCLIBackend())
    recovered = service.get_task("task-restart")
    persisted = store.get_context(context_id="ctx-restart")
    assert recovered.state == "failed" and recovered.error_code == "gateway_restarted"
    assert persisted and persisted.codex_thread_id == "thread-persisted"


def test_restart_recovers_every_active_task_beyond_old_page_limit(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = Store(settings.state_path)
    context = store.get_or_create_context(
        conversation_key="inbound:bulk",
        context_id="bulk",
        endpoint="codex://local",
        profile="inbound",
        direction="inbound",
        backend="app-server",
    )
    for index in range(105):
        now = now_iso()
        store.create_task(
            TaskRecord(
                bridge_task_id=f"bulk-{index}",
                a2a_task_id=f"bulk-{index}",
                context_id=context.context_id,
                conversation_key="inbound:bulk",
                profile="inbound",
                endpoint="codex://local",
                request_id=f"request-{index}",
                message_id=f"message-{index}",
                idempotency_key=f"inbound:bulk-{index}",
                request_fingerprint=str(index),
                mode="async",
                state="working",
                direction="inbound",
                created_at=now,
                updated_at=now,
            )
        )
    service = InboundService(settings, store=store, app_backend=FakeBackend(), cli_backend=FakeCLIBackend())
    assert len(store.list_tasks(direction="inbound", state="failed", limit=100)) == 100
    assert store.counts()["active_tasks"] == 0
    assert service.get_task("bulk-104").error_code == "gateway_restarted"


@pytest.mark.asyncio
async def test_input_required_continues_same_task_and_infers_context(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    backend = InputThenCompleteBackend()
    service = InboundService(settings, app_backend=backend, cli_backend=FakeCLIBackend())
    first, _ = await service.submit("start", context_id="ctx-input", message_id="input-1")
    waiting = await service.wait(first.bridge_task_id)
    assert waiting.state == "input_required"

    continued, deduplicated = await service.submit(
        "web",
        context_id=None,
        message_id="input-2",
        task_id=first.bridge_task_id,
    )
    completed = await service.wait(continued.bridge_task_id)
    repeated, repeated_dedup = await service.submit(
        "web",
        context_id=None,
        message_id="input-2",
        task_id=first.bridge_task_id,
    )
    assert not deduplicated and repeated_dedup
    assert completed.bridge_task_id == first.bridge_task_id == repeated.bridge_task_id
    assert completed.state == "completed" and backend.thread_inputs == [None, "codex-thread-input"]
    with pytest.raises(BridgeError, match="different tasks"):
        await service.submit(
            "again",
            context_id="wrong-context",
            message_id="input-3",
            task_id=first.bridge_task_id,
        )
    await service.aclose()


@pytest.mark.asyncio
async def test_queued_cancellation_never_reaches_backend(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_concurrency=1)
    backend = BlockingBackend()
    service = InboundService(settings, app_backend=backend, cli_backend=FakeCLIBackend())
    first, _ = await service.submit("first", context_id="ctx-a", message_id="cancel-1")
    await backend.started.wait()
    second, _ = await service.submit("must-not-run", context_id="ctx-b", message_id="cancel-2")
    canceled, _ = await service.cancel(second.bridge_task_id)
    backend.release.set()
    await service.wait(first.bridge_task_id)
    await service.wait(second.bridge_task_id)
    assert canceled.state == "canceled"
    assert backend.messages == ["first"]
    await service.aclose()


@pytest.mark.asyncio
async def test_agent_card_and_inbound_a2a_lifecycle(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, inbound_token="secret")
    service = InboundService(settings, app_backend=FakeBackend(), cli_backend=FakeCLIBackend())
    app = create_gateway_app(settings, service=service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9910") as client:
        card = (await client.get("/.well-known/agent-card.json")).json()
        assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        assert "protocolVersion" not in card and "url" not in card and "preferredTransport" not in card
        assert card["capabilities"] == {"streaming": True, "pushNotifications": False}
        assert card["securitySchemes"]["bearerAuth"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
        assert card["securityRequirements"] == [{"schemes": {"bearerAuth": {"list": []}}}]

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "m-1",
                    "role": "ROLE_USER",
                    "contextId": "ctx-http",
                    "parts": [{"text": "hello", "mediaType": "text/plain"}],
                }
            },
        }
        assert (await client.post("/", json=request)).status_code == 401
        response = await client.post("/", json=request, headers={"Authorization": "Bearer secret"})
        task = response.json()["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        task_id = task["id"]

        get_response = await client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": task_id}},
            headers={"Authorization": "Bearer secret"},
        )
        assert get_response.json()["result"]["contextId"] == "ctx-http"
        missing_response = await client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 20, "method": "GetTask", "params": {"id": "missing-task"}},
            headers={"Authorization": "Bearer secret"},
        )
        missing_error = missing_response.json()["error"]
        assert missing_response.status_code == 200 and missing_error["code"] == -32001
        assert missing_error["data"][0]["reason"] == "TASK_NOT_FOUND"
        cancel_response = await client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 3, "method": "CancelTask", "params": {"id": task_id}},
            headers={"Authorization": "Bearer secret"},
        )
        cancel_error = cancel_response.json()["error"]
        assert cancel_error["code"] == -32002
        assert cancel_error["data"][0]["reason"] == "TASK_NOT_CANCELABLE"

        streaming_request = {
            **request,
            "id": 4,
            "method": "SendStreamingMessage",
            "params": {
                "message": {
                    "messageId": "m-2",
                    "role": "ROLE_USER",
                    "contextId": "ctx-http",
                    "parts": [{"text": "continue", "mediaType": "text/plain"}],
                }
            },
        }
        streaming = await client.post(
            "/",
            json=streaming_request,
            headers={"Authorization": "Bearer secret"},
        )
        assert streaming.headers["content-type"].startswith("text/event-stream")
        frames = [json.loads(line.removeprefix("data: ")) for line in streaming.text.splitlines() if line]
        result_keys = [set(frame["result"]) for frame in frames]
        assert result_keys[0] == {"task"}
        assert {"statusUpdate"} in result_keys
        assert all("final" not in frame["result"].get("statusUpdate", {}) for frame in frames)

        listed = await client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 5, "method": "ListTasks", "params": {"contextId": "ctx-http"}},
            headers={"Authorization": "Bearer secret"},
        )
        assert len(listed.json()["result"]["tasks"]) == 2
        first_page = await client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "ListTasks",
                "params": {
                    "contextId": "ctx-http",
                    "status": "TASK_STATE_COMPLETED",
                    "pageSize": 1,
                    "historyLength": 1,
                    "includeArtifacts": False,
                },
            },
            headers={"Authorization": "Bearer secret"},
        )
        page = first_page.json()["result"]
        assert page["pageSize"] == 1 and page["totalSize"] == 2 and page["nextPageToken"]
        assert "artifacts" not in page["tasks"][0] and page["tasks"][0]["history"] == []
        second_page = await client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "ListTasks",
                "params": {"contextId": "ctx-http", "pageSize": 1, "pageToken": page["nextPageToken"]},
            },
            headers={"Authorization": "Bearer secret"},
        )
        assert second_page.json()["result"]["tasks"][0]["id"] != page["tasks"][0]["id"]
    await service.aclose()


@pytest.mark.asyncio
async def test_rpc_rejects_browser_cross_origin_bad_host_and_non_json(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    service = InboundService(settings, app_backend=FakeBackend(), cli_backend=FakeCLIBackend())
    app = create_gateway_app(settings, service=service)
    request = {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9910") as client:
        assert (await client.post("/", content=json.dumps(request))).status_code == 415
        assert (await client.post("/", json=request, headers={"Host": "evil.example"})).status_code == 403
        assert (await client.post("/", json=request, headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await client.post("/", json=request, headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        assert (await client.post("/", json=request)).status_code == 200
    await service.aclose()


@pytest.mark.asyncio
async def test_duplicate_live_streams_broadcast_and_terminal_replay_finishes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    backend = BlockingBackend()
    service = InboundService(settings, app_backend=backend, cli_backend=FakeCLIBackend())
    first_queue = service.create_subscription()
    task, _ = await service.submit(
        "broadcast",
        context_id="ctx-broadcast",
        message_id="broadcast-1",
        subscriber=first_queue,
    )
    await backend.started.wait()
    second_queue = service.create_subscription()
    duplicate, deduplicated = await service.submit(
        "broadcast",
        context_id="ctx-broadcast",
        message_id="broadcast-1",
        subscriber=second_queue,
    )

    async def collect(record: TaskRecord, queue: asyncio.Queue[TaskRecord] | None = None) -> list[str]:
        return [update.state async for update in service.updates(record, queue)]

    first_stream = asyncio.create_task(collect(task, first_queue))
    second_stream = asyncio.create_task(collect(duplicate, second_queue))
    backend.release.set()
    first_states, second_states = await asyncio.wait_for(
        asyncio.gather(first_stream, second_stream),
        timeout=2,
    )
    assert deduplicated and first_states[-1] == second_states[-1] == "completed"

    terminal, terminal_dedup = await service.submit(
        "broadcast",
        context_id="ctx-broadcast",
        message_id="broadcast-1",
    )
    assert terminal_dedup
    assert await asyncio.wait_for(collect(terminal), timeout=0.2) == ["completed"]
    assert task.bridge_task_id not in service._subscribers
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsupported_part",
    [
        {"data": {"key": "value"}, "mediaType": "application/json"},
        {"file": {"uri": "https://example.invalid/file.txt"}, "mediaType": "text/plain"},
        {"text": "also text", "data": {"mixed": True}, "mediaType": "text/plain"},
    ],
)
async def test_mixed_or_unsupported_parts_never_run_backend(
    tmp_path: Path,
    unsupported_part: dict[str, Any],
) -> None:
    settings = make_settings(tmp_path)
    backend = FakeBackend()
    service = InboundService(settings, app_backend=backend, cli_backend=FakeCLIBackend())
    app = create_gateway_app(settings, service=service)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "mixed-parts",
                "role": "ROLE_USER",
                "parts": [{"text": "valid", "mediaType": "text/plain"}, unsupported_part],
            }
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:9910",
    ) as client:
        response = await client.post("/", json=request)
    assert response.json()["error"]["code"] == -32005
    assert response.json()["error"]["data"][0]["reason"] == "CONTENT_TYPE_NOT_SUPPORTED"
    assert backend.thread_inputs == [] and service.store.counts()["tasks"] == 0
    await service.aclose()


@pytest.mark.asyncio
async def test_total_admission_is_bounded_across_queued_and_running_tasks(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_concurrency=1, max_pending_tasks=2)
    backend = BlockingBackend()
    service = InboundService(settings, app_backend=backend, cli_backend=FakeCLIBackend())
    admitted: list[TaskRecord] = []
    first, _ = await service.submit("work-0", context_id="admit-0", message_id="admit-0")
    admitted.append(first)
    await backend.started.wait()
    second, _ = await service.submit("work-1", context_id="admit-1", message_id="admit-1")
    admitted.append(second)
    rejected = 0
    for index in range(2, 20):
        with pytest.raises(BridgeError) as captured:
            await service.submit(
                f"work-{index}",
                context_id=f"admit-{index}",
                message_id=f"admit-{index}",
            )
        assert captured.value.code == "server_overloaded" and captured.value.retryable
        rejected += 1
    assert rejected == 18
    assert service.store.count_active_inbound_tasks() == len(service._workers) == 2
    assert service.store.counts()["contexts"] == 2
    backend.release.set()
    await asyncio.gather(*(service.wait(task.bridge_task_id) for task in admitted))
    await service.aclose()


@pytest.mark.asyncio
async def test_chunked_request_body_stops_reading_as_soon_as_limit_is_exceeded(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_request_bytes=1024)
    service = InboundService(settings, app_backend=FakeBackend(), cli_backend=FakeCLIBackend())
    app = create_gateway_app(settings, service=service)
    produced: list[int] = []

    async def oversized_chunks() -> AsyncIterator[bytes]:
        for index in range(4):
            produced.append(index)
            yield b"x" * 600

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:9910",
    ) as client:
        response = await client.post(
            "/",
            content=oversized_chunks(),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413 and produced == [0, 1]
    assert service.store.counts()["tasks"] == 0
    await service.aclose()


@pytest.mark.asyncio
async def test_continuation_transaction_rolls_back_and_retry_succeeds(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    backend = InputThenCompleteBackend()
    service = InboundService(settings, app_backend=backend, cli_backend=FakeCLIBackend())
    task, _ = await service.submit("start", context_id="ctx-atomic", message_id="atomic-1")
    waiting = await service.wait(task.bridge_task_id)
    assert waiting.state == "input_required"
    before = service.store.get_context(context_id="ctx-atomic")

    with sqlite3.connect(settings.state_path) as con:
        con.execute(
            "CREATE TRIGGER fail_continuation BEFORE UPDATE OF state ON tasks "
            "WHEN NEW.state='queued' BEGIN SELECT RAISE(ABORT, 'fault injection'); END"
        )
    with pytest.raises(BridgeError):
        await service.submit(
            "answer",
            context_id=None,
            message_id="atomic-2",
            task_id=task.bridge_task_id,
        )
    after_fault = service.store.get_context(context_id="ctx-atomic")
    assert service.store.get_inbound_message("atomic-2") is None
    assert service.get_task(task.bridge_task_id).state == "input_required"
    assert before and after_fault and before.turn_count == after_fault.turn_count

    with sqlite3.connect(settings.state_path) as con:
        con.execute("DROP TRIGGER fail_continuation")
    retried, _ = await service.submit(
        "answer",
        context_id=None,
        message_id="atomic-2",
        task_id=task.bridge_task_id,
    )
    assert (await service.wait(retried.bridge_task_id)).state == "completed"
    await service.aclose()


@pytest.mark.asyncio
async def test_post_commit_worker_fault_is_repaired_by_idempotent_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    backend = InputThenCompleteBackend()
    service = InboundService(settings, app_backend=backend, cli_backend=FakeCLIBackend())
    task, _ = await service.submit("start", context_id="ctx-worker-fault", message_id="fault-1")
    assert (await service.wait(task.bridge_task_id)).state == "input_required"
    original_spawn = service._spawn_worker
    injected = False

    def fail_once(task_id: str, message: str) -> None:
        nonlocal injected
        if not injected:
            injected = True
            raise RuntimeError("post-commit scheduling fault")
        original_spawn(task_id, message)

    monkeypatch.setattr(service, "_spawn_worker", fail_once)
    with pytest.raises(RuntimeError, match="post-commit"):
        await service.submit(
            "answer",
            context_id=None,
            message_id="fault-2",
            task_id=task.bridge_task_id,
        )
    assert service.store.get_inbound_message("fault-2") is not None
    assert service.get_task(task.bridge_task_id).state == "queued"
    await service.aclose()

    recovered = InboundService(settings, app_backend=backend, cli_backend=FakeCLIBackend())
    interrupted = recovered.get_task(task.bridge_task_id)
    assert interrupted.state == "failed" and interrupted.error_code == "gateway_restarted_before_start"
    replayed, deduplicated = await recovered.submit(
        "answer",
        context_id=None,
        message_id="fault-2",
        task_id=task.bridge_task_id,
    )
    assert deduplicated and (await recovered.wait(replayed.bridge_task_id)).state == "completed"
    await recovered.aclose()


@pytest.mark.asyncio
async def test_bearer_auth_scheme_is_case_insensitive_but_token_is_exact(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, inbound_token="CaseSensitiveToken")
    service = InboundService(settings, app_backend=FakeBackend(), cli_backend=FakeCLIBackend())
    app = create_gateway_app(settings, service=service)
    request = {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:9910",
    ) as client:
        accepted = await client.post("/", json=request, headers={"Authorization": "bearer CaseSensitiveToken"})
        rejected = await client.post("/", json=request, headers={"Authorization": "BEARER casesensitivetoken"})
    assert accepted.status_code == 200 and "result" in accepted.json()
    assert rejected.status_code == 401
    await service.aclose()
