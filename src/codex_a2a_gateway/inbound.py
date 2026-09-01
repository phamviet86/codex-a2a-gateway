from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from .codex_backend import AppServerBackend, BackendResult, CLIBackend, CodexBackend
from .models import TERMINAL_STATES, TURN_END_STATES, BridgeError, TaskRecord, TaskState, now_iso, request_fingerprint
from .settings import Settings
from .store import Store

A2A_STATES = {
    TaskState.QUEUED.value: "TASK_STATE_SUBMITTED",
    TaskState.SUBMITTED.value: "TASK_STATE_SUBMITTED",
    TaskState.WORKING.value: "TASK_STATE_WORKING",
    TaskState.INPUT_REQUIRED.value: "TASK_STATE_INPUT_REQUIRED",
    TaskState.COMPLETED.value: "TASK_STATE_COMPLETED",
    TaskState.FAILED.value: "TASK_STATE_FAILED",
    TaskState.CANCELED.value: "TASK_STATE_CANCELED",
    TaskState.REJECTED.value: "TASK_STATE_REJECTED",
    TaskState.OUTCOME_UNKNOWN.value: "TASK_STATE_FAILED",
}
INTERNAL_STATES = {
    "TASK_STATE_SUBMITTED": (TaskState.QUEUED.value, TaskState.SUBMITTED.value),
    "TASK_STATE_WORKING": (TaskState.WORKING.value,),
    "TASK_STATE_INPUT_REQUIRED": (TaskState.INPUT_REQUIRED.value,),
    "TASK_STATE_COMPLETED": (TaskState.COMPLETED.value,),
    "TASK_STATE_FAILED": (TaskState.FAILED.value, TaskState.OUTCOME_UNKNOWN.value),
    "TASK_STATE_CANCELED": (TaskState.CANCELED.value,),
    "TASK_STATE_REJECTED": (TaskState.REJECTED.value,),
}


