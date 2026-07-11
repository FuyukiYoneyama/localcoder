# server.py 動作解説

`server.py` は LocalCoder の中核。**標準ライブラリのみ**（外部依存ゼロ）で、
HTTPサーバー・Ollamaとの通信・エージェントのツール実行ループを1ファイルに実装している。
このドキュメントはソースを引用しながら、起動時の処理順に沿って動作を説明する。

参照は `server.py:<行番号>` の形式。

---

## 1. 設定値の読み込み（起動時に一度だけ）

```python
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
```
（`server.py:27-40`）

- `OLLAMA` / `PORT` は環境変数で上書き可能。デフォルトはどちらもlocalhost想定。
  WSLからWindows側Ollamaへ別経路で繋ぐ場合は `LOCALCODER_OLLAMA=http://<IP>:11434` を
  指定して起動する（実例は `REBUILD.md` の「8. 実施例ログ」参照）。
- `MAX_ITER=80` は「ユーザー1メッセージに対して、モデル発話→ツール実行を最大何往復
  許すか」の上限。無限ループでサーバーが固まるのを防ぐ安全弁。
- `EMPTY_RETRY_LIMIT` / `EMPTY_RESPONSE_NUDGE` はモデルが本文なし・ツール呼び出し
  なしの「空応答」で黙って止まった時の自動回復用（4-2節で詳説）。
- `HTTP_RETRY_LIMIT` / `HTTP_RETRY_DELAY` はOllama呼び出し自体がHTTPエラー等で
  失敗した時の自動再試行用（4-1節で詳説）。
- `CMD_TIMEOUT=180` は `run_command` ツール1回あたりのタイムアウト。
- `NUM_CTX=32768` はOllamaへ送るコンテキスト長。小型モデル・低VRAM機では
  `16384` 等に下げる調整ポイント。

会話履歴はサーバー内メモリではなく **1会話=1 JSONファイル** で永続化する:

```python
CANCEL = {}            # sid -> threading.Event
HISTORY_DIR = ROOT / "history"   # チャット履歴の保存先 (1会話 = 1 JSONファイル)
HISTORY_DIR.mkdir(exist_ok=True)
```
（`server.py:55-57`）

### 1-1. 起動ごとのCSRFトークンとワークスペース境界

```python
# CSRF対策: 起動ごとのランダムトークン。index.html配信時に埋め込み、
# 全POST APIで X-LocalCoder-Token ヘッダとして要求する。
# 外部サイトからの no-cors POST はこの値を知り得ないため全て拒否される。
TOKEN = secrets.token_hex(16)
HOME = Path.home().resolve()
# 作業フォルダ・ファイル操作を許可するルート。既定は WSL ホーム + Windows ドライブ(/mnt)。
ALLOWED_ROOTS = [Path(p).expanduser().resolve() for p in
                 os.environ.get("LOCALCODER_ALLOWED_ROOTS",
                                f"{HOME}:/mnt").split(":") if p]
# 画面初期表示時の作業フォルダ。個人の作業パスをリポジトリに埋め込まないよう
# 環境変数で指定する(未設定ならHOME)。index.html配信時にwindow変数として埋め込む。
DEFAULT_WORKSPACE = os.environ.get("LOCALCODER_DEFAULT_WORKSPACE", str(HOME))
```
（`server.py:59-74`）

`TOKEN` はプロセス起動のたびに毎回変わる32文字のランダム値。`do_GET`（3節）が
`index.html`配信時に埋め込み、`_post_ok()`/`_token_ok()`（2節）が検証する。
`ALLOWED_ROOTS`は`handle_chat()`（4節）でワークスペースの範囲チェックに使う——
既定は`$HOME`と`/mnt`（Windowsドライブ）で、`/mnt/c/...`のWindowsファイルも
編集できる（詳細は5-2節）。`LOCALCODER_ALLOWED_ROOTS`（コロン区切り）で上書きでき、
`$HOME`だけに戻すこともできる。トークン/ワークスペース制限はいずれも「127.0.0.1
バインドだけでは足りない」という設計判断への対応であり、詳しくは2節・4節・
5-2節で説明する。

`DEFAULT_WORKSPACE`は画面の作業フォルダ欄に最初から入れておく値。個人の作業パスを
`index.html`に直書きするとリポジトリ公開時にユーザー名やプロジェクト構成が漏れるため、
`LOCALCODER_DEFAULT_WORKSPACE`環境変数（起動スクリプト側で機種ごとに設定）から注入する
方式にした。未設定時は`HOME`にフォールバックするので、リポジトリ標準のindex.htmlは
特定ユーザーの情報を含まない。

---

## 2. HTTPリクエストの一次防御 — `_host_ok` / `_token_ok` / `_post_ok`

`Handler`には3つの検証ヘルパーがあり、GET/POSTの各ハンドラが用途に応じて組み合わせる:

```python
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
```
（`server.py:641-667`）

