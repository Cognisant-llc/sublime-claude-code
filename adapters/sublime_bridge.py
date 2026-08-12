"""Sublime-facing glue: wires the pure protocol core to the Sublime API.

Everything that touches ``sublime.*`` lives here (or in plugin_main.py).
Reads use ST4's thread-safe API directly; anything that mutates UI state is
marshalled to the main thread. Blocking tools (openDiff, M2) park their
request in ``PendingRequests`` so the reader thread never stalls.
"""

import json
import os
import queue
import threading

import sublime

from ..claudeide import jsonrpc, lockfile
from ..claudeide.jsonrpc import DEFERRED
from ..claudeide.mcp import MCPServer, ToolError
from ..claudeide.pathurl import path_to_uri
from ..claudeide.session import PendingRequests
from ..claudeide.wsserver import WSServer
from . import diff_view

SETTINGS_FILE = "Claude Code IDE.sublime-settings"
STATUS_KEY = "zz_claude_ide"
PLUGIN_VERSION = "0.1.1"

_main_thread = None  # type: Optional[threading.Thread]

_state = {
    "server": None,       # type: Optional[WSServer]
    "mcp": None,          # type: Optional[MCPServer]
    "pending": None,      # type: Optional[PendingRequests]
    "token": None,        # type: Optional[str]
    "port": None,         # type: Optional[int]
    "connected": False,
    "latest_selection": None,   # type: Optional[Dict[str, Any]]
    "last_selection_sent": None,
    "debounce_token": 0,
    "lock_folders": None,  # type: Optional[List[str]]
}


def settings():
    return sublime.load_settings(SETTINGS_FILE)


def log(msg):
    if settings().get("debug", False):
        print(f"[ClaudeCodeIDE] {msg}")


# ---------- threading helpers ----------


def remember_main_thread():
    global _main_thread
    _main_thread = threading.current_thread()


def run_on_main(fn, timeout=5.0):
    """Run ``fn`` on the UI thread and return its result. Safe to call from
    the reader thread; calls through directly when already on main."""
    if threading.current_thread() is _main_thread:
        return fn()
    q = queue.Queue()

    def wrapper():
        try:
            q.put(("ok", fn()))
        except Exception as exc:  # noqa: BLE001 - marshalled back to caller
            q.put(("err", exc))

    sublime.set_timeout(wrapper, 0)
    kind, value = q.get(timeout=timeout)
    if kind == "err":
        raise value
    return value


# ---------- lifecycle ----------


def is_running():
    return _state["server"] is not None


def server_port():
    return _state["port"]


def start():
    if is_running():
        log("already running on port {}".format(_state["port"]))
        return _state["port"]

    token = lockfile.generate_token()
    mcp = MCPServer(
        server_name=settings().get("ide_name", "Sublime Text"),
        version=PLUGIN_VERSION,
        logger=log,
    )
    pending = PendingRequests()
    _register_tools(mcp, pending)

    # A fixed port (settings "port") lets machine-wide env vars point every
    # claude session here permanently; fall back to a random port if busy.
    port_setting = settings().get("port")
    ranges = []
    if isinstance(port_setting, int) and 1024 <= port_setting <= 65535:
        ranges.append((port_setting, port_setting))
    ranges.append((10000, 65535))

    server = None
    port = None
    last_err = None
    for rng in ranges:
        candidate = WSServer(
            auth_token=token,
            on_message=_on_message,
            on_connect=_on_client_connect,
            on_disconnect=_on_client_disconnect,
            port_range=rng,
            logger=log,
        )
        try:
            port = candidate.start()
            server = candidate
            break
        except OSError as exc:
            last_err = exc
            continue
    if server is None:
        raise OSError(f"could not start IDE server: {last_err}")
    if isinstance(port_setting, int) and port != port_setting:
        sublime.status_message(
            f"Claude Code IDE: fixed port {port_setting} busy — using {port} "
            "(auto-connect env vars will not match)")

    folders = _all_folders()
    lockfile.write_lock(
        port=port,
        pid=os.getpid(),
        workspace_folders=folders,
        auth_token=token,
        ide_name=settings().get("ide_name", "Sublime Text"),
    )

    _state.update({
        "server": server, "mcp": mcp, "pending": pending, "token": token,
        "port": port, "connected": False, "lock_folders": folders,
    })
    diff_view.set_resolver(_resolve_and_send)
    log(f"started on port {port} (lock written)")
    _refresh_status_bar()
    return port


