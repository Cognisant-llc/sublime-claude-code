"""Open a file in Sublime Text through the plugin's own MCP ``openFile`` tool.

Why this exists: Claude Code only exposes ``getDiagnostics`` / ``executeCode``
from the ide server to the model, so an agent can never call
``mcp__ide__openFile`` itself — even when the session is connected (verified
2026-08-12; see docs/dev-notes.md). This CLI speaks the same lock-discovery +
authed-WebSocket protocol as Claude Code and calls ``openFile`` directly, so
an agent (or you, from any terminal) can surface a file in Sublime with the
plugin's full semantics: side-group placement, ``--preview`` transient tabs,
optional text selection. Works even for sessions that never ran ``/ide``.

Usage:
    python scripts/open_file.py PATH                # show, focus Sublime
    python scripts/open_file.py PATH --preview      # transient, no focus steal
    python scripts/open_file.py PATH --no-focus     # normal tab, no focus steal
    python scripts/open_file.py PATH --start-text "## Summary" [--end-text S]
                                     [--select-to-eol]

Exit codes: 0 opened / 1 no reachable Sublime server / 2 bad arguments.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.smoke import find_sublime_locks  # noqa: E402
from tests.wsclient import WSClient  # noqa: E402

RESPONSE_TIMEOUT = 8.0  # openFile marshals to the UI thread; allow a busy one


class ToolCallError(RuntimeError):
    """openFile failed (isError content, JSON-RPC error, or no response)."""


def request_from_argv(argv):
    """Map CLI argv to ``openFile`` tool arguments (pure; no I/O)."""
    ap = argparse.ArgumentParser(
        prog="open_file.py",
        description="Open a file in Sublime via the Claude Code IDE plugin.",
    )
    ap.add_argument("path", help="file to show (made absolute against cwd)")
    ap.add_argument("--preview", action="store_true",
                    help="transient tab; does not steal focus")
    ap.add_argument("--no-focus", action="store_true",
                    help="normal tab, but keep Sublime in the background")
    ap.add_argument("--start-text", help="select from this literal text")
    ap.add_argument("--end-text", help="…through the end of this literal text")
    ap.add_argument("--select-to-eol", action="store_true",
                    help="extend the selection to the end of its line")
    ns = ap.parse_args(argv)

    args = {
        "filePath": os.path.abspath(ns.path),
        "preview": ns.preview,
        "makeFrontmost": not (ns.preview or ns.no_focus),
    }
    if ns.start_text:
        args["startText"] = ns.start_text
        if ns.end_text:
            args["endText"] = ns.end_text
        if ns.select_to_eol:
            args["selectToEndOfLine"] = True
    return args


def wait_response(client, msg_id, timeout=RESPONSE_TIMEOUT):
    """Next response matching ``msg_id``, skipping server notifications
    (``selection_changed`` broadcasts can interleave while the user types)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.recv_json(timeout=max(0.1, deadline - time.time()))
        if resp is None:
            return None
        if resp.get("id") == msg_id:
            return resp
    return None


def extract_tool_result(resp):
    """Content text of a tools/call response; raise ToolCallError on failure."""
    if resp is None:
        raise ToolCallError("no response from Sublime (timeout)")
    if "error" in resp:
        err = resp["error"]
        raise ToolCallError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
    result = resp.get("result") or {}
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    if result.get("isError"):
        raise ToolCallError(text or "openFile failed")
    return text


def open_via_plugin(arguments, timeout=RESPONSE_TIMEOUT):
    """Connect to the newest live Sublime lock and call ``openFile``."""
    locks = find_sublime_locks()
    if not locks:
        raise ToolCallError(
            "no 'Sublime Text' lock file — is Sublime running with the plugin?")
    last = "no lock accepted a connection"
    for _, port, data in locks:
        try:
            client = WSClient(port, data["authToken"])
        except OSError as exc:
            last = f"port {port}: {exc}"  # stale lock (crashed instance) — try next
            continue
        try:
            client.send_json({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "open-file-cli", "version": "0"}},
            })
            if wait_response(client, 1, timeout) is None:
                last = f"port {port}: initialize timed out"
                continue
            client.send_json({"jsonrpc": "2.0", "method": "notifications/initialized"})
            client.send_json({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "openFile", "arguments": arguments},
            })
            return extract_tool_result(wait_response(client, 2, timeout))
        finally:
            client.close()
    raise ToolCallError(last)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):  # CP932 consoles must not crash on paths
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    arguments = request_from_argv(sys.argv[1:] if argv is None else argv)
    if not os.path.exists(arguments["filePath"]):
        print(f"NG: file not found: {arguments['filePath']}")
        return 2
    try:
        result = open_via_plugin(arguments)
    except ToolCallError as exc:
        print(f"NG: {exc}")
        return 1
    print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
