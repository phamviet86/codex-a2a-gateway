"""Broker tests use real PostgreSQL only; no SQLite durability substitutes.

Set HERMES_A2A_GATEWAY_TEST_POSTGRES_DSN to a disposable database. Every test owns
an isolated schema and drops only that schema; no Hermes/model task is executed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import signal
import socket
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.conninfo import make_conninfo
from starlette.applications import Starlette

from hermes_a2a_gateway.a2a import A2AClient
from hermes_a2a_gateway.broker import BrokerDispatcher, create_broker_app
from hermes_a2a_gateway.broker_peer import HermesBrokerPeer, peer_context
from hermes_a2a_gateway.broker_settings import BrokerSettings
from hermes_a2a_gateway.broker_store import BrokerError, BrokerStore, canonical, utcnow
from hermes_a2a_gateway.models import A2ATaskResult
from hermes_a2a_gateway.settings import Settings

TOKEN_A = "a" * 40
TOKEN_B = "b" * 40


@pytest.fixture
def broker_settings() -> Iterator[BrokerSettings]:
    dsn = os.environ.get("HERMES_A2A_GATEWAY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("real PostgreSQL requires HERMES_A2A_GATEWAY_TEST_POSTGRES_DSN")
    schema = "broker_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield BrokerSettings(
                database_url=make_conninfo(dsn, options=f"-c search_path={schema}"),
                device_tokens={"a": TOKEN_A, "b": TOKEN_B},
                encryption_key=Fernet.generate_key().decode(),
                public_url="https://broker.test",
                poll_seconds=0.05,
            )
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def ledger(broker_settings: BrokerSettings) -> Iterator[BrokerStore]:
    store = BrokerStore(broker_settings)
    try:
        yield store
    finally:
        store.close()


def command(**overrides: Any) -> dict[str, Any]:
    return {
        "operation_id": str(uuid.uuid4()),
        "flow_id": str(uuid.uuid4()),
        "prompt": "synthetic-private-prompt",
        "artifact_ids": [],
        **overrides,
    }


def task(
    handle: str = "peer-task", state: str = "completed", text: str = "synthetic result", **kw: Any
) -> A2ATaskResult:
    return A2ATaskResult(task_id=handle, context_id="peer-context", state=state, text=text, **kw)


class FakePeer:
    def __init__(self, *, lose_ack: bool = False):
        self.submissions: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.cancels: list[str] = []
        self.lose_ack = lose_ack
        self.result = task()
        self.cancel_error = False
        self.closed = False

    async def submit(self, claim: dict[str, Any], timeout: float) -> AsyncGenerator[A2ATaskResult, None]:
        self.submissions.append(claim)
        if self.lose_ack:
            raise TimeoutError("synthetic loss after mutation")
        yield task(state="working")

    async def get(self, handle: str) -> A2ATaskResult:
        self.gets.append(handle)
        return self.result

    async def cancel(self, handle: str) -> A2ATaskResult:
        self.cancels.append(handle)
        if self.cancel_error:
            raise TimeoutError("synthetic cancellation ACK loss")
        return task(state="working")

    async def close(self) -> None:
        self.closed = True


async def settle(dispatcher: BrokerDispatcher) -> None:
    """Run a scheduler tick and await the bounded finite fake-peer work it starts."""
    await dispatcher.step()
    await asyncio.gather(*dispatcher._streams.values(), *dispatcher._reads.values(), *dispatcher._cancels.values())


def test_configuration_redacts_secrets_and_requires_distinct_tokens() -> None:
    values = dict(
        database_url="postgresql://secret", device_tokens={"a": TOKEN_A}, encryption_key=Fernet.generate_key().decode()
    )
    settings = BrokerSettings(**values)
    assert TOKEN_A not in repr(settings)
    assert "postgresql://secret" not in repr(settings)
    for extra in (
        {"device_tokens": {"a": TOKEN_A, "b": TOKEN_A}},
        {"public_url": "http://remote.test"},
        {"public_url": "https://remote.test", "host": "0.0.0.0", "allow_loopback_http": True},
        {"public_url": "https://user:password@broker.test"},
        {"encryption_key": "secret-invalid-key"},
    ):
        with pytest.raises(ValueError) as caught:
            BrokerSettings(**{**values, **extra})
        assert TOKEN_A not in str(caught.value)
        assert "secret-invalid-key" not in str(caught.value)
    assert BrokerSettings(**{**values, "public_url": "http://127.0.0.1:8790", "allow_loopback_http": True})


def test_context_identity_is_device_scoped_and_stable() -> None:
    flow = str(uuid.uuid4())
    assert peer_context("a", flow) == peer_context("a", flow)
    assert peer_context("a", flow) != peer_context("b", flow)
    assert peer_context("a", flow) != peer_context("a", str(uuid.uuid4()))


def test_atomic_acceptance_duplicate_conflict_and_encryption(ledger: BrokerStore) -> None:
    request = command()
    first, created = ledger.accept("a", request)
    assert created and first["state"] == "accepted"
    assert ledger.accept("a", request) == (first, False)
    with pytest.raises(BrokerError, match="different request"):
        ledger.accept("a", {**request, "prompt": "changed"})
    with ledger.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM broker_v06_operations").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM broker_v06_outbox").fetchone()["n"] == 1
        row = conn.execute("SELECT cipher FROM broker_v06_payloads").fetchone()
        assert request["prompt"].encode() not in bytes(row["cipher"])
        assert ledger._decrypt(row["cipher"]) == request
    assert [seq for seq, _ in ledger.events("a", 0)] == [1]
    assert ledger.events("b", 0) == []
    with pytest.raises(BrokerError, match="not found"):
        ledger.get("b", request["operation_id"])


def test_acceptance_rolls_back_outbox_payload_and_event(ledger: BrokerStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any) -> None:
        raise RuntimeError("synthetic event write failure")

    monkeypatch.setattr(ledger, "_event", fail)
    with pytest.raises(RuntimeError):
        ledger.accept("a", command())
    with ledger.transaction() as conn:
        for table in ("operations", "payloads", "outbox", "events", "devices"):
            count = conn.execute(
                sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier("broker_v06_" + table))
            ).fetchone()
            assert count["n"] == 0


def test_concurrent_duplicates_commit_once(ledger: BrokerStore, broker_settings: BrokerSettings) -> None:
    other = BrokerStore(broker_settings)
    request = command()
    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda store: store.accept("a", request), [ledger, other]))
        assert sum(created for _, created in results) == 1
        assert results[0][0] == results[1][0]
        assert len(ledger.events("a", 0)) == 1
    finally:
        other.close()


def test_dispatcher_lock_and_restart_do_not_reclaim(ledger: BrokerStore, broker_settings: BrokerSettings) -> None:
    request = command()
    ledger.accept("a", request)
    with pytest.raises(BrokerError, match="lock is required"):
        ledger.claim()
    ledger.acquire_dispatcher()
    assert ledger.claim()["operation_id"] == request["operation_id"]
    other = BrokerStore(broker_settings)
    try:
        with pytest.raises(BrokerError, match="another broker"):
            other.acquire_dispatcher()
        ledger.close()  # Simulates loss of the process/session advisory lock.
        other.acquire_dispatcher()
        other.recover()
        recovered = other.get("a", request["operation_id"])
        assert recovered["state"] == "outcome_unknown"
        assert recovered["remote_task_id"] is None
        assert other.claim() is None
        assert other.accept("a", request) == (recovered, False)
    finally:
        other.close()


def test_flow_serialization_and_stable_result_id(ledger: BrokerStore) -> None:
    first = command()
    second = command(flow_id=first["flow_id"])
    ledger.accept("a", first)
    ledger.accept("a", second)
    ledger.acquire_dispatcher()
    assert ledger.claim()["operation_id"] == first["operation_id"]
    assert ledger.claim() is None
    unknown = ledger.finish("a", first["operation_id"], "outcome_unknown")
    assert ledger.claim() is None
    done = ledger.finish("a", first["operation_id"], "completed", {"text": "first", "artifacts": []})
    assert done["result_id"] == unknown["result_id"]
    assert ledger.finish("a", first["operation_id"], "completed", {"text": "changed", "artifacts": []}) == done
    assert ledger.claim()["operation_id"] == second["operation_id"]


def test_admission_limit_and_expired_payload(ledger: BrokerStore) -> None:
    ledger.settings.max_pending_per_device = 1
    request = command()
    ledger.accept("a", request)
    with pytest.raises(BrokerError, match="limit reached"):
        ledger.accept("a", command())
    with ledger.transaction() as conn:
        conn.execute("UPDATE broker_v06_payloads SET expires_at=%s", (utcnow() - timedelta(seconds=1),))
    ledger.purge()
    ledger.acquire_dispatcher()
    assert ledger.claim() is None
    assert ledger.get("a", request["operation_id"])["error"]["code"] == "payload_expired"
    assert ledger.accept("a", command())[1]


def test_replay_retention_gap_has_atomic_resume_cursor(ledger: BrokerStore) -> None:
    request = command()
    ledger.accept("a", request)
    ledger.finish("a", request["operation_id"], "completed", {"text": "hello", "artifacts": []})
    assert [seq for seq, _ in ledger.events("a", 1)] == [2]
    with ledger.transaction() as conn:
        conn.execute("UPDATE broker_v06_events SET expires_at=%s WHERE sequence=1", (utcnow() - timedelta(seconds=1),))
    with pytest.raises(BrokerError) as caught:
        ledger.events("a", 0)
    assert caught.value.status == 410
    assert caught.value.details == {"resume_after": 2}
    assert [seq for seq, _ in ledger.events("a", 1)] == [2]
    assert ledger.events("a", 2) == []
    with pytest.raises(BrokerError, match="ahead"):
        ledger.events("b", 2)


def test_results_are_encrypted_expire_and_receipts_are_idempotent(ledger: BrokerStore) -> None:
    request = command()
    ledger.accept("a", request)
    snapshot = ledger.finish("a", request["operation_id"], "completed", {"text": "private-result", "artifacts": []})
    for _ in range(2):
        ledger.receipt("a", request["operation_id"], snapshot["result_id"], "stored")
    with ledger.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM broker_v06_receipts").fetchone()["n"] == 1
        assert b"private-result" not in bytes(
            conn.execute("SELECT result_cipher FROM broker_v06_operations").fetchone()["result_cipher"]
        )
        conn.execute("UPDATE broker_v06_operations SET result_expires_at=%s", (utcnow() - timedelta(seconds=1),))
    ledger.purge()
    expired = ledger.get("a", request["operation_id"])
    assert expired["result"] is None and expired["error"]["code"] == "result_expired"
    assert expired["result_id"] == snapshot["result_id"]
    with pytest.raises(BrokerError):
        ledger.receipt("a", request["operation_id"], str(uuid.uuid4()), "stored")
    with pytest.raises(BrokerError):
        ledger.receipt("b", request["operation_id"], snapshot["result_id"], "stored")


def test_artifact_digest_quota_expiry_and_ciphertext(ledger: BrokerStore) -> None:
    ledger.settings.max_artifact_bytes = 8
    ledger.settings.device_quota_bytes = 10
    content = b"123456"
    digest = hashlib.sha256(content).hexdigest()
    descriptor = ledger.put_artifact("a", content, digest, "text/html")
    assert ledger.artifact("a", descriptor["artifact_id"]) == (descriptor, content)
    with ledger.transaction() as conn:
        assert content not in bytes(conn.execute("SELECT cipher FROM broker_v06_artifacts").fetchone()["cipher"])
    for args, code in [
        ((content, "bad", "text/plain"), "digest_mismatch"),
        ((b"123456789", "bad", "text/plain"), "artifact_too_large"),
        ((content, digest, "text/plain"), "artifact_quota"),
    ]:
        with pytest.raises(BrokerError) as caught:
            ledger.put_artifact("a", *args)
        assert caught.value.code == code
    with pytest.raises(BrokerError):
        ledger.artifact("b", descriptor["artifact_id"])
    with ledger.transaction() as conn:
        conn.execute("UPDATE broker_v06_artifacts SET expires_at=%s", (utcnow() - timedelta(seconds=1),))
    with pytest.raises(BrokerError):
        ledger.artifact("a", descriptor["artifact_id"])
    assert ledger.put_artifact("a", content, digest, "text/plain")


async def test_lost_submit_ack_never_resends(ledger: BrokerStore, broker_settings: BrokerSettings) -> None:
    request = command()
    ledger.accept("a", request)
    ledger.acquire_dispatcher()
    peer = FakePeer(lose_ack=True)
    dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
    await settle(dispatcher)
    assert ledger.get("a", request["operation_id"])["state"] == "outcome_unknown"
    for _ in range(3):
        await settle(dispatcher)
    assert len(peer.submissions) == 1
    assert not peer.gets


async def test_first_handle_reconciles_after_restart(ledger: BrokerStore, broker_settings: BrokerSettings) -> None:
    request = command()
    ledger.accept("a", request)
    ledger.acquire_dispatcher()
    peer = FakePeer()
    await settle(BrokerDispatcher(ledger, peer, broker_settings))
    assert ledger.get("a", request["operation_id"])["remote_task_id"] == "peer-task"
    ledger.close()
    restarted = BrokerStore(broker_settings)
    try:
        restarted.acquire_dispatcher()
        restarted.recover()
        unknown = restarted.get("a", request["operation_id"])
        await settle(BrokerDispatcher(restarted, peer, broker_settings))
        final = restarted.get("a", request["operation_id"])
        assert final["state"] == "completed" and final["result_id"] == unknown["result_id"]
        assert final["result"]["text"] == "synthetic result"
        assert peer.gets == ["peer-task"] and len(peer.submissions) == 1
    finally:
        restarted.close()


async def test_cancel_is_best_effort_and_ack_loss_is_not_retried(
    ledger: BrokerStore, broker_settings: BrokerSettings
) -> None:
    request = command()
    ledger.accept("a", request)
    ledger.acquire_dispatcher()
    peer = FakePeer()
    peer.result = task(state="working")
    peer.cancel_error = True
    dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
    await settle(dispatcher)
    snapshot, _ = ledger.cancel_intent("a", request["operation_id"])
    assert snapshot["state"] == "outcome_unknown" and snapshot["error"]["code"] == "cancel_unconfirmed"
    await settle(dispatcher)
    ledger.cancel_intent("a", request["operation_id"])
    await settle(dispatcher)
    assert peer.cancels == ["peer-task"]
    assert ledger.get("a", request["operation_id"])["state"] == "outcome_unknown"
    pending = command()
    ledger.accept("a", pending)
    assert ledger.cancel_intent("a", pending["operation_id"])[0]["state"] == "canceled"
    assert ledger.claim() is None


async def test_peer_binary_results_are_stored_once(ledger: BrokerStore, broker_settings: BrokerSettings) -> None:
    request = command()
    ledger.accept("a", request)
    ledger.acquire_dispatcher()
    peer = FakePeer()
    peer.result = task(
        artifacts=[{"artifactId": "unstable-peer-id", "parts": [{"raw": base64.b64encode(b"binary").decode()}]}]
    )
    dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
    await settle(dispatcher)
    await settle(dispatcher)
    snapshot = ledger.get("a", request["operation_id"])
    descriptor = snapshot["result"]["artifacts"][0]
    assert ledger.artifact("a", descriptor["artifact_id"])[1] == b"binary"
    await dispatcher.observe("a", request["operation_id"], peer.result)
    assert ledger.get("a", request["operation_id"]) == snapshot
    with ledger.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM broker_v06_artifacts").fetchone()["n"] == 1


async def test_api_auth_validation_scope_and_inert_download(
    ledger: BrokerStore, broker_settings: BrokerSettings
) -> None:
    app = create_broker_app(broker_settings, store=ledger, dispatch=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://broker.test") as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.post("/v1/operations", json=command())).status_code == 401
        a, b = {"Authorization": f"Bearer {TOKEN_A}"}, {"Authorization": f"Bearer {TOKEN_B}"}
        request = command()
        accepted = await client.post("/v1/operations", json=request, headers=a)
        assert accepted.status_code == 202
        assert (await client.post("/v1/operations", json=request, headers=a)).status_code == 200
        assert (
            await client.post("/v1/operations", json={**request, "prompt": "changed"}, headers=a)
        ).status_code == 409
        path = f"/v1/operations/{request['operation_id']}"
        assert (await client.get(path, headers=b)).status_code == 404
        assert (await client.post(path + "/cancel", headers=b)).status_code == 404
        assert (
            await client.post("/v1/operations", json={**command(), "url": "http://evil"}, headers=a)
        ).status_code == 400
        assert (await client.post("/v1/operations", content=b"bad secret", headers=a)).status_code == 400
        assert (await client.get("/v1/operations/invalid", headers=a)).status_code == 400
        assert (await client.get("/v1/events?after=bad", headers=a)).status_code == 400
        assert (await client.get("http://broker.test/v1/events", headers=a)).status_code == 400
        content = b"<script>untrusted</script>"
        uploaded = await client.post(
            "/v1/artifacts",
            content=content,
            headers={**a, "Content-Type": "text/html", "X-Content-SHA256": hashlib.sha256(content).hexdigest()},
        )
        assert uploaded.status_code == 201
        url = "/v1/artifacts/" + uploaded.json()["artifact_id"]
        assert (await client.get(url, headers=b)).status_code == 404
        downloaded = await client.get(url, headers=a)
        assert downloaded.content == content
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        request = command(artifact_ids=[uploaded.json()["artifact_id"]])
        assert (await client.post("/v1/operations", json=request, headers=b)).status_code == 404


async def sse_once(app: Starlette, *, after: str = "0", token: str = TOKEN_A) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    finished = asyncio.Event()
    sent_request = False

    async def receive() -> dict[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)
        if message["type"] == "http.response.body":
            finished.set()

    await asyncio.wait_for(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "method": "GET",
                "scheme": "https",
                "path": "/v1/events",
                "raw_path": b"/v1/events",
                "query_string": b"",
                "root_path": "",
                "http_version": "1.1",
                "headers": [(b"authorization", f"Bearer {token}".encode()), (b"last-event-id", after.encode())],
                "server": ("broker.test", 443),
                "client": ("127.0.0.1", 1234),
            },
            receive,
            send,
        ),
        2,
    )
    return messages


async def test_sse_wire_replay_and_gap_response(ledger: BrokerStore, broker_settings: BrokerSettings) -> None:
    request = command()
    first, _ = ledger.accept("a", request)
    final = ledger.finish("a", request["operation_id"], "completed", {"text": "done", "artifacts": []})
    app = create_broker_app(broker_settings, store=ledger, dispatch=False)
    messages = await sse_once(app, after="1")
    assert messages[0]["status"] == 200
    body = next(m["body"] for m in messages if m["type"] == "http.response.body")
    assert body == b"id: 2\nevent: operation\ndata: " + canonical(final) + b"\n\n"
    assert first["operation_id"] == final["operation_id"]
    with ledger.transaction() as conn:
        conn.execute("UPDATE broker_v06_events SET expires_at=%s", (utcnow() - timedelta(seconds=1),))
    gap = await sse_once(app)
    assert gap[0]["status"] == 410
    error = json.loads(next(m["body"] for m in gap if m["type"] == "http.response.body"))
    assert error["error"]["details"] == {"resume_after": 2}


async def test_hermes_stream_handle_and_exact_get(
    ledger: BrokerStore, broker_settings: BrokerSettings, fake_a2a: Any
) -> None:
    peer = HermesBrokerPeer(A2AClient(Settings(endpoint=fake_a2a.endpoint)))
    request = command()
    ledger.accept("a", request)
    ledger.acquire_dispatcher()
    try:
        dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
        await settle(dispatcher)
        assert ledger.get("a", request["operation_id"])["remote_task_id"]
        await settle(dispatcher)
        final = ledger.get("a", request["operation_id"])
        assert final["state"] == "completed"
        assert "fake-marker" in final["result"]["text"]
        assert fake_a2a.method_counts["SendStreamingMessage"] == 1
        assert fake_a2a.method_counts.get("GetTask", 0) == 0
    finally:
        await peer.close()


def test_input_artifacts_validated_before_acceptance_and_combined_bound(ledger: BrokerStore) -> None:
    def upload(content: bytes, media: str) -> str:
        return ledger.put_artifact("a", content, hashlib.sha256(content).hexdigest(), media)["artifact_id"]

    for artifact in [upload(b"html", "text/html"), upload(b"\xff", "text/plain")]:
        request = command(artifact_ids=[artifact])
        with pytest.raises(BrokerError) as caught:
            ledger.accept("a", request)
        assert caught.value.code == "unsupported_artifact"
        with pytest.raises(BrokerError, match="not found"):
            ledger.get("a", request["operation_id"])
    text_id = upload(b"text", "text/plain; charset=utf-8")
    ledger.settings.max_artifact_bytes = 5
    with pytest.raises(BrokerError) as caught:
        ledger.accept("a", command(prompt="ab", artifact_ids=[text_id]))
    assert caught.value.code == "artifacts_too_large"
    assert ledger.accept("a", command(prompt="a", artifact_ids=[text_id]))[1]


async def test_text_attachment_reaches_peer_as_untrusted_data(
    ledger: BrokerStore,
    broker_settings: BrokerSettings,
    fake_a2a: Any,
) -> None:
    content = b"synthetic-attachment-marker"
    descriptor = ledger.put_artifact("a", content, hashlib.sha256(content).hexdigest(), "text/plain")
    request = command(artifact_ids=[descriptor["artifact_id"]])
    ledger.accept("a", request)
    ledger.acquire_dispatcher()
    peer = HermesBrokerPeer(A2AClient(Settings(endpoint=fake_a2a.endpoint)))
    try:
        dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
        await settle(dispatcher)
        await settle(dispatcher)
        text = ledger.get("a", request["operation_id"])["result"]["text"]
        assert "synthetic-attachment-marker" in text and "untrusted" in text
        assert descriptor["sha256"] in text
    finally:
        await peer.close()


def test_concurrent_artifact_quota_cannot_overcommit(ledger: BrokerStore, broker_settings: BrokerSettings) -> None:
    broker_settings.device_quota_bytes = 6
    other = BrokerStore(broker_settings)
    content = b"123456"

    def put(store: BrokerStore) -> str:
        try:
            store.put_artifact("a", content, hashlib.sha256(content).hexdigest(), "text/plain")
            return "stored"
        except BrokerError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(put, [ledger, other]))
        assert sorted(results) == ["artifact_quota", "stored"]
    finally:
        other.close()


def test_tampered_artifact_rejected(ledger: BrokerStore) -> None:
    content = b"private"
    descriptor = ledger.put_artifact("a", content, hashlib.sha256(content).hexdigest(), "text/plain")
    with ledger.transaction() as conn:
        conn.execute("UPDATE broker_v06_artifacts SET sha256=%s", ("0" * 64,))
    with pytest.raises(BrokerError) as caught:
        ledger.artifact("a", descriptor["artifact_id"])
    assert caught.value.code == "artifact_corrupt"


async def test_url_and_oversized_peer_artifacts_are_not_fetched(
    ledger: BrokerStore,
    broker_settings: BrokerSettings,
) -> None:
    request = command()
    ledger.accept("a", request)
    ledger.acquire_dispatcher()
    peer = FakePeer()
    peer.result = task(artifacts=[{"parts": [{"url": "http://example.invalid/private"}]}])
    dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
    await settle(dispatcher)
    await settle(dispatcher)
    snapshot = ledger.get("a", request["operation_id"])
    assert snapshot["state"] == "failed" and snapshot["error"]["code"] == "unsupported_peer_artifact"
    assert len(peer.submissions) == 1


def test_result_limit_retains_stable_id_without_partial_payload(ledger: BrokerStore) -> None:
    request = command()
    ledger.accept("a", request)
    ledger.settings.max_result_bytes = 1024
    result = ledger.finish("a", request["operation_id"], "completed", {"text": "x" * 2000, "artifacts": []})
    assert result["state"] == "failed" and result["result"] is None
    assert result["error"]["code"] == "result_too_large"
    assert result["result_id"]


async def test_full_lifespan_starts_dispatcher_and_recovers_acceptance(broker_settings: BrokerSettings) -> None:
    store = BrokerStore(broker_settings)
    request = command()
    store.accept("a", request)
    store.close()
    peer = FakePeer()
    app = create_broker_app(broker_settings, peer=peer)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://broker.test") as client,
    ):
        for _ in range(50):
            response = await client.get(
                f"/v1/operations/{request['operation_id']}", headers={"Authorization": f"Bearer {TOKEN_A}"}
            )
            if response.json()["state"] == "completed":
                break
            await asyncio.sleep(0.02)
        assert response.json()["state"] == "completed"
    assert peer.closed and len(peer.submissions) == 1


async def test_first_terminal_task_is_persisted_without_get(
    ledger: BrokerStore, broker_settings: BrokerSettings
) -> None:
    class FirstTerminal(FakePeer):
        async def submit(self, claim: dict[str, Any], timeout: float) -> AsyncGenerator[A2ATaskResult, None]:
            self.submissions.append(claim)
            yield task(text="first task authoritative result")

        async def get(self, handle: str) -> A2ATaskResult:
            raise RuntimeError("peer task no longer exists")

    request = command()
    ledger.accept("a", request)
    ledger.acquire_dispatcher()
    peer = FirstTerminal()
    dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
    await settle(dispatcher)
    snapshot = ledger.get("a", request["operation_id"])
    assert snapshot["state"] == "completed"
    assert snapshot["result"]["text"] == "first task authoritative result"
    await settle(dispatcher)
    assert len(peer.submissions) == 1


async def wait_until(predicate: Any) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_retained_stream_completes_and_does_not_starve_flow_or_cancel(
    ledger: BrokerStore,
    broker_settings: BrokerSettings,
) -> None:
    first, second = command(), command()
    queued_same_flow = command(flow_id=first["flow_id"])

    class DisconnectSensitive(FakePeer):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()
            self.ready = asyncio.Event()
            self.premature_close = False

        async def submit(self, claim: dict[str, Any], timeout: float) -> AsyncGenerator[A2ATaskResult, None]:
            self.submissions.append(claim)
            handle = claim["operation_id"]
            definitive = False
            try:
                yield task(handle=handle, state="working", text="")
                if handle == first["operation_id"]:
                    self.ready.set()
                    await self.release.wait()
                definitive = True
                yield task(handle=handle, text="stream reached completion")
            finally:
                if not definitive:
                    self.premature_close = True

        async def cancel(self, handle: str) -> A2ATaskResult:
            self.cancels.append(handle)
            return task(handle=handle, state="working")

    peer = DisconnectSensitive()
    broker_settings.max_concurrent_streams = 2
    ledger.acquire_dispatcher()
    ledger.accept("a", first)
    ledger.accept("a", second)
    ledger.accept("a", queued_same_flow)
    dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
    try:
        await asyncio.wait_for(dispatcher.step(), 1)
        await peer.ready.wait()
        await wait_until(lambda: ledger.get("a", second["operation_id"])["state"] == "completed")
        assert ledger.get("a", first["operation_id"])["remote_task_id"] == first["operation_id"]
        assert ledger.get("a", queued_same_flow["operation_id"])["state"] == "accepted"
        assert not peer.premature_close
        assert len(peer.submissions) == 2 and not peer.gets
        ledger.cancel_intent("a", first["operation_id"])
        await asyncio.wait_for(dispatcher.step(), 1)
        await wait_until(lambda: len(peer.cancels) == 1)
        assert peer.cancels == [first["operation_id"]]
        assert ledger.get("a", first["operation_id"])["state"] == "running"
        peer.release.set()
        await wait_until(lambda: ledger.get("a", first["operation_id"])["state"] == "completed")
        await settle(dispatcher)
        assert ledger.get("a", queued_same_flow["operation_id"])["state"] == "completed"
        assert not peer.premature_close and len(peer.submissions) == 3
    finally:
        await dispatcher.aclose()


async def test_stream_limit_and_shutdown_keep_claims_nonreplayable(
    ledger: BrokerStore,
    broker_settings: BrokerSettings,
) -> None:
    class HeldStream(FakePeer):
        def __init__(self) -> None:
            super().__init__()
            self.exits = 0

        async def submit(self, claim: dict[str, Any], timeout: float) -> AsyncGenerator[A2ATaskResult, None]:
            self.submissions.append(claim)
            try:
                yield task(handle=claim["operation_id"], state="working")
                await asyncio.Event().wait()
            finally:
                self.exits += 1

    broker_settings.max_concurrent_streams = 2
    requests = [command() for _ in range(3)]
    ledger.acquire_dispatcher()
    for request in requests:
        ledger.accept("a", request)
    peer = HeldStream()
    dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
    await dispatcher.step()
    await wait_until(lambda: len(peer.submissions) == 2)
    assert len(dispatcher._streams) == 2
    await dispatcher.step()
    assert len(peer.submissions) == 2
    await dispatcher.aclose()
    assert peer.exits == 2 and not dispatcher._streams
    for request in requests[:2]:
        snapshot = ledger.get("a", request["operation_id"])
        assert snapshot["state"] == "outcome_unknown"
        assert snapshot["remote_task_id"] == request["operation_id"]
    assert ledger.get("a", requests[2]["operation_id"])["state"] == "accepted"
    # No claimed operation can become dispatchable, even after recovery.
    ledger.recover()
    assert ledger.claim()["operation_id"] == requests[2]["operation_id"]
    assert ledger.claim() is None


async def test_stream_saved_handle_then_error_recovers_only_by_get(
    ledger: BrokerStore,
    broker_settings: BrokerSettings,
) -> None:
    class Disconnected(FakePeer):
        async def submit(self, claim: dict[str, Any], timeout: float) -> AsyncGenerator[A2ATaskResult, None]:
            self.submissions.append(claim)
            yield task(state="working")
            raise ConnectionResetError("synthetic stream reset")

    request = command()
    ledger.accept("a", request)
    ledger.acquire_dispatcher()
    peer = Disconnected()
    dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
    try:
        await settle(dispatcher)
        unknown = ledger.get("a", request["operation_id"])
        assert unknown["state"] == "outcome_unknown" and unknown["remote_task_id"] == "peer-task"
        await settle(dispatcher)
        final = ledger.get("a", request["operation_id"])
        assert final["state"] == "completed" and final["result_id"] == unknown["result_id"]
        assert len(peer.submissions) == 1 and peer.gets == ["peer-task"]
    finally:
        await dispatcher.aclose()


def test_snapshot_timestamps_normalize_postgres_session_timezone(ledger: BrokerStore) -> None:
    with ledger.transaction() as conn:
        conn.execute("SET TIME ZONE 'Asia/Ho_Chi_Minh'")
    snapshot, _ = ledger.accept("a", command())
    assert snapshot["created_at"].endswith("Z") and snapshot["updated_at"].endswith("Z")


async def test_peer_accumulates_bounded_stream_artifacts_until_status_completion() -> None:
    class EventClient(A2AClient):
        async def _sse(
            self, method: str, params: dict[str, Any], *, timeout: float, stream_read_timeout: float | None = 10.0
        ) -> AsyncGenerator[dict[str, Any], None]:
            yield {"task": {"id": "handle", "status": {"state": "TASK_STATE_WORKING"}}}
            yield {
                "artifactUpdate": {"taskId": "handle", "artifact": {"artifactId": "a", "parts": [{"text": "first"}]}}
            }
            yield {
                "artifactUpdate": {
                    "taskId": "handle",
                    "append": True,
                    "lastChunk": True,
                    "artifact": {"artifactId": "a", "parts": [{"text": "second"}]},
                }
            }
            yield {"statusUpdate": {"taskId": "handle", "status": {"state": "TASK_STATE_COMPLETED"}}}

    peer = HermesBrokerPeer(EventClient(Settings()))
    claim = {**command(), "device_id": "a", "attachments": []}
    try:
        results = [item async for item in peer.submit(claim, 5)]
        assert results[-1].state == "completed" and results[-1].text == "firstsecond"
        assert len(results[-1].artifacts[0]["parts"]) == 1
        peer.max_snapshot_bytes = 10
        with pytest.raises(BrokerError, match="exceeds configured limit"):
            _ = [item async for item in peer.submit(claim, 5)]
    finally:
        await peer.close()


async def test_broker_silent_stream_timeout_override_preserves_legacy_default() -> None:
    observed: list[float | None] = []

    async def response(request: httpx.Request) -> httpx.Response:
        observed.append(request.extensions["timeout"]["read"])
        body = json.loads(request.content)
        event = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "task": {"id": "handle", "status": {"state": "TASK_STATE_COMPLETED"}},
            },
        }
        return httpx.Response(200, content=b"data: " + canonical(event) + b"\n\n")

    client = A2AClient(Settings())
    await client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    client._card = {"name": "synthetic"}
    peer = HermesBrokerPeer(client)
    try:
        _ = [item async for item in client._sse("SendStreamingMessage", {}, timeout=1)]
        _ = [item async for item in peer.submit({**command(), "device_id": "a", "attachments": []}, 1)]
        assert observed == [10.0, None]
    finally:
        await peer.close()


async def test_absolute_stream_deadline_closes_and_never_resends(
    ledger: BrokerStore, broker_settings: BrokerSettings
) -> None:
    class SilentPeer(FakePeer):
        def __init__(self) -> None:
            super().__init__()
            self.stream_closed = False

        async def submit(self, claim: dict[str, Any], timeout: float) -> AsyncGenerator[A2ATaskResult, None]:
            self.submissions.append(claim)
            try:
                yield task(state="working")
                await asyncio.Event().wait()
            finally:
                self.stream_closed = True

    broker_settings.peer_timeout_seconds = 1
    request = command()
    ledger.accept("a", request)
    ledger.acquire_dispatcher()
    peer = SilentPeer()
    dispatcher = BrokerDispatcher(ledger, peer, broker_settings)
    try:
        await settle(dispatcher)
        assert peer.stream_closed
        snapshot = ledger.get("a", request["operation_id"])
        assert snapshot["state"] == "outcome_unknown" and snapshot["remote_task_id"] == "peer-task"
        await settle(dispatcher)
        assert len(peer.submissions) == 1 and peer.gets == ["peer-task"]
    finally:
        await dispatcher.aclose()


async def test_peer_binary_append_chunks_preserve_exact_bytes() -> None:
    class BinaryClient(A2AClient):
        async def _sse(
            self,
            method: str,
            params: dict[str, Any],
            *,
            timeout: float,
            stream_read_timeout: float | None = 10.0,
        ) -> AsyncGenerator[dict[str, Any], None]:
            for index, content in enumerate((b"\x00first", b"\xfflast")):
                yield {
                    "artifactUpdate": {
                        "taskId": "handle",
                        "append": bool(index),
                        "lastChunk": bool(index),
                        "artifact": {"artifactId": "binary", "parts": [{"raw": base64.b64encode(content).decode()}]},
                    }
                }
            yield {"statusUpdate": {"taskId": "handle", "status": {"state": "TASK_STATE_COMPLETED"}}}

    peer = HermesBrokerPeer(BinaryClient(Settings()))
    try:
        results = [item async for item in peer.submit({**command(), "device_id": "a", "attachments": []}, 1)]
        parts = results[-1].artifacts[0]["parts"]
        assert len(parts) == 1 and base64.b64decode(parts[0]["raw"]) == b"\x00first\xfflast"
    finally:
        await peer.close()


async def test_sigterm_with_open_sse_exits_bounded_and_releases_dispatcher_lock(
    broker_settings: BrokerSettings,
) -> None:
    # Run the real production run_broker()/Uvicorn entry, replacing only Hermes
    # with a harmless peer that holds an active stream until process shutdown.
    script = """
