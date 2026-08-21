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

# Cell index constants for layout manipulation (Origami-style)
XMIN, YMIN, XMAX, YMAX = range(4)

# Global 2-column layout for side-by-side diff
DIFF_LAYOUT = {"cols": [0.0, 0.5, 1.0], "rows": [0.0, 1.0], "cells": [[0, 0, 1, 1], [1, 0, 2, 1]]}

_diffs = {}         # tab_name -> record dict
_phantom_sets = {}  # right view id -> PhantomSet (must be retained)
_resolver = None    # set by bridge: fn(client_id, request_id, outcome_text)

# Track views before first diff opens (restored when last diff closes)
_saved_views = {}   # type: dict[int, int]  # view_id -> orig_group
_active_view_before_diff = None  # view_id of the active view before first diff opened

# Settings file name
SETTINGS_FILE = "Claude Code IDE.sublime-settings"


def set_resolver(fn):
    global _resolver
    _resolver = fn


def _resolve(client_id, request_id, text):
    if _resolver is not None:
        _resolver(client_id, request_id, text)


def active_count():
    return len(_diffs)


# ---------- UI construction ----------


def open_diff_ui(client_id, request_id, old_file_path, new_file_path,
                 new_file_contents, tab_name):
    window = sublime.active_window()
    prev_layout = window.layout()

    # Save the active view before changing layout (store in diff record for restoration)
    prev_active_view = window.active_view()

    # Save all sheets with their original group indices so we can restore
    # them when the diff closes (views get shuffled when layout changes).
    # Use view IDs since Sheet objects may become invalid after layout change.
    global _saved_views
    for sheet in window.sheets():
        group, _index = window.get_sheet_index(sheet)
        if group is not None:
            view = sheet.view()
            if view is not None:
                _saved_views[view.id()] = group

    old_text = ""
    if old_file_path and os.path.exists(old_file_path):
        with open(old_file_path, encoding="utf-8", errors="replace") as fh:
            old_text = fh.read()

    target = pick_target_path(old_file_path, new_file_path)
    if tab_name in _diffs:
        tab_name = f"{tab_name} ({request_id})"
    syntax = _syntax_for(target)

    # Determine whether to use global 2-column layout or split current pane
    use_side_group = _open_in_side_group()

    if use_side_group:
        # Use the fixed global 2-column layout (left: old, right: new)
        window.set_layout(DIFF_LAYOUT)
        left_group, right_group = 0, 1

    lines = changed_new_lines(old_text, new_file_contents)

    if use_side_group:
        # For side group mode, set up left/right views in their groups
        window.focus_group(left_group)
        left = window.new_file()
        left.set_scratch(True)
        left.set_name(f"OLD: {os.path.basename(target)}")
        if syntax:
            left.assign_syntax(syntax)
        left.run_command("append", {"characters": old_text})
        left.set_read_only(True)

        window.focus_group(right_group)
        right = window.new_file()
        right.set_scratch(True)
        right.set_name(tab_name)
        if syntax:
            right.assign_syntax(syntax)
        right.run_command("append", {"characters": new_file_contents})
        right.settings().set("claude_diff_tab", tab_name)
    else:
        # Split the source group (containing target file) 50/50 horizontally
        # Find the group containing the target file (or use active group)
        source_group = _find_view_group(window, target)
        if source_group is None:
            source_group = window.active_group()
        left, right = _split_group_horizontal(
            window, source_group, old_text, new_file_contents, target, tab_name, syntax
        )

    # Add diff highlighting and action phantoms to the right view (proposal)
    if lines:
        regions = [right.line(right.text_point(ln, 0)) for ln in lines]
        right.add_regions(REGION_KEY, regions, scope="markup.inserted",
                          flags=sublime.DRAW_NO_FILL)

    _add_action_phantom(right, tab_name)

    # Store the mode used so we can restore correctly
    diff_record = {
        "client_id": client_id,
        "request_id": request_id,
        "left_id": left.id(),
        "right_id": right.id(),
        "window_id": window.id(),
        "target": target,
        "prev_layout": prev_layout,
        "prev_active_view": prev_active_view,  # Store view object for restoration
        "use_side_group": use_side_group,
        "source_group": source_group if not use_side_group else None,
        "resolved": False,
    }
    _diffs[tab_name] = diff_record
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
    # Two content blocks (outcome + final contents): the client ignores a
    # bare FILE_SAVED and would re-prompt/retry the edit without the body.
    _resolve(rec["client_id"], rec["request_id"], ["FILE_SAVED", content])
    _teardown(tab_name)
    sublime.status_message(f"Claude diff accepted → {os.path.basename(rec['target'])}")
    return True


def reject(tab_name):
    rec = _diffs.get(tab_name)
    if rec is None or rec["resolved"]:
        return False
    rec["resolved"] = True
    _resolve(rec["client_id"], rec["request_id"], ["DIFF_REJECTED", tab_name])
    _teardown(tab_name)
    sublime.status_message("Claude diff rejected")
    return True