なぜこれが要るか: `ThreadingHTTPServer` は `127.0.0.1` にしかバインドしていない
（7節）が、それだけでは**同じPC上でブラウザが開いている悪意あるWebページ**からの
攻撃を防げない。ブラウザの同一オリジンポリシーは「レスポンスを読む」ことは
禁止するが、`fetch(url, {mode:"no-cors", method:"POST", body:...})` のような
**送るだけ**のリクエストはクロスオリジンでも通ってしまう。もし何のチェックも
無ければ、悪意あるページが `http://localhost:8765/api/chat` に任意のプロンプトを
POSTし、`run_command` 経由でローカルマシン上のコマンドを実行させられる。GET側にも
同様の懸念がある：DNSリバインディング（攻撃者ドメインの名前解決を後から127.0.0.1に
切り替える手法）を使うと、通常のCORS/SOPでは防げない形で同一オリジン扱いになり、
`/api/sessions`のような履歴データを読まれうる。そのため`_host_ok()`はGET/POST
両方の入口（`do_GET`/`_post_ok`）で必ず通す。

役割の違い:

| 関数 | 使う場面 | 防ぐ攻撃 |
|---|---|---|
| `_host_ok()` | 全GET・全POST | DNSリバインディング（Hostヘッダが攻撃者ドメインのまま） |
| `_post_ok()`内のOrigin検証 | POST全般 | 他オリジンのページからのクロスオリジンPOST |
| `_post_ok()`内のContent-Type検証 | POST全般 | `no-cors`では`application/json`を送れない（単純リクエストの制約）ため、これだけでも大半のCSRFを阻止できる |
| `_token_ok()` | POST全般＋履歴系GET | 上記をすり抜けても、トークンを知らない限り最終的に拒否される本命の防御 |

`secrets.compare_digest` はタイミング攻撃（文字列比較にかかる時間差からトークンを
推測する攻撃）を避けるための定数時間比較。

`do_POST`（3節）は`_post_ok()`が `False` を返すと即座に `403 forbidden` を返し、
`/api/stop` や `/api/chat` の中身には一切入らない。`do_GET`は`_host_ok()`のみを
入口でチェックし、履歴を返すパスだけ個別に`_token_ok()`も要求する（3節）。

---

## 3. HTTPエンドポイント一覧

`Handler` クラス（`http.server.BaseHTTPRequestHandler` 継承）がGET/POSTを捌く。

```python
def do_GET(self):
    if not self._host_ok():
        self._json({"error": "forbidden"}, 403)
        return
    if self.path in ("/", "/index.html"):
        ...
    elif self.path.startswith("/vendor/"):
        ...
    elif self.path == "/api/models":
        ...
    elif self.path == "/api/sessions":
        ...
    elif self.path.startswith("/api/session?"):
        ...
    elif self.path == "/api/health":
        ...
```
（`server.py:670-732`）

```python
def do_POST(self):
    if not self._post_ok():
        self._json({"error": "forbidden"}, 403)
        return
    if self.path == "/api/stop":
        ...
    if self.path == "/api/session/delete":
        ...
    if self.path == "/api/chat":
        self.handle_chat()
        ...
```
（`server.py:735-756`）

| メソッド | パス | 役割 |
|---|---|---|
| GET | `/`, `/index.html` | GUI本体（`index.html`）にCSRFトークンを埋め込んで返す |
| GET | `/vendor/*.js` | 同梱の`marked`/`DOMPurify`を静的配信（CDN不使用） |
| GET | `/api/models` | Ollamaの `/api/tags` を中継してモデル一覧を返す |
| GET | `/api/sessions` | 保存済み会話一覧（`history/*.json`）**要トークン** |
| GET | `/api/session?sid=...` | 特定セッションの履歴を返す **要トークン** |
| GET | `/api/health` | 死活監視用（`{"ok": true}`） |
| POST | `/api/stop` | 実行中エージェントループの中断シグナル（要トークン） |
| POST | `/api/session/delete` | セッション削除（要トークン） |
| POST | `/api/chat` | **本体**。ユーザー発話を受けてエージェントループを開始しSSEで応答（要トークン） |

全GETは入口の`_host_ok()`でDNSリバインディング対策を通る（2節）。加えて
`/api/sessions`・`/api/session?sid=`はプロンプト・ツール結果・ファイル内容という
機密を返すため、個別に`_token_ok()`も要求する。`/`・`/vendor/*.js`・`/api/models`・
`/api/health`はトークン取得前に呼ぶ必要があり、かつ機密を返さないため対象外。

`/`（トップページ）配信時、`index.html`のテンプレートに毎回新しいトークンを
文字列置換で埋め込む:

```python
if self.path in ("/", "/index.html"):
    body = (ROOT / "index.html").read_bytes()
    inject = (f'<script>window.LC_TOKEN={json.dumps(TOKEN)};'
             f'window.LC_DEFAULT_WORKSPACE={json.dumps(DEFAULT_WORKSPACE)};'
             f'</script></head>').encode()
    body = body.replace(b"</head>", inject, 1)
    self.send_response(200)
    ...
```
（`server.py:674-680`）

