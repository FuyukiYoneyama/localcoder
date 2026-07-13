# LocalCoder 完全再構築ガイド（移植用）

このファイル1つで、AI（Claude Code / codex 等）または人間が別のPCに
**LocalCoder** — ローカルLLM(Ollama)だけで動くGUIコーディングエージェント —
を完全に再構築できる。外部APIキー不要、依存ライブラリ不要。

> **AIへの指示**: このドキュメントを渡されたら、「1. 前提条件」を確認・整備した後、
> 「3. ファイル一式」の3ファイルを記載どおりのパスに作成し、「4. 検証手順」を実行して
> 動作確認まで行うこと。環境差分（ディストロ名・デスクトップパス等）は「5. 環境差分の調整」を参照。

---

## 1. 前提条件

| 項目 | 要件 | 確認コマンド (PowerShell) |
|---|---|---|
| OS | Windows 10/11 + WSL2 (Ubuntu系) | `wsl -l -v` |
| Ollama | Windows版 0.12以降（0.20.2で動作確認） | `ollama --version` |
| Python | WSL内に python3（3.8以降、標準ライブラリのみ使用） | `wsl -- python3 --version` |
| モデル | ツール呼び出し対応モデル。推奨 `gpt-oss:20b` | `ollama list` |
| GPU | 8GB VRAM で gpt-oss:20b が実用速度（一部CPUオフロード） | `nvidia-smi` |

### 1-1. Ollamaのインストール（未導入の場合）

https://ollama.com/download からWindows版をインストールし、モデルを取得:

```powershell
ollama pull gpt-oss:20b     # 推奨: MoEで高速、ツール呼び出しが確実 (13GB)
ollama pull qwen3:8b        # 軽量代替 (5GB)
```

### 1-2. WSL→Windows Ollama の接続経路

WSLの `localhost:11434` からWindows側Ollamaに届く必要がある。

**方法A（推奨）: mirroredネットワーク** — **Windows 11専用**（Windows 10では
`networkingMode=mirrored` を書いても無視され、WSLはNATモードのまま動く。移植先が
Windows 10なら最初から方法Cへ）。`C:\Users\<user>\.wslconfig` に:

```ini
[wsl2]
networkingMode=mirrored
```

を書いて `wsl --shutdown` 後に再起動。確認:

```powershell
wsl -- curl -s http://localhost:11434/api/version
# → {"version":"..."} が返ればOK
```

**方法B（mirroredにできない場合）**: Windows側で環境変数 `OLLAMA_HOST=0.0.0.0` を設定して
Ollamaを再起動し、WSL内からはWindowsホストIP（`ip route show default | awk '{print $3}'`）を使う。
その場合、後述の server.py を環境変数で切り替えられる:
`LOCALCODER_OLLAMA=http://<ホストIP>:11434 python3 ~/localcoder/server.py`

⚠ `OLLAMA_HOST=0.0.0.0` はOllamaを**LAN上の全端末に公開**してしまう。AIエージェントに
作業させる場合、この変更は無許可では拒否されることがある（実際に発生した事例は
「8. 実施例ログ」参照）。**方法C（Windows 10で推奨）**として、`0.0.0.0`の代わりに
`vEthernet (WSL)` アダプタのIPだけにバインドし、ファイアウォールもそのアダプタ経由に
限定する方法が安全。詳細は「8. 実施例ログ」参照。

### 1-3. コンテキスト長の拡大（重要）

Ollamaのデフォルトコンテキスト(4K)ではエージェントが履歴を忘れて破綻する。
Windowsのユーザー環境変数に設定して Ollama を再起動:

```powershell
[Environment]::SetEnvironmentVariable('OLLAMA_CONTEXT_LENGTH','32768','User')
Stop-Process -Name ollama -Force
Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" -ArgumentList "serve" -WindowStyle Hidden
```

（server.py側もリクエスト毎に `num_ctx: 32768` を指定するため、二重の保険になっている）

---

## 2. アーキテクチャ

```
[Edge アプリモードウィンドウ]  ← GUIに見えるが実体はブラウザ
        │ HTTP + SSE
        ▼
[WSL: server.py :8765]         ← Python標準ライブラリのみのHTTPサーバー
   ├─ GET  /                  → index.html (チャットGUI, CSRFトークン埋め込み)
   ├─ GET  /vendor/*.js       → 同梱のmarked/DOMPurify (CDN不使用)
   ├─ GET  /api/models        → Ollamaのモデル一覧を中継(モデルごとのvision対応可否付き)
   ├─ POST /api/chat          → エージェントループ (SSEでイベント配信) [要トークン]
   ├─ POST /api/stop          → 実行中断 [要トークン]
   ├─ GET  /api/sessions      → 保存済み会話の一覧 (履歴サイドバー用) [要トークン]
   ├─ GET  /api/session?sid=  → 1会話の全メッセージ (再開用) [要トークン]
   └─ POST /api/session/delete → 会話の削除 [要トークン]
        │ /api/chat (streaming, tools付き)
        ▼
[Windows: Ollama :11434]       ← ローカルLLM本体
```

**エージェントループの動作**: ユーザー入力 → LLMに tools 付きで問い合わせ →
LLMがツール呼び出しを返したら承認なしで即実行 → 結果を履歴に追加して再問い合わせ →
ツール呼び出しがなくなる（=タスク完了）まで最大80回繰り返す。

**ツール8種**: `run_command`（bash実行, cwd=作業フォルダ, 180秒タイムアウト、停止ボタン/
タイムアウトでプロセスグループごとkill）/ `read_file`（`.pdf`は`pdftotext`で自動テキスト抽出）/
`write_file` / `edit_file` /
`list_dir`（ファイル系は作業フォルダ外へのアクセスを拒否）/ `web_search`（DuckDuckGo HTML版の
スクレイピング、APIキー不要・無料）/ `fetch_url`（ページ取得→HTMLタグ除去テキスト、10KB上限）/
`view_image`（画像を読み込み、visionモデルなら次回のOllama呼び出しで実際に見せる）。
作業フォルダ自体も `$HOME`(または`ALLOWED_ROOTS`)配下でなければリクエストが拒否される。

**履歴自動圧縮の設計ノート**: 会話履歴の推定トークン数（ASCII=4文字/トークン、
非ASCII=1文字/トークンの概算）が予算（`NUM_CTX - RESERVE_TOKENS` = 24576）を超えると、
サーバーが各ループ反復の先頭で自動圧縮する。第1段階は古いツール結果の切り詰め
（直近`KEEP_RECENT_TOOLS`件は無傷、それ以前は500文字に切る。安価・LLM不使用）。
それでも溢れる場合は第2段階として、直近`KEEP_RECENT_MSGS`件を原文のまま残し、
それ以前をLLM自身に要約させて「【自動要約】」1メッセージに置換する（`compact`
SSEイベントでGUIに🗜表示）。**要約入力自体の肥大に注意**: 要約対象がnum_ctxを
超えるとollamaがプロンプト前方（=要約指示）を切り捨てて要約が壊れるため、
各メッセージを抜粋化（先頭7割+末尾3割で1000文字）した上で、入力が
`SUMMARIZE_INPUT_TOKENS`（NUM_CTX/2）を超える場合はチャンクに分割して各々要約する
（この欠陥は実際に発生し、分割要約の導入で解消した）。要約呼び出しが失敗した場合は
「【自動省略】」マーカーで単純省略にフォールバックする（文脈は失うが溢れて壊れる
よりよい）。分割境界がtoolメッセージに当たる場合は、呼び出し元assistantとペアが
壊れないよう境界を手前にずらす。検証: 冒頭に埋めた固有情報（コードネーム）が
56868→7746トークンへの圧縮を生き残り、モデルが正答することを確認済み。

**圧縮の世代劣化対策（インクリメンタル要約 + 機械抽出ファイル一覧）**: 長時間
セッションで圧縮が2回以上走ると、「古い部分」に前回の「【自動要約】」マーカー
自体が含まれ、それを毎回LLMに生ログとして食わせて丸ごと再要約すると、要約が
要約をパラフレーズし直す「伝言ゲーム」状態になり、特にファイルパスや目的・方向性
といった具体的事実が回を追うごとに薄れる問題があった。対策として`compact_history`
は圧縮対象`old`の先頭が既存マーカーかどうかを`_parse_marker`で判定し、マーカー
であればそのまま「これまでの要約」として扱って**再要約せず**、`update_summary`
（`UPDATE_SUMMARIZE_PROMPT`）が既存要約と新規分の生ログを**1回のLLM呼び出し**で
直接統合する（「既存の要約に含まれる事実は明確に古くなった場合を除き失わずに
引き継げ」と明示。当初は「新規分を要約→既存要約とマージ」の2回呼び出しだったが、
遅いローカルハードでは圧縮1回の停止時間が倍になるため1回に統合した）。さらに
変更ファイル一覧は`extract_changed_files`でLLMを介さず`write_file`/`edit_file`
呼び出しから機械的に抽出し、要約本文とは別ブロック（`--- 変更ファイル一覧 ---`）
としてマーカーに常に付記する。このブロックはLLMの出力ではなく次回`_parse_marker`で
単純にパースして引き継がれる値なので、要約の質に関わらずファイルパスが正確なまま
保持される（`build_work_state`も同じ`extract_changed_files`を使うよう統一した。
なお結果が`ERROR`の書き込みは「変更したファイル」に数えない——失敗した書き込みを
成功と報告すると、モデルが「ファイルは作成済み」と誤認するハルシネーションを
実際に誘発した）。要約自体が失敗した場合も、既存の要約とファイル一覧があれば
それは保持したまま新規分のみ省略するようにし、以前の「丸ごと省略」よりも情報を
残すようにした。

**圧縮の頻発によるストール対策（重複除去 + ヒステリシス）**: 実測で「会話が予算の
天井（24576トークン）に張り付き、数イテレーションごとに圧縮が再発して作業が
まったく進まない」セッションが発生した（73KBの履歴のうち37KBが、モデルが同じ
ファイルを読み直し続けたことによる**同一内容のツール結果の重複**だった）。
天井ギリギリで圧縮を止めるとすぐ再超過するうえ、安価な切り詰めでも履歴**前方**を
書き換えるためollamaのプロンプトキャッシュが毎回無効化され、全プロンプトの
再処理（CPUオフロード時は数分）が毎イテレーション発生するのが「進まない」の
正体だった。対策は3つ: (1) `dedupe_tool_results`（第0段階・LLM不使用）が
同一内容のツール結果の古い方を短い参照文に置換する（該当セッションでは
これだけで24168→14605トークン）。(2) ヒステリシス——発動は予算超過時のまま、
いったん発動したら目標（予算×`COMPACT_TARGET_RATIO`=0.6）まで一気に下げ、
再発までの間隔を空ける。切り詰め後も目標を上回る場合は予算以下でも要約に進む。
(3) 作業状態ダッシュボードに「同じツール呼び出し（同名+同引数）を3回連続で
繰り返している」警告を追加し、成功していても進展のない繰り返し（同じファイルの
再読など）をモデル自身に自覚させる。

**予算天井付近での空応答対策（先回り圧縮 + 強制圧縮）**: 実測で、書き込み系
ツールを一度も呼ばずドキュメント読解だけで会話が予算(24576トークン)の**99%**
（24240〜24316トークン）まで伸び、その状態でモデルが本文もツール呼び出しも
無い「空応答」を2回連続（自動リトライ1回を使い切っても）返して何も進まない
セッションが発生した。原因は二重: (a) 弱い/量子化されたローカルモデルは、
巨大な文脈を読み込んだ状態だと内部の思考でRESERVE_TOKENSの生成余白を
使い切り、可視の本文が一切残らないことがある（モデル側の限界）。(b) 従来の
空応答リトライは「続けてください」を1文足すだけで文脈量をほとんど減らさない
ため、ほぼ同じ状態でもう一度投げても同じ壁に当たるだけだった（LocalCoder側の
設計の隙間）。対策は2つ: (1) `compact_history`の発動判定を「予算超過時」から
「予算×`PROACTIVE_COMPACT_RATIO`(0.9)を超えた時点」に前倒しした——正式に超過
してから動くのでは手遅れで、天井の手前で先回りして縮める（該当セッションを
再生すると、実際に空応答が起きたメッセージより前の時点で新しいtriggerを
超えており、発動していれば空応答自体を防げていたことを確認済み）。
(2) 空応答で自動リトライする直前に`compact_history(..., force=True)`を呼び、
trigger未満でも強制的に圧縮してから次の試行に入るようにした（リトライは
文脈を実際に減らして初めて意味がある）。

**実行中バージョンの可視化**: 「バグを直してpushしたのに再現する」という報告を
調べたところ、原因はコードではなく**LocalCoderを再起動しておらず古いプロセスの
ままだった**ことだった（WSL側で`python3 server.py`が直した後もずっと動き続けて
いた）。同じ切り分けを毎回手作業でやらずに済むよう、起動時に`server.py`が
自分の置かれたディレクトリで`git rev-parse --short=7 HEAD`を実行し、結果を
`window.LC_VERSION`としてindex.htmlに埋め込みヘッダーに表示するようにした
（未コミットの変更があれば`+dirty`を付記）。`git log`の最新コミットハッシュと
画面表示を見比べるだけで、今動いているのが最新版かどうかが一目で分かる。
git実行に失敗した場合は"unknown"にフォールバックする。

**履歴タイトルが圧縮後にどれも同じになる問題**: 履歴一覧のタイトルは
`save_session`が「最初のuserメッセージの内容」から毎回作り直していたため、
圧縮が起きて`messages[0]`が「【自動要約】ここまでの会話が長くなったため、
古い部分は以下の要約に置き換えられた:...」という定型マーカーに置き換わった
瞬間、以後の保存すべてでタイトルもこの定型文になってしまい、履歴一覧で
別プロジェクトのセッションと区別がつかなくなっていた（実際にユーザーから
報告された）。対策として`derive_title`はマーカーを検出したら`_parse_marker`で
要約本文を取り出しそちらをタイトルにするよう変更し、さらに`save_session`は
**タイトルを初回保存時に一度だけ確定し、以後は既存タイトルを常に使い回す**
ようにした（毎回作り直すこと自体が原因なので、そもそも作り直さない）。既存の
壊れたタイトルを持つ履歴ファイル(5件)は、`derive_title`と同じロジックで
一度だけ書き換えるスクリプトを実行して移行した（`SUMMARY_BODY_SEP`導入前の
旧マーカー形式は区切りが無いため、`「...置き換えられた:\n」`の直後を本文と
みなすフォールバックが必要だった）。

**圧縮まわりの未着手アイディア（将来やるなら）**:
- **C. ユーザーの逐語指示の永続ピン留め**: 「覚えておいて」等のユーザー発言を
  検出し、圧縮ループの外側にある専用リストに保存して、system prompt直後に毎回
  そのまま挿入する。要約（LLM生成）を経由しないため、何度圧縮が起きても文言が
  変化・脱落しない。現状はSUMMARIZE_PROMPT側で「一字一句そのまま」と指示している
  だけで、遵守はLLM任せになっている。
