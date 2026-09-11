"""Developer helper commands.

Shipped with the package because they are harmless for normal users and
essential for contributors: Sublime only hot-reloads *top-level* plugin
files, so editing anything under ``adapters/`` or ``claudeide/`` silently
keeps the old code running until the cached submodules are purged.
"""

import json
import os
import sys
import tempfile

import sublime
import sublime_plugin

_PKG = __package__.split(".")[0] if __package__ else "ClaudeCodeIDE"
_SELF = __name__


class ClaudeIdeDevReloadCommand(sublime_plugin.ApplicationCommand):
    """Fully reload the plugin, including cached submodules.

    Note: restarting the server regenerates the auth token, so Claude
    sessions connected before the reload must reconnect via `/ide`
    (new sessions auto-connect as usual).
    """

    def run(self):
        keep = (_PKG + ".plugin_main", _SELF)
        for name in list(sys.modules):
            if name.startswith(_PKG + ".") and name not in keep:
                del sys.modules[name]
        sublime_plugin.reload_plugin(_PKG + ".plugin_main")
        sublime.status_message(_PKG + ": fully reloaded")


class ClaudeIdeDevViewSettingCommand(sublime_plugin.ApplicationCommand):
    """Set a settings key on every view whose file basename matches
    (smoke scripts: probe per-view theme/colour behaviour)."""

    def run(self, basename, key, value):
        n = 0
        for window in sublime.windows():
            for view in window.views():
                if os.path.basename(view.file_name() or "") == basename:
                    view.settings().set(key, value)
                    n += 1
        print(f"[{_PKG}] {key}={value!r} on {n} view(s) named {basename}")


class ClaudeIdeDevRestoreTabsCommand(sublime_plugin.ApplicationCommand):
    """Two-phase repair for buffers ``set_name`` detached: back the buffer text
    up, close the buffer (scratch, no prompt), then reopen the file from disk.
    Log: <temp>/claude_ide_probe.json."""

    def run(self, paths_file, backup_dir):
        out = os.path.join(tempfile.gettempdir(), "claude_ide_probe.json")
        log = []
        with open(paths_file, encoding="utf-8") as fh:
            paths = json.load(fh)
        for path in paths:
            base = os.path.basename(path)
            for window in sublime.windows():
                for view in list(window.views()):
                    if view.file_name() or not (view.name() or "").endswith(base):
                        continue
                    bak = os.path.join(backup_dir, base + ".detached.txt")
                    with open(bak, "w", encoding="utf-8", newline="") as fh:
                        fh.write(view.substr(sublime.Region(0, view.size())))
                    view.set_scratch(True)
                    closed = view.close()
                    log.append({"path": path, "closed": closed, "backup": bak,
                                "valid_after": view.is_valid()})

        def reopen():
            for path in paths:
                if not os.path.exists(path):
                    log.append({"path": path, "reopen": "missing"})
                    continue
                window = sublime.active_window()
                have = any(v.file_name() == path for w in sublime.windows() for v in w.views())
                if not have:
                    window.open_file(path)
                log.append({"path": path, "reopened": not have})
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(log, fh, ensure_ascii=False)

        sublime.set_timeout(reopen, 800)


