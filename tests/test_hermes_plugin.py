from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

from codex_a2a_gateway import cli
from codex_a2a_gateway.hermes_plugin.asset import register, tools


class FakeState:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value


class FakeContext:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.state = FakeState()
        self.config = config or {}
        self.registered: list[dict[str, Any]] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def register_tool(self, **kwargs: Any) -> None:
        self.registered.append(kwargs)


def test_standalone_plugin_registers_all_declared_tools() -> None:
    ctx = FakeContext()
    register(ctx)
    assert {entry["name"] for entry in ctx.registered} == {
        "codex_a2a_call",
        "codex_a2a_get",
        "codex_a2a_wait",
        "codex_a2a_list",
        "codex_a2a_cancel",
    }
    assert {entry["toolset"] for entry in ctx.registered} == {"codex_a2a"}


def test_plugin_call_submits_early_and_persists_handle(monkeypatch) -> None:
    ctx = FakeContext()
    calls: list[tuple[dict[str, Any], bool]] = []

    def fake_request(
        endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False
    ) -> dict[str, Any]:
        del endpoint, timeout
        calls.append((payload, extension))
        return {
            "result": {
                "task": {
                    "id": "task-1",
                    "contextId": "context-1",
                    "status": {"state": "TASK_STATE_SUBMITTED"},
                    "artifacts": [{"parts": [{"text": "sensitive task result"}]}],
                }
            }
        }

    monkeypatch.setattr(tools, "_request", fake_request)
    monkeypatch.setattr(tools, "_advertises_execution_preferences", lambda endpoint, timeout: True)
    result = json.loads(
        tools._call(
            ctx,
            {"message": "safe", "context_id": "context-1", "model": "gpt-test", "reasoning_effort": "high"},
        )
    )
    assert result["ok"] and result["task"]["id"] == "task-1"
    assert result["handleInfo"]["remote_task_id"] == "task-1"
    assert "task" not in ctx.state.get("handles")[0]
    assert "sensitive task result" not in json.dumps(ctx.state.get("handles"))
    payload, extension = calls[0]
    assert payload["params"]["configuration"] == {"returnImmediately": True}
    assert extension and payload["params"]["message"]["extensions"] == [tools.EXTENSION_URI]
    assert ctx.state.get("handles")[0]["context_id"] == "context-1"


def test_plugin_uses_its_own_configured_loopback_endpoint(monkeypatch) -> None:
    ctx = FakeContext({"endpoint": "http://127.0.0.1:9921", "timeout": 7})
    observed: list[tuple[str, float]] = []

    def fake_request(
        endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False
    ) -> dict[str, Any]:
        del payload, extension
        observed.append((endpoint, timeout))
        return {"result": {"task": {"id": "task-config", "status": {"state": "TASK_STATE_SUBMITTED"}}}}

    monkeypatch.setattr(tools, "_request", fake_request)
    assert json.loads(tools._call(ctx, {"message": "configured"}))["ok"]
    assert observed == [("http://127.0.0.1:9921", 7.0)]


def test_plugin_checks_agent_card_before_sending_execution_preferences(monkeypatch) -> None:
    ctx = FakeContext()
    sends: list[dict[str, Any]] = []
    monkeypatch.setattr(tools, "_advertises_execution_preferences", lambda endpoint, timeout: False)
    monkeypatch.setattr(tools, "_request", lambda endpoint, payload, timeout, **kwargs: sends.append(payload))
    result = json.loads(tools._call(ctx, {"message": "preference", "model": "gpt-test"}))
    assert not result["ok"] and "Agent Card" in result["error"]
    assert sends == [] and ctx.state.get("handles", []) == []


def test_plugin_reads_agent_card_with_no_redirect_opener(monkeypatch) -> None:
    observed: list[Any] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            del args

        def read(self) -> bytes:
            return json.dumps({"capabilities": {"extensions": [{"uri": tools.EXTENSION_URI}]}}).encode()

    class Opener:
        def open(self, request: Any, timeout: float) -> Response:
            observed.extend([request, timeout])
            return Response()

    monkeypatch.setattr(tools, "_NO_REDIRECT_OPENER", Opener())
    assert tools._advertises_execution_preferences("http://127.0.0.1:9910", 5)
    assert observed[0].method == "GET" and observed[0].full_url.endswith("/.well-known/agent-card.json")