クライアント側（`index.html`）はこの `window.LC_TOKEN` を読み、全POSTで
`X-LocalCoder-Token` ヘッダとして送り返す（後述）。同時に埋め込まれる
`window.LC_DEFAULT_WORKSPACE`（1-1節）は、作業フォルダ欄の初期値として
ページ読み込み時にJSが1回だけ反映する（値は`json.dumps()`でエスケープしてから
埋め込むため、パスに引用符等が含まれてもスクリプトインジェクションにならない）。

### 3-1. 同梱JSの配信 — `/vendor/*.js`

```python
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
```
（`server.py:685-697`）

`index.html`は`marked`/`DOMPurify`を`https://cdn.jsdelivr.net/...`ではなく
`/vendor/marked.min.js`・`/vendor/purify.min.js`から読み込む。このページには
`window.LC_TOKEN`（＝コマンド実行に到達できる権限）が埋め込まれるため、そこで
動くJSは事実上同じ権限を持つ。CDN配信のままだと、CDN側の改ざんや、バージョン
無指定URLでの意図しない自動更新がそのままローカルマシンでのコマンド実行に
直結してしまう。ファイル名は `re.fullmatch(r"[\w.-]+\.js", name)` で
英数字・`.`・`-`・`_`のみかつ`.js`拡張子に限定しており、`../`のような
パストラバーサル文字列は正規表現にマッチしないため`f.is_file()`まで到達せず
404になる。

`/api/models` はOllama未起動時にエラーを日本語化して返す:

```python
elif self.path == "/api/models":
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as r:
            data = json.loads(r.read())
        self._json({"models": [m["name"] for m in data.get("models", [])]})
    except Exception as e:
        self._json({"error": f"Ollamaに接続できません: {e}"}, 502)
```
（`server.py:698-704`）

### 3-2. 作業フォルダ選択ダイアログ — `/api/browse`

GUIの「📁 参照」ボタンが叩くエンドポイント:

```python
elif self.path.startswith("/api/browse"):
    # フォルダ選択ダイアログ用。ディレクトリ構造の開示のためトークン必須
    if not self._token_ok():
        self._json({"error": "forbidden"}, 403)
        return
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    self._json(list_subdirs(q.get("path", [""])[0]))
```
（`server.py:705-711`）

ブラウザ標準の`<input type="file" webkitdirectory>`はセキュリティ上、選択した
フォルダの絶対パスを返さない（相対パスのファイル一覧しか取れない）ため、作業
フォルダの選択には使えない。かわりにこのエンドポイントで`$HOME`配下のディレクトリ
構造をサーバー側から返し、`index.html`側にモーダル式のブラウザを実装している
（5-2節`list_subdirs()`参照）。ディレクトリ構造もプライベートな情報なので、
履歴系と同様にトークンを要求する。

---

## 4. エージェントループの本体 — `handle_chat`

`/api/chat` にPOSTされると（＝`_post_ok()`を通過した後）、レスポンスは即座に
**SSE (Server-Sent Events)** に切り替わる:

```python
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
```
（`server.py:758-780`）

ここで重要なのは2点:

1. **会話履歴をサーバーが保持しない** 設計。クライアント（`index.html`）が
   毎回全履歴を `messages` として送り、サーバーはシステムプロンプトを先頭に
   足すだけのステートレス構成になっている。
2. **ワークスペースは `ALLOWED_ROOTS` 配下のみ許可**（既定=`$HOME` + Windows
   ドライブ`/mnt`）。リクエストの `workspace` 値を無条件に信用すると `/etc` 等の
   システムディレクトリを作業場に指定されうるため、`ws.resolve()` した結果が
   いずれかの許可ルート配下であることを`under_allowed()`（5-2節）でチェックする。
   このチェックを通過した `ws` だけが以降 `exec_tool()`（5節）に渡る。
   `/mnt`が既定で含まれるので`/mnt/c/Users/...`のようなWindowsフォルダを作業場に
   できる。

### 4-1. Ollamaへストリーミング問い合わせ

```python
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
```
（`server.py:785-824`）

反復の先頭で毎回 `compact_history()`（4-4節）を通しているのは、リクエスト開始時に
長すぎる履歴が来た場合と、ツール結果が反復のたびに積み上がって途中で予算を超える
場合の両方に対応するため（予算内なら何もしない）。

**作業状態ダッシュボード（`build_work_state`, 4-1a節参照）**: 続けて
`work_state = build_work_state(messages[1:])` を計算し、非空なら
`messages`本体ではなく`call_messages`という別変数に一時的に追加する。
`payload["messages"]`にはこの`call_messages`を使うが、応答受信後に
`messages.append(amsg)`するのは元の`messages`のほう（4-2節）——つまり
ダッシュボードは**その回のOllama呼び出しにだけ**見え、保存される会話履歴や
次回リクエストの`history`には一切残らない使い捨ての情報である。

### 4-1a. 作業状態ダッシュボード — `build_work_state`

会話ログの要約（4-4節）とは別に、「変更したファイル」「直近の実行コマンドと
結果」「同じコマンドの繰り返し失敗」を**LLMを使わず機械的に**履歴から
抽出して短い文字列にする:

```python
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
```
（`server.py:494-565`）

