"""Claude Code hook: append one JSON line per tool call to the activity log.

Register in ``~/.claude/settings.json`` (see README "Recent Activity"):

    "PreToolUse":  [{"matcher": "Bash", "hooks": [{"type": "command",
                     "command": "py -3 \\"<repo>/scripts/activity_hook.py\\"", "timeout": 5}]}],
    "PostToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit", "hooks": [...same...]}]

Records (``~/.claude/logs/file-activity.jsonl``):

    {"ts": 1788500000.1, "ev": "edit", "sid": "<session_id>", "name": "gg-ds-8e",
     "cwd": "C:\\\\...", "path": "C:\\\\...\\\\STATUS.md", "tool": "Edit", "agent": "Explore"}
    {"ts": ..., "ev": "bash_start" | "bash_end", "sid": ..., "name": ..., "cwd": ...}

The session *name* is resolved here from ``~/.claude/sessions/<pid>.json``
so it survives after the session ends. Never raises: a hook failure must
not disturb the agent, so every problem exits 0 silently.
"""

import glob
import json
import os
import sys
import time

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def claude_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def log_path(home=None):
    return os.path.join(home or claude_home(), "logs", "file-activity.jsonl")


def resolve_session_name(session_id, home=None):
    """Name of the live session with this id, or None."""
    for fn in glob.glob(os.path.join(home or claude_home(), "sessions", "*.json")):
        try:
            with open(fn, encoding="utf-8") as fh:
                j = json.load(fh)
        except (OSError, ValueError):
            continue
        if j.get("sessionId") == session_id:
            return j.get("name")
    return None


def record_from_hook(payload, now=None, name=None):
    """Map a hook stdin payload to a log record (pure; None if irrelevant)."""
    ev_name = payload.get("hook_event_name")
    tool = payload.get("tool_name") or ""
    rec = {
        "ts": round(now if now is not None else time.time(), 3),
        "sid": payload.get("session_id") or "",
        "cwd": payload.get("cwd") or "",
    }
    if name:
        rec["name"] = name
    if payload.get("agent_type"):
        rec["agent"] = payload["agent_type"]
    if tool == "Bash":
        if ev_name == "PreToolUse":
            rec["ev"] = "bash_start"
        elif ev_name == "PostToolUse":
            rec["ev"] = "bash_end"
        else:
            return None
        return rec
    if tool in EDIT_TOOLS and ev_name == "PostToolUse":
        tin = payload.get("tool_input") or {}
        path = tin.get("file_path") or tin.get("notebook_path")
        if not path:
            return None
        rec["ev"] = "edit"
        rec["path"] = os.path.normpath(path)
        rec["tool"] = tool
        return rec
    return None


def append_record(rec, path=None):
    path = path or log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        name = resolve_session_name(payload.get("session_id"))
        rec = record_from_hook(payload, name=name)
        if rec:
            append_record(rec)
    except Exception:  # noqa: BLE001 - a hook must never fail the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
