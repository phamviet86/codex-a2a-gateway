"""Regressions for the retained modern public surface and historical store contract."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from pydantic import ValidationError

from hermes_a2a_gateway.a2a import A2AClient
from hermes_a2a_gateway.broker_peer import peer_context
from hermes_a2a_gateway.cli import build_parser
from hermes_a2a_gateway.client_settings import ClientSettings
from hermes_a2a_gateway.client_store import ClientStore
from hermes_a2a_gateway.models import A2AError
from hermes_a2a_gateway.native_delivery import NativeOrigin
from hermes_a2a_gateway.settings import Settings, is_loopback_url

# SQL dump created by actual v0.6.0b1 ClientStore at commit a139dd694299d1a3e2dca4b4ab76ded59dc087d6.
# Source SHA256: 60f93a50d43085f1d7762bcde04ec2912193cdc97d3c02adda13a792955ac0e1.
# All content, IDs and the encryption key are synthetic test data.
V06_SQL = (
    "BEGIN TRANSACTION;\n"
    "CREATE TABLE client_cursor(\n"
    "              singleton INTEGER PRIMARY KEY CHECK(singleton=1), seq INTEGER NOT NULL);\n"
    'INSERT INTO "client_cursor" VALUES(1,37);\n'
    "CREATE TABLE client_flows(thread_id TEXT PRIMARY KEY, flow_id TEXT NOT NULL UNIQUE);\n"
    "INSERT INTO \"client_flows\" VALUES('00000000-0000-0000-0000-000000000001','39be0444-e5c3-49"
    "4e-aba4-b201b9342d38');\n"
    "CREATE TABLE client_operations(\n"
    "              operation_id TEXT PRIMARY KEY, flow_id TEXT NOT NULL, thread_id TEXT NOT NUL"
    "L,\n"
    "              turn_id TEXT NOT NULL, call_id TEXT NOT NULL, digest TEXT NOT NULL, payload "
    "BLOB,\n"
    "              payload_expires REAL NOT NULL, snapshot TEXT, submission_state TEXT NOT NULL"
    ",\n"
    "              delivery_state TEXT NOT NULL, delivery_id TEXT, queue_id TEXT, inline_deadli"
    "ne REAL NOT NULL,\n"
    "              cancel_state TEXT, conflict INTEGER NOT NULL DEFAULT 0, created REAL NOT NUL"
    "L,\n"
    "              updated REAL NOT NULL, expires REAL NOT NULL, expired INTEGER NOT NULL DEFAU"
    "LT 0,\n"
    "              UNIQUE(thread_id, turn_id, call_id));\n"
    "INSERT INTO \"client_operations\" VALUES('b2718aed-fcfe-469e-a967-90545df8cb78','39be0444-e5"
    "c3-494e-aba4-b201b9342d38','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000"
    "-000000000002','frozen-v06-call','1cec5dbafd1862f24f27df6018431ae6db8145c0537c41a562c29a12"
    "498ebfdd',X'6741414141414271734747575063703351304E30425937336E5670587347684162334D794B3951"
    "33436661546A55746D45357232744953635F534E6671546B332D4C326C582D62435836497A46646B74784A3148"
    "6D586967303435524B4935346273307A3633504B5F7157434839546A4B6A7477753563654B566F4E6B6C415676"
    "794F665559355F644951544E76506C507242586A4B6F6B6E644B616573393975572D573551583942544249336F"
    "7357394A5054634B4A547A37613757386A4C616C53435A4D456C6F326A472D49675F4551536466596C71376B46"
    "42726270616E6B643479766B764147785264523772456C366244655A337767684964722D463641724979794578"
    "5252725542453963574650436C77455A773653397A30635571305F6D4441703934733179374D6535676C695972"
    "2D513D',4102444800.0,NULL,'pending','ready',NULL,NULL,0.0,NULL,0,1789944214.416884,1789944"
    "214.416884,4102444800.0,0);\n"
    "INSERT INTO \"client_operations\" VALUES('ff6f2df9-2b68-4b10-a616-4e4035454977','39be0444-e5"
    "c3-494e-aba4-b201b9342d38','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000"
    "-000000000003','frozen-v06-late-call','5b572adc62a6aae1d1268c1315eefdc0358c9397f32a99a1bec"
    'cd935823c8b96\',NULL,4102444800.0,\'{"created_at":"2026-09-20T00:00:00Z","error":null,"flow_'
    'id":"39be0444-e5c3-494e-aba4-b201b9342d38","operation_id":"ff6f2df9-2b68-4b10-a616-4e40354'
    '54977","remote_task_id":"fixture-peer-task","result":{"artifacts":[],"text":"synthetic fix'
    'ture result"},"result_id":"00000000-0000-0000-0000-000000000004","state":"completed","upda'
    "ted_at\":\"2026-09-20T00:00:01Z\"}','accepted','enqueueing','d03778a7-8982-44e9-8c1f-f0b93dd8"
    "600f',NULL,0.0,'sending',0,1789944214.4219468,1789944214.422122,4102444800.0,0);\n"
    "CREATE TABLE client_receipts(\n"
    "              operation_id TEXT NOT NULL, result_id TEXT NOT NULL, status TEXT NOT NULL,\n"
    "              sent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(operation_id,result_id,status))"
    ";\n"
    "INSERT INTO \"client_receipts\" VALUES('ff6f2df9-2b68-4b10-a616-4e4035454977','00000000-0000"
    "-0000-0000-000000000004','stored',1);\n"
    "COMMIT;"
)
V06_TEST_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


@pytest.mark.parametrize("command", ["broker", "client", "client-mcp", "client-doctor"])
def test_public_cli_accepts_only_current_commands(command):
    assert build_parser().parse_args([command]).command == command


@pytest.mark.parametrize(
    "arguments",
    [[], ["serve"], ["gateway"], ["smoke"], ["doctor"], ["install-skill"], ["install-hermes-plugin"]],
)
def test_public_cli_rejects_implicit_and_removed_modes(arguments, capsys):
    with pytest.raises(SystemExit) as failure:
        build_parser().parse_args(arguments)
    assert failure.value.code == 2
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:9900", "https://localhost:9900/a2a", "http://[::1]:9900"],
)
def test_internal_peer_accepts_loopback(endpoint):
    settings = Settings(endpoint=endpoint + "/", token="synthetic-private-token")
    assert settings.endpoint == endpoint
    assert settings.card_url == endpoint + "/.well-known/agent-card.json"
    assert "synthetic-private-token" not in repr(settings)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://192.168.1.2:9900",
        "https://example.com",
        "http://localhost.example.com",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:invalid",
        "http://user:secret@127.0.0.1",
        "http://127.0.0.1?token=secret",
        "http://127.0.0.1#secret",
        "file:///tmp/socket",
        "http://[::1",
    ],
)
def test_internal_peer_rejects_nonloopback_and_embedded_credentials(endpoint):
    assert not is_loopback_url(endpoint)
    with pytest.raises(ValidationError) as failure:
        Settings(endpoint=endpoint)
    assert endpoint not in str(failure.value)


async def mocked_peer(handler):
    client = A2AClient(Settings(token="synthetic-private-token"))
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


@pytest.mark.parametrize(
    "advertised",
    ["https://example.com/rpc", "http://127.0.0.1?token=secret", "http://user:secret@localhost", "http://127.0.0.2"],
)
async def test_discovery_rejects_unsafe_card_before_rpc(advertised):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"supportedInterfaces": [{"protocolBinding": "JSONRPC", "url": advertised}]})

    client = await mocked_peer(handler)
    try:
        with pytest.raises(A2AError) as failure:
            await client.get_task("exact-task")
        assert failure.value.code == "unsafe_agent_card"
        assert [request.method for request in requests] == ["GET"]
        assert "secret" not in str(failure.value)
    finally:
        await client.aclose()


@pytest.mark.parametrize("status", [301, 401, 403, 429])
async def test_discovery_does_not_follow_redirect_or_fallback_on_authentication_error(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://example.com/private"})

    client = await mocked_peer(handler)
    try:
        with pytest.raises(A2AError):
            await client.discover()
        assert len(requests) == 1
        assert requests[0].url.path == "/.well-known/agent-card.json"
    finally:
        await client.aclose()


async def test_peer_card_404_fallback_and_tenant_are_preserved():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("agent-card.json"):
            return httpx.Response(404)
        if request.url.path.endswith("agent.json"):
            return httpx.Response(
                200,
                json={
                    "supportedInterfaces": [
                        {"protocolBinding": "JSONRPC", "url": "http://localhost:9900/rpc", "tenant": "fixture-tenant"}
                    ]
                },
            )
        return httpx.Response(200, json={"result": {"id": "exact-task", "status": {"state": "TASK_STATE_COMPLETED"}}})

    client = await mocked_peer(handler)
    try:
        result = await client.get_task("exact-task")
        assert result.task_id == "exact-task"
        assert result.state == "completed"
        assert str(requests[-1].url) == "http://localhost:9900/rpc/"
        assert json.loads(requests[-1].content)["params"] == {"id": "exact-task", "tenant": "fixture-tenant"}
        await client.discover()
        assert len(requests) == 3
    finally:
        await client.aclose()


async def test_ambiguous_peer_mutation_is_not_replayed():
    methods = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        methods.append(json.loads(request.content)["method"])
        raise httpx.ReadTimeout("synthetic untrusted secret response")

    client = await mocked_peer(handler)
    try:
        with pytest.raises(A2AError) as failure:
            await client.cancel_task("exact-task")
        assert failure.value.code == "a2a_transport_ambiguous"
        assert not failure.value.retryable
        assert methods == ["CancelTask"]
        assert "untrusted secret" not in str(failure.value)
    finally:
        await client.aclose()


async def test_exact_peer_task_read_can_retry_without_changing_identity():
    commands = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        commands.append(json.loads(request.content))
        if len(commands) < 3:
            raise httpx.ReadTimeout("synthetic transient failure")
        return httpx.Response(200, json={"result": {"id": "exact-task", "status": {"state": "TASK_STATE_WORKING"}}})

    client = await mocked_peer(handler)
    try:
        result = await client.get_task("exact-task")
        assert result.task_id == "exact-task"
        assert len(commands) == 3
        assert commands[0] == commands[1] == commands[2]
        assert commands[0]["method"] == "GetTask"
        assert commands[0]["params"] == {"id": "exact-task"}
    finally:
        await client.aclose()


def test_v06_sqlite_copy_preserves_identity_ciphertext_receipts_and_unknown_delivery(tmp_path):
    old_dir = tmp_path / "v06"
    old_dir.mkdir(mode=0o700)
    original = old_dir / "client.sqlite3"
    with sqlite3.connect(original) as old:
        old.executescript(V06_SQL)
        old.row_factory = sqlite3.Row
        before = {row["operation_id"]: dict(row) for row in old.execute("SELECT * FROM client_operations")}
        receipts = list(old.execute("SELECT * FROM client_receipts"))
        flows = list(old.execute("SELECT * FROM client_flows"))
        new_dir = tmp_path / "v07" / "client"
        new_dir.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(new_dir / "client.sqlite3") as copied:
            old.backup(copied)
    original_bytes = original.read_bytes()
    settings = ClientSettings(
        broker_url="https://broker.invalid", token="synthetic-test-token", payload_key=V06_TEST_KEY, state_dir=new_dir
    )
    store = ClientStore(settings)
    try:
        assert store.cursor == 37
        assert list(store.db.execute("SELECT * FROM client_flows")) == flows
        assert list(store.db.execute("SELECT * FROM client_receipts WHERE status='stored'")) == receipts
        assert len(before) == 2
        for operation_id, row in before.items():
            after = store.row(operation_id)
            expected = dict(row)
            if row["delivery_state"] == "enqueueing":
                expected["delivery_state"] = "delivery_outcome_unknown"
                expected["cancel_state"] = "outcome_unknown"
                assert after["delivery_id"] is not None
                assert after["queue_id"] is None
                assert store.claim_delivery(operation_id) is None
                assert len(store.receipts()) == 1
                assert store.receipts()[0]["status"] == "delivery_outcome_unknown"
            else:
                assert store.payload(operation_id)["prompt"] == "synthetic v0.6 fixture prompt"
                assert b"synthetic v0.6 fixture prompt" not in after["payload"]
                native = NativeOrigin(row["thread_id"], row["turn_id"], row["call_id"])
                assert store.resolve_call(native)["operation_id"] == operation_id
                assert store.create(native, "synthetic v0.6 fixture prompt", [], 0) == operation_id
            assert after == expected
    finally:
        store.close()
    # A second restart must not create another delivery intent or receipt.
    reopened = ClientStore(settings)
    try:
        assert reopened.cursor == 37
        assert len(reopened.ids()) == 2
        assert len(reopened.receipts()) == 1
        assert all(reopened.claim_delivery(operation_id) is None for operation_id in before)
    finally:
        reopened.close()
    assert original.read_bytes() == original_bytes


def test_peer_context_identity_survives_package_rename():
    # Golden value from the v0.6 namespace and length-prefixed device/flow encoding.
    flow = "00000000-0000-0000-0000-000000000001"
    assert peer_context("fixture-device", flow) == "b1920e02-3a21-5d07-b862-47bef2e2cada"
    assert peer_context("another-device", flow) != peer_context("fixture-device", flow)


async def test_loopback_peer_ignores_inherited_proxy_environment(fake_a2a, monkeypatch):
    # An unusable proxy proves the real loopback HTTP requests cannot route through it.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    client = A2AClient(Settings(endpoint=fake_a2a.endpoint))
    try:
        assert await client.discover()
    finally:
        await client.aclose()
