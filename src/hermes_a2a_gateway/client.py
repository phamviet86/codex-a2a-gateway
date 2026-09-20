"""Private Unix HTTP daemon for durable broker commands and local native delivery.

Run with ``python -m hermes_a2a_gateway.client``; add ``doctor`` for a read-only
capability report. MCP processes use the Unix socket and never open SQLite.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import socket
import ssl
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from cryptography.fernet import InvalidToken
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .client_settings import PREFIX, ClientSettings
from .client_store import ClientStore
from .native_delivery import (
    DesktopHostDelivery,
    NativeOrigin,
    NativeOutcomeUnknown,
    NativeQueueDelivery,
    NativeUnsupported,
    native_uuid,
    result_reference,
)
from .ssh_tunnel import SSHTunnel


class ClientValidationError(ValueError):
    def __init__(self, code: str, message: str, field: str) -> None:
        super().__init__(message)
        self.payload = {"error": {"code": code, "message": message, "field": field}}


class BrokerTransport(httpx.AsyncBaseTransport):
    """Verified HTTPS with optional configured SNI and an owned-tunnel guard."""

    def __init__(self, settings: ClientSettings, tunnel: SSHTunnel | None) -> None:
        self.settings, self.tunnel = settings, tunnel
        self._transport = self._new_transport()
        self._generation = 0
        self._rotation = asyncio.Lock()
        self.tls = "not_checked"
        self.broker = "not_checked"

    def _new_transport(self) -> httpx.AsyncHTTPTransport:
        return httpx.AsyncHTTPTransport(verify=self.settings.ssl_context(), trust_env=False, retries=0)

    def health(self) -> dict[str, Any]:
        paused = self.tunnel is not None and (not self.tunnel.available or self.tunnel.generation != self._generation)
        return {
            "ssh": self.tunnel.health() if self.tunnel else {"mode": "direct", "state": "disabled"},
            "tls": {"verification_enabled": True, "state": "not_checked" if paused else self.tls},
            "broker": {"state": "paused" if paused else self.broker},
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.tunnel is not None:
            if not self.tunnel.available:
                raise httpx.ConnectError("ssh_tunnel_unavailable", request=request)
            async with self._rotation:
                if self._generation != self.tunnel.generation:
                    old = self._transport
                    self._transport = self._new_transport()
                    self._generation = self.tunnel.generation
                    self.tls, self.broker = "not_checked", "not_checked"
                    await old.aclose()
            if self.settings.ssh_tls_server_name:
                name = self.settings.ssh_tls_server_name
                request.extensions["sni_hostname"] = name
                authority = f"[{name}]" if ":" in name else name
                if self.settings.ssh_remote_port != 443:
                    authority += f":{self.settings.ssh_remote_port}"
                request.headers["Host"] = authority
        try:
            response = await self._transport.handle_async_request(request)
        except httpx.HTTPError as exc:
            cause: BaseException | None = exc
            tls_failure = False
            for _ in range(10):
                if cause is None:
                    break
                tls_failure |= isinstance(cause, ssl.SSLError)
                cause = cause.__cause__ or cause.__context__
            self.tls = "verification_failed" if tls_failure else "not_checked"
            self.broker = "not_checked" if tls_failure else "connection_failed"
            raise
        self.tls = "verified" if request.url.scheme == "https" else "development_http"
        self.broker = (
            "authentication_failed"
            if response.status_code in {401, 403}
            else "unavailable"
            if response.status_code >= 500
            else "reachable"
            if request.url.path == "/healthz"
            else "authenticated"
        )
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


class ClientService:
    def __init__(
        self,
        settings: ClientSettings,
        *,
        broker: httpx.AsyncClient | None = None,
        delivery: DesktopHostDelivery | None = None,
    ) -> None:
        self.settings = settings
        self.tunnel = SSHTunnel(settings.ssh_config()) if settings.transport_mode == "ssh-tunnel" else None
        self.transport = BrokerTransport(settings, self.tunnel) if broker is None else None
        self.store = ClientStore(settings)
        self.broker = broker or httpx.AsyncClient(
            base_url=settings.broker_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.token}"},
            transport=self.transport,
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self.delivery = delivery or NativeQueueDelivery(settings.codex_command, settings.request_timeout_seconds)
        self.tasks: list[asyncio.Task[None]] = []
        self.changed = asyncio.Event()
        self.wake = asyncio.Event()
        self.last_transport_error: str | None = None
        self.native_status: dict[str, Any] = {"supported": False, "reason": "not_checked"}
        self._unknown_reconciled: set[str] = set()

    async def start(self) -> None:
        if self.tunnel is not None:
            await self.tunnel.start()
        if self.settings.native_delivery:
            try:
                self.native_status = await self.delivery.capability()
            except NativeUnsupported:
                self.native_status = {"supported": False, "reason": "unsupported_cli_or_schema_use_explicit_pull"}
        else:
            self.native_status = {"supported": False, "reason": "disabled_use_explicit_pull"}
        self.tasks = [asyncio.create_task(self._work()), asyncio.create_task(self._events())]

    async def close(self) -> None:
        try:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.broker.aclose()
        finally:
            try:
                if self.tunnel is not None:
                    await self.tunnel.close()
            finally:
                self.store.close()

    def ingest(self, snapshot: Any, seq: int | None = None, *, expected_id: str | None = None) -> None:
        if not isinstance(snapshot, dict) or (expected_id is not None and snapshot.get("operation_id") != expected_id):
            raise ValueError("broker returned a different operation identity")
        self.store.accept_snapshot(snapshot, seq)
        self.changed.set()
        self.wake.set()

    async def refresh(self, operation_id: str) -> bool:
        response = await self.broker.get(f"/v1/operations/{operation_id}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        self.ingest(response.json(), expected_id=operation_id)
        return True

    async def recover(self) -> None:
        for operation_id in self.store.ids():
            await self.refresh(operation_id)

    async def dispatch_pending(self) -> None:
        for operation_id in self.store.ids(pending=True):
            # GET first after an ambiguous broker ACK. Only this new broker API has
            # an exact-ID idempotency contract; this never retries Hermes directly.
            if await self.refresh(operation_id):
                continue
            try:
                payload = self.store.payload(operation_id)
            except InvalidToken:
                self.store.mark_submission(operation_id, "payload_key_unavailable")
                continue
            if payload is None:
                self.store.mark_submission(operation_id, "outcome_unknown")
                continue
            response = await self.broker.post("/v1/operations", json=payload)
            if response.status_code in {400, 403, 409, 413, 422}:
                self.store.mark_submission(operation_id, "rejected")
                self.changed.set()
                continue
            response.raise_for_status()
            self.ingest(response.json(), expected_id=operation_id)

    async def flush_receipts(self) -> None:
        for receipt in self.store.receipts():
            response = await self.broker.post(
                "/v1/receipts", json={key: receipt[key] for key in ("operation_id", "result_id", "status")}
            )
            response.raise_for_status()
            self.store.receipt_sent(receipt)

    async def deliver_ready(self) -> None:
        for operation_id in self.store.ids():
            row = self.store.row(operation_id)
            if row["delivery_state"] == "delivery_outcome_unknown" and operation_id not in self._unknown_reconciled:
                self._unknown_reconciled.add(operation_id)
                if self.native_status["supported"] and row["snapshot"]:
                    snapshot = json.loads(row["snapshot"])
                    text = result_reference(operation_id, snapshot["result_id"])
                    try:
                        queue_id = await self.delivery.reconcile(row["thread_id"], row["delivery_id"], text)
                        if queue_id:
                            self.store.finish_delivery(operation_id, "queued", queue_id)
                    except (NativeUnsupported, NativeOutcomeUnknown):
                        pass
                continue
            claimed = self.store.claim_delivery(operation_id)
            if not claimed:
                continue
            if not self.native_status["supported"]:
                self.store.finish_delivery(operation_id, "unsupported")
                continue
            text = result_reference(operation_id, claimed["snapshot"]["result_id"])
            try:
                queue_id = await self.delivery.enqueue(claimed["thread_id"], claimed["delivery_id"], text)
            except NativeUnsupported:
                self.store.finish_delivery(operation_id, "unsupported")
            except NativeOutcomeUnknown:
                self.store.finish_delivery(operation_id, "delivery_outcome_unknown")
                self._unknown_reconciled.add(operation_id)
            else:
                self.store.finish_delivery(operation_id, "queued", queue_id)

    async def _work(self) -> None:
        cycle = 0
        while True:
            self.wake.clear()
            try:
                if cycle % 5 == 0:
                    await self.recover()
                await self.dispatch_pending()
                await self.flush_receipts()
                self.last_transport_error = None
            except (httpx.HTTPError, ValueError):
                self.last_transport_error = "broker_unavailable_or_invalid_response"
            await self.deliver_ready()
            if cycle % 60 == 0:
                self.store.prune()
            cycle += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), 1)

    async def _events(self) -> None:
        while True:
            try:
                async with self.broker.stream("GET", "/v1/events", params={"after": self.store.cursor}) as response:
                    if response.status_code == 410:
                        await response.aread()
                        details = response.json().get("error", {}).get("details", {})
                        resume_after = details.get("resume_after")
                        # Capture the broker watermark before GET recovery; later
                        # events will replay, including changes during recovery.
                        await self.recover()
                        if type(resume_after) is int and resume_after >= 0:
                            self.store.advance(resume_after)
                    else:
                        response.raise_for_status()
                        event_id: int | None = None
                        event_type = "message"
                        data: list[str] = []
                        size = 0
                        async for line in response.aiter_lines():
                            size += len(line)
                            if size > 2 * 1024 * 1024:
                                raise ValueError("SSE event exceeds client bound")
                            if not line:
                                if data and event_type == "operation":
                                    if event_id is None:
                                        raise ValueError("SSE operation requires a durable cursor")
                                    if event_id > self.store.cursor:
                                        self.ingest(json.loads("\n".join(data)), event_id)
                                event_id, event_type, data, size = None, "message", [], 0
                            elif line.startswith("id:"):
                                event_id = int(line[3:].strip())
                            elif line.startswith("event:"):
                                event_type = line[6:].strip()
                            elif line.startswith("data:"):
                                data.append(line[5:].lstrip(" "))
            except (httpx.HTTPError, ValueError):
                self.last_transport_error = "events_disconnected_get_recovery_available"
            await asyncio.sleep(1)

    async def submit(
        self, origin: NativeOrigin, prompt: str, artifact_ids: list[str], wait: float | None
    ) -> dict[str, Any]:
        seconds = self.settings.inline_wait_seconds if wait is None else wait
        self._validate_wait(seconds, field="wait_seconds")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 262144:
            raise ClientValidationError("invalid_prompt", "prompt must contain 1..262144 UTF-8 bytes.", "prompt")
        if not isinstance(artifact_ids, list) or len(artifact_ids) > 16:
            raise ClientValidationError(
                "invalid_artifact_ids", "artifact_ids must be a list of at most 16 UUIDs.", "artifact_ids"
            )
        try:
            for artifact_id in artifact_ids:
                native_uuid(artifact_id, "artifact_id")
        except ValueError as exc:
            raise ClientValidationError(
                "invalid_artifact_ids", "Each artifact_id must be a UUID.", "artifact_ids"
            ) from exc
        operation_id = self.store.create(origin, prompt, artifact_ids, seconds)
        self.wake.set()
        try:
            return await self.wait(origin, operation_id, seconds, refresh=False)
        finally:
            self.store.release_inline(operation_id)
            self.wake.set()

    @staticmethod
    def _validate_wait(seconds: float, *, field: str = "timeout") -> None:
        if type(seconds) not in {int, float} or not 0 <= seconds <= 60 or not math.isfinite(seconds):
            raise ClientValidationError("invalid_wait", f"{field} must be a finite number from 0 to 60 seconds.", field)

    async def get(self, origin: NativeOrigin, operation_id: str) -> dict[str, Any]:
        native_uuid(operation_id, "operation_id")
        self.store.row(operation_id, origin)
        with contextlib.suppress(httpx.HTTPError, ValueError):
            await self.refresh(operation_id)
        return self.store.view(operation_id, origin, consume=True)

    async def wait(
        self,
        origin: NativeOrigin,
        operation_id: str,
        seconds: float,
        *,
        refresh: bool = True,
    ) -> dict[str, Any]:
        self._validate_wait(seconds)
        native_uuid(operation_id, "operation_id")
        self.store.row(operation_id, origin)
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            self.changed.clear()
            result = self.store.view(operation_id, origin)
            if result.get("result_id") or result["submission_state"] in {"rejected", "payload_key_unavailable"}:
                return self.store.view(operation_id, origin, consume=True)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self.store.view(operation_id, origin, consume=True)
            if refresh:
                # Background recovery handles network work, keeping the tool wait bounded.
                self.wake.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), min(remaining, 1))

    async def cancel(self, origin: NativeOrigin, operation_id: str) -> dict[str, Any]:
        native_uuid(operation_id, "operation_id")
        row = self.store.row(operation_id, origin)
        if row["cancel_state"] is None:
            self.store.cancel_state(operation_id, "sending")
            try:
                response = await self.broker.post(f"/v1/operations/{operation_id}/cancel")
                response.raise_for_status()
                self.ingest(response.json(), expected_id=operation_id)
            except (httpx.HTTPError, ValueError):
                self.store.cancel_state(operation_id, "outcome_unknown")
            else:
                self.store.cancel_state(operation_id, "requested")
        return {**self.store.view(operation_id, origin), "cancellation_note": "Best effort; computation may continue."}

    async def upload(self, data: bytes, content_type: str) -> dict[str, Any]:
        if not 0 < len(data) <= self.settings.max_upload_bytes:
            raise ValueError("artifact size is outside configured client bounds")
        if not content_type or len(content_type) > 128 or any(c in content_type for c in "\r\n"):
            raise ValueError("invalid artifact content type")
        digest = hashlib.sha256(data).hexdigest()
        # Explicit, one-shot action. An uncertain ACK is never automatically replayed.
        try:
            response = await self.broker.post(
                "/v1/artifacts",
                content=data,
                headers={
                    "Content-Type": content_type,
                    "X-Content-SHA256": digest,
                },
            )
            response.raise_for_status()
            result = response.json()
            native_uuid(result.get("artifact_id"), "artifact_id")
            if result.get("sha256") != digest or result.get("size") != len(data):
                raise ValueError("artifact response identity disagrees")
            return dict(result)
        except (httpx.HTTPError, ValueError):
            return {"error": {"code": "upload_outcome_unknown", "message": "Upload ACK unavailable; not replayed."}}

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "native_delivery": self.native_status,
            "event_cursor": self.store.cursor,
            "transport_error": self.last_transport_error,
            "explicit_pull_available": True,
            "connection": self.transport.health() if self.transport else {"mode": "injected_test_transport"},
        }

    async def diagnostics(self) -> dict[str, Any]:
        # Only GETs, through the daemon's existing connection. Doctor never owns
        # another SSH process, creates an operation, or invokes a model.
        if self.tunnel is None or self.tunnel.available:
            try:
                response = await self.broker.get("/healthz", timeout=3)
                if response.status_code == 200 and response.json().get("ok") is True:
                    await self.broker.get("/v1/operations/00000000-0000-0000-0000-000000000000", timeout=3)
            except (httpx.HTTPError, ValueError):
                pass
        return self.health()


def create_client_app(service: ClientService, *, manage_lifecycle: bool = True) -> Starlette:
    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if manage_lifecycle:
            await service.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                await service.close()

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(service.health())

    async def diagnostics(request: Request) -> JSONResponse:
        return JSONResponse(await service.diagnostics())

    async def command(request: Request) -> JSONResponse:
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > service.settings.max_upload_bytes * 4 // 3 + 512 * 1024:
                    return JSONResponse({"error": {"code": "request_too_large"}}, status_code=413)
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ClientValidationError("invalid_request", "Request must be a JSON object.", "request")
            origin = NativeOrigin.from_meta(payload.get("meta"))
            args = payload.get("arguments", {})
            action = payload.get("action")
            if not isinstance(args, dict):
                raise ClientValidationError("invalid_arguments", "arguments must be a JSON object.", "arguments")
            if action == "submit":
                result = await service.submit(
                    origin, args.get("prompt", ""), args.get("artifact_ids", []), args.get("wait_seconds")
                )
            elif action == "resolve_call":
                result = service.store.resolve_call(origin)
            elif action == "get":
                result = await service.get(origin, args["operation_id"])
            elif action == "wait":
                result = await service.wait(origin, args["operation_id"], args.get("timeout", 30))
            elif action == "cancel":
                result = await service.cancel(origin, args["operation_id"])
            elif action == "upload":
                result = await service.upload(base64.b64decode(args["data"], validate=True), args["content_type"])
            else:
                raise ValueError("unknown client action")
            return JSONResponse(result)
        except ClientValidationError as exc:
            return JSONResponse(exc.payload, status_code=400)
        except KeyError:
            return JSONResponse({"error": {"code": "not_found_or_missing_argument"}}, status_code=404)
        except (ValueError, TypeError, AttributeError):
            return JSONResponse({"error": {"code": "invalid_client_request"}}, status_code=400)
        except Exception:
            # Never serialize exception values: HTTP errors can embed bearer-bearing requests.
            return JSONResponse({"error": {"code": "client_internal"}}, status_code=500)

    return Starlette(
        routes=[
            Route("/healthz", health),
            Route("/diagnostics", diagnostics),
            Route("/command", command, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def client_socket_path() -> Path:
    return (
        Path(os.environ.get(PREFIX + "STATE_DIR", str(Path.home() / ".local/state/hermes-a2a-gateway/client")))
        / "client.sock"
    )


async def _doctor() -> dict[str, Any]:
    try:
        settings = ClientSettings.from_env()
    except (ValueError, OSError):
        return {
            "ok": False,
            "error": "invalid_client_configuration",
            "hint": "Set BROKER_URL, TOKEN, PAYLOAD_KEY; check CA_FILE.",
        }
    report: dict[str, Any] = {"ok": True, "tls_verification": True, "explicit_pull_available": True}
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(settings.socket_path)),
            base_url="http://client",
            timeout=12,
        ) as client:
            response = await client.get("/diagnostics")
            response.raise_for_status()
            report["daemon"] = response.json()
            connection = report["daemon"].get("connection", {})
            report.update(connection)
            report["ok"] = connection.get("broker", {}).get("state") == "authenticated"
    except (httpx.HTTPError, ValueError):
        report.update(
            ok=False, daemon={"error": "unavailable", "hint": "Start the client daemon with the same STATE_DIR."}
        )
    if settings.native_delivery:
        try:
            report["native_delivery"] = await NativeQueueDelivery(settings.codex_command).capability()
        except NativeUnsupported:
            report["native_delivery"] = {
                "supported": False,
                "hint": "Use a verified Codex CLI or gateway_get/gateway_wait.",
            }
    else:
        report["native_delivery"] = {"supported": False, "reason": "disabled"}
    return report


def client_doctor() -> dict[str, Any]:
    """Read-only diagnostics; never creates a native task or queues a message."""
    return asyncio.run(_doctor())


def run_client() -> None:
    settings = ClientSettings.from_env()
    service = ClientService(settings)
    path = settings.socket_path
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("refusing to replace a non-socket client path")
            path.unlink()  # Exclusive SQLite lock already proves no other client writer.
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(128)
        config = uvicorn.Config(create_client_app(service), access_log=False, log_level="warning")
        server = uvicorn.Server(config)
        server.run(sockets=[listener])
    finally:
        listener.close()
        if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()
        # Starlette normally closes this; initialization failure still releases lock.
        with contextlib.suppress(Exception):
            service.store.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["doctor"]:
        print(json.dumps(client_doctor()))
    elif not sys.argv[1:]:
        run_client()
    else:
        raise SystemExit("usage: python -m hermes_a2a_gateway.client [doctor]")