`_iter_tool_calls_with_results`は、`assistant`メッセージの`tool_calls`配列と、
その直後に続く`tool`ロールのメッセージ列を**同じ順序で**突き合わせる
（`exec_tool`が`for tc in tool_calls:`で1つずつ実行し、その都度
`messages.append({"role":"tool",...})`しているため、必ずこの順序で並ぶ——4-2節）。

`build_work_state`が拾う3種の情報:
1. **変更ファイル一覧**: `write_file`/`edit_file`の`path`引数を出現順・重複なしで収集
2. **直近`RECENT_COMMANDS_SHOWN`(5)件のコマンドと結果**: `run_command`の結果が
   `exit_code=0`で始まるかどうかで成功/失敗を判定し、結果の先頭行を添える
3. **繰り返し失敗の警告**: 直近`FAIL_REPEAT_THRESHOLD`(3)件が**同一コマンド**かつ
   **全て失敗**なら、同じアプローチを繰り返さず根本原因を洗い直すよう促す一文を追加

なぜ機械的に(LLMを使わず)行うか: 要約(`summarize_old`)と同じ土俵でLLMに
「今何が起きているか」を書かせると、要約自体が不正確になるリスクをそのまま
引き継ぐ。ここで拾う3種の情報はいずれもプログラムで確定的に導出できる事実
（ファイルパスの文字列、コマンドの終了コード）なので、コード側で機械的に
組み立てる方が安価かつ100%正確になる。目的・サブタスク・「どの仮説が
外れたか」といった意味的な判断はLLMに頼らざるを得ないため、今回はあえて
対象外にした（ユーザーとの設計議論の結論。詳細はREBUILD.md参照）。

圧縮済み(`compact_history`が要約に置き換えた)古い部分は`tool_calls`構造が
失われているため、この関数は**直近の非圧縮ウィンドウのみ**を反映する。
古い変更点は要約の自然文側（`SUMMARIZE_PROMPT`）に残る。

検証: 単体テスト10件（空履歴・ファイル抽出・コマンド結果表示・3回連続失敗の
検知・2回では未検知・別コマンド成功後は誤検知しない・`messages`本体を
書き換えないこと）に加え、実際のgpt-oss:20bで通常タスク（hello.py作成）が
従来通り動くこと、`cmake ..`を3回連続失敗させた履歴から続行させた場合に
4回目も同じコマンドを盲目的に繰り返さなかったことを確認済み。

**Ollama呼び出し失敗時の自動再試行**: `ollama_stream(payload)`は`urlopen()`を
内部で呼ぶため、Ollama側がHTTP 500やタイムアウト・接続断で応答すると
`urllib.error.URLError`（`HTTPError`はそのサブクラス）が送出される。実運用で
gpt-oss:20bが一過性の500エラーを返す事例に遭遇したため、この例外を
`try/except`で捕捉し、`HTTP_RETRY_LIMIT`(1)回まで`HTTP_RETRY_DELAY`(2秒)待って
`continue`——同じ`payload`のまま次の`for it`周回に入り、Ollamaへ再度問い合わせる。
例外は`for chunk in ollama_stream(...)`の**最初の`next()`時点**（＝`urlopen()`
実行時）で送出されるため、`content`等はまだ空のまま失敗しており、再試行しても
二重送信や履歴破損は起きない（`assistant`メッセージはストリーム完了後にしか
`messages`へ追加されないため）。上限を超えたら`raise`で再送出し、外側の
`except urllib.error.URLError`（4-3節）に処理を委譲して通常通りエラー表示する。
`http_retries`も`empty_retries`と同様`handle_chat`のローカル変数で、
ユーザーの1メッセージごとにリセットされる。モックテストで回復ケース
（1回失敗→再試行で成功）と行き詰まりケース（2回とも失敗→通知後に通常の
エラー表示、呼び出しは2回で打ち切り）の両方を検証済み。

`ollama_stream` は `/api/chat` にJSON Linesでストリーミングする薄いラッパー:

```python
def ollama_stream(payload: dict):
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            line = line.strip()
            if line:
                yield json.loads(line)
```
（`server.py:371-379`）

（`ollama_ask` は履歴要約用の非ストリーミング問い合わせ。4-4節参照。）

チャンクごとに `thinking`（推論過程）・`content`（本文）・`tool_calls`（ツール呼び出し要求）
の3種が流れてきうる。それぞれをそのままブラウザへSSEで中継する
（`type: "think"` / `type: "token"`）ので、GUI側はモデルの思考過程を逐次表示できる。

ループの各反復の先頭で `ev.is_set()`（キャンセルフラグ）をチェックしており、
ユーザーが停止ボタンを押すと、ストリーミング受信中でも次のチャンクを待たずに
即座に打ち切れる。

### 4-2. ツール呼び出しがなければ終了、あれば実行して次の往復へ

```python
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
    result = exec_tool(name, args, ws, ev)
    self._sse({"type": "tool_end", "name": name,
               "result": result if len(result) <= 4000
               else result[:4000] + "\n...[truncated]..."})
    messages.append({"role": "tool", "tool_name": name,
                     "name": name, "content": result})
```
（`server.py:826-867`）

