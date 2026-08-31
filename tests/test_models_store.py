from __future__ import annotations

from pathlib import Path

import pytest

from codex_hermes_a2a_bridge.models import BridgeError, TaskRecord, now_iso, request_fingerprint
from codex_hermes_a2a_bridge.settings import Settings
from codex_hermes_a2a_bridge.store import Store


def test_settings_only_accept_loopback(tmp_path: Path) -> None:
    settings = Settings(endpoint="http://127.0.0.1:9900", state_path=tmp_path / "db.sqlite")
    assert settings.card_url.endswith("/.well-known/agent-card.json")
    with pytest.raises(ValueError):
        Settings(endpoint="https://example.com")
    with pytest.raises(ValueError, match="PUBLIC_URL requires"):
        Settings(inbound_public_url="https://gateway.example.com")
    exposed = Settings(
        inbound_host="0.0.0.0",
        inbound_public_url="https://gateway.example.com",
        inbound_token="secret",
    )
    assert exposed.advertised_url == "https://gateway.example.com"


def test_context_mapping_close_and_turn_budget(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    context = store.get_or_create_context(conversation_key="c1", endpoint="http://127.0.0.1:9900")
    assert (
        store.get_or_create_context(conversation_key="c1", endpoint="http://127.0.0.1:9900").context_id
        == context.context_id
    )
    assert store.increment_turn(context.context_id, 1).turn_count == 1
    with pytest.raises(BridgeError, match="turn budget"):
        store.increment_turn(context.context_id, 1)
    assert store.close_context(context_id=context.context_id).status == "closed"
    replacement = store.get_or_create_context(conversation_key="c1", endpoint="http://127.0.0.1:9900")
    assert replacement.context_id != context.context_id


def test_task_state_and_idempotency(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    context = store.get_or_create_context(conversation_key="c", endpoint="http://127.0.0.1:9900")
    now = now_iso()
    task = TaskRecord(
        bridge_task_id="bt-1",
        context_id=context.context_id,
        conversation_key="c",
        endpoint=context.endpoint,
        request_id="r1",
        message_id="m1",
        idempotency_key="idem",
        request_fingerprint=request_fingerprint("hello", context.context_id, "default"),
        mode="sync",
        created_at=now,
        updated_at=now,
    )
    store.create_task(task)
    store.update_task("bt-1", state="working", a2a_task_id="remote-1")
    completed = store.update_task("bt-1", state="completed", result_text="ok")
    assert completed.completed_at and store.get_task("remote-1") == completed
    assert store.update_task("bt-1", state="failed").state == "completed"
    assert store.get_task_by_idempotency("idem") == completed
