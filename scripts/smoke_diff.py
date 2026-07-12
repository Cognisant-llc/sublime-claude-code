"""Interactive smoke test for the M2 openDiff review UI.

Sends a real openDiff tools/call to the plugin inside the running Sublime,
then blocks until the human clicks Accept / Reject in the editor.
On FILE_SAVED it verifies the file content on disk.

Usage:  uv run python scripts/smoke_diff.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.smoke import find_sublime_locks  # noqa: E402
from tests.wsclient import WSClient  # noqa: E402

OLD_CONTENT = "def greet():\n    print('hello')\n\n\ngreet()\n"
NEW_CONTENT = (
    "def greet(name: str = 'Sublime') -> None:\n"
    "    print(f'hello, {name}!')\n\n\ngreet('Claude')\n"
)


def main():
    locks = find_sublime_locks()
    if not locks:
        print("NG: no Sublime lock")
        return 1
    _, port, data = locks[0]
    client = WSClient(port, data["authToken"])
    print(f"OK connect      port={port}")

    client.send_json({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "smoke-diff", "version": "0"}},
    })
    assert client.recv_json()["result"]["protocolVersion"] == "2025-03-26"
    client.send_json({"jsonrpc": "2.0", "method": "notifications/initialized"})

    client.send_json({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = sorted(t["name"] for t in client.recv_json()["result"]["tools"])
    has_diff = "present" if "openDiff" in names else "MISSING"
    print(f"OK tools/list   {len(names)} tools (openDiff {has_diff})")
    if "openDiff" not in names:
        return 1

    target = os.path.join(tempfile.gettempdir(), "claude_diff_e2e_target.py")
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(OLD_CONTENT)
    print(f"OK target file  {target}")

    print(">> openDiff sent — Sublime に diff が開きます。"
          "右ペインの ✓Accept または ✗Reject を押してください…")
    client.send_json({
        "jsonrpc": "2.0", "id": "diff-e2e-1", "method": "tools/call",
        "params": {"name": "openDiff", "arguments": {
            "old_file_path": target,
            "new_file_path": target,
            "new_file_contents": NEW_CONTENT,
            "tab_name": "CLAUDE-DIFF-E2E",
        }},
    })

    resp = client.recv_json(timeout=180)
    if resp is None:
        print("NG: no resolution within 180s")
        return 1
    outcome = resp["result"]["content"][0]["text"]
    print(f"OK resolution   {outcome}")

    if outcome == "FILE_SAVED":
        with open(target, encoding="utf-8") as fh:
            on_disk = fh.read()
        if on_disk == NEW_CONTENT:
            print("OK disk write   file matches proposed content exactly")
        elif "hello" in on_disk and on_disk != OLD_CONTENT:
            print("OK disk write   file updated (hand-edited variant)")
        else:
            print("NG disk write   file content unexpected:")
            print(on_disk)
            return 1

    # protocol follow-up claude would send: close any leftovers
    client.send_json({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "closeAllDiffTabs", "arguments": {}},
    })
    print(f"OK cleanup      {client.recv_json()['result']['content'][0]['text']}")

    client.close()
    print("\nDIFF SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