- **D. 3層の段階的減衰**: 現状は「直近`KEEP_RECENT_MSGS`件=原文／それ以前=即要約」
  という二値のカットだが、中間層として「原文ではないが要約もしない、`_excerpt`
  抜粋のみ」の層を挟み、直近の詳細が急に消えるショックを緩和する。
- **E. 「現在のゴール」専用フィールド**: 要約プロンプトに現在の作業目標を1行で
  明示的に出力させ、圧縮結果の先頭に固定フォーマットで配置して本文中に埋没させない。
  次回以降の圧縮でもこのフィールドだけは常に無条件で引き継ぐ（Cと同様、圧縮の
  繰り返しで方向性が薄まるのを防ぐのが狙い）。

**空応答自動回復の設計ノート**: ローカルモデル（特にgpt-oss:20b）は、ビルド失敗など
行き詰まった状況で、本文なし・ツール呼び出しなしという「空応答」を返してターンを
静かに終えることがある。この場合サーバーは正常終了として扱うため、ユーザーには
何も表示されず、実質的に停止したように見えてしまう（実際に本番で遭遇し、
セッション履歴ファイルの最後のassistantメッセージがcontent=""であることで確認した
バグ）。対策として、`content`が空かつ`tool_calls`も無いターンを検出したら、
`EMPTY_RETRY_LIMIT`(1)回まで自動で「続けてください」という合成ユーザーメッセージ
（`EMPTY_RESPONSE_NUDGE`、`(システム自動継続)`と明記して会話履歴上も見分けられる
ようにする）を追加してループを継続する。それでも空応答なら諦めて
`{"type": "notice", "message": "⚠ ..."}` をGUIに送りユーザーに手動介入を促す
（`server.py`参照）。モックテストで両ケース（1回の自動継続で回復／2回とも空で
警告して停止）を検証済み。無限ループの心配はない: `empty_retries`は
`handle_chat`のローカル変数なのでユーザーの1メッセージごとにリセットされ、
かつ`EMPTY_RETRY_LIMIT`で上限を切っている。

**HTTP再試行の設計ノート**: 上記の「空応答」とは別に、Ollama呼び出し自体が
`urllib.error.URLError`（HTTP 500等、接続断も含む）で失敗するケースがある
（実際にgpt-oss:20bで一過性の500エラーに遭遇した）。`for chunk in
ollama_stream(payload):` を`try/except urllib.error.URLError`で囲み、
`HTTP_RETRY_LIMIT`(1)回まで`HTTP_RETRY_DELAY`(2秒)待って自動再試行する
（GUIに🔔で通知）。上限を超えたら`raise`で外側のハンドラに委譲し、従来通り
赤いエラー表示で停止する。こちらも`http_retries`は`handle_chat`のローカル
変数でユーザー1メッセージごとにリセットされ、再試行は`messages`を一切
変更せずに行う（失敗時点では`assistant`メッセージがまだ履歴に追加されて
いないため、同じ`payload`でそのまま再試行しても履歴に矛盾は生じない）。
モックテストで回復ケース（1回失敗→再試行で成功）と行き詰まりケース（2回とも
失敗→通知後に通常のエラー表示で停止、呼び出しは2回で打ち切り）の両方を
検証済み。

**完了ノーティスの設計ノート**: ユーザーがタスクマネージャーでGPU/CPU監視をしながら
待つなど、画面から目を離している間に処理が終わっても気付けない、という要望への
対応。`index.html`側で`/api/chat`のストリーム読み取りループが終わった直後
（成功・エラー・停止いずれの経路でも通る`send()`関数の共通末尾）に`notifyDone()`
を呼ぶ。中身は2つ: (1) Web Audio APIで生成する短いベル音（外部音声ファイル不要、
`OscillatorNode`+`GainNode`で880Hzのサイン波を0.4秒フェードアウト）、(2) ブラウザ標準
の`Notification` API によるデスクトップ通知（ページ読み込み時に`Notification.
requestPermission()`を1回呼んでおく）。デスクトップ通知はウィンドウがフォーカスを
失っていても表示されるため、まさに「他の作業をしながら気付きたい」という要望に合う。
ユーザー自身が「■停止」を押した場合は`stoppedByUser`フラグで通知を抑制する
（自分で止めた操作についてわざわざ知らせる必要はないため）。サーバー側の変更は
不要（完全にクライアント側`index.html`のみで完結する機能）。

**作業時刻の記録の設計ノート**: 「プロンプトを投げた時」と「作業が終わった/中断した
時」を記録してほしいという要望への対応。`handle_chat()`冒頭で
`turn_started_at = time.time()`を取り、`turn_status`（初期値`"completed"`）を
以降の各終了経路で上書きする: 停止ボタン検知2箇所（ストリーム受信中・ツール実行中）
で`"stopped"`、`for...else`のMAX_ITER到達で`"max_iter"`、
`BrokenPipeError`/`ConnectionResetError`で`"disconnected"`、`URLError`や
その他の例外で`"error"`。`finally`節で`{"started_at", "ended_at", "status"}`の
`turn`辞書を組み立て、`save_session(..., turn=turn)`に渡す。

`save_session()`は元々毎回ファイル全体を上書きする実装だったため、単純に
`turns`キーを追加しただけでは前回までの記録が消えてしまう。そのため保存前に
既存ファイルがあれば`turns`配列を読み出し、そこに今回の`turn`をappendしてから
書き戻すようにした（`server.py`参照）。個々のメッセージ内容（`messages`配列）には
一切手を入れていない——時刻は独立した`turns`配列にのみ記録することで、
Ollamaに送るメッセージのスキーマや`compact_history()`のトークン見積もりに
影響を与えないようにしている。

検証: (1) 単体テストで`save_session`を複数回呼び、`turns`が上書きされず
蓄積されること、(2) 実際のgpt-oss:20bでの正常完了リクエストで
`status="completed"`・実測所要時間（37.3秒）が正しく記録されること、
(3) 実際に停止ボタンを押した場合に`status="stopped"`となること、
(4) ollama_streamを常時失敗にモックした場合に`status="error"`となること、
の4パターンを確認済み。ワークスペース検証エラーなど、`try/finally`ブロックに
入る前に早期returnするケースは対象外（実質的な作業が何も始まっていないため）。

**システムプロンプト規律強化の設計ノート**: 実際のPicoCalcプロジェクトでの運用中、
ローカルモデル（gpt-oss:20b）に次の失敗パターンが繰り返し観測された:
(a) ユーザーが仕様書等で正確な値（GPIOピン番号・I2Cアドレス等）を与えても、
それを使わず別の"それらしい"値を捏造する、(b) `pico_fat_fs`のような実在しない
CMakeターゲット/関数名を存在を確認せず使う、(c) ビルド検証を自分で実行せず
「次はcmake/makeを実行してください」とユーザーに丸投げして終わる。
`SYSTEM_PROMPT`にこれらへの対抗ルールを追記した:
「与えられた正確な値はそのまま使い、自分の推測に置き換えない」
「ライブラリ/関数/パスの実在を確信できなければ使う前に確認する」
「run_commandで実際に実行し、成功を観測するまでタスク完了にしない。
『次にユーザーが○○を実行してください』で終わらせない」
「以前ビルド/実行に成功していたファイルを編集したら、壊していないか再確認する」。

検証（モックではなく実際のgpt-oss:20bで実施）:
1. 正確な値（I2C1/GPIO6/GPIO7/0x1F等）をそのまま転記させる単純タスク → 全項目一致
2. 仕様書を読んでコード（`kbd_init()`関数）に落とし込ませるタスク → i2c1・
   GPIO6/7・0x1Fを正しく使用（以前のようにi2c0・GPIO4/5・0x20への捏造なし）
3. わざと出力ファイル名が壊れたMakefileを渡し「makeを実行して確認して」と
   依頼 → 自力でMakefileの誤りを発見・修正し、`make`実行→生成物を`list_dir`で
   確認→実行して終了コードまで確認してから完了報告（「次はユーザーが」という
   丸投げが発生しなかった）

**Windows操作対応の設計ノート**: LocalCoderはWSL(Linux)内で動くが、WSLの
相互運用機能により**元々**Windowsコマンドは実行できた——`run_command`は
サンドボックス無しの`bash -lc`なので、`powershell.exe -Command "..."`や
`.exe`直接呼び出しでWindows側が動く（`powershell.exe`が実際に動作することを
確認済み）。Windowsファイルも`/mnt/c/...`から読める。ブロックされていたのは
2点だけ: (1) 専用ファイルツール(read_file/write_file/edit_file/list_dir)と
作業フォルダが`$HOME`配下に制限されていたため`/mnt/c`のWindowsファイルを直接
編集できない、(2) システムプロンプトがWindows操作の存在をモデルに伝えていない。
対応として、`under_home()`を`ALLOWED_ROOTS`(既定=`[$HOME, /mnt]`、環境変数
`LOCALCODER_ALLOWED_ROOTS`で上書き可)に対する`under_allowed()`へ一般化し、
`handle_chat`のワークスペース検証と`list_subdirs`(フォルダ選択ダイアログ)を
これに切り替えた。`resolve_path`(ファイルツールのサンドボックス)は元々
「選ばれたワークスペース配下」に閉じる実装なので変更不要——ワークスペースが
`/mnt/c/...`になれば自動的にそのWindowsフォルダ内に閉じる。システムプロンプトには
「Windowsファイルは`/mnt/c`配下」「Windowsコマンドは`powershell.exe`で(cmd.exeは
cwdがWSLパスだと警告を出すのでpowershell.exe推奨)」を明記した。

セキュリティ上の注意: 「ワークスペースを`$HOME`配下に制限」は元々4層防御の
1層だったが、`run_command`は元からサンドボックス無しでこの層の対象外だった
（つまりこの層は専用ファイルツールにしか効いていなかった）。`/mnt`まで許可すると
専用ファイルツールもWindowsドライブに届くようになる。外部Webページからの悪用を
防ぐ本丸(トークン+localhostバインド)は不変。`$HOME`だけに戻したい場合は
`LOCALCODER_ALLOWED_ROOTS=$HOME`を設定する。

検証: 単体テスト13件(HOME/`/mnt`配下は許可、`/etc`・`/usr`・`/`は不許可、
Windowsディレクトリの一覧取得、`/mnt/c/Users`の親が`/mnt/c`で辿れること、
`/mnt`はルートなので「上へ」なし、範囲外はHOMEにフォールバック、resolve_pathが
Windowsワークスペース内は許可し外への脱出は拒否)、および実際のgpt-oss:20bで
`/mnt/c/Users/fyone/lc_win_test`を作業フォルダにしてwrite_fileでWindows実ファイル
(`C:\Users\fyone\lc_win_test\note.txt`)を作成し、`powershell.exe`でWindowsユーザー名
(`fyone`)を取得するE2Eが成功することを確認済み。

**作業状態ダッシュボードの設計ノート**: システムプロンプトの規律強化だけでは、
「さっき変更したファイルを忘れる」「同じ失敗を繰り返す」という、会話ログの
自然文からは拾いにくい種類の失敗までは防げない。要約(`compact_history`/
`summarize_old`)はLLMに頼る以上、要約自体が雑になるリスクを抱えたままである。
そこで会話ログの圧縮とは別に、**機械的に(LLMを使わず)確定できる事実だけ**を
毎回組み立てて画面外のダッシュボードとして注入する方式を追加した:

- `_iter_tool_calls_with_results()`: `assistant`の`tool_calls`と、直後に続く
  同じ順序の`tool`メッセージ群を突き合わせ、(ツール名, 引数, 結果) を発生順に
  取り出す
- `build_work_state()`: 上記から「変更したファイル一覧」（`write_file`/
  `edit_file`の対象パス）、「直近`RECENT_COMMANDS_SHOWN`件のコマンドと
  exit_code」、「同一コマンドが`FAIL_REPEAT_THRESHOLD`(3)回連続で失敗したら
  警告」を組み立てる（`server.py`参照）

この結果は`handle_chat`の`for it`ループ毎に再計算し、**保存される会話履歴
(`messages`)には一切追加しない**——Ollama呼び出し1回分だけの使い捨てメッセージ
（`call_messages = messages + [{"role":"user","content":WORK_STATE_PREFIX+...}]`）
として差し込む。要約プロンプトのような意味的な判断（目的・サブタスク・
どの仮説が外れたか）はLLMに頼らざるを得ないため今回は対象外とし、まずは
コストゼロ・100%正確な機械的部分だけを実装した（ユーザーとの設計議論の結論）。

なお圧縮済み(要約に置き換わった)古い部分は`tool_calls`構造が失われるため、
このダッシュボードの「変更ファイル一覧」は直近の非圧縮ウィンドウのみを反映する
（古い変更点は要約の自然文側に残る）。

検証: (1) 単体テスト10件（空履歴/ファイル抽出/コマンド結果表示/3回連続失敗の
検知/2回では未検知/別コマンド成功後は誤検知しない/`messages`本体を書き換えない
こと、を確認）、(2) 実際のgpt-oss:20bでhello.py作成タスクが従来通り動くこと、
(3) わざと`cmake ..`を3回連続失敗させた履歴から続行させた場合、4回目も
同じコマンドを盲目的に繰り返さず、`list_dir`/`find`で状況を調べ直してから
別のアプローチ（`cmake -S . -B build`）に切り替えたことを確認済み
（ただしこのテストはワークスペースが実際に空だったため、その事実自体が
調査を促した可能性もあり、ダッシュボード警告単体の効果を完全には
切り分けられていない）。

**フォルダ選択ダイアログの設計ノート**: ブラウザ標準の`<input type="file" webkitdirectory>`は
セキュリティ上の理由で絶対パスを返さない（相対パスのファイル一覧しか取れない）ため、
作業フォルダ選択には使えない。代わりにサーバー側に`GET /api/browse?path=...`
（要トークン、`$HOME`配下のみ許可・範囲外は`$HOME`にフォールバック、隠しディレクトリは
除外）を追加し、GUI側にモーダル式のディレクトリブラウザを実装した
（`list_subdirs()`, `server.py`参照）。「📁 参照」ボタン→現在の作業フォルダ欄の値を
起点に一覧表示→ディレクトリクリックで下降、「.. (上へ)」でHOMEまで上昇可、
「このフォルダを選択」で確定、という単純なナビゲーション。

