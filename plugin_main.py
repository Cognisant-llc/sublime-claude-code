"""Sublime Text entry point for the Claude Code IDE integration plugin."""

import sublime
import sublime_plugin

from .adapters import sublime_bridge as bridge


def plugin_loaded():
    bridge.remember_main_thread()
    if bridge.settings().get("auto_start", True):
        sublime.set_timeout(_safe_start, 200)


def _safe_start():
    try:
        bridge.start()
    except Exception as exc:  # noqa: BLE001 - never break plugin load
        print(f"[ClaudeCodeIDE] start failed: {exc}")
        sublime.status_message(f"Claude Code IDE: start failed ({exc})")


def plugin_unloaded():
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


class ClaudeIdeEventListener(sublime_plugin.EventListener):
    def on_selection_modified_async(self, view):
        bridge.on_selection_modified(view)

    def on_activated_async(self, view):
        bridge.on_activated(view)
