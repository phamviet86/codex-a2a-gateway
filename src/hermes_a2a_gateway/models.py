"""A2A peer task shapes shared by the broker and its transport."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


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


class A2AError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, rpc_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.rpc_code = rpc_code


class A2ATaskResult(BaseModel):
    task_id: str
    context_id: str
    state: str
    text: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)
