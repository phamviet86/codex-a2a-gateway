"""Read-only inbound transport checks; never submit a Codex model turn."""

from __future__ import annotations

import shutil
from typing import Any

import httpx

from .settings import Settings


async def inbound_status(settings: Settings) -> dict[str, Any]:
    host = settings.inbound_host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1" if host == "0.0.0.0" else "::1"
    if ":" in host:
        host = f"[{host}]"
    endpoint = f"http://{host}:{settings.inbound_port}"
    checks: dict[str, bool] = {
        "workspace_exists": settings.codex_workspace.is_dir(),
        "codex_executable_found": shutil.which(settings.codex_bin) is not None,
        "gateway_health": False,
        "agent_card": False,
    }
    errors: dict[str, str] = {}
    headers = {"Authorization": f"Bearer {settings.inbound_token}"} if settings.inbound_token else {}
    async with httpx.AsyncClient(timeout=settings.connect_timeout, follow_redirects=False, trust_env=False) as client:
        for name, suffix in (("gateway_health", "/health"), ("agent_card", "/.well-known/agent-card.json")):
            try:
                response = await client.get(endpoint + suffix, headers=headers)
                response.raise_for_status()
                payload = response.json()
                if name == "gateway_health":
                    checks[name] = payload.get("ok") is True and payload.get("service") == "codex-a2a-gateway"
                else:
                    interfaces = payload.get("supportedInterfaces", [])
                    checks[name] = any(
                        isinstance(item, dict) and item.get("protocolVersion") == "1.0" for item in interfaces
                    )
                if not checks[name]:
                    errors[name] = "unexpected_response"
            except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
                # Do not echo response bodies, URLs with credentials, or exception text.
                errors[name] = type(exc).__name__
    return {
        "ok": all(checks.values()),
        "mode": "inbound",
        "checks": checks,
        "errors": errors,
        "authentication": "unverified",
        "model_execution": "unverified",
        "worker_tools": "unverified",
        "note": "Read-only transport checks do not prove Codex sign-in, model execution, or worker MCP availability.",
    }
