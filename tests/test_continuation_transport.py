"""Transport evidence required before a continuation can bind a new remote ID."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from codex_a2a_gateway.a2a import A2AClient
from codex_a2a_gateway.core import BridgeService
from codex_a2a_gateway.settings import Settings


@pytest.mark.parametrize("envelope", ["exact", "wrong_id", "missing_id", "missing_jsonrpc", "wrong_jsonrpc"])
async def test_stream_envelope_must_match_submitting_rpc_before_new_binding(tmp_path: Path, envelope: str) -> None:
    settings = Settings(endpoint="http://127.0.0.1:9999", state_path=tmp_path / "state.sqlite")
    client = A2AClient(settings)
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"name": "peer", "url": settings.endpoint})
        body = json.loads(request.content)
        calls.append(body)
        assert body["method"] == "SendStreamingMessage"
        message = body["params"]["message"]
        continuation = "taskId" in message
        task = {
            "id": "remote-second" if continuation else "remote-first",
            "contextId": message["contextId"],
            "status": {"state": "TASK_STATE_COMPLETED" if continuation else "TASK_STATE_INPUT_REQUIRED"},
            "artifacts": [{"parts": [{"text": "answer" if continuation else "question"}]}],
        }
        response = {"jsonrpc": "2.0", "id": body["id"], "result": {"task": task}}
        if continuation:
            if envelope == "wrong_id":
                response["id"] = calls[0]["id"]
            elif envelope == "missing_id":
                response.pop("id")
            elif envelope == "missing_jsonrpc":
                response.pop("jsonrpc")
            elif envelope == "wrong_jsonrpc":
                response["jsonrpc"] = "1.0"
        return httpx.Response(
            200, text="data: " + json.dumps(response) + "\n\n", headers={"content-type": "text/event-stream"}
        )

    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = BridgeService(settings, client=client)
    try:
        first = await service.chat("question", mode="sync")
        second = await service.chat("answer", task_id=first["bridge_task_id"], mode="sync")
        assert second["state"] == ("completed" if envelope == "exact" else "outcome_unknown")
        assert second["a2a_task_id"] == ("remote-second" if envelope == "exact" else "remote-first")
        assert bool(second["result_id"]) is (envelope == "exact")
        assert len(calls) == 2
    finally:
        await service.aclose()


@pytest.mark.parametrize("event_kind", ["statusUpdate", "artifactUpdate"])
@pytest.mark.parametrize("metadata", [{"requestMessageId": "wrong"}, {"requestMessageId": None}, "invalid"])
def test_update_parser_preserves_conflicting_message_metadata(event_kind: str, metadata: Any) -> None:
    update = {
        "taskId": "task",
        "contextId": "context",
        "status": {"state": "TASK_STATE_WORKING"},
        "artifact": {"parts": [{"text": "piece"}]},
        "metadata": metadata,
    }
    parsed = A2AClient.parse_stream_event({event_kind: update}, fallback_context="context")
    assert parsed is not None
    assert parsed.raw["metadata"] == metadata


@pytest.mark.parametrize("event_kind", ["statusUpdate", "artifactUpdate"])
def test_update_parser_does_not_invent_wire_context(event_kind: str) -> None:
    parsed = A2AClient.parse_stream_event({event_kind: {"taskId": "task"}}, fallback_context="context")
    assert parsed is not None and parsed.context_id == "context"
    assert not parsed.raw.get("contextId")