import asyncio
import hermes_a2a_gateway.broker as broker
from hermes_a2a_gateway.models import A2ATaskResult
class HeldPeer:
    async def submit(self, claim, timeout):
        yield A2ATaskResult(task_id="held-shutdown-task", context_id="synthetic",
                           state="working", text="")
        await asyncio.Event().wait()
    async def get(self, handle):
        raise RuntimeError("no GET while the initial stream is active")
    async def cancel(self, handle):
        raise RuntimeError("no cancellation requested")
    async def close(self):
        pass
broker.HermesBrokerPeer = lambda **kwargs: HeldPeer()
broker.run_broker()
"""
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("HERMES_A2A_GATEWAY_BROKER_")}
    prefix = "HERMES_A2A_GATEWAY_BROKER_"
    environment.update(
        {
            prefix + key: value
            for key, value in {
                "DATABASE_URL": broker_settings.database_url,
                "DEVICE_TOKENS": json.dumps(broker_settings.device_tokens),
                "ENCRYPTION_KEY": broker_settings.encryption_key,
                "PUBLIC_URL": origin,
                "HOST": "127.0.0.1",
                "PORT": str(port),
                "ALLOW_LOOPBACK_HTTP": "true",
                "POLL_SECONDS": "0.05",
            }.items()
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    request = command()
    try:
        async with httpx.AsyncClient(
            base_url=origin, timeout=2, headers={"Authorization": f"Bearer {TOKEN_A}"}
        ) as client:
            async with asyncio.timeout(5):
                while True:
                    try:
                        if (await client.get("/healthz")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.02)
            assert (await client.post("/v1/operations", json=request)).status_code == 202
            async with asyncio.timeout(3):
                while True:
                    snapshot = (await client.get(f"/v1/operations/{request['operation_id']}")).json()
                    if snapshot["remote_task_id"] == "held-shutdown-task":
                        break
                    await asyncio.sleep(0.02)
            assert snapshot["state"] == "running"
            async with client.stream("GET", "/v1/events", timeout=None) as events:
                assert events.status_code == 200
                lines = events.aiter_lines()
                assert (await anext(lines)).startswith("id:")
                started = time.monotonic()
                process.send_signal(signal.SIGTERM)
                # The client deliberately remains connected until the server exits.
                await asyncio.wait_for(process.wait(), timeout=9)
                assert time.monotonic() - started < 9
                assert process.returncode in (0, -signal.SIGTERM)
        reopened = BrokerStore(broker_settings)
        try:
            reopened.acquire_dispatcher()  # Proves the old process released its advisory lock.
            final = reopened.get("a", request["operation_id"])
            assert final["state"] == "outcome_unknown"
            assert final["remote_task_id"] == "held-shutdown-task"
            assert reopened.claim() is None
        finally:
            reopened.close()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if process.stderr is not None:
            await process.stderr.read()
