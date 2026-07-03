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
MAX_ITER = 40          # 1リクエストあたりの最大ツールループ回数
CMD_TIMEOUT = 180      # コマンド実行タイムアウト(秒)
NUM_CTX = 32768
```
（`server.py:25-30`）

- `OLLAMA` / `PORT` は環境変数で上書き可能。デフォルトはどちらもlocalhost想定。
  WSLからWindows側Ollamaへ別経路で繋ぐ場合は `LOCALCODER_OLLAMA=http://<IP>:11434` を
  指定して起動する（実例は `REBUILD.md` の「8. 実施例ログ」参照）。
- `MAX_ITER=40` は「ユーザー1メッセージに対して、モデル発話→ツール実行を最大何往復
  許すか」の上限。無限ループでサーバーが固まるのを防ぐ安全弁。
- `CMD_TIMEOUT=180` は `run_command` ツール1回あたりのタイムアウト。
- `NUM_CTX=32768` はOllamaへ送るコンテキスト長。小型モデル・低VRAM機では
  `16384` 等に下げる調整ポイント。

会話履歴はサーバー内メモリではなく **1会話=1 JSONファイル** で永続化する:

```python
CANCEL = {}            # sid -> threading.Event
HISTORY_DIR = ROOT / "history"   # チャット履歴の保存先 (1会話 = 1 JSONファイル)
HISTORY_DIR.mkdir(exist_ok=True)
```
（`server.py:32-34`）

### 1-1. 起動ごとのCSRFトークンとワークスペース境界

```python
# CSRF対策: 起動ごとのランダムトークン。index.html配信時に埋め込み、
# 全POST APIで X-LocalCoder-Token ヘッダとして要求する。
# 外部サイトからの no-cors POST はこの値を知り得ないため全て拒否される。
TOKEN = secrets.token_hex(16)
HOME = Path.home().resolve()     # ワークスペースはこの配下のみ許可
```
（`server.py:36-40`）

`TOKEN` はプロセス起動のたびに毎回変わる32文字のランダム値。`do_GET`（3節）が
`index.html`配信時に埋め込み、`_post_ok()`（2節）が全POSTで検証する。`HOME`は
`handle_chat()`（4節）でワークスペースの範囲チェックに使う。どちらも「127.0.0.1
バインドだけでは足りない」という設計判断への対応であり、詳しくは2節・4節・
5-2節で説明する。

---

## 2. HTTPリクエストの一次防御 — `_post_ok`

全POSTリクエストは、パスごとの処理に入る前にこの関数でCSRF/DNSリバインディング
対策を通す:

```python
def _post_ok(self) -> bool:
    """POST の CSRF / DNSリバインディング対策。

    - Host がローカル以外 → DNSリバインディング攻撃
    - Origin がローカル以外 → 他サイトからのクロスオリジンPOST
    - Content-Type が application/json 以外 → no-cors で送れる単純リクエスト
    - トークン不一致 → このページを経由しないリクエスト
    """
    host = (self.headers.get("Host") or "").split(":")[0]
    if host not in ("localhost", "127.0.0.1"):
        return False
    origin = self.headers.get("Origin")
    if origin:
        if urllib.parse.urlparse(origin).hostname not in ("localhost", "127.0.0.1"):
            return False
    ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
    if ctype != "application/json":
        return False
    return secrets.compare_digest(
        self.headers.get("X-LocalCoder-Token", ""), TOKEN)
```
（`server.py:303-322`）

なぜこれが要るか: `ThreadingHTTPServer` は `127.0.0.1` にしかバインドしていない
（7節）が、それだけでは**同じPC上でブラウザが開いている悪意あるWebページ**からの
攻撃を防げない。ブラウザの同一オリジンポリシーは「レスポンスを読む」ことは
禁止するが、`fetch(url, {mode:"no-cors", method:"POST", body:...})` のような
**送るだけ**のリクエストはクロスオリジンでも通ってしまう。もし何のチェックも
無ければ、悪意あるページが `http://localhost:8765/api/chat` に任意のプロンプトを
POSTし、`run_command` 経由でローカルマシン上のコマンドを実行させられる。

4つのチェックはそれぞれ役割が違う:

| チェック | 防ぐ攻撃 |
|---|---|
| `Host` がlocalhost系 | DNSリバインディング（悪意あるドメインが名前解決だけ127.0.0.1に切り替える攻撃） |
| `Origin` がlocalhost系 | 他オリジンのページからのクロスオリジンPOST |
| `Content-Type: application/json` 必須 | `no-cors` モードでは`application/json`を送れない（単純リクエストの制約）ため、これだけでも大半のCSRFを阻止できる |
| `X-LocalCoder-Token` 一致 | 上3つをすり抜けても、トークンを知らない限り最終的に拒否される本命の防御 |

