# 開発ノート — プロトコル知見・E2E 発見・落とし穴

> 最終更新: 2026-07-12 | 更新者: Claude session（初日実装）

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
7. ST の submodule は hot-reload されない → dev 時は sys.modules 掃除＋`sublime_plugin.reload_plugin`（`dev_commands.py` の `claude_ide_dev_reload` として製品化済み）

## 実 claude での diff レビュー E2E（2026-07-12、デモ撮影で発見・修正）

8. **openDiff の応答は content 2要素が必須**: `["FILE_SAVED", <保存後の全文>]` / `["DIFF_REJECTED", <tab_name>]`。
   **PROTOCOL.md は1要素しか書いていない（ドキュメントドリフト）** — 正は claudecode.nvim の実装（diff.lua）。
   1要素だけ返すと実 claude は許可質問に留まり続け、編集をリトライして openDiff を再送する（tab hash が増殖）。
   自作スモーククライアントは形を検証しないため気づけない＝**実クライアント E2E でのみ捕捉可能だった**
9. **`bypassPermissions` モードでは openDiff レビューは発生しない**（Edit が即ディスク書き込み）。
   デモ/検証は project-local `.claude/settings.json` で `defaultMode: "default"` にして行う
10. **Sublime が env 登録より前から起動していると Terminus のシェルは CLAUDE_CODE_SSE_PORT を継承しない**
    → `terminus_open` の `env` パラメータで注入可（ST 再起動不要）
11. **クライアントプロセスを強制 kill すると WS がゾンビ化**（clients カウント残留・status ⚡ のまま）。
    次の書き込みで reap される。即クリアはサーバー reload
12. claude の初回オンボーディング（フォルダ信頼「Security guide」・「use my browser」）は**フォルダ/状態ごとに TUI をブロック**する
    → デモ撮影前にウォームアップ起動で消化しておく。なお lock の workspaceFolders はウィンドウ/フォルダ変化で自動更新される（実測）

## 未検証（Open Questions）

- openDiff FILE_SAVED 後、Claude はディスク再読込するか（応答2要素目に全文を返すので実質解消。厳密な再読込タイミングは未計測）
- /ide 手動選択ルートの実機確認
- マルチウィンドウ: per-window server か active-window routing か（M3 判断）
- accept 時に即 UI teardown している（claudecode.nvim は close_tab 到着まで保持）。close_tab ハンドラが寛容なので実害は未観測だが、將来 close_tab 駆動へ寄せるか要検討

## デモ GIF 撮影パイプライン（再現手順）

- シーン: `~/demo/greet.py`＋project-local settings（defaultMode: default）。User/`_demo_claude_tmp.py`（撮影後削除）が terminus_open(env注入)＋タイピングアニメ＋accept を提供
- 録画: ffmpeg gdigrab 領域 (8,56) 1264x736（DWM 不可視境界 8px を考慮、タイトルバー除外）、h264_nvenc、-t 固定で自己finalize
- 編集: trim+setpts で可変速（待ち5倍速・見せ場等速）→ palettegen/paletteuse で GIF（960px/12fps/~640KB）
- 罠: SetForegroundWindow はフォアグラウンドロックで無効 → 邪魔な窓（Chrome）は SW_MINIMIZE で退かす／`subl` への `~` パスは `C:\c\...` に化ける（Windows 形式パスを渡す）

## dev ワークフロー

- junction: `%APPDATA%\Sublime Text\Packages\ClaudeCodeIDE` → 本リポ
- **一発検証**: `uv run python scripts/dev_check.py`（pytest → `claude_ide_dev_reload` → 状態 dump → smoke）。unit のみは `--no-live`
- 個別: `subl --command claude_ide_dev_reload`（submodule 込み reload）／`subl --command claude_ide_dump_state` → `%TEMP%\claude_ide_state.json`／`scripts/smoke_multi.py`・`smoke_diff.py`（対話）
- dev コマンド2枚はパッケージ同梱（`dev_commands.py`）。旧 User/_claude_ide_*_tmp.py は削除済み（2026-07-12）