class ClaudeIdeDumpStateCommand(sublime_plugin.ApplicationCommand):
    """Dump server/window state to <temp>/claude_ide_state.json.

    Machine-readable observability for the smoke scripts (scripts/smoke*.py)
    and for bug reports.
    """

    def run(self):
        out = os.path.join(tempfile.gettempdir(), "claude_ide_state.json")
        try:
            from .adapters import sublime_bridge as bridge

            sheets = []
            for window in sublime.windows():
                for sheet in window.sheets():
                    fn = sheet.file_name()
                    view = sheet.view()
                    diff_tab = view.settings().get("claude_diff_tab") if view else None
                    if fn or diff_tab:
                        group, _index = window.get_sheet_index(sheet)
                        sheets.append(
                            {
                                "file": os.path.basename(fn) if fn else None,
                                "group": group,
                                "has_view": view is not None,
                                "diff_tab": diff_tab,
                                "session": view.settings().get("claude_session") if view else None,
                                "name": view.name() if view else None,
                                "dirty": view.is_dirty() if view else None,
                                "window": window.id(),
                            }
                        )
            buffers = [{"name": v.name(), "dirty": v.is_dirty(), "scratch": v.is_scratch(),
                        "window": w.id(), "group": w.get_view_index(v)[0], "size": v.size()}
                       for w in sublime.windows() for v in w.views()
                       if not v.file_name() and not v.settings().get("claude_activity_panel")]
            transients = []
            for window in sublime.windows():
                if not hasattr(window, "transient_view_in_group"):
                    break
                for group in range(window.num_groups()):
                    view = window.transient_view_in_group(group)
                    if view is not None and view.file_name():
                        transients.append(
                            {"file": os.path.basename(view.file_name()), "group": group}
                        )
            panels = []
            for window in sublime.windows():
                for view in window.views():
                    if not view.settings().get("claude_activity_panel"):
                        continue
                    longest = max(view.lines(sublime.Region(0, view.size())),
                                  key=lambda r: r.size(), default=sublime.Region(0, 0))
                    xs = [view.text_to_layout(pt)[0] for pt in range(longest.a, longest.b + 1)]
                    ys = [view.text_to_layout(pt)[1] for pt in range(longest.a, longest.b + 1)]
                    wrap_at = next((i for i, y in enumerate(ys) if y > ys[0]), None)
                    panels.append(
                        {
                            "group": window.get_view_index(view)[0],
                            "layout_cols": window.get_layout().get("cols"),
                            "em_width": view.em_width(),
                            "viewport": list(view.viewport_extent()),
                            "layout_extent": list(view.layout_extent()),
                            "longest_chars": longest.size(),
                            "longest_x_max": max(xs) if xs else None,
                            "wrap_at_char": wrap_at,
                            "adv_probe": (view.text_to_layout(1)[0] - view.text_to_layout(0)[0]),
                            "text_width": view.settings().get("claude_activity_width"),
                            "summary": view.settings().get("claude_activity_summary"),
                            "targets": view.settings().get("claude_activity_targets"),
                            "text": view.substr(sublime.Region(0, view.size())),
                            "line_height": view.line_height(),
                            "lines": view.rowcol(view.size())[0] + 1,
                        }
                    )
            state = {
                "running": bridge.is_running(),
                "port": bridge.server_port(),
                "clients": bridge.client_count(),
                "num_groups": sublime.active_window().num_groups(),
                "sheets": sheets,
                "transients": transients,  # preview tabs are invisible to sheets()
                "buffers": buffers,  # unsaved / detached buffers (no file)
                "raw": [{"window": w.id(), "active": w.id() == sublime.active_window().id(),
                         "sheets": len(w.sheets()), "views": len(w.views()),
                         "groups": [len(w.sheets_in_group(g)) for g in range(w.num_groups())],
                         "names": [(v.id(), os.path.basename(v.file_name() or ""), v.name())
                                   for v in w.views()]}
                        for w in sublime.windows()],
                "activity_panels": panels,
                "ui": sublime.ui_info() if hasattr(sublime, "ui_info") else None,
                "theme_files": sublime.find_resources("*.sublime-theme"),
                "windows": [
                    {
                        "id": w.id(),
                        "active": w.id() == sublime.active_window().id(),
                        "sidebar_visible": w.is_sidebar_visible(),
                        "project": os.path.basename(w.project_file_name() or ""),
                        "folders": len(w.folders()),
                    }
                    for w in sublime.windows()
                ],
            }
        except Exception as exc:  # noqa: BLE001 - always produce a file
            state = {"error": str(exc)}
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        # Console-only trace: a status_message here would spam the status bar
        # when smoke/dev scripts poll this command.
        print(f"[{_PKG}] state dumped to {out}")