`secrets.compare_digest` はタイミング攻撃（文字列比較にかかる時間差からトークンを
推測する攻撃）を避けるための定数時間比較。

`do_POST`（3節）はこの関数が `False` を返すと即座に `403 forbidden` を返し、
`/api/stop` や `/api/chat` の中身には一切入らない。

---

## 3. HTTPエンドポイント一覧

`Handler` クラス（`http.server.BaseHTTPRequestHandler` 継承）がGET/POSTを捌く。

```python
def do_GET(self):
    if self.path in ("/", "/index.html"):
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
（`server.py:325-356`）

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
（`server.py:359-380`）

| メソッド | パス | 役割 |
|---|---|---|
| GET | `/`, `/index.html` | GUI本体（`index.html`）にCSRFトークンを埋め込んで返す |
| GET | `/api/models` | Ollamaの `/api/tags` を中継してモデル一覧を返す |
| GET | `/api/sessions` | 保存済み会話一覧（`history/*.json`） |
| GET | `/api/session?sid=...` | 特定セッションの履歴を返す |
| GET | `/api/health` | 死活監視用（`{"ok": true}`） |
| POST | `/api/stop` | 実行中エージェントループの中断シグナル（要トークン） |
| POST | `/api/session/delete` | セッション削除（要トークン） |
| POST | `/api/chat` | **本体**。ユーザー発話を受けてエージェントループを開始しSSEで応答（要トークン） |

GETエンドポイントにはトークン検証が無い（GETはブラウザの通常ナビゲーションで
発生し得るため、`no-cors`の脅威モデルとは別軸。読み取り専用でありコマンド実行を
一切引き起こさない）。

`/`（トップページ）配信時、`index.html`のテンプレートに毎回新しいトークンを
文字列置換で埋め込む:

```python
if self.path in ("/", "/index.html"):
    body = (ROOT / "index.html").read_bytes()
    body = body.replace(
        b"</head>",
        b'<script>window.LC_TOKEN="' + TOKEN.encode() + b'";</script></head>', 1)
    self.send_response(200)
    ...
```
（`server.py:325-335`）

クライアント側（`index.html`）はこの `window.LC_TOKEN` を読み、全POSTで
`X-LocalCoder-Token` ヘッダとして送り返す（後述）。

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
（`server.py:336-342`）

---

## 4. エージェントループの本体 — `handle_chat`

`/api/chat` にPOSTされると（＝`_post_ok()`を通過した後）、レスポンスは即座に
**SSE (Server-Sent Events)** に切り替わる:

```python
def handle_chat(self):
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
    if not (wsr == HOME or str(wsr).startswith(str(HOME) + os.sep)):
        self._sse({"type": "error",
                   "message": f"ワークスペースはホーム({HOME})配下のみ指定できます: {ws}"})
        return

    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(ws=ws)}]
    messages += body.get("messages", [])
```
（`server.py:382-401`）

ここで重要なのは2点:

1. **会話履歴をサーバーが保持しない** 設計。クライアント（`index.html`）が
   毎回全履歴を `messages` として送り、サーバーはシステムプロンプトを先頭に
   足すだけのステートレス構成になっている。
2. **ワークスペースは `$HOME` 配下のみ許可**。リクエストの `workspace` 値を
   無条件に信用すると `/etc` や `C:\` 等を作業場に指定されうるため、
   `ws.resolve()` した結果が `HOME` と一致するか、その配下であることを
   ここでチェックする（1-1節の `HOME` 定数）。このチェックを通過した `ws` だけが
   以降 `exec_tool()`（5節）に渡る。

### 4-1. Ollamaへストリーミング問い合わせ

```python
for it in range(MAX_ITER):
    payload = {"model": model, "messages": messages, "tools": TOOLS,
               "stream": True, "options": {"num_ctx": NUM_CTX}}
    content, thinking, tool_calls = "", "", []
    for chunk in ollama_stream(payload):
        if ev.is_set():
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
```
（`server.py:404-422`）

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
（`server.py:262-270`）

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
    break

for tc in tool_calls:
    if ev.is_set():
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
（`server.py:424-451`）

これが **エージェントループの心臓部**：
1. モデルの応答（`assistant` メッセージ）を履歴に追加
2. `tool_calls` が無ければ「タスク完了」とみなしループを抜ける
3. あれば `exec_tool(name, args, ws, ev)` で実際に実行し、結果を `role: "tool"`
   メッセージとして履歴に追加。**キャンセル用の `ev` を渡す**ことで、
   `run_command`（5-1節）が実行中でも停止ボタンに反応できるようにしている
4. `messages` が増えた状態で `for it in range(MAX_ITER)` の次周回に入り、
   再びOllamaに問い合わせる（モデルはツール結果を見て次の一手を判断する）

`tool_name` と `name` を両方入れているのは、Ollamaのバージョン間でtoolメッセージの
キー名が変わった経緯を吸収するため。

`MAX_ITER` 回を超えてもツール呼び出しが続く場合はエラーを返す:

```python
else:
    self._sse({"type": "error",
               "message": f"最大ループ回数({MAX_ITER})に達しました"})
```
（`server.py:452-454`）

（この `else` は `for...else` 構文で、`break` されずにループが尽きた場合のみ実行される）

### 4-3. 終了処理

```python
self._sse({"type": "history", "messages": messages[1:]})
self._sse({"type": "all_done"})
```
（`server.py:457-458`）

システムプロンプト（`messages[0]`）を除いた全履歴をクライアントへ返す。
クライアントはこれを次回リクエストの `messages` としてそのまま送り返すことで
文脈を維持する。

`finally` 節で、成功・エラー・中断のいずれの経路でも会話をディスクへ自動保存する:

```python
finally:
    # 会話を自動保存 (エラーや途中停止でもそこまでの内容を残す)
    if len(messages) > 1:
        try:
            save_session(sid, model, str(ws), messages[1:])
        except Exception:
            pass
```
（`server.py:471-477`）

---

## 5. ツール実行 — `exec_tool` / `run_command`

`exec_tool` が6種のツール名を振り分ける:

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
（`server.py:234-259`）

設計上の要点:

- **例外は握りつぶさずモデルに返す**（`except Exception as e: return f"ERROR: ..."`）。
  これにより、例えば `python` コマンドが無い環境でエラーが返ると、モデルが
  `python3` に自分で切り替えて再試行する、といった自己回復的な挙動が生まれる。
- **read_file / write_file / list_dir はすべて `resolve_path()` を経由** する（5-2節）。

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
（`server.py:202-231`）

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
（`server.py:193-199`）

相対パスはワークスペース基準で解決し、絶対パスもいったん `resolve()` して
シンボリックリンク経由の脱出も含めて正規化した上で、文字列前方一致で
ワークスペース外へのアクセスを拒否する。**ファイル操作系ツールがワークスペースの
外に触れられないようにする防波堤**がこの関数。

一方、`run_command` はこのサンドボックスの対象外（`cwd=ws` を渡すのみ）であり、
`cd ..` や絶対パス指定で任意の場所を触れてしまう。README/REBUILD.mdにも
「サンドボックスなし・ユーザー権限フル実行が設計方針」と明記されている通り、
ツール実行そのものに制限は設けていない。かわりに実際の防御線は3層になっている:

1. **`ThreadingHTTPServer` が `127.0.0.1` のみにバインド**（7節）— 他ホストから
   直接叩けない
2. **`_post_ok()` によるCSRFトークン検証**（2節）— 同一PC上の悪意あるWebページ
   からのno-cors POSTを拒否する（127.0.0.1バインドだけでは防げない攻撃面）
3. **ワークスペースを `$HOME` 配下に制限**（4節）— 万一①②を越えられても、
   操作対象のディレクトリ自体を広げられない

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
（`server.py:110-128`）

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
（`server.py:131-149`）

`script`/`style`/`svg`/`head` タグの中身は無視しつつ、テキストノードだけを
収集する。結果は1万文字で切り詰められる（`server.py:159-160`）。

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
（`server.py:480-487`）

`ThreadingHTTPServer` なので、複数タブ・複数セッションからの同時リクエストにも
スレッド単位で並行対応する。ポートが既に使用中（＝二重起動）の場合はエラーで
即終了するだけで、既存プロセスを奪ったり殺したりはしない
（README記載「サーバーが既に起動していれば二重起動しない」の実体）。

`("127.0.0.1", PORT)` へのバインドが第一の防御線だが、2節・5-2節で説明した通り
これ単体では同一PC上の悪意あるWebページからのCSRFを防げないため、`_post_ok()`
のトークン検証と組み合わせて初めて実用的な防御になっている。

---

## 8. 処理フロー図（概略）

```
ブラウザ(index.html)
   │  GET /  → HTMLにCSRFトークンを埋め込んで返す (window.LC_TOKEN)
   ▼
   │  POST /api/chat  { model, workspace, messages }  + X-LocalCoder-Token
   ▼
_post_ok()  ── NG → 403 forbidden で即終了
   │ OK
   ▼
handle_chat()
   │  workspace が $HOME 配下か検証 ── NG → エラーSSEで終了
   │  messages = [system] + body.messages
   ▼
┌─ for it in range(MAX_ITER) ──────────────────────────────┐
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
  `window.LC_TOKEN` を全POSTに付与し、Markdown描画時に `DOMPurify.sanitize()` を
  通すGUI側の実装
