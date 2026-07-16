# server.py 動作解説

> 文書の役割: 現在の server.py の構造と処理順を説明する。利用開始は README.md、変更履歴は CHANGELOG.md、移植手順は REBUILD.md を参照する。

`server.py` は LocalCoder の中核。**標準ライブラリのみ**（外部依存ゼロ）で、
HTTPサーバー・Ollamaとの通信・エージェントのツール実行ループ・可逆操作レイヤーを
1ファイルに実装している。このドキュメントはソースを引用しながら、
起動時の処理順に沿って動作を説明する。

参照は、行番号ではなく関数名・クラス名・定数名を基準にする。

---

## 1. 設定値の読み込み（起動時に一度だけ）

```python
SERVER_VERSION = _detect_version()
MAX_ITER = 80          # 1リクエストあたりの最大ツールループ回数
TOOL_STUCK_LIMIT = 3   # 同じツール呼び出しが同じエラーで連続失敗した回数の上限
...
CMD_TIMEOUT = 180      # コマンド実行タイムアウト(秒)
NUM_CTX = 32768
```
（`server.py`）

- `OLLAMA` / `PORT`（`server.py`）は環境変数で上書き可能。デフォルトは
  どちらもlocalhost想定。WSLからWindows側Ollamaへ別経路で繋ぐ場合は
  `LOCALCODER_OLLAMA=http:///<IP>:11434` を指定（実例は `REBUILD.md` 「8. 実施例ログ」）。
- `SERVER_VERSION`（`server.py`）: `_detect_version()`が起動中のgitコミット
  ハッシュ（先頭7桁、未コミット変更があれば`+dirty`付記）を取得する。「直したはずが
  直っていない」の実際の原因は再起動を忘れて古いプロセスのままだったことだったため、
  画面右上に表示して`git log`の最新と見比べられるようにしている（3節）。
- `MAX_ITER=80` は「ユーザー1メッセージに対して、モデル発話→ツール実行を最大何往復
  許すか」の上限。
- `TOOL_STUCK_LIMIT=3`: 同じツール呼び出し（名前+引数）が同じエラーで3回連続失敗
  したら、`MAX_ITER`を待たずその場で打ち切る（`track_tool_repeat`, 6-3節）。

会話履歴はサーバー内メモリではなく **1会話=1 JSONファイル** で永続化する:

```python
CANCEL = {}            # sid -> threading.Event
SELF_CHECK_RESULTS = []  # 起動時セルフチェックの結果。main()で1回だけ設定される
HISTORY_DIR = ROOT / "history"   # チャット履歴の保存先 (1会話 = 1 JSONファイル)
SCHEMA_VERSION = 2      # 履歴JSONの形式バージョン。v2でturnごとの診断情報を追加。
HISTORY_DIR.mkdir(exist_ok=True)
```
（`server.py`）

### 1-1. 起動ごとのCSRFトークンとワークスペース境界

```python
TOKEN = secrets.token_hex(16)
HOME = Path.home().resolve()
# 作業フォルダ・ファイル操作を許可するルート。既定は WSL ホーム + Windows ドライブ(/mnt)。
ALLOWED_ROOTS = [Path(p).expanduser().resolve() for p in
                 os.environ.get("LOCALCODER_ALLOWED_ROOTS",
                                f"{HOME}:/mnt").split(":") if p]
DEFAULT_WORKSPACE = os.environ.get("LOCALCODER_DEFAULT_WORKSPACE", str(HOME))
```
（`server.py`）

`TOKEN` はプロセス起動のたびに毎回変わる32文字のランダム値。`do_GET`（3節）が
`index.html`配信時に埋め込み、`_post_ok()`/`_token_ok()`（2節）が検証する。
`ALLOWED_ROOTS`は`handle_chat()`（5節）でワークスペースの範囲チェックに使う——
既定は`$HOME`と`/mnt`（Windowsドライブ）で、`/mnt/c/...`のWindowsファイルも
編集できる（詳細は8-2節）。`LOCALCODER_ALLOWED_ROOTS`（コロン区切り）で上書きでき、
`$HOME`だけに戻すこともできる。

### 1-2. 可逆操作レイヤーと外部送信ポリシーの定数

```python
# --- 可逆操作レイヤー (REVERSIBLE_OPERATIONS.md 第1段階) ---
LEDGER_DIR_NAME = ".localcoder"
TXN_SUBDIR = Path(LEDGER_DIR_NAME) / "transactions"
TXN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")

# --- 外部送信ポリシー (REVERSIBLE_OPERATIONS.md 第3段階 §8) ---
EXTERNAL_SEND_POLICY = os.environ.get("LOCALCODER_EXTERNAL_SEND_POLICY", "allow_recorded")
```
（`server.py`）

`REVERSIBLE_OPERATIONS.md`の設計原則「危険なのは取り消せない状態変更だけ」を
コード全体の骨格にしている。詳細は7節。

---

## 2. HTTPリクエストの一次防御 — `_host_ok` / `_token_ok` / `_post_ok`

`Handler`には3つの検証ヘルパーがあり、GET/POSTの各ハンドラが用途に応じて組み合わせる
（実装は`server.py`付近、`_host_ok`/`_token_ok`/`_post_ok`の3メソッド）。

