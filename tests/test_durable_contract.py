from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_codex_backends import FakeProcess
from test_hermes_plugin import FakeContext
from test_inbound_gateway import BlockingBackend, FakeBackend, InputThenCompleteBackend, make_settings
from test_recovery import _unknown_task

from codex_a2a_gateway.a2a import A2AClient
from codex_a2a_gateway.codex_backend import AppServerBackend, BackendResult
from codex_a2a_gateway.core import BridgeService
from codex_a2a_gateway.gateway import create_gateway_app
from codex_a2a_gateway.hermes_plugin.asset import tools
from codex_a2a_gateway.inbound import InboundService
from codex_a2a_gateway.models import A2AError, A2ATaskResult, BridgeError
from codex_a2a_gateway.settings import Settings


class Peer:
    rpc_url = "http://127.0.0.1:9999/"
    tenant = ""
    parse_task = A2AClient.parse_task
    parse_stream_event = A2AClient.parse_stream_event

    def __init__(self) -> None:
        self.discovery = asyncio.Event()
        self.discovery.set()
        self.finish = asyncio.Event()
        self.calls: list[tuple[str, str, str | None]] = []
        self.pages: dict[str, dict[str, Any]] = {"": {"tasks": []}}
        self.remote: dict[str, Any] = {}
        self.fail_before_ack = False
        self.input = False

    async def discover(self, **_: Any) -> dict[str, Any]:
        await self.discovery.wait()
        return {}

    async def aclose(self) -> None:
        pass

    async def stream_message(self, message: str, context_id: str, message_id: str, **kwargs: Any):
        self.calls.append((context_id, message_id, kwargs.get("task_id")))
        if self.fail_before_ack:
            raise A2AError("a2a_transport_ambiguous", "ack lost")
        task = {
            "id": kwargs.get("task_id") or "remote-" + message_id,
            "contextId": context_id,
            "metadata": {"requestMessageId": message_id},
            "status": {"state": "TASK_STATE_WORKING"},
        }
        self.remote[task["id"]] = task
        yield {"task": task}
        await self.finish.wait()
        task = {
            **task,
            "status": {"state": "TASK_STATE_INPUT_REQUIRED" if self.input else "TASK_STATE_COMPLETED"},
            "artifacts": [{"parts": [{"text": message}]}],
        }
        self.remote[task["id"]] = task
        yield {"task": task}

    async def list_tasks(self, *, page_token: str = "", **_: Any) -> dict[str, Any]:
        return self.pages[page_token]

    async def get_task(self, task_id: str) -> A2ATaskResult:
        if task_id not in self.remote:
            raise A2AError("a2a_task_not_found", "missing")
        return self.parse_task(self.remote[task_id])


def bridge(tmp_path: Path, peer: Peer) -> BridgeService:
    return BridgeService(
        Settings(
            endpoint=peer.rpc_url.rstrip("/"),
            state_path=tmp_path / "out.sqlite",
            conversation_dir=tmp_path / "conversations",
        ),
        client=peer,
    )  # type: ignore[arg-type]


async def finish_worker(service: BridgeService, task_id: str) -> None:
    worker = service._workers.get(task_id)
    if worker:
        await asyncio.wait_for(asyncio.shield(worker), 1)


async def test_handle_exists_before_discovery_and_restart_never_resends(tmp_path: Path) -> None:
    peer = Peer()
    peer.discovery.clear()
    service = bridge(tmp_path, peer)
    first = await asyncio.wait_for(
        service.chat("mutation", mode="async", idempotency_key="one", origin={"question_id": "q1"}), 0.2
    )
    assert first["bridge_task_id"] and first["message_id"] and not peer.calls
    await service.aclose()
    restarted = bridge(tmp_path, peer)
    try:
        current = await restarted.task_get(first["bridge_task_id"], refresh=False)
        assert current["state"] == "outcome_unknown" and current["origin"] == {"question_id": "q1"}
        replay = await restarted.chat("mutation", mode="async", idempotency_key="one")
        assert replay["deduplicated"] and replay["bridge_task_id"] == first["bridge_task_id"]
        assert not peer.calls
    finally:
        await restarted.aclose()


