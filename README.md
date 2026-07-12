# Claude Code IDE for Sublime Text

**English** | [日本語](./README.ja.md)

Native [Claude Code](https://claude.com/product/claude-code) IDE integration for Sublime Text 4 — in-editor diff review (accept/reject), live selection sharing, and `@`-mentions, speaking the same WebSocket/MCP protocol as the official VS Code and JetBrains extensions.

> **Unofficial community plugin** — not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic, PBC.

**Status: core features working** — server + context sharing (M1), in-editor diff review (M2), and parallel multi-session support are implemented and tested end-to-end. Polish phase (M3) in progress; not yet on Package Control.

## Motivation — why Sublime Text in the LLM era?

Now that an agent writes much of the code, what should an editor be? Our answer:

- **Instant and lightweight.** Sublime opens in a blink and stays at ~100–300 MB while agent sessions, terminals, and browsers eat the rest of your RAM. The editor is where you *read and judge* code; it should never be the heavy part of the stack.
- **Trivially extensible by you.** In an LLM-first workflow, the editor is personal infrastructure: when Claude can write a Sublime plugin in minutes, a scriptable Python API beats a marketplace of prebuilt features. You compose exactly the cockpit you want — this plugin itself is proof.
- **No bundled AI, by choice.** AI-native IDEs (Cursor, Windsurf, …) and VS Code couple the agent to the editor — with their own subscription, model markup, and upgrade cadence. Claude Code is editor-agnostic; the missing piece was only the thin protocol layer that lets Sublime *talk* to it. This plugin adds that layer, so the editor stays fast and yours, and the agent stays first-class.

## What it does

When Claude Code connects (via `/ide` or auto-connect), the plugin provides:

- **In-editor diff review** — Claude's proposed edits open as a side-by-side diff; accept, reject, or hand-edit before accepting (M2)
- **Context sharing** — current selection, open tabs, workspace folders, dirty state
- **`selection_changed` streaming** — Claude always knows what you're looking at
- **`@`-mention** — send the current selection range to the prompt
- Lock-file discovery — works from Terminus inside Sublime *or* any external terminal

## Install (manual, while in development)

1. Clone this repo anywhere.
2. Link it into Sublime's `Packages` as `ClaudeCodeIDE` (name matters — it becomes the Python package name):
   - **Windows**: `mklink /J "%APPDATA%\Sublime Text\Packages\ClaudeCodeIDE" "C:\path\to\repo"`
   - **macOS/Linux**: `ln -s /path/to/repo "~/Library/Application Support/Sublime Text/Packages/ClaudeCodeIDE"`
3. Restart Sublime Text. The status bar shows `Claude ○ :<port>` when the server is listening.
4. In any terminal, run `claude`, then `/ide` and pick **Sublime Text**.

## Development

Protocol core (`claudeide/`) is pure Python 3.8 with zero dependencies and never imports `sublime`, so it is unit-testable outside Sublime:

```bash
uv venv --python 3.8
uv pip install pytest
uv run pytest
```

The Sublime-facing layer lives in `adapters/sublime_bridge.py` + `plugin_main.py`.

## Protocol

Implements Claude Code's IDE integration protocol (WebSocket + [MCP](https://modelcontextprotocol.io) 2025-03-26): lock file at `~/.claude/ide/<port>.lock`, localhost-only WebSocket with `x-claude-code-ide-authorization`, and the standard tool set (`openFile`, `openDiff`, `getCurrentSelection`, `getOpenEditors`, …).

Protocol reference: [coder/claudecode.nvim PROTOCOL.md](https://github.com/coder/claudecode.nvim/blob/main/PROTOCOL.md) — huge thanks to that project for documenting it.

## License

MIT
