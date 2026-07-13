# Don't Switch Your Editor — Connect the Agent

*Why we wired Claude Code into Sublime Text instead of moving to an AI-native IDE — and why the coupling, not the editor, is the thing to choose.*

You switched editors for the AI. Now that editor is the heaviest app on your machine, and last year it changed what "unlimited" means.

In June 2025, Cursor moved its $20 Pro plan to what was effectively usage-based pricing; "unlimited" turned out to mean unlimited only on Auto mode. The backlash was loud enough that the CEO apologized and the company refunded surprise charges from the transition window ([Cursor's own post](https://cursor.com/blog/june-2025-pricing), [TechCrunch](https://techcrunch.com/2025/07/07/cursor-apologizes-for-unclear-pricing-changes-that-upset-users/)). Around the same time, "I left Cursor because it got heavy" posts became a genre of their own.

Here's the claim of this essay: **that fatigue is not about any particular product. It's a property of coupling the agent to the editor.** And as of 2026, you can choose the other structure.

*(Bias disclosure: we build LLM systems for a living, let agents write most of our code, and I have personally used Sublime Text for years. Discount accordingly. This is an argument about structure, not a plea for my favorite editor.)*

## AI-native editors get heavy and expensive for structural reasons

Three couplings, three consequences:

1. **Revenue coupling.** In 2025, an AI editor's subscription was largely a resale of model inference: when model costs or usage patterns shifted, the editor had two levers — raise prices or tighten limits. June 2025 wasn't greed so much as that structure becoming visible. Note what happened next: Cursor answered the resale squeeze by building [its own models](https://cursor.com/blog/composer). A rational move — and one that couples you *harder*: now choosing the editor chooses your model, and choosing the model chooses your editor. Vertical integration means both are hostages together.
2. **Technical coupling.** Most AI-native editors are VS Code forks: an Electron shell, plus an always-on codebase indexer, plus AI UI — and the permanent tax of tracking upstream. "It got slower with every update" is what this architecture feels like from the inside.
3. **Choice coupling.** In the bundled world, switching agents means switching editors. Your keybindings, extensions, and muscle memory are hostages. New coding agents ship monthly now; re-homing your entire environment each time doesn't scale. It's an N agents × M editors problem.

That last multiplication should sound familiar. Language support × editors was the same shape, and the [Language Server Protocol](https://en.wikipedia.org/wiki/Language_Server_Protocol) collapsed it from M×N to M+N in 2016: write the server once, run it in any editor.

## The same decoupling is happening to agents, right now

Two concrete threads:

- **[ACP (Agent Client Protocol)](https://zed.dev/acp)** — started by Zed in 2025, joined by JetBrains ([announcement](https://blog.jetbrains.com/ai/2025/10/jetbrains-zed-open-interoperability-for-ai-coding-agents-in-your-ide/)), with an in-IDE [agent registry](https://blog.jetbrains.com/ai/2026/01/acp-agent-registry/) since January 2026. Ecosystem-wide there are already [dozens of implementations](https://agentclientprotocol.com/get-started/clients), including community clients for Neovim and Emacs.
- **Claude Code's IDE protocol** — Claude Code lives in a terminal and doesn't need an editor at all. Its official VS Code and JetBrains extensions talk to it over a thin WebSocket/MCP protocol, which the community has documented ([claudecode.nvim's PROTOCOL.md](https://github.com/coder/claudecode.nvim/blob/main/PROTOCOL.md)).

The quiet shift of 2025–26: **the agent became a product independent of the editor.** For the first time, the coupling itself is a choice.

## The decoupled stack

Ours looks like this:

```
┌─────────────────────────┐     thin protocol      ┌──────────────────────────┐
│  Agent (terminal)       │  WebSocket + JSON-RPC  │  Your editor             │
│  plans, edits, verifies │ ◄────────────────────► │  read, judge, hand-edit  │
│  swap it anytime        │  selection / tabs /    │  10 years of muscle      │
│                         │  diff review           │  memory intact           │
└─────────────────────────┘                        └──────────────────────────┘
```

The agent runs in the terminal. The editor is where a human reads, judges, and occasionally fixes by hand. A thin layer shuttles selection, open tabs, and diff proposals between them. The experience is close to an AI editor; the structure is not:

| | Coupled (AI-native IDE) | Decoupled (agent + protocol + editor) |
|---|---|---|
| Switch agents | move your whole environment | change a terminal command |
| Switch editors | effectively locked in | anytime — the agent comes along |
| Pricing | model costs bundled into editor sub | agent contract only; editor can be a one-time buy |
| Weight | indexer + AI UI, growing | editor stays stock; agent is a separate process |
| When something breaks | wait for an update | fix a thin layer yourself |

"Don't switch your editor" does **not** mean "use Sublime". It means the agent should no longer decide your editor for you. If you love Zed, use Zed — the agent follows. Decoupling includes the freedom to leave *and* the freedom to stay.

## We tested how thin the layer really is

Claims about "thin layers" deserve numbers, so we built one: this repository — a Sublime Text 4 client for Claude Code's IDE protocol.

- **~1,800 lines total, zero dependencies** (pure-stdlib Python 3.8): WebSocket server, JSON-RPC, MCP, lock-file discovery, side-by-side diff review with accept/reject, multi-session support
- **~2 days** from first line to working end-to-end, pairing with the agent itself
- **164 MB** — measured RAM of the entire editor while writing this (Sublime, two windows, LSP running, this plugin connected)

Two caveats on that last number, because it's easy to over-read. The lightness is mostly Sublime being native (non-Electron), not decoupling magic — decoupling's contribution is that *you keep the freedom to pick a light editor at all*. And the agent still costs RAM in its own process (~500 MB per session measured here); decoupling doesn't delete that footprint, it moves it out of the place where you read and judge code.

Honesty requires the cost side too: **decoupling means you own the protocol drift.** The documented protocol and the real client already disagreed in one place — the diff-review response needs two content blocks, not the documented one — and we only caught it by testing against the real client ([dev notes](./dev-notes.md)). When the official side changes, we chase. That's the deal.

One more thing, said before someone else says it: this layer speaks Claude Code's *native* protocol, while the industry is converging on ACP — and Claude Code itself already speaks ACP inside Zed ([official adapter](https://zed.dev/blog/claude-code-via-acp)). So our layer may well be the thing that gets replaced. That's fine; it's the point. **When the layer is thin, switching layers is thin too.**

But when something breaks, the thing you debug is an 1,800-line layer, not a forked IDE. LSP's history suggests these protocols stabilize as implementations multiply. The direction is set.

## Where coupling still wins

Fairness section. Millisecond-latency tab completion — the thing Cursor's Tab is genuinely great at — needs deep editor integration; a decoupled stack won't replicate it. Teams that want one standardized environment, or developers who'd rather not live near a terminal, get real value from the bundle.

So the line is roughly: **if your center of value is completion-while-typing, stay coupled; if your workflow is shifting toward delegating design/implementation/verification to an agent, decouple.** The center of gravity of coding is shifting toward the second mode — and there, what you need from an editor is fast startup, readable diffs, and staying out of the way.

## Ten years of muscle memory is an appreciating asset

An editor is not just a tool; it's an extension of your hands. The keybindings you've tuned for a decade and the little plugins you wrote for yourself are not sunk costs — they compound.

Our answer for the AI era isn't "move editors every year". It's: **let the agent evolve outside the editor, keep the editor that fits your hands, connect them with a thin protocol.** This repo is that layer for Sublime Text. The [README](../README.md) has the 4-step setup.

---

*日本語版（より詳しい構造論）: [エディタを替えるな、エージェントを繋げ — Cognisant Insights](https://cognisant.io/insights/editor-agent-decoupling)*

*Written by [Daichi Kudo](https://github.com/Daichi-Kudo) (Cognisant LLC), with AI assistance; facts checked against the linked sources, measurements taken on the author's machine, 2026-07.*