class InboundService:
    """Durable A2A-to-Codex task orchestration, separate from the outbound Hermes service."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: Store | None = None,
        app_backend: CodexBackend | None = None,
        cli_backend: CodexBackend | None = None,
    ):
        self.settings = settings
        self.store = store or Store(settings.state_path)
        self.backends: dict[str, CodexBackend] = {
            "app-server": app_backend
            or AppServerBackend(
                codex_bin=settings.codex_bin,
                workspace=settings.codex_workspace,
                timeout=settings.codex_timeout,
                approval_policy=settings.approval_policy,
                allowed_models=settings.codex_allowed_models,
                default_model=settings.codex_default_model,
                allowed_reasoning_efforts=settings.codex_allowed_reasoning_efforts,
                default_reasoning_effort=settings.codex_default_reasoning_effort,
            ),
            "cli": cli_backend
            or CLIBackend(
                codex_bin=settings.codex_bin,
                workspace=settings.codex_workspace,
                timeout=settings.codex_timeout,
            ),
        }
        self._context_locks: dict[str, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._admission_lock = asyncio.Lock()
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[TaskRecord]]] = {}
        self._recover_after_restart()

    def _recover_after_restart(self) -> None:
        self.store.fail_active_inbound_tasks(
            error_code="gateway_restarted",
            error_message=(
                "The gateway restarted while this task was active. The context-to-thread mapping was retained; "
                "send a new message in the same context to continue."
            ),
        )

    async def aclose(self) -> None:
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._subscribers.clear()

    @property
    def backend_capabilities(self) -> dict[str, Any]:
        configured = self.backends[self.settings.backend]
        return {
            "backend": configured.name,
            "appServerTransport": "stdio" if configured.name == "app-server" else None,
            "cliFallbackEnabled": self.settings.cli_fallback,
            "inputRequired": False,
            "cancel": "best-effort",
            "pushNotifications": False,
        }

    @staticmethod
    def task_payload(
        task: TaskRecord,
        *,
        include_artifacts: bool = True,
        history_length: int | None = None,
    ) -> dict[str, Any]:
        state = A2A_STATES.get(task.state, "TASK_STATE_FAILED")
        payload: dict[str, Any] = {
            "id": task.a2a_task_id or task.bridge_task_id,
            "contextId": task.context_id,
            "status": {"state": state, "timestamp": task.updated_at},
        }
        if include_artifacts and task.state == TaskState.COMPLETED.value and task.result_text:
            payload["artifacts"] = [
                {
                    "artifactId": f"artifact-{task.bridge_task_id}",
                    "name": "Codex response",
                    "parts": [{"text": task.result_text, "mediaType": "text/plain"}],
                }
            ]
        elif task.result_text or task.error_message:
            payload["status"]["message"] = {
                "messageId": f"status-{task.bridge_task_id}",
                "role": "ROLE_AGENT",
                "contextId": task.context_id,
                "parts": [
                    {
                        "text": task.result_text or task.error_message or "Task failed",
                        "mediaType": "text/plain",
                    }
                ],
            }
        metadata: dict[str, Any] = {}
        # This non-sensitive correlation value lets a durable client recover
        # only the task created by its original A2A message, not any task that
        # happens to share a reused context.
        if task.message_id:
            metadata["requestMessageId"] = task.message_id
        if task.error_code:
            metadata["errorCode"] = task.error_code
        if task.execution_metadata:
            metadata["executionPreferences"] = task.execution_metadata
        if metadata:
            payload["metadata"] = metadata
        if history_length is not None and history_length > 0:
            payload["history"] = []
        return payload

    async def submit(
        self,
        message: str,
        *,
        context_id: str | None,
        message_id: str,
        task_id: str | None = None,
        execution_preferences: dict[str, Any] | None = None,
        subscriber: asyncio.Queue[TaskRecord] | None = None,
    ) -> tuple[TaskRecord, bool]:
        message = message.strip()
        if not message:
            raise BridgeError("invalid_message", "message must contain text")
        if len(message) > self.settings.max_message_chars:
            raise BridgeError("message_too_large", f"message exceeds {self.settings.max_message_chars} characters")
        if not message_id or len(message_id) > 256:
            raise BridgeError("invalid_message_id", "messageId must contain 1 to 256 characters")
        continued_task: TaskRecord | None = None
        if task_id:
            continued_task = self.store.get_task(task_id)
            if not continued_task or continued_task.direction != "inbound":
                raise BridgeError("task_not_found", "inbound taskId was not found")
            if context_id and context_id != continued_task.context_id:
                raise BridgeError("task_context_mismatch", "message taskId and contextId refer to different tasks")
            context_id = continued_task.context_id
        existing_message = self.store.get_inbound_message(message_id)
        if not context_id and existing_message:
            context_id = existing_message["context_id"]
        actual_context_id = (context_id or f"ctx-{uuid.uuid4().hex}").strip()
        if not actual_context_id or len(actual_context_id) > 256:
            raise BridgeError("invalid_context_id", "contextId must contain 1 to 256 characters")
        conversation_key = f"inbound:{actual_context_id}"
        preference_fingerprint = json.dumps(execution_preferences or {}, sort_keys=True, separators=(",", ":"))
        fingerprint = request_fingerprint(f"{message}\n{preference_fingerprint}", actual_context_id, "inbound")
        if existing_message:
            continued_id = continued_task.bridge_task_id if continued_task else None
            if (
                existing_message["request_fingerprint"] != fingerprint
                or existing_message["context_id"] != actual_context_id
                or (continued_id and existing_message["bridge_task_id"] != continued_id)
            ):
                raise BridgeError("idempotency_conflict", "messageId was already used for a different request")
            existing = self.store.get_task(existing_message["bridge_task_id"])
            if not existing:
                raise BridgeError("task_not_found", "messageId refers to a missing task")
            if subscriber is not None:
                self._bind_subscriber(existing.bridge_task_id, subscriber)
            return await self._resume_deduplicated(existing, message), True
        if continued_task and continued_task.state != TaskState.INPUT_REQUIRED.value:
            raise BridgeError("invalid_task_state", "taskId continuation is only valid for input-required tasks")
        if continued_task and execution_preferences:
            raise BridgeError(
                "execution_preferences_continuation_unsupported",
                "execution preferences cannot be changed while continuing an input-required task",
            )

        lock = self._context_locks.setdefault(actual_context_id, asyncio.Lock())
        async with lock:
            existing_message = self.store.get_inbound_message(message_id)
            if existing_message:
                existing = self.store.get_task(existing_message["bridge_task_id"])
                continued_id = continued_task.bridge_task_id if continued_task else None
                if (
                    existing
                    and existing_message["request_fingerprint"] == fingerprint
                    and existing_message["context_id"] == actual_context_id
                    and (not continued_id or existing.bridge_task_id == continued_id)
                ):
                    if subscriber is not None:
                        self._bind_subscriber(existing.bridge_task_id, subscriber)
                    return await self._resume_deduplicated(existing, message), True
                raise BridgeError("idempotency_conflict", "messageId was already used for a different request")
            async with self._admission_lock:
                if self.store.count_active_inbound_tasks() >= self.settings.inbound_admission_limit:
                    raise BridgeError(
                        "server_overloaded",
                        f"gateway already has {self.settings.inbound_admission_limit} queued or running tasks",
                        retryable=True,
                    )
                context = self.store.get_or_create_context(
                    conversation_key=conversation_key,
                    context_id=actual_context_id,
                    endpoint="codex://local",
                    profile="inbound",
                    direction="inbound",
                    backend=self.settings.backend,
                )
                if context.direction != "inbound":
                    raise BridgeError("context_conflict", "contextId is already used by an outbound mapping")
                now = now_iso()
                if continued_task:
                    actual_task_id = continued_task.bridge_task_id
                    task = self.store.continue_inbound_task_atomic(
                        bridge_task_id=actual_task_id,
                        context_id=context.context_id,
                        message_id=message_id,
                        request_fingerprint=fingerprint,
                        max_turns=self.settings.max_turns,
                    )
                else:
                    actual_task_id = f"task-{uuid.uuid4().hex}"
                    candidate = TaskRecord(
                        bridge_task_id=actual_task_id,
                        a2a_task_id=actual_task_id,
                        context_id=context.context_id,
                        conversation_key=conversation_key,
                        profile="inbound",
                        endpoint="codex://local",
                        request_id=f"request-{uuid.uuid4().hex}",
                        message_id=message_id,
                        idempotency_key=f"inbound:{message_id}",
                        request_fingerprint=fingerprint,
                        mode="async",
                        direction="inbound",
                        execution_metadata=execution_preferences or {},
                        created_at=now,
                        updated_at=now,
                    )
                    task = self.store.create_inbound_task_atomic(candidate, max_turns=self.settings.max_turns)
                if subscriber is not None:
                    self._bind_subscriber(actual_task_id, subscriber)
                try:
                    self._spawn_worker(actual_task_id, message)
                except Exception:
                    if subscriber is not None:
                        subscribers = self._subscribers.get(actual_task_id)
                        if subscribers is not None:
                            subscribers.discard(subscriber)
                            if not subscribers:
                                self._subscribers.pop(actual_task_id, None)
                    raise
            return task, False

    @staticmethod
    def create_subscription() -> asyncio.Queue[TaskRecord]:
        return asyncio.Queue(maxsize=8)

    def _bind_subscriber(self, task_id: str, queue: asyncio.Queue[TaskRecord]) -> None:
        self._subscribers.setdefault(task_id, set()).add(queue)

    async def _resume_deduplicated(self, task: TaskRecord, message: str) -> TaskRecord:
        async with self._admission_lock:
            latest = self.store.get_task(task.bridge_task_id) or task
            if latest.state == TaskState.FAILED.value and latest.error_code == "gateway_restarted_before_start":
                if self.store.count_active_inbound_tasks() >= self.settings.inbound_admission_limit:
                    raise BridgeError("server_overloaded", "gateway worker admission is full", retryable=True)
                latest = self.store.requeue_inbound_after_restart(latest.bridge_task_id)
            if (
                latest.state == TaskState.QUEUED.value
                and not latest.cancel_requested
                and latest.bridge_task_id not in self._workers
            ):
                if len(self._workers) >= self.settings.inbound_admission_limit:
                    raise BridgeError("server_overloaded", "gateway worker admission is full", retryable=True)
                self._spawn_worker(latest.bridge_task_id, message)
            return latest

    def _spawn_worker(self, task_id: str, message: str) -> None:
        if task_id in self._workers:
            return
        worker = asyncio.create_task(self._run(task_id, message), name=f"codex-a2a-{task_id}")
        self._workers[task_id] = worker

        def drop_worker(_done: asyncio.Task[None], key: str = task_id) -> None:
            self._workers.pop(key, None)

        worker.add_done_callback(drop_worker)

    async def _notify(self, task: TaskRecord) -> None:
        for queue in tuple(self._subscribers.get(task.bridge_task_id, ())):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(task)

    async def _run_backend(self, backend: CodexBackend, task: TaskRecord, message: str) -> BackendResult:
        async def on_started(thread_id: str, turn_id: str | None) -> None:
            self.store.set_codex_thread(task.context_id, thread_id, backend.name)
            updated = self.store.update_task(task.bridge_task_id, codex_turn_id=turn_id)
            await self._notify(updated)

        async def on_update(text: str, append: bool) -> None:
            updated = (
                self.store.append_task_result(task.bridge_task_id, text)
                if append
                else self.store.update_task(task.bridge_task_id, result_text=text)
            )
            await self._notify(updated)

        async def on_execution_decision(preferences: dict[str, Any]) -> None:
            updated = self.store.update_task(task.bridge_task_id, execution_metadata=preferences)
            await self._notify(updated)

        context = self.store.get_context(context_id=task.context_id)
        return await backend.run(
            task_id=task.bridge_task_id,
            prompt=message,
            thread_id=context.codex_thread_id if context else None,
            message_id=task.message_id,
            execution_preferences=task.execution_metadata,
            on_started=on_started,
            on_update=on_update,
            on_execution_decision=on_execution_decision,
        )

    async def _run(self, task_id: str, message: str) -> None:
        task = self.store.get_task(task_id)
        if not task:
            return
        lock = self._context_locks.setdefault(task.context_id, asyncio.Lock())
        async with lock:
            await self._run_unlocked(task_id, message)

    async def _run_unlocked(self, task_id: str, message: str) -> None:
        task = self.store.get_task(task_id)
        if not task or task.state in TERMINAL_STATES or task.cancel_requested:
            return
        async with self._semaphore:
            task = self.store.get_task(task_id)
            if not task or task.state in TERMINAL_STATES or task.cancel_requested:
                return
            await self._run_with_slot(task_id, message)

    async def _run_with_slot(self, task_id: str, message: str) -> None:
        task = self.store.get_task(task_id)
        if not task or task.state in TERMINAL_STATES or task.cancel_requested:
            return
        try:
            task = self.store.update_task(task_id, state=TaskState.WORKING.value)
            await self._notify(task)
            context = self.store.get_context(context_id=task.context_id)
            backend_name = context.backend if context and context.codex_thread_id else self.settings.backend
            backend = self.backends.get(backend_name or "") or self.backends[self.settings.backend]
            try:
                result = await self._run_backend(backend, task, message)
            except (BridgeError, OSError) as primary_error:
                latest_context = self.store.get_context(context_id=task.context_id)
                can_fallback = (
                    backend.name == "app-server"
                    and self.settings.cli_fallback
                    and latest_context is not None
                    and not latest_context.codex_thread_id
                    and not task.execution_metadata
                )
                if not can_fallback:
                    raise primary_error
                self.store.add_event(task_id, "backend_fallback", TaskState.WORKING.value, "App Server to CLI")
                result = await self._run_backend(self.backends["cli"], task, message)
            current = self.store.get_task(task_id)
            if current and current.state not in TERMINAL_STATES:
                current = self.store.update_task(
                    task_id,
                    state=result.state,
                    result_text=result.text,
                    error_code=result.error_code or "",
                    error_message=result.error_message or "",
                    codex_turn_id=result.turn_id,
                    execution_metadata=current.execution_metadata,
                )
                await self._notify(current)
        except asyncio.CancelledError:
            raise
        except BridgeError as exc:
            current = self.store.get_task(task_id)
            if current and current.state not in TERMINAL_STATES:
                current = self.store.update_task(
                    task_id,
                    state=TaskState.FAILED.value,
                    error_code=exc.code,
                    error_message=exc.message,
                    execution_metadata=current.execution_metadata,
                )
                await self._notify(current)
        except Exception as exc:
            current = self.store.get_task(task_id)
            if current and current.state not in TERMINAL_STATES:
                current = self.store.update_task(
                    task_id,
                    state=TaskState.FAILED.value,
                    error_code="backend_internal",
                    error_message=f"Codex backend failed: {type(exc).__name__}",
                    execution_metadata=current.execution_metadata,
                )
                await self._notify(current)

    async def wait(self, task_id: str, timeout: float | None = None) -> TaskRecord:
        task = self.store.get_task(task_id)
        if not task or task.direction != "inbound":
            raise BridgeError("task_not_found", "inbound task not found")
        worker = self._workers.get(task.bridge_task_id)
        if worker:
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(worker), timeout=timeout or self.settings.codex_timeout)
        return self.store.get_task(task.bridge_task_id) or task

    async def updates(
        self,
        task: TaskRecord,
        subscriber: asyncio.Queue[TaskRecord] | None = None,
    ) -> AsyncIterator[TaskRecord]:
        queue = subscriber if subscriber is not None else self.create_subscription()
        subscribers = self._subscribers.setdefault(task.bridge_task_id, set())
        subscribers.add(queue)
        try:
            initial = task if subscriber is not None else self.store.get_task(task.bridge_task_id) or task
            yield initial
            if initial.state in TURN_END_STATES:
                return
            while True:
                updated = await queue.get()
                yield updated
                if updated.state in TURN_END_STATES:
                    return
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(task.bridge_task_id, None)

    def get_task(self, task_id: str) -> TaskRecord:
        task = self.store.get_task(task_id)
        if not task or task.direction != "inbound":
            raise BridgeError("task_not_found", "inbound task not found")
        return task

    @staticmethod
    def _encode_page_token(task: TaskRecord) -> str:
        raw = json.dumps([task.updated_at, task.bridge_task_id], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_page_token(token: str) -> tuple[str, str] | None:
        if not token:
            return None
        try:
            padded = token + "=" * (-len(token) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("invalid_page_token", "pageToken is invalid") from exc
        if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, str) for item in value):
            raise BridgeError("invalid_page_token", "pageToken is invalid")
        return value[0], value[1]

    def list_tasks(
        self,
        *,
        context_id: str | None,
        status: str | None,
        page_size: int,
        page_token: str,
        history_length: int | None,
        include_artifacts: bool,
        updated_after: str | None,
    ) -> dict[str, Any]:
        internal_states: tuple[str, ...] | None = None
        if status and status != "TASK_STATE_UNSPECIFIED":
            internal_states = INTERNAL_STATES.get(status)
            if not internal_states:
                raise BridgeError("invalid_status", "status is not a supported A2A task state")
        if history_length is not None and history_length < 0:
            raise BridgeError("invalid_history_length", "historyLength must be zero or greater")
        if updated_after:
            try:
                parsed = datetime.fromisoformat(updated_after.replace("Z", "+00:00"))
            except ValueError as exc:
                raise BridgeError("invalid_timestamp", "statusTimestampAfter must be an RFC 3339 timestamp") from exc
            if parsed.tzinfo is None:
                raise BridgeError("invalid_timestamp", "statusTimestampAfter must include a timezone")
            updated_after = parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        tasks, total, has_more = self.store.list_tasks_page(
            context_id=context_id,
            states=internal_states,
            updated_after=updated_after,
            cursor=self._decode_page_token(page_token),
            page_size=page_size,
        )
        return {
            "tasks": [
                self.task_payload(task, include_artifacts=include_artifacts, history_length=history_length)
                for task in tasks
            ],
            "nextPageToken": self._encode_page_token(tasks[-1]) if has_more and tasks else "",
            "pageSize": page_size,
            "totalSize": total,
        }

    async def cancel(self, task_id: str) -> tuple[TaskRecord, bool]:
        task = self.get_task(task_id)
        if task.state in TERMINAL_STATES:
            raise BridgeError("task_not_cancelable", "task is already in a terminal state")
        task = self.store.update_task(task.bridge_task_id, cancel_requested=True)
        if task.state in TERMINAL_STATES:
            raise BridgeError("task_not_cancelable", "task reached a terminal state before cancellation")
        context = self.store.get_context(context_id=task.context_id)
        backend_name = context.backend if context and context.backend else self.settings.backend
        backend = self.backends[backend_name]
        sent = await backend.cancel(
            task.bridge_task_id,
            context.codex_thread_id if context else None,
            task.codex_turn_id,
        )
        latest = self.store.get_task(task.bridge_task_id) or task
        if latest.state not in TERMINAL_STATES:
            latest = self.store.update_task(task.bridge_task_id, state=TaskState.CANCELED.value)
            await self._notify(latest)
        return latest, sent
