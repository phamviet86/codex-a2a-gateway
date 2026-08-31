from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fake_a2a import FakeA2AServer
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


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

        status = await session.call_tool("hermes_status")
        assert status.structured_content["ok"] is True
        chat = await session.call_tool(
            "hermes_chat",
            {"message": "long operation stdio", "conversation_key": "stdio-conv", "mode": "async"},
        )
        task = chat.structured_content
        assert task["state"] == "working"
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
