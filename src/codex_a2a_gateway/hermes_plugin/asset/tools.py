"""Reliable Hermes -> Codex A2A client tools.

The built-in Hermes ``a2a_call`` remains a synchronous convenience tool.  This
toolset always submits with ``returnImmediately`` and keeps the returned handle
in Hermes-owned ``ctx.state`` so timeout recovery never resends a request.
"""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

EXTENSION_URI = "https://github.com/phamviet86/codex-a2a-gateway/blob/main/docs/execution-preferences-extension-v1.md"
TERMINAL = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_INPUT_REQUIRED",
}


class GatewayRejection(ValueError):
    """A definite JSON-RPC rejection returned by the local gateway."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not allow a loopback request to leave its validated origin."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loopback_endpoint(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path not in {"", "/"}:
        raise ValueError("endpoint must be a loopback HTTP(S) origin without a path")
    if parsed.hostname.lower() != "localhost":
        try:
            if not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise ValueError("endpoint must be loopback-only")
        except ValueError as exc:
            raise ValueError("endpoint must be loopback-only") from exc
    return parsed.geturl()


def _endpoint(ctx: Any) -> str:
    return _loopback_endpoint(str(ctx.get_config("endpoint", "http://127.0.0.1:9910")))


def _timeout(ctx: Any) -> float:
    try:
        return max(1.0, min(float(ctx.get_config("timeout", 30)), 300.0))
    except (TypeError, ValueError):
        return 30.0


def _request(endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    if extension:
        headers["A2A-Extensions"] = EXTENSION_URI
    request = urllib.request.Request(
        endpoint + "/",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers=headers,
        method="POST",
    )
    with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:  # noqa: S310 -- endpoint is loopback-validated
        value = json.loads(response.read().decode())
    if not isinstance(value, dict):
        raise ValueError("gateway returned a non-object response")
    if "error" in value:
        raise GatewayRejection(str((value["error"] or {}).get("message") or "gateway rejected the request"))
    return value


def _advertises_execution_preferences(endpoint: str, timeout: float) -> bool:
    """Check the local receiver before sending an opt-in extension request."""
    request = urllib.request.Request(
        endpoint + "/.well-known/agent-card.json",
        headers={"Accept": "application/json", "A2A-Version": "1.0"},
        method="GET",
    )
    with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:  # noqa: S310 -- endpoint is loopback-validated
        card = json.loads(response.read().decode())
    if not isinstance(card, dict):
        return False
    capabilities = card.get("capabilities")
    extensions = capabilities.get("extensions") if isinstance(capabilities, dict) else None
    return isinstance(extensions, list) and any(
        isinstance(extension, dict) and extension.get("uri") == EXTENSION_URI for extension in extensions
    )


def _task_from_response(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result") or {}
    task = result.get("task") if isinstance(result, dict) else None
    if not isinstance(task, dict) or not isinstance(task.get("id"), str):
        raise ValueError("gateway response did not contain an A2A task handle")
    return task


MAX_HANDLES = 200
PERSISTED_HANDLE_FIELDS = {
    "handle_id",
    "remote_task_id",
    "context_id",
    "endpoint",
    "message_id",
    "rpc_id",
    "request_fingerprint",
    "preferences",
    "state",
    "failure_code",
    "attempt_number",
    "updated_at",
}


def _handles(ctx: Any) -> list[dict[str, Any]]:
    values = ctx.state.get("handles", [])
    return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []


def _handle_ids(ctx: Any) -> list[str]:
    return [str(handle["handle_id"]) for handle in _handles(ctx) if handle.get("handle_id")]


def _public_handle(handle: dict[str, Any]) -> dict[str, Any]:
    """Return durable metadata without task text, artifacts, or status payloads."""
    return {key: handle[key] for key in PERSISTED_HANDLE_FIELDS if key in handle}


def _save_handle(ctx: Any, handle: dict[str, Any]) -> dict[str, Any]:
    handle_id = str(handle["handle_id"])
    handles = [entry for entry in _handles(ctx) if str(entry.get("handle_id")) != handle_id]
    # Hermes state is durable and quota-limited. Do not persist remote task
    # snapshots/results/artifacts; only the handle required for recovery.
    handles.append(_public_handle(handle))
    handles.sort(key=lambda entry: float(entry.get("updated_at") or 0), reverse=True)
    ctx.state.set("handles", handles[:MAX_HANDLES])
    return handle


def _record(ctx: Any, task: dict[str, Any], endpoint: str, handle: dict[str, Any]) -> dict[str, Any]:
    handle["remote_task_id"] = str(task["id"])
    handle["context_id"] = str(task.get("contextId") or handle.get("context_id") or "")
    handle["endpoint"] = endpoint
    handle["state"] = str((task.get("status") or {}).get("state") or "TASK_STATE_SUBMITTED")
    handle["updated_at"] = time.time()
    return _save_handle(ctx, handle)


def _record_rejection(ctx: Any, handle: dict[str, Any], error: str) -> dict[str, Any]:
    """Persist a definite refusal so it cannot be mistaken for a timeout."""
    handle["state"] = "TASK_STATE_REJECTED"
    del error
    handle["failure_code"] = "gateway_rejected"
    handle["updated_at"] = time.time()
    return _save_handle(ctx, handle)


def _mark_unknown(ctx: Any, handle: dict[str, Any]) -> dict[str, Any]:
    handle["state"] = "outcome_unknown"
    handle["updated_at"] = time.time()
    return _save_handle(ctx, handle)


def _is_redirect(error: urllib.error.HTTPError) -> bool:
    return 300 <= error.code < 400


def _load_handle(ctx: Any, handle_id: str) -> dict[str, Any]:
    for handle in _handles(ctx):
        if handle.get("handle_id") == handle_id:
            return handle
    raise ValueError("unknown task handle; use codex_a2a_call or codex_a2a_list first")


def _get(ctx: Any, handle: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    endpoint = _loopback_endpoint(str(handle["endpoint"]))
    remote_task_id = str(handle.get("remote_task_id") or "")
    if not remote_task_id:
        recovered = _recover_unique(ctx, handle)
        if recovered is None:
            return _mark_unknown(ctx, handle), None
        return recovered
    response = _request(
        endpoint,
        {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "GetTask", "params": {"id": remote_task_id}},
        _timeout(ctx),
    )
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("id"), str):
        raise ValueError("gateway returned an invalid task")
    return _record(ctx, result, endpoint, handle), result


def _recover_unique(ctx: Any, handle: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    context_id = str(handle.get("context_id") or "")
    message_id = str(handle.get("message_id") or "")
    if not context_id or not message_id:
        return None
    endpoint = _loopback_endpoint(str(handle["endpoint"]))
    response = _request(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "ListTasks",
            "params": {"contextId": context_id, "pageSize": 100},
        },
        _timeout(ctx),
    )
    tasks = (response.get("result") or {}).get("tasks", []) if isinstance(response.get("result"), dict) else []
    # A recovery candidate may replace a stale binding on this same local
    # handle, but must never take a task already bound to another handle.
    bound = {
        str(candidate.get("remote_task_id"))
        for candidate in _handles(ctx)
        if candidate.get("handle_id") != handle.get("handle_id") and candidate.get("remote_task_id")
    }
    candidates = [
        task
        for task in tasks
        if isinstance(task, dict)
        and isinstance(task.get("id"), str)
        and str(task["id"]) not in bound
        and isinstance(task.get("metadata"), dict)
        and task["metadata"].get("requestMessageId") == message_id
    ]
    if len(candidates) != 1:
        return None
    return _record(ctx, candidates[0], endpoint, handle), candidates[0]


def _call(ctx: Any, args: dict[str, Any]) -> str:
    message = str(args.get("message") or "").strip()
    if not message:
        return _json({"ok": False, "error": "message is required"})
    continuation_id = str(args.get("task_id") or "")
    preferences = {
        key: args[key]
        for key in ("model", "reasoning_effort", "require_exact")
        if key in args and args[key] not in (None, "")
    }
    if continuation_id:
        try:
            handle = _load_handle(ctx, continuation_id)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        if preferences:
            return _json(
                {
                    "ok": False,
                    "handle": handle["handle_id"],
                    "handleInfo": _public_handle(handle),
                    "error": "execution preferences cannot change on an input-required continuation",
                }
            )
        if handle.get("state") != "TASK_STATE_INPUT_REQUIRED" or not handle.get("remote_task_id"):
            return _json(
                {
                    "ok": False,
                    "handle": handle["handle_id"],
                    "handleInfo": _public_handle(handle),
                    "error": "continuation requires an input-required handle with a remote task id",
                }
            )
        if args.get("context_id") and str(args["context_id"]) != str(handle.get("context_id") or ""):
            return _json(
                {
                    "ok": False,
                    "handle": handle["handle_id"],
                    "handleInfo": _public_handle(handle),
                    "error": "continuation context_id must match the saved handle",
                }
            )
        endpoint = _loopback_endpoint(str(handle["endpoint"]))
        context_id = str(handle.get("context_id") or "")
        message_id = str(args.get("message_id") or f"hermes-{uuid.uuid4().hex}")
        a2a_message: dict[str, Any] = {
            "messageId": message_id,
            "taskId": str(handle["remote_task_id"]),
            "role": "ROLE_USER",
            "parts": [{"text": message, "mediaType": "text/plain"}],
        }
        # Persist the new continuation attempt before any network send while
        # retaining the same local handle and remote task correlation.
        handle.update(
            {
                "message_id": message_id,
                "attempt_number": int(handle.get("attempt_number") or 0) + 1,
                "state": "TASK_STATE_SUBMITTED",
                "updated_at": time.time(),
            }
        )
        _save_handle(ctx, handle)
    else:
        endpoint = _endpoint(ctx)
        context_id = str(args.get("context_id") or uuid.uuid4().hex)
        message_id = str(args.get("message_id") or f"hermes-{uuid.uuid4().hex}")
        if preferences:
            try:
                supported = _advertises_execution_preferences(endpoint, _timeout(ctx))
            except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                return _json(
                    {"ok": False, "error": f"execution preferences unsupported: Agent Card unavailable ({exc})"}
                )
            if not supported:
                return _json({"ok": False, "error": "execution preferences unsupported by this Agent Card"})
        a2a_message = {
            "messageId": message_id,
            "role": "ROLE_USER",
            "contextId": context_id,
            "parts": [{"text": message, "mediaType": "text/plain"}],
        }
        handle = {
            "handle_id": f"local-{uuid.uuid4().hex}",
            "remote_task_id": "",
            "context_id": context_id,
            "endpoint": endpoint,
            "message_id": message_id,
            "preferences": preferences,
            "state": "TASK_STATE_SUBMITTED",
            "attempt_number": 1,
            "updated_at": time.time(),
        }
    a2a_message = {
        **a2a_message,
    }
    if preferences:
        a2a_message["extensions"] = [EXTENSION_URI]
        a2a_message["metadata"] = {"executionPreferences": preferences}
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {"message": a2a_message, "configuration": {"returnImmediately": True}},
    }
    handle.update(
        {
            "rpc_id": payload["id"],
            "request_fingerprint": uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(payload, sort_keys=True)).hex,
            "updated_at": time.time(),
        }
    )
    _save_handle(ctx, handle)
    try:
        task = _task_from_response(_request(endpoint, payload, _timeout(ctx), extension=bool(preferences)))
        handle = _record(ctx, task, endpoint, handle)
        return _json({"ok": True, "handle": handle["handle_id"], "handleInfo": _public_handle(handle), "task": task})
    except urllib.error.HTTPError as exc:
        _record_rejection(ctx, handle, str(exc))
        return _json(
            {"ok": False, "handle": handle["handle_id"], "handleInfo": _public_handle(handle), "error": str(exc)}
        )
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        # The request may have reached Codex.  The pre-recorded handle is the
        # only safe recovery target; callers must get/wait/list, never resend.
        _mark_unknown(ctx, handle)
        return _json(
            {
                "ok": False,
                "state": "outcome_unknown",
                "handle": handle["handle_id"],
                "handleInfo": _public_handle(handle),
                "error": str(exc),
            }
        )
    except GatewayRejection as exc:
        _record_rejection(ctx, handle, str(exc))
        return _json(
            {"ok": False, "handle": handle["handle_id"], "handleInfo": _public_handle(handle), "error": str(exc)}
        )
    except ValueError as exc:
        # A malformed response after SendMessage is ambiguous: the server may
        # have accepted the request before returning invalid data.
        _mark_unknown(ctx, handle)
        return _json(
            {
                "ok": False,
                "state": "outcome_unknown",
                "handle": handle["handle_id"],
                "handleInfo": _public_handle(handle),
                "error": str(exc),
            }
        )


def _get_tool(ctx: Any, args: dict[str, Any]) -> str:
    handle: dict[str, Any] | None = None
    try:
        handle = _load_handle(ctx, str(args.get("task_id") or ""))
        handle, task = _get(ctx, handle)
        return _json(
            {"ok": task is not None, "handle": handle["handle_id"], "handleInfo": _public_handle(handle), "task": task}
        )
    except urllib.error.HTTPError as exc:
        if _is_redirect(exc) and handle is not None:
            _record_rejection(ctx, handle, str(exc))
            return _json(
                {
                    "ok": False,
                    "handle": handle["handle_id"],
                    "handleInfo": _public_handle(handle),
                    "error": str(exc),
                }
            )
        if exc.code == 404:
            handle = _load_handle(ctx, str(args.get("task_id") or ""))
            recovered = _recover_unique(ctx, handle)
            if recovered is not None:
                handle, task = recovered
                return _json(
                    {
                        "ok": True,
                        "handle": handle["handle_id"],
                        "handleInfo": _public_handle(handle),
                        "task": task,
                        "recovery": "unique_list_tasks_candidate",
                    }
                )
            _mark_unknown(ctx, handle)
            return _json(
                {
                    "ok": False,
                    "state": "outcome_unknown",
                    "handle": handle["handle_id"],
                    "handleInfo": _public_handle(handle),
                    "error": "no unique ListTasks recovery candidate",
                }
            )
        return _json({"ok": False, "error": str(exc)})
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        if handle is not None:
            _mark_unknown(ctx, handle)
        return _json(
            {
                "ok": False,
                "state": "outcome_unknown",
                "handle": handle["handle_id"] if handle else None,
                "handleInfo": _public_handle(handle) if handle else None,
                "error": str(exc),
            }
        )
    except ValueError as exc:
        if handle is not None:
            _mark_unknown(ctx, handle)
        return _json(
            {
                "ok": False,
                "state": "outcome_unknown" if handle is not None else None,
                "handle": handle["handle_id"] if handle else None,
                "handleInfo": _public_handle(handle) if handle else None,
                "error": str(exc),
            }
        )


def _wait(ctx: Any, args: dict[str, Any]) -> str:
    handle: dict[str, Any] | None = None
    try:
        handle = _load_handle(ctx, str(args.get("task_id") or ""))
        deadline = time.monotonic() + max(1.0, min(float(args.get("timeout") or _timeout(ctx)), 300.0))
        while time.monotonic() < deadline:
            handle, task = _get(ctx, handle)
            if handle["state"] in TERMINAL:
                return _json(
                    {
                        "ok": task is not None,
                        "handle": handle["handle_id"],
                        "handleInfo": _public_handle(handle),
                        "task": task,
                    }
                )
            time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))
        _mark_unknown(ctx, handle)
        return _json(
            {
                "ok": False,
                "state": "outcome_unknown",
                "handle": handle["handle_id"],
                "handleInfo": _public_handle(handle),
            }
        )
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        if handle is not None:
            _mark_unknown(ctx, handle)
        return _json(
            {
                "ok": False,
                "state": "outcome_unknown",
                "handle": handle["handle_id"] if handle else None,
                "handleInfo": _public_handle(handle) if handle else None,
                "error": str(exc),
            }
        )
    except urllib.error.HTTPError as exc:
        if _is_redirect(exc) and handle is not None:
            _record_rejection(ctx, handle, str(exc))
        return _json(
            {
                "ok": False,
                "handle": handle["handle_id"] if handle else None,
                "handleInfo": _public_handle(handle) if handle else None,
                "error": str(exc),
            }
        )
    except ValueError as exc:
        if handle is not None:
            _mark_unknown(ctx, handle)
        return _json(
            {
                "ok": False,
                "state": "outcome_unknown" if handle is not None else None,
                "handle": handle["handle_id"] if handle else None,
                "handleInfo": _public_handle(handle) if handle else None,
                "error": str(exc),
            }
        )


def _list(ctx: Any, args: dict[str, Any]) -> str:
    local = [
        {
            key: handle.get(key)
            for key in ("handle_id", "context_id", "message_id", "state", "remote_task_id", "updated_at")
        }
        for handle in _handles(ctx)
    ]
    try:
        endpoint = _endpoint(ctx)
        params: dict[str, Any] = {"pageSize": min(max(int(args.get("page_size") or 50), 1), 100)}
        if args.get("context_id"):
            params["contextId"] = str(args["context_id"])
        response = _request(
            endpoint, {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "ListTasks", "params": params}, _timeout(ctx)
        )
        return _json({"ok": True, "handles": local, "remote": response.get("result", {})})
    except (ValueError, urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        return _json({"ok": False, "handles": local, "error": str(exc)})


def _cancel(ctx: Any, args: dict[str, Any]) -> str:
    handle: dict[str, Any] | None = None
    try:
        handle = _load_handle(ctx, str(args.get("task_id") or ""))
        if not handle.get("remote_task_id"):
            recovered = _recover_unique(ctx, handle)
            if recovered is None:
                _mark_unknown(ctx, handle)
                return _json(
                    {
                        "ok": False,
                        "state": "outcome_unknown",
                        "handle": handle["handle_id"],
                        "handleInfo": _public_handle(handle),
                        "error": "no unique ListTasks recovery candidate; cancellation was not sent",
                    }
                )
            handle, _task = recovered
        endpoint = _loopback_endpoint(str(handle["endpoint"]))
        response = _request(
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": uuid.uuid4().hex,
                "method": "CancelTask",
                "params": {"id": handle["remote_task_id"]},
            },
            _timeout(ctx),
        )
        return _json({"ok": True, "result": response.get("result", {}), "computationStopped": "unknown"})
    except urllib.error.HTTPError as exc:
        if _is_redirect(exc) and handle is not None:
            _record_rejection(ctx, handle, str(exc))
        return _json(
            {
                "ok": False,
                "handle": handle["handle_id"] if handle else None,
                "handleInfo": _public_handle(handle) if handle else None,
                "error": str(exc),
            }
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if handle is not None:
            _mark_unknown(ctx, handle)
        return _json(
            {
                "ok": False,
                "state": "outcome_unknown",
                "handle": handle["handle_id"] if handle else None,
                "handleInfo": _public_handle(handle) if handle else None,
                "error": str(exc),
            }
        )
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)})


def _schema(
    name: str, description: str, properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []},
    }


def register_tools(ctx: Any) -> None:
    handlers = {
        "codex_a2a_call": (
            _call,
            _schema(
                "codex_a2a_call",
                "Submit a Codex task and persist its A2A handle; never waits for completion.",
                {
                    "message": {"type": "string"},
                    "task_id": {"type": "string", "description": "Saved local handle for INPUT_REQUIRED continuation"},
                    "context_id": {"type": "string"},
                    "message_id": {"type": "string"},
                    "model": {"type": "string"},
                    "reasoning_effort": {"type": "string"},
                    "require_exact": {"type": "boolean"},
                },
                ["message"],
            ),
        ),
        "codex_a2a_get": (
            _get_tool,
            _schema(
                "codex_a2a_get",
                "Get a previously persisted Codex A2A task.",
                {"task_id": {"type": "string"}},
                ["task_id"],
            ),
        ),
        "codex_a2a_wait": (
            _wait,
            _schema(
                "codex_a2a_wait",
                "Poll a persisted task without resending; timeout becomes outcome_unknown.",
                {"task_id": {"type": "string"}, "timeout": {"type": "number"}},
                ["task_id"],
            ),
        ),
        "codex_a2a_list": (
            _list,
            _schema(
                "codex_a2a_list",
                "List Codex A2A tasks by optional context.",
                {"context_id": {"type": "string"}, "page_size": {"type": "integer"}},
            ),
        ),
        "codex_a2a_cancel": (
            _cancel,
            _schema(
                "codex_a2a_cancel",
                "Request best-effort cancellation for a persisted task.",
                {"task_id": {"type": "string"}},
                ["task_id"],
            ),
        ),
    }
    for name, (handler, schema) in handlers.items():
        ctx.register_tool(
            name=name,
            toolset="codex_a2a",
            schema=schema,
            handler=lambda args, h=handler, **_: h(ctx, args),
            description=schema["description"],
            emoji="🔁",
        )
