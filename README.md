# Claude Code IDE for Sublime Text

**English** | [日本語](./README.ja.md)

Native [Claude Code](https://claude.com/product/claude-code) IDE integration for Sublime Text 4 — in-editor diff review (accept/reject), live selection sharing, and `@`-mentions, speaking the same WebSocket/MCP protocol as the official VS Code and JetBrains extensions.

> **Unofficial community plugin** — not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic, PBC.

![Claude Code proposing an edit in Sublime Text: the terminal runs claude, a side-by-side diff opens in the editor, and Accept writes the file](docs/demo.gif)

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
2. Link it into Sublime's `Packages` as `ClaudeCodeIDE` (any folder name works — imports are relative — but this name keeps settings-file lookup consistent):
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

## FAQ

### Does Claude Code work with Sublime Text?

Yes — with this plugin. Claude Code has no built-in Sublime Text support (official extensions exist for VS Code and JetBrains), but its IDE integration is a documented WebSocket/MCP protocol. This plugin implements that protocol natively in Sublime, so features like in-editor diff review, selection context, and `@`-mentions work the same way they do in the official extensions.

### Is this an official Anthropic plugin?

No. It is an unofficial community plugin, not affiliated with or endorsed by Anthropic. It speaks the same protocol the official IDE extensions use, as documented by the [claudecode.nvim](https://github.com/coder/claudecode.nvim/blob/main/PROTOCOL.md) project.

### How do I connect Claude Code to Sublime Text?

Two ways:

1. **Manual**: run `claude` in any terminal, type `/ide`, and pick **Sublime Text**.
2. **Auto-connect**: set a fixed `"port"` in the plugin settings and export `CLAUDE_CODE_SSE_PORT=<port>` and `ENABLE_IDE_INTEGRATION=true` machine-wide. Every new `claude` session then attaches to Sublime automatically (~2 s after launch), from any terminal — including Terminus inside Sublime.

### What happens when Claude edits a file?

The proposed change opens as a side-by-side diff tab in Sublime. Accept it (`ctrl+alt+enter`), reject it (`ctrl+alt+backspace`), or edit the proposal by hand before accepting. Claude blocks until you decide — nothing touches disk without your review.

### Does my code get sent anywhere?

The plugin itself sends nothing to the network. It runs a WebSocket server on `127.0.0.1` (localhost only, token-authenticated) that talks to the Claude Code process on your machine. What Claude Code itself sends to Anthropic is governed by Claude Code, exactly as when you use it without an IDE.

### Can I run multiple Claude Code sessions at once?

Yes. The server accepts concurrent clients, so several Claude sessions (e.g. one per project or task) can attach to the same Sublime instance in parallel; diff reviews from each are tracked independently.

### Which platforms are supported?

The protocol core is dependency-free Python 3.8 (the Sublime Text 4 plugin host). Developed and tested end-to-end on Windows; macOS and Linux use the same code paths and are expected to work — issues and reports welcome.

### Why keep Sublime instead of an AI-native IDE?

Because the agent doesn't need to live inside the editor. Claude Code runs in a terminal; the editor's job is reading, judging, and occasionally hand-editing what the agent proposes — a job Sublime does instantly and in ~100–300 MB of RAM. Decoupling means you upgrade the agent and the editor independently, with no bundled subscription or lock-in. See [Motivation](#motivation--why-sublime-text-in-the-llm-era).

## License

MIT