**edit_fileの設計ノート**: 完全一致のfind/replace（old_string→new_string、
`replace_all`オプション付き）。既存ファイルの部分修正で全文書き換え（write_file）を
使うと、大きいファイルほど出力トークンを浪費し、小型モデルは途中の行を書き換え忘れて
ファイルを壊しやすい。そのためシステムプロンプトで「部分修正はedit_file優先、
write_fileは新規作成か全面書き換えのみ」と誘導している。小型モデルは完全一致の
old_stringを作るのが苦手な場合があるため、失敗時のエラーメッセージに次の一手
（read_fileで正確にコピーせよ／一意になるまで文脈を足せ／諦めてwrite_fileにせよ）を
書いてあり、モデルが自己回復できる。gpt-oss:20bでは「PORT変更とDEBUG変更を
edit_file 2回でピンポイント修正→read_fileで検証」という理想的な挙動を確認済み。

**POST APIはCSRFトークン必須**: 起動ごとに生成するランダムトークンを`index.html`配信時に
埋め込み、全POSTで `X-LocalCoder-Token` ヘッダ＋Origin/Host/Content-Type検証を行う
（127.0.0.1バインドだけでは、同一PC上の悪意あるWebページからのno-cors POSTを防げないため）。
**全GETもHostヘッダを検証**（DNSリバインディング対策）し、履歴を返す`/api/sessions`・
`/api/session?sid=`はGETでもトークン必須（`/`・`/api/health`・`/api/models`は
トークン取得前に呼ぶ必要があるためチェック対象外。機密を返さないので問題ない）。

**依存JSはCDNではなく同梱**: `marked`・`DOMPurify`は`~/localcoder/vendor/`にバージョン
固定で配置し、`server.py`が`/vendor/*.js`として静的配信する。このページには
`window.LC_TOKEN`（コマンド実行に到達できる権限）が埋め込まれているため、CDN配信の
JSを使うとCDN側の改ざんやバージョン無指定URLの自動更新がそのままLocalCoderの実行権限
になってしまう。同梱により完全オフラインでも動作する。

**web_searchの実装ノート**: `https://html.duckduckgo.com/html/?q=…` をUser-Agent偽装付きで
GET し、`result__a`（タイトル+URL）と `result__snippet` を正規表現で抽出。結果URLは
`uddg=` パラメータに包まれているのでURLデコードして取り出す。一部サイト（raspberrypi.com等）は
fetch_url を403で拒否するが、エラーはそのままモデルに返るので別の検索結果を自分で試す。

**SSEイベントプロトコル** (`data: {json}\n\n` 形式):
`think`(思考トークン) / `token`(本文トークン) / `turn_done`(1応答完了) /
`tool_start`,`tool_end`(ツール実行) / `history`(全会話履歴→クライアントが保持し次回送信) /
`all_done` / `error`

**会話状態はクライアント側が保持**（サーバーはステートレス）。`history` イベントで
tool呼び出し含む完全履歴を返し、次のPOSTでそのまま送り返す方式。

**チャット履歴の永続化**: 各会話は完了時（エラー・停止時も含む）に
`~/localcoder/history/<sid>.json` へ自動保存される（title=最初のユーザー発言、
updated_at、model、workspace、全messages）。GUIの左サイドバーが `/api/sessions` で
一覧表示し、クリックで `/api/session?sid=` から読み込んで会話を再開できる
（tool呼び出しカードも復元）。sidはファイル名になるため `[A-Za-z0-9_-]` のみに
サニタイズすること（パストラバーサル防止）。履歴はプライベートな内容を含むので
`.gitignore` に `history/` を入れる。

**画像・PDF対応の設計ノート**: 「画像やPDFを扱えるようにしてほしい」という要望への対応。
検討した2案:
(a) チャット欄に添付UI(📎ボタン/ファイル選択)を追加し、クライアント側でbase64化して
送る方式、(b) 既存のエージェント自律ループに乗せ、専用ツールとして実装する方式。
LocalCoderは元々「エージェントがワークスペース配下(Windowsドライブ含む)のファイルに
自由にアクセスできる」設計であり、ユーザーは既に`read_file`にパスを渡すだけでよい
仕組みに慣れている。(a)は添付UI・capability連動の有効/無効化・base64送受信など
UIの複雑さが増す一方、(b)は既存のtool-callingループにそのまま乗るため実装量が
少なく、一貫性も高い。ユーザーとの設計議論を経ず(b)を選択したが、既存アーキテクチャ
との親和性を優先した判断であり、「チャットに直接ファイルを貼り付けたい」ニーズが
出た場合は(a)を後から追加できる(両立可能)。

- `read_file`が`.pdf`拡張子を検出したら`pdftotext -layout <path> -`をsubprocessで
  呼び出しテキスト化する(`pdf_to_text()`)。`pdftotext`(poppler-utils)が無い環境では
  `FileNotFoundError`を捕まえ導入コマンドを案内するエラー文字列を返す(サーバー全体は
  落とさない)。スキャン画像PDF等で抽出結果が空文字なら、その旨をエージェントに返す。
- 新規ツール`view_image`: パスを受け取り拡張子(`IMAGE_EXTS`)を検証、
  `model_capabilities(model)`(`/api/show`の`capabilities`配列をモデル名でキャッシュする
  `_CAPS_CACHE`辞書経由、`vision`を含むか判定)で現在のモデルがvision対応か確認する。
  非対応ならエラー文字列を返すのみ(ここでOllamaへの実際の画像送信は行わない)。
  対応していれば画像をbase64化して`pending_images`リスト(呼び出し元から渡される
  可変リスト)に積み、`"OK: loaded ..."`という短いテキストだけをtool結果として返す。
- `handle_chat`のツール実行ループが1イテレーション分の`tool_calls`を処理し終えた後、
  `pending_images`が空でなければ、`{"role":"user","content":"(view_imageで読み込んだ
  画像)","images":[...]}`という合成のuserメッセージを`messages`に追加してから次の
  Ollama呼び出しへ進む。**tool役割のメッセージに直接`images`を載せる案は採らなかった**
  ——Ollamaのvisionテンプレートはuser/assistantロールでの画像を前提にしている実装が
  大半で、tool役割での画像添付が全モデルで確実に効くかを検証していないため、より
  安全側のuserメッセージ合成を選んだ。ライブ表示用に`{"type":"image","b64":...}`の
  SSEイベントも同時に送る(この合成メッセージは通常の会話履歴として保存されるため、
  セッションを開き直した際もそのまま画像として復元される)。
- `/api/models`のレスポンスを`["name", ...]`から`[{"name":..., "vision":bool}, ...]`
  に変更し(`index.html`の`loadModels()`も追従)、モデル選択の横に
  「👁 画像対応 / 🚫 画像非対応」バッジを常時表示するようにした。`model_capabilities()`
  の結果はモデル名ごとにプロセス内キャッシュするため、モデル一覧を開くたびに毎回
  `/api/show`を叩き直すことはない(モデルの追加/削除はollama pull/rmでしか起きず、
  サーバー起動中に変わることは事実上ない)。
- トークン見積もり(`estimate_tokens`)に`TOKENS_PER_IMAGE`(768、目安値)×画像枚数を
  加算するようにした。画像はテキストではないため元の文字数ベースの概算では
  ほぼゼロと見積もられてしまい、コンテキスト溢れの原因になり得るため。
- 要約(`render_transcript`、`compact_history`の入力)では画像の中身(base64)を
  要約対象に含めず、「[画像N枚が添付されていた(要約には含まれない)]」という
  短いマーカーに留める。base64をそのまま要約プロンプトに混ぜるとトークンを
  無駄に消費し、要約自体が壊れるリスクがあるため。

検証: 実際に動かしているOllama(qwen3-vl:8b/gpt-oss:20b/qwen3:8b等)に対し、
(1) 自作の最小PDF(`pdftotext`が標準搭載のPythonライブラリで生成困難なため生の
PDF構文で自作)を`read_file`で読ませてテキストが一致すること、(2) 単色PNGを
`view_image`で読ませ、vision対応のqwen3-vl:8bが正しく色を回答すること、
(3) vision非対応のqwen3:8bで`view_image`を呼ぶと明示的なエラーが返り、
エージェントがユーザーへのモデル切り替え案内を自分の言葉で生成すること、
(4) 保存済みセッションJSONに`images`フィールドが正しく残り、履歴を開き直すと
ブラウザ側でサムネイル(`<img>`)として実際に描画される(`naturalWidth/Height`が
元画像と一致)こと、の4パターンをE2Eで確認済み。

---

## 3. ファイル一式

以下を作成する。①②④はWSL内 `~/localcoder/`、③はWindowsのデスクトップ。

### ① `~/localcoder/server.py`