def handle_view_close(view):
    """on_pre_close hook: manually closing the proposal tab = Reject."""
    vid = view.id()
    for tab_name, rec in list(_diffs.items()):
        if rec["right_id"] == vid and not rec["resolved"]:
            rec["resolved"] = True
            _resolve(rec["client_id"], rec["request_id"], ["DIFF_REJECTED", tab_name])
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


def close_for_client(client_id):
    """One session disconnected: tear down only its tabs, silently
    (its pending entries are already resolved by the bridge)."""
    for tab_name, rec in list(_diffs.items()):
        if rec["client_id"] == client_id:
            rec["resolved"] = True
            _teardown(tab_name)


def close_all_silent():
    """Server stopping: tear down all diff UI without sending responses."""
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
        # Restore the original layout first (recreates all groups)
        window.set_layout(rec["prev_layout"])
        # Now move saved views back to their original groups
        global _saved_views
        # Use a copy of saved_views since we're clearing it after
        saved_copy = dict(_saved_views)
        for view in window.views():
            vid = view.id()
            if vid in saved_copy:
                orig_group = saved_copy[vid]
                try:
                    window.focus_view(view)
                    window.run_command("move_to_group", {"group": orig_group})
                except Exception:
                    pass
        # Clear saved views after restoration
        _saved_views.clear()

        # Restore focus to the view that was active before the diff opened
        prev_active = rec.get("prev_active_view")
        if prev_active is not None and prev_active.window() == window:
            window.focus_view(prev_active)


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


def settings():
    return sublime.load_settings(SETTINGS_FILE)


def _open_in_side_group():
    """Check the open_in_side_group setting. Returns True for global layout,
    False to split the current pane 50/50."""
    return settings().get("open_in_side_group", True)


def _find_view_group(window, file_path):
    """Find the group index containing a view with the given file path."""
    norm = os.path.normcase(os.path.normpath(file_path)) if file_path else ""
    for group in range(window.num_groups()):
        for view in window.views_in_group(group):
            fn = view.file_name()
            if fn and os.path.normcase(os.path.normpath(fn)) == norm:
                return group
    return None


def _split_group_horizontal(window, source_group, old_text, new_contents, target_path, tab_name, syntax):
    """Split the given group horizontally 50/50 and create left/right views.

    Uses Origami-style pane creation: splits the source group by inserting a column
    at the midpoint, creating two adjacent cells in the same row span.

    Returns (left_view, right_view) tuple where:
    - left_view contains old_text in a read-only scratch buffer
    - right_view contains new_contents with diff highlighting
    """
    layout = window.layout()
    cols = list(layout["cols"])
    rows = list(layout["rows"])
    cells = [list(c) for c in layout["cells"]]

    # Origami-style create_pane "right": pop the source cell, insert column, reinsert
    old_cell = cells.pop(source_group)  # Remove original cell from its position

    # Push right cells after and insert mid-column (like Origami's push_right_cells_after + cols.insert)
    for cell in cells:
        if cell[XMIN] >= old_cell[XMAX]:
            cell[XMIN] += 1
        if cell[XMAX] >= old_cell[XMAX]:
            cell[XMAX] += 1

    # Insert column at midpoint between left and right edges of source cell
    cols.insert(old_cell[XMAX], (cols[old_cell[XMIN]] + cols[old_cell[XMAX]]) / 2)

    # For "right" direction: focused = old_cell (left), unfocused = new_cell (right)
    new_cell = [old_cell[XMAX], old_cell[YMIN], old_cell[XMAX] + 1, old_cell[YMAX]]

    cells.insert(source_group, old_cell)   # Left cell stays at source position
    cells.append(new_cell)                  # Right cell goes to end (new group)

    new_layout = {"cols": cols, "rows": rows, "cells": cells}
    window.set_layout(new_layout)

    num_groups = len(cells)
    right_group = num_groups - 1

    # Left view in source group (shows old file content)
    window.focus_group(source_group)
    left = window.new_file()
    left.set_scratch(True)
    left.set_name(f"OLD: {os.path.basename(target_path)}")
    if syntax:
        left.assign_syntax(syntax)
    left.run_command("append", {"characters": old_text})
    left.set_read_only(True)

    # Right view in the new right group (shows proposed changes)
    window.focus_group(right_group)
    right = window.new_file()
    right.set_scratch(True)
    right.set_name(tab_name)
    if syntax:
        right.assign_syntax(syntax)
    right.run_command("append", {"characters": new_contents})
    right.settings().set("claude_diff_tab", tab_name)

    return left, right


def _find_view(file_path):
    norm = os.path.normcase(os.path.normpath(file_path))
    for window in sublime.windows():
        for view in window.views():
            fn = view.file_name()
            if fn and os.path.normcase(os.path.normpath(fn)) == norm:
                return view
    return None