これが **エージェントループの心臓部**：
1. モデルの応答（`assistant` メッセージ）を履歴に追加
2. `tool_calls` が無ければ「タスク完了」とみなしループを抜ける——ただし本文も
   空なら「空応答」（下記）として扱う
3. あれば `exec_tool(name, args, ws, ev)` で実際に実行し、結果を `role: "tool"`
   メッセージとして履歴に追加。**キャンセル用の `ev` を渡す**ことで、
   `run_command`（5-1節）が実行中でも停止ボタンに反応できるようにしている
4. `messages` が増えた状態で `for it in range(MAX_ITER)` の次周回に入り、
   再びOllamaに問い合わせる（モデルはツール結果を見て次の一手を判断する）

`tool_name` と `name` を両方入れているのは、Ollamaのバージョン間でtoolメッセージの
キー名が変わった経緯を吸収するため。

**空応答からの自動回復**: ローカルモデル（特にgpt-oss:20b）は、ビルド失敗など
行き詰まった状況で本文なし・ツール呼び出しなしという「空応答」を返し、ターンを
静かに終えることがある。これは`if not tool_calls: break`にそのまま入ってしまうと、
サーバーは正常終了として`all_done`を返すため、**ユーザーには何も起きていないよう
に見えて実質的に停止する**（実運用で遭遇し、保存済みセッションの最後の
assistantメッセージが`content=""`であることで確認したバグ）。対策として、
`content`が空文字かつ`tool_calls`も無い場合は`EMPTY_RETRY_LIMIT`(1)回まで、
「続けてください」という合成`user`メッセージ（`EMPTY_RESPONSE_NUDGE`。
`(システム自動継続)`と明記し、会話履歴を見返したときに人間の発言と区別できる
ようにしてある）を追加して`continue`し、ループを継続する。それでも空応答なら
諦めて`{"type": "notice", "message": "⚠ ..."}`をGUIに送り、ユーザーに手動介入を
促して`break`する。`empty_retries`は`handle_chat`のローカル変数なので
ユーザーの1メッセージごとにリセットされ、`EMPTY_RETRY_LIMIT`で上限も切って
あるため無限ループの心配はない。

`MAX_ITER` 回を超えてもツール呼び出しが続く場合はエラーを返す:

```python
else:
    turn_status = "max_iter"
    self._sse({"type": "error",
               "message": f"最大ループ回数({MAX_ITER})に達しました"})
```
（`server.py:868-871`）

（この `else` は `for...else` 構文で、`break` されずにループが尽きた場合のみ実行される）

### 4-3. 終了処理

```python
self._sse({"type": "history", "messages": messages[1:]})
self._sse({"type": "all_done"})
```
（`server.py:874-875`）

システムプロンプト（`messages[0]`）を除いた全履歴をクライアントへ返す。
クライアントはこれを次回リクエストの `messages` としてそのまま送り返すことで
文脈を維持する。

`finally` 節で、成功・エラー・中断のいずれの経路でも会話をディスクへ自動保存する。
あわせて「プロンプトを受け取った時刻」と「完了/中断した時刻」も記録する:

```python
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
```
（`server.py:890-899`）

`turn_started_at`は`handle_chat()`冒頭（`server.py:759`）で取得する受信時刻。
`turn_status`は初期値`"completed"`で、以降の各終了経路で上書きされる:
停止ボタン検知2箇所（ストリーム受信中・ツール実行中）で`"stopped"`、
`for...else`のMAX_ITER到達で`"max_iter"`、`BrokenPipeError`/
`ConnectionResetError`で`"disconnected"`、`URLError`やその他の例外で`"error"`。
何も上書きされなければ通常完了の`"completed"`のまま`finally`に到達する。

`save_session()`自体は元々毎回ファイル全体を上書きする実装のため、単純に
`turns`キーを追加しただけでは前回までの記録が消える。そのため保存前に既存
ファイルがあれば`turns`配列を読み出し、そこに今回の`turn`をappendしてから
書き戻す（`server.py:216-235`）。個々の`messages`配列には一切手を入れず、
独立した`turns`配列にのみ時刻を記録することで、Ollamaに送るメッセージの
スキーマにも`compact_history()`のトークン見積もりにも影響を与えない。

### 4-4. 履歴の自動圧縮 — `compact_history`

長い会話やツール結果の蓄積で履歴が `NUM_CTX`(32768) に近づくと、ollamaは黙って
プロンプト前方を切り捨てる。システムプロンプトやツール定義の文脈が押し出されると
エージェントは静かに壊れるため、各反復の先頭で `compact_history()` を通し、
予算超過時にサーバー側で自動圧縮する。

```python
RESERVE_TOKENS = 8192   # 生成(thinking含む)+システムプロンプト用に確保する分
KEEP_RECENT_MSGS = 6    # 要約時に原文のまま残す直近メッセージ数
KEEP_RECENT_TOOLS = 4   # 切り詰めずに残す直近のツール結果数
TOOL_TRIM_CHARS = 500   # 古いツール結果の切り詰め後サイズ
MSG_EXCERPT_CHARS = 1000            # 要約入力で1メッセージから取る最大文字数
SUMMARIZE_INPUT_TOKENS = NUM_CTX // 2  # 要約1回の入力上限 (超えたら分割要約)
```
（`server.py:48-53`）

