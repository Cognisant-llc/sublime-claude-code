"""Sublime Text entry point for the Claude Code IDE integration plugin."""

import sublime
import sublime_plugin

from .adapters import activity_panel, diff_view
from .adapters import sublime_bridge as bridge


def plugin_loaded():
    bridge.remember_main_thread()
    if bridge.settings().get("auto_start", True):
        sublime.set_timeout(_safe_start, 200)
    sublime.set_timeout(_safe_start_activity, 400)


def _safe_start():
    try:
        bridge.start()
    except Exception as exc:  # noqa: BLE001 - never break plugin load
        print(f"[ClaudeCodeIDE] start failed: {exc}")
        sublime.status_message(f"Claude Code IDE: start failed ({exc})")


def _safe_start_activity():
    try:
        activity_panel.start()
    except Exception as exc:  # noqa: BLE001 - the panel must never break the server
        print(f"[ClaudeCodeIDE] activity panel start failed: {exc}")


def plugin_unloaded():
    try:
        activity_panel.stop()
    except Exception as exc:  # noqa: BLE001
        print(f"[ClaudeCodeIDE] activity stop failed: {exc}")
    try:
        bridge.stop()
    except Exception as exc:  # noqa: BLE001
        print(f"[ClaudeCodeIDE] stop failed: {exc}")


class ClaudeIdeStartCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        _safe_start()
        sublime.status_message(bridge.status_summary().split("\n")[0])

    def is_enabled(self):
        return not bridge.is_running()


class ClaudeIdeStopCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        bridge.stop()
        sublime.status_message("Claude Code IDE: stopped")

    def is_enabled(self):
        return bridge.is_running()


class ClaudeIdeStatusCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        sublime.message_dialog(bridge.status_summary())


class ClaudeIdeCopyLaunchCommand(sublime_plugin.ApplicationCommand):
    """Copy an env-prefixed `claude` launch line for any terminal (git-bash)."""

    def run(self):
        line = bridge.launch_env_line()
        if line is None:
            sublime.status_message("Claude Code IDE: server not running")
            return
        sublime.set_clipboard(line)
        sublime.status_message("Claude Code IDE: launch command copied")

    def is_enabled(self):
        return bridge.is_running()


class ClaudeIdeAtMentionCommand(sublime_plugin.TextCommand):
    """Send the current selection to Claude as an @-mention."""

    def run(self, edit):
        bridge.send_at_mention(self.view)


class ClaudeIdeReplaceContentCommand(sublime_plugin.TextCommand):
    """Replace the whole buffer (used when accepting a diff into a dirty view)."""

    def run(self, edit, text):
        self.view.replace(edit, sublime.Region(0, self.view.size()), text)


class ClaudeIdeDiffAcceptCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        tab = self.view.settings().get("claude_diff_tab")
        if tab:
            diff_view.accept(tab)

    def is_enabled(self):
        return bool(self.view.settings().get("claude_diff_tab"))


class ClaudeIdeDiffRejectCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        tab = self.view.settings().get("claude_diff_tab")
        if tab:
            diff_view.reject(tab)

    def is_enabled(self):
        return bool(self.view.settings().get("claude_diff_tab"))


# ---------- Recent Activity panel ----------


def _is_panel(view):
    return bool(view and view.settings().get(activity_panel.PANEL_SETTING))


class ClaudeIdeActivityOpenCommand(sublime_plugin.WindowCommand):
    """Open (or focus) the Recent Activity panel in this window."""

    def run(self):
        if not activity_panel.is_running():
            _safe_start_activity()
        activity_panel.open_panel(self.window, focus=True)


class ClaudeIdeActivityRefreshCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = activity_panel.find_panel(self.window)
        if view is None:
            view = activity_panel.open_panel(self.window, focus=False)
        activity_panel.refresh(view)

    def is_enabled(self):
        return activity_panel.is_running()