```python
#!/usr/bin/env python3
"""LocalCoder — ローカルLLM(Ollama)で動くGUIコーディングエージェント。

依存ライブラリなし(Python標準ライブラリのみ)。
Windows側 Ollama (localhost:11434) に接続し、ツール(ファイル読み書き・
コマンド実行)を全自動で実行するエージェントループを提供する。
ブラウザで http://localhost:8765 を開いて使う。
"""
from __future__ import annotations  # `dict | None` 等の新型ヒント構文をPython 3.8/3.9でも使えるようにする

import base64
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OLLAMA = os.environ.get("LOCALCODER_OLLAMA", "http://localhost:11434")
PORT = int(os.environ.get("LOCALCODER_PORT", "8765"))
ROOT = Path(__file__).resolve().parent
MAX_ITER = 80          # 1リクエストあたりの最大ツールループ回数
EMPTY_RETRY_LIMIT = 1  # モデルが本文なし・ツール呼び出しなしで終える"空応答"時、
                       # 自動で続行を促す回数の上限 (それでも空ならユーザーに通知して停止)
EMPTY_RESPONSE_NUDGE = ("(システム自動継続) 直前の応答が空でした。作業が完了して"
                        "いるなら結果を要約し、未完了ならツールを使って作業を"
                        "続けてください。")
HTTP_RETRY_LIMIT = 1   # Ollama呼び出しがHTTPエラー/接続エラーで失敗した時、
                       # 自動で再試行する回数の上限 (それでも失敗ならユーザーに通知して停止)
HTTP_RETRY_DELAY = 2.0  # 再試行までの待機秒数 (瞬間的なGPU/VRAM負荷の解消を待つ)
CMD_TIMEOUT = 180      # コマンド実行タイムアウト(秒)
NUM_CTX = 32768
PDF_TIMEOUT = 60       # pdftotext実行タイムアウト(秒)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_MAX_BYTES = 20_000_000   # view_imageで読み込む画像の最大サイズ
TOKENS_PER_IMAGE = 768         # 画像1枚のトークン概算(モデル依存の目安値)

# --- 作業状態ダッシュボード (会話ログではなく、機械的に導出した「今の盤面」) ---
WORK_STATE_PREFIX = "(作業状態 - 自動生成、会話には保存されません) "
RECENT_COMMANDS_SHOWN = 5   # ダッシュボードに載せる直近コマンド数
FAIL_REPEAT_THRESHOLD = 3   # 同一コマンドがこの回数以上連続失敗したら警告する

# --- 履歴の自動圧縮 (コンテキスト溢れ対策) ---
RESERVE_TOKENS = 8192   # 生成(thinking含む)+システムプロンプト用に確保する分
KEEP_RECENT_MSGS = 6    # 要約時に原文のまま残す直近メッセージ数
KEEP_RECENT_TOOLS = 4   # 切り詰めずに残す直近のツール結果数
TOOL_TRIM_CHARS = 500   # 古いツール結果の切り詰め後サイズ
MSG_EXCERPT_CHARS = 1000            # 要約入力で1メッセージから取る最大文字数
SUMMARIZE_INPUT_TOKENS = NUM_CTX // 2  # 要約1回の入力上限 (超えたら分割要約)

CANCEL = {}            # sid -> threading.Event
HISTORY_DIR = ROOT / "history"   # チャット履歴の保存先 (1会話 = 1 JSONファイル)
HISTORY_DIR.mkdir(exist_ok=True)

# CSRF対策: 起動ごとのランダムトークン。index.html配信時に埋め込み、
# 全POST APIで X-LocalCoder-Token ヘッダとして要求する。
# 外部サイトからの no-cors POST はこの値を知り得ないため全て拒否される。
TOKEN = secrets.token_hex(16)
HOME = Path.home().resolve()
# 作業フォルダ・ファイル操作を許可するルート。既定は WSL ホーム + Windows ドライブ(/mnt)。
# これにより /mnt/c/Users/... のような Windows 側ファイルも read_file/write_file/
# edit_file/list_dir で直接編集できる。run_command は元々サンドボックス無しなので
# powershell.exe 等で Windows コマンドも実行可能(WSL相互運用)。
# 環境変数 LOCALCODER_ALLOWED_ROOTS(コロン区切り)で上書きでき、HOMEだけに戻すこともできる。
ALLOWED_ROOTS = [Path(p).expanduser().resolve() for p in
                 os.environ.get("LOCALCODER_ALLOWED_ROOTS",
                                f"{HOME}:/mnt").split(":") if p]
# 画面初期表示時の作業フォルダ。個人の作業パスをリポジトリに埋め込まないよう
# 環境変数で指定する(未設定ならHOME)。index.html配信時にwindow変数として埋め込む。
DEFAULT_WORKSPACE = os.environ.get("LOCALCODER_DEFAULT_WORKSPACE", str(HOME))

SYSTEM_PROMPT = """You are LocalCoder, an autonomous coding agent running on the user's machine.
Workspace directory: {ws}

Rules:
- You have tools: run_command, read_file, write_file, edit_file, list_dir, web_search, fetch_url, view_image. Use them freely without asking permission.
- read_file automatically extracts text from PDF files (.pdf) via pdftotext — just call it with the PDF path like any other file. If a PDF is a scanned image with no extractable text, you'll get a message saying so.
- To actually see an image (screenshot, photo, diagram), call view_image with its path. This only works if the currently selected model supports vision — if it doesn't, view_image returns an error; tell the user in your reply to switch to a vision-capable model (e.g. qwen3-vl, llava, gemma3) in the model dropdown, then retry. Do not guess what an image contains without calling view_image first.
- You run inside WSL (Linux) but can also operate on the user's Windows system. Windows files live under /mnt/c, /mnt/d, etc. (e.g. C:\\Users\\name\\file becomes /mnt/c/Users/name/file), and read_file/write_file/edit_file/list_dir work on those paths too. To run a Windows program or command, use run_command and invoke it via powershell.exe, e.g. run_command with `powershell.exe -NoProfile -Command "Get-ChildItem"`, or call an .exe directly. Prefer powershell.exe over cmd.exe (cmd.exe prints a warning when the working directory is a WSL path). The workspace itself may be a Windows path such as /mnt/c/Users/name/project.
- To change part of an existing file, prefer edit_file (exact find & replace) instead of rewriting the whole file with write_file. Use write_file only for new files or complete rewrites.
- When you need up-to-date information (library usage, API docs, error messages, versions), use web_search first, then fetch_url on the most promising result. Prefer official documentation.
- Inspect existing files before editing them. Never overwrite a file you have not read.
- After making changes, VERIFY them by running the code, build, or tests with run_command. A task is not done until you have actually executed it yourself and observed it succeed. Never end your turn by telling the user to run the next command (e.g. "next, run cmake and make") — you have run_command, so run it yourself and report the real output.
- Never assume a library, package, CMake target, function, or file path exists just because it sounds plausible. If you are not certain, check it first (list_dir/read_file/run_command such as grep or find, or web_search) before writing code that depends on it. If a build/link error mentions a missing or unresolved name, go look for the real one instead of guessing a similarly-named alternative.
- When the user or a referenced document gives you exact values (pin numbers, addresses, register maps, function/library names, versions), copy them verbatim. Do not substitute your own guess or a "close enough" value, even if it seems reasonable — read the source with read_file and reuse exactly what it says.
- After editing a file that previously built or ran successfully, re-verify the whole thing still builds/runs afterward — treat previously-fixed bugs as things you could accidentally reintroduce, and check for that.
- Keep working autonomously until the task is fully done; do not stop to ask for confirmation.
- Relative paths are resolved from the workspace directory.
- When the task is complete, summarize what you did.
- Always reply to the user in Japanese."""

TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command (bash) in the workspace directory and return exit code, stdout and stderr. Use for building, running, testing, searching (grep), git, installing, etc.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The bash command to run"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file and return its content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path (relative to workspace or absolute)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write content to a file, creating parent directories if needed. Overwrites existing content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path (relative to workspace or absolute)"},
            "content": {"type": "string", "description": "Full file content to write"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Edit an existing text file by exact string replacement. Preferred over write_file for changing part of a file: cheaper and safer than rewriting the whole file. old_string must match the file content exactly, including whitespace and indentation.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path (relative to workspace or absolute)"},
            "old_string": {"type": "string", "description": "Exact existing text to find. Must be unique in the file unless replace_all is true."},
            "new_string": {"type": "string", "description": "Text to replace it with"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"}},
            "required": ["path", "old_string", "new_string"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files and directories at a path.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path, default is workspace root"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web (DuckDuckGo). Returns titles, URLs and snippets. Use for documentation, error messages, library usage, current versions.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Number of results, default 6"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Download a web page and return its readable text content (HTML tags stripped). Use after web_search to read a promising result.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Full URL starting with http:// or https://"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "view_image",
        "description": "Load an image file (png/jpg/gif/webp/bmp) so you can see it. Only works if the current model supports vision; returns an error otherwise.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Image file path (relative to workspace or absolute)"}},
            "required": ["path"]}}},
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def http_get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "ja,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        data = r.read(2_000_000)
    m = re.search(r"charset=([\w-]+)", ctype)
    return data.decode(m.group(1) if m else "utf-8", errors="replace")


def web_search(query: str, max_results: int = 6) -> str:
    html_text = http_get("https://html.duckduckgo.com/html/?q="
                         + urllib.parse.quote(query))
    titles = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html_text, re.S)
    snippets = re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>', html_text, re.S)
    out = []
    for i, (href, title) in enumerate(titles[:max_results]):
        um = re.search(r"[?&]uddg=([^&]+)", href)
        if um:
            href = urllib.parse.unquote(um.group(1))
        title = unescape(re.sub(r"<[^>]+>", "", title)).strip()
        snip = ""
        if i < len(snippets):
            snip = unescape(re.sub(r"<[^>]+>", "", snippets[i])).strip()
        out.append(f"{i + 1}. {title}\n   {href}\n   {snip}")
    return "\n".join(out) if out else "(no results)"


class _TextExtract(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, d):
        if not self.depth and d.strip():
            self.parts.append(d.strip())


def fetch_url_text(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "ERROR: URL must start with http:// or https://"
    html_text = http_get(url)
    p = _TextExtract()
    p.feed(html_text)
    text = "\n".join(p.parts)
    if len(text) > 10000:
        text = text[:10000] + "\n...[truncated]..."
    return text or "(no readable text on this page)"


_CAPS_CACHE: dict[str, list] = {}   # モデル名 -> capabilities配列 (/api/showは変わらないので使い回す)


def model_capabilities(model: str) -> list:
    if model in _CAPS_CACHE:
        return _CAPS_CACHE[model]
    caps = []
    try:
        req = urllib.request.Request(
            OLLAMA + "/api/show", data=json.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            caps = json.loads(r.read()).get("capabilities", [])
    except Exception:
        pass
    _CAPS_CACHE[model] = caps
    return caps


def pdf_to_text(path: Path) -> str:
    """pdftotext(poppler-utils)でPDFからテキストを抽出する。スキャン画像PDF等で
    テキストが取れない場合はその旨を返す(vision対応モデルならview_imageで
    ページを画像化して見る手もあるが、まずはテキスト抽出を試すのが軽量)。"""
    try:
        p = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                           capture_output=True, text=True, timeout=PDF_TIMEOUT)
    except FileNotFoundError:
        return ("ERROR: pdftotext(poppler-utils)が見つかりません。"
                "`sudo apt install poppler-utils` を実行してください。")
    except subprocess.TimeoutExpired:
        return f"ERROR: PDFの読み込みがタイムアウトしました({PDF_TIMEOUT}s)"
    if p.returncode != 0:
        return f"ERROR: pdftotext failed: {p.stderr.strip()[:300]}"
    text = p.stdout
    if not text.strip():
        return "(このPDFから抽出できるテキストがありません。スキャン画像PDFの可能性があります)"
    if len(text) > 60000:
        text = text[:60000] + "\n...[truncated]..."
    return text


def _safe_sid(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", sid)[:40] or "default"


def save_session(sid: str, model: str, workspace: str, messages: list,
                  turn: dict | None = None):
    sid = _safe_sid(sid)
    title = next((m["content"] for m in messages
                  if m.get("role") == "user" and m.get("content")), "(無題)")
    path = HISTORY_DIR / f"{sid}.json"
    # turns: プロンプト受信〜完了/中断までの時刻ログ。既存ファイルがあれば読み継ぐ
    # (save_sessionは毎回ファイル全体を上書きするため、ここで読まないと消えてしまう)。
    turns = []
    if path.exists():
        try:
            turns = json.loads(path.read_text(encoding="utf-8")).get("turns", [])
        except Exception:
            turns = []
    if turn is not None:
        turns.append(turn)
    data = {"sid": sid, "title": title[:60], "updated_at": time.time(),
            "model": model, "workspace": workspace, "messages": messages,
            "turns": turns}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def list_sessions() -> list:
    out = []
    for f in HISTORY_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"sid": d["sid"], "title": d.get("title", "(無題)"),
                        "updated_at": d.get("updated_at", 0),
                        "model": d.get("model", ""),
                        "workspace": d.get("workspace", "")})
        except Exception:
            continue
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out[:200]


def resolve_path(ws: Path, p: str) -> Path:
    full = Path(p) if os.path.isabs(p) else ws / p
    full = full.resolve()
    ws = ws.resolve()
    if not (str(full) == str(ws) or str(full).startswith(str(ws) + os.sep)):
        raise ValueError(f"path is outside the workspace: {p}")
    return full


def under_allowed(p: Path) -> bool:
    """p が ALLOWED_ROOTS のいずれか(既定=HOME + /mnt)の配下か。"""
    p = p.resolve()
    return any(p == root or str(p).startswith(str(root) + os.sep)
               for root in ALLOWED_ROOTS)


def list_subdirs(path: str) -> dict:
    """作業フォルダ選択ダイアログ用。ALLOWED_ROOTS(HOME + Windowsドライブ)配下のみ一覧する。"""
    p = Path(path or DEFAULT_WORKSPACE).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = HOME
    if not p.is_dir() or not under_allowed(p):
        p = HOME
    dirs = sorted((e.name for e in p.iterdir()
                  if e.is_dir() and not e.name.startswith(".")),
                  key=str.lower)
    # 許可ルート自身では「上へ」を出さない(それ以上遡れない)。それ以外は
    # 親も許可ルート配下である限り遡れる。
    parent = (str(p.parent) if p not in ALLOWED_ROOTS and under_allowed(p.parent)
              else None)
    return {"path": str(p), "parent": parent, "dirs": dirs}


def run_command(cmd: str, ws: Path, cancel) -> str:
    # start_new_session=True でプロセスグループを分離し、キャンセル/タイムアウト時に
    # killpg でパイプの先やバックグラウンド子プロセスまで確実に止める
    p = subprocess.Popen(["bash", "-lc", cmd], cwd=ws,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    deadline = time.time() + CMD_TIMEOUT
    killed = ""
    while True:
        try:
            stdout, stderr = p.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                killed = "cancelled by user"
            elif time.time() > deadline:
                killed = f"timed out ({CMD_TIMEOUT}s)"
            if killed:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = p.communicate()
                break
    out = (stdout or "") + (("\n[stderr]\n" + stderr) if stderr else "")
    if len(out) > 12000:
        out = out[:6000] + "\n...[truncated]...\n" + out[-6000:]
    if killed:
        return f"ERROR: command {killed}\n{out}"
    return f"exit_code={p.returncode}\n{out}"


def exec_tool(name: str, args: dict, ws: Path, cancel=None, model: str | None = None,
              pending_images: list | None = None) -> str:
    try:
        if name == "run_command":
            return run_command(args["command"], ws, cancel)
        if name == "read_file":
            f = resolve_path(ws, args["path"])
            if f.suffix.lower() == ".pdf":
                return pdf_to_text(f)
            t = f.read_text(errors="replace")
            if len(t) > 60000:
                t = t[:60000] + "\n...[truncated]..."
            return t
        if name == "write_file":
            f = resolve_path(ws, args["path"])
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(args["content"])
            return f"OK: wrote {len(args['content'])} chars to {args['path']}"
        if name == "edit_file":
            f = resolve_path(ws, args["path"])
            old, new = args["old_string"], args["new_string"]
            if not old:
                return "ERROR: old_string must not be empty"
            if old == new:
                return "ERROR: old_string and new_string are identical"
            if not f.is_file():
                return f"ERROR: file not found: {args['path']}"
            t = f.read_text(errors="replace")
            n = t.count(old)
            if n == 0:
                return ("ERROR: old_string not found in file. Use read_file to see "
                        "the current content and copy the exact text including "
                        "whitespace and indentation. If matching is too hard, "
                        "rewrite the file with write_file instead.")
            if n > 1 and not args.get("replace_all"):
                return (f"ERROR: old_string occurs {n} times. Include more "
                        "surrounding lines to make it unique, or set "
                        "replace_all=true to replace every occurrence.")
            f.write_text(t.replace(old, new))
            return (f"OK: replaced {n if args.get('replace_all') else 1} "
                    f"occurrence(s) in {args['path']}")
        if name == "list_dir":
            f = resolve_path(ws, args.get("path") or ".")
            items = sorted(e.name + ("/" if e.is_dir() else "") for e in f.iterdir())
            return "\n".join(items)[:8000] or "(empty)"
        if name == "web_search":
            return web_search(args["query"], int(args.get("max_results") or 6))
        if name == "fetch_url":
            return fetch_url_text(args["url"])
        if name == "view_image":
            f = resolve_path(ws, args["path"])
            if f.suffix.lower() not in IMAGE_EXTS:
                return f"ERROR: not a supported image type: {f.suffix or '(none)'}"
            if not f.is_file():
                return f"ERROR: file not found: {args['path']}"
            if "vision" not in model_capabilities(model or ""):
                return (f"ERROR: the current model ('{model}') does not support vision. "
                        "Tell the user to switch to a vision-capable model "
                        "(e.g. qwen3-vl, llava, gemma3) in the model dropdown, then retry.")
            data = f.read_bytes()
            if len(data) > IMAGE_MAX_BYTES:
                return f"ERROR: image too large (>{IMAGE_MAX_BYTES // 1_000_000}MB)"
            if pending_images is not None:
                pending_images.append(base64.b64encode(data).decode())
            return f"OK: loaded {args['path']}, it will be shown to you now"
        return f"ERROR: unknown tool {name}"
    except Exception as e:  # noqa: BLE001 - report all tool errors to the model
        return f"ERROR: {type(e).__name__}: {e}"


def ollama_stream(payload: dict):
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            line = line.strip()
            if line:
                yield json.loads(line)


def ollama_ask(model: str, prompt: str) -> str:
    """非ストリーミングの単発問い合わせ (履歴要約用)。"""
    payload = {"model": model, "stream": False,
               "messages": [{"role": "user", "content": prompt}],
               "options": {"num_ctx": NUM_CTX}}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read()).get("message", {}).get("content", "")


# ---------- 履歴の自動圧縮 ----------
def estimate_text_tokens(t: str) -> int:
    """トークン数の概算。ASCIIは4文字/トークン、日本語等の非ASCIIは1文字/トークンで見積もる。"""
    ascii_n = sum(1 for c in t if ord(c) < 128)
    return ascii_n // 4 + (len(t) - ascii_n)


def estimate_tokens(messages: list) -> int:
    total = 0
    for m in messages:
        total += 8  # メッセージ枠のオーバーヘッド
        text = str(m.get("content") or "")
        if m.get("tool_calls"):
            text += json.dumps(m["tool_calls"], ensure_ascii=False)
        total += estimate_text_tokens(text)
        if m.get("images"):
            total += TOKENS_PER_IMAGE * len(m["images"])
    return total


def _excerpt(t: str, limit: int = MSG_EXCERPT_CHARS) -> str:
    """長文を先頭7割+末尾3割の抜粋にする (要約入力の肥大防止)。"""
    if len(t) <= limit:
        return t
    return t[:limit * 7 // 10] + "\n…[中略]…\n" + t[-(limit * 3 // 10):]


def trim_old_tool_results(messages: list) -> bool:
    """直近KEEP_RECENT_TOOLS件を除くツール結果を切り詰める(圧縮の第1段階・安価)。"""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    changed = False
    for i in tool_idx[:-KEEP_RECENT_TOOLS] if len(tool_idx) > KEEP_RECENT_TOOLS else []:
        c = messages[i].get("content") or ""
        if len(c) > TOOL_TRIM_CHARS:
            messages[i]["content"] = (c[:TOOL_TRIM_CHARS]
                                      + "\n...[古いツール結果のため切り詰め]...")
            changed = True
    return changed


def render_transcript(messages: list) -> str:
    """要約プロンプト用に会話を平文化する。各メッセージは抜粋化して肥大を防ぐ。"""
    parts = []
    for m in messages:
        role = m.get("role", "?")
        c = str(m.get("content") or "")
        if m.get("tool_calls"):
            calls = ", ".join(
                f"{tc.get('function', {}).get('name', '?')}"
                f"({json.dumps(tc.get('function', {}).get('arguments', {}), ensure_ascii=False)[:200]})"
                for tc in m["tool_calls"])
            c = (c + f"\n[ツール呼び出し: {calls}]").strip()
        if role == "tool":
            c = f"[{m.get('tool_name', 'tool')}の結果] {c[:800]}"
        else:
            c = _excerpt(c)
        if m.get("images"):
            c += f"\n[画像{len(m['images'])}枚が添付されていた(要約には含まれない)]"
        parts.append(f"### {role}\n{c}")
    return "\n\n".join(parts)


SUMMARIZE_PROMPT = """以下はコーディングエージェントとユーザーの会話ログである。
今後の作業を継続するために必要な情報だけを日本語で簡潔に要約せよ。必ず含めること:
- ユーザーの目的・指示・好み (「覚えておいて」と言われた事項は一字一句そのまま)
- 作成/変更したファイル (パス付き) とその内容の要点
- 判明した重要な技術的事実・決定事項
- 未完了の作業・次にやること
出力は要約本文のみ。前置きや締めの文は不要。

--- 会話ログ ---
{log}
--- ログここまで ---

上記ログを冒頭の指示に従って日本語で要約せよ。出力は要約本文のみ。"""


def summarize_old(old: list, model: str) -> str:
    """古いメッセージ群を要約する。入力が要約1回の上限を超える場合は分割して各々要約。

    (要約入力自体がnum_ctxを超えるとollamaがプロンプト前方=指示部分を切り捨てて
    しまい、要約が壊れる。必ず1回分をSUMMARIZE_INPUT_TOKENS以内に収める)
    """
    chunks, cur, cur_tok = [], [], 0
    for m in old:
        tok = estimate_text_tokens(render_transcript([m]))
        if cur and cur_tok + tok > SUMMARIZE_INPUT_TOKENS:
            chunks.append(cur)
            cur, cur_tok = [], 0
        cur.append(m)
        cur_tok += tok
    if cur:
        chunks.append(cur)
    parts = []
    for ch in chunks:
        s = ollama_ask(model, SUMMARIZE_PROMPT.format(
            log=render_transcript(ch))).strip()
        if s:
            parts.append(s)
    if not parts:
        raise ValueError("empty summary")
    return "\n\n".join(parts)


def _iter_tool_calls_with_results(messages: list):
    """(ツール名, 引数dict, 結果文字列またはNone) を発生順にyieldする。

    assistantのtool_callsと、直後に続くtoolメッセージ群(同じ順序)を突き合わせる。
    """
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            j = i + 1
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except json.JSONDecodeError:
                        args = {}
                result = None
                if j < n and messages[j].get("role") == "tool":
                    result = messages[j].get("content", "")
                    j += 1
                yield name, args, result
            i = j
        else:
            i += 1


def build_work_state(messages: list) -> str:
    """会話履歴(system除く)から、変更ファイル一覧・直近コマンド結果・繰り返し失敗を
    機械的に(LLMを使わず)抽出して短いダッシュボード文字列にする。空なら""を返す。

    圧縮済み(compact_history で要約済み)の古い部分はtool_calls構造が失われている
    ため対象外——直近の非圧縮ウィンドウのみを反映する。古い変更点は要約プロンプト
    (SUMMARIZE_PROMPT)の側で自然文として残る。
    """
    changed_files = []
    commands = []  # (command, result_or_None) を発生順に
    for name, args, result in _iter_tool_calls_with_results(messages):
        if name in ("write_file", "edit_file"):
            path = args.get("path")
            if path and path not in changed_files:
                changed_files.append(path)
        elif name == "run_command":
            commands.append((args.get("command", ""), result))

    lines = []
    if changed_files:
        lines.append("変更したファイル: " + ", ".join(changed_files))

    recent = commands[-RECENT_COMMANDS_SHOWN:]
    if recent:
        lines.append("直近の実行コマンド:")
        for cmd, result in recent:
            r = result or ""
            ok = r.startswith("exit_code=0")
            status = "OK" if ok else "失敗/要確認"
            first_line = r.splitlines()[0] if r else "(結果なし)"
            lines.append(f"  - `{cmd}` → {status} ({first_line[:80]})")

    if len(commands) >= FAIL_REPEAT_THRESHOLD:
        tail = commands[-FAIL_REPEAT_THRESHOLD:]
        same_cmd = len({c for c, _ in tail}) == 1
        all_failed = all(not (r or "").startswith("exit_code=0") for _, r in tail)
        if same_cmd and all_failed:
            lines.append(
                f"⚠ 同じコマンド「{tail[-1][0]}」が直近{FAIL_REPEAT_THRESHOLD}回連続で"
                "失敗しています。同じアプローチを繰り返さず、根本原因を洗い出すか"
                "別の仮説を試してください。")

    return "\n".join(lines)


def compact_history(messages: list, model: str, sse) -> list:
    """messages(先頭はsystem)が予算を超えていたら圧縮して返す。超えていなければそのまま。

    第1段階: 古いツール結果の切り詰め (安価・LLM不使用)
    第2段階: 直近KEEP_RECENT_MSGS件を残して古い部分をLLMで要約し1メッセージに置換
    要約失敗時: 古い部分を単純に省略 (最後の手段。文脈は失われるが溢れて壊れるよりよい)
    """
    budget = NUM_CTX - RESERVE_TOKENS
    est = estimate_tokens(messages)
    if est <= budget:
        return messages

    trim_old_tool_results(messages)
    est2 = estimate_tokens(messages)
    if est2 <= budget:
        sse({"type": "compact",
             "message": f"古いツール結果を切り詰めました (推定 {est}→{est2} トークン)"})
        return messages

    body = messages[1:]
    split = len(body) - KEEP_RECENT_MSGS
    # toolメッセージは直前のassistant(tool_calls)とペアなので、境界がtoolなら手前へずらす
    while split > 0 and body[split].get("role") == "tool":
        split -= 1
    if split <= 0:
        return messages  # 直近メッセージだけで予算超過。これ以上は縮められない
    old, recent = body[:split], body[split:]

    sse({"type": "compact", "message": "履歴が長いため古い部分を要約しています…"})
    try:
        summary = summarize_old(old, model)
        marker = ("【自動要約】ここまでの会話が長くなったため、"
                  "古い部分は以下の要約に置き換えられた:\n" + summary)
    except Exception as e:
        marker = ("【自動省略】以前の会話は長すぎたため省略された "
                  f"(要約も失敗: {type(e).__name__})。必要な情報は改めて確認すること。")
    compacted = [messages[0], {"role": "user", "content": marker}, *recent]
    est3 = estimate_tokens(compacted)
    sse({"type": "compact",
         "message": f"履歴を圧縮しました (推定 {est}→{est3} トークン)"})
    return compacted


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # quiet
        pass

    # ---------- helpers ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse(self, obj):
        self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _host_ok(self) -> bool:
        """Host がローカル以外 → DNSリバインディング攻撃なので拒否。GET/POST共通。"""
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def _token_ok(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-LocalCoder-Token", ""), TOKEN)

    def _post_ok(self) -> bool:
        """POST の CSRF / DNSリバインディング対策。

        - Host がローカル以外 → DNSリバインディング攻撃
        - Origin がローカル以外 → 他サイトからのクロスオリジンPOST
        - Content-Type が application/json 以外 → no-cors で送れる単純リクエスト
        - トークン不一致 → このページを経由しないリクエスト
        """
        if not self._host_ok():
            return False
        origin = self.headers.get("Origin")
        if origin:
            if urllib.parse.urlparse(origin).hostname not in ("localhost", "127.0.0.1"):
                return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            return False
        return self._token_ok()

    # ---------- GET ----------
    def do_GET(self):
        if not self._host_ok():
            self._json({"error": "forbidden"}, 403)
            return
        if self.path in ("/", "/index.html"):
            body = (ROOT / "index.html").read_bytes()
            inject = (f'<script>window.LC_TOKEN={json.dumps(TOKEN)};'
                     f'window.LC_DEFAULT_WORKSPACE={json.dumps(DEFAULT_WORKSPACE)};'
                     f'</script></head>').encode()
            body = body.replace(b"</head>", inject, 1)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/vendor/"):
            # 同梱JS(marked/DOMPurify)の静的配信。CDN依存を排除しオフラインでも動く
            name = self.path[len("/vendor/"):]
            f = ROOT / "vendor" / name
            if re.fullmatch(r"[\w.-]+\.js", name) and f.is_file():
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)
        elif self.path == "/api/models":
            try:
                with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as r:
                    data = json.loads(r.read())
                names = [m["name"] for m in data.get("models", [])]
                models = [{"name": n, "vision": "vision" in model_capabilities(n)}
                         for n in names]
                self._json({"models": models})
            except Exception as e:
                self._json({"error": f"Ollamaに接続できません: {e}"}, 502)
        elif self.path.startswith("/api/browse"):
            # フォルダ選択ダイアログ用。ディレクトリ構造の開示のためトークン必須
            if not self._token_ok():
                self._json({"error": "forbidden"}, 403)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(list_subdirs(q.get("path", [""])[0]))
        elif self.path == "/api/sessions":
            # 履歴はプロンプト・ツール結果・ファイル内容を含むためトークン必須
            if not self._token_ok():
                self._json({"error": "forbidden"}, 403)
                return
            self._json({"sessions": list_sessions()})
        elif self.path.startswith("/api/session?"):
            if not self._token_ok():
                self._json({"error": "forbidden"}, 403)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sid = _safe_sid(q.get("sid", [""])[0])
            f = HISTORY_DIR / f"{sid}.json"
            if f.exists():
                self._json(json.loads(f.read_text(encoding="utf-8")))
            else:
                self._json({"error": "not found"}, 404)
        elif self.path == "/api/health":
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    # ---------- POST ----------
    def do_POST(self):
        if not self._post_ok():
            self._json({"error": "forbidden"}, 403)
            return
        if self.path == "/api/stop":
            body = self._read_body()
            ev = CANCEL.get(body.get("sid", ""))
            if ev:
                ev.set()
            self._json({"ok": True})
            return
        if self.path == "/api/session/delete":
            body = self._read_body()
            f = HISTORY_DIR / f"{_safe_sid(body.get('sid', ''))}.json"
            if f.exists():
                f.unlink()
            self._json({"ok": True})
            return
        if self.path == "/api/chat":
            self.handle_chat()
            return
        self._json({"error": "not found"}, 404)

    def handle_chat(self):
        turn_started_at = time.time()  # プロンプト受信時刻 (中断/完了時刻とセットで記録する)
        turn_status = "completed"
        body = self._read_body()
        model = body.get("model", "gpt-oss:20b")
        ws = Path(body.get("workspace", "~")).expanduser()
        sid = body.get("sid", "default")
        ev = CANCEL.setdefault(sid, threading.Event())
        ev.clear()

        self._sse_headers()
        if not ws.is_dir():
            self._sse({"type": "error", "message": f"ワークスペースが存在しません: {ws}"})
            return
        wsr = ws.resolve()
        if not under_allowed(wsr):
            roots = ", ".join(str(r) for r in ALLOWED_ROOTS)
            self._sse({"type": "error",
                       "message": f"ワークスペースは {roots} 配下のみ指定できます: {ws}"})
            return

        messages = [{"role": "system", "content": SYSTEM_PROMPT.format(ws=ws)}]
        messages += body.get("messages", [])
        empty_retries = 0
        http_retries = 0

        try:
            for it in range(MAX_ITER):
                # 予算超過時は自動圧縮 (リクエスト開始時とツール結果肥大時の両方を守る)
                messages = compact_history(messages, model, self._sse)
                # 作業状態ダッシュボードはOllama呼び出し1回分にのみ差し込む使い捨て
                # メッセージ。保存される会話履歴(messages)自体には加えない。
                work_state = build_work_state(messages[1:])
                call_messages = messages
                if work_state:
                    call_messages = messages + [
                        {"role": "user", "content": WORK_STATE_PREFIX + work_state}]
                payload = {"model": model, "messages": call_messages, "tools": TOOLS,
                           "stream": True, "options": {"num_ctx": NUM_CTX}}
                content, thinking, tool_calls = "", "", []
                try:
                    for chunk in ollama_stream(payload):
                        if ev.is_set():
                            turn_status = "stopped"
                            self._sse({"type": "error", "message": "停止しました"})
                            return
                        msg = chunk.get("message", {})
                        if msg.get("thinking"):
                            thinking += msg["thinking"]
                            self._sse({"type": "think", "text": msg["thinking"]})
                        if msg.get("content"):
                            content += msg["content"]
                            self._sse({"type": "token", "text": msg["content"]})
                        if msg.get("tool_calls"):
                            tool_calls.extend(msg["tool_calls"])
                        if chunk.get("done"):
                            break
                except urllib.error.URLError as e:
                    # 500/接続エラー等。GPU/VRAMの瞬間的な負荷などで一時的に
                    # 失敗することがあるため、ユーザーの手を止めず1回だけ自動再試行する。
                    if http_retries < HTTP_RETRY_LIMIT:
                        http_retries += 1
                        self._sse({"type": "notice",
                                   "message": f"Ollama接続エラーが発生したため再試行しています… ({e})"})
                        time.sleep(HTTP_RETRY_DELAY)
                        continue
                    raise

                amsg = {"role": "assistant", "content": content}
                if tool_calls:
                    amsg["tool_calls"] = tool_calls
                messages.append(amsg)
                self._sse({"type": "turn_done"})

                if not tool_calls:
                    if not content.strip() and empty_retries < EMPTY_RETRY_LIMIT:
                        # 本文なし・ツール呼び出しなしで終える"空応答"は、ユーザーには
                        # 何も起きていないように見えて実質的に停止してしまう。
                        # 1回だけ自動で続行を促し、それでも空なら諦めて通知する。
                        empty_retries += 1
                        self._sse({"type": "notice",
                                   "message": "モデルが空の応答を返したため、続行を促しています…"})
                        messages.append({"role": "user", "content": EMPTY_RESPONSE_NUDGE})
                        continue
                    if not content.strip():
                        self._sse({"type": "notice",
                                   "message": "⚠ モデルが空の応答のまま停止しました。"
                                              "具体的な指示を送って続けさせてください。"})
                    break

                pending_images = []
                for tc in tool_calls:
                    if ev.is_set():
                        turn_status = "stopped"
                        self._sse({"type": "error", "message": "停止しました"})
                        return
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    self._sse({"type": "tool_start", "name": name, "args": args})
                    result = exec_tool(name, args, ws, ev, model=model,
                                       pending_images=pending_images)
                    self._sse({"type": "tool_end", "name": name,
                               "result": result if len(result) <= 4000
                               else result[:4000] + "\n...[truncated]..."})
                    messages.append({"role": "tool", "tool_name": name,
                                     "name": name, "content": result})
                if pending_images:
                    # view_imageで読み込んだ画像は、tool結果(テキストのみ)とは別に
                    # 合成のuserメッセージとして差し込み、次のOllama呼び出しで
                    # visionモデルに実際に見せる。ライブ表示用にSSEでも個別に送る。
                    messages.append({"role": "user",
                                     "content": "(view_imageで読み込んだ画像)",
                                     "images": pending_images})
                    for b64 in pending_images:
                        self._sse({"type": "image", "b64": b64})
            else:
                turn_status = "max_iter"
                self._sse({"type": "error",
                           "message": f"最大ループ回数({MAX_ITER})に達しました"})

            # システムプロンプトを除いた全履歴を返す(次ターンで文脈維持)
            self._sse({"type": "history", "messages": messages[1:]})
            self._sse({"type": "all_done"})
        except (BrokenPipeError, ConnectionResetError):
            turn_status = "disconnected"
        except urllib.error.URLError as e:
            turn_status = "error"
            try:
                self._sse({"type": "error", "message": f"Ollama接続エラー: {e}"})
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            turn_status = "error"
            try:
                self._sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
            except Exception:
                pass
        finally:
            # 会話を自動保存 (エラーや途中停止でもそこまでの内容を残す)
            # あわせて「プロンプトを受けてから完了/中断するまで」の時刻も記録する
            turn = {"started_at": turn_started_at, "ended_at": time.time(),
                    "status": turn_status}
            if len(messages) > 1:
                try:
                    save_session(sid, model, str(ws), messages[1:], turn=turn)
                except Exception:
                    pass


def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"port {PORT} already in use — LocalCoder is probably already running")
        return
    print(f"LocalCoder running: http://localhost:{PORT}  (ollama: {OLLAMA})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
```

