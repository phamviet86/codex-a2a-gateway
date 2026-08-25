from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp import types
from mcp.server import MCPServer
from pydantic import Field

from .core import BridgeService
from .models import BridgeError
from .settings import Settings

INSTRUCTIONS = (
    "Use hermes_chat to delegate analysis or work to the Hermes default agent. "
    "Reuse conversation_key (or returned context_id) for follow-up turns. For long tasks, "
    "use mode='async', then hermes_task_wait/get; answer input_required by calling hermes_chat "
    "again in the same conversation. Cancellation is best-effort and never proves computation stopped. "
    "Do not repeatedly resend a mutating request after an ambiguous timeout; provide idempotency_key. "
    "This server exposes conversation/task operations only, not Hermes administration."
)

mcp = MCPServer(
    "codex-hermes-a2a-bridge",
    description="Local MCP bridge from Codex to the Hermes default agent over A2A v1.0.",
    instructions=INSTRUCTIONS,
    version="0.1.1",
    log_level="WARNING",
)

_service: BridgeService | None = None


def get_service() -> BridgeService:
    global _service
    if _service is None:
        _service = BridgeService(Settings.from_env())
    return _service


async def _safe(call: Any) -> dict[str, Any]:
    try:
        return await call
    except BridgeError as exc:
        return exc.as_result()
    except Exception as exc:  # MCP tools must return a short, model-readable failure.
        return {
            "ok": False,
            "error": {"code": "bridge_internal", "message": f"Bridge failure: {type(exc).__name__}"},
            "retryable": False,
        }


READ_ONLY = types.ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
MUTATING = types.ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


@mcp.tool(
    name="hermes_status",
    description="Check bridge persistence, Hermes health, connectivity, and a concise Agent Card summary.",
    annotations=READ_ONLY,
    structured_output=True,
)
async def hermes_status() -> dict[str, Any]:
    return await _safe(get_service().status())


@mcp.tool(
    name="hermes_chat",
    description="Start or continue a Hermes conversation; returns a durable bridge task and A2A context mapping.",
    annotations=MUTATING,
    structured_output=True,
)
async def hermes_chat(
    message: Annotated[str, Field(description="User request for Hermes", min_length=1)],
    conversation_key: Annotated[str | None, Field(description="Stable Codex conversation identifier")] = None,
    context_id: Annotated[
        str | None, Field(description="Existing A2A contextId; normally reuse the returned value")
    ] = None,
    profile: Annotated[Literal["default"], Field(description="Hermes profile; v0.1 supports default only")] = "default",
    mode: Annotated[
        Literal["auto", "sync", "async"], Field(description="auto waits briefly, sync waits, async returns early")
    ] = "auto",
    timeout: Annotated[float | None, Field(description="Absolute task/stream timeout in seconds", ge=1, le=300)] = None,
    idempotency_key: Annotated[
        str | None, Field(description="Client key used to deduplicate exactly matching submissions", max_length=256)
    ] = None,
) -> dict[str, Any]:
    return await _safe(
        get_service().chat(
            message,
            conversation_key=conversation_key,
            context_id=context_id,
            profile=profile,
            mode=mode,
            timeout=timeout,
            idempotency_key=idempotency_key,
        )
    )


@mcp.tool(
    name="hermes_task_get",
    description="Get one bridge task, its Hermes status/result/input request, and recent lifecycle events.",
    annotations=READ_ONLY,
    structured_output=True,
)
async def hermes_task_get(
    task_id: Annotated[str, Field(description="bridge_task_id or known A2A task id", min_length=1)],
    refresh: Annotated[bool, Field(description="Refresh a nonterminal task from Hermes when possible")] = True,
) -> dict[str, Any]:
    return await _safe(get_service().task_get(task_id, refresh=refresh))


@mcp.tool(
    name="hermes_tasks_list",
    description="List durable bridge tasks, optionally filtered by conversation and bridge state.",
    annotations=READ_ONLY,
    structured_output=True,
)
async def hermes_tasks_list(
    conversation_key: Annotated[str | None, Field(description="Optional Codex conversation identifier")] = None,
    status: Annotated[str | None, Field(description="Optional bridge state such as working or completed")] = None,
    limit: Annotated[int, Field(description="Maximum tasks", ge=1, le=100)] = 20,
) -> dict[str, Any]:
    return await _safe(get_service().tasks_list(conversation_key=conversation_key, status=status, limit=limit))


@mcp.tool(
    name="hermes_task_wait",
    description="Wait for task progress/result using the active stream, A2A subscribe, then polling fallback.",
    annotations=READ_ONLY,
    structured_output=True,
)
async def hermes_task_wait(
    task_id: Annotated[str, Field(description="bridge_task_id or known A2A task id", min_length=1)],
    timeout: Annotated[float, Field(description="Maximum wait in seconds", ge=1, le=300)] = 30,
) -> dict[str, Any]:
    return await _safe(get_service().task_wait(task_id, timeout=timeout))


@mcp.tool(
    name="hermes_task_cancel",
    description="Request task cancellation; response is explicit that Hermes may continue underlying computation.",
    annotations=MUTATING,
    structured_output=True,
)
async def hermes_task_cancel(
    task_id: Annotated[str, Field(description="bridge_task_id or known A2A task id", min_length=1)],
    timeout: Annotated[float, Field(description="Cancel request timeout in seconds", ge=1, le=60)] = 10,
) -> dict[str, Any]:
    return await _safe(get_service().task_cancel(task_id, timeout=timeout))


@mcp.tool(
    name="hermes_contexts",
    description="List, inspect, or close bridge-owned conversation/context mappings; close never deletes Hermes data.",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
    structured_output=True,
)
async def hermes_contexts(
    action: Annotated[Literal["list", "inspect", "close"], Field(description="Mapping operation")] = "list",
    conversation_key: Annotated[str | None, Field(description="Select a mapping by Codex conversation")] = None,
    context_id: Annotated[str | None, Field(description="Select a mapping by A2A contextId")] = None,
    limit: Annotated[int, Field(description="Maximum rows/tasks", ge=1, le=100)] = 20,
) -> dict[str, Any]:
    return await _safe(
        get_service().contexts(action=action, conversation_key=conversation_key, context_id=context_id, limit=limit)
    )


def run_stdio() -> None:
    mcp.run(transport="stdio")