処理は2段階＋フォールバック:

1. **第1段階 (安価・LLM不使用)** — `trim_old_tool_results()`: 直近
   `KEEP_RECENT_TOOLS` 件を除くツール結果を500文字に切り詰める。ツール出力
   （ビルドログ・ファイル内容等）が履歴肥大の主因で、古い分は詳細が不要な
   ことが多い。これで予算内に収まれば要約はしない。
2. **第2段階 (LLM要約)** — 直近 `KEEP_RECENT_MSGS` 件を原文のまま残し、それ以前を
   `summarize_old()` で要約して `{"role": "user", "content": "【自動要約】…"}` の
   1メッセージに置換する。分割境界が `tool` メッセージに当たる場合は、呼び出し元
   assistant とのペアが壊れないよう境界を手前へずらす。
3. **フォールバック** — 要約呼び出しが失敗（Ollama エラー等）した場合は
   「【自動省略】」マーカーで単純省略する。文脈は失われるが、溢れて静かに壊れる
   よりは明示的に欠落を伝えるほうがよい。

トークン数は `estimate_text_tokens()` で概算する（ASCII=4文字/トークン、日本語等の
非ASCII=1文字/トークン）。厳密ではないが、圧縮の発動判定には十分な精度。

**要約入力自体の肥大対策**（`summarize_old()`, `server.py:467-491`）: 要約対象の
ログが `NUM_CTX` を超えると、要約プロンプト自体の前方（=要約指示）が ollama に
切り捨てられ、モデルがログをオウム返しするだけの壊れた「要約」を返す——という
欠陥が実際に発生した。対策として、(a) 各メッセージを先頭7割+末尾3割の1000文字に
抜粋化し、(b) それでも入力が `SUMMARIZE_INPUT_TOKENS`(NUM_CTX/2) を超える場合は
チャンクに分割して各々要約し結合する。(c) 要約指示はログの前後両方に置く。
検証では、履歴冒頭に埋めた固有情報が 56868→7746 トークンへの圧縮を生き残り、
圧縮後の会話でモデルが正答した。

圧縮が起きると `{"type": "compact", "message": "…"}` SSEイベントが流れ、GUIに
「🗜 履歴を圧縮しました (推定 X→Y トークン)」と表示される。圧縮後の履歴は
`history` イベント経由でクライアントに渡るため、次のリクエストから恒久的に
圧縮済み履歴が使われる（`history/<sid>.json` にも圧縮後が保存される）。

---

## 5. ツール実行 — `exec_tool` / `run_command`

`exec_tool` が7種のツール名を振り分ける:

```python
def exec_tool(name: str, args: dict, ws: Path, cancel=None) -> str:
    try:
        if name == "run_command":
            return run_command(args["command"], ws, cancel)
        if name == "read_file":
            f = resolve_path(ws, args["path"])
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
            ...  # 空文字/同一文字列/ファイル不存在チェック
            t = f.read_text(errors="replace")
            n = t.count(old)
            if n == 0:
                return ("ERROR: old_string not found in file. Use read_file to see "
                        ...)
            if n > 1 and not args.get("replace_all"):
                return (f"ERROR: old_string occurs {n} times. ..."
                        ...)
            f.write_text(t.replace(old, new))
            return (f"OK: replaced ...")
        if name == "list_dir":
            f = resolve_path(ws, args.get("path") or ".")
            items = sorted(e.name + ("/" if e.is_dir() else "") for e in f.iterdir())
            return "\n".join(items)[:8000] or "(empty)"
        if name == "web_search":
            return web_search(args["query"], int(args.get("max_results") or 6))
        if name == "fetch_url":
            return fetch_url_text(args["url"])
        return f"ERROR: unknown tool {name}"
    except Exception as e:  # noqa: BLE001 - report all tool errors to the model
        return f"ERROR: {type(e).__name__}: {e}"
```
（`server.py:320-368`、edit_fileのエラーメッセージ等は抜粋）

設計上の要点:

- **例外は握りつぶさずモデルに返す**（`except Exception as e: return f"ERROR: ..."`）。
  これにより、例えば `python` コマンドが無い環境でエラーが返ると、モデルが
  `python3` に自分で切り替えて再試行する、といった自己回復的な挙動が生まれる。
- **read_file / write_file / edit_file / list_dir はすべて `resolve_path()` を経由**
  する（5-2節）。
- **edit_file は既存ファイルの部分修正専用**。全文書き換え（write_file）だと大きい
  ファイルほど出力トークンを浪費し、小型モデルは途中の行を書き換え忘れて壊しやすい
  ため、システムプロンプトで edit_file 優先に誘導している。old_string は完全一致かつ
  一意でなければならず、0件/複数件ヒット時のエラーメッセージには「read_fileで正確に
  コピーせよ」「文脈を足して一意にせよ／replace_all=trueにせよ」「無理なら
  write_fileで書き直せ」という次の一手が書いてあり、失敗してもモデルが自力で
  回復できるようにしてある。