def stop():
    server = _state["server"]
    pending = _state["pending"]
    if pending is not None and server is not None:
        for client_id, resp in pending.resolve_all("DIFF_REJECTED"):
            server.send_to(client_id, json.dumps(resp, ensure_ascii=False))
    try:
        run_on_main(diff_view.close_all_silent)
    except Exception as exc:  # noqa: BLE001
        log(f"diff cleanup on stop failed: {exc}")
    if server is not None:
        server.stop()
    if _state["port"] is not None:
        lockfile.remove_lock(_state["port"])
    _state.update({
        "server": None, "mcp": None, "pending": None, "token": None,
        "port": None, "connected": False, "lock_folders": None,
    })
    _refresh_status_bar()
    log("stopped")


def status_summary():
    if not is_running():
        return "Claude Code IDE: stopped"
    conn = "connected" if _state["connected"] else "waiting for Claude (/ide)"
    return "Claude Code IDE: port {} — {}\nlock: {}".format(
        _state["port"], conn, lockfile.lock_path(_state["port"]))


def launch_env_line():
    """Env-prefixed launch line for a POSIX-ish shell (git-bash)."""
    if not is_running():
        return None
    return "CLAUDE_CODE_SSE_PORT={} ENABLE_IDE_INTEGRATION=true claude".format(_state["port"])


# ---------- transport plumbing ----------


def _on_message(client_id, text):
    log(f"<- #{client_id} {text[:300]}")
    mcp = _state["mcp"]
    server = _state["server"]
    if mcp is None or server is None:
        return
    out = mcp.handle_text(text, client_id)
    if out is not None:
        log(f"-> #{client_id} {out[:300]}")
        server.send_to(client_id, out)


def _notify(method, params):
    """Broadcast an IDE-originated notification to every connected session."""
    server = _state["server"]
    if server is not None:
        server.broadcast(json.dumps(jsonrpc.notification(method, params), ensure_ascii=False))


def client_count():
    server = _state["server"]
    return server.client_count if server is not None else 0


def _on_client_connect(client_id):
    _state["connected"] = True
    _refresh_status_bar()
    log(f"client #{client_id} connected ({client_count()} total)")


def _resolve_and_send(client_id, request_id, text):
    """Resolve a deferred tool request (openDiff) and push its response to
    the session that asked."""
    pending = _state["pending"]
    server = _state["server"]
    if pending is None or server is None:
        return
    resp = pending.resolve(client_id, request_id, text)
    if resp is not None:
        server.send_to(client_id, json.dumps(resp, ensure_ascii=False))


def _on_client_disconnect(client_id):
    pending = _state["pending"]
    if pending is not None:
        pending.resolve_all_for(client_id, "DIFF_REJECTED")  # unblock; peer is gone
    try:
        run_on_main(lambda: diff_view.close_for_client(client_id))
    except Exception as exc:  # noqa: BLE001
        log(f"diff cleanup on disconnect failed: {exc}")
    _state["connected"] = client_count() > 0
    _refresh_status_bar()
    log(f"client #{client_id} disconnected ({client_count()} total)")


# ---------- status bar ----------


def _status_text():
    if not is_running():
        return ""
    count = client_count()
    if count > 1:
        return "Claude ⚡×{}:{}".format(count, _state["port"])
    if count == 1:
        return "Claude ⚡:{}".format(_state["port"])
    return "Claude ○:{}".format(_state["port"])


def _refresh_status_bar():
    def op():
        text = _status_text()
        for window in sublime.windows():
            view = window.active_view()
            if view is None:
                continue
            if text:
                view.set_status(STATUS_KEY, text)
            else:
                view.erase_status(STATUS_KEY)

    try:
        run_on_main(op)
    except Exception as exc:  # noqa: BLE001
        log(f"status bar update failed: {exc}")