なぜこれが要るか: `ThreadingHTTPServer` は `127.0.0.1` にしかバインドしていない
（9節）が、それだけでは**同じPC上でブラウザが開いている悪意あるWebページ**からの
攻撃を防げない。`fetch(url, {mode:"no-cors", method:"POST", body:...})` のような
**送るだけ**のリクエストはクロスオリジンでも通ってしまう。もし何のチェックも
無ければ、悪意あるページが `http://localhost:8765/api/chat` に任意のプロンプトを
POSTし、`run_command` 経由でローカルマシン上のコマンドを実行させられる。

| 関数 | 使う場面 | 防ぐ攻撃 |
|---|---|---|
| `_host_ok()` | 全GET・全POST | DNSリバインディング（Hostヘッダが攻撃者ドメインのまま） |
| `_post_ok()`内のOrigin検証 | POST全般 | 他オリジンのページからのクロスオリジンPOST |
| `_post_ok()`内のContent-Type検証 | POST全般 | `no-cors`では`application/json`を送れない（単純リクエストの制約） |
| `_token_ok()` | POST全般＋履歴系GET | トークンを知らない限り最終的に拒否される本命の防御 |

`do_POST`（3節）は`_post_ok()`が `False` を返すと即座に `403 forbidden` を返す。

---

## 3. HTTPエンドポイント一覧

```python
elif self.path == "/api/selfcheck":
    self._json({"checks": SELF_CHECK_RESULTS})
elif self.path.startswith("/api/diagnostic_bundle"):
    if not self._token_ok():
        self._json({"error": "forbidden"}, 403)
        return
    ...
    self._json(build_diagnostic_bundle(sid=sid, error=error))
```
（`server.py`、`do_GET`全体は2184-2264）

```python
if self.path == "/api/transaction/rollback":
    self._handle_txn_action(rollback_transaction)
    return
if self.path == "/api/transaction/reapply":
    self._handle_txn_action(reapply_transaction)
    return
```
（`server.py`、`do_POST`全体は2267-2294）

| メソッド | パス | 役割 |
|---|---|---|
| GET | `/`, `/index.html` | GUI本体にトークン・バージョン・既定作業フォルダを埋め込んで返す |
| GET | `/vendor/*.js` | 同梱の`marked`/`DOMPurify`を静的配信（CDN不使用） |
| GET | `/api/models` | Ollamaのモデル一覧＋vision対応可否を返す |
| GET | `/api/browse` | 作業フォルダ選択ダイアログ用のディレクトリ一覧 **要トークン** |
| GET | `/api/sessions` / `/api/session?sid=` | 保存済み会話一覧・詳細 **要トークン** |
| GET | `/api/health` | 死活監視用 |
| GET | `/api/selfcheck` | 起動時セルフチェック結果（4節） |
| GET | `/api/diagnostic_bundle` | 問題報告用の診断パッケージ **要トークン**（4節） |
| POST | `/api/stop` | 実行中断（要トークン） |
| POST | `/api/session/delete` | セッション削除（要トークン） |
| POST | `/api/transaction/rollback` / `/reapply` | 可逆操作レイヤーのundo/redo（要トークン、7節） |
| POST | `/api/chat` | **本体**。エージェントループを開始しSSEで応答（要トークン） |

`/`配信時、`index.html`にトークン・既定作業フォルダ・実行中バージョンの3つを
文字列置換で埋め込む:

```python
inject = (f'<script>window.LC_TOKEN={json.dumps(TOKEN)};'
         f'window.LC_DEFAULT_WORKSPACE={json.dumps(DEFAULT_WORKSPACE)};'
         f'window.LC_VERSION={json.dumps(SERVER_VERSION)};'
         f'</script></head>').encode()
```
（`server.py`）

`/api/transaction/rollback`・`/reapply`は`_handle_txn_action`（`server.py`）
経由で呼ばれる。workspaceは`under_allowed()`で、トランザクションIDは`TXN_ID_RE`で
形式検証されるため、リクエストや台帳が改竄されてもワークスペース外のファイルには
触れない（7節）。

---

## 4. 起動時セルフチェックと診断バンドル

**セルフチェック**（`run_self_check`, `server.py`）は起動時に1回だけ実行され、
Ollama接続・推奨モデル(`gpt-oss`/`ornith`系統)の有無・`pdftotext`の有無・
`ALLOWED_ROOTS`の実在・**外部送信ポリシーの値が既知か**・履歴ディレクトリへの
書き込み可否・（設定時のみ）MCP設定の妥当性、を確認する。全項目は診断用の警告に
留まり、失敗してもサーバー起動は止めない（「依存機能の段階的劣化」方針）。結果は
`SELF_CHECK_RESULTS`にキャッシュされ、`/api/selfcheck`で返す。GUI側は失敗項目が
あればヘッダ下に警告バナーを表示する。

**診断バンドル**（`build_diagnostic_bundle`, `server.py`）はバージョン・
Python/OS情報・セルフチェック結果・主要な設定値（`num_ctx`等）・接続中MCPサーバーの
名前と生存とツール数のみ・Ollamaバージョン・モデル一覧・（`sid`指定時）そのセッションの
`turns`診断情報、をJSONでまとめる。**個人パス（`ALLOWED_ROOTS`/`DEFAULT_WORKSPACE`の
実パス）・CSRFトークン・会話本文は含めない**（`allowed_roots_count`のように件数だけ
入れる、`server.py`）。ユーザーが「🩺 診断」ボタンでダウンロードし、バグ報告に
添付する用途。

---

## 5. エージェントループの本体 — `handle_chat`

`/api/chat` にPOSTされると、レスポンスは即座に **SSE** に切り替わる:

