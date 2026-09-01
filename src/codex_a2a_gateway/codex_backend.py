from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import __version__
from .models import BridgeError, TaskState

StartedCallback = Callable[[str, str | None], Awaitable[None]]
UpdateCallback = Callable[[str, bool], Awaitable[None]]
ExecutionDecisionCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class BackendResult:
    state: str
    text: str = ""
    thread_id: str = ""
    turn_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class CodexBackend(Protocol):
    name: str
    supports_input_required: bool

    async def run(
        self,
        *,
        task_id: str,
        prompt: str,
        thread_id: str | None,
        message_id: str,
        execution_preferences: dict[str, Any] | None = None,
        on_started: StartedCallback,
        on_update: UpdateCallback,
        on_execution_decision: ExecutionDecisionCallback | None = None,
    ) -> BackendResult: ...

    async def cancel(self, task_id: str, thread_id: str | None, turn_id: str | None) -> bool: ...


class _SubprocessBackend:
    def __init__(self, *, codex_bin: str, workspace: Path, timeout: float):
        self.codex_bin = codex_bin
        self.workspace = Path(workspace)
        self.timeout = timeout
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def _stop(self, task_id: str) -> bool:
        process = self._processes.get(task_id)
        if not process or process.returncode is not None:
            return False
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()
        return True


