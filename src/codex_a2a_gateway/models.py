from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def request_fingerprint(message: str, context_id: str, profile: str) -> str:
    body = json.dumps(
        {"message": message, "context_id": context_id, "profile": profile},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class TaskState(StrEnum):
    QUEUED = "queued"
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"
    OUTCOME_UNKNOWN = "outcome_unknown"


TERMINAL_STATES = {
    TaskState.COMPLETED.value,
    TaskState.FAILED.value,
    TaskState.CANCELED.value,
    TaskState.REJECTED.value,
}

TURN_END_STATES = TERMINAL_STATES | {TaskState.INPUT_REQUIRED.value}


A2A_STATE_MAP = {
    "TASK_STATE_SUBMITTED": TaskState.SUBMITTED.value,
    "TASK_STATE_WORKING": TaskState.WORKING.value,
    "TASK_STATE_INPUT_REQUIRED": TaskState.INPUT_REQUIRED.value,
    "TASK_STATE_AUTH_REQUIRED": TaskState.INPUT_REQUIRED.value,
    "TASK_STATE_COMPLETED": TaskState.COMPLETED.value,
    "TASK_STATE_FAILED": TaskState.FAILED.value,
    "TASK_STATE_CANCELED": TaskState.CANCELED.value,
    "TASK_STATE_REJECTED": TaskState.REJECTED.value,
}


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    TaskState.QUEUED.value: {
        TaskState.SUBMITTED.value,
        TaskState.WORKING.value,
        TaskState.INPUT_REQUIRED.value,
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
        TaskState.CANCELED.value,
        TaskState.REJECTED.value,
        TaskState.OUTCOME_UNKNOWN.value,
    },
    TaskState.SUBMITTED.value: {
        TaskState.WORKING.value,
        TaskState.INPUT_REQUIRED.value,
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
        TaskState.CANCELED.value,
        TaskState.REJECTED.value,
        TaskState.OUTCOME_UNKNOWN.value,
    },
    TaskState.WORKING.value: {
        TaskState.INPUT_REQUIRED.value,
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
        TaskState.CANCELED.value,
        TaskState.REJECTED.value,
        TaskState.OUTCOME_UNKNOWN.value,
    },
    TaskState.OUTCOME_UNKNOWN.value: {
        TaskState.SUBMITTED.value,
        TaskState.WORKING.value,
        TaskState.INPUT_REQUIRED.value,
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
        TaskState.CANCELED.value,
        TaskState.REJECTED.value,
    },
    TaskState.INPUT_REQUIRED.value: {
        TaskState.QUEUED.value,
        TaskState.WORKING.value,
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
        TaskState.CANCELED.value,
        TaskState.REJECTED.value,
    },
}


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_result(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message}, "retryable": self.retryable}


class A2AError(BridgeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, rpc_code: int | None = None):
        super().__init__(code, message, retryable=retryable)
        self.rpc_code = rpc_code


class ContextRecord(BaseModel):
    context_id: str
    conversation_key: str
    profile: str = "default"
    endpoint: str
    tenant: str = ""
    status: Literal["open", "closed"] = "open"
    turn_count: int = 0
    last_task_id: str | None = None
    created_at: str
    updated_at: str
    closed_at: str | None = None
    direction: Literal["outbound", "inbound"] = "outbound"
    codex_thread_id: str | None = None
    backend: str | None = None


class TaskRecord(BaseModel):
    bridge_task_id: str
    a2a_task_id: str | None = None
    context_id: str
    conversation_key: str
    profile: str = "default"
    endpoint: str
    request_id: str
    message_id: str
    idempotency_key: str | None = None
    request_fingerprint: str
    mode: Literal["auto", "sync", "async"]
    state: str = TaskState.QUEUED.value
    result_text: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    cancel_requested: bool = False
    hop_count: int = 0
    created_at: str
    updated_at: str
    completed_at: str | None = None
    direction: Literal["outbound", "inbound"] = "outbound"
    codex_turn_id: str | None = None
    # Inbound-only, receiver-controlled execution preference decision.  This
    # deliberately excludes prompt content and is persisted so a task handle
    # remains explainable after a restart.
    execution_metadata: dict[str, Any] = Field(default_factory=dict)


class A2ATaskResult(BaseModel):
    task_id: str
    context_id: str
    state: str
    text: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)