```python
def handle_chat(self):
    turn_started_at = time.time()
    turn_status = "completed"
    ...
    wsr = ws.resolve()
    if not under_allowed(wsr):
        ...
    messages = [{"role": "system", "content": sys_prompt}]
    messages += body.get("messages", [])
    # 可逆操作レイヤー(REVERSIBLE_OPERATIONS.md): 1リクエスト=1トランザクション。
    txn = Transaction(wsr)
```
（`server.py`付近。`sys_prompt`はMCP接続時に追加ツールの説明を付記する
——`server.py`付近）

ポイントは3つ:

1. **会話履歴をサーバーが保持しない**ステートレス設計。クライアントが毎回全履歴を送る。
2. **ワークスペースは `ALLOWED_ROOTS` 配下のみ許可**（既定=`$HOME` + `/mnt`）。
3. **1リクエスト=1トランザクション**。`Transaction(wsr)`を生成し、後述のツール実行
   （`exec_tool`経由）へ`txn`として渡す。ファイル操作が無いターンではディスクに
   何も作られない（7節）。

### 5-1. Ollamaへのストリーミング問い合わせとツール実行

反復の先頭で`compact_history()`（8節）と`build_work_state()`（9節）を通したあと、
Ollamaへ問い合わせる。ツール呼び出しの実行はこう配線されている:

```python
result = exec_tool(name, args, ws, ev, model=model,
                   pending_images=pending_images,
                   sid=sid, call_id=tc.get("id"),
                   messages=messages, txn=txn)
```
（`server.py`付近）

`run_command`の結果には構造化データ（6-4節）も付記する:

```python
if name == "run_command":
    tool_msg["meta"] = parse_command_result(result)
```

ツール呼び出し1回ごとに`tool_start`/`tool_end`のSSEが流れ、`view_image`が画像を
読み込んだ場合は全tool_calls処理後に合成の`user`メッセージとして追加される
（既存の挙動、変更なし）。

**空応答からの自動回復・Ollama呼び出し失敗時の自動再試行**（`EMPTY_RETRY_LIMIT`/
`HTTP_RETRY_LIMIT`）は従来通り。空応答のリトライ前には`compact_history(...,
force=True)`で強制圧縮してから再試行するようになった（8節）。

### 5-2. 終了処理とターン診断情報

```python
self._sse({"type": "summary", "data": {
    "status": turn_status,
    "duration_seconds": round(time.time() - turn_started_at, 1),
    "changed_files": extract_changed_files(messages[1:]),
    "unverified_changes": find_unverified_changes(messages[1:]),
    "tool_call_count": diag_tool_call_count,
    "compact_count": diag_compact_count,
    "txn_id": txn.id if txn.has_ops else None,
    "txn_ops": len(txn.operations),
    "external_sends": len(txn.external_sends),
    "workspace": str(wsr),
}})
```
（`server.py`付近）

**作業サマリーカード**（IMPROVEMENTS.md §7.1）はターン終了ごとにGUIへ表示される
「終了理由・所要時間・ツール呼び出し回数・変更ファイル数・未検証変更数・外部送信件数・
圧縮回数」。`txn_id`が付けば「⎌ このターンの変更を元に戻す」ボタンが出る（7節）。

**未検証の変更**（`find_unverified_changes`, 10-4節）: `write_file`/`edit_file`で
変更したファイルのうち、その後1度も`run_command`が実行されていないものを機械的に
検出する。モデルが「完了した」と申告しただけで実際にはビルド/テストで検証していない
状態を検知する（IMPROVEMENTS.md §3.3）。

`finally`節でトランザクションを確定し（`txn.finalize(turn_status)`）、会話を
自動保存する。あわせて`turn`辞書にターン診断情報一式
（`est_tokens_start`/`end`・`compact_count`・`http_retries`・`empty_retries`・
`tool_call_count`・`tool_exec_seconds`・`iterations_used`・
`changed_files_count`・`unverified_changes_count`・`txn_id`・`txn_ops`・
`external_sends`）を`history/<sid>.json`の`turns`配列へ記録する
（IMPROVEMENTS.md §2.3、障害調査のたびに履歴JSONから手計算していた値を機械的に残す）。
`save_session`（`server.py`）はタイトルを初回保存時に一度だけ確定し
（圧縮で先頭メッセージが要約マーカーに置き換わってもタイトルが変わらないように、
`server.py`）、`schema_version`（現在2）を付与する。

停止・エラー時も**トランザクションの自動ロールバックはしない**
（REVERSIBLE_OPERATIONS.md §10、途中までの変更が有益な場合があるためユーザーが選ぶ）。

---

## 6. ツール実行 — `ToolProvider` アーキテクチャ

`exec_tool`は薄いエントリポイントで、実体は`ToolProvider`インターフェースへの
ディスパッチに分離されている（IMPROVEMENTS.md §13.2、将来のMCP対応を見越した設計）:

```python
class ToolProvider(Protocol):
    def list_tools(self) -> list[dict]: ...
    def call_tool(self, name: str, args: dict, ctx: ToolContext) -> str: ...


TOOL_PROVIDERS: list[ToolProvider] = [BuiltinToolProvider()]


def exec_tool(name, args, ws, cancel=None, model=None, pending_images=None,
              sid=None, call_id=None, messages=None, txn=None) -> str:
    ctx = ToolContext(ws=ws, cancel=cancel, model=model, pending_images=pending_images,
                      sid=sid, call_id=call_id, messages=messages, txn=txn)
    provider = _provider_for_tool(name)
    if provider is None:
        return f"ERROR: unknown tool {name}"
    return provider.call_tool(name, args, ctx)
```
（`ToolContext`: `server.py`、`ToolProvider`: 1137-1146、
`exec_tool`: 1539-1554、`all_tools`/`_provider_for_tool`: 1512-1537）

