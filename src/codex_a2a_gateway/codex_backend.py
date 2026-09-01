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
        on_started: StartedCallback,
        on_update: UpdateCallback,
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

    def __init__(self, *, codex_bin: str, workspace: Path, timeout: float, approval_policy: str):
        super().__init__(codex_bin=codex_bin, workspace=workspace, timeout=timeout)
        self.approval_policy = approval_policy
        self._request_ids: dict[str, int] = {}

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
        on_started: StartedCallback,
        on_update: UpdateCallback,
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

                thread_method = "thread/resume" if thread_id else "thread/start"
                thread_params: dict[str, Any] = {
                    "approvalPolicy": self.approval_policy,
                    "cwd": str(self.workspace),
                }
                if thread_id:
                    thread_params["threadId"] = thread_id
                else:
                    thread_params["serviceName"] = "codex-a2a-gateway"
                await self._write(process, {"id": 2, "method": thread_method, "params": thread_params})
                thread_result, interaction = await self._response(process, 2)
                if interaction:
                    return BackendResult(TaskState.INPUT_REQUIRED.value, interaction, thread_id or "")
                thread = thread_result.get("thread") or {}
                actual_thread_id = str(thread.get("id") or thread_id or "") if isinstance(thread, dict) else ""
                if not actual_thread_id:
                    raise BridgeError("app_server_protocol", "Codex App Server did not return a thread id")
                await on_started(actual_thread_id, None)

                await self._write(
                    process,
                    {
                        "id": 3,
                        "method": "turn/start",
                        "params": {
                            "threadId": actual_thread_id,
                            "input": [{"type": "text", "text": prompt}],
                            "clientUserMessageId": message_id,
                        },
                    },
                )
                turn_result, interaction = await self._response(process, 3)
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
        on_started: StartedCallback,
        on_update: UpdateCallback,
    ) -> BackendResult:
        del message_id
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