def on_activated(view):
    text = _status_text()
    if text:
        view.set_status(STATUS_KEY, text)
    else:
        view.erase_status(STATUS_KEY)
    _maybe_update_lock_folders()


# ---------- workspace / selection tracking ----------


def _all_folders():
    folders = []
    for window in sublime.windows():
        for f in window.folders():
            if f not in folders:
                folders.append(f)
    return folders


def _maybe_update_lock_folders():
    if not is_running():
        return
    folders = _all_folders()
    if folders != _state["lock_folders"]:
        _state["lock_folders"] = folders
        lockfile.write_lock(
            port=_state["port"], pid=os.getpid(), workspace_folders=folders,
            auth_token=_state["token"],
            ide_name=settings().get("ide_name", "Sublime Text"),
        )
        log(f"lock workspaceFolders updated: {folders}")


def _selection_payload(view):
    regions = view.sel()
    region = regions[0] if len(regions) > 0 else sublime.Region(0, 0)
    text = view.substr(region)
    sl, sc = view.rowcol(region.begin())
    el, ec = view.rowcol(region.end())
    file_name = view.file_name()
    return {
        "text": text,
        "filePath": file_name,
        "fileUrl": path_to_uri(file_name) if file_name else None,
        "selection": {
            "start": {"line": sl, "character": sc},
            "end": {"line": el, "character": ec},
            "isEmpty": region.empty(),
        },
    }


def on_selection_modified(view):
    """Debounced selection_changed notification (called from the async
    worker via EventListener)."""
    if not is_running() or not _state["connected"]:
        return
    if view.file_name() is None or view.settings().get("is_widget"):
        return

    _state["debounce_token"] += 1
    token = _state["debounce_token"]
    delay = int(settings().get("selection_debounce_ms", 200))

    def fire():
        if token != _state["debounce_token"]:
            return  # superseded by a newer edit
        payload = _selection_payload(view)
        if not payload["selection"]["isEmpty"]:
            _state["latest_selection"] = payload
        serialized = json.dumps(payload, sort_keys=True)
        if serialized == _state["last_selection_sent"]:
            return
        _state["last_selection_sent"] = serialized
        _notify("selection_changed", payload)

    sublime.set_timeout_async(fire, delay)


def send_at_mention(view):
    if not is_running() or not _state["connected"]:
        sublime.status_message("Claude Code IDE: not connected")
        return
    if view.file_name() is None:
        sublime.status_message("Claude Code IDE: no file for @-mention")
        return
    regions = view.sel()
    region = regions[0] if len(regions) > 0 else sublime.Region(0, 0)
    sl, _ = view.rowcol(region.begin())
    el, _ = view.rowcol(region.end())
    _notify("at_mentioned", {
        "filePath": view.file_name(), "lineStart": sl, "lineEnd": el,
    })
    base = os.path.basename(view.file_name())
    sublime.status_message(f"Claude Code IDE: sent @{base}#L{sl + 1}-{el + 1}")


# ---------- tool implementations (called on reader thread) ----------


def _find_view(file_path):
    norm = os.path.normcase(os.path.normpath(file_path))
    for window in sublime.windows():
        for view in window.views():
            fn = view.file_name()
            if fn and os.path.normcase(os.path.normpath(fn)) == norm:
                return view
    return None


def _language_id(view):
    try:
        syntax = view.syntax()
        if syntax is not None and syntax.scope:
            return syntax.scope.split(".")[-1]
    except Exception:  # noqa: BLE001 - older API surface
        pass
    return "plaintext"