`ToolContext`は各ツールに必要な状態（`ws`/`cancel`/`model`/`pending_images`/
`sid`/`call_id`/`messages`/`txn`）を1つにまとめたコンテナ。`_provider_for_tool`は
`list_tools()`の定義済みツール名と突き合わせるデータ駆動方式（if/elifの連鎖ではない）。
`all_tools()`は全プロバイダのツール定義を集め、名前が重複したら**先に登録された
プロバイダを優先**する（組み込みが先頭固定）。

`BuiltinToolProvider`（`server.py`）が組み込み12ツールを振り分ける。
`read_file`/`write_file`/`edit_file`/`list_dir`/`delete_file`/`delete_directory`/
`move_file`/`copy_file`は既に3-4節・7節で触れた通り。例外処理も強化されている:

```python
except KeyError as e:
    # 弱いローカルモデルほど引数を一部欠落させたツール呼び出しを返しがちなので、
    # 生のKeyErrorよりモデルが自己修正しやすい具体的な指示にする。
    return (f"ERROR: missing required argument {e} for tool '{name}'. "
            f"Call {name} again with all required arguments included.")
```
（`server.py`）

### 6-1. 差分中心の再読 — `read_file`のキャッシュヒット判定

```python
if ctx.messages is not None:
    prev = find_previous_read(ctx.messages, args["path"])
    if prev is not None and prev == t:
        digest = hashlib.sha256(t.encode()).hexdigest()[:16]
        return (f"(内容は前回read_fileした時から変わっていません。"
                f"SHA256={digest}、{len(t)}文字。前回の内容をそのまま参照してください)")
```
（`server.py`。`find_previous_read`: 10-3節）

同じパスを前回読んだ時と一字一句同じなら全文の再送を省略する（IMPROVEMENTS.md §6.3の
第一歩。完全なdiff計算はスコープ外）。

### 6-2. PDF・画像対応

`read_file`は`.pdf`拡張子を自動判別し`pdf_to_text()`（`server.py`）が
`pdftotext -layout`でテキスト抽出する。スキャン画像PDF等でテキストが取れない場合は
その旨を返す。`view_image`（`server.py`）は`model_capabilities()`
（`server.py`、`/api/show`の`capabilities`をモデル名でキャッシュ）で
現在のモデルがvision対応か判定し、対応していれば`pending_images`に積んで次の
Ollama呼び出しで実際に見せる。

### 6-3. ツール呼び出しの頑健性 — 特殊トークン除去・連続失敗検知

```python
def sanitize_tool_name(raw_name: str) -> str:
    """一部のモデルは<|tool_call_argument_begin|>のような内部特殊トークンを
    name欄に混入させて返すことがあり、完全一致ディスパッチが永遠に失敗し
    続ける原因になっていた。"""
    return TOOL_NAME_TOKEN_RE.sub("", raw_name).strip()
```
（`server.py`）

```python
def track_tool_repeat(name, args, result, last_failed_sig, repeat_count):
    """同じツール呼び出し(名前+引数)が同じ結果で連続失敗した回数を追跡する。
    TOOL_STUCK_LIMIT(3)に達したら進展が見込めないと判断しMAX_ITERを待たず
    打ち切る。"""
```
（`server.py`。`handle_chat`側の使用箇所でループを`break`する）

### 6-4. 構造化ツール結果

```python
def parse_command_result(result: str) -> dict:
    """run_commandの文字列結果を構造化データに変換する(IMPROVEMENTS.md §4.1)。
    「モデルには読みやすい文字列を渡し、サーバー側では機械判定に構造化データを
    使う」という方針を、既存の文字列契約を一切変えずに実現するアダプタ。"""
```
（`server.py`）

`{"ok": bool|None, "exit_code": int|None, "timed_out": bool, "cancelled": bool}`を
返す。`content`（モデル向け文字列）はそのままに、`tool`メッセージへ`meta`フィールド
として追記するだけ（5-1節）。`build_work_state`（9節）の`exit_code=0`判定を
この関数に置き換えている。

---

## 7. 可逆操作レイヤー（REVERSIBLE_OPERATIONS.md）

設計原則（`REVERSIBLE_OPERATIONS.md` §1）:

> 危険なのは**不可逆な状態変更**である。読み取りは対象の状態を変更しないため、
> 読み取りだけでは原則として危険を生まない。

この原則を承認ダイアログではなく「変更前状態を必ず記録し、ターン単位で戻せる
ようにする」という形でコード化したのがこのレイヤー。3段階で実装されている。

### 7-1. 第1段階: ファイル編集の可逆化

`Transaction`クラス（`server.py`）が1回の`/api/chat`リクエストを1つの
トランザクションとして扱う台帳。置き場所はワークスペース配下
`.localcoder/transactions/<id>/`（`id`は`20260715-153012-a83f`形式）。**最初の
書き込みが起きるまで何も作らない**（読み取りだけのターンでは痕跡ゼロ）。

