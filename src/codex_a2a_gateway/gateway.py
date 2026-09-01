from __future__ import annotations

import hmac
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__
from .inbound import InboundService
from .models import BridgeError
from .settings import Settings

METHOD_ALIASES = {
    "message/send": "SendMessage",
    "message/stream": "SendStreamingMessage",
    "tasks/get": "GetTask",
    "tasks/cancel": "CancelTask",
    "tasks/list": "ListTasks",
}
PART_CONTENT_MEMBERS = {"text", "raw", "url", "data", "file"}
EXECUTION_PREFERENCES_EXTENSION_URI = (
    "https://github.com/phamviet86/codex-a2a-gateway/blob/main/docs/execution-preferences-extension-v1.md"
)


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str, data: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _message_input(params: dict[str, Any]) -> tuple[str, str | None, str, str | None]:
    message = params.get("message")
    if not isinstance(message, dict):
        raise BridgeError("invalid_params", "params.message must be an object")
    message_id = message.get("messageId")
    if not isinstance(message_id, str):
        raise BridgeError("invalid_params", "message.messageId must be a string")
    role = message.get("role")
    if role not in {"ROLE_USER", "user"}:
        raise BridgeError("invalid_params", "only user-role messages are accepted")
    parts = message.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 32:
        raise BridgeError("invalid_params", "message.parts must contain 1 to 32 parts")
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            raise BridgeError("invalid_params", "every message part must be an object")
        content_members = PART_CONTENT_MEMBERS.intersection(part)
        if content_members != {"text"}:
            if content_members:
                raise BridgeError("unsupported_content", "every part must contain text content only")
            raise BridgeError("invalid_params", "every message part must contain exactly one content member")
        media_type = part.get("mediaType")
        if media_type not in (None, "", "text/plain"):
            raise BridgeError("unsupported_content", f"unsupported text mediaType: {media_type}")
        text = part["text"]
        if not isinstance(text, str):
            raise BridgeError("invalid_params", "part.text must be a string")
        text_parts.append(text)
    context_id = message.get("contextId") or params.get("contextId")
    if context_id is not None and not isinstance(context_id, str):
        raise BridgeError("invalid_params", "contextId must be a string")
    task_id = message.get("taskId")
    if task_id is not None and not isinstance(task_id, str):
        raise BridgeError("invalid_params", "message.taskId must be a string")
    return "\n".join(text_parts), context_id, message_id, task_id


def _execution_preferences(params: dict[str, Any], settings: Settings, extensions_header: str) -> dict[str, Any]:
    """Parse the negotiated A2A extension; App Server validates its catalog."""
    message = params.get("message")
    if not isinstance(message, dict):
        return {}
    message_extensions = message.get("extensions")
    metadata = message.get("metadata")
    has_extension_data = isinstance(metadata, dict) and "executionPreferences" in metadata
    header_uris = {item.strip() for item in extensions_header.split(",") if item.strip()}
    message_advertises_ours = isinstance(message_extensions, list) and (
        EXECUTION_PREFERENCES_EXTENSION_URI in message_extensions
    )
    if (
        not has_extension_data
        and EXECUTION_PREFERENCES_EXTENSION_URI not in header_uris
        and not message_advertises_ours
    ):
        # Other A2A extensions are owned by their respective peers.  This
        # gateway ignores them unless they attempt to invoke ours.
        return {}
    if not isinstance(message_extensions, list) or not all(isinstance(item, str) for item in message_extensions):
        raise BridgeError("invalid_extension", "message.extensions must be a list of extension URIs")
    if EXECUTION_PREFERENCES_EXTENSION_URI not in header_uris:
        raise BridgeError("invalid_extension", "A2A-Extensions must advertise the execution-preferences URI")
    if EXECUTION_PREFERENCES_EXTENSION_URI not in message_extensions:
        raise BridgeError("invalid_extension", "message.extensions must advertise the execution-preferences URI")
    if not isinstance(metadata, dict):
        raise BridgeError("invalid_extension", "message.metadata must be an object")
    extension = metadata.get("executionPreferences")
    if extension is None:
        raise BridgeError("invalid_extension", "message.metadata.executionPreferences is required")
    if settings.backend != "app-server":
        raise BridgeError(
            "execution_preferences_unsupported",
            "Model and reasoning preferences require the App Server backend.",
        )
    if not isinstance(extension, dict):
        raise BridgeError("invalid_extension", "execution-preferences extension must be an object")
    model = extension.get("model")
    effort = extension.get("reasoning_effort", extension.get("reasoningEffort"))
    require_exact = extension.get("require_exact", extension.get("requireExact", False))
    if model is not None and (not isinstance(model, str) or not 1 <= len(model.strip()) <= 200):
        raise BridgeError("invalid_extension", "extension model must be a non-empty string up to 200 characters")
    if effort is not None and (not isinstance(effort, str) or not 1 <= len(effort.strip()) <= 64):
        raise BridgeError(
            "invalid_extension", "extension reasoning_effort must be a non-empty string up to 64 characters"
        )
    if not isinstance(require_exact, bool):
        raise BridgeError("invalid_extension", "extension require_exact must be a boolean")
    if model is None and effort is None:
        raise BridgeError("invalid_extension", "execution-preferences extension requires model or reasoning_effort")
    return {
        "requested": {
            "model": model.strip() if isinstance(model, str) else None,
            "reasoningEffort": effort.strip() if isinstance(effort, str) else None,
            "requireExact": require_exact,
        }
    }