### ② `~/localcoder/index.html`

```html
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>LocalCoder — ローカルLLMコーディングエージェント</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:#12141a; --panel:#1b1e27; --panel2:#232733; --border:#2e3342;
  --text:#e6e8ef; --dim:#8b91a3; --accent:#e8734a; --green:#4ac28b; --red:#e05b5b;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:"Segoe UI","Yu Gothic UI",sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;gap:10px;align-items:center;padding:10px 16px;
  background:var(--panel);border-bottom:1px solid var(--border);flex-wrap:wrap}
header h1{font-size:16px;margin:0 8px 0 0;color:var(--accent)}
label{font-size:12px;color:var(--dim)}
select,input[type=text]{background:var(--panel2);color:var(--text);border:1px solid var(--border);
  border-radius:6px;padding:6px 8px;font-size:13px}
#workspace{width:320px}
button{background:var(--panel2);color:var(--text);border:1px solid var(--border);
  border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer}
button:hover{border-color:var(--accent)}
#stopBtn{display:none;border-color:var(--red);color:var(--red)}
#status{font-size:12px;color:var(--dim);margin-left:auto}
#layout{flex:1;display:flex;min-height:0}
#side{width:230px;flex-shrink:0;background:var(--panel);border-right:1px solid var(--border);
  overflow-y:auto;display:flex;flex-direction:column}
#sideHead{padding:10px 12px;font-size:12px;color:var(--dim);border-bottom:1px solid var(--border);
  position:sticky;top:0;background:var(--panel)}
.sess{padding:8px 10px;border-bottom:1px solid var(--border);cursor:pointer;font-size:13px;
  display:flex;gap:6px;align-items:flex-start}
.sess:hover{background:var(--panel2)}
.sess.active{background:var(--panel2);box-shadow:inset 3px 0 0 var(--accent)}
.sess .body{flex:1;min-width:0}
.sess .st{overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.4}
.sess .sd{color:var(--dim);font-size:11px;margin-top:2px}
.sess .del{color:var(--dim);padding:0 4px;flex-shrink:0}
.sess .del:hover{color:var(--red)}
#maincol{flex:1;display:flex;flex-direction:column;min-width:0}
#chat{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:860px;padding:10px 14px;border-radius:10px;line-height:1.55;
  white-space:pre-wrap;word-break:break-word;font-size:14px}
.user{align-self:flex-end;background:#2a3550;border:1px solid #3a4a70}
.assistant{align-self:flex-start;background:var(--panel);border:1px solid var(--border)}
.assistant.md{white-space:normal}
.assistant.md pre{background:#0d0f14;padding:10px;border-radius:8px;overflow-x:auto;font-size:13px}
.assistant.md code{background:#0d0f14;padding:1px 5px;border-radius:4px;font-size:13px}
.think{align-self:flex-start;color:var(--dim);font-size:12px;max-width:860px;
  border-left:3px solid var(--border);padding:4px 10px;white-space:pre-wrap}
details.tool{align-self:flex-start;max-width:860px;width:fit-content;background:var(--panel2);
  border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:13px}
details.tool summary{cursor:pointer;color:var(--green)}
details.tool pre{background:#0d0f14;padding:8px;border-radius:6px;overflow-x:auto;
  max-height:260px;font-size:12px;white-space:pre-wrap}
.imgmsg{display:flex;flex-wrap:wrap;gap:8px;background:transparent;border:none;padding:0}
.imgmsg img{max-width:280px;max-height:280px;border-radius:8px;border:1px solid var(--border);object-fit:contain}
.err{align-self:center;color:var(--red);font-size:13px}
.notice{align-self:center;color:#e0b04a;background:#2a2418;border:1px solid #4a3f24;
  border-radius:8px;padding:6px 14px;font-size:13px}
footer{display:flex;gap:10px;padding:12px 16px;background:var(--panel);border-top:1px solid var(--border)}
#input{flex:1;background:var(--panel2);color:var(--text);border:1px solid var(--border);
  border-radius:8px;padding:10px;font-size:14px;resize:none;font-family:inherit;min-height:44px;max-height:200px}
#sendBtn{background:var(--accent);color:#fff;border:none;padding:0 22px;font-weight:600}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--dim);
  border-top-color:var(--accent);border-radius:50%;animation:sp 1s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
#browseBtn{padding:6px 10px}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;
  align-items:center;justify-content:center;z-index:100}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:10px;
  width:520px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;
  box-shadow:0 10px 40px rgba(0,0,0,.4)}
.modal h3{margin:0;padding:12px 16px;font-size:14px;border-bottom:1px solid var(--border);
  color:var(--accent)}
.modal .curpath{padding:8px 16px;font-size:12px;color:var(--dim);word-break:break-all;
  border-bottom:1px solid var(--border)}
.modal .dirlist{flex:1;overflow-y:auto;padding:6px 0}
.modal .direntry{padding:8px 16px;cursor:pointer;font-size:13px;display:flex;gap:8px;align-items:center}
.modal .direntry:hover{background:var(--panel2)}
.modal .direntry.up{color:var(--dim)}
.modal .empty{padding:16px;color:var(--dim);font-size:13px}
.modal .modal-footer{display:flex;gap:8px;justify-content:flex-end;padding:10px 16px;
  border-top:1px solid var(--border)}
.modal .modal-footer button.primary{background:var(--accent);color:#fff;border:none}
</style>
</head>
<body>
<header>
  <h1>🛠 LocalCoder</h1>
  <label>モデル <select id="model"></select></label>
  <span id="visionBadge" style="font-size:12px;color:var(--dim)"></span>
  <label>作業フォルダ <input type="text" id="workspace" placeholder="/home/youruser/project"></label>
  <button id="browseBtn" title="フォルダを選ぶ">📁 参照</button>
  <button id="newBtn">＋ 新規チャット</button>
  <button id="stopBtn">■ 停止</button>
  <span id="status"></span>
</header>
<div id="browseModal" class="modal-overlay" style="display:none">
  <div class="modal">
    <h3>📁 作業フォルダを選ぶ</h3>
    <div class="curpath" id="browsePath"></div>
    <div class="dirlist" id="browseList"></div>
    <div class="modal-footer">
      <button id="browseCancel">キャンセル</button>
      <button id="browseSelect" class="primary">このフォルダを選択</button>
    </div>
  </div>
</div>
<div id="layout">
  <aside id="side">
    <div id="sideHead">📚 履歴（クリックで再開）</div>
    <div id="sessions"></div>
  </aside>
  <div id="maincol">
    <div id="chat"></div>
    <footer>
      <textarea id="input" placeholder="やりたいことを日本語で入力 (Shift+Enterで改行 / Enterで送信)"></textarea>
      <button id="sendBtn">送信</button>
    </footer>
  </div>
</div>
<!-- CDNではなく同梱JSを配信 (vendor/ 内、バージョンは *.version 参照)。
     外部CDNのJSはこのページの権限(=コマンド実行)を持ってしまうため使わない -->
<script src="/vendor/marked.min.js"></script>
<script src="/vendor/purify.min.js"></script>
<script>
const $=id=>document.getElementById(id);
const chat=$("chat"), input=$("input"), status=$("status");
let sid=newSid();
let history=[];      // サーバに渡す完全な会話履歴(tool呼び出し含む)
let running=false;

function newSid(){return Date.now().toString(36)+Math.random().toString(36).slice(2,8)}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;chat.appendChild(e);scroll();return e}
function scroll(){chat.scrollTop=chat.scrollHeight}
function md(e,text){
  // LLM出力・Webから取得した内容にHTMLが混ざってもXSSにならないようsanitize必須
  if(window.marked&&window.DOMPurify){e.classList.add("md");e.innerHTML=DOMPurify.sanitize(marked.parse(text))}
  else e.textContent=text;
}
function post(url,obj){
  return fetch(url,{method:"POST",headers:{"Content-Type":"application/json",
    "X-LocalCoder-Token":window.LC_TOKEN||""},body:JSON.stringify(obj)});
}
function getAuth(url){
  // 履歴系GETはプライベートな内容を返すためトークン必須
  return fetch(url,{headers:{"X-LocalCoder-Token":window.LC_TOKEN||""}});
}
function argSummary(name,args){
  if(typeof args==="string"){try{args=JSON.parse(args)}catch{return args}}
  return String(args.command||args.path||args.query||args.url||JSON.stringify(args)).slice(0,120);
}
function toolCard(name,argstr,live){
  const d=document.createElement("details"); d.className="tool";
  d.innerHTML="<summary>🔧 <b></b> — <code></code>"+(live?" <span class='spin'></span>":"")+"</summary><pre></pre>";
  d.querySelector("b").textContent=name;
  d.querySelector("code").textContent=argstr;
  d.querySelector("pre").textContent=live?"実行中…":"";
  chat.appendChild(d); scroll(); return d;
}
function setRunning(v){
  running=v;
  $("sendBtn").disabled=v;
  $("stopBtn").style.display=v?"inline-block":"none";
  status.innerHTML=v?'<span class="spin"></span> 実行中…':"";
}

let MODEL_VISION={};   // モデル名 -> vision対応かどうか
function updateVisionBadge(){
  const name=$("model").value;
  const badge=$("visionBadge");
  if(!name){badge.textContent="";return}
  badge.textContent=MODEL_VISION[name]?"👁 画像対応":"🚫 画像非対応";
}
async function loadModels(){
  try{
    const r=await fetch("/api/models"); const d=await r.json();
    if(d.error){el("div","err",d.error);return}
    const sel=$("model"); sel.innerHTML=""; MODEL_VISION={};
    const names=d.models.map(m=>{MODEL_VISION[m.name]=!!m.vision;return m.name});
    for(const m of names){const o=document.createElement("option");o.value=o.textContent=m;sel.appendChild(o)}
    const pref=["gpt-oss:20b","glm-4.7-flash:latest","qwen3:8b"];
    for(const p of pref){if(names.includes(p)){sel.value=p;break}}
    updateVisionBadge();
  }catch(e){el("div","err","サーバに接続できません: "+e)}
}
$("model").addEventListener("change",updateVisionBadge);

// ---------- 履歴サイドバー ----------
async function loadSessions(){
  try{
    const d=await(await getAuth("/api/sessions")).json();
    const box=$("sessions"); box.innerHTML="";
    for(const s of d.sessions){
      const item=document.createElement("div");
      item.className="sess"+(s.sid===sid?" active":"");
      const dt=new Date(s.updated_at*1000);
      const ds=`${dt.getMonth()+1}/${dt.getDate()} ${String(dt.getHours()).padStart(2,"0")}:${String(dt.getMinutes()).padStart(2,"0")}`;
      item.innerHTML="<div class='body'><div class='st'></div><div class='sd'></div></div><span class='del' title='削除'>✕</span>";
      item.querySelector(".st").textContent=s.title;
      item.querySelector(".sd").textContent=ds;
      item.onclick=()=>openSession(s.sid);
      item.querySelector(".del").onclick=async ev=>{
        ev.stopPropagation();
        if(!confirm("この会話を削除しますか？\n"+s.title))return;
        await post("/api/session/delete",{sid:s.sid});
        if(s.sid===sid)newChat();
        loadSessions();
      };
      box.appendChild(item);
    }
  }catch(e){/* sidebar failure is non-fatal */}
}

async function openSession(id){
  if(running)return;
  try{
    const d=await(await getAuth("/api/session?sid="+encodeURIComponent(id))).json();
    if(d.error)return;
    sid=d.sid; history=d.messages||[];
    if(d.workspace)$("workspace").value=d.workspace;
    const sel=$("model");
    if(d.model&&[...sel.options].some(o=>o.value===d.model))sel.value=d.model;
    updateVisionBadge();
    renderHistory(history);
    loadSessions();
  }catch(e){el("div","err","履歴の読み込みに失敗: "+e)}
}

function imageBubble(images){
  const d=document.createElement("div"); d.className="msg assistant imgmsg";
  for(const b64 of images){
    const img=document.createElement("img");
    img.src="data:image/png;base64,"+b64;
    d.appendChild(img);
  }
  chat.appendChild(d); scroll(); return d;
}
function renderHistory(msgs){
  chat.innerHTML="";
  const pending=[];
  for(const m of msgs){
    if(m.role==="user"&&m.images&&m.images.length){imageBubble(m.images)}
    else if(m.role==="user"){el("div","msg user",m.content)}
    else if(m.role==="assistant"){
      if(m.content){const e=el("div","msg assistant","");md(e,m.content)}
      for(const tc of (m.tool_calls||[])){
        const fn=tc.function||{};
        pending.push(toolCard(fn.name||"?",argSummary(fn.name,fn.arguments||{}),false));
      }
    }else if(m.role==="tool"){
      const d=pending.shift();
      if(d)d.querySelector("pre").textContent=m.content;
    }
  }
  scroll();
}

function newChat(){sid=newSid();history=[];chat.innerHTML="";loadSessions()}

// ---------- 完了ノーティス (ベル音 + デスクトップ通知) ----------
if(window.Notification&&Notification.permission==="default"){
  Notification.requestPermission();
}
function playBell(){
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    const o=ctx.createOscillator(), g=ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.type="sine"; o.frequency.value=880; g.gain.value=0.001;
    g.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime+0.01);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime+0.35);
    o.start(); o.stop(ctx.currentTime+0.4);
  }catch(e){/* 音が出せない環境でも無視 */}
}
function notifyDone(body){
  playBell();
  if(window.Notification&&Notification.permission==="granted"){
    try{new Notification("🛠 LocalCoder — 入力待ちです",{body:(body||"").slice(0,200)})}
    catch(e){}
  }
}
let stoppedByUser=false;

// ---------- 送信 ----------
async function send(){
  const text=input.value.trim();
  if(!text||running)return;
  input.value="";
  el("div","msg user",text);
  history.push({role:"user",content:text});
  setRunning(true);
  stoppedByUser=false;

  let curAssistant=null, curThink=null, curText="";
  try{
    const resp=await post("/api/chat",{sid,model:$("model").value,workspace:$("workspace").value,messages:history});
    const reader=resp.body.getReader(); const dec=new TextDecoder(); let buf="";
    while(true){
      const {done,value}=await reader.read();
      if(done)break;
      buf+=dec.decode(value,{stream:true});
      let i;
      while((i=buf.indexOf("\n\n"))>=0){
        const line=buf.slice(0,i).trim(); buf=buf.slice(i+2);
        if(!line.startsWith("data:"))continue;
        const ev=JSON.parse(line.slice(5));
        handle(ev);
      }
    }
  }catch(e){el("div","err","通信エラー: "+e)}
  setRunning(false);
  loadSessions();
  if(!stoppedByUser)notifyDone(curText||"作業が完了し、入力待ちに戻りました。");

  function handle(ev){
    if(ev.type==="think"){
      if(!curThink)curThink=el("div","think","");
      curThink.textContent+=ev.text; scroll();
    }else if(ev.type==="token"){
      curThink=null;
      if(!curAssistant){curAssistant=el("div","msg assistant","");curText=""}
      curText+=ev.text; curAssistant.textContent=curText; scroll();
    }else if(ev.type==="turn_done"){
      if(curAssistant)md(curAssistant,curText);
      curAssistant=null; curThink=null; scroll();
    }else if(ev.type==="image"){
      imageBubble([ev.b64]);
    }else if(ev.type==="tool_start"){
      toolCard(ev.name,argSummary(ev.name,ev.args),true);
    }else if(ev.type==="tool_end"){
      const tools=chat.querySelectorAll("details.tool");
      const d=tools[tools.length-1];
      if(d){d.querySelector(".spin")?.remove(); d.querySelector("pre").textContent=ev.result}
      scroll();
    }else if(ev.type==="compact"){
      el("div","think","🗜 "+ev.message);
    }else if(ev.type==="notice"){
      el("div","notice","🔔 "+ev.message);
    }else if(ev.type==="history"){
      history=ev.messages;
    }else if(ev.type==="error"){
      el("div","err","⚠ "+ev.message);
    }
  }
}

$("sendBtn").onclick=send;
input.addEventListener("keydown",e=>{
  if(e.key==="Enter"&&!e.shiftKey&&!e.isComposing){e.preventDefault();send()}
});
$("stopBtn").onclick=()=>{stoppedByUser=true;post("/api/stop",{sid})};
$("newBtn").onclick=newChat;
if(window.LC_DEFAULT_WORKSPACE)$("workspace").value=window.LC_DEFAULT_WORKSPACE;
loadModels();
loadSessions();

// ---------- フォルダ選択ダイアログ ----------
let browsePath="";
async function openBrowse(){
  $("browseModal").style.display="flex";
  await browseTo($("workspace").value.trim());
}
async function browseTo(path){
  try{
    const d=await(await getAuth("/api/browse?path="+encodeURIComponent(path||""))).json();
    if(d.error){el("div","err","フォルダ一覧の取得に失敗: "+d.error);return}
    browsePath=d.path;
    $("browsePath").textContent=d.path;
    const list=$("browseList"); list.innerHTML="";
    if(d.parent){
      const up=document.createElement("div");
      up.className="direntry up"; up.textContent="⬆ .. (上へ)";
      up.onclick=()=>browseTo(d.parent);
      list.appendChild(up);
    }
    if(!d.dirs.length&&!d.parent){
      list.innerHTML+="<div class='empty'>サブフォルダはありません</div>";
    }
    for(const name of d.dirs){
      const it=document.createElement("div");
      it.className="direntry"; it.textContent="📁 "+name;
      it.onclick=()=>browseTo(d.path+"/"+name);
      list.appendChild(it);
    }
  }catch(e){el("div","err","フォルダ一覧の取得に失敗: "+e)}
}
$("browseBtn").onclick=openBrowse;
$("browseCancel").onclick=()=>{$("browseModal").style.display="none"};
$("browseSelect").onclick=()=>{
  $("workspace").value=browsePath;
  $("browseModal").style.display="none";
};
$("browseModal").addEventListener("click",e=>{
  if(e.target===$("browseModal"))$("browseModal").style.display="none";
});
</script>
</body>
</html>
```