```python
def record_before_write(self, f: Path) -> None:
    """write_file/edit_fileの書き込み直前(親ディレクトリ作成よりも前)に呼ぶ。"""
    rel = self._rel(f)
    if rel in self._seen:
        return
    self._seen.add(rel)
    self._ensure_dir()
    if f.is_file():
        data = f.read_bytes()
        st = f.stat()
        op = {"type": "write", "path": rel, "existed_before": True,
              "before_sha256": hashlib.sha256(data).hexdigest(),
              "before_mode": st.st_mode, "before_mtime": st.st_mtime,
              "backup_path": self._backup_bytes("before", rel, data)}
    else:
        op = {"type": "create", "path": rel, "existed_before": False,
              "created_dirs": self._created_dirs_for(self.ws, f)}
    self.operations.append(op)
    self._write_manifest()
```
（`server.py`）

同じファイルを1ターン中に何度変更しても、変更前状態の保存は**最初の1回だけ**
（`_seen`セット）。新規ファイルの場合、ロールバックで空になったら取り除くべき
新規親ディレクトリも深い順に記録する。`manifest.json`は操作のたびに書き直すため、
ターン途中でプロセスが落ちても、そこまでの操作は台帳に残る。

**原子的書き込み**（`atomic_write`/`atomic_write_bytes`, `server.py`）:
一時ファイル（`.{name}.localcoder-tmp`）へ書いてから`os.replace`で置き換える。
プロセス停止や書き込みエラーで対象ファイルが中途半端な内容になるのを防ぐ。

**台帳の保護**（`in_ledger_area`, `server.py`）: `.localcoder/`自体への
`write_file`/`edit_file`は拒否する（モデルが台帳を書き換えると可逆性の保証が壊れる）。
台帳ディレクトリ作成時に自己無視の`.gitignore`（内容は`*`）を1回だけ書き、ユーザーの
gitリポジトリを汚さない。

### 7-2. 第2段階: 削除・移動・コピーの可逆化

専用ツール4種が生の`rm`/`mv`より優先される（システムプロンプト`server.py`）。
削除は即時消去せず`trash/`へ退避し、`delete_directory`は配下の全ファイルを
相対パスごと保存してサブツリーを丸ごと復元可能にする:

```python
def record_delete(self, f: Path) -> None:
    if f.is_dir():
        entries = []
        for child in sorted(f.rglob("*")):
            if child.is_file() or child.is_symlink():
                crel = self._rel(child)
                self._backup_bytes("trash", crel, child.read_bytes())
                entries.append({"path": crel, "mode": child.stat().st_mode})
            elif child.is_dir():
                entries.append({"path": self._rel(child), "dir": True})
        op = {"type": "delete_dir", "path": rel, ...}
```
（`server.py`、抜粋）

`move_file`/`copy_file`は移動元・移動先・上書きされた既存内容を記録する
（`record_move`/`record_copy`, `server.py`）。新規親ディレクトリの検出は
**mkdir前に**行う点が重要（mkdir後だと既存扱いになり検出漏れする——実装中に発見・
修正したバグ）:

```python
created_dirs = (ctx.txn.created_dirs_for(dst) if ctx.txn is not None else [])
dst.parent.mkdir(parents=True, exist_ok=True)
if name == "move_file":
    os.replace(src, dst)
    ctx.txn.record_move(src, dst, dst_existed, created_dirs)
```
（`server.py`、抜粋）

台帳領域への削除・移動先指定・ワークスペースルート自体の削除は拒否する
（`server.py`）。

### 7-3. 第3段階: 外部送信の分類とポリシー

「危険なのは取り消せないネットへの書き込み」という原則の中核。`run_command`の
コマンド文字列から外部送信を検出する:

```python
_EXTERNAL_SEND_PATTERNS = [
    (re.compile(r"\bgit\s+push\b"), "git push (リモートへのコミット反映)"),
    (re.compile(r"\bcurl\b(?=.*(?:-X\s*(?:POST|PUT|PATCH|DELETE)\b|...))"),
     "curl による送信/アップロード ..."),
    (re.compile(r"\b(?:scp|sftp)\b"), "scp/sftp (リモートへのファイル転送)"),
    (re.compile(r"\b(?:npm|yarn|pnpm)\s+publish\b"), "npm/yarn/pnpm publish ..."),
    ...
]
```
（`server.py`。git push / curl・wgetの送信系 / scp・sftp / rsync・sshの
リモート / npm・twine・gh release・docker push等の公開 / aws s3・gsutilアップロード /
メール送信、を検出。**GET(取得)は安全側として対象外**）

```python
if name == "run_command":
    cmd = args["command"]
    reasons = classify_external_send(cmd)
    if reasons:
        if EXTERNAL_SEND_POLICY == "deny":
            if ctx.txn is not None:
                ctx.txn.record_external_send(cmd, reasons, executed=False)
            return ("ERROR: この操作は外部への送信(取り消せないネットワーク"
                    "書き込み)を含むため、現在のポリシー(deny)では実行"
                    f"できません。検出理由: {'; '.join(reasons)}。...")
        if ctx.txn is not None:
            ctx.txn.record_external_send(cmd, reasons, executed=True)
    return run_command(cmd, ws, cancel, sid=ctx.sid, call_id=ctx.call_id)
```
（`server.py`）

`LOCALCODER_EXTERNAL_SEND_POLICY`環境変数で挙動を選ぶ：既定`allow_recorded`
（従来通り実行するが送信内容を必ず`manifest.json`の`external_sends`へ記録）、または
`deny`（実行前に拒否しモデルへユーザー依頼を促す）。`ask`（実行前のUI同期確認）は
SSE往復承認が必要なため未実装。`run_command`前後のファイル差分検出・Git差分保存
（§7-B）は観測用で可逆化に直結しないため見送っている。