class ClaudeIdeActivityToggleCodeCommand(sublime_plugin.WindowCommand):
    """Show every file type, or only documents & media (default)."""

    def run(self):
        view = activity_panel.find_panel(self.window)
        if view is not None:
            activity_panel.toggle_code(view)

    def is_enabled(self):
        return activity_panel.find_panel(self.window) is not None


class ClaudeIdeActivitySetWindowCommand(sublime_plugin.WindowCommand):
    """Change the time window (hours) of the panel."""

    def run(self, hours):
        view = activity_panel.find_panel(self.window)
        if view is not None:
            activity_panel.set_window_hours(view, hours)

    def is_enabled(self, hours=24):
        return activity_panel.find_panel(self.window) is not None


class ClaudeIdeActivityActivateCommand(sublime_plugin.TextCommand):
    """Enter / click inside the panel: open the file or fold a project.
    Bound to a single click by Default.sublime-mousemap (event → point)."""

    def want_event(self):
        return True

    def run(self, edit, point=None, event=None):
        if point is None and event and "x" in event and "y" in event:
            point = self.view.window_to_text((event["x"], event["y"]))
        activity_panel.activate(self.view, point)

    def is_enabled(self):
        return _is_panel(self.view)


class ClaudeIdeActivityHideCommand(sublime_plugin.TextCommand):
    """Hide the project under the caret from the panel (persists)."""

    def run(self, edit, point=None):
        activity_panel.hide_project_at(self.view, point)

    def is_enabled(self):
        return _is_panel(self.view)


class ClaudeIdeActivityShowHiddenCommand(sublime_plugin.WindowCommand):
    """Un-hide every project hidden with the hide command."""

    def run(self):
        activity_panel.unhide_all()

    def is_enabled(self):
        return activity_panel.is_running()


class ClaudeIdeActivityEditHiddenCommand(sublime_plugin.ApplicationCommand):
    """Open the settings so the always-hidden `hide_projects` list can be edited."""

    def run(self):
        sublime.run_command("edit_settings", {
            "base_file": "${packages}/Claude Code IDE/Claude Code IDE.sublime-settings",
            "default": ('// Settings in here override those in "Claude Code IDE'
                        '/Claude Code IDE.sublime-settings"\n{\n\t"activity_panel": '
                        '{\n\t\t"hide_projects": [$0]\n\t}\n}\n'),
        })


class ClaudeIdeActivityQuickCommand(sublime_plugin.WindowCommand):
    """Fuzzy list of recent files (newest first) — no screen space needed."""

    def run(self):
        if not activity_panel.is_running():
            _safe_start_activity()
        items = activity_panel.flat_items(window=self.window)
        if not items:
            sublime.status_message("Recent Activity: nothing in the current window")
            return

        def on_done(index):
            if index >= 0:
                activity_panel.open_target(self.window, items[index][2])

        self.window.show_quick_panel([[name, detail] for name, detail, _ in items], on_done)


class ClaudeIdeEventListener(sublime_plugin.EventListener):
    def on_selection_modified_async(self, view):
        bridge.on_selection_modified(view)

    def on_activated_async(self, view):
        bridge.on_activated(view)

    def on_activated(self, view):
        if _is_panel(view):
            activity_panel.render_view(view)
            # everything is now "seen": clear the * markers on the next render
            sublime.set_timeout(lambda: activity_panel.mark_seen(view), 0)
        else:
            activity_panel._update_status(view)

    def on_deactivated(self, view):
        if _is_panel(view):
            activity_panel.render_view(view)

    def on_text_command(self, view, name, args):
        # double-click = drag_select by words; turn it into "activate this line"
        if _is_panel(view) and name == "drag_select" and (args or {}).get("by") == "words":
            event = (args or {}).get("event") or {}
            if "x" in event and "y" in event:
                point = view.window_to_text((event["x"], event["y"]))
                return ("claude_ide_activity_activate", {"point": point})
        return None

    def on_pre_close(self, view):
        diff_view.handle_view_close(view)
