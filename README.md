# LocalCoder

ローカルLLM (Windows側 Ollama) だけで動く、GUIコーディングエージェント。
codex / claude code のようにファイル読み書き・コマンド実行を全自動で行う。
外部のLLM APIやクラウドAIは使わない。Python側は標準ライブラリのみで動作する。
ただし、web_search / fetch_url を使うと検索語やアクセス先URLはインターネットへ送信される。

> ## ⚠️⚠️⚠️ 重要な警告：これは「承認なしでコマンドを実行するエージェント」です ⚠️⚠️⚠️
>
> **`run_command` ツールにサンドボックスはありません。** LLMが実行するコマンドは
> **あなたのユーザーアカウントと全く同じ権限**で、承認プロンプト無しにそのまま
> 実行されます。制限されているのは「ファイル読み書き（read_file/write_file/
> list_dir）の対象パス」が作業フォルダ配下に限られることだけです。
>
> **`run_command` はこの制限を受けません。** 作業フォルダの外のファイルを消す、
> 上書きする、`git push --force` する、`curl`で外部にデータを送信する、
> システム設定を変更する——技術的にはすべて可能です。CSRF/XSS/DNSリバインディング
> 対策は「悪意あるWebページが無断でLocalCoderを操作すること」を防ぎますが、
> **LLM自身が誤って（あるいは指示を誤解して）危険なコマンドを実行することは防げません。**
>
> - 重要なファイルがあるフォルダを作業フォルダに指定しない、または事前にバックアップ/
>   git管理下に置く
> - 破壊的な操作（削除・上書き・公開push等）を頼むときは指示を具体的にする
> - 信頼できないモデル・素性の分からないカスタムモデルは使わない
> - 本番環境・共有サーバー・重要データのあるマシンでは実行しない
>
> **自己責任でご利用ください。**

## この README の役割

この README は、LocalCoder の入口として現在の全体像、起動方法、主要機能、文書の読み分けだけを扱う。実装の詳細、再構築手順、改善計画は末尾の関連ドキュメントへ分離する。

## 起動方法

デスクトップの **LocalCoder.bat** をダブルクリック。
(WSL内でサーバーが起動し、Edgeのアプリウィンドウが開く)

手動起動する場合:

```
wsl -d ubuntu-24.04 -- bash -lc "python3 ~/localcoder/server.py"
→ ブラウザで http://localhost:8765
```

## 使い方

1. 画面上部でモデルを選択 (推奨: gpt-oss:20b。ツール呼び出しが最も安定)
2. 作業フォルダを指定。手入力のほか「📁 参照」ボタンでフォルダ選択ダイアログが
   開く (`$HOME` および Windowsドライブ `/mnt/c` 等の配下を移動可能)。未入力時は
   環境変数 `LOCALCODER_DEFAULT_WORKSPACE`の値、無ければ`$HOME`が自動で入る
3. やりたいことを日本語で入力して送信
4. エージェントが自動でファイル作成・編集・コマンド実行・検証まで行う
   (承認プロンプトは一切なし。ツール実行内容は 🔧 カードで確認できる)

## 主な機能

- Ollama のツール呼び出しを使った自律的なファイル編集、コマンド実行、検証
- ブラウザGUI、SSEストリーミング、会話履歴、完了通知
- ワークスペース制限付きファイル操作と、ターン単位の undo / redo
- 履歴圧縮、空応答回復、再試行、反復失敗検知、作業状態ダッシュボード
- Web検索、ページ取得、PDFテキスト抽出、画像入力
- Windowsドライブ上のファイル操作と PowerShell 呼び出し
- stdio / JSON-RPC 2.0 による信頼済みローカルMCP接続

一般の外部MCP接続（HTTPS、OAuth、承認、監査）はまだ実装していない。計画と安全要件は EXTERNAL_MCP_SECURITY.md を参照。

## 前提

- Windows側 Ollama (localhost:11434)。WSLは mirrored ネットワークなので直結。
- コンテキスト長はWindows環境変数 OLLAMA_CONTEXT_LENGTH=32768 で拡大済み。
- PDFのテキスト抽出には poppler-utils (`pdftotext`) が必要。
  `sudo apt install poppler-utils` (Ubuntu WSLには標準で入っていることが多い)。
  未導入でもサーバーは動くが、read_fileでPDFを開くとその旨のエラーを返す。

## モデルの目安 (RTX 3070 8GB VRAM)

- `gpt-oss:20b` — 推奨。MoEで実質高速、ツール呼び出しが確実
- `glm-4.7-flash` — 高性能だが19GBなのでCPU分担が大きく遅め
- `qwen3:8b` — 軽量。簡単なタスク向け

## 関連ドキュメント

- [CHANGELOG.md](CHANGELOG.md) — 時系列の変更履歴
- [MANUAL.html](MANUAL.html) — 人間向け操作マニュアル
- [REBUILD.md](REBUILD.md) — 別PCへの完全再構築ガイド＋各機能の設計ノート
- [SERVER.md](SERVER.md) — `server.py` のアーキテクチャ解説
- [REVERSIBLE_OPERATIONS.md](REVERSIBLE_OPERATIONS.md) — 不可逆性を基準にした可逆操作・ロールバック安全設計
- [IMPROVEMENTS.md](IMPROVEMENTS.md) — 信頼性・観測性・テスト・性能・保守・配布を含む改善ロードマップ
- [METACOGNITIVE_REPLANNING.md](METACOGNITIVE_REPLANNING.md) — 停滞・目的逸脱を検知して作業方針を自動再構築するメタ認知・再計画パス設計
- [EXTERNAL_MCP_SECURITY.md](EXTERNAL_MCP_SECURITY.md) — 内部MCPを安全な既定値として維持しつつ、将来の外部MCP連携に必要な認証・ポリシー・承認・監査を定義する計画書

## テスト

```bash
python3 -m unittest discover -s tests -t .
```

Ollama不要、標準ライブラリのみで完結する回帰テスト。詳細は
[REBUILD.md](REBUILD.md) §4-1参照。

## セッションログの分析(方針再評価のデバッグ)

停滞・早すぎる介入等が疑われるセッションがあれば、`tools/replay_review.py`で
現在のコードによる方針再評価の発火タイミングを機械的に(Ollama不要で)確認できる。

```bash
python3 tools/replay_review.py history/<sid>.json
```

新しい実障害パターンを見つけたら`tests/fixtures/review_incidents/`へ
パス匿名化のうえ追加し、`tests/test_review_replay.py`に期待値を回帰テストとして
固定する運用にしている(詳細はREBUILD.md該当セクション参照)。