### ③ デスクトップの `LocalCoder.bat`（Windows側）

2種類ある。**mirroredネットワークが使える機種は「標準版」で十分**（このリポジトリの
動作確認PCも標準版を使用中）。**WSLがNAT構成（mirrored非対応）の機種、または標準版が
ダブルクリックしても無反応になる機種**は「ASCII安全版＋LocalCoder.ps1」を使う。

**標準版**（mirroredネットワーク機。「1-2」のmirrored設定が入っていれば動く）:

```bat
@echo off
rem LocalCoder — ローカルLLM(Ollama)コーディングエージェント起動
rem サーバーが既に起動していれば二重起動しない(server.py側で処理)
start "LocalCoder Server" /min wsl -d ubuntu-24.04 -- bash -lc "python3 ~/localcoder/server.py"
ping -n 3 127.0.0.1 >nul
start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app=http://localhost:8765/
```

**ASCII安全版＋`LocalCoder.ps1`**（NAT構成機、または日本語コメント入りbatが
ダブルクリックしても何も起きない機種向け）:

⚠ **bat本体はASCII文字のみで書くこと。** 日本語コメントをUTF-8で書くと、
日本語版Windowsの`cmd.exe`（既定でShift-JISとして解釈する）がバイト列を誤読し、
コメントの途中を別コマンドとして実行しようとして壊れる（実際に発生した事例は
「8. 実施例ログ 6番」参照）。

