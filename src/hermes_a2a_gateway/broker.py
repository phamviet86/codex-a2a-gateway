"""Authenticated HTTP/SSE broker. Run with ``python -m hermes_a2a_gateway.broker``."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager, suppress
from typing import Any, Literal, Protocol

import psycopg
import uvicorn
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .broker_peer import HermesBrokerPeer
from .broker_settings import BrokerSettings
from .broker_store import FINAL, BrokerError, BrokerStore, canonical
from .models import A2ATaskResult

logger = logging.getLogger(__name__)


class Peer(Protocol):
    def submit(self, claim: dict[str, Any], timeout: float) -> AsyncGenerator[A2ATaskResult, None]: ...
    async def get(self, handle: str) -> A2ATaskResult: ...
    async def cancel(self, handle: str) -> A2ATaskResult: ...
    async def close(self) -> None: ...


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    operation_id: uuid.UUID
    flow_id: uuid.UUID
    prompt: str = Field(min_length=1, max_length=1_000_000)
    artifact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=16)


class ReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    operation_id: uuid.UUID
    result_id: uuid.UUID
    status: Literal["stored", "delivered", "delivery_outcome_unknown"]


def error_response(error: BrokerError) -> JSONResponse:
    value: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.details is not None:
        value["details"] = error.details
    return JSONResponse({"error": value}, status_code=error.status, headers={"Cache-Control": "no-store"})


class DeviceAuthentication:
    """Pure ASGI middleware also protects streaming and unknown routes."""

    def __init__(self, app: ASGIApp, settings: BrokerSettings):
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        # Uvicorn accepts forwarded scheme only from explicitly trusted loopback
        # proxies. Direct non-loopback HTTP is rejected even with a valid token.
        if scope.get("scheme") != "https" and not self.settings.allow_loopback_http:
            await error_response(BrokerError("https_required", "HTTPS transport is required", 400))(
                scope, receive, send
            )
            return
        headers = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
        device = None
        if len(headers) == 1 and headers[0].startswith(b"Bearer "):
            token = headers[0][7:]
            for candidate, secret in self.settings.device_tokens.items():
                if hmac.compare_digest(token, secret.encode()):
                    device = candidate
        if device is None:
            await error_response(BrokerError("unauthorized", "valid device bearer token required", 401))(
                scope, receive, send
            )
            return
        scope.setdefault("state", {})["device_id"] = device
        try:
            await self.app(scope, receive, send)
        except BrokerError as exc:
            await error_response(exc)(scope, receive, send)
        except psycopg.Error:
            # Avoid driver exceptions exposing DSNs or query parameters in HTTP/logs.
            await error_response(BrokerError("ledger_unavailable", "broker ledger unavailable", 503))(
                scope, receive, send
            )


class BrokerDispatcher:
    def __init__(self, store: BrokerStore, peer: Peer, settings: BrokerSettings):
        self.store, self.peer, self.settings = store, peer, settings
        self._streams: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._reads: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._cancels: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._observations = asyncio.Lock()
        self._closed = False

    async def observe(self, device: str, operation: str, task: A2ATaskResult) -> None:
        async with self._observations:
            await self._observe(device, operation, task)

    async def _observe(self, device: str, operation: str, task: A2ATaskResult) -> None:
        await asyncio.to_thread(self.store.save_handle, device, operation, task.task_id)
        if task.state not in FINAL | {"rejected", "input_required"}:
            return
        current = await asyncio.to_thread(self.store.get, device, operation)
        if current["state"] in FINAL:
            return
        if task.state in {"rejected", "input_required"}:
            await asyncio.to_thread(
                self.store.finish,
                device,
                operation,
                "failed",
                None,
                "peer_requires_input" if task.state == "input_required" else "peer_rejected",
                "peer could not complete the operation",
            )
            return
        descriptors = []
        for artifact in task.artifacts:
            for part in artifact.get("parts", []):
                if not isinstance(part, dict):
                    continue
                if "url" in part:
                    # Never follow upstream URLs or return them as downloadable files.
                    raise BrokerError("unsupported_peer_artifact", "peer URL artifacts are not supported", 502)
                if "raw" not in part:
                    continue
                raw = part["raw"]
                if not isinstance(raw, str) or len(raw) > ((self.settings.max_artifact_bytes + 2) // 3) * 4:
                    raise BrokerError("artifact_too_large", "peer artifact exceeds configured limit", 502)
                try:
                    content = base64.b64decode(raw, validate=True)
                except (ValueError, binascii.Error):
                    raise BrokerError("invalid_peer_artifact", "peer artifact has invalid encoding", 502) from None
                descriptors.append(
                    await asyncio.to_thread(
                        self.store.put_artifact,
                        device,
                        content,
                        hashlib.sha256(content).hexdigest(),
                        str(part.get("mediaType", "application/octet-stream")),
                    )
                )
        await asyncio.to_thread(
            self.store.finish, device, operation, task.state, {"text": task.text, "artifacts": descriptors}
        )

    async def _unknown(self, device: str, operation: str) -> None:
        await asyncio.to_thread(
            self.store.finish,
            device,
            operation,
            "outcome_unknown",
            None,
            "submission_unknown",
            "submission may have started; no automatic resend",
        )

    async def _submit(self, claim: dict[str, Any]) -> None:
        device, operation = claim["device_id"], claim["operation_id"]
        terminal_seen = False
        try:
            # A stream disconnect may stop Hermes. Drain through a definitive
            # terminal event, saving each handle immediately; never detach at ACK.
            async with (
                asyncio.timeout(self.settings.peer_timeout_seconds),
                aclosing(self.peer.submit(claim, self.settings.peer_timeout_seconds)) as stream,
            ):
                async for result in stream:
                    if not result.task_id:
                        continue
                    terminal_seen = result.state in FINAL | {"rejected", "input_required"}
                    await self.observe(device, operation, result)
                    if terminal_seen:
                        return
            # EOF is not completion, including after receiving a valid handle.
            await self._unknown(device, operation)
        except asyncio.CancelledError:
            await self._unknown(device, operation)
            raise
        except psycopg.Error:
            raise
        except BrokerError as exc:
            if terminal_seen and exc.code in {
                "artifact_too_large",
                "artifact_quota",
                "unsupported_peer_artifact",
                "invalid_peer_artifact",
                "result_too_large",
            }:
                await asyncio.to_thread(self.store.finish, device, operation, "failed", None, exc.code, exc.message)
            else:
                await self._unknown(device, operation)
        except Exception:
            await self._unknown(device, operation)

    async def _cancel(self, device: str, operation: str, handle: str) -> None:
        try:
            async with asyncio.timeout(10):
                result = await self.peer.cancel(handle)
            if result.task_id == handle:
                await self.observe(device, operation, result)
        except psycopg.Error:
            raise
        except Exception:
            # Intent was claimed durably. Never repeat an ambiguous cancellation.
            logger.info("peer cancellation unconfirmed; read-only reconciliation remains available")

    async def _reconcile(self, device: str, operation: str, handle: str) -> None:
        try:
            async with asyncio.timeout(10):
                result = await self.peer.get(handle)
            if result.task_id == handle:
                await self.observe(device, operation, result)
        except BrokerError as exc:
            if exc.code in {
                "artifact_too_large",
                "artifact_quota",
                "unsupported_peer_artifact",
                "invalid_peer_artifact",
            }:
                await asyncio.to_thread(self.store.finish, device, operation, "failed", None, exc.code, exc.message)
        except psycopg.Error:
            raise
        except Exception:
            # Read-only failures can be retried; the mutation is never replayed.
            return

    async def step(self) -> None:
        if self._closed:
            raise RuntimeError("dispatcher is closed")
        for tasks in (self._streams, self._reads, self._cancels):
            for key, job in list(tasks.items()):
                if job.done():
                    del tasks[key]
                    job.result()  # Database failure stops dispatch, never reconnects/replays.
        await asyncio.to_thread(self.store.purge)
        limit = self.settings.max_concurrent_streams
        while len(self._cancels) < limit:
            cancellation = await asyncio.to_thread(self.store.claim_cancel)
            if cancellation is None:
                break
            device, operation, handle = cancellation
            self._cancels[device, operation] = asyncio.create_task(
                self._cancel(device, operation, handle),
                name="broker-cancel",
            )
        if len(self._reads) < limit:
            for saved in await asyncio.to_thread(self.store.reconcilable):
                key = saved["device_id"], str(saved["operation_id"])
                # Let active streams supply complete artifacts and terminal status.
                if key in self._streams or key in self._reads or key in self._cancels:
                    continue
                self._reads[key] = asyncio.create_task(
                    self._reconcile(*key, saved["remote_task_id"]),
                    name="broker-reconcile",
                )
                if len(self._reads) >= limit:
                    break
        while len(self._streams) < limit:
            claim = await asyncio.to_thread(self.store.claim)
            if claim is None:
                break
            key = claim["device_id"], claim["operation_id"]
            self._streams[key] = asyncio.create_task(self._submit(claim), name="broker-submit")

    async def aclose(self) -> None:
        self._closed = True
        keys = list(self._streams)
        jobs = [job for group in (self._streams, self._reads, self._cancels) for job in group.values()]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        # A claimed coroutine can be canceled before its body ever starts.
        for device, operation in keys:
            snapshot = await asyncio.to_thread(self.store.get, device, operation)
            if snapshot["state"] == "running":
                await self._unknown(device, operation)
        self._streams.clear()
        self._reads.clear()
        self._cancels.clear()

    async def run(self) -> None:
        await asyncio.to_thread(self.store.acquire_dispatcher)
        await asyncio.to_thread(self.store.recover)
        try:
            while True:
                await self.step()
                await asyncio.sleep(self.settings.poll_seconds)
        finally:
            await self.aclose()


async def bounded_body(request: Request, limit: int) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise BrokerError("request_too_large", "request exceeds configured size limit", 413)
        data.extend(chunk)
    return bytes(data)


def operation_id(request: Request) -> str:
    try:
        return str(uuid.UUID(request.path_params["operation_id"]))
    except (ValueError, AttributeError):
        raise BrokerError("invalid_id", "operation ID must be a UUID") from None


def create_broker_app(
    settings: BrokerSettings,
    *,
    store: BrokerStore | None = None,
    peer: Peer | None = None,
    dispatch: bool = True,
) -> Starlette:
    """Construct the API; ``dispatch=False`` is for explicitly isolated API tests."""
    ledger = store
    peer_adapter = peer
    worker: asyncio.Task[None] | None = None

    def db() -> BrokerStore:
        if ledger is None:
            raise BrokerError("ledger_unavailable", "broker ledger is not initialized", 503)
        if worker is not None and worker.done():
            raise BrokerError("dispatcher_stopped", "broker dispatcher stopped; restart is required", 503)
        return ledger

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        nonlocal ledger, peer_adapter, worker
        if ledger is None:
            try:
                ledger = await asyncio.to_thread(BrokerStore, settings)
            except psycopg.Error:
                raise RuntimeError("broker ledger initialization failed") from None
        active_ledger = ledger
        peer_adapter = peer_adapter or HermesBrokerPeer(
            max_snapshot_bytes=settings.max_result_bytes + ((settings.max_artifact_bytes + 2) // 3) * 4
        )
        if dispatch:
            dispatcher = BrokerDispatcher(active_ledger, peer_adapter, settings)
            # Lock and recover before accepting requests, not asynchronously later.
            await asyncio.to_thread(active_ledger.acquire_dispatcher)
            await asyncio.to_thread(active_ledger.recover)

            async def loop() -> None:
                try:
                    while True:
                        await dispatcher.step()
                        await asyncio.sleep(settings.poll_seconds)
                finally:
                    await dispatcher.aclose()

            worker = asyncio.create_task(loop(), name="broker-dispatcher")
        try:
            yield
        finally:
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await worker
            await peer_adapter.close()
            await asyncio.to_thread(active_ledger.close)

    async def health(request: Request) -> Response:
        try:
            db()
        except BrokerError as exc:
            return error_response(exc)
        return JSONResponse({"ok": True})

    async def accept(request: Request) -> Response:
        raw = await bounded_body(request, settings.max_request_bytes)
        try:
            command = OperationRequest.model_validate_json(raw)
        except ValidationError:
            raise BrokerError("invalid_request", "invalid operation request") from None
        snapshot, created = await asyncio.to_thread(
            db().accept, request.state.device_id, command.model_dump(mode="json")
        )
        return JSONResponse(snapshot, status_code=202 if created else 200, headers={"Cache-Control": "no-store"})

    async def get(request: Request) -> Response:
        return JSONResponse(
            await asyncio.to_thread(db().get, request.state.device_id, operation_id(request)),
            headers={"Cache-Control": "no-store"},
        )

    async def cancel(request: Request) -> Response:
        snapshot, _ = await asyncio.to_thread(db().cancel_intent, request.state.device_id, operation_id(request))
        return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})

    async def receipt(request: Request) -> Response:
        try:
            command = ReceiptRequest.model_validate_json(await bounded_body(request, 4096))
        except ValidationError:
            raise BrokerError("invalid_request", "invalid receipt request") from None
        await asyncio.to_thread(
            db().receipt, request.state.device_id, str(command.operation_id), str(command.result_id), command.status
        )
        return JSONResponse({"ok": True})

    async def events(request: Request) -> Response:
        try:
            after = int(request.query_params.get("after", request.headers.get("Last-Event-ID", "0")))
            if after < 0 or after > 2**63 - 1:
                raise ValueError
        except ValueError:
            raise BrokerError("invalid_cursor", "cursor must be a nonnegative integer") from None
        initial = await asyncio.to_thread(db().events, request.state.device_id, after)

        async def stream() -> AsyncIterator[bytes]:
            cursor, batch = after, initial
            while True:
                for sequence, snapshot in batch:
                    yield (
                        b"id: " + str(sequence).encode() + b"\nevent: operation\ndata: " + canonical(snapshot) + b"\n\n"
                    )
                    cursor = sequence
                if await request.is_disconnected():
                    return
                await asyncio.sleep(settings.poll_seconds)
                try:
                    batch = await asyncio.to_thread(db().events, request.state.device_id, cursor)
                except BrokerError as exc:
                    # A gap after headers cannot become an HTTP 410; send an explicit
                    # error and close. Reconnect then returns the normal HTTP error.
                    yield (
                        b"event: error\ndata: "
                        + canonical(
                            {
                                "error": {
                                    "code": exc.code,
                                    "message": exc.message,
                                    "details": exc.details,
                                }
                            }
                        )
                        + b"\n\n"
                    )
                    return
                except psycopg.Error:
                    return
                if not batch:
                    yield b": keepalive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    async def upload(request: Request) -> Response:
        content = await bounded_body(request, settings.max_artifact_bytes)
        descriptor = await asyncio.to_thread(
            db().put_artifact,
            request.state.device_id,
            content,
            request.headers.get("X-Content-SHA256", ""),
            request.headers.get("Content-Type", "application/octet-stream"),
        )
        return JSONResponse(descriptor, status_code=201, headers={"Cache-Control": "no-store"})

    async def download(request: Request) -> Response:
        try:
            artifact = str(uuid.UUID(request.path_params["artifact_id"]))
        except ValueError:
            raise BrokerError("invalid_id", "artifact ID must be a UUID") from None
        descriptor, content = await asyncio.to_thread(db().artifact, request.state.device_id, artifact)
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{artifact}"',
                "X-Content-SHA256": descriptor["sha256"],
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "no-store",
            },
        )

    app = Starlette(
        routes=[
            Route("/healthz", health),
            Route("/v1/operations", accept, methods=["POST"]),
            Route("/v1/operations/{operation_id}", get),
            Route("/v1/operations/{operation_id}/cancel", cancel, methods=["POST"]),
            Route("/v1/events", events),
            Route("/v1/receipts", receipt, methods=["POST"]),
            Route("/v1/artifacts", upload, methods=["POST"]),
            Route("/v1/artifacts/{artifact_id}", download),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(DeviceAuthentication, settings=settings)
    return app


def run_broker() -> None:
    try:
        settings = BrokerSettings.from_env()
    except (ValidationError, ValueError):
        raise SystemExit(
            "Invalid broker configuration; check HERMES_A2A_GATEWAY_BROKER_* environment variables"
        ) from None
    uvicorn.run(
        create_broker_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
        # Persistent event subscribers must not indefinitely delay lifespan cleanup.
        timeout_graceful_shutdown=5,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1,::1",
    )


if __name__ == "__main__":
    run_broker()
