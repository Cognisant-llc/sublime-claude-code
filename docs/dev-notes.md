# 開発ノート — プロトコル知見・E2E 発見・落とし穴

> 最終更新: 2026-07-12 | 更新者: Claude session（初日実装）
> 関連: [launch-plan.md](./launch-plan.md), 実装計画 `~/.claude/plans/swift-wishing-turtle.md`

## プロトコル（一次情報: coder/claudecode.nvim PROTOCOL.md）

- lock: `~/.claude/ide/<port>.lock` = `{pid, workspaceFolders, ideName, transport:"ws", authToken(=secrets.token_hex(16))}`
- WS: 127.0.0.1 のみ、認証ヘッダー `x-claude-code-ide-authorization`、JSON-RPC 2.0 / MCP 2025-03-26
- 接続経路: (a) env `CLAUDE_CODE_SSE_PORT`+`ENABLE_IDE_INTEGRATION=true` で起動時自動接続 (b) 対話中 `/ide`
- openDiff は blocking → FILE_SAVED / DIFF_REJECTED。`close_tab` のみ snake_case。応答は content 配列ラップ

## 実機 E2E で判明したこと（2026-07-12）

1. **`claude -p`（print モード）は IDE 統合を初期化しない**（env/--ide とも不可）。E2E は対話モード必須
2. **env 自動接続は対話モードで起動 ~2 秒**。HKCU\Environment の永続 env を新規プロセスが継承することも実証
3. **画像タブは sheet（`sheet.view()` が None）** — views() 列挙では不可視。getOpenEditors/close 系は sheets ベースで書く（バグとして発見・修正済み）
4. **`subl --command "terminus_open {...}"` は実行されない**（terminus_open は外部 subl 経由で呼べない）。Launch コマンドはプラグイン内から `window.run_command` で
5. **プラグインのリロード/再起動でトークン再生成 → 既存セッションは切断**。新セッションは自動接続、既存は `/ide` で再接続
6. Windows の罠: **.cmd バッチは CRLF 必須**（LF だと静かに誤動作）、日本語引数は CP932 化け → ASCII に。explorer.exe 経由の .cmd 起動はセキュリティ確認で走らないことがある
7. ST の submodule は hot-reload されない → dev 時は sys.modules 掃除＋`sublime_plugin.reload_plugin`（User/_claude_ide_reload_tmp.py、M3 で製品化予定）

## 未検証（Open Questions）

- openDiff FILE_SAVED 後、Claude はディスク再読込するか（M2 実測残）→ 記事ネタ
- /ide 手動選択ルートの実機確認／cwd が lock workspaceFolders 外の場合の自動接続可否
- マルチウィンドウ: per-window server か active-window routing か（M3 判断）

## dev ワークフロー（現状）

- junction: `%APPDATA%\Sublime Text\Packages\ClaudeCodeIDE` → 本リポ
- 検証: `uv run pytest` → `subl --command reload_claude_ide_tmp` → `uv run python scripts/smoke.py` / `smoke_multi.py` / `smoke_diff.py`（対話）
- 状態確認: `subl --command dump_claude_ide_state_tmp` → `%TEMP%\claude_ide_state.json`