class AppServerBackend(_SubprocessBackend):
    """One stdio JSON-RPC App Server connection per active A2A turn."""

    name = "app-server"
    supports_input_required = True

    def __init__(
        self,
        *,
        codex_bin: str,
        workspace: Path,
        timeout: float,
        approval_policy: str,
        allowed_models: tuple[str, ...] = (),
        default_model: str = "",
        allowed_reasoning_efforts: tuple[str, ...] = (),
        default_reasoning_effort: str = "",
    ):
        super().__init__(codex_bin=codex_bin, workspace=workspace, timeout=timeout)
        self.approval_policy = approval_policy
        self.allowed_models = allowed_models
        self.default_model = default_model
        self.allowed_reasoning_efforts = allowed_reasoning_efforts
        self.default_reasoning_effort = default_reasoning_effort
        self._request_ids: dict[str, int] = {}

    @staticmethod
    def _catalog_models(result: dict[str, Any]) -> list[dict[str, Any]]:
        models = result.get("data")
        if not isinstance(models, list):
            raise BridgeError("app_server_protocol", "Codex App Server model/list omitted data")
        return [model for model in models if isinstance(model, dict)]

    async def _model_catalog(
        self, process: asyncio.subprocess.Process, request_id: int
    ) -> tuple[list[dict[str, Any]], int]:
        catalog: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            await self._write(process, {"id": request_id, "method": "model/list", "params": params})
            result, interaction = await self._response(process, request_id)
            if interaction:
                raise BridgeError(
                    "app_server_protocol", "Codex App Server requested interaction during model discovery"
                )
            catalog.extend(self._catalog_models(result))
            next_cursor = result.get("nextCursor")
            if next_cursor is None or next_cursor == "":
                return catalog, request_id + 1
            if not isinstance(next_cursor, str):
                raise BridgeError("app_server_protocol", "Codex App Server model/list returned an invalid nextCursor")
            cursor = next_cursor
            request_id += 1

    def _choose_execution_preferences(
        self, catalog: list[dict[str, Any]], preferences: dict[str, Any]
    ) -> dict[str, Any]:
        raw_requested = preferences.get("requested")
        local_default_only = not isinstance(raw_requested, dict)
        requested: dict[str, Any] = raw_requested if isinstance(raw_requested, dict) else {}
        requested_model = requested.get("model")
        requested_effort = requested.get("reasoningEffort")
        require_exact = bool(requested.get("requireExact"))
        identifiers = {
            str(value)
            for item in catalog
            for value in (item.get("id"), item.get("model"))
            if isinstance(value, str) and value
        }
        by_identifier = {
            str(value): item
            for item in catalog
            for value in (item.get("id"), item.get("model"))
            if isinstance(value, str) and value
        }

        def receiver_default() -> str:
            for item in catalog:
                candidate = str(item.get("model") or item.get("id") or "")
                if item.get("isDefault") and (not self.allowed_models or candidate in self.allowed_models):
                    return candidate
            for item in catalog:
                candidate = str(item.get("model") or item.get("id") or "")
                if not self.allowed_models or candidate in self.allowed_models:
                    return candidate
            return ""

        selected_model = requested_model or self.default_model or receiver_default()
        fallback = False
        if selected_model and (
            selected_model not in identifiers or (self.allowed_models and selected_model not in self.allowed_models)
        ):
            if require_exact and requested_model:
                preferences["decision"] = {
                    "requested": requested,
                    "effective": {"model": None, "reasoningEffort": None},
                    "fallbackApplied": False,
                    "backend": self.name,
                    "rejected": True,
                    "reason": "requested_model_unavailable",
                }
                raise BridgeError(
                    "execution_preference_unavailable", "Requested Codex model is not available to this receiver."
                )
            fallback = True
            selected_model = receiver_default()
        selected_entry = by_identifier.get(selected_model, {})
        supported_efforts = {
            str(option.get("reasoningEffort"))
            for option in selected_entry.get("supportedReasoningEfforts", [])
            if isinstance(option, dict) and isinstance(option.get("reasoningEffort"), str)
        }
        selected_effort = (
            requested_effort or self.default_reasoning_effort or selected_entry.get("defaultReasoningEffort")
        )
        if selected_effort and (
            selected_effort not in supported_efforts
            or (self.allowed_reasoning_efforts and selected_effort not in self.allowed_reasoning_efforts)
        ):
            if require_exact and requested_effort:
                preferences["decision"] = {
                    "requested": requested,
                    "effective": {"model": selected_model or None, "reasoningEffort": None},
                    "fallbackApplied": False,
                    "backend": self.name,
                    "rejected": True,
                    "reason": "requested_reasoning_effort_unavailable",
                }
                raise BridgeError(
                    "execution_preference_unavailable",
                    "Requested reasoning effort is not available for the selected Codex model.",
                )
            fallback = True
            selected_effort = str(selected_entry.get("defaultReasoningEffort") or "")
            if selected_effort not in supported_efforts or (
                self.allowed_reasoning_efforts and selected_effort not in self.allowed_reasoning_efforts
            ):
                selected_effort = next(
                    (
                        str(option.get("reasoningEffort"))
                        for option in selected_entry.get("supportedReasoningEfforts", [])
                        if isinstance(option, dict)
                        and isinstance(option.get("reasoningEffort"), str)
                        and (
                            not self.allowed_reasoning_efforts
                            or str(option["reasoningEffort"]) in self.allowed_reasoning_efforts
                        )
                    ),
                    "",
                )
        decision = {
            "requested": requested,
            "effective": {
                "model": selected_model or None,
                "reasoningEffort": selected_effort or None,
            },
            "fallbackApplied": fallback,
            "backend": self.name,
        }
        if local_default_only:
            decision["source"] = "receiver-default"
        preferences["decision"] = decision
        return {"model": selected_model, "effort": selected_effort}

    @staticmethod
    async def _write(process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
        if process.stdin is None:
            raise BridgeError("app_server_closed", "Codex App Server stdin is unavailable")
        process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await process.stdin.drain()

    @staticmethod
    async def _read(process: asyncio.subprocess.Process) -> dict[str, Any]:
        if process.stdout is None:
            raise BridgeError("app_server_closed", "Codex App Server stdout is unavailable")
        line = await process.stdout.readline()
        if not line:
            code = await process.wait()
            raise BridgeError("app_server_closed", f"Codex App Server exited before completion ({code})")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BridgeError("app_server_protocol", "Codex App Server emitted invalid JSONL") from exc
        if not isinstance(value, dict):
            raise BridgeError("app_server_protocol", "Codex App Server emitted a non-object frame")
        return value

    async def _server_request(self, process: asyncio.subprocess.Process, message: dict[str, Any]) -> str | None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if request_id is None or not method:
            return None
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            await self._write(process, {"id": request_id, "result": {"decision": "cancel"}})
            return "Codex requires operator approval; resubmit after adjusting the gateway approval policy or task."
        if method == "item/permissions/requestApproval":
            await self._write(process, {"id": request_id, "result": {"permissions": {}}})
            return "Codex requires additional permissions; adjust the gateway policy before retrying."
        if method == "item/tool/requestUserInput":
            await self._write(process, {"id": request_id, "result": {"answers": {}}})
            return "Codex requires additional user input; send the answer as the next message in this context."
        await self._write(
            process,
            {"id": request_id, "error": {"code": -32601, "message": "Gateway cannot satisfy this client request"}},
        )
        return "Codex requested an unsupported client interaction."

    async def _response(
        self, process: asyncio.subprocess.Process, request_id: int
    ) -> tuple[dict[str, Any], str | None]:
        while True:
            message = await self._read(process)
            if message.get("id") == request_id:
                if "error" in message:
                    error = message.get("error") or {}
                    text = error.get("message") if isinstance(error, dict) else str(error)
                    raise BridgeError("app_server_rpc", f"Codex App Server request failed: {text}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise BridgeError("app_server_protocol", "Codex App Server response omitted an object result")
                return result, None
            requested = await self._server_request(process, message)
            if requested:
                return {}, requested

    async def run(
        self,
        *,
        task_id: str,
        prompt: str,
        thread_id: str | None,
        message_id: str,
        execution_preferences: dict[str, Any] | None = None,
        on_started: StartedCallback,
        on_update: UpdateCallback,
        on_execution_decision: ExecutionDecisionCallback | None = None,
    ) -> BackendResult:
        process = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.workspace,
        )
        self._processes[task_id] = process
        execution_preferences = execution_preferences or {}
        try:
            async with asyncio.timeout(self.timeout):
                await self._write(
                    process,
                    {
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {
                                "name": "codex-a2a-gateway",
                                "title": "Codex A2A Gateway",
                                "version": __version__,
                            },
                            "capabilities": {"experimentalApi": False},
                        },
                    },
                )
                await self._response(process, 1)
                await self._write(process, {"method": "initialized", "params": {}})

                # Keep the ordinary App Server protocol unchanged. Discovery
                # is needed only when a remote preference or local default
                # would cause this gateway to select model/effort explicitly.
                if execution_preferences or self.default_model or self.default_reasoning_effort:
                    # Configuration may narrow this catalog but can never make
                    # an unavailable model/effort appear supported.
                    catalog, next_request_id = await self._model_catalog(process, 2)
                    try:
                        execution = self._choose_execution_preferences(catalog, execution_preferences)
                    except BridgeError:
                        if on_execution_decision and execution_preferences.get("decision"):
                            await on_execution_decision(execution_preferences)
                        raise
                    if on_execution_decision and execution_preferences.get("decision"):
                        # Persist before thread/turn start so a later failure
                        # cannot erase the receiver's effective choice.
                        await on_execution_decision(execution_preferences)
                else:
                    next_request_id = 2
                    execution = {}

                thread_method = "thread/resume" if thread_id else "thread/start"
                thread_params: dict[str, Any] = {
                    "approvalPolicy": self.approval_policy,
                    "cwd": str(self.workspace),
                }
                if thread_id:
                    thread_params["threadId"] = thread_id
                else:
                    thread_params["serviceName"] = "codex-a2a-gateway"
                await self._write(process, {"id": next_request_id, "method": thread_method, "params": thread_params})
                thread_result, interaction = await self._response(process, next_request_id)
                if interaction:
                    return BackendResult(TaskState.INPUT_REQUIRED.value, interaction, thread_id or "")
                thread = thread_result.get("thread") or {}
                actual_thread_id = str(thread.get("id") or thread_id or "") if isinstance(thread, dict) else ""
                if not actual_thread_id:
                    raise BridgeError("app_server_protocol", "Codex App Server did not return a thread id")
                await on_started(actual_thread_id, None)

                turn_params: dict[str, Any] = {
                    "threadId": actual_thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "clientUserMessageId": message_id,
                }
                # These fields are only supplied after inbound validation has
                # selected a receiver-approved effective value.  The client
                # preference is never copied blindly into App Server params.
                model = execution.get("model")
                reasoning_effort = execution.get("effort")
                if model:
                    turn_params["model"] = model
                if reasoning_effort:
                    turn_params["effort"] = reasoning_effort
                await self._write(
                    process,
                    {
                        "id": next_request_id + 1,
                        "method": "turn/start",
                        "params": turn_params,
                    },
                )
                turn_result, interaction = await self._response(process, next_request_id + 1)
                if interaction:
                    return BackendResult(TaskState.INPUT_REQUIRED.value, interaction, actual_thread_id)
                turn = turn_result.get("turn") or {}
                turn_id = str(turn.get("id") or "") if isinstance(turn, dict) else ""
                if not turn_id:
                    raise BridgeError("app_server_protocol", "Codex App Server did not return a turn id")
                await on_started(actual_thread_id, turn_id)

                delta_chunks: list[str] = []
                final_text = ""
                while True:
                    message = await self._read(process)
                    interaction = await self._server_request(process, message)
                    if interaction:
                        return BackendResult(
                            TaskState.INPUT_REQUIRED.value,
                            interaction,
                            actual_thread_id,
                            turn_id,
                        )
                    method = str(message.get("method") or "")
                    params = message.get("params") or {}
                    if method == "item/agentMessage/delta" and isinstance(params, dict):
                        delta = params.get("delta")
                        if isinstance(delta, str):
                            delta_chunks.append(delta)
                            await on_update(delta, True)
                    elif method == "item/completed" and isinstance(params, dict):
                        item = params.get("item") or {}
                        if isinstance(item, dict) and item.get("type") == "agentMessage":
                            text = item.get("text")
                            if isinstance(text, str):
                                final_text = text
                                await on_update(final_text, False)
                    elif method == "turn/completed" and isinstance(params, dict):
                        completed = params.get("turn") or {}
                        status = str(completed.get("status") or "failed") if isinstance(completed, dict) else "failed"
                        text = final_text or "".join(delta_chunks)
                        if status == "completed":
                            return BackendResult(TaskState.COMPLETED.value, text, actual_thread_id, turn_id)
                        if status == "interrupted":
                            return BackendResult(TaskState.CANCELED.value, text, actual_thread_id, turn_id)
                        error = completed.get("error") if isinstance(completed, dict) else None
                        return BackendResult(
                            TaskState.FAILED.value,
                            text,
                            actual_thread_id,
                            turn_id,
                            "codex_turn_failed",
                            str(error or "Codex turn failed"),
                        )
        except TimeoutError as exc:
            raise BridgeError("codex_timeout", "Codex App Server turn exceeded its configured timeout") from exc
        finally:
            await self._stop(task_id)
            self._processes.pop(task_id, None)

    async def cancel(self, task_id: str, thread_id: str | None, turn_id: str | None) -> bool:
        process = self._processes.get(task_id)
        if process and process.returncode is None and thread_id and turn_id:
            request_id = self._request_ids.get(task_id, 100) + 1
            self._request_ids[task_id] = request_id
            try:
                await self._write(
                    process,
                    {
                        "id": request_id,
                        "method": "turn/interrupt",
                        "params": {"threadId": thread_id, "turnId": turn_id},
                    },
                )
                return True
            except (BridgeError, BrokenPipeError, ConnectionResetError):
                pass
        return await self._stop(task_id)


