"""Side-by-side diff review UI for the blocking openDiff tool.

Left pane: current file content (read-only). Right pane: Claude's proposal
in an editable scratch buffer — hand-edit freely, then Accept writes the
edited buffer to disk and answers FILE_SAVED; Reject answers DIFF_REJECTED.
Manually closing the proposal tab counts as Reject (no orphaned pending).

All functions here must run on the main thread (callers marshal).
"""

import os

import sublime

from ..claudeide.diffcalc import changed_new_lines, pick_target_path

REGION_KEY = "claude_diff_changed"

DIFF_LAYOUT = {"cols": [0.0, 0.5, 1.0], "rows": [0.0, 1.0], "cells": [[0, 0, 1, 1], [1, 0, 2, 1]]}

_diffs = {}         # tab_name -> record dict
_phantom_sets = {}  # right view id -> PhantomSet (must be retained)
_resolver = None    # set by bridge: fn(request_id, outcome_text)


def set_resolver(fn):
    global _resolver
    _resolver = fn


def _resolve(request_id, text):
    if _resolver is not None:
        _resolver(request_id, text)


def active_count():
    return len(_diffs)


# ---------- UI construction ----------


def open_diff_ui(request_id, old_file_path, new_file_path, new_file_contents, tab_name):
    window = sublime.active_window()
    prev_layout = window.layout()

    old_text = ""
    if old_file_path and os.path.exists(old_file_path):
        with open(old_file_path, encoding="utf-8", errors="replace") as fh:
            old_text = fh.read()

    target = pick_target_path(old_file_path, new_file_path)
    if tab_name in _diffs:
        tab_name = f"{tab_name} ({request_id})"
    syntax = _syntax_for(target)

    window.set_layout(DIFF_LAYOUT)

    window.focus_group(0)
    left = window.new_file()
    left.set_scratch(True)
    left.set_name(f"OLD: {os.path.basename(target)}")
    if syntax:
        left.assign_syntax(syntax)
    left.run_command("append", {"characters": old_text})
    left.set_read_only(True)

    window.focus_group(1)
    right = window.new_file()
    right.set_scratch(True)
    right.set_name(tab_name)
    if syntax:
        right.assign_syntax(syntax)
    right.run_command("append", {"characters": new_file_contents})
    right.settings().set("claude_diff_tab", tab_name)

    lines = changed_new_lines(old_text, new_file_contents)
    if lines:
        regions = [right.line(right.text_point(ln, 0)) for ln in lines]
        right.add_regions(REGION_KEY, regions, scope="markup.inserted",
                          flags=sublime.DRAW_NO_FILL)

    _add_action_phantom(right, tab_name)

    _diffs[tab_name] = {
        "request_id": request_id,
        "left_id": left.id(),
        "right_id": right.id(),
        "window_id": window.id(),
        "target": target,
        "prev_layout": prev_layout,
        "resolved": False,
    }
    right.show(sublime.Region(0, 0))
    window.focus_view(right)
    sublime.status_message(f"Claude diff: {tab_name} — Accept/Reject in the right pane")


def _add_action_phantom(view, tab_name):
    html = """
    <body id="claude-diff-actions">
      <style>
        body { padding: 6px 0; }
        a.btn { padding: 3px 12px; border-radius: 4px; text-decoration: none; }
        a.accept { background-color: #1c4d2e; color: #e8ffe8; }
        a.reject { background-color: #5a1f1f; color: #ffecec; }
        span.hint { color: color(var(--foreground) alpha(0.5)); font-size: 0.9em; }
      </style>
      <div>
        <a class="btn accept" href="accept">✓ Accept</a>&nbsp;&nbsp;
        <a class="btn reject" href="reject">✗ Reject</a>&nbsp;&nbsp;
        <span class="hint">Claude Code proposal — edit freely, Accept writes the file</span>
      </div>
    </body>"""
    ps = sublime.PhantomSet(view, "claude_diff_actions")
    phantom = sublime.Phantom(
        sublime.Region(0, 0), html, sublime.LAYOUT_BLOCK,
        on_navigate=lambda href, t=tab_name: _on_action(href, t),
    )
    ps.update([phantom])
    _phantom_sets[view.id()] = ps


