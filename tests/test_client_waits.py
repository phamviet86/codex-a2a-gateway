from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet

from hermes_a2a_gateway.client import ClientService, create_client_app
from hermes_a2a_gateway.client_settings import ClientSettings


@pytest.fixture
def client(tmp_path):
    settings = ClientSettings(
        broker_url="https://broker.invalid",
        token="test",
        payload_key=Fernet.generate_key().decode(),
        state_dir=tmp_path,
        inline_wait_seconds=15,
    )
    return ClientService(
        settings,
        broker=httpx.AsyncClient(
            base_url=settings.broker_url, transport=httpx.MockTransport(lambda request: httpx.Response(404))
        ),
    )


def metadata():
    return {"threadId": str(uuid4()), "callId": str(uuid4()), "x-codex-turn-metadata": {"turn_id": str(uuid4())}}


@pytest.mark.parametrize("wait,expected", [(0, 0), (20, 20), (60, 60), (None, 15)])
async def test_submit_explicit_wait_is_not_limited_by_configured_default(client, monkeypatch, wait, expected):
    observed = []

    async def bounded_wait(origin, operation_id, seconds, *, refresh=True):
        observed.append(seconds)
        return client.store.view(operation_id, origin)

    monkeypatch.setattr(client, "wait", bounded_wait)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_client_app(client, manage_lifecycle=False)), base_url="http://client"
    ) as ipc:
        response = await ipc.post(
            "/command",
            json={"action": "submit", "meta": metadata(), "arguments": {"prompt": "harmless", "wait_seconds": wait}},
        )
    assert response.status_code == 200
    assert len(client.store.ids()) == 1
    assert observed == [expected]
    await client.close()


@pytest.mark.parametrize("wait", [-1, 61, 10**1000, float("nan"), float("inf"), float("-inf"), "20", True, [], {}])
async def test_invalid_wait_reports_safe_actionable_error_before_persistence(client, wait):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_client_app(client, manage_lifecycle=False)), base_url="http://client"
    ) as ipc:
        response = await ipc.post(
            "/command",
            content=json.dumps(
                {
                    "action": "submit",
                    "meta": metadata(),
                    "arguments": {"prompt": "PRIVATE PROMPT", "wait_seconds": wait},
                }
            ),
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_wait"
    assert response.json()["error"]["field"] == "wait_seconds"
    assert "0 to 60" in response.json()["error"]["message"]
    assert "PRIVATE PROMPT" not in response.text
    assert client.store.ids() == []
    await client.close()


@pytest.mark.parametrize(
    "arguments,code",
    [
        (None, "invalid_arguments"),
        ({}, "invalid_prompt"),
        ({"prompt": 7}, "invalid_prompt"),
        ({"prompt": "p", "artifact_ids": "bad"}, "invalid_artifact_ids"),
        ({"prompt": "p", "artifact_ids": ["not-uuid"]}, "invalid_artifact_ids"),
    ],
)
async def test_malformed_submit_arguments_have_specific_safe_errors(client, arguments, code):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_client_app(client, manage_lifecycle=False)), base_url="http://client"
    ) as ipc:
        response = await ipc.post("/command", json={"action": "submit", "meta": metadata(), "arguments": arguments})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert client.store.ids() == []
    await client.close()


@pytest.mark.parametrize("count,status", [(16, 200), (17, 400)])
async def test_artifact_count_matches_broker_before_persistence(client, count, status):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_client_app(client, manage_lifecycle=False)), base_url="http://client"
    ) as ipc:
        response = await ipc.post(
            "/command",
            json={
                "action": "submit",
                "meta": metadata(),
                "arguments": {
                    "prompt": "harmless",
                    "artifact_ids": [str(uuid4()) for _ in range(count)],
                    "wait_seconds": 0,
                },
            },
        )
    assert response.status_code == status
    assert len(client.store.ids()) == (1 if count == 16 else 0)
    if status == 400:
        assert response.json()["error"]["code"] == "invalid_artifact_ids"
    await client.close()