```bat
@echo off
rem LocalCoder launcher (local-LLM coding agent)
rem NOTE: keep this file ASCII-only; cmd.exe misparses UTF-8 Japanese comments
rem Wake the WSL distro first so the \\wsl.localhost UNC path is reachable
wsl -d ubuntu-24.04 -- true
rem Copy LocalCoder.ps1 to a local path before running it. PowerShell's
rem RemoteSigned policy requires a digital signature for unsigned scripts
rem on network/UNC paths (\\wsl.localhost\...), even though local unsigned
rem scripts are allowed. Running a local copy avoids that restriction
rem without weakening the execution policy. Copying on every launch keeps
rem the repo's LocalCoder.ps1 as the single source of truth (no manual sync).
copy /Y "\\wsl.localhost\<distro>\home\<user>\localcoder\LocalCoder.ps1" "%TEMP%\LocalCoder.ps1" >nul
powershell -NoProfile -File "%TEMP%\LocalCoder.ps1"
```

`LocalCoder.ps1`（リポジトリ同梱）はゲートウェイIPの自動検出→必要ならOllamaをそのIPで
再bind→WSL内でserver.py起動→Edgeでアプリウィンドウを開く、を一括で行う
（Windows 10 + NAT構成のWSLでOllamaへの経路を確保する実装。詳細は「8. 実施例ログ」）。
mirrored機ではこの経路は不要。

⚠ **UNCパス上のps1は`RemoteSigned`ポリシーでも実行がブロックされる。** `powershell -File`に
直接`\\wsl.localhost\...`のパスを渡すと、「デジタル署名されていません。このスクリプトは
現在のシステムでは実行できません」というエラーで失敗する（`@echo off`のため一瞬で
ウィンドウが閉じ、症状としては「ダブルクリックしても何も起きない/黒い画面がすぐ消える」
に見える）。原因はローカルファイルとネットワークパス上のファイルでセキュリティゾーンの
扱いが異なるため。`-ExecutionPolicy Bypass`で回避する方法もあるが、ポリシーの緩和を
伴わない上記の「`%TEMP%`へコピーしてから実行」の方が望ましい（実際に発生した事例は
「8. 実施例ログ」参照）。

### ④ `~/localcoder/vendor/`（同梱JS、CDNは使わない）

marked と DOMPurify をバージョン固定でダウンロードして配置する。`server.py`が
`/vendor/*.js` として静的配信し、`index.html`はCDNではなくこのローカルパスを
参照する（「7. 設計上の注意」10番参照）。

```bash
mkdir -p ~/localcoder/vendor && cd ~/localcoder/vendor
# marked (UMD/minified build)
curl -sL -o marked.min.js "https://cdn.jsdelivr.net/npm/marked@18.0.5/lib/marked.umd.min.js"
echo 18.0.5 > marked.version
# DOMPurify
curl -sL -o purify.min.js "https://cdn.jsdelivr.net/npm/dompurify@3.4.11/dist/purify.min.js"
echo 3.4.11 > dompurify.version
```

バージョンを上げる場合はこのコマンドのバージョン番号を書き換えて再取得するだけでよい
（`*.version`ファイルは記録用でserver.py/index.htmlの動作には使われない）。

---

## 4. 検証手順

構築後、この順で確認する:

```bash
# (WSL内で)
# 1. サーバー起動
nohup python3 ~/localcoder/server.py > ~/localcoder/server.log 2>&1 &

# 2. ヘルスチェック
curl -s http://localhost:8765/api/health          # → {"ok": true}
curl -s http://localhost:8765/api/models          # → モデル一覧が返る
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8765/vendor/marked.min.js  # → 200
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8765/vendor/purify.min.js  # → 200

# 3. CSRFトークンの取得（起動ごとにランダム。index.html配信時に埋め込まれる）
#    POST系APIは全てこのトークンを X-LocalCoder-Token ヘッダで要求する（無いと403）
TOKEN=$(curl -s http://localhost:8765/ | grep -o 'LC_TOKEN="[a-f0-9]*"' | cut -d'"' -f2)
echo "token=$TOKEN"

# 4. エージェントのエンドツーエンドテスト（ワークスペースは $HOME 配下でなければならない）
mkdir -p ~/lc_test
cat > /tmp/lc_req.json <<EOF
{"sid":"test1","model":"gpt-oss:20b","workspace":"$HOME/lc_test",
 "messages":[{"role":"user","content":"hello.py というファイルを作って hello world と出力するようにして、実行して確認して"}]}
EOF
curl -s -N -X POST http://localhost:8765/api/chat \
  -H 'Content-Type: application/json' -H "X-LocalCoder-Token: $TOKEN" \
  --data @/tmp/lc_req.json | tail -5
cat ~/lc_test/hello.py   # → print("hello world") ができていれば合格
```

期待される挙動: SSEで `tool_start`(write_file) → `tool_end` → `tool_start`(run_command) →
… → `all_done` が流れ、hello.py が実際に作成・実行される。トークン無しでPOSTすると
`{"error": "forbidden"}` (403) が返るのが正しい挙動（動作確認ではない、CSRF対策の効果確認）。
（初回はモデルロードで1〜3分かかる。`python`が無ければ`python3`に自動で切り替えるなど、
エラー自己回復が観察できれば完璧）

最後に LocalCoder.bat をダブルクリックし、GUIウィンドウが開いてモデル一覧が
表示されることを確認する。

### 4-1. 自動テスト（Ollama不要・純粋関数のみ）

`tests/` に、履歴圧縮・作業状態ダッシュボード・ツール呼び出しの正規化・履歴
保存を対象にした回帰テストがある（`IMPROVEMENTS.md` §2.1〜2.2の実装）。
標準ライブラリの`unittest`のみで完結し、Ollamaへの接続もモデルのダウンロードも
不要（`ollama_ask`は`tests/_helpers.py`の`FakeOllama`で差し替える）。コードを
変更したら、サーバーを起動する前にまずこれを走らせる。

```bash
cd ~/localcoder
python3 -m unittest discover -s tests -t . -v
```

`tests/fixtures/` には実際に問題が起きたセッションのJSON（パスのみ匿名化した
実データ）を置く運用にしている。**`tests/fixtures/`自体は`.gitignore`対象**
（このマシンにのみ存在し、GitHubへは一切コミットされない）。実際の会話内容
（プロジェクトのコード片・やり取り本文）を含むため、パス匿名化程度では
リポジトリに含めるべきではないと判断した。fixtureが無い環境（`git clone`
直後など）では該当テストだけが`unittest.SkipTest`で自動スキップされ、
スイート全体は失敗しない（`tests/_helpers.py`の`load_fixture`参照）。

このマシンで運用している fixture と対応する不具合（参考。ファイル自体は
このリポジトリには含まれない）:

| fixture | 対応する不具合 |
|---|---|
| `leaked_special_token.json` | ツール名に特殊トークンが混入し呼び出しが永久に失敗 |
| `repeated_compaction.json` | 同一ツール結果の重複で圧縮が頻発しストール |
| `empty_response_near_budget.json` | 予算の99%まで伸びて空応答を繰り返す |
| `stuck_write_file.json` | `write_file`の`content`欠落を繰り返し連続失敗検出が発動 |

新しい実障害セッションが見つかったら、同じ要領で（このマシンの）
`tests/fixtures/`に追加し、その不具合を検知する回帰テストを`tests/test_*.py`
に足す。別のマシンで再構築する場合は、そのマシン上の実障害セッションから
同様に作成する。

---

## 5. 環境差分の調整（移植先で変わる箇所）

| 箇所 | このPCでの値 | 移植先での調整方法 |
|---|---|---|
| WSLディストロ名 | `ubuntu-24.04` | `wsl -l -v` で確認し bat の `-d` を変更 |
| デスクトップパス | `E:\desktop`（移動済み） | 通常は `%USERPROFILE%\Desktop`。PowerShellの `[Environment]::GetFolderPath('Desktop')` で確認 |
| 作業フォルダの初期表示値 | このPCでは`LOCALCODER_DEFAULT_WORKSPACE`環境変数で`/home/fuyuki/pico_dvl/codex`を指定 | 環境変数を移植先のプロジェクトパスに合わせて変更、または未設定のまま(`$HOME`が自動表示される) |
| Edgeのパス | `C:\Program Files (x86)\Microsoft\Edge\...` | 無ければ bat 最終行を `start "" http://localhost:8765/` に |
| Ollamaの場所 | Windows側 localhost:11434 | WSL内Ollamaでも同URLで可。別ホストなら環境変数 `LOCALCODER_OLLAMA` |
| ポート | 8765 | 競合時は環境変数 `LOCALCODER_PORT` |

## 6. カスタマイズポイント

