# LocalCoder

ローカルLLM (Windows側 Ollama) だけで動く、GUIコーディングエージェント。
codex / claude code のようにファイル読み書き・コマンド実行を全自動で行う。
外部APIは一切使わない。依存ライブラリなし (Python標準ライブラリのみ)。

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

## 構成

- `server.py` — HTTPサーバー + エージェントループ (Ollama /api/chat + tools)
- `index.html` — チャットGUI (SSEストリーミング表示)
- ツール: run_command / read_file / write_file / edit_file / list_dir /
  delete_file / delete_directory / move_file / copy_file / web_search / fetch_url / view_image
  - ファイル操作は作業フォルダ内に制限
  - edit_file は完全一致のfind/replace。既存ファイルの部分修正は全文書き換えでなく
    こちらが優先される (トークン節約 + 書き換え漏れ事故の防止)
  - delete_file/delete_directory/move_file/copy_file は生の`rm`/`mv`より優先される
    可逆な削除・移動ツール (ターン単位でundoできる)
  - コマンドは作業フォルダをcwdとして実行 (タイムアウト180秒)
  - web_search は DuckDuckGo (無料・APIキー不要)、fetch_url はページ本文取得
  - read_file は `.pdf` を自動判別し `pdftotext` でテキスト抽出する (poppler-utils必須。
    スキャン画像PDFなどテキストが取れない場合はその旨を返す)
  - view_image は画像ファイル(png/jpg/gif/webp/bmp)を読み込む。現在選択中のモデルが
    vision対応 (`/api/show`の`capabilities`で判定) ならOllamaへの次の呼び出しで
    実際に見せる。非対応モデルではエラーを返し、エージェントがユーザーにモデル
    切り替えを促す。画面上部のモデル選択の横に「👁 画像対応 / 🚫 画像非対応」
    バッジで対応状況を確認できる
- Windows 側の操作にも対応: 作業フォルダ・ファイル操作ツールは WSLホーム(`$HOME`)
  に加え Windowsドライブ(`/mnt/c` 等)の配下も許可され、`C:\...` のファイルを
  read_file/write_file/edit_file で直接編集できる。Windowsコマンドは run_command
  から `powershell.exe -NoProfile -Command "..."` で実行できる(WSL相互運用)。
  許可範囲は環境変数 `LOCALCODER_ALLOWED_ROOTS`(コロン区切り)で変更でき、`$HOME`
  だけに戻すことも可能
- 可逆操作レイヤー(第1〜2段階): ファイルの変更・削除・移動・コピーを書き込み
  前に変更前状態としてワークスペース配下 `.localcoder/transactions/` へ自動保存し
  (1リクエスト=1トランザクション)、ターン終了サマリーの「⎌ このターンの変更を
  元に戻す」ボタンでいつでも復元できる(再適用=redoも可)。write_file/edit_fileは
  原子的書き込み、削除は専用ゴミ箱へ退避。削除・移動には専用ツール
  (delete_file/delete_directory/move_file/copy_file)があり、生の`rm`/`mv`より
  優先される(前者は戻せるが後者は戻せない)。読み取りだけのターンでは何も
  作られない。設計の全体像は [REVERSIBLE_OPERATIONS.md](REVERSIBLE_OPERATIONS.md) 参照
- 履歴の自動圧縮: 会話がコンテキスト長(32K)に近づくと、古いツール結果の切り詰め →
  古い会話のLLM要約への置換、を自動で行う (画面に 🗜 表示)。長い会話でも
  システムプロンプトや直近の文脈が押し出されて壊れることがない
- 空応答からの自動回復: モデルが本文なし・ツール呼び出しなしで黙って止まった場合、
  1回だけ自動で続行を促す (画面に 🔔 表示)。それでも空なら諦めてユーザーに通知する
  (無言で停止したように見えて実は完了している/固まっている、を防ぐ)
- Ollama呼び出し失敗時の自動再試行: HTTP 500等でOllamaへの問い合わせが失敗した場合、
  2秒待って1回だけ自動再試行する (画面に 🔔 表示)。それでも失敗すれば通常通り
  エラー表示して停止する (GPU/VRAMの瞬間的な負荷による一時的な失敗を手動再送
  せずに乗り越える)
- 完了ノーティス: 1回のリクエストが終わり入力待ちに戻ると、ベル音 + デスクトップ
  通知 (ブラウザのNotification API) で知らせる。他の作業（タスクマネージャーで
  GPU/CPU監視等）をしながらでも完了に気付ける。ユーザーが「■停止」を押した
  場合は通知しない
- 作業時刻の記録: プロンプトを受け取った時刻と、完了/停止/エラーで処理が
  終わった時刻を`history/<sid>.json`の`turns`配列に記録する
  (`{started_at, ended_at, status}`。statusは`completed`/`stopped`/`max_iter`/
  `error`/`disconnected`)。GPU負荷の記録などと突き合わせて後から確認できる
- システムプロンプトの信頼性強化: 実運用で「与えられた正確な値を使わず別の値を
  捏造する」「ビルド検証をせず『次にユーザーがcmake/makeを実行してください』と
  丸投げする」という失敗パターンが繰り返し観測されたため、SYSTEM_PROMPTに
  「与えられた正確な値はそのまま使う」「存在を確信できないライブラリ/関数名/
  パスは検証してから使う」「タスク完了は自分で実行して確認してから」という
  規律を明記した
- 作業状態ダッシュボード: 会話ログの圧縮とは別に、変更したファイル一覧・直近の
  実行コマンドと結果・同じコマンドが3回連続で失敗している場合の警告を、
  履歴から機械的に(LLM不使用で)毎回組み立て、その回のOllama呼び出しにだけ
  差し込む(保存される会話履歴自体は汚さない使い捨てのダッシュボード)。
  小型モデルが「さっき変更したファイルを忘れる」「同じ失敗を繰り返す」ことへの対策
- MCPサーバー接続 (任意): `mcp_servers.json` に定義した外部MCPサーバー
  (stdio / JSON-RPC 2.0) のツールを、組み込みツールと同列にモデルへ提供する。
  形式は [mcp_servers.json.example](mcp_servers.json.example) を参照
  (コピーしてパスを自分の環境に合わせる。ファイルが無ければこの機能は無効)。
  対応トランスポートはstdioのみ＝サーバーは子プロセスとしてローカル起動され、
  通信はマシン内で完結する。サーバーの起動失敗・応答失敗があってもLocalCoder
  本体は通常通り動く。最初の実利用サーバーは PicoCalc Expert MCP
  (PicoCalc開発資料の検索・参照、別リポジトリ)

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