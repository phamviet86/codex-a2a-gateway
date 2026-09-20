from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from hermes_a2a_gateway import client_mcp
from hermes_a2a_gateway.native_delivery import NativeOrigin


def context():
    thread, turn = str(uuid4()), str(uuid4())
    return SimpleNamespace(
        request_context=SimpleNamespace(
            meta={
                "threadId": thread,
                "callId": "native-call",
                "x-codex-turn-metadata": {"thread_id": thread, "turn_id": turn},
            }
        )
    )


async def test_separate_mcp_tools_have_no_model_native_identity():
    tools = await client_mcp.mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "gateway_submit",
        "gateway_get",
        "gateway_wait",
        "gateway_cancel",
        "gateway_upload_artifact",
    }
    for tool in tools:
        schema = tool.input_schema
        assert "ctx" not in schema["properties"]
        assert not any(key in schema["properties"] for key in ("thread_id", "threadId", "meta", "flow_id"))
    assert "operation_id" in next(tool for tool in tools if tool.name == "gateway_get").input_schema["required"]


def test_actual_mcp_v2_meta_is_an_open_dict():
    ctx = context()
    assert NativeOrigin.from_meta(client_mcp.request_meta(ctx)).thread_id == ctx.request_context.meta["threadId"]


async def test_missing_metadata_fails_before_transport():
    response = await client_mcp.invoke(SimpleNamespace(request_context=SimpleNamespace(meta=None)), "get", {})
    assert response.is_error
    assert response.structured_content["error"]["code"] == "native_metadata_required"


async def test_upload_is_explicit_bounded_regular_file_and_never_sends_path(tmp_path, monkeypatch):
    file = tmp_path / "artifact.txt"
    file.write_text("harmless fixture")
    recorded = []

    async def invoke(ctx, action, arguments):
        recorded.append((action, arguments))
        return "uploaded"

    monkeypatch.setattr(client_mcp, "invoke", invoke)
    assert await client_mcp.gateway_upload_artifact(str(file), context(), "text/plain") == "uploaded"
    action, arguments = recorded[0]
    assert action == "upload"
    assert "path" not in arguments
    assert str(file) not in json.dumps(arguments)
    symlink = tmp_path / "link"
    symlink.symlink_to(file)
    assert (await client_mcp.gateway_upload_artifact(str(symlink), context())).is_error
    monkeypatch.setenv("HERMES_A2A_GATEWAY_CLIENT_MAX_UPLOAD_BYTES", "2")
    assert (await client_mcp.gateway_upload_artifact(str(file), context())).is_error
    assert len(recorded) == 1