def _on_action(href, tab_name):
    if href == "accept":
        accept(tab_name)
    elif href == "reject":
        reject(tab_name)


# ---------- outcomes ----------


def accept(tab_name):
    rec = _diffs.get(tab_name)
    if rec is None or rec["resolved"]:
        return False
    right = _view_by_id(rec["right_id"])
    if right is None:
        return reject(tab_name)
    content = right.substr(sublime.Region(0, right.size()))
    try:
        _write_target(rec["target"], content)
    except OSError as exc:
        sublime.error_message(f"Claude diff: writing {rec['target']} failed:\n{exc}")
        return False
    rec["resolved"] = True
    _resolve(rec["request_id"], "FILE_SAVED")
    _teardown(tab_name)
    sublime.status_message(f"Claude diff accepted → {os.path.basename(rec['target'])}")
    return True


def reject(tab_name):
    rec = _diffs.get(tab_name)
    if rec is None or rec["resolved"]:
        return False
    rec["resolved"] = True
    _resolve(rec["request_id"], "DIFF_REJECTED")
    _teardown(tab_name)
    sublime.status_message("Claude diff rejected")
    return True


def handle_view_close(view):
    """on_pre_close hook: manually closing the proposal tab = Reject."""
    vid = view.id()
    for tab_name, rec in list(_diffs.items()):
        if rec["right_id"] == vid and not rec["resolved"]:
            rec["resolved"] = True
            _resolve(rec["request_id"], "DIFF_REJECTED")
            sublime.set_timeout(lambda t=tab_name: _teardown(t), 0)
            return


def close_tab(tab_name):
    """Protocol close_tab for one of our diff tabs. Returns True if handled."""
    if tab_name in _diffs:
        reject(tab_name)      # resolves if still pending
        _teardown(tab_name)   # no-op if reject already tore down
        return True
    return False


def close_all():
    """Protocol closeAllDiffTabs. Returns number of tabs closed."""
    tabs = list(_diffs.keys())
    for tab_name in tabs:
        reject(tab_name)
        _teardown(tab_name)
    return len(tabs)


def close_all_silent():
    """Client disconnected: tear down UI without sending responses."""
    for tab_name, rec in list(_diffs.items()):
        rec["resolved"] = True
        _teardown(tab_name)


# ---------- internals ----------


def _write_target(target, content):
    existing = _find_view(target)
    if existing is not None and existing.is_dirty():
        existing.run_command("claude_ide_replace_content", {"text": content})
        existing.run_command("save")
        return
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    if existing is not None:
        existing.run_command("revert")


def _teardown(tab_name):
    rec = _diffs.pop(tab_name, None)
    if rec is None:
        return
    _phantom_sets.pop(rec["right_id"], None)
    for vid in (rec["left_id"], rec["right_id"]):
        view = _view_by_id(vid)
        if view is not None:
            view.set_scratch(True)
            view.close()
    window = _window_by_id(rec["window_id"])
    same_window_active = any(r["window_id"] == rec["window_id"] for r in _diffs.values())
    if window is not None and not same_window_active:
        window.set_layout(rec["prev_layout"])


def _syntax_for(path):
    try:
        return sublime.find_syntax_for_file(path)
    except Exception:  # noqa: BLE001 - older builds
        return None


def _view_by_id(view_id):
    for window in sublime.windows():
        for view in window.views():
            if view.id() == view_id:
                return view
    return None


def _window_by_id(window_id):
    for window in sublime.windows():
        if window.id() == window_id:
            return window
    return None


def _find_view(file_path):
    norm = os.path.normcase(os.path.normpath(file_path))
    for window in sublime.windows():
        for view in window.views():
            fn = view.file_name()
            if fn and os.path.normcase(os.path.normpath(fn)) == norm:
                return view
    return None