### 5-1. `run_command` — 停止ボタンとタイムアウトを実効化する

```python
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
```
（`server.py:288-317`）

以前は `subprocess.run(..., timeout=CMD_TIMEOUT)` を1回呼ぶだけの実装だったが、
それだと**停止ボタンを押しても`communicate()`がブロックしたままで、実行中の
コマンドを殺せない**問題があった（UIだけ「停止しました」と表示されて、裏では
コマンドが動き続ける）。今の実装は0.5秒ごとに `communicate(timeout=0.5)` を
ポーリングし、その都度 `cancel.is_set()`（停止ボタン）と `deadline`（180秒
タイムアウト）の両方をチェックする。どちらかに該当したら `os.killpg()` で
**プロセスグループごと** SIGKILL する。`start_new_session=True` でプロセスを
専用のセッション/プロセスグループに分離しているため、`cmd`がパイプやバック
グラウンド子プロセスを生んでいても道連れにできる（単に `p.kill()` するだけでは
`bash -lc` の子孫プロセスが残ってしまう）。

出力が12KBを超えると先頭6KB＋末尾6KBに切り詰める。小型ローカルモデルは
コンテキストが溢れると応答が破綻しやすいための防御策。

### 5-2. パスサンドボックス — `resolve_path`

```python
def resolve_path(ws: Path, p: str) -> Path:
    full = Path(p) if os.path.isabs(p) else ws / p
    full = full.resolve()
    ws = ws.resolve()
    if not (str(full) == str(ws) or str(full).startswith(str(ws) + os.sep)):
        raise ValueError(f"path is outside the workspace: {p}")
    return full
```
（`server.py:253-259`）

作業フォルダ選択ダイアログ用に、`resolve_path`の直後に類似ロジックの
ディレクトリ一覧関数がある:

```python
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
```
（`server.py:262-285`）

`under_allowed`は`ALLOWED_ROOTS`（既定=`$HOME` + `/mnt`）配下かを判定する。
`resolve_path`（ファイルツールのサンドボックス）は「選ばれたワークスペース配下」に
閉じるので変更不要——ワークスペースが`/mnt/c/...`ならファイルツールもそのWindows
フォルダ内に閉じる。`under_allowed`は`handle_chat`のワークスペース検証（4節）と
この`list_subdirs`が使う。範囲外や存在しないパスは黙って`HOME`にフォールバックする
（GUI側は毎回サーバーの返す`path`を正として画面を更新するので、フォールバックが
起きても不整合にならない）。隠しディレクトリ（`.`始まり、`.git`等）は一覧から除外し、
許可ルート自身（`HOME`や`/mnt`）では`parent`が`None`になり「上へ」を出さない
（例: `/mnt/c/Users`の親は`/mnt/c`で辿れるが、`/mnt`まで上がるとそこがルートなので
止まる）。`/api/browse`（3節）がこの関数を呼ぶ。Windowsフォルダを作業場にしたい
場合は作業フォルダ欄に`/mnt/c/Users/<名前>/...`を直接入力するか、参照ダイアログで
そこまで辿る。

並び順は `key=str.lower` で大文字小文字を無視する。素の`sorted()`はUnicode
コードポイント順（大文字A-Zが小文字a-zより前）になるため、例えば
`MP3Player`が`arm-gnu-toolchain`より前に来てしまい、人が期待する辞書順
（大文字小文字を区別しない名前順）にならない。

相対パスはワークスペース基準で解決し、絶対パスもいったん `resolve()` して
シンボリックリンク経由の脱出も含めて正規化した上で、文字列前方一致で
ワークスペース外へのアクセスを拒否する。**ファイル操作系ツールがワークスペースの
外に触れられないようにする防波堤**がこの関数。

一方、`run_command` はこのサンドボックスの対象外（`cwd=ws` を渡すのみ）であり、
`cd ..` や絶対パス指定で任意の場所を触れてしまう。README/REBUILD.mdにも
「サンドボックスなし・ユーザー権限フル実行が設計方針」と明記されている通り、
ツール実行そのものに制限は設けていない。かわりに実際の防御線は4層になっている:

1. **`ThreadingHTTPServer` が `127.0.0.1` のみにバインド**（7節）— 他ホストから
   直接叩けない
2. **`_host_ok()` による全GET/POST共通のHost検証**（2節）— DNSリバインディングで
   ①を迂回されるのを防ぐ
3. **`_post_ok()` によるCSRFトークン検証**（2節）— 同一PC上の悪意あるWebページ
   からのno-cors POSTを拒否する（127.0.0.1バインドだけでは防げない攻撃面）。
   履歴を返す`/api/sessions`等はGETでも`_token_ok()`を要求する（3節）
4. **ワークスペースを `$HOME` 配下に制限**（4節）— 万一①〜③を越えられても、
   操作対象のディレクトリ自体を広げられない

