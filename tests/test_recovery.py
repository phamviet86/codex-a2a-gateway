from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codex_hermes_a2a_bridge.core import BridgeService
from codex_hermes_a2a_bridge.models import A2AError, TaskRecord, now_iso, request_fingerprint
from codex_hermes_a2a_bridge.settings import Settings


def _unknown_task(service: BridgeService, context_id: str, bridge_task_id: str = "bt-unknown") -> TaskRecord:
    context = service.store.get_or_create_context(
        conversation_key="recover-conversation",
        context_id=context_id,
        endpoint=service.settings.endpoint + "/",
    )
    now = now_iso()
    task = TaskRecord(
        bridge_task_id=bridge_task_id,
        context_id=context.context_id,
        conversation_key=context.conversation_key,
        endpoint=context.endpoint,
        request_id="request-recovery",
        message_id="message-recovery",
        request_fingerprint=request_fingerprint("late work", context.context_id, "default"),
        mode="sync",
        state="outcome_unknown",
        error_code="a2a_transport_ambiguous",
        error_message="ReadTimeout",
        created_at=now,
        updated_at=now,
    )
    return service.store.create_task(task)


@pytest.mark.asyncio
async def test_sync_uses_stream_and_completes_after_initial_timeout_without_resend(fake_a2a, tmp_path: Path) -> None:
    service = BridgeService(
        Settings(
            endpoint=fake_a2a.endpoint,
            state_path=tmp_path / "state.sqlite",
            conversation_dir=tmp_path / "conversations",
            correlation_timeout=30,
        )
    )
    try:
        initial = await service.chat(
            "delayed result",
            conversation_key="delayed",
            mode="sync",
            timeout=1,
        )
        assert initial["state"] == "working"
        assert initial["a2a_task_id"]
        completed = await service.task_wait(initial["bridge_task_id"], timeout=3)
        assert completed["state"] == "completed"
        assert "delayed result" in completed["result"]
        assert fake_a2a.method_counts.get("SendStreamingMessage") == 1
        assert fake_a2a.method_counts.get("SendMessage", 0) == 0
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_transport_drop_before_task_id_retries_once_with_durable_message_dedup(fake_a2a, tmp_path: Path) -> None:
    service = BridgeService(
        Settings(
            endpoint=fake_a2a.endpoint,
            state_path=tmp_path / "state.sqlite",
            conversation_dir=tmp_path / "conversations",
            correlation_timeout=30,
        )
    )
    try:
        result = await service.chat(
            "drop before first event",
            conversation_key="durable-retry",
            mode="sync",
            timeout=5,
        )
        assert result["state"] == "completed"
        assert result["result"] == "durable retry result"
        assert fake_a2a.method_counts["SendStreamingMessage"] == 2
        assert len(fake_a2a.tasks) == 1
        assert fake_a2a.turns[result["context_id"]] == 1
        events = service.store.list_events(result["bridge_task_id"])
        assert any(event["event_type"] == "safe_retry" for event in events)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_timeout_before_task_id_recovers_late_result_without_redispatch(fake_a2a, tmp_path: Path) -> None:
    service = BridgeService(
        Settings(
            endpoint=fake_a2a.endpoint,
            state_path=tmp_path / "state.sqlite",
            conversation_dir=tmp_path / "conversations",
            correlation_timeout=30,
        )
    )
    try:
        original_stream = service.client.stream_message
        attempts = 0

        async def timeout_once(message: str, context_id: str, message_id: str, *, timeout: float):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                accepted = fake_a2a._make_task(
                    {
                        "messageId": message_id,
                        "contextId": context_id,
                        "parts": [{"text": message}],
                    }
                )
                accepted["artifacts"] = [{"artifactId": "a1", "parts": [{"text": "late durable result"}]}]
                raise A2AError("a2a_timeout", "simulated timeout after durable accept")
            async for event in original_stream(message, context_id, message_id, timeout=timeout):
                yield event

        service.client.stream_message = timeout_once  # type: ignore[method-assign]
        completed = await service.chat(
            "timeout before first event",
            conversation_key="durable-timeout",
            mode="sync",
            timeout=1,
        )
        assert completed["state"] == "completed"
        assert completed["result"] == "late durable result"
        assert attempts == 2
        assert fake_a2a.method_counts["SendStreamingMessage"] == 1
        assert len(fake_a2a.tasks) == 1
        assert fake_a2a.turns[completed["context_id"]] == 1
        events = service.store.list_events(completed["bridge_task_id"])
        assert any(event["event_type"] == "safe_retry" and "a2a_timeout" in event["message"] for event in events)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_outcome_unknown_recovers_by_context_list_without_resend(fake_a2a, tmp_path: Path) -> None:
    context_id = "codex-recover-list"
    service = BridgeService(
        Settings(
            endpoint=fake_a2a.endpoint,
            state_path=tmp_path / "state.sqlite",
            conversation_dir=tmp_path / "conversations",
        )
    )
    try:
        task = _unknown_task(service, context_id)
        remote = fake_a2a._make_task({"contextId": context_id, "parts": [{"text": "late work"}]})
        result = await service.task_get(task.bridge_task_id)
        assert result["state"] == "completed"
        assert result["a2a_task_id"] == remote["id"]
        assert result["recovery_strategy"] == "a2a_list"
        assert fake_a2a.method_counts.get("SendMessage", 0) == 0
        assert fake_a2a.method_counts.get("SendStreamingMessage", 0) == 0
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_outcome_unknown_falls_back_to_official_conversation_store(fake_a2a, tmp_path: Path) -> None:
    context_id = "codex-recover-disk"
    conversation_dir = tmp_path / "conversations"
    conversation_dir.mkdir()
    service = BridgeService(
        Settings(
            endpoint=fake_a2a.endpoint,
            state_path=tmp_path / "state.sqlite",
            conversation_dir=conversation_dir,
        )
    )
    try:
        task = _unknown_task(service, context_id)
        timestamp = time.time()
        path = conversation_dir / f"{context_id}.jsonl"
        records = [
            {"ts": timestamp, "role": "user", "text": "late work", "task_id": "task-on-disk"},
            {"ts": timestamp + 1, "role": "agent", "text": "late result from disk", "task_id": "task-on-disk"},
        ]
        path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
        result = await service.task_get(task.bridge_task_id)
        assert result["state"] == "completed"
        assert result["result"] == "late result from disk"
        assert result["a2a_task_id"] == "task-on-disk"
        assert result["recovery_strategy"] == "conversation_store"
        assert "does not store A2A task state" in result["recovery_warning"]
        assert fake_a2a.method_counts.get("SendMessage", 0) == 0
        assert fake_a2a.method_counts.get("SendStreamingMessage", 0) == 0
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_recovery_refuses_ambiguous_multiple_local_tasks(fake_a2a, tmp_path: Path) -> None:
    context_id = "codex-recover-ambiguous"
    service = BridgeService(
        Settings(
            endpoint=fake_a2a.endpoint,
            state_path=tmp_path / "state.sqlite",
            conversation_dir=tmp_path / "conversations",
        )
    )
    try:
        first = _unknown_task(service, context_id, "bt-unknown-one")
        _unknown_task(service, context_id, "bt-unknown-two")
        fake_a2a._make_task({"contextId": context_id, "parts": [{"text": "late work"}]})
        result = await service.task_get(first.bridge_task_id)
        assert result["state"] == "outcome_unknown"
        assert result["a2a_task_id"] is None
        assert "recovery_strategy" not in result
        assert fake_a2a.method_counts.get("SendMessage", 0) == 0
        assert fake_a2a.method_counts.get("SendStreamingMessage", 0) == 0
    finally:
        await service.aclose()