def _register_tools(mcp, pending):
    obj = {"type": "object", "properties": {}}

    def t(name, description, schema, handler):
        mcp.register_tool(name, description, schema, handler)

    t("openFile", "Open a file in the editor and optionally select text",
      {"type": "object", "properties": {
          "filePath": {"type": "string"},
          "preview": {"type": "boolean"},
          "startText": {"type": "string"},
          "endText": {"type": "string"},
          "selectToEndOfLine": {"type": "boolean"},
          "makeFrontmost": {"type": "boolean"},
      }, "required": ["filePath"]},
      _tool_open_file)

    t("getCurrentSelection", "Get the current selection in the active editor", obj,
      _tool_get_current_selection)

    t("getLatestSelection", "Get the most recent text selection (even from non-active editors)",
      obj, lambda args, ctx: _state["latest_selection"] or {
          "success": False, "message": "no selection recorded yet"})

    t("getOpenEditors", "List open editor tabs", obj, _tool_get_open_editors)

    t("getWorkspaceFolders", "List workspace folders", obj, _tool_get_workspace_folders)

    t("getDiagnostics", "Get language diagnostics",
      {"type": "object", "properties": {"uri": {"type": "string"}}},
      lambda args, ctx: [])  # M3: LSP integration

    t("checkDocumentDirty", "Check whether a document has unsaved changes",
      {"type": "object", "properties": {"filePath": {"type": "string"}},
       "required": ["filePath"]},
      _tool_check_dirty)

    t("saveDocument", "Save a document",
      {"type": "object", "properties": {"filePath": {"type": "string"}},
       "required": ["filePath"]},
      _tool_save_document)

    t("openDiff", "Open a diff review (blocking until the user accepts or rejects)",
      {"type": "object", "properties": {
          "old_file_path": {"type": "string"},
          "new_file_path": {"type": "string"},
          "new_file_contents": {"type": "string"},
          "tab_name": {"type": "string"},
      }, "required": ["old_file_path", "new_file_contents", "tab_name"]},
      lambda args, ctx: _tool_open_diff(args, ctx, pending))

    t("close_tab", "Close a tab by name",
      {"type": "object", "properties": {"tab_name": {"type": "string"}},
       "required": ["tab_name"]},
      _tool_close_tab)

    t("closeAllDiffTabs", "Close all diff tabs", obj,
      lambda args, ctx: f"CLOSED_{run_on_main(diff_view.close_all)}_DIFF_TABS")

    t("executeCode", "Execute python code in a Jupyter kernel",
      {"type": "object", "properties": {"code": {"type": "string"}}},
      lambda args, ctx: (_ for _ in ()).throw(
          ToolError("executeCode is not supported in Sublime Text")))


def _tool_get_current_selection(args, ctx):
    def op():
        window = sublime.active_window()
        view = window.active_view() if window else None
        if view is None:
            return {"success": False, "message": "no active editor"}
        return _selection_payload(view)

    return run_on_main(op)


SIDE_LAYOUT = {"cols": [0.0, 0.55, 1.0], "rows": [0.0, 1.0],
               "cells": [[0, 0, 1, 1], [1, 0, 2, 1]]}


def _side_group(window):
    """Group index for Claude-opened files: a right-hand pane, so the
    user's own tabs stay untouched. Creates the split on first use."""
    if not settings().get("open_in_side_group", True):
        return -1
    if window.num_groups() == 1:
        window.set_layout(SIDE_LAYOUT)
    return window.num_groups() - 1


def _tool_open_file(args, ctx):
    file_path = args.get("filePath")
    if not file_path or not os.path.exists(file_path):
        raise ToolError(f"file not found: {file_path}")
    preview = bool(args.get("preview", False))
    make_frontmost = bool(args.get("makeFrontmost", True))

    def op():
        window = sublime.active_window()
        flags = sublime.TRANSIENT if preview else 0
        group = _side_group(window)
        view = window.open_file(file_path, flags, group=group)
        if make_frontmost:
            window.focus_view(view)
            if hasattr(window, "bring_to_front"):
                window.bring_to_front()
        sublime.status_message(f"Claude opened: {os.path.basename(file_path)}")
        _select_when_loaded(
            view,
            args.get("startText"),
            args.get("endText"),
            bool(args.get("selectToEndOfLine", False)),
        )
        return view

    run_on_main(op)
    if make_frontmost:
        return f"Opened file: {file_path}"
    return {"success": True, "filePath": file_path,
            "languageId": run_on_main(lambda: _language_id(_find_view(file_path)))
            if _find_view(file_path) else "plaintext"}