@pytest.mark.parametrize("ack", [False, True])
async def test_timeout_late_result_exact_recovery_without_execution(tmp_path: Path, ack: bool) -> None:
    peer = Peer()
    peer.fail_before_ack = not ack
    service = bridge(tmp_path, peer)
    task = await service.chat("work", mode="async", idempotency_key="work")
    if ack:
        waited = await service.task_wait(task["bridge_task_id"], timeout=1)
        assert waited["timed_out"] and waited["state"] == "working"
    else:
        await finish_worker(service, task["bridge_task_id"])
    saved = service.store.get_task(task["bridge_task_id"])
    assert saved
    await service.aclose()
    remote = {
        "id": saved.a2a_task_id or "late",
        "contextId": saved.context_id,
        "metadata": {"requestMessageId": saved.message_id},
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"text": "late answer"}]}],
    }
    peer.pages = {"": {"tasks": [], "nextPageToken": "second"}, "second": {"tasks": [remote]}}
    peer.remote[remote["id"]] = remote
    recovered = bridge(tmp_path, peer)
    try:
        result = await recovered.task_get(saved.bridge_task_id)
        assert result["state"] == "completed" and result["result"] == "late answer"
        assert len(peer.calls) == 1
    finally:
        await recovered.aclose()


@pytest.mark.parametrize("kind", ["unrelated", "ambiguous", "wrong_context", "repeated_page"])
async def test_recovery_never_infers_by_context_unique_candidate(tmp_path: Path, kind: str) -> None:
    peer = Peer()
    service = bridge(tmp_path, peer)
    task = _unknown_task(service, "ctx")
    raw = {
        "id": "candidate",
        "contextId": "ctx",
        "metadata": {"requestMessageId": task.message_id},
        "status": {"state": "TASK_STATE_COMPLETED"},
    }
    if kind == "unrelated":
        raw["metadata"] = {}
    if kind == "wrong_context":
        raw["contextId"] = "other"
    peer.pages[""] = {"tasks": [raw]}
    if kind == "ambiguous":
        peer.pages[""]["tasks"].append({**raw, "id": "another"})
    if kind == "repeated_page":
        peer.pages[""]["nextPageToken"] = "loop"
        peer.pages["loop"] = peer.pages[""]
    try:
        result = await service.task_get(task.bridge_task_id)
        assert result["state"] == "outcome_unknown" and not result["a2a_task_id"] and not peer.calls
    finally:
        await service.aclose()


async def test_concurrent_independent_jobs_busy_context_and_context_only_followup(tmp_path: Path) -> None:
    peer = Peer()
    service = bridge(tmp_path, peer)
    try:
        first, second = await asyncio.gather(service.chat("first", mode="async"), service.chat("second", mode="async"))
        assert first["context_id"] != second["context_id"]
        with pytest.raises(BridgeError, match="unresolved"):
            await service.chat("unsafe followup", context_id=first["context_id"])
        peer.finish.set()
        await asyncio.gather(*(finish_worker(service, t["bridge_task_id"]) for t in (first, second)))
        followup = await service.chat("followup", context_id=first["context_id"], mode="sync")
        assert followup["conversation_key"] == first["conversation_key"] and followup["state"] == "completed"
        assert len(peer.calls) == 3
    finally:
        await service.aclose()


async def test_input_continuation_same_handle_and_attempt_dedup(tmp_path: Path) -> None:
    peer = Peer()
    peer.input = True
    peer.finish.set()
    service = bridge(tmp_path, peer)
    try:
        first = await service.chat("question", mode="sync", idempotency_key="initial")
        peer.input = False
        second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync", idempotency_key="answer")
        assert second["bridge_task_id"] == first["bridge_task_id"]
        assert second["a2a_task_id"] == first["a2a_task_id"] and second["state"] == "completed"
        assert peer.calls[-1][2] == first["a2a_task_id"]
        for key, message in [("answer", "answer"), ("initial", "question")]:
            replay = await service.chat(message, idempotency_key=key)
            assert replay["deduplicated"]
        assert len(peer.calls) == 2
    finally:
        await service.aclose()


