"""End-to-end test of the core stack wired exactly like the plugin does it:
WSServer -> MCPServer.handle_text -> send_text, plus deferred openDiff
resolution through PendingRequests. No Sublime involved."""

import json

import pytest

from claudeide import jsonrpc, lockfile
from claudeide.jsonrpc import DEFERRED
from claudeide.mcp import MCPServer
from claudeide.session import PendingRequests
from claudeide.wsserver import WSServer

from .wsclient import WSClient


@pytest.fixture()
def stack():
    token = lockfile.generate_token()
    mcp = MCPServer(server_name="Sublime Text", version="0.0.0-test")
    pending = PendingRequests()

    mcp.register_tool(
        "getOpenEditors", "List open editors", {"type": "object"},
        lambda args, ctx: {"tabs": [{"uri": "file:///C:/x.py", "isActive": True}]},
    )

    def open_diff(args, ctx):
        pending.add(ctx["client_id"], ctx["id"], {"tab_name": args.get("tab_name")})
        return DEFERRED

    mcp.register_tool("openDiff", "Blocking diff review", {"type": "object"}, open_diff)

    server = WSServer(
        auth_token=token,
        on_message=lambda cid, text: _pump(server, mcp, cid, text),
    )
    port = server.start()
    client = WSClient(port, token)
    yield server, mcp, pending, client
    client.close()
    server.stop()


def _pump(server, mcp, client_id, text):
    out = mcp.handle_text(text, client_id)
    if out is not None:
        server.send_to(client_id, out)


def test_full_mcp_flow(stack):
    server, mcp, pending, client = stack

    # initialize -> response
    client.send_json({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "claude-code", "version": "test"}},
    })
    resp = client.recv_json()
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == "2025-03-26"

    # initialized notification -> silence
    client.send_json({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # tools/list
    client.send_json({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {t["name"] for t in client.recv_json()["result"]["tools"]}
    assert tools == {"getOpenEditors", "openDiff"}

    # plain tool call
    client.send_json({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "getOpenEditors", "arguments": {}},
    })
    resp = client.recv_json()
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["tabs"][0]["isActive"] is True


def test_deferred_open_diff_resolves_later(stack):
    server, mcp, pending, client = stack

    client.send_json({
        "jsonrpc": "2.0", "id": "diff-1", "method": "tools/call",
        "params": {"name": "openDiff",
                   "arguments": {"tab_name": "review: x.py", "new_file_contents": "y"}},
    })
    # no immediate answer (blocking tool)
    assert client.recv_json(timeout=0.5) is None
    assert pending.get_meta(1, "diff-1") == {"tab_name": "review: x.py"}

    # ... user clicks Accept -> UI thread resolves
    resp = pending.resolve(1, "diff-1", "FILE_SAVED")
    server.send_to(1, json.dumps(resp, ensure_ascii=False))

    got = client.recv_json()
    assert got["id"] == "diff-1"
    assert got["result"]["content"][0]["text"] == "FILE_SAVED"


def test_two_sessions_with_colliding_request_ids(stack):
    """Two Claude sessions call openDiff with the SAME JSON-RPC id — the
    pending map must keep them apart and route each outcome correctly."""
    server, mcp, pending, client1 = stack
    client2 = WSClient(server.port, client1_token_of(stack))

    for c in (client1, client2):
        c.send_json({
            "jsonrpc": "2.0", "id": "diff-X", "method": "tools/call",
            "params": {"name": "openDiff",
                       "arguments": {"tab_name": f"tab-{id(c)}", "new_file_contents": "z"}},
        })
    assert client1.recv_json(timeout=0.5) is None
    assert client2.recv_json(timeout=0.5) is None

    # resolve client 1 as saved, client 2 as rejected
    server.send_to(1, json.dumps(pending.resolve(1, "diff-X", "FILE_SAVED")))
    server.send_to(2, json.dumps(pending.resolve(2, "diff-X", "DIFF_REJECTED")))

    assert client1.recv_json()["result"]["content"][0]["text"] == "FILE_SAVED"
    assert client2.recv_json()["result"]["content"][0]["text"] == "DIFF_REJECTED"
    client2.close()


def client1_token_of(stack):
    server, _mcp, _pending, _client = stack
    return server._auth_token


def test_server_push_notification_reaches_client(stack):
    server, mcp, pending, client = stack
    server.send_text(json.dumps(jsonrpc.notification(
        "selection_changed",
        {"text": "foo", "filePath": "C:\\x.py",
         "selection": {"start": {"line": 0, "character": 0},
                       "end": {"line": 0, "character": 3}, "isEmpty": False}},
    ), ensure_ascii=False))
    note = client.recv_json()
    assert note["method"] == "selection_changed"
    assert note["params"]["text"] == "foo"
