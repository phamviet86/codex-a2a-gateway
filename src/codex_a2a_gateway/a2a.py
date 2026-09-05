from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from . import __version__
from .models import A2A_STATE_MAP, A2AError, A2ATaskResult
from .settings import Settings, is_loopback_url


class A2AClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        timeout = httpx.Timeout(
            connect=settings.connect_timeout,
            read=max(10.0, settings.default_timeout),
            write=10.0,
            pool=5.0,
        )
        headers = {"A2A-Version": "1.0", "User-Agent": f"codex-a2a-gateway/{__version__}"}
        if settings.token:
            headers["Authorization"] = f"Bearer {settings.token}"
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=False)
        self._card: dict[str, Any] | None = None
        self._rpc_url = settings.endpoint + "/"
        self._tenant = ""

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _http_error(exc: httpx.HTTPStatusError) -> A2AError:
        status = exc.response.status_code
        if status in (401, 403):
            return A2AError("a2a_auth", f"Hermes rejected authentication (HTTP {status})", retryable=False)
        if status == 429:
            return A2AError("a2a_rate_limited", "Hermes rate limited the bridge", retryable=True)
        if status == 404:
            return A2AError("a2a_not_found", "Hermes endpoint was not found (HTTP 404)", retryable=False)
        return A2AError("a2a_http", f"Hermes HTTP error {status}", retryable=status >= 500)

    async def _get_json(self, url: str, *, retries: int = 2) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = await self._client.get(url)
                response.raise_for_status()
                value = response.json()
                if not isinstance(value, dict):
                    raise A2AError("a2a_invalid_response", "Hermes returned a non-object JSON response")
                return value
            except httpx.HTTPStatusError as exc:
                mapped = self._http_error(exc)
                if not mapped.retryable or mapped.code == "a2a_rate_limited" or attempt >= retries:
                    raise mapped from exc
                last = exc
            except (httpx.TransportError, json.JSONDecodeError) as exc:
                last = exc
                if attempt >= retries:
                    raise A2AError(
                        "a2a_unreachable", f"Could not reach Hermes: {type(exc).__name__}", retryable=True
                    ) from exc
            await asyncio.sleep(0.1 * (2**attempt))
        raise A2AError("a2a_unreachable", f"Could not reach Hermes: {last}", retryable=True)

    async def discover(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._card is not None and not refresh:
            return self._card
        try:
            card = await self._get_json(self.settings.card_url)
        except A2AError as exc:
            if exc.code != "a2a_not_found":
                raise
            card = await self._get_json(self.settings.legacy_card_url)
        selected: dict[str, Any] | None = None
        for iface in card.get("supportedInterfaces") or []:
            if isinstance(iface, dict) and iface.get("protocolBinding") == "JSONRPC" and iface.get("url"):
                selected = iface
                break
        candidate = str((selected or {}).get("url") or card.get("url") or self.settings.endpoint).strip()
        if not is_loopback_url(candidate):
            raise A2AError("unsafe_agent_card", "Hermes Agent Card advertised a non-loopback RPC URL")
        parsed = urlparse(candidate)
        endpoint = self.settings.endpoint
        expected = urlparse(endpoint)
        if parsed.hostname not in {expected.hostname, "localhost", "127.0.0.1", "::1"}:
            raise A2AError("unsafe_agent_card", "Hermes Agent Card changed the loopback host")
        self._rpc_url = candidate.rstrip("/") + "/"
        self._tenant = str((selected or {}).get("tenant") or "")
        self._card = card
        return card

    async def health(self) -> dict[str, Any]:
        return await self._get_json(self.settings.health_url)

    @property
    def rpc_url(self) -> str:
        return self._rpc_url

    @property
    def tenant(self) -> str:
        return self._tenant

    def _rpc_body(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._tenant and "tenant" not in params:
            params = {**params, "tenant": self._tenant}
        return {"jsonrpc": "2.0", "id": f"bridge-{uuid.uuid4().hex}", "method": method, "params": params}

    async def _post_rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        read_only: bool = False,
    ) -> Any:
        await self.discover()
        body = self._rpc_body(method, params)
        attempts = 3 if read_only else 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self._client.post(self._rpc_url, json=body, timeout=timeout)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise A2AError("a2a_invalid_response", "Hermes returned a non-object JSON-RPC response")
                if "error" in payload:
                    err = payload.get("error") or {}
                    rpc_code = err.get("code") if isinstance(err, dict) else None
                    message = str(err.get("message") if isinstance(err, dict) else err)
                    code = "a2a_task_not_found" if rpc_code == -32001 else "a2a_rpc_error"
                    raise A2AError(code, message, retryable=rpc_code == -32051, rpc_code=rpc_code)
                return payload.get("result")
            except httpx.HTTPStatusError as exc:
                mapped = self._http_error(exc)
                if (
                    not read_only
                    or not mapped.retryable
                    or mapped.code == "a2a_rate_limited"
                    or attempt + 1 >= attempts
                ):
                    raise mapped from exc
                last = exc
            except (httpx.TransportError, json.JSONDecodeError) as exc:
                last = exc
                if not read_only or attempt + 1 >= attempts:
                    code = "a2a_transport_ambiguous" if not read_only else "a2a_unreachable"
                    raise A2AError(code, f"Hermes transport failed: {type(exc).__name__}", retryable=read_only) from exc
            await asyncio.sleep(0.1 * (2**attempt))
        raise A2AError("a2a_unreachable", f"Hermes request failed: {last}", retryable=True)

    @staticmethod
    def _unwrap_task(result: Any) -> dict[str, Any]:
        if isinstance(result, dict) and isinstance(result.get("task"), dict):
            return result["task"]
        if isinstance(result, dict) and isinstance(result.get("message"), dict):
            return result["message"]
        if isinstance(result, dict):
            return result
        raise A2AError("a2a_invalid_response", "Hermes result did not contain a Task or Message")

    @staticmethod
    def _part_text(part: dict[str, Any]) -> str:
        if isinstance(part.get("text"), str):
            return part["text"]
        if "data" in part:
            return json.dumps(part["data"], ensure_ascii=False)
        if isinstance(part.get("url"), str):
            return part["url"]
        return ""

    @classmethod
    def _message_text(cls, message: dict[str, Any] | None) -> str:
        if not isinstance(message, dict):
            return ""
        return "\n".join(
            filter(None, (cls._part_text(p) for p in message.get("parts") or [] if isinstance(p, dict)))
        ).strip()

    @classmethod
    def parse_task(cls, result: Any, *, fallback_context: str = "") -> A2ATaskResult:
        task = cls._unwrap_task(result)
        status = task.get("status") or {}
        state_raw = str(status.get("state") or "TASK_STATE_COMPLETED")
        state = A2A_STATE_MAP.get(state_raw, state_raw.lower().replace("task_state_", ""))
        artifacts = [a for a in (task.get("artifacts") or []) if isinstance(a, dict)]
        text = ""
        for artifact in artifacts:
            text = cls._message_text(artifact)
            if text:
                break
        if not text:
            text = cls._message_text(status.get("message"))
        if not text and task.get("role"):
            text = cls._message_text(task)
        return A2ATaskResult(
            task_id=str(task.get("id") or task.get("taskId") or ""),
            context_id=str(task.get("contextId") or fallback_context),
            state=state,
            text=text,
            artifacts=artifacts,
            raw=task,
        )

    @staticmethod
    def _message(message: str, context_id: str, message_id: str) -> dict[str, Any]:
        return {
            "messageId": message_id,
            "role": "ROLE_USER",
            "contextId": context_id,
            "parts": [{"text": message, "mediaType": "text/plain"}],
        }

    async def send_message(self, message: str, context_id: str, message_id: str, *, timeout: float) -> A2ATaskResult:
        result = await self._post_rpc(
            "SendMessage",
            {"message": self._message(message, context_id, message_id)},
            timeout=timeout,
            read_only=False,
        )
        return self.parse_task(result, fallback_context=context_id)

    async def _sse(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> AsyncIterator[dict[str, Any]]:
        await self.discover()
        body = self._rpc_body(method, params)
        stream_timeout = httpx.Timeout(connect=self.settings.connect_timeout, read=10.0, write=10.0, pool=5.0)
        try:
            async with asyncio.timeout(timeout):
                async with self._client.stream("POST", self._rpc_url, json=body, timeout=stream_timeout) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw:
                            continue
                        envelope = json.loads(raw)
                        if not isinstance(envelope, dict):
                            continue
                        if "error" in envelope:
                            err = envelope.get("error") or {}
                            raise A2AError("a2a_rpc_error", str(err.get("message") if isinstance(err, dict) else err))
                        result = envelope.get("result", envelope)
                        if isinstance(result, dict):
                            yield result
        except TimeoutError as exc:
            raise A2AError("a2a_timeout", "A2A stream exceeded its absolute timeout", retryable=False) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500 and method == "SendStreamingMessage":
                raise A2AError("a2a_transport_ambiguous", "server error after sending request") from exc
            raise self._http_error(exc) from exc
        except httpx.TransportError as exc:
            raise A2AError("a2a_transport_ambiguous", f"A2A stream failed: {type(exc).__name__}") from exc
        except json.JSONDecodeError as exc:
            raise A2AError("a2a_invalid_sse", "A2A SSE contained invalid JSON") from exc

    async def stream_message(
        self,
        message: str,
        context_id: str,
        message_id: str,
        *,
        timeout: float,
        task_id: str | None = None,
        origin: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        wire_message = self._message(message, context_id, message_id)
        if task_id:
            wire_message["taskId"] = task_id
        if origin:
            wire_message["metadata"] = {"origin": origin}
        async for event in self._sse(
            "SendStreamingMessage",
            {"message": wire_message},
            timeout=timeout,
        ):
            yield event

    async def get_task(self, task_id: str, *, timeout: float = 10.0) -> A2ATaskResult:
        result = await self._post_rpc("GetTask", {"id": task_id}, timeout=timeout, read_only=True)
        return self.parse_task(result)

    async def list_tasks(
        self,
        *,
        context_id: str = "",
        state: str = "",
        page_token: str = "",
        limit: int = 50,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pageSize": max(1, min(limit, 100)),
            "includeArtifacts": True,
        }
        if page_token:
            params["pageToken"] = page_token
        if context_id:
            params["contextId"] = context_id
        if state:
            params["status"] = state
        result = await self._post_rpc("ListTasks", params, timeout=timeout, read_only=True)
        return result if isinstance(result, dict) else {"tasks": []}

    async def cancel_task(self, task_id: str, *, timeout: float = 10.0) -> A2ATaskResult:
        result = await self._post_rpc("CancelTask", {"id": task_id}, timeout=timeout, read_only=False)
        return self.parse_task(result)

    async def subscribe_task(self, task_id: str, *, timeout: float) -> AsyncIterator[dict[str, Any]]:
        async for event in self._sse("SubscribeToTask", {"id": task_id}, timeout=timeout):
            yield event

    @classmethod
    def parse_stream_event(cls, event: dict[str, Any], *, fallback_context: str = "") -> A2ATaskResult | None:
        if isinstance(event.get("task"), dict):
            return cls.parse_task(event["task"], fallback_context=fallback_context)
        update = event.get("statusUpdate")
        if isinstance(update, dict):
            task = {
                "id": update.get("taskId"),
                "contextId": update.get("contextId") or fallback_context,
                "status": update.get("status") or {},
            }
            return cls.parse_task(task, fallback_context=fallback_context)
        artifact = event.get("artifactUpdate")
        if isinstance(artifact, dict):
            task = {
                "id": artifact.get("taskId"),
                "contextId": artifact.get("contextId") or fallback_context,
                "status": {"state": "TASK_STATE_WORKING"},
                "artifacts": [artifact.get("artifact") or {}],
            }
            return cls.parse_task(task, fallback_context=fallback_context)
        return None