def _select_when_loaded(view, start_text, end_text, to_eol, tries=100):
    if not start_text:
        return

    def attempt():
        if view.is_loading():
            if tries > 0:
                sublime.set_timeout(
                    lambda: _select_when_loaded(view, start_text, end_text, to_eol, tries - 1),
                    50)
            return
        start_region = view.find(start_text, 0, sublime.LITERAL)
        if start_region is None or start_region.a == -1:
            return
        end_point = start_region.b
        if end_text:
            end_region = view.find(end_text, start_region.b, sublime.LITERAL)
            if end_region is not None and end_region.a != -1:
                end_point = end_region.b
        if to_eol:
            end_point = view.line(end_point).b
        selection = sublime.Region(start_region.a, end_point)
        view.sel().clear()
        view.sel().add(selection)
        view.show_at_center(selection)

    attempt()


def _guess_language(file_name):
    ext = os.path.splitext(file_name)[1].lower().lstrip(".")
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "tga"):
        return "image"
    return ext or "plaintext"


def _tool_get_open_editors(args, ctx):
    # Tabs are sheets, not views: image tabs have sheet.view() == None and
    # would be invisible to a views()-based walk (found via live E2E).
    def op():
        tabs = []
        for window in sublime.windows():
            active_sheet = window.active_sheet()
            active_id = active_sheet.id() if active_sheet else -1
            for sheet in window.sheets():
                file_name = sheet.file_name()
                if not file_name:
                    continue
                view = sheet.view()
                tabs.append({
                    "uri": path_to_uri(file_name),
                    "isActive": sheet.id() == active_id,
                    "label": os.path.basename(file_name),
                    "languageId": _language_id(view) if view else _guess_language(file_name),
                    "isDirty": view.is_dirty() if view is not None else False,
                })
        return {"tabs": tabs}

    return run_on_main(op)


def _tool_get_workspace_folders(args, ctx):
    def op():
        folders = [
            {"name": os.path.basename(f) or f, "uri": path_to_uri(f), "path": f}
            for f in _all_folders()
        ]
        root = folders[0]["path"] if folders else ""
        return {"success": True, "folders": folders, "rootPath": root}

    return run_on_main(op)


def _tool_check_dirty(args, ctx):
    file_path = args.get("filePath", "")

    def op():
        view = _find_view(file_path)
        if view is None:
            return {"success": False, "message": f"Document not open: {file_path}"}
        return {"success": True, "filePath": file_path,
                "isDirty": view.is_dirty(), "isUntitled": False}

    return run_on_main(op)


def _tool_save_document(args, ctx):
    file_path = args.get("filePath", "")

    def op():
        view = _find_view(file_path)
        if view is None:
            return {"success": False, "message": f"Document not open: {file_path}"}
        view.run_command("save")
        return {"success": True, "filePath": file_path, "saved": True}

    return run_on_main(op)


def _tool_open_diff(args, ctx, pending):
    """Blocking diff review: park the request, build UI on main, defer."""
    old_path = args.get("old_file_path") or ""
    new_path = args.get("new_file_path")
    contents = args.get("new_file_contents", "")
    tab_name = args.get("tab_name") or "Claude diff"
    request_id = ctx["id"]
    client_id = ctx.get("client_id")

    pending.add(client_id, request_id, {"tab_name": tab_name})
    log(f"openDiff deferred: client=#{client_id} id={request_id} tab={tab_name!r}")

    def ui():
        try:
            diff_view.open_diff_ui(client_id, request_id, old_path, new_path,
                                   contents, tab_name)
        except Exception as exc:  # noqa: BLE001 - never leave a pending orphan
            log(f"openDiff UI failed: {exc}")
            _resolve_and_send(client_id, request_id, ["DIFF_REJECTED", tab_name])

    sublime.set_timeout(ui, 0)
    return DEFERRED


def _tool_close_tab(args, ctx):
    tab_name = args.get("tab_name", "")

    def op():
        if diff_view.close_tab(tab_name):
            return True
        for window in sublime.windows():
            for view in window.views():
                label = view.name() or (
                    os.path.basename(view.file_name()) if view.file_name() else "")
                if label == tab_name:
                    view.close()
                    return True
        return False

    run_on_main(op)
    return "TAB_CLOSED"
