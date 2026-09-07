"""Smoke test: a diff review must give back a 2x2 grid window untouched.

Opens a new window in the running Sublime with four files in a 2x2 grid,
sends a real openDiff through the plugin's WebSocket, closes it again with
closeAllDiffTabs, and checks that the layout has four groups and every file
sits in its original group (the bug in issue #1). Runs against whichever
``diff_layout`` mode is configured; switch the setting and run again to
cover the other one. Closes its own window at the end.

Usage:  uv run python scripts/smoke_layout.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.smoke import find_sublime_locks  # noqa: E402
from tests.wsclient import WSClient  # noqa: E402

GRID = {"cols": [0.0, 0.5, 1.0], "rows": [0.0, 0.5, 1.0],
        "cells": [[0, 0, 1, 1], [1, 0, 2, 1], [0, 1, 1, 2], [1, 1, 2, 2]]}
NAMES = ["claude_layout_smoke_a.py", "claude_layout_smoke_b.py",
         "claude_layout_smoke_c.py", "claude_layout_smoke_d.py"]


def subl(*args):
    # subl on Windows often exits 1 ("Failed to receive exit code") after
    # forwarding to the running instance: the command still ran.
    subprocess.run(["subl", *args], check=False, capture_output=True)


def dump():
    subl("--command", "claude_ide_dump_state")
    time.sleep(1.2)
    with open(os.path.join(tempfile.gettempdir(), "claude_ide_state.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def mapping(d):
    return {s["file"]: s["group"] for s in d["sheets"] if s["file"] in NAMES}


def diff_tabs(d):
    return [s["diff_tab"] for s in d["sheets"] if s["diff_tab"]]


def main():
    locks = find_sublime_locks()
    if not locks:
        print("NG: no Sublime lock")
        return 1
    _, port, data = locks[0]

    paths = []
    for n in NAMES:
        p = os.path.join(tempfile.gettempdir(), n)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"# {n}\nprint('hello')\n")
        paths.append(p)

    subl("-n", *paths)
    time.sleep(2.5)
    subl("--command", "set_layout " + json.dumps(GRID))
    time.sleep(0.6)
    for p, g in zip(paths[1:], (1, 2, 3)):
        subl(p)
        time.sleep(0.4)
        subl("--command", "move_to_group " + json.dumps({"group": g}))
        time.sleep(0.4)
    before = mapping(dump())
    if before != dict(zip(NAMES, (0, 1, 2, 3))):
        print(f"NG grid setup   {before}")
        return 1
    print("OK grid setup   4 files in 4 groups")

    client = WSClient(port, data["authToken"])
    client.send_json({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "smoke-layout", "version": "0"}},
    })
    assert client.recv_json()["result"]["protocolVersion"] == "2025-03-26"
    client.send_json({"jsonrpc": "2.0", "method": "notifications/initialized"})
    client.send_json({
        "jsonrpc": "2.0", "id": "d1", "method": "tools/call",
        "params": {"name": "openDiff", "arguments": {
            "old_file_path": paths[3], "new_file_path": paths[3],
            "new_file_contents": "print('changed')\n", "tab_name": "LAYOUT-SMOKE"}},
    })
    time.sleep(2.0)
    mid = dump()
    print(f"OK review open  groups={mid['num_groups']} tabs={diff_tabs(mid)}")
    client.send_json({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "closeAllDiffTabs", "arguments": {}}})
    for _ in range(2):
        client.recv_json(timeout=5)
    client.close()
    time.sleep(1.2)

    after = dump()
    ok = after["num_groups"] == 4 and mapping(after) == before and not diff_tabs(after)
    print(f"{'OK' if ok else 'NG'} restored     groups={after['num_groups']} "
          f"mapping={mapping(after)} tabs={diff_tabs(after)}")

    active = [w for w in after["windows"] if w["active"]][0]
    if active["project"] == "" and active["folders"] == 0:
        subl("--command", "close_window")
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
    print("\nLAYOUT SMOKE " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