def test_plugin_continues_input_required_on_same_handle_without_preferences(monkeypatch) -> None:
    ctx = FakeContext()
    ctx.state.set(
        "handles",
        [
            {
                "handle_id": "local-input",
                "remote_task_id": "remote-input",
                "context_id": "ctx-input",
                "endpoint": "http://127.0.0.1:9910",
                "message_id": "first-message",
                "state": "TASK_STATE_INPUT_REQUIRED",
                "attempt_number": 1,
            }
        ],
    )
    calls: list[dict[str, Any]] = []

    def send_continuation(
        endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False
    ) -> dict[str, Any]:
        del endpoint, timeout, extension
        calls.append(payload)
        persisted = ctx.state.get("handles")[0]
        assert persisted["rpc_id"] == payload["id"] and persisted["attempt_number"] == 2
        return {
            "result": {
                "task": {
                    "id": "remote-input",
                    "contextId": "ctx-input",
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            }
        }

    monkeypatch.setattr(tools, "_request", send_continuation)
    result = json.loads(tools._call(ctx, {"task_id": "local-input", "message": "the answer", "message_id": "answer-2"}))
    assert result["ok"] and result["handle"] == "local-input"
    assert len(ctx.state.get("handles")) == 1 and result["handleInfo"]["attempt_number"] == 2
    sent_message = calls[0]["params"]["message"]
    assert sent_message["taskId"] == "remote-input" and "contextId" not in sent_message

    rejected = json.loads(tools._call(ctx, {"task_id": "local-input", "message": "again", "model": "gpt-test"}))
    assert not rejected["ok"] and "cannot change" in rejected["error"] and len(calls) == 1


def test_plugin_marks_malformed_send_and_get_responses_unknown(monkeypatch) -> None:
    ctx = FakeContext()
    monkeypatch.setattr(tools, "_request", lambda endpoint, payload, timeout, **kwargs: {"result": {}})
    malformed_send = json.loads(tools._call(ctx, {"message": "malformed"}))
    assert malformed_send["state"] == "outcome_unknown"
    handle_id = malformed_send["handle"]

    ctx.state.get("handles")[0].update({"remote_task_id": "remote-malformed", "state": "TASK_STATE_WORKING"})
    malformed_get = json.loads(tools._get_tool(ctx, {"task_id": handle_id}))
    assert malformed_get["state"] == "outcome_unknown"
    assert ctx.state.get("handles")[0]["state"] == "outcome_unknown"


def test_plugin_persists_definite_json_rpc_rejection(monkeypatch) -> None:
    ctx = FakeContext()

    def rejected(endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False) -> dict[str, Any]:
        del endpoint, payload, timeout, extension
        raise tools.GatewayRejection("receiver denied the request")

    monkeypatch.setattr(tools, "_request", rejected)
    result = json.loads(tools._call(ctx, {"message": "denied"}))
    assert not result["ok"] and result["handleInfo"]["state"] == "TASK_STATE_REJECTED"
    assert ctx.state.get("handles")[0]["failure_code"] == "gateway_rejected"


def test_plugin_wait_timeout_marks_unknown_without_resend(monkeypatch) -> None:
    ctx = FakeContext()
    ctx.state.set(
        "handles",
        [
            {
                "handle_id": "local-2",
                "remote_task_id": "task-2",
                "context_id": "context-2",
                "endpoint": "http://127.0.0.1:9910",
                "state": "TASK_STATE_WORKING",
            }
        ],
    )
    methods: list[str] = []
    clock = iter([0.0, 0.0, 0.0, 2.0])

    def fake_request(
        endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False
    ) -> dict[str, Any]:
        del endpoint, timeout, extension
        methods.append(payload["method"])
        return {"result": {"id": "task-2", "contextId": "context-2", "status": {"state": "TASK_STATE_WORKING"}}}

    monkeypatch.setattr(tools, "_request", fake_request)
    monkeypatch.setattr(tools.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(tools.time, "sleep", lambda _seconds: None)
    result = json.loads(tools._wait(ctx, {"task_id": "local-2", "timeout": 1}))
    assert result["state"] == "outcome_unknown"
    assert methods == ["GetTask"]
    assert ctx.state.get("handles")[0]["state"] == "outcome_unknown"


def test_plugin_transport_failures_mark_existing_handles_unknown(monkeypatch) -> None:
    ctx = FakeContext()
    ctx.state.set(
        "handles",
        [
            {
                "handle_id": "local-transport",
                "remote_task_id": "task-transport",
                "context_id": "context-transport",
                "endpoint": "http://127.0.0.1:9910",
                "state": "TASK_STATE_WORKING",
            }
        ],
    )

    def timeout_request(
        endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False
    ) -> dict[str, Any]:
        del endpoint, payload, timeout, extension
        raise TimeoutError("network ended")

    monkeypatch.setattr(tools, "_request", timeout_request)
    assert json.loads(tools._get_tool(ctx, {"task_id": "local-transport"}))["state"] == "outcome_unknown"
    assert ctx.state.get("handles")[0]["state"] == "outcome_unknown"
    ctx.state.get("handles")[0]["state"] = "TASK_STATE_WORKING"
    assert json.loads(tools._cancel(ctx, {"task_id": "local-transport"}))["state"] == "outcome_unknown"
    assert ctx.state.get("handles")[0]["state"] == "outcome_unknown"


def test_plugin_timeout_before_response_recovers_without_second_send(monkeypatch) -> None:
    ctx = FakeContext()
    sent_methods: list[str] = []

    def timeout_request(
        endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False
    ) -> dict[str, Any]:
        del endpoint, timeout, extension
        sent_methods.append(payload["method"])
        raise TimeoutError("client stopped waiting")

    monkeypatch.setattr(tools, "_request", timeout_request)
    timed_out = json.loads(tools._call(ctx, {"message": "once", "context_id": "context-timeout"}))
    assert timed_out["handleInfo"]["state"] == "outcome_unknown" and sent_methods == ["SendMessage"]
    handle_id = timed_out["handle"]

    def recovered_request(
        endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False
    ) -> dict[str, Any]:
        del endpoint, timeout, extension
        sent_methods.append(payload["method"])
        return {
            "result": {
                "tasks": [
                    {
                        "id": "remote-recovered",
                        "contextId": "context-timeout",
                        "status": {"state": "TASK_STATE_WORKING"},
                        "metadata": {"requestMessageId": timed_out["handleInfo"]["message_id"]},
                    }
                ]
            }
        }

    monkeypatch.setattr(tools, "_request", recovered_request)
    recovered = json.loads(tools._get_tool(ctx, {"task_id": handle_id}))
    assert recovered["ok"] and recovered["task"]["id"] == "remote-recovered"
    assert sent_methods == ["SendMessage", "ListTasks"]


def test_plugin_unique_list_recovery_refuses_ambiguity(monkeypatch) -> None:
    ctx = FakeContext()
    handle = {
        "handle_id": "local-3",
        "remote_task_id": "stale-remote-id",
        "context_id": "context-3",
        "endpoint": "http://127.0.0.1:9910",
        "message_id": "message-3",
    }

    def one_candidate(
        endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False
    ) -> dict[str, Any]:
        del endpoint, payload, timeout, extension
        return {
            "result": {
                "tasks": [
                    {
                        "id": "recovered",
                        "contextId": "context-3",
                        "status": {"state": "TASK_STATE_WORKING"},
                        "metadata": {"requestMessageId": "message-3"},
                    }
                ]
            }
        }

    monkeypatch.setattr(tools, "_request", one_candidate)
    assert tools._recover_unique(ctx, handle)[0]["remote_task_id"] == "recovered"

    def two_candidates(
        endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False
    ) -> dict[str, Any]:
        del endpoint, payload, timeout, extension
        return {"result": {"tasks": [{"id": "a"}, {"id": "b"}]}}

    monkeypatch.setattr(tools, "_request", two_candidates)
    assert tools._recover_unique(ctx, handle) is None


def test_plugin_recovery_requires_matching_message_id_and_no_result_persistence(monkeypatch) -> None:
    ctx = FakeContext()
    handle = {
        "handle_id": "local-correlated",
        "remote_task_id": "",
        "context_id": "reused-context",
        "endpoint": "http://127.0.0.1:9910",
        "message_id": "original-message",
        "state": "outcome_unknown",
    }

    def unrelated_task(
        endpoint: str, payload: dict[str, Any], timeout: float, *, extension: bool = False
    ) -> dict[str, Any]:
        del endpoint, payload, timeout, extension
        return {
            "result": {
                "tasks": [
                    {
                        "id": "another-task",
                        "contextId": "reused-context",
                        "status": {"state": "TASK_STATE_COMPLETED"},
                        "artifacts": [{"parts": [{"text": "sensitive result"}]}],
                        "metadata": {"requestMessageId": "another-message"},
                    }
                ]
            }
        }

    monkeypatch.setattr(tools, "_request", unrelated_task)
    assert tools._recover_unique(ctx, handle) is None


def test_plugin_does_not_follow_redirects(monkeypatch) -> None:
    class RedirectingOpener:
        def open(self, request: Any, timeout: float) -> None:
            del timeout
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", {}, None)

    monkeypatch.setattr(tools, "_NO_REDIRECT_OPENER", RedirectingOpener())
    try:
        tools._request("http://127.0.0.1:9910", {"jsonrpc": "2.0"}, 1)
    except urllib.error.HTTPError as exc:
        assert exc.code == 302
    else:
        raise AssertionError("redirect was unexpectedly followed")

    result = json.loads(tools._call(FakeContext(), {"message": "do not redirect"}))
    assert not result["ok"] and result["handleInfo"]["state"] == "TASK_STATE_REJECTED"


def test_cli_installs_bundled_hermes_plugin_idempotently(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    assert cli._install_hermes_plugin(replace=False) == 0
    target = tmp_path / ".hermes" / "plugins" / "codex-a2a-gateway"
    assert (target / "plugin.yaml").is_file()
    assert cli._install_hermes_plugin(replace=False) == 0


def test_cli_installs_plugin_under_active_hermes_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "active-hermes"))
    assert cli._install_hermes_plugin(replace=False) == 0
    assert (tmp_path / "active-hermes" / "plugins" / "codex-a2a-gateway" / "plugin.yaml").is_file()
