from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fake_a2a import FakeA2AServer
from jsonschema import validate
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError


@pytest.mark.asyncio
async def test_mcp_stdio_initialize_list_and_all_tools(fake_a2a: FakeA2AServer, tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "HERMES_A2A_ENDPOINT": fake_a2a.endpoint,
            "CODEX_A2A_GATEWAY_STATE_PATH": str(tmp_path / "stdio.sqlite"),
            "CODEX_A2A_GATEWAY_DEFAULT_TIMEOUT": "5",
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_a2a_gateway.cli", "serve"],
        cwd=Path(__file__).parents[1],
        env=env,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        assert "Use hermes_chat" in (initialized.instructions or "")
        tools = await session.list_tools()
        expected = {
            "hermes_status",
            "hermes_chat",
            "hermes_task_get",
            "hermes_tasks_list",
            "hermes_task_wait",
            "hermes_task_cancel",
            "hermes_contexts",
        }
        assert {tool.name for tool in tools.tools} == expected
        for tool in tools.tools:
            assert tool.input_schema["type"] == "object"
            assert tool.output_schema["type"] == "object"
        chat_description = next(tool for tool in tools.tools if tool.name == "hermes_chat")
        assert chat_description.annotations.destructive_hint is True
        assert chat_description.annotations.open_world_hint is True

        status = await session.call_tool("hermes_status")
        assert status.structured_content["ok"] is True
        assert status.is_error is False
        validate(status.structured_content, next(t for t in tools.tools if t.name == "hermes_status").output_schema)
        assert json.loads(status.content[0].text) == status.structured_content
        missing = await session.call_tool("hermes_task_get", {"task_id": "missing-task"})
        assert missing.is_error is True
        assert missing.structured_content["ok"] is False
        assert json.loads(missing.content[0].text) == missing.structured_content
        with pytest.raises(MCPError) as unknown:
            await session.call_tool("unknown_tool", {})
        assert unknown.value.code == -32602
        chat = await session.call_tool(
            "hermes_chat",
            {"message": "long operation stdio", "conversation_key": "stdio-conv", "mode": "async"},
        )
        task = chat.structured_content
        assert task["state"] in {"queued", "submitted", "working"}
        got = await session.call_tool("hermes_task_get", {"task_id": task["bridge_task_id"], "refresh": False})
        assert got.structured_content["bridge_task_id"] == task["bridge_task_id"]
        listed = await session.call_tool("hermes_tasks_list", {"conversation_key": "stdio-conv"})
        assert listed.structured_content["count"] == 1
        waited = await session.call_tool("hermes_task_wait", {"task_id": task["bridge_task_id"], "timeout": 1})
        assert waited.structured_content["state"] == "working"
        contexts = await session.call_tool("hermes_contexts", {"action": "inspect", "conversation_key": "stdio-conv"})
        assert contexts.structured_content["context"]["conversation_key"] == "stdio-conv"
        canceled = await session.call_tool("hermes_task_cancel", {"task_id": task["bridge_task_id"]})
        assert canceled.structured_content["cancel_requested"] is True
        assert canceled.structured_content["computation_stopped"] == "unknown"
