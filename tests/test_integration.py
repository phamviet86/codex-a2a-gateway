from __future__ import annotations

from pathlib import Path

import pytest

from codex_hermes_a2a_bridge.a2a import A2AClient
from codex_hermes_a2a_bridge.core import BridgeService
from codex_hermes_a2a_bridge.models import BridgeError
from codex_hermes_a2a_bridge.settings import Settings


@pytest.mark.asyncio
async def test_a2a_and_all_service_tool_operations(fake_a2a, tmp_path: Path) -> None:
    settings = Settings(
        endpoint=fake_a2a.endpoint,
        state_path=tmp_path / "state.sqlite",
        conversation_dir=tmp_path / "conversations",
        default_timeout=5,
        auto_wait=2,
    )
    service = BridgeService(settings)
    try:
        status = await service.status()
        assert status["ok"] and status["hermes"]["agent_card"]["name"] == "Fake Hermes"

        first = await service.chat("hello", conversation_key="conv", mode="sync", idempotency_key="one")
        assert first["state"] == "completed" and "turn=1" in first["result"]
        repeated = await service.chat("hello", conversation_key="conv", mode="sync", idempotency_key="one")
        assert repeated["deduplicated"] is True
        with pytest.raises(BridgeError, match="different request"):
            await service.chat("changed", conversation_key="conv", mode="sync", idempotency_key="one")

        second = await service.chat("follow up", conversation_key="conv", context_id=first["context_id"], mode="auto")
        waited = await service.task_wait(second["bridge_task_id"], timeout=2)
        assert waited["state"] == "completed" and "turn=2" in waited["result"]
        got = await service.task_get(second["bridge_task_id"])
        assert got["context_id"] == first["context_id"]
        listed = await service.tasks_list(conversation_key="conv")
        assert listed["count"] == 2
        contexts = await service.contexts(action="inspect", context_id=first["context_id"])
        assert contexts["context"]["turn_count"] == 2

        question = await service.chat("need input", conversation_key="conv", mode="sync")
        assert question["state"] == "input_required" and question["needs_input"] is True
        continuation = await service.chat("use 42", conversation_key="conv", mode="sync")
        assert continuation["context_id"] == first["context_id"] and "turn=4" in continuation["result"]

        long_task = await service.chat("long operation", conversation_key="cancel-conv", mode="async")
        assert long_task["state"] == "working"
        canceled = await service.task_cancel(long_task["bridge_task_id"])
        assert canceled["cancel_requested"] and canceled["state"] == "canceled"
        assert canceled["computation_stopped"] == "unknown"
        assert "does not guarantee" in canceled["note"]

        closed = await service.contexts(action="close", context_id=first["context_id"])
        assert closed["context"]["status"] == "closed" and "not deleted" in closed["note"]

        remote_list = await service.client.list_tasks(limit=10)
        assert len(remote_list["tasks"]) >= 5
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_transport_ambiguity_is_not_retried(tmp_path: Path) -> None:
    settings = Settings(endpoint="http://127.0.0.1:1", state_path=tmp_path / "state.sqlite", connect_timeout=0.1)
    client = A2AClient(settings)
    try:
        with pytest.raises(BridgeError) as error:
            await client.send_message("x", "ctx", "m", timeout=1)
        assert error.value.code in {"a2a_unreachable", "a2a_transport_ambiguous"}
    finally:
        await client.aclose()