async def test_wrong_result_and_receipt_rejected_duplicate_result_immutable(tmp_path: Path) -> None:
    peer = Peer()
    peer.finish.set()
    service = bridge(tmp_path, peer)
    try:
        task = await service.chat("answer", mode="sync", origin={"question_id": "q"})
        with pytest.raises(BridgeError, match="saved task"):
            service._apply_remote(
                task["bridge_task_id"], A2ATaskResult(task_id="wrong", context_id=task["context_id"], state="completed")
            )
        service._apply_remote(
            task["bridge_task_id"],
            A2ATaskResult(
                task_id=task["a2a_task_id"], context_id=task["context_id"], state="completed", text="overwrite"
            ),
        )
        with pytest.raises(BridgeError, match="different originating"):
            await service.task_get(task["bridge_task_id"], expected_origin={"question_id": "other"})
        with pytest.raises(BridgeError, match="receipt"):
            await service.task_get(task["bridge_task_id"], acknowledge_result_id="wrong")
        for _ in range(2):
            result = await service.task_get(
                task["bridge_task_id"], acknowledge_result_id=task["result_id"], expected_origin={"question_id": "q"}
            )
            assert result["acknowledged"] and result["result"] == "answer"
        assert len(peer.calls) == 1
    finally:
        await service.aclose()


async def test_worker_cannot_delegate_back_to_hermes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    peer = Peer()
    service = bridge(tmp_path, peer)
    monkeypatch.setenv("CODEX_A2A_GATEWAY_WORKER_TASK_ID", "parent")
    try:
        with pytest.raises(BridgeError, match="delegate back"):
            await service.chat("delegate again")
        assert not peer.calls
    finally:
        await service.aclose()


async def test_inbound_returns_second_handle_while_first_context_turn_runs(tmp_path: Path) -> None:
    backend = BlockingBackend()
    service = InboundService(make_settings(tmp_path), app_backend=backend)
    try:
        first, _ = await service.submit("first", context_id="ctx", message_id="first")
        await backend.started.wait()
        second, _ = await asyncio.wait_for(service.submit("second", context_id="ctx", message_id="second"), 0.2)
        assert second.state == "queued" and backend.messages == ["first"]
        waited = await service.wait(first.bridge_task_id, timeout=0.01)
        assert waited.state == "working"
        backend.release.set()
        assert (await service.wait(second.bridge_task_id)).state == "completed"
        assert backend.messages == ["first", "second"]
    finally:
        await service.aclose()


async def test_inbound_restart_read_recovery_keeps_exact_turn_and_never_runs_again(tmp_path: Path) -> None:
    backend = BlockingBackend()
    settings = make_settings(tmp_path)
    service = InboundService(settings, app_backend=backend)
    first, _ = await service.submit("mutation", context_id="ctx", message_id="request")
    await backend.started.wait()
    await service.aclose()

    class Recovering(FakeBackend):
        async def recover(self, thread_id: str, turn_id: str) -> BackendResult:
            assert (thread_id, turn_id) == ("blocking-thread", "blocking-turn")
            return BackendResult("completed", "late result", thread_id, turn_id)

    recovered_backend = Recovering()
    restarted = InboundService(settings, app_backend=recovered_backend)
    try:
        replay, dedup = await restarted.submit("mutation", context_id="ctx", message_id="request")
        assert dedup and replay.state == "outcome_unknown"
        result = await restarted.refresh_task(first.bridge_task_id)
        assert result.state == "completed" and result.result_text == "late result"
        assert recovered_backend.thread_inputs == []
    finally:
        await restarted.aclose()