class CLIBackend(_SubprocessBackend):
    """Explicit compatibility fallback using `codex exec --json` JSONL."""

    name = "cli"
    supports_input_required = False

    async def run(
        self,
        *,
        task_id: str,
        prompt: str,
        thread_id: str | None,
        message_id: str,
        execution_preferences: dict[str, Any] | None = None,
        on_started: StartedCallback,
        on_update: UpdateCallback,
        on_execution_decision: ExecutionDecisionCallback | None = None,
    ) -> BackendResult:
        del message_id
        del on_execution_decision
        if execution_preferences:
            raise BridgeError(
                "execution_preferences_unsupported",
                "The explicit CLI backend does not support model or reasoning preferences.",
            )
        if thread_id:
            command = [self.codex_bin, "exec", "resume", "--json", thread_id, "-"]
        else:
            command = [self.codex_bin, "exec", "--json", "--cd", str(self.workspace), "-"]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.workspace,
        )
        self._processes[task_id] = process
        if process.stdin is None or process.stdout is None:
            raise BridgeError("cli_closed", "Codex CLI stdio is unavailable")
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()
        actual_thread_id = thread_id or ""
        final_text = ""
        turn_failed = False
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise BridgeError("cli_protocol", "codex exec emitted invalid JSONL") from exc
                    if not isinstance(event, dict):
                        continue
                    event_type = str(event.get("type") or "")
                    if event_type == "thread.started":
                        actual_thread_id = str(event.get("thread_id") or actual_thread_id)
                        await on_started(actual_thread_id, None)
                    elif event_type == "item.completed":
                        item = event.get("item") or {}
                        if isinstance(item, dict) and item.get("type") == "agent_message":
                            text = item.get("text")
                            if isinstance(text, str):
                                final_text = text
                                await on_update(final_text, False)
                    elif event_type in {"turn.failed", "error"}:
                        turn_failed = True
                code = await process.wait()
                if code != 0 or turn_failed:
                    return BackendResult(
                        TaskState.FAILED.value,
                        final_text,
                        actual_thread_id,
                        error_code="codex_cli_failed",
                        error_message=f"codex exec failed with exit code {code}",
                    )
                if not actual_thread_id:
                    raise BridgeError("cli_protocol", "codex exec did not emit thread.started")
                return BackendResult(TaskState.COMPLETED.value, final_text, actual_thread_id)
        except TimeoutError as exc:
            raise BridgeError("codex_timeout", "codex exec exceeded its configured timeout") from exc
        finally:
            await self._stop(task_id)
            self._processes.pop(task_id, None)

    async def cancel(self, task_id: str, thread_id: str | None, turn_id: str | None) -> bool:
        del thread_id, turn_id
        return await self._stop(task_id)