### 7-4. ロールバックと再適用（undo/redo）

```python
def rollback_transaction(ws: Path, txn_id: str) -> dict:
    """write/create/delete/delete_dir/move の各操作型を逆順に取り消す。
    ロールバック自体も可逆にするため、戻す直前の各ファイルの内容を after/ へ
    退避してから復元する。"""
    ...
    for op in reversed(manifest.get("operations", [])):
        typ = op.get("type", "write")
        if typ in ("write", "create"):
            ...  # 変更前状態を復元 or 新規作成分を削除
        elif typ == "delete":
            ...  # trash/から復元
        elif typ == "delete_dir":
            ...  # サブツリーを丸ごと復元
        elif typ == "move":
            ...  # dstを消してsrcへ戻す。上書きされた既存があれば復元
```
（`server.py`、要旨）

`reapply_transaction`（`server.py`）はロールバック済みトランザクションの
変更を再適用する（`after/`退避分を書き戻す、再度削除する、再度移動する）。何度でも
undo/redoを往復できる。`manifest`内のパスは常に`resolve_path()`で検証するため、
台帳が改竄されていてもワークスペース外のファイルには触れない（`_txn_manifest_path`
が`TXN_ID_RE`でIDの形式も検証、`server.py`）。

実際の防御線は、この可逆操作レイヤーを含めると**5層**になる:

1. `ThreadingHTTPServer` が `127.0.0.1` のみにバインド（11節）
2. `_host_ok()` による全GET/POST共通のHost検証（DNSリバインディング対策、2節）
3. `_post_ok()` によるCSRFトークン検証（2節）
4. ワークスペースを `ALLOWED_ROOTS` 配下に制限（5節）
5. **可逆操作レイヤー**（本節）— 万一①〜④を越えられても、ローカルの変更は
   ターン単位で確実に戻せる。取り消せないネットへの書き込みだけは別枠で
   記録・制御する

---

## 8. 履歴の自動圧縮 — `compact_history`

長い会話やツール結果の蓄積で履歴が`NUM_CTX`(32768)に近づくと、ollamaは黙って
プロンプト前方を切り捨てる。各反復の先頭で`compact_history()`を通し、予算超過時に
サーバー側で自動圧縮する。

```python
def compact_history(messages: list, model: str, sse, force: bool = False) -> list:
```
（`server.py`付近）

処理は多段階:

1. **重複除去**（`dedupe_tool_results`, `server.py`）— 同一内容のツール
   結果は最新の1件だけ残し、古い方は短い参照文に置換する（LLM不使用）。弱いモデルが
   同じファイルを何度も読み直すと、履歴の半分が同一内容の重複だったセッションが
   実際にあった。
2. **古いツール結果の切り詰め**（`trim_old_tool_results`, `server.py`）—
   直近`KEEP_RECENT_TOOLS`(4)件を除くツール結果を500文字に切り詰める。
3. **要約**（`summarize_old`, `server.py`）— 直近`KEEP_RECENT_MSGS`(6)件を
   原文のまま残し、それ以前を要約して1メッセージに置換する。
4. **フォールバック** — 要約失敗時は「【自動省略】」マーカーで単純省略する。

**先回り圧縮**（`PROACTIVE_COMPACT_RATIO=0.9`）: 予算を正式に超過する前、90%を
超えた時点で先回りして縮める。天井ギリギリ（実測で予算の99%）まで会話を伸ばした
状態でモデルに応答させると、生成用の余白がほぼ無くなり空応答を返して進まなくなる
実例があったための対策。空応答リトライの直前にも`compact_history(...,
force=True)`を呼び、しきい値未満でも強制圧縮してから再試行する（5-1節）。

**圧縮のヒステリシス**（`COMPACT_TARGET_RATIO=0.6`）: 発動したら予算の60%まで
一気に下げる。天井ギリギリまでしか下げないと、数イテレーションごとに圧縮が
再発し、履歴前方の書き換えでollamaのプロンプトキャッシュが毎回無効化されて
激遅になる問題があった。

### 8-1. 世代劣化を防ぐインクリメンタル要約

```python
def update_summary(prev: str, new_raw: list, model: str) -> tuple[str, str | None]:
```
（`server.py`）

圧縮が複数回起きると、前回の要約自体を毎回生ログとして丸ごと再要約してしまい、
繰り返すほど内容（特にファイルパスや作業の方向性）が薄まる問題があった。既存の
圧縮マーカーを検出した場合は再要約せず「これまでの要約」として引き継ぎ、新規分の
会話だけを要約してマージする1回のLLM呼び出しに統合した（以前は「新規分の要約→
既存要約とマージ」の2回呼び出しで、圧縮1回あたりの停止時間が長かった）。

### 8-2. 機械抽出される3種の付帯情報

要約本文とは別ブロックとして、圧縮マーカーに常に正確な値のまま引き継がれる:

- **変更ファイル一覧**（`extract_changed_files`, `server.py`）—
  `write_file`/`edit_file`/`delete_file`/`delete_directory`/`move_file`/
  `copy_file`の実行結果から機械抽出（LLM不使用）。7節のツール追加に合わせて
  delete/moveのdstも数えるよう拡張済み。