@pytest.mark.parametrize("wrong", [False, True])
async def test_app_server_recovery_is_read_only_and_requires_exact_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrong: bool
) -> None:
    process = FakeProcess(
        [
            {"id": 1, "result": {}},
            {
                "id": 2,
                "result": {
                    "thread": {
                        "id": "wrong" if wrong else "thread",
                        "turns": [
                            {"id": "turn", "status": "completed", "items": [{"type": "agentMessage", "text": "answer"}]}
                        ],
                    }
                },
            },
        ]
    )

    async def start(*_: Any, **__: Any):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    backend = AppServerBackend(codex_bin="codex", workspace=tmp_path, timeout=2, approval_policy="never")
    result = await backend.recover("thread", "turn")
    assert (result is None) == wrong
    assert [frame["method"] for frame in process.stdin.frames] == ["initialize", "initialized", "thread/read"]


async def test_plugin_gateway_roundtrip_lost_ack_restart_dedup_input_and_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = InputThenCompleteBackend()
    service = InboundService(make_settings(tmp_path), app_backend=backend)
    app = create_gateway_app(make_settings(tmp_path), service=service)
    ctx = FakeContext()
    loop = asyncio.get_running_loop()
    lose_ack = True
    methods: list[str] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9910") as client:

        def request(endpoint: str, payload: dict[str, Any], timeout: float, **_: Any):
            nonlocal lose_ack
            methods.append(payload["method"])
            response = asyncio.run_coroutine_threadsafe(client.post("/", json=payload), loop).result(2)
            value = response.json()
            if "error" in value:
                raise tools.GatewayRejection(value["error"]["message"])
            if payload["method"] == "SendMessage" and lose_ack:
                lose_ack = False
                raise TimeoutError("lost ack after commit")
            return value

        monkeypatch.setattr(tools, "_request", request)
        try:
            args = {"message": "work", "message_id": "first", "origin": {"question_id": "q"}}
            initial = json.loads(await asyncio.to_thread(tools._call, ctx, args))
            assert initial.get("state") == "outcome_unknown", initial
            handle = initial["handle"]
            # Simulate a Hermes tool-session restart using persisted state only.
            reloaded = FakeContext()
            reloaded.state.values = json.loads(json.dumps(ctx.state.values))
            duplicate = json.loads(await asyncio.to_thread(tools._call, reloaded, args))
            assert duplicate["deduplicated"] and duplicate["handle"] == handle
            got = json.loads(await asyncio.to_thread(tools._get_tool, reloaded, {"task_id": handle}))
            assert got["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
            answer = {"message": "target", "task_id": handle, "message_id": "answer"}
            await asyncio.to_thread(tools._call, reloaded, answer)
            final = json.loads(await asyncio.to_thread(tools._wait, reloaded, {"task_id": handle, "timeout": 1}))
            assert final["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
            receipt = final["task"]["metadata"]["resultId"]
            for _ in range(2):
                ack = json.loads(
                    await asyncio.to_thread(
                        tools._get_tool,
                        reloaded,
                        {"task_id": handle, "acknowledge_result_id": receipt, "expected_origin": {"question_id": "q"}},
                    )
                )
                assert ack["handleInfo"]["acknowledged_result_id"] == receipt
            assert backend.calls == 2 and methods.count("SendMessage") == 2
        finally:
            await service.aclose()


@pytest.mark.parametrize("proof", [False, True])
def test_plugin_explicit_resume_requires_receiver_proof(monkeypatch: pytest.MonkeyPatch, proof: bool) -> None:
    ctx = FakeContext()
    methods: list[str] = []
    request_id = ""

    def request(endpoint: str, payload: dict[str, Any], timeout: float, **_: Any):
        nonlocal request_id
        methods.append(payload["method"])
        if payload["method"] == "SendMessage":
            message = payload["params"]["message"]
            request_id = message["messageId"]
            task = {
                "id": "remote",
                "contextId": message["contextId"],
                "status": {"state": "TASK_STATE_SUBMITTED"},
                "metadata": {"requestMessageId": request_id},
            }
            return {"result": {"task": task}}
        saved = tools._handles(ctx)[0]
        return {
            "result": {
                "id": "remote",
                "contextId": saved["context_id"],
                "status": {"state": "TASK_STATE_FAILED"},
                "metadata": {
                    "requestMessageId": request_id,
                    "errorCode": "gateway_restarted_before_start" if proof else "gateway_restarted",
                },
            }
        }

    monkeypatch.setattr(tools, "_request", request)
    args = {"message": "mutation", "message_id": "stable"}
    initial = json.loads(tools._call(ctx, args))
    resumed = json.loads(tools._call(ctx, {**args, "resume": True}))
    assert resumed["ok"] == proof
    assert resumed["handle"] == initial["handle"]
    assert methods.count("SendMessage") == (2 if proof else 1)


@pytest.mark.parametrize("kind", ["wrong_id", "wrong_context", "wrong_request"])
def test_plugin_rejects_misdirected_results(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    ctx = FakeContext()
    handle = {
        "handle_id": "local",
        "remote_task_id": "remote",
        "context_id": "ctx",
        "message_id": "message",
        "endpoint": "http://127.0.0.1:9910",
        "state": "TASK_STATE_WORKING",
    }
    tools._save_handle(ctx, handle)
    task = {
        "id": "remote",
        "contextId": "ctx",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "metadata": {"requestMessageId": "message"},
        "artifacts": [{"parts": [{"text": "wrong answer"}]}],
    }
    if kind == "wrong_id":
        task["id"] = "wrong"
    if kind == "wrong_context":
        task["contextId"] = "wrong"
    if kind == "wrong_request":
        task["metadata"]["requestMessageId"] = "wrong"
    monkeypatch.setattr(tools, "_request", lambda *args, **kwargs: {"result": task})
    result = json.loads(tools._get_tool(ctx, {"task_id": "local"}))
    assert not result["ok"] and "task" not in result
    assert tools._handles(ctx)[0]["remote_task_id"] == "remote"


def test_plugin_capacity_does_not_evict_durable_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakeContext()
    monkeypatch.setattr(tools, "MAX_HANDLES", 1)
    tools._save_handle(ctx, {"handle_id": "original", "message_id": "original"})
    monkeypatch.setattr(tools, "_request", lambda *args, **kwargs: pytest.fail("must not send"))
    result = json.loads(tools._call(ctx, {"message": "second"}))
    assert not result["ok"] and "capacity" in result["error"]
    assert tools._handle_ids(ctx) == ["original"]


async def test_legacy_disk_record_cannot_supply_request_correlation(tmp_path: Path) -> None:
    import time

    peer = Peer()
    service = bridge(tmp_path, peer)
    task = _unknown_task(service, "ctx")
    folder = tmp_path / "conversations"
    folder.mkdir()
    (folder / "ctx.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {"ts": time.time(), "role": "user", "text": "same prompt", "task_id": "unrelated"},
                {"ts": time.time(), "role": "agent", "text": "wrong result", "task_id": "unrelated"},
            ]
        )
    )
    try:
        assert (await service.task_get(task.bridge_task_id))["state"] == "outcome_unknown"
    finally:
        await service.aclose()


async def test_missing_acknowledged_hermes_task_stays_unknown(tmp_path: Path) -> None:
    peer = Peer()
    service = bridge(tmp_path, peer)
    task = await service.chat("mutation", mode="async")
    await service._changed[task["bridge_task_id"]].wait()
    saved = service.store.get_task(task["bridge_task_id"])
    assert saved and saved.a2a_task_id
    await service.aclose()
    peer.remote.clear()
    restarted = bridge(tmp_path, peer)
    try:
        result = await restarted.task_get(saved.bridge_task_id)
        assert result["state"] == "outcome_unknown" and result["a2a_task_id"] == saved.a2a_task_id
        assert len(peer.calls) == 1
    finally:
        await restarted.aclose()


async def test_inbound_pre_turn_ack_restart_does_not_reexecute(tmp_path: Path) -> None:
    class PreAck(FakeBackend):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        async def run(self, **kwargs: Any) -> BackendResult:
            await kwargs["on_started"]("thread", None)
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    backend = PreAck()
    settings = make_settings(tmp_path)
    service = InboundService(settings, app_backend=backend)
    task, _ = await service.submit("mutation", context_id="ctx", message_id="message")
    await backend.started.wait()
    await service.aclose()
    after = FakeBackend()
    restarted = InboundService(settings, app_backend=after)
    try:
        replay, dedup = await restarted.submit("mutation", context_id="ctx", message_id="message")
        result = await restarted.refresh_task(task.bridge_task_id)
        assert dedup and replay.state == result.state == "outcome_unknown" and not after.thread_inputs
        assert InboundService.task_payload(result)["metadata"]["bridgeState"] == "outcome_unknown"
    finally:
        await restarted.aclose()


async def test_backend_refuses_other_turn_notification(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = FakeProcess(
        [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread"}}},
            {"id": 3, "result": {"turn": {"id": "turn"}}},
            {
                "method": "turn/completed",
                "params": {"threadId": "thread", "turn": {"id": "unrelated", "status": "completed"}},
            },
        ]
    )

    async def start(*_: Any, **__: Any):
        return process

    async def ignore(*_: Any):
        pass

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    backend = AppServerBackend(codex_bin="codex", workspace=tmp_path, timeout=2, approval_policy="never")
    with pytest.raises(BridgeError, match="another thread/turn"):
        await backend.run(
            task_id="task", prompt="work", thread_id=None, message_id="message", on_started=ignore, on_update=ignore
        )


async def test_replaying_old_message_cannot_start_newer_queued_continuation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    backend = InputThenCompleteBackend()
    service = InboundService(settings, app_backend=backend)
    task, _ = await service.submit("old question", context_id="ctx", message_id="old")
    await service.wait(task.bridge_task_id)
    # Commit the continuation but simulate restart before its worker is scheduled.
    from codex_a2a_gateway.models import request_fingerprint

    service.store.continue_inbound_task_atomic(
        bridge_task_id=task.bridge_task_id,
        context_id="ctx",
        message_id="new",
        request_fingerprint=request_fingerprint("new answer\n{}", "ctx", "inbound"),
        max_turns=10,
    )
    await service.aclose()
    later = FakeBackend()
    restarted = InboundService(settings, app_backend=later)
    try:
        replay, dedup = await restarted.submit("old question", context_id="ctx", message_id="old")
        assert dedup and replay.error_code == "gateway_restarted_before_start"
        await asyncio.sleep(0)
        assert later.thread_inputs == []
        current, dedup = await restarted.submit(
            "new answer", context_id="ctx", message_id="new", task_id=task.bridge_task_id
        )
        assert dedup and (await restarted.wait(current.bridge_task_id)).state == "completed"
        assert len(later.thread_inputs) == 1
    finally:
        await restarted.aclose()


async def test_lost_continuation_ack_cannot_reuse_uncorrelated_old_task_snapshot(tmp_path: Path) -> None:
    peer = Peer()
    peer.input = True
    peer.finish.set()
    service = bridge(tmp_path, peer)
    try:
        original = await service.chat("question", mode="sync")
        peer.remote[original["a2a_task_id"]].pop("metadata")
        peer.fail_before_ack = True
        continued = await service.chat("answer", task_id=original["bridge_task_id"], mode="sync")
        assert continued["state"] == "outcome_unknown"
        result = await service.task_get(original["bridge_task_id"])
        assert result["state"] == "outcome_unknown" and not result["result"]
        assert "exact requestMessageId" in result["refresh_warning"]
        assert len(peer.calls) == 2
    finally:
        await service.aclose()
