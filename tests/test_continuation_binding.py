"""Regression coverage for peers allocating a remote ID for each continuation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_durable_contract import Peer, bridge

from codex_a2a_gateway.models import A2AError, A2ATaskResult, BridgeError


class ContinuationPeer(Peer):
    """Hermes-shaped responses: preserved context, optional message attribution."""

    def __init__(self) -> None:
        super().__init__()
        self.new_id = True
        self.metadata = False
        self.loss = ""
        self.bad_context = False
        self.wrong_metadata = False
        self.forced_id: str | None = None
        self.pause_continuation = False
        self.omit_context = False
        self.ack_kind = "task"

    async def subscribe_task(self, task_id: str, **_: Any):
        yield {"task": self.remote[task_id]}

    async def stream_message(self, message: str, context_id: str, message_id: str, **kwargs: Any):
        previous = kwargs.get("task_id")
        self.calls.append((context_id, message_id, previous))
        if previous and self.loss == "before":
            raise A2AError("a2a_transport_ambiguous", "lost before acknowledgement")
        remote_id = self.forced_id or ("remote-" + message_id if self.new_id or not previous else previous)
        raw: dict[str, Any] = {
            "id": remote_id,
            "contextId": "wrong" if previous and self.bad_context else context_id,
            "status": {"state": "TASK_STATE_WORKING"},
        }
        if previous and self.omit_context:
            raw.pop("contextId")
        if self.metadata or (previous and self.wrong_metadata):
            raw["metadata"] = {"requestMessageId": "stale" if self.wrong_metadata else message_id}
        self.remote[remote_id] = raw
        if previous and self.ack_kind == "statusUpdate":
            yield {"statusUpdate": {"taskId": remote_id, **{key: value for key, value in raw.items() if key != "id"}}}
        else:
            yield {"task": raw}
        final = {
            **raw,
            "status": {
                "state": "TASK_STATE_COMPLETED"
                if previous and not self.pause_continuation
                else "TASK_STATE_INPUT_REQUIRED"
            },
            "artifacts": [{"parts": [{"text": message}]}],
        }
        self.remote[remote_id] = final
        if previous and self.loss == "after":
            raise A2AError("a2a_transport_ambiguous", "lost after acknowledgement")
        yield {"task": final}


@pytest.mark.parametrize("new_id", [True, False])
async def test_continuation_preserves_handle_origin_and_deduplicates(tmp_path: Path, new_id: bool) -> None:
    peer = ContinuationPeer()
    peer.new_id = new_id
    service = bridge(tmp_path, peer)
    try:
        first = await service.chat("question", mode="sync", idempotency_key="first", origin={"question_id": "q"})
        second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync", idempotency_key="second")
        assert second["state"] == "completed"
        assert second["bridge_task_id"] == first["bridge_task_id"]
        assert second["origin"] == first["origin"]
        assert second["context_id"] == first["context_id"]
        assert (second["a2a_task_id"] != first["a2a_task_id"]) is new_id
        assert second["result"] == "answer" and second["result_id"]
        assert peer.calls[-1][2] == first["a2a_task_id"]
        for key, message in [("first", "question"), ("second", "answer")]:
            assert (await service.chat(message, idempotency_key=key))["deduplicated"]
        acknowledged = await service.task_get(
            first["bridge_task_id"], acknowledge_result_id=second["result_id"], expected_origin={"question_id": "q"}
        )
        assert acknowledged["acknowledged"]
        with pytest.raises(BridgeError):
            await service.task_get(first["bridge_task_id"], expected_origin={"question_id": "other"})
        with pytest.raises(BridgeError):
            await service.task_get(first["bridge_task_id"], acknowledge_result_id="invented")
        assert len(peer.calls) == 2
    finally:
        await service.aclose()


@pytest.mark.parametrize(
    "new_id,metadata,expected",
    [(True, False, "completed"), (False, False, "outcome_unknown"), (False, True, "completed")],
)
async def test_restart_after_ack_requires_current_attempt_provenance(
    tmp_path: Path, new_id: bool, metadata: bool, expected: str
) -> None:
    peer = ContinuationPeer()
    peer.new_id, peer.metadata = new_id, metadata
    service = bridge(tmp_path, peer)
    first = await service.chat("question", mode="sync")
    peer.loss = "after"
    second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
    await service.aclose()
    restarted = bridge(tmp_path, peer)
    try:
        result = await restarted.task_get(second["bridge_task_id"])
        assert result["state"] == expected
        assert result["result"] == ("answer" if expected == "completed" else "")
        assert len(peer.calls) == 2
    finally:
        await restarted.aclose()


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("operation", ["get", "wait"])
async def test_lost_ack_recovery_checks_exact_message_even_when_predecessor_exists(
    tmp_path: Path, exact: bool, operation: str
) -> None:
    peer = ContinuationPeer()
    service = bridge(tmp_path, peer)
    first = await service.chat("question", mode="sync")
    peer.loss = "before"
    second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
    candidate = {
        "id": "unacknowledged-continuation",
        "contextId": second["context_id"],
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"text": "recovered"}]}],
    }
    if exact:
        candidate["metadata"] = {"requestMessageId": second["message_id"]}
    peer.pages[""] = {"tasks": [candidate]}
    await service.aclose()
    restarted = bridge(tmp_path, peer)
    try:
        result = (
            await restarted.task_get(first["bridge_task_id"])
            if operation == "get"
            else await restarted.task_wait(first["bridge_task_id"], timeout=1)
        )
        assert result["state"] == ("completed" if exact else "outcome_unknown")
        assert result["result"] == ("recovered" if exact else "")
        assert len(peer.calls) == 2
    finally:
        await restarted.aclose()


@pytest.mark.parametrize("conflict", ["context", "metadata", "missing_context"])
@pytest.mark.parametrize("ack_kind", ["task", "statusUpdate"])
async def test_direct_new_id_rejects_conflicting_evidence(tmp_path: Path, conflict: str, ack_kind: str) -> None:
    peer = ContinuationPeer()
    service = bridge(tmp_path, peer)
    try:
        first = await service.chat("question", mode="sync")
        peer.ack_kind = ack_kind
        peer.omit_context = conflict == "missing_context"
        peer.bad_context = conflict == "context"
        peer.wrong_metadata = conflict == "metadata"
        second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
        assert second["state"] == "outcome_unknown"
        assert second["a2a_task_id"] == first["a2a_task_id"]
        assert not second["result"] and not second["result_id"]
        assert len(peer.calls) == 2
    finally:
        await service.aclose()


async def test_binding_freezes_and_stale_worker_cannot_update_new_attempt(tmp_path: Path) -> None:
    peer = ContinuationPeer()
    service = bridge(tmp_path, peer)
    try:
        first = await service.chat("question", mode="sync")
        peer.loss = "after"
        second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
        for remote_id, message_id in [
            ("third-remote-id", second["message_id"]),
            (first["a2a_task_id"], second["message_id"]),
            (second["a2a_task_id"], first["message_id"]),
        ]:
            with pytest.raises(BridgeError):
                service._apply_remote(
                    second["bridge_task_id"],
                    A2ATaskResult(task_id=remote_id, context_id=second["context_id"], state="completed", text="stale"),
                    submission_message_id=message_id,
                )
        result = await service.task_get(second["bridge_task_id"])
        assert result["state"] == "completed" and result["result"] == "answer"
        assert len(peer.calls) == 2
    finally:
        await service.aclose()


@pytest.mark.parametrize("historical", [False, True])
async def test_new_id_cannot_steal_another_jobs_binding(tmp_path: Path, historical: bool) -> None:
    peer = ContinuationPeer()
    service = bridge(tmp_path, peer)
    try:
        occupied = await service.chat("another question", mode="sync")
        if historical:
            await service.chat("another answer", task_id=occupied["bridge_task_id"], mode="sync")
        first = await service.chat("question", mode="sync")
        peer.forced_id = occupied["a2a_task_id"]
        second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
        assert second["state"] == "outcome_unknown"
        assert second["a2a_task_id"] == first["a2a_task_id"]
        untouched = await service.task_get(occupied["bridge_task_id"], refresh=False)
        assert untouched["result"] == ("another answer" if historical else "another question")
        assert len(peer.calls) == (4 if historical else 3)
    finally:
        await service.aclose()


async def test_new_attempt_rejects_historical_remote_id(tmp_path: Path) -> None:
    peer = ContinuationPeer()
    service = bridge(tmp_path, peer)
    try:
        first = await service.chat("question", mode="sync")
        peer.pause_continuation = True
        second = await service.chat("clarification", task_id=first["bridge_task_id"], mode="sync")
        assert second["state"] == "input_required"
        peer.pause_continuation = False
        peer.forced_id = first["a2a_task_id"]
        third = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
        assert third["state"] == "outcome_unknown"
        assert third["a2a_task_id"] == second["a2a_task_id"]
        assert not third["result_id"] and not third["result"]
        assert len(peer.calls) == 3
    finally:
        await service.aclose()


async def test_restart_without_binding_does_not_invent_acknowledgement(tmp_path: Path) -> None:
    import sqlite3

    peer = ContinuationPeer()
    service = bridge(tmp_path, peer)
    first = await service.chat("question", mode="sync")
    peer.loss = "after"
    second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
    await service.aclose()
    # Simulate a pre-migration database whose task row has no durable ACK provenance.
    with sqlite3.connect(tmp_path / "out.sqlite") as connection:
        connection.execute("DROP TABLE outbound_bindings")
    restarted = bridge(tmp_path, peer)
    try:
        result = await restarted.task_get(second["bridge_task_id"])
        assert result["state"] == "outcome_unknown" and not result["result_id"]
        assert len(peer.calls) == 2
    finally:
        await restarted.aclose()


async def test_status_only_new_remote_id_cannot_establish_binding(tmp_path: Path) -> None:
    peer = ContinuationPeer()
    service = bridge(tmp_path, peer)
    try:
        first = await service.chat("question", mode="sync")
        peer.ack_kind = "statusUpdate"
        second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
        assert second["state"] == "outcome_unknown"
        assert second["a2a_task_id"] == first["a2a_task_id"]
        assert not second["result_id"]
    finally:
        await service.aclose()


@pytest.mark.parametrize("error", [A2AError("a2a_transport_ambiguous", "old failure"), RuntimeError("old failure")])
async def test_stale_worker_error_and_done_callback_preserve_new_worker(tmp_path: Path, error: Exception) -> None:
    import asyncio

    class RacePeer(ContinuationPeer):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.new_entered = asyncio.Event()
            self.new_release = asyncio.Event()
            self.discoveries = 0

        async def discover(self, **_: Any) -> dict[str, Any]:
            self.discoveries += 1
            if self.discoveries == 1:
                self.entered.set()
                await self.release.wait()
                raise error
            self.new_entered.set()
            await self.new_release.wait()
            return {}

    peer = RacePeer()
    service = bridge(tmp_path, peer)
    try:
        first = await service.chat("question", mode="async")
        await peer.entered.wait()
        old_worker = service._workers[first["bridge_task_id"]]
        # A concurrently obtained turn boundary permits the next submission before
        # the old discovery/transport coroutine has finished unwinding.
        service.store.update_task(first["bridge_task_id"], state="input_required", a2a_task_id="old-remote")
        second = await service.chat("answer", task_id=first["bridge_task_id"], mode="async")
        new_worker = service._workers[first["bridge_task_id"]]
        assert new_worker is not old_worker
        peer.release.set()
        await asyncio.wait_for(asyncio.shield(old_worker), 1)
        await asyncio.wait_for(peer.new_entered.wait(), 1)
        await asyncio.sleep(0)
        assert service._workers[first["bridge_task_id"]] is new_worker
        current = await service.task_get(second["bridge_task_id"], refresh=False)
        assert current["message_id"] == second["message_id"] and current["state"] == "queued"
        assert not peer.calls
        peer.new_release.set()
        await asyncio.wait_for(asyncio.shield(new_worker), 1)
        result = await service.task_get(second["bridge_task_id"], refresh=False)
        assert result["state"] == "completed" and result["result"] == "answer"
        assert len(peer.calls) == 1
    finally:
        await service.aclose()


@pytest.mark.parametrize("agent_attribution", [None, "old-message", "current"])
async def test_continuation_transcript_requires_exact_agent_attribution(
    tmp_path: Path, agent_attribution: str | None
) -> None:
    import json
    import time

    peer = ContinuationPeer()
    service = bridge(tmp_path, peer)
    try:
        first = await service.chat("question", mode="sync")
        peer.loss = "before"
        second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
        directory = tmp_path / "conversations"
        directory.mkdir(exist_ok=True)
        user = {"ts": time.time(), "role": "user", "task_id": "disk-new", "message_id": second["message_id"]}
        agent: dict[str, Any] = {
            "ts": time.time(),
            "role": "agent",
            "task_id": "disk-new",
            "task_state": "completed",
            "text": "disk answer",
        }
        if agent_attribution is not None:
            agent["requestMessageId"] = second["message_id"] if agent_attribution == "current" else agent_attribution
        (directory / f"{second['context_id']}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in [agent, user]) + "\n", encoding="utf-8"
        )
        result = await service.task_get(second["bridge_task_id"])
        assert result["state"] == ("completed" if agent_attribution == "current" else "outcome_unknown")
        assert result["result"] == ("disk answer" if agent_attribution == "current" else "")
        assert len(peer.calls) == 2
    finally:
        await service.aclose()


@pytest.mark.parametrize("operation", ["get_notfound", "get_result", "cancel"])
async def test_delayed_old_read_or_cancel_cannot_mutate_continuation(tmp_path: Path, operation: str) -> None:
    import asyncio

    class DelayedPeer(ContinuationPeer):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.first_finish = asyncio.Event()
            self.reply_started = asyncio.Event()
            self.reply_release = asyncio.Event()

        async def stream_message(self, message: str, context_id: str, message_id: str, **kwargs: Any):
            async for event in super().stream_message(message, context_id, message_id, **kwargs):
                yield event
                if not kwargs.get("task_id") and event["task"]["status"]["state"] == "TASK_STATE_WORKING":
                    self.first_started.set()
                    await self.first_finish.wait()

        async def get_task(self, task_id: str) -> A2ATaskResult:
            snapshot = dict(self.remote[task_id])
            self.reply_started.set()
            await self.reply_release.wait()
            if operation == "get_notfound":
                raise A2AError("a2a_task_not_found", "old task missing")
            return self.parse_task(snapshot)

        async def cancel_task(self, task_id: str, **_: Any) -> A2ATaskResult:
            snapshot = dict(self.remote[task_id])
            self.reply_started.set()
            await self.reply_release.wait()
            return self.parse_task({**snapshot, "status": {"state": "TASK_STATE_CANCELED"}})

    peer = DelayedPeer()
    service = bridge(tmp_path, peer)
    try:
        first = await service.chat("question", mode="async")
        await asyncio.wait_for(peer.first_started.wait(), 1)
        first_worker = service._workers[first["bridge_task_id"]]
        delayed = asyncio.create_task(
            service.task_cancel(first["bridge_task_id"])
            if operation == "cancel"
            else service.task_get(first["bridge_task_id"])
        )
        await asyncio.wait_for(peer.reply_started.wait(), 1)
        peer.first_finish.set()
        await asyncio.wait_for(asyncio.shield(first_worker), 1)
        second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
        peer.reply_release.set()
        old_response = await asyncio.wait_for(delayed, 1)
        assert old_response["message_id"] == second["message_id"]
        assert old_response["state"] == "completed" and old_response["result"] == "answer"
        current = await service.task_get(second["bridge_task_id"], refresh=False)
        assert current["result_id"] == second["result_id"]
        assert len(peer.calls) == 2
    finally:
        await service.aclose()


async def test_wait_retries_exact_recovery_when_new_task_appears_after_first_poll(tmp_path: Path) -> None:
    class LaterPeer(ContinuationPeer):
        def __init__(self) -> None:
            super().__init__()
            self.list_calls = 0
            self.subscribe_calls = 0
            self.candidate: dict[str, Any] = {}

        async def list_tasks(self, **_: Any) -> dict[str, Any]:
            self.list_calls += 1
            return {"tasks": [self.candidate] if self.list_calls >= 3 else []}

        async def subscribe_task(self, task_id: str, **_: Any):
            self.subscribe_calls += 1
            raise AssertionError("an unbound predecessor must not consume the wait budget")
            yield {}  # pragma: no cover

    peer = LaterPeer()
    service = bridge(tmp_path, peer)
    first = await service.chat("question", mode="sync")
    peer.loss = "before"
    second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
    peer.candidate = {
        "id": "late-new-task",
        "contextId": second["context_id"],
        "metadata": {"requestMessageId": second["message_id"]},
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"text": "late answer"}]}],
    }
    await service.aclose()
    restarted = bridge(tmp_path, peer)
    try:
        result = await restarted.task_wait(second["bridge_task_id"], timeout=2)
        assert result["state"] == "completed" and result["result"] == "late answer"
        assert result["a2a_task_id"] == "late-new-task"
        assert peer.list_calls >= 3 and peer.subscribe_calls == 0
        assert len(peer.calls) == 2
    finally:
        await restarted.aclose()


async def legacy_unknown_continuation(tmp_path: Path, peer: ContinuationPeer):
    import sqlite3

    service = bridge(tmp_path, peer)
    first = await service.chat("question", mode="sync")
    peer.loss = "before"
    second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
    await service.aclose()
    with sqlite3.connect(tmp_path / "out.sqlite") as connection:
        connection.execute("DROP TABLE outbound_bindings")
        connection.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    return bridge(tmp_path, peer), first, second


@pytest.mark.parametrize("claimant", ["later_attempt", "another_job"])
async def test_legacy_predecessor_is_reserved_after_exact_new_task_recovery(tmp_path: Path, claimant: str) -> None:
    peer = ContinuationPeer()
    service, first, second = await legacy_unknown_continuation(tmp_path, peer)
    try:
        assert service.store.outbound_bindings(first["bridge_task_id"]) == []
        peer.pages[""] = {
            "tasks": [
                {
                    "id": "exact-new-task",
                    "contextId": second["context_id"],
                    "metadata": {"requestMessageId": second["message_id"]},
                    "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
                }
            ]
        }
        recovered = await service.task_get(second["bridge_task_id"])
        assert recovered["state"] == "input_required" and recovered["a2a_task_id"] == "exact-new-task"
        lineage = service.store.outbound_bindings(first["bridge_task_id"])
        assert any(
            row["message_id"] == "" and row["remote_task_id"] == first["a2a_task_id"] and row["provenance"] == "legacy"
            for row in lineage
        )
        peer.loss = ""
        peer.forced_id = first["a2a_task_id"]
        claimed = await service.chat(
            "new claimant", mode="sync", task_id=first["bridge_task_id"] if claimant == "later_attempt" else None
        )
        assert claimed["state"] == "outcome_unknown" and not claimed["result_id"]
        assert len(peer.calls) == 3
    finally:
        await service.aclose()


@pytest.mark.parametrize("failure", ["invalid_state", "write_abort"])
async def test_binding_and_legacy_reservation_roll_back_with_failed_task_update(tmp_path: Path, failure: str) -> None:
    import sqlite3

    peer = ContinuationPeer()
    service, first, second = await legacy_unknown_continuation(tmp_path, peer)
    try:
        before = service.store.get_task(second["bridge_task_id"])
        if failure == "write_abort":
            with sqlite3.connect(tmp_path / "out.sqlite") as connection:
                connection.execute(
                    "CREATE TRIGGER reject_update BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT, 'injected'); END"
                )
        with pytest.raises(BridgeError if failure == "invalid_state" else sqlite3.IntegrityError):
            service.store.update_task(
                second["bridge_task_id"],
                state="not-a-state" if failure == "invalid_state" else "working",
                a2a_task_id="must-not-bind",
                binding_provenance="exact_message",
                expected_message_id=second["message_id"],
            )
        assert service.store.outbound_bindings(second["bridge_task_id"]) == []
        after = service.store.get_task(second["bridge_task_id"])
        assert after == before and after.a2a_task_id == first["a2a_task_id"]
        assert len(peer.calls) == 2
    finally:
        await service.aclose()