- **ツール追加**: `TOOLS` にJSONスキーマを1個追加し、`exec_tool()` に分岐を1個追加するだけ
- **ループ上限**: `MAX_ITER = 80`、コマンドタイムアウト: `CMD_TIMEOUT = 180`
- **コンテキスト長**: `NUM_CTX = 32768`（VRAMが少ないPCでは16384に下げる）
- **システムプロンプト**: `SYSTEM_PROMPT` を編集（英語で書き「日本語で返答せよ」と指示するのが
  小型モデルには最も安定）
- **モデル選択の優先順位**: index.html の `pref` 配列

## 7. 設計上の注意（AIが再実装・改造する場合）

1. **Ollama native API (`/api/chat`) を使うこと**。OpenAI互換 `/v1/chat/completions` でも
   動くが、native APIは `thinking` フィールド（推論過程）が取れる。ストリーミングは
   JSON Lines形式で、`message.tool_calls` は途中チャンクに現れ、`done:true` で終端。
2. **tool結果メッセージ**は `{"role":"tool","tool_name":名前,"name":名前,"content":結果}`。
   `tool_name`(新)と`name`(旧)の両方を入れるとOllamaのバージョン差を吸収できる。
3. **`run_command` の出力は必ず切り詰める**（12KB上限、先頭6KB+末尾6KB）。
   小型モデルはコンテキスト溢れで即座に破綻する。
4. **会話履歴はクライアント保持**にするとサーバーがステートレスになり実装が単純。
5. **ファイル操作のパス検査**（workspace外拒否）は `resolve()` 後の文字列前方一致で行う。
   シンボリックリンク経由の脱出もこれで防げる。
6. **セキュリティ**: run_command はサンドボックスなしでユーザー権限実行（本人の希望による
   全自動設計）。127.0.0.1バインドだけでは**不十分**（同一PC上で開いている悪意あるWebページが
   `fetch("http://localhost:8765/api/chat", {mode:"no-cors", method:"POST", ...})` のような
   no-corsリクエストを送れてしまい、任意プロンプトでのコマンド実行につながる）。そのため
   起動ごとのランダムトークン（`X-LocalCoder-Token`）＋Origin/Host/Content-Type検証を
   全POST APIに必須化している（`_post_ok()`）。承認プロンプトを増やさずに閉じられる
   唯一の攻撃面がここなので、再実装する場合もこの層は省略しないこと。
7. **停止ボタンを機能させる**: `subprocess.run()`は結果を返すまでブロックするため、
   キャンセル要求が来ても実行中のコマンドを止められない。`Popen(start_new_session=True)`
   でプロセスグループを分離し、キャンセル/タイムアウト時に `os.killpg(pid, SIGKILL)` で
   子プロセスごと確実に終了させる（`run_command()`）。
8. **ワークスペースはホーム配下に制限**: リクエストの`workspace`値をそのまま信用すると
   `/etc`等を作業場に指定されうる。`ws.resolve()`した結果が`Path.home()`と一致または
   その配下であることを`handle_chat()`冒頭でチェックする。
9. **クライアント側のMarkdown描画は必ずsanitizeする**: LLM出力やWeb取得内容には
   HTML/`<script>`/イベントハンドラが混入しうるため、`marked.parse()`の結果を
   そのまま`innerHTML`に入れてはいけない。`DOMPurify.sanitize()`を必ず通す。
10. **依存JSはCDNから読み込まない**: `index.html`には`window.LC_TOKEN`（＝コマンド実行に
    到達できる権限）が埋め込まれるため、ページ内で動くJSは事実上その権限を持つ。CDN配信の
    JSはCDN側の改ざんや、バージョン無指定URLでの意図しない自動更新がそのままRCEに直結する。
    `marked`/`DOMPurify`は`vendor/`にバージョン固定でダウンロードして同梱し、`server.py`が
    `/vendor/*.js`として配信する（ファイル名は`[\w.-]+\.js`の完全一致のみ許可し
    パストラバーサルを防ぐ）。副作用として完全オフライン動作も実現する。
11. **履歴系GETにもトークンを要求する**: `/api/sessions`・`/api/session?sid=`は
    プロンプト・ツール結果・ファイル内容を含む機密情報を返す。127.0.0.1バインド＋
    通常のCORSだけでは、DNSリバインディング攻撃（攻撃者ドメインの名前解決を127.0.0.1に
    差し替える手法）でSOP判定そのものを回避されうるため、GETでもHostヘッダ検証を全体に
    かけ、履歴系だけは追加でトークンも要求する。`/`・`/api/health`・`/api/models`は
    トークン取得前に呼ぶ必要がありかつ機密を返さないため対象外でよい。

---

## 8. 実施例ログ: Windows 10 + GTX 1660 (6GB) への移植（2026-07-03）

AIエージェント（Claude Code）が本ドキュメントを読まずに、既存のLocalCoderセットが
置かれた別PCで「デスクトップから起動できるようにし、環境差分を吸収してほしい」と
依頼された際の実施記録。**`server.py` / `index.html` / README類は無改造**。追加した
ファイルは `LocalCoder.ps1`（リポジトリ内）と `LocalCoder.bat`（デスクトップ）の2つのみ。

### 環境

| 項目 | 元PC（README記載） | 移植先PC |
|---|---|---|
| OS | (未記載、Windows 11想定) | Windows 10 Pro build 19045 |
| GPU | RTX 3070 (8GB VRAM) | GTX 1660 (6GB VRAM) |
| WSLディストロ | `ubuntu-24.04` | `Ubuntu-20.04` |
| Ollamaモデル | `gpt-oss:20b` 推奨 | 未インストール（tool呼び出し非対応モデルのみ） |

### 詰まった点と解決

1. **mirroredネットワークが有効化できない** — `.wslconfig` に `networkingMode=mirrored`
   を書いて `wsl --shutdown` しても、WSL側は相変わらずNATのプライベートIP
   (`172.21.x.x/20`) のまま。原因はWindows 10であること（mirrored networkingは
   Windows 11専用機能）。`wsl --version` や `.wslconfig` の反映有無だけでは気づきにくいので、
   まず `[System.Environment]::OSVersion.Version` でWindows 11(Build 22000+)かを確認するのが早い。

2. **`OLLAMA_HOST=0.0.0.0` は自動化ポリシーで拒否された** — 「LAN全体にOllamaを公開する」
   変更として、明示的な許可なしにAIエージェントが実行することがブロックされた
   (Claude Codeの自動権限判定による)。ユーザーの追加承認を得た上で、代わりに
   「WSL専用の仮想アダプタIPだけにバインドする」方法（方法C）へ切り替えた:

   ```powershell
   # WSL側から見える"ゲートウェイIP" = Windows側 vEthernet (WSL) アダプタのIP
   $ip = (Get-NetIPAddress -InterfaceAlias "vEthernet (WSL)" -AddressFamily IPv4).IPAddress
   [Environment]::SetEnvironmentVariable("OLLAMA_HOST", "${ip}:11434", "User")
   # ollama.exe を再起動して反映
   ```

   これだけでは依然としてWindows Firewallに阻まれ、WSL側からの接続はタイムアウトした
   （`Get-NetFirewallRule` にOllama用の許可ルールが無かった）。**管理者権限**で
   インターフェース単位のスコープを持つ許可ルールを1本追加して解決:

   ```powershell
   New-NetFirewallRule -DisplayName "Ollama (WSL only)" -Direction Inbound -Protocol TCP `
     -LocalPort 11434 -Action Allow -RemoteAddress 172.16.0.0/12
   ```

   スコープは `-InterfaceAlias "vEthernet (WSL)"` ではなく **IP範囲で指定すること**。
   当初InterfaceAliasで作成したところ、Windows再起動でWSL仮想アダプタが再作成される
   とルールが古いアダプタ実体を指したままになり、無言で効かなくなった（ルールは
   Enabledのまま表示されるので気づきにくい）。`172.16.0.0/12` はWSL2 NATが使う
   プライベート帯で、OllamaはWSLゲートウェイIPにしかbindしていないため、これでも
   LANには公開されない。ファイアウォールルール追加はこの実施では管理者権限が無く
   自動実行できなかったため、コマンドをユーザーに提示して手動実行してもらった。

3. **推奨モデルが未インストール、既存モデルの大半はtool呼び出し非対応** —
   `ollama show <model>` の `Capabilities` に `tools` が出るかで判別できる
   （このPCでは `llama3` / `gemma3` / `qwen2.5vl` は非対応、`cogito` / 独自の
   `gemma4:e2b` は対応していた）。6GB VRAMを踏まえ `gpt-oss:20b`(約13GB)ではなく
   軽量な `qwen3:8b`(5.2GB, Q4)を新規pull。既存の `cogito:latest`(4.9GB)もtool対応の
   ため予備選択肢として使える。

4. **IPがPC再起動で変わりうる問題への対処** — ゲートウェイIPを起動のたびに動的検出し、
   Ollamaの実際のbind先とズレていれば自動で再bind＆再起動するロジックを
   `LocalCoder.ps1` に実装（詳細はスクリプト本体）。ファイアウォール側は
   `RemoteAddress 172.16.0.0/12` の範囲スコープなので、WSLサブネットが再起動で
   変わっても追従不要（前述の通りInterfaceAliasスコープは再起動で無効化するため不可）。

5. **セキュリティレビュー後の追加修正2巡** — 別レビュー（AIによる指摘）を受けて
   `server.py`/`index.html`を2段階で強化した。1巡目: POST全体へのCSRFトークン＋
   Origin/Host/Content-Type検証（`_post_ok`）、`run_command`のPopen+killpg化（停止
   ボタン実効化）、workspaceの`$HOME`配下制限、Markdown描画のDOMPurify sanitize。
   2巡目: 依存JS(`marked`/`DOMPurify`)をCDNからではなく`vendor/`に同梱配信、GET全体
   へのHostヘッダ検証、履歴系GET(`/api/sessions`・`/api/session?sid=`)へのトークン
   必須化。詳細は「7. 設計上の注意」6〜11番、コードは`server.py`/`SERVER.md`参照。

6. **バッチファイルの文字コード事故** — `LocalCoder.bat`に日本語コメント
   （`rem ローカルLLM(Ollama)コーディングエージェント起動`）をUTF-8で書いたところ、
   日本語版Windowsの`cmd.exe`（既定Shift-JIS解釈）がマルチバイト文字の後半バイトを
   コマンド区切りと誤認し、コメントの一部が「認識できないコマンド」として実行時
   エラーになった（ダブルクリックしても何も起きない、に見える）。UNC作業ディレクトリ
   （`\\wsl.localhost\...`）非対応の警告も別途出るため、原因切り分けには
   `cmd /c LocalCoder.bat` を対話的に実行してエラー出力を直接見るのが早い。
   対処: bat内は**ASCIIのみ**にし、`wsl -d <distro> -- true` を先頭に置いて
   WSLを明示的に起こしてからUNCパスを参照する（3節③参照）。

7. **ASCII化した後も直らなかった「黒い画面がすぐ消える」事故** — 6番を修正した後も、
   ダブルクリックすると黒いコンソールが一瞬出てすぐ消える症状が再発した。`@echo off`と
   `pause`無しのため原因が一切見えず、原因切り分け用に`pause`付き・`@echo on`のデバッグ版
   batを別途作って実行させたところ、実際のエラーが判明した:

   ```
   ファイル \\wsl.localhost\...\LocalCoder.ps1 を読み込めません。ファイル ...\LocalCoder.ps1
   はデジタル署名されていません。このスクリプトは現在のシステムでは実行できません。
   ```

   `Get-ExecutionPolicy -List`で`LocalMachine: RemoteSigned`を確認済みでも、**UNCパス
   （ネットワークパス）上の未署名スクリプトは、ローカルパスの未署名スクリプトとは
   異なるセキュリティゾーン扱いとなり、RemoteSignedポリシーでも実行がブロックされる**。
   `-ExecutionPolicy Bypass`で回避する手もあるが、ポリシーを緩めることになるため、
   代わりに**起動のたびに`LocalCoder.ps1`を`%TEMP%`（ローカルパス）へ`copy /Y`してから
   そちらを実行する**方式にした。ローカルパスなら未署名でもRemoteSignedで実行できる上、
   リポジトリ側の`LocalCoder.ps1`更新が次回起動時に自動反映されるため手動同期も不要。
   詳細は3節③のASCII安全版テンプレート参照。

   教訓として、`.bat`のトラブルシューティングは**最初から`pause`付き・`@echo on`の
   デバッグ版を作って実行させる**のが最短ルート。`@echo off`のまま原因を推測しても
   憶測が外れやすい（実際、最初は「他のウィンドウの裏に隠れているだけでは」という
   誤った仮説を挟んでしまった）。

### 最終構成（このPC固有、リポジトリ非同梱）

- `LocalCoder.ps1`（リポジトリ内に追加）: ゲートウェイIP自動検出 → Ollama再bind
  （必要な場合のみ）→ `wsl -d Ubuntu-20.04 -- bash -lc "... LOCALCODER_OLLAMA=http://<ip>:11434 python3 server.py"`
  → Edgeでアプリウィンドウを開く、を一括実行
- `LocalCoder.bat`（デスクトップ等）: `wsl -d <distro> -- true` でWSLを起こし、
  `LocalCoder.ps1`を`%TEMP%`へコピーしてから`powershell -NoProfile -File`で実行する
  薄いラッパー（`-ExecutionPolicy Bypass`は不要。**コメントはASCIIのみ**、UNCパス上の
  スクリプトを直接指定しない、の2点が必須。詳細は上記6・7番）

### 教訓

- 「移植先PCの前提を疑わずに手順書通りに進める」と、Windows 10かどうか・
  GPU VRAM量・WSLディストロ名の3点で必ずどこかに引っかかる。着手前に
  `[System.Environment]::OSVersion.Version` / `nvidia-smi` / `wsl -l -v` の3コマンドで
  前提差分を先に洗い出すと手戻りが減る。
- ネットワークを外部公開する変更（`0.0.0.0`バインド等）はAIエージェントが単独実行
  できない設計になっていることがある。行き詰まったら「スコープを狭めた代替案」
  （今回は特定インターフェースへのバインド＋ファイアウォールのインターフェーススコープ）
  を用意し、それでも管理者権限が必要な部分はユーザーに1行コマンドとして手渡すのが早い。

---

## 付録: 周辺環境（LocalCoderとは独立だが同時に構築したもの）

- **codex CLI 0.142.5**（WSL `~/.local/bin/codex`、`npm install -g --prefix ~/.local @openai/codex`）
  `~/.codex/config.toml` で組み込み `ollama` プロバイダ + `approval_policy="never"` +
  `sandbox_mode="workspace-write"`。注意: codex 0.142以降は `wire_api="chat"` 廃止、
  カスタム `[model_providers.ollama]` 定義も禁止（組み込みを使う）。
- **aider 0.86.2**（`~/.aider.conf.yml`: `model: ollama_chat/gpt-oss:20b`, `yes-always: true`,
  `set-env: [OLLAMA_API_BASE=http://localhost:11434]`）
