"""Two simultaneous pseudo-sessions against the live plugin: both must
initialize, call tools concurrently (with the same JSON-RPC ids), and get
correctly routed answers.

Usage:  uv run python scripts/smoke_multi.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.smoke import find_sublime_locks  # noqa: E402
from tests.wsclient import WSClient  # noqa: E402


def init(client, name):
    client.send_json({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": name, "version": "0"}},
    })
    resp = client.recv_json()
    assert resp and resp["result"]["protocolVersion"] == "2025-03-26"
    client.send_json({"jsonrpc": "2.0", "method": "notifications/initialized"})


def main():
    locks = find_sublime_locks()
    if not locks:
        print("NG: no lock")
        return 1
    _, port, data = locks[0]

    c1 = WSClient(port, data["authToken"])
    c2 = WSClient(port, data["authToken"])
    print(f"OK connect x2   port={port}")

    init(c1, "session-A")
    init(c2, "session-B")
    print("OK initialize   both sessions")

    # identical request ids from both sessions, interleaved
    c1.send_json({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                  "params": {"name": "getWorkspaceFolders", "arguments": {}}})
    c2.send_json({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                  "params": {"name": "getOpenEditors", "arguments": {}}})

    r1 = c1.recv_json()
    r2 = c2.recv_json()
    assert r1 and "folders" in r1["result"]["content"][0]["text"], f"c1 got: {r1}"
    assert r2 and "tabs" in r2["result"]["content"][0]["text"], f"c2 got: {r2}"
    print("OK routing      c1->workspaceFolders / c2->openEditors (same id=7)")

    c1.close()
    # c2 must survive c1's disconnect
    c2.send_json({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                  "params": {"name": "getWorkspaceFolders", "arguments": {}}})
    r = c2.recv_json()
    assert r and r["id"] == 8, f"c2 after c1 close: {r}"
    print("OK resilience   c2 alive after c1 disconnect")
    c2.close()

    print("\nMULTI SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
