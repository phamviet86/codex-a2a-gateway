from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from codex_a2a_gateway.a2a import A2AClient
from codex_a2a_gateway.core import BridgeService
from codex_a2a_gateway.models import A2AError
from codex_a2a_gateway.settings import Settings


class AmbiguousClient:
    rpc_url = "http://127.0.0.1:9900/"
    tenant = ""

    def __init__(self) -> None:
        self.send_calls = 0

    async def discover(self, *, refresh: bool = False) -> dict[str, Any]:
        return {"name": "stub"}

    async def stream_message(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.send_calls += 1
        if self.send_calls:
            raise A2AError("a2a_transport_ambiguous", "connection dropped")
        yield {}  # pragma: no cover - keeps the test double an async iterator

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_ambiguous_mutating_send_is_not_retried(tmp_path: Path) -> None:
    client = AmbiguousClient()
    service = BridgeService(
        Settings(state_path=tmp_path / "state.sqlite", conversation_dir=tmp_path / "conversations"),
        client=client,  # type: ignore[arg-type]
    )
    try:
        result = await service.chat("side effect", conversation_key="ambiguous", mode="sync")
        assert result["state"] == "outcome_unknown"
        assert result["error"]["code"] == "a2a_transport_ambiguous"
        assert client.send_calls == 1
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_agent_card_falls_back_only_on_canonical_404(tmp_path: Path) -> None:
    settings = Settings(endpoint="http://127.0.0.1:9900", state_path=tmp_path / "state.sqlite")
    client = A2AClient(settings)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(404, json={"error": "missing"})
        return httpx.Response(200, json={"name": "legacy", "url": "http://127.0.0.1:9900/"})

    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert (await client.discover())["name"] == "legacy"
        assert paths == ["/.well-known/agent-card.json", "/.well-known/agent.json"]
    finally:
        await client.aclose()
