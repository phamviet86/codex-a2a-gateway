from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from fake_a2a import FakeA2AServer
from jsonschema import validate


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_wire_negotiation_errors_and_stdout(version: str, fake_a2a: FakeA2AServer, tmp_path: Path) -> None:
    env = {
        **os.environ,
        "HERMES_A2A_ENDPOINT": fake_a2a.endpoint,
        "CODEX_A2A_GATEWAY_STATE_PATH": str(tmp_path / "wire.sqlite3"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "codex_a2a_gateway.cli", "serve"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin and process.stdout and process.stderr
    lines: queue.Queue[str | None] = queue.Queue()
    errors: list[str] = []
    stdout = process.stdout

    def read_stdout() -> None:
        for line in stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    stderr_reader = threading.Thread(target=lambda: errors.extend(process.stderr.readlines()), daemon=True)
    reader.start()
    stderr_reader.start()
    sequence = 0

    def call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        params = dict(params or {})
        if version == "2026-07-28":
            params["_meta"] = {
                "io.modelcontextprotocol/protocolVersion": version,
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": "wire-test", "version": "1"},
            }
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": sequence, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        while True:
            line = lines.get(timeout=15)
            assert line is not None, "server exited: " + "".join(errors)
            result = json.loads(line)  # Every stdout line must be a protocol frame.
            assert result.get("jsonrpc") == "2.0"
            if result.get("id") == sequence:
                return result

    try:
        if version == "2026-07-28":
            initialized = call("server/discover")
            assert version in initialized["result"]["supportedVersions"]
        else:
            initialized = call(
                "initialize",
                {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "wire-test", "version": "1"}},
            )
            assert initialized["result"]["protocolVersion"] == version
            process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            process.stdin.flush()
        tools = call("tools/list")["result"]["tools"]
        assert len(tools) == 7
        status = call("tools/call", {"name": "hermes_status", "arguments": {}})["result"]
        assert status.get("isError", False) is False
        assert status["structuredContent"]["ok"] is True
        validate(status["structuredContent"], next(t for t in tools if t["name"] == "hermes_status")["outputSchema"])
        assert json.loads(status["content"][0]["text"]) == status["structuredContent"]
        failure = call("tools/call", {"name": "hermes_task_get", "arguments": {"task_id": "absent"}})["result"]
        assert failure["isError"] is True
        assert failure["structuredContent"]["ok"] is False
        assert json.loads(failure["content"][0]["text"]) == failure["structuredContent"]
        unknown = call("tools/call", {"name": "not_a_tool", "arguments": {}})
        assert unknown["error"]["code"] == -32602
        invalid = call("tools/call", {"name": "hermes_task_get", "arguments": {}})
        assert invalid["result"]["isError"] is True  # Known-tool input validation.
        malformed = call("tools/call", {"arguments": {}})
        assert malformed["error"]["code"] == -32602
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=2)
        stderr_reader.join(timeout=2)
        while not lines.empty():
            line = lines.get_nowait()
            if line is not None:
                assert json.loads(line)["jsonrpc"] == "2.0"
        assert process.returncode == 0, "".join(errors)