- **固定指示**（`extract_pinned_instructions`, `server.py`）— ユーザー
  発言に「覚えて」「忘れないで」が含まれれば継続指示とみなし、一字一句そのまま
  引き継ぐ（要約LLMの「そのまま残せ」という指示への遵守頼みをやめた、
  IMPROVEMENTS.md §3.2）。
- **現在のゴール**（`extract_current_goal`/`_extract_goal_line`,
  `server.py`, 1956-1970）— 要約プロンプトの出力1行目に
  `GOAL: <現在の最終目標を1文で>`を必ず出力させ、抽出して専用ブロックに保存する。
  新しい判定が得られなければ前回のゴールを引き継ぐ。`GOAL_LINE_RE`は`\s*`ではなく
  `[ \t]*`を使う点に注意（`\s`は改行にもマッチするため、GOAL値が空の場合に
  次行の内容まで巻き込んで抽出してしまうバグが実際にあった、`server.py`）。

```python
def _parse_marker(content: str) -> tuple[str | None, list[str], list[str], str | None]:
    """マーカーcontentから (要約本文, 変更ファイル一覧, 固定指示一覧, 現在のゴール) を取り出す。"""
```
（`server.py`。`build_marker`が逆方向の組み立て、1931-1955）

---

## 9. 作業状態ダッシュボード — `build_work_state`

会話ログの要約（8節）とは別に、「変更したファイル」「直近の実行コマンドと結果」
「同じコマンドの繰り返し失敗」「現在のゴール」「固定指示」を**LLMを使わず機械的に**
履歴から抽出し、その回のOllama呼び出しにのみ一時的に差し込む（保存される会話履歴
自体には加えない）:

```python
work_state = build_work_state(messages[1:])
call_messages = messages
if work_state:
    call_messages = messages + [
        {"role": "user", "content": WORK_STATE_PREFIX + work_state}]
```
（`server.py`が`build_work_state`本体、呼び出し箇所は5-1節）

`_iter_tool_calls_with_results`（`server.py`）が`assistant`の
`tool_calls`と直後の`tool`メッセージ列を突き合わせて発生順にyieldする土台関数。
`build_work_state`はこれを使い、同一コマンドが`FAIL_REPEAT_THRESHOLD`(3)回連続
失敗していれば「同じアプローチを繰り返さず根本原因を洗い出せ」という警告文を
追加する。圧縮済みの古い部分は`tool_calls`構造が失われているため、この関数は
**直近の非圧縮ウィンドウのみ**を反映する（古い変更点は8節の要約側に残る）。

---

## 10. MCPクライアント（IMPROVEMENTS.md §13 / 第6段階）

外部MCPサーバーを組み込みツールと同列に扱うための`ToolProvider`実装:

```python
class McpToolProvider:
    """対応トランスポートはstdio(子プロセス+改行区切りJSON-RPC 2.0)のみ。
    起動は遅延(初回のlist_tools/call_toolまでプロセスを作らない)。起動や通信に
    失敗しても例外は外へ漏らさず、list_tools()は空リスト・call_tool()は
    "ERROR: ..."文字列を返す(依存機能の段階的劣化)。"""
```
（`server.py`）

設定は`mcp_servers.json`（個人パスを含むため`.gitignore`対象。
`mcp_servers.json.example`をリポジトリにコミット）:

```json
{"mcpServers": {"名前": {"command": "python3", "args": ["..."], "env": {"KEY": "VALUE"}}}}
```

ファイルが無ければMCP機能は完全に無効で従来通り動く（`load_mcp_providers`,
`server.py`）。`main()`（12節）が起動時に`TOOL_PROVIDERS`へ追加し、
一度`list_tools()`を呼んで先に起動しておく（初回チャットの待ち時間短縮と、
起動失敗の早期発見のため）。JSON-RPC通信は`threading.Lock`で直列化し
（`ThreadingHTTPServer`の複数スレッドから安全に呼べるようにする）、ハング・
切断したサーバーは呼び出し失敗時に殺して次回再起動を試みる。stdioの子プロセスは
このマシン内で完結するため、7節の外部送信ポリシーの対象外として扱える
（HTTP/SSEトランスポートは実装しない）。

`Handler.handle_chat`はMCP接続時、システムプロンプトへ追加ツールの一覧を明示する
（`server.py`付近。ローカルの弱いモデルは`tools`スキーマだけだと
追加ツールを使い忘れることがあるため）。

将来の外部MCP（認証・ポリシー・承認・監査が必要な非stdioサーバー）への拡張計画は
`EXTERNAL_MCP_SECURITY.md`にまとめてある。

---

## 11. `run_command` とパスサンドボックス

`run_command`（`server.py`）は`Popen(start_new_session=True)`で
プロセスグループを分離し、0.5秒ごとの`communicate(timeout=0.5)`ポーリングで
`cancel.is_set()`（停止ボタン）と`CMD_TIMEOUT`(180秒)の両方をチェック、
該当したら`os.killpg()`でプロセスグループごとSIGKILLする（単に`kill()`する
だけでは`bash -lc`の子孫プロセスが残ってしまう）。出力が12KBを超えると
`save_full_tool_output()`（`server.py`）で完全な内容を
`history/tool_output/<sid>/<call_id>.txt`へ診断用に保存してから、モデルへは
先頭6KB＋末尾6KBに切り詰める。

`resolve_path`（`server.py`）はファイル操作ツール共通のサンドボックス。
`under_allowed`/`list_subdirs`（`server.py`）はワークスペース選択・
`/api/browse`用に`ALLOWED_ROOTS`配下かを判定する（3節）。並び順は`key=str.lower`
で大文字小文字を無視する。

