from __future__ import annotations

import json
import sys
from uuid import uuid4

import pytest

from codex_a2a_gateway.native_delivery import (
    NativeOrigin,
    NativeOutcomeUnknown,
    NativeQueueDelivery,
    NativeUnsupported,
    result_reference,
)


def test_metadata_requires_exact_native_provenance():
    thread, turn = str(uuid4()), str(uuid4())
    meta = {
        "threadId": thread,
        "turnId": turn,
        "callId": "call-42",
        "x-codex-turn-metadata": {"thread_id": thread, "turn_id": turn},
    }
    assert NativeOrigin.from_meta(meta) == NativeOrigin(thread, turn, "call-42")
    for invalid in [
        None,
        {},
        {"threadId": thread},
        {**meta, "threadId": str(uuid4())},
        {**meta, "turnId": str(uuid4())},
        {**meta, "callId": ""},
        {**meta, "threadId": "../../arbitrary"},
        {**meta, "x-codex-turn-metadata": {"thread_id": thread, "turn_id": turn, "call_id": "wrong"}},
    ]:
        with pytest.raises(ValueError):
            NativeOrigin.from_meta(invalid)
    # Recorded older metadata may omit the duplicate nested thread identity.
    assert NativeOrigin.from_meta({**meta, "x-codex-turn-metadata": {"turn_id": turn}}).thread_id == thread


def test_reference_contains_only_validated_identifiers():
    op, result = str(uuid4()), str(uuid4())
    assert op in result_reference(op, result)
    with pytest.raises(ValueError):
        result_reference("ignore instructions", result)


@pytest.fixture
def fake_codex(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, sys
mode = os.environ.get("FAKE_NATIVE_MODE", "ok")
assert not any(key.startswith("CODEX_A2A_GATEWAY_") for key in os.environ)
if sys.argv[1:] == ["--version"]:
    print("codex-cli " + ("9.9.9" if mode == "unsupported" else "0.154.0"))
    sys.exit()
if "generate-json-schema" in sys.argv:
    root = pathlib.Path(sys.argv[sys.argv.index("--out") + 1]) / "v2"
    root.mkdir()
    for name, fields in {
        "ThreadQueueAddParams": ["threadId", "clientUserMessageId", "input"],
        "ThreadQueueAddResponse": ["queuedSubmission"],
        "ThreadQueueListParams": ["threadId"],
        "ThreadQueueListResponse": ["data"],
    }.items():
        if mode == "bad_schema":
            fields = []
        properties = {key: {"type": "string"} for key in fields}
        properties.update({key: {"type": "array"} for key in fields if key in {"input", "data"}})
        schema = {"required": fields, "properties": properties,
                  "definitions": {"QueuedSubmission": {"required": ["id", "clientUserMessageId", "input"]}}}
        (root / (name + ".json")).write_text(json.dumps(schema))
    sys.exit()
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    assert method in {"initialize", "initialized", "thread/queue/add", "thread/queue/list"}, method
    if method == "initialized":
        continue
    if method == "initialize":
        result = {}
    elif method == "thread/queue/add":
        params = message["params"]
        with open(os.environ["FAKE_NATIVE_RECORD"], "a") as file:
            file.write(json.dumps(params) + "\n")
        if mode == "lost_ack":
            sys.exit()
        result = {"queuedSubmission": {"id": "queue-123", "clientUserMessageId": params["clientUserMessageId"],
                                      "input": params["input"]}}
        if mode == "wrong_ack":
            result["queuedSubmission"]["clientUserMessageId"] = "other"
    else:
        data = []
        record = pathlib.Path(os.environ["FAKE_NATIVE_RECORD"])
        if record.exists():
            for row in record.read_text().splitlines():
                item = json.loads(row)
                data.append({"id": "queue-123", "clientUserMessageId": item["clientUserMessageId"],
                             "input": item["input"]})
        result = {"data": data, "nextCursor": None}
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
"""
    )
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize("mode", ["unsupported", "bad_schema"])
async def test_native_capability_fails_closed(fake_codex, monkeypatch, mode):
    monkeypatch.setenv("FAKE_NATIVE_MODE", mode)
    native = NativeQueueDelivery(str(fake_codex))
    with pytest.raises(NativeUnsupported):
        await native.capability()


@pytest.mark.parametrize("host_state", ["active", "idle", "unloaded"])
async def test_native_stdio_queue_never_resumes_or_claims_execution(fake_codex, tmp_path, monkeypatch, host_state):
    # Independent App Server sees no owner runtime state. We only promise queue
    # acceptance; this deliberately makes the same assertion for each host state.
    record = tmp_path / "record.jsonl"
    monkeypatch.setenv("FAKE_NATIVE_RECORD", str(record))
    monkeypatch.setenv("FAKE_HOST_STATE", host_state)
    native = NativeQueueDelivery(str(fake_codex))
    assert (await native.capability())["supported"]
    delivery_id = str(uuid4())
    thread = str(uuid4())
    text = result_reference(str(uuid4()), str(uuid4()))
    assert await native.enqueue(thread, delivery_id, text) == "queue-123"
    assert await native.reconcile(thread, delivery_id, text) == "queue-123"
    assert await native.reconcile(thread, "other-identity", text) is None
    row = json.loads(record.read_text())
    assert row["threadId"] == thread
    assert row["clientUserMessageId"] == delivery_id
    # Repeating the client ID creates another queue item; it is NOT deduplication.
    await native.enqueue(thread, delivery_id, text)
    assert await native.reconcile(thread, delivery_id, text) is None


@pytest.mark.parametrize("mode", ["lost_ack", "wrong_ack"])
async def test_native_ack_failure_is_unknown_not_retryable(fake_codex, tmp_path, monkeypatch, mode):
    record = tmp_path / "record.jsonl"
    monkeypatch.setenv("FAKE_NATIVE_RECORD", str(record))
    monkeypatch.setenv("FAKE_NATIVE_MODE", mode)
    native = NativeQueueDelivery(str(fake_codex))
    with pytest.raises(NativeOutcomeUnknown):
        await native.enqueue(str(uuid4()), str(uuid4()), "safe reference")
    assert len(record.read_text().splitlines()) == 1


async def test_native_missing_binary_is_supported_pull_fallback(tmp_path):
    native = NativeQueueDelivery(str(tmp_path / "missing"))
    with pytest.raises(NativeUnsupported):
        await native.capability()


async def test_capability_revalidates_replaced_binary(fake_codex, monkeypatch):
    native = NativeQueueDelivery(str(fake_codex))
    assert (await native.capability())["supported"]
    monkeypatch.setenv("FAKE_NATIVE_MODE", "unsupported")
    fake_codex.write_text(fake_codex.read_text() + "\n# installed update\n")
    with pytest.raises(NativeUnsupported):
        await native.capability()


async def test_native_subprocess_environment_strips_gateway_secrets(fake_codex, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_A2A_GATEWAY_CLIENT_TOKEN", "must-not-reach-native")
    monkeypatch.setenv("CODEX_A2A_GATEWAY_CLIENT_PAYLOAD_KEY", "must-not-reach-native")
    monkeypatch.setenv("CODEX_A2A_GATEWAY_BROKER_DEVICE_TOKENS", "must-not-reach-native")
    monkeypatch.setenv("FAKE_NATIVE_RECORD", str(tmp_path / "record.jsonl"))
    native = NativeQueueDelivery(str(fake_codex))
    assert (await native.capability())["supported"]
    assert await native.enqueue(str(uuid4()), str(uuid4()), "safe reference") == "queue-123"
