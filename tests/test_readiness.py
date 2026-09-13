from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from codex_a2a_gateway import cli, readiness
from codex_a2a_gateway.settings import Settings


@pytest.mark.asyncio
async def test_inbound_readiness_needs_no_hermes_or_live_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = (
            {"ok": True, "service": "codex-a2a-gateway"}
            if request.url.path == "/health"
            else {"supportedInterfaces": [{"protocolVersion": "1.0"}]}
        )
        return httpx.Response(200, json=payload)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        readiness.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    monkeypatch.setattr(readiness.shutil, "which", lambda name: "/fixture/codex")
    monkeypatch.setenv("CODEX_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("CODEX_A2A_BEARER_TOKEN", "secret-fixture-token")
    monkeypatch.setattr(cli, "BridgeService", lambda settings: pytest.fail("inbound contacted Hermes or opened state"))
    assert await cli._status("inbound") == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["model_execution"] == result["authentication"] == result["worker_tools"] == "unverified"
    assert "secret-fixture-token" not in output
    assert len(requests) == 2 and all(request.method == "GET" for request in requests)
    assert all(request.headers["authorization"] == "Bearer secret-fixture-token" for request in requests)
    assert not (tmp_path / "state.sqlite3").exists()


@pytest.mark.asyncio
async def test_readiness_does_not_follow_redirects_or_expose_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.invalid"}, text="secret-fixture-token")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        readiness.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    monkeypatch.setattr(readiness.shutil, "which", lambda name: None)
    result = await readiness.inbound_status(Settings(codex_workspace=tmp_path / "absent"))
    assert result["ok"] is False
    assert not result["checks"]["workspace_exists"] and not result["checks"]["codex_executable_found"]
    assert set(result["errors"].values()) == {"HTTPStatusError"}
    assert "secret-fixture-token" not in json.dumps(result)