`run_command`自体はこのサンドボックス対象外（`cwd=ws`のみ）で、README/REBUILD.mdに
「サンドボックスなし・ユーザー権限フル実行」と明記の通り制限を設けていない。実際の
防御は7節末尾に整理した5層構造。

---

## 12. 起動処理

```python
def main():
    global SELF_CHECK_RESULTS
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"port {PORT} already in use — LocalCoder is probably already running")
        return
    SELF_CHECK_RESULTS = run_self_check()
    for c in SELF_CHECK_RESULTS:
        print(f"  [{'OK' if c['ok'] else 'NG'}] {c['name']}: {c['detail']}")
    for p in load_mcp_providers():
        TOOL_PROVIDERS.append(p)
        n = len(p.list_tools())
        print(f"  [{'OK' if n else 'NG'}] MCP {p.name}: ...")
    print(f"LocalCoder running: http://localhost:{PORT}  (ollama: {OLLAMA})")
    srv.serve_forever()
```
（`server.py`、抜粋）

ポート二重使用時は既存プロセスを奪わずエラーで即終了。セルフチェック（4節）と
MCPサーバーの先行起動（10節）は`ThreadingHTTPServer`のbind成功後・
`serve_forever()`前に行う。

---

## 13. Web検索・ページ取得ツールの実装

`web_search`（`server.py`）はDuckDuckGoのHTML版をUser-Agent偽装付きで
スクレイピングする軽量実装。`fetch_url_text`（`server.py`）は自前の
`HTMLParser`サブクラス`_TextExtract`（320-338）で`script`/`style`/`svg`/`head`を
除いたテキストノードだけを収集し、1万文字で切り詰める。取得したテキストは
`tool`メッセージ経由でモデルの応答に混入しうるため、クライアント側の
`DOMPurify.sanitize()`（`index.html`）がHTML注入の最後の防波堤になる。

---

## 14. 処理フロー図（概略）

```
ブラウザ(index.html)
   │  GET /  → _host_ok()検証 → トークン/バージョン/既定作業フォルダを埋め込んで返す
   ▼
   │  POST /api/chat  { model, workspace, messages }  + X-LocalCoder-Token
   ▼
_post_ok()  ── NG → 403 forbidden
   │ OK
   ▼
handle_chat()
   │  workspace が ALLOWED_ROOTS 配下か検証
   │  txn = Transaction(wsr)  ← 可逆操作レイヤー、1リクエスト=1トランザクション
   ▼
┌─ for it in range(MAX_ITER) ──────────────────────────────────┐
│  compact_history() 予算超過/先回りで自動圧縮 → SSE: compact    │
│  build_work_state() をその回だけ差し込む                       │
│  ollama_stream(payload) → SSE: think/token を逐次転送          │
│  tool_calls が無ければ break (空応答なら自動リトライ)           │
│  あれば: exec_tool(...,txn=txn) を各tool_callで実行             │
│    write/edit/delete/move/copy は txn に変更前状態を記録        │
│    run_commandの外部送信は classify_external_send で分類・記録  │
│    停止ボタン/タイムアウトで確実にkillpg                       │
│         → SSE: tool_start / tool_end                           │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
SSE: summary(終了理由・txn_id等) → history(全履歴) → all_done
   │
   ▼
txn.finalize() / save_session()  (.localcoder/transactions/ と history/<sid>.json)
```

---

## 関連ドキュメント

- [README.md](README.md) — 起動方法・使い方の概要
- [REBUILD.md](REBUILD.md) — 別PCへの移植手順、設計上の注意点、実施例ログ
- [REVERSIBLE_OPERATIONS.md](REVERSIBLE_OPERATIONS.md) — 可逆操作レイヤーの設計原則
  （本ドキュメント7節の元ネタ）
- [IMPROVEMENTS.md](IMPROVEMENTS.md) — 信頼性・観測性・テスト・性能・保守・配布を
  含む改善ロードマップ（ToolProvider分離・構造化ツール結果・MCP対応前提の設計判断等）
- [METACOGNITIVE_REPLANNING.md](METACOGNITIVE_REPLANNING.md) — 停滞・目的逸脱を
  検知して作業方針を再評価する機能の設計・実装記録（第1〜3段階は実装済み）
- [EXTERNAL_MCP_SECURITY.md](EXTERNAL_MCP_SECURITY.md) — 非stdioの外部MCPサーバーに
  必要な認証・ポリシー・承認・監査の計画書（未実装、計画のみ）
- [CHANGELOG.md](CHANGELOG.md) — 変更履歴。各エントリがコミットハッシュ付きで
  検証結果まで記録されている
- [tests/](tests/) — stdlib `unittest`のみの回帰テスト一式。
  実行方法は`REBUILD.md`「4-1」参照
- [index.html](index.html) — 本ドキュメントで扱ったSSEイベント（`think`/`token`/
  `tool_start`/`tool_end`/`turn_done`/`compact`/`summary`/`notice`/`image`/
  history / strategy_review / all_done / error を受け取り、window.LC_TOKENを全POSTと履歴系GETに
  付与し、Markdown描画時に`DOMPurify.sanitize()`を通し、同梱の
  `/vendor/marked.min.js`・`/vendor/purify.min.js`を読み込み、可逆操作レイヤーの
  ロールバック/再適用ボタンを提供するGUI側の実装
- [vendor/](vendor/) — CDNから読み込まず同梱している依存JS（`marked`・`DOMPurify`）