def create_gateway_app(settings: Settings, *, service: InboundService | None = None) -> Starlette:
    inbound = service or InboundService(settings)

    async def card(_request: Request) -> JSONResponse:
        security_schemes: dict[str, Any] = {}
        security_requirements: list[dict[str, Any]] = []
        if settings.inbound_token:
            security_schemes["bearerAuth"] = {
                "httpAuthSecurityScheme": {
                    "scheme": "Bearer",
                    "description": "Bearer token configured for this local gateway",
                }
            }
            security_requirements.append({"schemes": {"bearerAuth": {"list": []}}})
        payload: dict[str, Any] = {
            "name": settings.agent_name,
            "description": (
                "Generic local A2A gateway backed by Codex. Supports text conversations, streaming, task lookup, "
                "and best-effort cancellation; push notifications are not implemented."
            ),
            "version": __version__,
            "supportedInterfaces": [
                {
                    "url": settings.advertised_url + "/",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                }
            ],
            "capabilities": {"streaming": True, "pushNotifications": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {
                    "id": "codex-conversation",
                    "name": "Codex conversation",
                    "description": "Send a text task to Codex and continue it with the returned contextId.",
                    "tags": ["coding", "analysis", "conversation"],
                    "inputModes": ["text/plain"],
                    "outputModes": ["text/plain"],
                }
            ],
        }
        if settings.backend == "app-server":
            payload["capabilities"]["extensions"] = [
                {
                    "uri": EXECUTION_PREFERENCES_EXTENSION_URI,
                    "description": "Receiver-controlled model and reasoning preferences for inbound A2A tasks.",
                    "required": False,
                }
            ]
        if security_schemes:
            payload["securitySchemes"] = security_schemes
            payload["securityRequirements"] = security_requirements
        return JSONResponse(payload)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "service": "codex-a2a-gateway",
                "capabilities": inbound.backend_capabilities,
                "state": inbound.store.counts(),
            }
        )

    def authorized(request: Request) -> bool:
        if not settings.inbound_token:
            return True
        value = request.headers.get("authorization", "")
        scheme, separator, token = value.partition(" ")
        return bool(separator) and scheme.lower() == "bearer" and hmac.compare_digest(token, settings.inbound_token)

    def ingress_allowed(request: Request) -> bool:
        host = request.headers.get("host", "").lower()
        advertised = urlparse(settings.advertised_url)
        allowed_hosts = {
            f"{settings.inbound_host.lower()}:{settings.inbound_port}",
            f"127.0.0.1:{settings.inbound_port}",
            f"localhost:{settings.inbound_port}",
            f"[::1]:{settings.inbound_port}",
        }
        if advertised.netloc:
            allowed_hosts.add(advertised.netloc.lower())
        if host not in allowed_hosts:
            return False
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site and fetch_site.lower() != "same-origin":
            return False
        origin = request.headers.get("origin")
        if not origin:
            return True
        if not settings.inbound_token:
            return False
        parsed_origin = urlparse(origin)
        allowed_origins = {
            f"{request.url.scheme}://{host}",
            settings.advertised_url,
        }
        return origin.rstrip("/") in {value.rstrip("/") for value in allowed_origins} and bool(
            parsed_origin.scheme and parsed_origin.netloc
        )

    def sse(envelope: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(envelope, separators=(',', ':'))}\n\n".encode()

    async def stream_frames(
        request_id: Any,
        task_record: Any,
        subscriber: Any,
        *,
        legacy: bool,
    ) -> AsyncIterator[bytes]:
        previous_text = ""
        previous_state = ""
        async for updated in inbound.updates(task_record, subscriber):
            task = inbound.task_payload(updated)
            if legacy:
                yield sse(_rpc_result(request_id, {"task": task}))
                continue
            if not previous_state:
                yield sse(_rpc_result(request_id, {"task": task}))
            else:
                current_text = updated.result_text
                if current_text != previous_text:
                    is_append = current_text.startswith(previous_text)
                    text = current_text[len(previous_text) :] if is_append else current_text
                    artifact = {
                        "artifactId": f"artifact-{updated.bridge_task_id}",
                        "parts": [{"text": text, "mediaType": "text/plain"}],
                    }
                    if not previous_text:
                        artifact["name"] = "Codex response"
                    event = {
                        "taskId": task["id"],
                        "contextId": task["contextId"],
                        "artifact": artifact,
                        "append": bool(previous_text and is_append),
                    }
                    yield sse(_rpc_result(request_id, {"artifactUpdate": event}))
                state = task["status"]["state"]
                if state != previous_state:
                    event = {
                        "taskId": task["id"],
                        "contextId": task["contextId"],
                        "status": task["status"],
                    }
                    yield sse(_rpc_result(request_id, {"statusUpdate": event}))
            previous_text = updated.result_text
            previous_state = task["status"]["state"]

    async def rpc(request: Request) -> Response:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return JSONResponse(_rpc_error(None, -32600, "Content-Type must be application/json"), status_code=415)
        if not ingress_allowed(request):
            return JSONResponse(_rpc_error(None, -32000, "Forbidden request origin or host"), status_code=403)
        if not authorized(request):
            return JSONResponse(_rpc_error(None, -32000, "Unauthorized"), status_code=401)
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > settings.max_request_bytes:
            return JSONResponse(_rpc_error(None, -32600, "Request too large"), status_code=413)
        body_buffer = bytearray()
        async for chunk in request.stream():
            if len(body_buffer) + len(chunk) > settings.max_request_bytes:
                return JSONResponse(_rpc_error(None, -32600, "Request too large"), status_code=413)
            body_buffer.extend(chunk)
        body = bytes(body_buffer)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, RecursionError):
            return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse(_rpc_error(None, -32600, "Invalid Request"), status_code=400)
        request_id = payload.get("id")
        raw_method = str(payload.get("method") or "")
        method = METHOD_ALIASES.get(raw_method, raw_method)
        legacy = raw_method in METHOD_ALIASES
        params = payload.get("params") or {}
        if payload.get("jsonrpc") != "2.0" or not isinstance(params, dict):
            return JSONResponse(_rpc_error(request_id, -32600, "Invalid Request"), status_code=400)
        try:
            if method in {"SendMessage", "SendStreamingMessage"}:
                text, context_id, message_id, task_id = _message_input(params)
                execution_preferences = _execution_preferences(
                    params,
                    settings,
                    request.headers.get("a2a-extensions", ""),
                )
                subscriber = inbound.create_subscription() if method == "SendStreamingMessage" else None
                task, _deduplicated = await inbound.submit(
                    text,
                    context_id=context_id,
                    message_id=message_id,
                    task_id=task_id,
                    execution_preferences=execution_preferences,
                    subscriber=subscriber,
                )
                if method == "SendStreamingMessage":
                    return StreamingResponse(
                        stream_frames(request_id, task, subscriber, legacy=legacy),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )
                configuration = params.get("configuration") or {}
                if not isinstance(configuration, dict):
                    raise BridgeError("invalid_configuration", "configuration must be an object")
                return_immediately = configuration.get("returnImmediately", False)
                if not isinstance(return_immediately, bool):
                    raise BridgeError("invalid_configuration", "returnImmediately must be a boolean")
                completed = task if return_immediately else await inbound.wait(task.bridge_task_id)
                task_result = inbound.task_payload(completed)
                return JSONResponse(_rpc_result(request_id, task_result if legacy else {"task": task_result}))
            if method == "GetTask":
                task_id = params.get("id") or params.get("taskId")
                if not isinstance(task_id, str):
                    raise BridgeError("invalid_params", "task id is required")
                history_length = params.get("historyLength")
                if history_length is not None and (not isinstance(history_length, int) or history_length < 0):
                    raise BridgeError("invalid_history_length", "historyLength must be zero or greater")
                return JSONResponse(
                    _rpc_result(
                        request_id,
                        inbound.task_payload(inbound.get_task(task_id), history_length=history_length),
                    )
                )
            if method == "CancelTask":
                task_id = params.get("id") or params.get("taskId")
                if not isinstance(task_id, str):
                    raise BridgeError("invalid_params", "task id is required")
                task, sent = await inbound.cancel(task_id)
                result = inbound.task_payload(task)
                result.setdefault("metadata", {})["cancelSent"] = sent
                result["metadata"]["computationStopped"] = "unknown"
                return JSONResponse(_rpc_result(request_id, result))
            if method == "ListTasks":
                context_id = params.get("contextId")
                if context_id is not None and not isinstance(context_id, str):
                    raise BridgeError("invalid_params", "contextId must be a string")
                page_size = params.get("pageSize", 50)
                if not isinstance(page_size, int) or not 1 <= page_size <= 100:
                    raise BridgeError("invalid_page_size", "pageSize must be between 1 and 100")
                status = params.get("status")
                page_token = params.get("pageToken", "")
                history_length = params.get("historyLength")
                include_artifacts = params.get("includeArtifacts", False)
                updated_after = params.get("statusTimestampAfter")
                if status is not None and not isinstance(status, str):
                    raise BridgeError("invalid_status", "status must be a string")
                if not isinstance(page_token, str):
                    raise BridgeError("invalid_page_token", "pageToken must be a string")
                if history_length is not None and not isinstance(history_length, int):
                    raise BridgeError("invalid_history_length", "historyLength must be an integer")
                if not isinstance(include_artifacts, bool):
                    raise BridgeError("invalid_include_artifacts", "includeArtifacts must be a boolean")
                if updated_after is not None and not isinstance(updated_after, str):
                    raise BridgeError("invalid_timestamp", "statusTimestampAfter must be a string")
                result = inbound.list_tasks(
                    context_id=context_id,
                    status=status,
                    page_size=page_size,
                    page_token=page_token,
                    history_length=history_length,
                    include_artifacts=include_artifacts,
                    updated_after=updated_after,
                )
                return JSONResponse(_rpc_result(request_id, result))
            return JSONResponse(_rpc_error(request_id, -32601, "Method not found"))
        except BridgeError as exc:
            a2a_errors = {
                "task_not_found": (-32001, "TASK_NOT_FOUND", "a2a-protocol.org"),
                "task_not_cancelable": (-32002, "TASK_NOT_CANCELABLE", "a2a-protocol.org"),
                "unsupported_content": (-32005, "CONTENT_TYPE_NOT_SUPPORTED", "a2a-protocol.org"),
                "server_overloaded": (-32000, "SERVER_OVERLOADED", "codex-a2a-gateway"),
            }
            if exc.code in a2a_errors:
                rpc_code, reason, domain = a2a_errors[exc.code]
                metadata: dict[str, str] = {}
                task_id = params.get("id") or params.get("taskId")
                message = params.get("message")
                if not task_id and isinstance(message, dict):
                    task_id = message.get("taskId")
                if isinstance(task_id, str):
                    metadata["taskId"] = task_id
                if exc.retryable:
                    metadata["retryable"] = "true"
                details = [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": reason,
                        "domain": domain,
                        "metadata": metadata,
                    }
                ]
            else:
                invalid = exc.code.startswith("invalid_") or exc.code in {
                    "task_context_mismatch",
                    "idempotency_conflict",
                }
                rpc_code = -32602 if invalid else -32603
                details = [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": exc.code.upper(),
                        "domain": "codex-a2a-gateway",
                    }
                ]
            return JSONResponse(_rpc_error(request_id, rpc_code, exc.message, details))

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await inbound.aclose()

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", card, methods=["GET"]),
            Route("/.well-known/agent.json", card, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/", rpc, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def run_gateway(settings: Settings | None = None) -> None:
    actual = settings or Settings.from_env()
    if not Path(actual.codex_bin).is_file() and shutil.which(actual.codex_bin) is None:
        raise RuntimeError("CODEX_CLI_BIN was not found")
    uvicorn.run(
        create_gateway_app(actual),
        host=actual.inbound_host,
        port=actual.inbound_port,
        log_level="warning",
        access_log=False,
    )
