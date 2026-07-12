import json

import pytest

from claudeide.jsonrpc import DEFERRED
from claudeide.mcp import MCPServer, ToolError, tool_text_response


@pytest.fixture()
def server():
    srv = MCPServer(server_name="Sublime Text", version="0.1.0")
    srv.register_tool(
        "echo", "Echo back", {"type": "object", "properties": {"msg": {"type": "string"}}},
        lambda args, ctx: args["msg"],
    )
    srv.register_tool(
        "close_tab", "Close a tab", {"type": "object"},
        lambda args, ctx: "TAB_CLOSED",
    )
    srv.register_tool(
        "getStuff", "Structured result", {"type": "object"},
        lambda args, ctx: {"items": [1, 2], "日本語": "はい"},
    )
    srv.register_tool(
        "openDiff", "Blocking diff", {"type": "object"},
        lambda args, ctx: DEFERRED,
    )
    srv.register_tool(
        "failing", "Raises ToolError", {"type": "object"},
        lambda args, ctx: (_ for _ in ()).throw(ToolError("not supported")),
    )
    return srv


def _call(server, msg_id, name, arguments=None):
    text = json.dumps({
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    out = server.handle_text(text)
    return json.loads(out) if out is not None else None


def test_initialize(server):
    out = server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
    }))
    resp = json.loads(out)
    assert resp["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["serverInfo"]["name"] == "Sublime Text"


def test_initialized_notification_is_silent(server):
    out = server.handle_text(json.dumps({
        "jsonrpc": "2.0", "method": "notifications/initialized",
    }))
    assert out is None


def test_tools_list(server):
    out = server.handle_text(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    resp = json.loads(out)
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert "echo" in tools and "close_tab" in tools
    assert tools["echo"]["description"] == "Echo back"
    assert tools["echo"]["inputSchema"]["type"] == "object"


def test_tools_call_wraps_string_result(server):
    resp = _call(server, 3, "echo", {"msg": "hello"})
    assert resp["result"] == {"content": [{"type": "text", "text": "hello"}]}


def test_tools_call_json_stringifies_dict_result(server):
    resp = _call(server, 4, "getStuff")
    text = resp["result"]["content"][0]["text"]
    assert json.loads(text) == {"items": [1, 2], "日本語": "はい"}
    assert "はい" in text  # ensure_ascii=False


def test_tools_call_unknown_tool_is_error(server):
    resp = _call(server, 5, "nope")
    assert "error" in resp


def test_tools_call_deferred_returns_none(server):
    assert _call(server, 6, "openDiff") is None


def test_tools_call_tool_error_becomes_is_error_content(server):
    resp = _call(server, 7, "failing")
    assert resp["result"]["isError"] is True
    assert "not supported" in resp["result"]["content"][0]["text"]


def test_parse_error_returns_jsonrpc_error():
    srv = MCPServer()
    out = srv.handle_text("{broken")
    resp = json.loads(out)
    assert resp["error"]["code"] == -32700


def test_tool_text_response_shape():
    r = tool_text_response("id-1", "FILE_SAVED")
    assert r == {
        "jsonrpc": "2.0", "id": "id-1",
        "result": {"content": [{"type": "text", "text": "FILE_SAVED"}]},
    }