もう1つ別軸の防御として、**依存JSをCDNから読み込まない**（3-1節）がある。
`window.LC_TOKEN`が埋め込まれたページで動くJSは③のトークンをそのまま使える
権限を持つため、CDN側の改ざんやバージョン無指定URLの自動更新をサプライチェーン
攻撃の入口にしないよう、`marked`/`DOMPurify`を`vendor/`にバージョン固定で同梱している。

---

## 6. Web検索・ページ取得ツールの実装

外部APIキーなしで動く検索は DuckDuckGoのHTML版をスクレイピングして実現している:

```python
def web_search(query: str, max_results: int = 6) -> str:
    html_text = http_get("https://html.duckduckgo.com/html/?q="
                         + urllib.parse.quote(query))
    titles = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html_text, re.S)
    ...
```
（`server.py:158-176`）

正規表現でDuckDuckGoのHTML構造から検索結果のリンク・タイトル・スニペットを
抜き出しているだけの軽量実装（＝DuckDuckGo側のHTML構造が変わると壊れる）。

ページ本文取得は自前の `HTMLParser` サブクラスでタグを除去する:

```python
class _TextExtract(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    ...
    def handle_data(self, d):
        if not self.depth and d.strip():
            self.parts.append(d.strip())
```
（`server.py:179-197`）

`script`/`style`/`svg`/`head` タグの中身は無視しつつ、テキストノードだけを
収集する。結果は1万文字で切り詰められる（`server.py:207-208`）。

`fetch_url` で取得したテキストや `web_search` の結果はそのまま `tool` メッセージの
`content` としてモデルに渡り、最終的にモデルの応答（Markdown）としてブラウザに
描画されうる。そのため **クライアント側のMarkdown描画時のsanitize**（`index.html`
側、下記「関連ドキュメント」参照）が、この経路から入りうるHTML注入の最後の防波堤になる。

---

## 7. 起動処理

```python
def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"port {PORT} already in use — LocalCoder is probably already running")
        return
    print(f"LocalCoder running: http://localhost:{PORT}  (ollama: {OLLAMA})")
    srv.serve_forever()
```
（`server.py:902-909`）

`ThreadingHTTPServer` なので、複数タブ・複数セッションからの同時リクエストにも
スレッド単位で並行対応する。ポートが既に使用中（＝二重起動）の場合はエラーで
即終了するだけで、既存プロセスを奪ったり殺したりはしない
（README記載「サーバーが既に起動していれば二重起動しない」の実体）。

`("127.0.0.1", PORT)` へのバインドが第一の防御線だが、2節・5-2節で説明した通り
これ単体では同一PC上の悪意あるWebページからのCSRFやDNSリバインディングを防げない
ため、`_host_ok()`/`_post_ok()`のトークン検証、および3-1節の依存JS同梱と組み合わせて
初めて実用的な防御になっている。

---

## 8. 処理フロー図（概略）

```
ブラウザ(index.html)
   │  GET /  → _host_ok()検証 → HTMLにCSRFトークンを埋め込んで返す (window.LC_TOKEN)
   │  GET /vendor/*.js → 同梱marked/DOMPurifyを配信 (CDN不使用)
   ▼
   │  POST /api/chat  { model, workspace, messages }  + X-LocalCoder-Token
   ▼
_post_ok()  ── NG → 403 forbidden で即終了 (Host/Origin/Content-Type/トークン検証)
   │ OK
   ▼
handle_chat()
   │  workspace が $HOME 配下か検証 ── NG → エラーSSEで終了
   │  messages = [system] + body.messages
   ▼
┌─ for it in range(MAX_ITER) ──────────────────────────────┐
│  compact_history() 予算超過なら履歴を自動圧縮 → SSE: compact│
│  ollama_stream(payload) → SSE: think/token を逐次転送     │
│  tool_calls が無ければ break                              │
│  あれば: exec_tool(name,args,ws,cancel) を各tool_callで実行│
│    run_command は停止ボタン/タイムアウトで確実にkillpg     │
│         → SSE: tool_start / tool_end                      │
│         → messages に role:"tool" として結果を追加         │
└──────────────────────────────────────────────────────────┘
   │
   ▼
SSE: history(全履歴) → all_done
   │
   ▼
save_session()  (history/<sid>.json に保存)
```

---

## 関連ドキュメント

- [README.md](README.md) — 起動方法・使い方の概要
- [REBUILD.md](REBUILD.md) — 別PCへの移植手順、設計上の注意点、実施例ログ
- [index.html](index.html) — 本ドキュメントで扱ったSSEイベント（`think`/`token`/
  `tool_start`/`tool_end`/`turn_done`/`history`/`all_done`/`error`）を受け取り、
  `window.LC_TOKEN` を全POST＋履歴系GET（`getAuth()`）に付与し、Markdown描画時に
  `DOMPurify.sanitize()` を通し、同梱の`/vendor/marked.min.js`・`/vendor/purify.min.js`
  を読み込むGUI側の実装
- [vendor/](vendor/) — CDNから読み込まず同梱している依存JS（`marked`・`DOMPurify`）。
  バージョンは`vendor/*.version`に記録、更新手順はREBUILD.md「3-④」参照
