from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from codex_a2a_gateway import __version__
from codex_a2a_gateway.codex_backend import AppServerBackend, CLIBackend
from codex_a2a_gateway.models import BridgeError


class FakeStdin:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            if line.startswith("{"):
                self.frames.append(json.loads(line))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeProcess:
    def __init__(self, frames: list[dict[str, Any]], exit_code: int = 0) -> None:
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        for frame in frames:
            self.stdout.feed_data((json.dumps(frame) + "\n").encode())
        self.stdout.feed_eof()
        self.returncode: int | None = None
        self._exit_code = exit_code

    def terminate(self) -> None:
        self.returncode = self._exit_code

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode


@pytest.mark.asyncio
async def test_app_server_stdio_protocol(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = FakeProcess(
        [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            {"id": 3, "result": {"turn": {"id": "turn-1", "status": "inProgress"}}},
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "i", "delta": "hel"},
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "agentMessage", "text": "hello"},
                },
            },
            {
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
            },
        ]
    )

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    started: list[tuple[str, str | None]] = []
    updates: list[str] = []
    backend = AppServerBackend(codex_bin="codex", workspace=tmp_path, timeout=2, approval_policy="never")
    result = await backend.run(
        task_id="task-1",
        prompt="hello",
        thread_id=None,
        message_id="message-1",
        on_started=lambda thread, turn: _append_started(started, thread, turn),
        on_update=lambda text, append: _append_update(updates, text, append),
    )
    assert result.state == "completed" and result.text == "hello"
    assert started[-1] == ("thread-1", "turn-1")
    assert [frame.get("method") for frame in process.stdin.frames] == [
        "initialize",
        "initialized",
        "thread/start",
        "turn/start",
    ]
    assert process.stdin.frames[0]["params"]["clientInfo"]["version"] == __version__


@pytest.mark.asyncio
async def test_app_server_exact_request_user_input_method(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = FakeProcess(
        [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-input"}}},
            {"id": 3, "result": {"turn": {"id": "turn-input", "status": "inProgress"}}},
            {
                "id": 99,
                "method": "item/tool/requestUserInput",
                "params": {"threadId": "thread-input", "turnId": "turn-input", "questions": []},
            },
        ]
    )

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    backend = AppServerBackend(codex_bin="codex", workspace=tmp_path, timeout=2, approval_policy="never")
    result = await backend.run(
        task_id="task-input",
        prompt="ask",
        thread_id=None,
        message_id="message-input",
        on_started=lambda thread, turn: _append_started([], thread, turn),
        on_update=lambda text, append: _append_update([], text, append),
    )
    response = next(frame for frame in process.stdin.frames if frame.get("id") == 99)
    assert response == {"id": 99, "result": {"answers": {}}}
    assert result.state == "input_required" and result.thread_id == "thread-input"


@pytest.mark.asyncio
async def test_app_server_model_list_validates_and_sends_model_and_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = FakeProcess(
        [
            {"id": 1, "result": {}},
            {
                "id": 2,
                "result": {
                    "data": [
                        {
                            "id": "gpt-test",
                            "model": "gpt-test",
                            "isDefault": True,
                            "defaultReasoningEffort": "medium",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "medium"},
                                {"reasoningEffort": "high"},
                            ],
                        }
                    ]
                },
            },
            {"id": 3, "result": {"thread": {"id": "thread-model"}}},
            {"id": 4, "result": {"turn": {"id": "turn-model", "status": "inProgress"}}},
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-model", "status": "completed"}},
            },
        ]
    )

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    preferences: dict[str, Any] = {"requested": {"model": "gpt-test", "reasoningEffort": "high", "requireExact": True}}
    persisted: list[dict[str, Any]] = []
    backend = AppServerBackend(codex_bin="codex", workspace=tmp_path, timeout=2, approval_policy="never")
    await backend.run(
        task_id="task-model",
        prompt="hello",
        thread_id=None,
        message_id="message-model",
        execution_preferences=preferences,
        on_started=lambda thread, turn: _append_started([], thread, turn),
        on_update=lambda text, append: _append_update([], text, append),
        on_execution_decision=lambda decision: _append_decision(persisted, decision),
    )
    turn = next(frame for frame in process.stdin.frames if frame.get("method") == "turn/start")
    assert turn["params"]["model"] == "gpt-test" and turn["params"]["effort"] == "high"
    assert preferences["decision"]["fallbackApplied"] is False
    assert persisted == [preferences]


def test_receiver_defaults_select_from_live_catalog(tmp_path: Path) -> None:
    backend = AppServerBackend(
        codex_bin="codex",
        workspace=tmp_path,
        timeout=2,
        approval_policy="never",
        default_model="gpt-test",
        default_reasoning_effort="high",
    )
    preferences: dict[str, Any] = {}
    selected = backend._choose_execution_preferences(
        [
            {
                "id": "gpt-test",
                "model": "gpt-test",
                "isDefault": True,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}],
            }
        ],
        preferences,
    )
    assert selected == {"model": "gpt-test", "effort": "high"}
    assert preferences["decision"]["source"] == "receiver-default"


def test_exact_unavailable_preference_is_recorded_before_rejection(tmp_path: Path) -> None:
    backend = AppServerBackend(codex_bin="codex", workspace=tmp_path, timeout=2, approval_policy="never")
    preferences: dict[str, Any] = {
        "requested": {"model": "not-in-catalog", "reasoningEffort": "high", "requireExact": True}
    }

    with pytest.raises(BridgeError, match="Requested Codex model"):
        backend._choose_execution_preferences(
            [
                {
                    "id": "gpt-test",
                    "model": "gpt-test",
                    "isDefault": True,
                    "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
                }
            ],
            preferences,
        )

    assert preferences["decision"] == {
        "requested": preferences["requested"],
        "effective": {"model": None, "reasoningEffort": None},
        "fallbackApplied": False,
        "backend": "app-server",
        "rejected": True,
        "reason": "requested_model_unavailable",
    }


@pytest.mark.asyncio
async def test_cli_jsonl_fallback_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = FakeProcess(
        [
            {"type": "thread.started", "thread_id": "cli-thread"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed"},
        ]
    )

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    started: list[tuple[str, str | None]] = []
    updates: list[str] = []
    backend = CLIBackend(codex_bin="codex", workspace=tmp_path, timeout=2)
    result = await backend.run(
        task_id="task-cli",
        prompt="work",
        thread_id=None,
        message_id="message-cli",
        on_started=lambda thread, turn: _append_started(started, thread, turn),
        on_update=lambda text, append: _append_update(updates, text, append),
    )
    assert result.state == "completed" and result.thread_id == "cli-thread" and result.text == "done"
    assert backend.supports_input_required is False


async def _append_started(target: list[tuple[str, str | None]], thread: str, turn: str | None) -> None:
    target.append((thread, turn))


async def _append_update(target: list[str], text: str, append: bool) -> None:
    del append
    target.append(text)


async def _append_decision(target: list[dict[str, Any]], decision: dict[str, Any]) -> None:
    target.append(decision)
