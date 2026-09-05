from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import suppress
from typing import Any, Literal, cast

from . import __version__
from .a2a import A2AClient
from .models import (
    TERMINAL_STATES,
    TURN_END_STATES,
    A2AError,
    A2ATaskResult,
    BridgeError,
    TaskRecord,
    TaskState,
    now_iso,
    request_fingerprint,
    result_receipt,
)
from .recovery import ConversationRecovery
from .settings import Settings
from .store import Store


class BridgeService:
    """Durable conversation/task orchestration over Hermes A2A."""

    def __init__(self, settings: Settings, *, store: Store | None = None, client: A2AClient | None = None):
        self.settings = settings
        self.store = store or Store(settings.state_path)
        self.client = client or A2AClient(settings)
        self.conversation_recovery = ConversationRecovery(settings.conversation_dir)
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._context_locks: dict[str, asyncio.Lock] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._changed: dict[str, asyncio.Event] = {}
        self.store.mark_interrupted_outbound()

    async def aclose(self) -> None:
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        await self.client.aclose()

    @staticmethod
    def _card_summary(card: dict[str, Any]) -> dict[str, Any]:
        caps = card.get("capabilities") or {}
        return {
            "name": card.get("name"),
            "version": card.get("version"),
            "description": card.get("description"),
            "streaming": bool(caps.get("streaming")),
            "push_notifications": bool(caps.get("pushNotifications")),
            "interfaces": [
                {"binding": item.get("protocolBinding"), "url": item.get("url")}
                for item in card.get("supportedInterfaces") or []
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _task_result(task: TaskRecord, *, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": task.state not in {TaskState.FAILED.value, TaskState.REJECTED.value},
            "bridge_task_id": task.bridge_task_id,
            "a2a_task_id": task.a2a_task_id,
            "conversation_key": task.conversation_key,
            "context_id": task.context_id,
            "profile": task.profile,
            "message_id": task.message_id,
            "artifacts": task.artifacts,
            "origin": task.origin,
            "delivery": "pull",
            "result_id": result_receipt(task),
            "state": task.state,
            "result": task.result_text,
            "needs_input": task.state == TaskState.INPUT_REQUIRED.value,
            "cancel_requested": task.cancel_requested,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
        if task.error_code:
            payload["error"] = {"code": task.error_code, "message": task.error_message or ""}
        if events is not None:
            payload["events"] = events
        return payload

    def _signal(self, bridge_task_id: str) -> None:
        event = self._changed.get(bridge_task_id)
        if event:
            event.set()

    def _apply_remote(self, bridge_task_id: str, result: A2ATaskResult, *, from_submission: bool = False) -> TaskRecord:
        current = self.store.get_task(bridge_task_id)
        if not current:
            raise BridgeError("task_not_found", "bridge task not found")
        if (result.context_id and result.context_id != current.context_id) or (
            current.a2a_task_id and result.task_id != current.a2a_task_id
        ):
            raise BridgeError("correlation_mismatch", "remote result does not match the saved task/context")
        metadata = result.raw.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise BridgeError("correlation_mismatch", "remote metadata is invalid")
        if metadata.get("requestMessageId") and metadata["requestMessageId"] != current.message_id:
            raise BridgeError("correlation_mismatch", "remote result belongs to a different request")
        if (
            current.attempt_number > 1
            and not from_submission
            and metadata.get("requestMessageId") != current.message_id
        ):
            raise BridgeError("correlation_mismatch", "continuation retrieval requires exact requestMessageId")
        if current.state in TERMINAL_STATES:
            return current
        if result.task_id:
            bound = self.store.get_task(result.task_id)
            if bound and bound.bridge_task_id != current.bridge_task_id:
                raise BridgeError("correlation_mismatch", "remote task is already bound to another request")
        text = result.text or current.result_text
        artifacts = result.artifacts or current.artifacts
        updated = self.store.update_task(
            bridge_task_id,
            state=result.state,
            a2a_task_id=result.task_id or None,
            result_text=text,
            artifacts=artifacts,
            error_code="",
            error_message="",
        )
        self._signal(bridge_task_id)
        return updated

    def _apply_error(self, bridge_task_id: str, exc: BridgeError) -> TaskRecord:
        state = (
            TaskState.OUTCOME_UNKNOWN.value
            if exc.code
            in {"a2a_transport_ambiguous", "a2a_timeout", "a2a_invalid_sse", "correlation_mismatch", "bridge_internal"}
            else TaskState.FAILED.value
        )
        updated = self.store.update_task(
            bridge_task_id,
            state=state,
            error_code=exc.code,
            error_message=exc.message,
        )
        self._signal(bridge_task_id)
        return updated

    async def status(self) -> dict[str, Any]:
        card: dict[str, Any] | None = None
        health: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        try:
            card, health = await asyncio.gather(self.client.discover(refresh=True), self.client.health())
        except BridgeError as exc:
            error = exc.as_result()["error"]
        return {
            "ok": error is None,
            "bridge": {"version": __version__, "transport": "stdio", "state": self.store.counts()},
            "hermes": {
                "endpoint": self.settings.endpoint,
                "reachable": error is None,
                "health": health,
                "agent_card": self._card_summary(card) if card else None,
            },
            "error": error,
        }

    async def chat(
        self,
        message: str,
        *,
        conversation_key: str | None = None,
        context_id: str | None = None,
        profile: str = "default",
        mode: str = "auto",
        timeout: float | None = None,
        idempotency_key: str | None = None,
        origin: dict[str, str] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        if os.environ.get("CODEX_A2A_GATEWAY_WORKER_TASK_ID"):
            raise BridgeError(
                "delegation_cycle", "an inbound Codex worker must return to Hermes, not delegate back to it"
            )
        message = message.strip()
        if not message:
            raise BridgeError("invalid_message", "message must not be empty")
        if len(message) > self.settings.max_message_chars:
            raise BridgeError("message_too_large", f"message exceeds {self.settings.max_message_chars} characters")
        if profile != "default":
            raise BridgeError("unsupported_profile", "v0.1 routes only to the active Hermes default profile")
        if mode not in {"auto", "sync", "async"}:
            raise BridgeError("invalid_mode", "mode must be auto, sync, or async")
        actual_timeout = min(float(timeout or self.settings.default_timeout), 300.0)
        if actual_timeout < 1:
            raise BridgeError("invalid_timeout", "timeout must be at least one second")
        origin = origin or {}
        if set(origin) - {"conversation_id", "question_id", "parent_job_id"} or any(
            not isinstance(value, str) or len(value) > 256 for value in origin.values()
        ):
            raise BridgeError(
                "invalid_origin", "origin accepts short conversation_id, question_id and parent_job_id handles"
            )
        previous = self.store.get_task_by_idempotency(idempotency_key) if idempotency_key else None
        if previous:
            if (
                previous.direction != "outbound"
                or previous.request_fingerprint != request_fingerprint(message, previous.context_id, profile)
                or (context_id and context_id != previous.context_id)
                or (conversation_key and conversation_key != previous.conversation_key)
                or (task_id and task_id not in {previous.bridge_task_id, previous.a2a_task_id})
                or (origin and previous.origin != origin)
            ):
                raise BridgeError("idempotency_conflict", "idempotency_key was already used for a different request")
            return {**self._task_result(previous), "deduplicated": True}
        continued = self.store.get_task(task_id) if task_id else None
        if task_id:
            if (
                not continued
                or continued.direction != "outbound"
                or continued.state != "input_required"
                or not continued.a2a_task_id
            ):
                raise BridgeError("invalid_task_state", "task_id must identify an input-required outbound task")
            if (context_id and context_id != continued.context_id) or (
                conversation_key and conversation_key != continued.conversation_key
            ):
                raise BridgeError("context_conflict", "continuation must use the saved context")
            if origin and origin != continued.origin:
                raise BridgeError("origin_mismatch", "continuation must retain its originating job")
            context_id, conversation_key = continued.context_id, continued.conversation_key
        mapped = self.store.get_context(context_id=context_id) if context_id else None
        conversation_key = (
            conversation_key or (mapped.conversation_key if mapped else None) or f"conversation-{uuid.uuid4().hex}"
        ).strip()
        if len(conversation_key) > 256:
            raise BridgeError("invalid_conversation_key", "conversation_key is too long")

        context = self.store.get_or_create_context(
            conversation_key=conversation_key,
            context_id=context_id,
            profile=profile,
            endpoint=self.settings.endpoint + "/",
            tenant="",
        )
        if context.direction != "outbound":
            raise BridgeError("context_conflict", "context belongs to the inbound worker")
        fingerprint = request_fingerprint(message, context.context_id, profile)
        if idempotency_key:
            existing = self.store.get_task_by_idempotency(idempotency_key)
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise BridgeError(
                        "idempotency_conflict", "idempotency_key was already used for a different request"
                    )
                return {**self._task_result(existing), "deduplicated": True}

        active = self.store.active_context_task(context.context_id)
        if active:
            raise BridgeError(
                "context_busy", "context has an unresolved task; get/wait that handle or use a new context"
            )
        if not continued and context.last_task_id:
            last = self.store.get_task(context.last_task_id)
            if last and last.state == "input_required":
                raise BridgeError("task_id_required", "continue input-required work with its explicit task_id")
        self.store.increment_turn(context.context_id, self.settings.max_turns)
        now = now_iso()
        task = TaskRecord(
            bridge_task_id=f"bt-{uuid.uuid4().hex}",
            context_id=context.context_id,
            conversation_key=conversation_key,
            profile=profile,
            endpoint=self.settings.endpoint + "/",
            request_id=f"request-{uuid.uuid4().hex}",
            message_id=f"message-{uuid.uuid4().hex}",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            origin=origin,
            mode=cast(Literal["auto", "sync", "async"], mode),
            created_at=now,
            updated_at=now,
        )
        if continued:
            task = self.store.continue_outbound(continued.bridge_task_id, task)
        else:
            self.store.create_task(task)
        self._changed[task.bridge_task_id] = asyncio.Event()

        worker = asyncio.create_task(
            self._stream_worker(
                task.bridge_task_id,
                message,
                max(actual_timeout, self.settings.correlation_timeout),
            ),
            name=f"hermes-{task.bridge_task_id}",
        )
        self._workers[task.bridge_task_id] = worker

        def drop_worker(_done: asyncio.Task[None], key: str = task.bridge_task_id) -> None:
            self._workers.pop(key, None)

        worker.add_done_callback(drop_worker)

        if mode == "sync":
            wait_for = min(actual_timeout, self.settings.sync_wait)
        elif mode == "auto":
            wait_for = min(self.settings.auto_wait, actual_timeout)
        else:
            wait_for = 0.0
        with suppress(asyncio.TimeoutError):
            if mode in {"sync", "auto"}:
                await asyncio.wait_for(asyncio.shield(worker), timeout=wait_for)
            else:
                await asyncio.wait_for(self._changed[task.bridge_task_id].wait(), timeout=wait_for)
        current = self.store.get_task(task.bridge_task_id)
        assert current is not None
        return {**self._task_result(current), "timed_out": current.state not in TURN_END_STATES}

    async def _stream_worker(self, bridge_task_id: str, message: str, timeout: float) -> None:
        task = self.store.get_task(bridge_task_id)
        if not task:
            return
        try:
            lock = self._context_locks.setdefault(task.context_id, asyncio.Lock())
            async with lock, self._semaphore:
                await self.client.discover()
                self.store.update_task(bridge_task_id, state=TaskState.SUBMITTED.value)
                kwargs: dict[str, Any] = {"timeout": timeout}
                if task.origin:
                    kwargs["origin"] = task.origin
                if task.a2a_task_id:
                    kwargs["task_id"] = task.a2a_task_id
                async for event in self.client.stream_message(message, task.context_id, task.message_id, **kwargs):
                    parsed = self.client.parse_stream_event(event, fallback_context=task.context_id)
                    if parsed:
                        self._apply_remote(bridge_task_id, parsed, from_submission=True)
                        if parsed.state in TURN_END_STATES:
                            break
                current = self.store.get_task(bridge_task_id)
                if current and current.state not in TURN_END_STATES:
                    self._apply_error(
                        bridge_task_id, BridgeError("a2a_transport_ambiguous", "stream ended before a turn result")
                    )
        except asyncio.CancelledError:
            raise
        except BridgeError as exc:
            current = self.store.get_task(bridge_task_id)
            if current and current.state not in TERMINAL_STATES:
                self._apply_error(bridge_task_id, exc)
        except Exception as exc:
            current = self.store.get_task(bridge_task_id)
            if current and current.state not in TERMINAL_STATES:
                self._apply_error(
                    bridge_task_id,
                    BridgeError("bridge_internal", f"stream worker failed: {type(exc).__name__}"),
                )
        finally:
            self._signal(bridge_task_id)

    async def _recover_unknown(self, task: TaskRecord) -> tuple[TaskRecord, str | None]:
        if task.state != TaskState.OUTCOME_UNKNOWN.value:
            return task, None
        candidates: dict[str, A2ATaskResult] = {}
        page_token = ""
        seen: set[str] = set()
        try:
            for _ in range(100):
                kwargs: dict[str, Any] = {"context_id": task.context_id, "limit": 100}
                if page_token:
                    kwargs["page_token"] = page_token
                listed = await self.client.list_tasks(**kwargs)
                for raw in listed.get("tasks") or []:
                    if not isinstance(raw, dict):
                        continue
                    parsed = self.client.parse_task(raw)
                    metadata = raw.get("metadata") or {}
                    if not isinstance(metadata, dict):
                        continue
                    if (
                        parsed.context_id == task.context_id
                        and parsed.task_id
                        and metadata.get("requestMessageId") == task.message_id
                        and (not task.a2a_task_id or task.a2a_task_id == parsed.task_id)
                    ):
                        candidates[parsed.task_id] = parsed
                page_token = str(listed.get("nextPageToken") or "")
                if not page_token:
                    break
                if page_token in seen:
                    return task, None
                seen.add(page_token)
            else:
                return task, None
            if len(candidates) == 1:
                recovered = self._apply_remote(task.bridge_task_id, next(iter(candidates.values())))
                return recovered, "a2a_list_exact_message"
        except BridgeError:
            pass
        recovered_result = self.conversation_recovery.recover(task, assigned_task_ids=set(), unresolved_count=1)
        if recovered_result:
            try:
                return self._apply_remote(task.bridge_task_id, recovered_result), "conversation_store"
            except BridgeError:
                pass
        return task, None

    async def task_get(
        self,
        task_id: str,
        *,
        refresh: bool = True,
        acknowledge_result_id: str | None = None,
        expected_origin: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task or task.direction != "outbound":
            raise BridgeError("task_not_found", "outbound bridge task not found")
        if expected_origin is not None and expected_origin != task.origin:
            raise BridgeError("origin_mismatch", "task belongs to a different originating question/conversation")
        if acknowledge_result_id:
            self.store.acknowledge_result(task, acknowledge_result_id)
        recovery_strategy: str | None = None
        if refresh and task.state == TaskState.OUTCOME_UNKNOWN.value and not task.a2a_task_id:
            task, recovery_strategy = await self._recover_unknown(task)
        if refresh and task.a2a_task_id and task.state not in TERMINAL_STATES:
            try:
                task = self._apply_remote(task.bridge_task_id, await self.client.get_task(task.a2a_task_id))
            except BridgeError as exc:
                if exc.code != "a2a_task_not_found":
                    return {**self._task_result(task), "refresh_warning": exc.message}
                task = self.store.update_task(
                    task.bridge_task_id, state="outcome_unknown", error_code="a2a_task_not_found"
                )
                task, recovery_strategy = await self._recover_unknown(task)
        result = self._task_result(task, events=self.store.list_events(task.bridge_task_id, 20))
        result["acknowledged"] = self.store.result_acknowledged(result_receipt(task))
        if recovery_strategy:
            result["recovery_strategy"] = recovery_strategy
            if recovery_strategy == "conversation_store":
                result["recovery_warning"] = (
                    "Recovered from peer persistence with an exact message ID and explicit task state."
                )
        return result

    async def tasks_list(
        self, *, conversation_key: str | None = None, status: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        if status and status not in {item.value for item in TaskState}:
            raise BridgeError("invalid_status", "unknown bridge task status")
        tasks = self.store.list_tasks(
            conversation_key=conversation_key, state=status, direction="outbound", limit=limit
        )
        return {"ok": True, "tasks": [self._task_result(task) for task in tasks], "count": len(tasks)}

    async def task_wait(self, task_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
        timeout = max(1.0, min(timeout, 300.0))
        try:
            async with asyncio.timeout(timeout):
                return await self._task_wait(task_id, timeout=timeout)
        except TimeoutError:
            task = self.store.get_task(task_id)
            if not task or task.direction != "outbound":
                raise BridgeError("task_not_found", "outbound bridge task not found") from None
            return {
                **self._task_result(task),
                "timed_out": task.state not in TURN_END_STATES,
                "wait_strategy": "wait_budget_expired",
            }

    async def _task_wait(self, task_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task or task.direction != "outbound":
            raise BridgeError("task_not_found", "outbound bridge task not found")
        timeout = max(1.0, min(timeout, 300.0))
        if task.state in TURN_END_STATES:
            return {**self._task_result(task), "wait_strategy": "already_terminal"}

        worker = self._workers.get(task.bridge_task_id)
        if worker:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(worker), timeout=timeout)
            task = self.store.get_task(task.bridge_task_id) or task
            return {
                **self._task_result(task),
                "wait_strategy": "active_stream",
                "timed_out": task.state not in TURN_END_STATES,
            }

        strategy = "poll"
        deadline = asyncio.get_running_loop().time() + timeout
        if task.state == TaskState.OUTCOME_UNKNOWN.value and not task.a2a_task_id:
            strategy = "correlation_recovery"
            while asyncio.get_running_loop().time() < deadline:
                task, recovered_by = await self._recover_unknown(task)
                if recovered_by:
                    strategy = recovered_by
                if task.state in TURN_END_STATES:
                    result = {**self._task_result(task), "wait_strategy": strategy}
                    if strategy == "conversation_store":
                        result["recovery_warning"] = (
                            "Recovered from peer persistence with an exact message ID and explicit task state."
                        )
                    return result
                if task.a2a_task_id:
                    break
                await asyncio.sleep(min(1.0, max(0.05, deadline - asyncio.get_running_loop().time())))
        a2a_task_id = task.a2a_task_id
        if a2a_task_id:
            try:
                strategy = "subscribe"
                remaining = max(0.05, deadline - asyncio.get_running_loop().time())
                async for event in self.client.subscribe_task(a2a_task_id, timeout=remaining):
                    parsed = self.client.parse_stream_event(event, fallback_context=task.context_id)
                    if parsed:
                        task = self._apply_remote(task.bridge_task_id, parsed)
                        if task.state in TURN_END_STATES:
                            return {**self._task_result(task), "wait_strategy": strategy}
            except A2AError:
                strategy = "poll_fallback"

            while asyncio.get_running_loop().time() < deadline:
                try:
                    task = self._apply_remote(task.bridge_task_id, await self.client.get_task(a2a_task_id))
                except A2AError as exc:
                    if exc.code == "a2a_task_not_found":
                        break
                if task.state in TURN_END_STATES:
                    break
                await asyncio.sleep(min(0.5, max(0.05, deadline - asyncio.get_running_loop().time())))
        return {**self._task_result(task), "wait_strategy": strategy, "timed_out": task.state not in TURN_END_STATES}

    async def task_cancel(self, task_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task or task.direction != "outbound":
            raise BridgeError("task_not_found", "outbound bridge task not found")
        task = self.store.update_task(task.bridge_task_id, cancel_requested=True)
        if task.state in TURN_END_STATES:
            return {
                **self._task_result(task),
                "cancel_sent": False,
                "computation_stopped": "unknown",
                "note": "Task was already terminal; no claim is made that Hermes computation was interrupted.",
            }
        if not task.a2a_task_id:
            return {
                **self._task_result(task),
                "cancel_sent": False,
                "computation_stopped": "unknown",
                "note": "Cancel recorded locally; no Hermes task id was known yet.",
            }
        try:
            remote = await self.client.cancel_task(task.a2a_task_id, timeout=timeout)
            task = self._apply_remote(task.bridge_task_id, remote)
            cancel_sent = True
        except BridgeError as exc:
            task = self.store.get_task(task.bridge_task_id) or task
            return {
                **self._task_result(task),
                "cancel_sent": False,
                "computation_stopped": "unknown",
                "cancel_error": {"code": exc.code, "message": exc.message},
                "note": "Cancellation is best-effort; Hermes may still be computing.",
            }
        return {
            **self._task_result(task),
            "cancel_sent": cancel_sent,
            "computation_stopped": "unknown",
            "note": "Hermes acknowledged task cancellation; A2A cancellation does not guarantee the live turn stopped.",
        }

    async def contexts(
        self,
        *,
        action: str = "list",
        conversation_key: str | None = None,
        context_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if action == "list":
            rows = self.store.list_contexts(limit=limit, conversation_key=conversation_key)
            return {"ok": True, "contexts": [row.model_dump() for row in rows], "count": len(rows)}
        if action == "inspect":
            row = self.store.get_context(context_id=context_id, conversation_key=conversation_key)
            if not row:
                raise BridgeError("context_not_found", "context mapping not found")
            tasks = self.store.list_tasks(conversation_key=row.conversation_key, limit=limit)
            return {"ok": True, "context": row.model_dump(), "tasks": [self._task_result(task) for task in tasks]}
        if action == "close":
            row = self.store.close_context(context_id=context_id, conversation_key=conversation_key)
            return {
                "ok": True,
                "context": row.model_dump(),
                "note": "Only the bridge mapping was closed; Hermes data was not deleted.",
            }
        raise BridgeError("invalid_action", "action must be list, inspect, or close")
