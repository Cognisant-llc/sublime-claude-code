"""Manual smoke test: connect to the plugin inside a *running* Sublime Text
exactly the way Claude Code does (lock discovery + authed WebSocket + MCP),
then exercise the M1 tool surface.

Usage:  uv run python scripts/smoke.py
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.wsclient import WSClient  # noqa: E402


def find_sublime_locks():
    lock_dir = os.path.join(os.path.expanduser("~"), ".claude", "ide")
    found = []
    for path in glob.glob(os.path.join(lock_dir, "*.lock")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if data.get("ideName") == "Sublime Text" and data.get("transport") == "ws":
            port = int(os.path.basename(path).split(".")[0])
            found.append((os.path.getmtime(path), port, data))
    return sorted(found, reverse=True)


def rpc(client, msg_id, method, params=None):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    client.send_json(msg)
    resp = client.recv_json()
    assert resp is not None, f"no response for {method}"
    assert resp.get("id") == msg_id, f"id mismatch for {method}: {resp}"
    return resp


def tool(client, msg_id, name, arguments=None):
    resp = rpc(client, msg_id, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "result" in resp, f"tool {name} errored: {resp}"
    text = resp["result"]["content"][0]["text"]
    try:
        return json.loads(text)
    except ValueError:
        return text


def main():
    locks = find_sublime_locks()
    if not locks:
        print("NG: no 'Sublime Text' lock file found — is the plugin loaded?")
        return 1

    client = None
    for _, port, data in locks:
        try:
            client = WSClient(port, data["authToken"])
            print(f"OK connect      port={port} pid={data['pid']}")
            print(f"   workspaces   {data['workspaceFolders']}")
            break
        except OSError as exc:
            print(f"-- stale lock?  port={port} ({exc})")
    if client is None:
        print("NG: could not connect to any lock")
        return 1

    resp = rpc(client, 1, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "smoke-client", "version": "0"},
    })
    info = resp["result"]
    print(f"OK initialize   {info['serverInfo']['name']} {info['serverInfo']['version']} "
          f"proto={info['protocolVersion']}")
    client.send_json({"jsonrpc": "2.0", "method": "notifications/initialized"})

    tools = rpc(client, 2, "tools/list")["result"]["tools"]
    names = sorted(t["name"] for t in tools)
    print(f"OK tools/list   {len(names)} tools: {', '.join(names)}")

    editors = tool(client, 3, "getOpenEditors")
    print(f"OK openEditors  {len(editors.get('tabs', []))} tabs")
    for tab in editors.get("tabs", [])[:5]:
        marker = "*" if tab["isActive"] else " "
        dirty = "+" if tab["isDirty"] else " "
        print(f"   {marker}{dirty} [{tab['languageId']}] {tab['label']}")

    ws = tool(client, 4, "getWorkspaceFolders")
    print(f"OK workspaces   {[f['path'] for f in ws.get('folders', [])]}")

    sel = tool(client, 5, "getCurrentSelection")
    if sel.get("selection"):
        s = sel["selection"]
        print(f"OK selection    {sel.get('filePath')} "
              f"L{s['start']['line']}:{s['start']['character']}"
              f"-L{s['end']['line']}:{s['end']['character']} empty={s['isEmpty']}")
    else:
        print(f"OK selection    (no active editor: {sel})")

    dirty = tool(client, 6, "checkDocumentDirty",
                 {"filePath": sel.get("filePath") or "C:\\nonexistent.txt"})
    print(f"OK checkDirty   {dirty}")

    client.close()
    print("\nSMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
