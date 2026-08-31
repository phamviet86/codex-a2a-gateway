from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fake_a2a import FakeA2AServer

from codex_a2a_gateway.core import BridgeService
from codex_a2a_gateway.models import TaskRecord, now_iso, request_fingerprint
from codex_a2a_gateway.settings import Settings


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
async def test_sync_uses_stream_and_completes_after_initial_timeout_without_resend(
    fake_a2a: FakeA2AServer, tmp_path: Path
) -> None:
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
async def test_outcome_unknown_recovers_by_context_list_without_resend(fake_a2a: FakeA2AServer, tmp_path: Path) -> None:
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
async def test_outcome_unknown_falls_back_to_official_conversation_store(
    fake_a2a: FakeA2AServer, tmp_path: Path
) -> None:
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
async def test_recovery_refuses_ambiguous_multiple_local_tasks(fake_a2a: FakeA2AServer, tmp_path: Path) -> None:
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
