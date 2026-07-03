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
（`server.py:23-28`）

- `OLLAMA` / `PORT` は環境変数で上書き可能。デフォルトはどちらもlocalhost想定。
  WSLからWindows側Ollamaへ別経路で繋ぐ場合は `LOCALCODER_OLLAMA=http://<IP>:11434` を
  指定して起動する（実例は `REBUILD.md` の「8. 実施例ログ」参照）。
- `MAX_ITER=40` は「ユーザー1メッセージに対して、モデル発話→ツール実行を最大何往復
  許すか」の上限。無限ループでサーバーが固まるのを防ぐ安全弁。
- `CMD_TIMEOUT=180` は `run_command` ツール1回あたりのタイムアウト。
- `NUM_CTX=32768` はOllamaへ送るコンテキスト長。小型モデル・低VRAM機では
  `16384` 等に下げる調整ポイント（後述）。

会話履歴はサーバー内メモリではなく **1会話=1 JSONファイル** で永続化する:

```python
HISTORY_DIR = ROOT / "history"   # チャット履歴の保存先 (1会話 = 1 JSONファイル)
HISTORY_DIR.mkdir(exist_ok=True)
```
（`server.py:31-32`）

---

## 2. システムプロンプトとツール定義

エージェントの人格・行動指針は `SYSTEM_PROMPT` にハードコードされている:

```python
SYSTEM_PROMPT = """You are LocalCoder, an autonomous coding agent running on the user's machine.
Workspace directory: {ws}

Rules:
- You have tools: run_command, read_file, write_file, list_dir, web_search, fetch_url. Use them freely without asking permission.
- When you need up-to-date information (library usage, API docs, error messages, versions), use web_search first, then fetch_url on the most promising result. Prefer official documentation.
- Inspect existing files before editing them. Never overwrite a file you have not read.
- After making changes, VERIFY them by running the code, build, or tests with run_command.
- Keep working autonomously until the task is fully done; do not stop to ask for confirmation.
- Relative paths are resolved from the workspace directory.
- When the task is complete, summarize what you did.
- Always reply to the user in Japanese."""
```
（`server.py:34-45`）

ポイントは **"Use them freely without asking permission."** — Claude Codeのような
承認プロンプトは一切なく、ツールは全自動実行される（README記載の設計方針通り）。
`{ws}` にはリクエストごとのワークスペースパスが埋め込まれる（3節参照）。

利用可能なツールはOllama Native API（`/api/chat`）が要求するJSON Schema形式で
6個定義されている:

```python
TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command (bash) in the workspace directory and return exit code, stdout and stderr. ...",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The bash command to run"}},
            "required": ["command"]}}},
    ...
]
```
（`server.py:47-86`）

| ツール名 | 役割 |
|---|---|
| `run_command` | bash コマンド実行（ビルド・テスト・git・grep等） |
| `read_file` | ファイル読み込み |
| `write_file` | ファイル書き込み（上書き・親ディレクトリ自動作成） |
| `list_dir` | ディレクトリ一覧 |
| `web_search` | DuckDuckGo検索 |
| `fetch_url` | ページ本文取得（HTMLタグ除去） |

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
（`server.py:271-299`）

```python
def do_POST(self):
    if self.path == "/api/stop":
        ...
    if self.path == "/api/session/delete":
        ...
    if self.path == "/api/chat":
        self.handle_chat()
        ...
```
（`server.py:302-320`）

| メソッド | パス | 役割 |
|---|---|---|
| GET | `/`, `/index.html` | GUI本体（`index.html`）を返す |
| GET | `/api/models` | Ollamaの `/api/tags` を中継してモデル一覧を返す |
| GET | `/api/sessions` | 保存済み会話一覧（`history/*.json`） |
| GET | `/api/session?sid=...` | 特定セッションの履歴を返す |
| GET | `/api/health` | 死活監視用（`{"ok": true}`） |
| POST | `/api/stop` | 実行中エージェントループの中断シグナル |
| POST | `/api/session/delete` | セッション削除 |
| POST | `/api/chat` | **本体**。ユーザー発話を受けてエージェントループを開始しSSEで応答 |

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
（`server.py:279-285`）

---

## 4. エージェントループの本体 — `handle_chat`

`/api/chat` にPOSTされると、レスポンスは即座に **SSE (Server-Sent Events)** に切り替わる:

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

    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(ws=ws)}]
    messages += body.get("messages", [])
```
（`server.py:322-336`）

ここで重要なのは **会話履歴をサーバーが保持しない** 設計。クライアント（`index.html`）が
毎回全履歴を `messages` として送り、サーバーはシステムプロンプトを先頭に足すだけの
ステートレス構成になっている（`REBUILD.md` の設計上の注意4と対応）。

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
（`server.py:339-357`）

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
（`server.py:229-237`）

チャンクごとに `thinking`（推論過程）・`content`（本文）・`tool_calls`（ツール呼び出し要求）
の3種が流れてきうる。それぞれをそのままブラウザへSSEで中継する
（`type: "think"` / `type: "token"`）ので、GUI側はモデルの思考過程を逐次表示できる。

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
    result = exec_tool(name, args, ws)
    self._sse({"type": "tool_end", "name": name,
               "result": result if len(result) <= 4000
               else result[:4000] + "\n...[truncated]..."})
    messages.append({"role": "tool", "tool_name": name,
                     "name": name, "content": result})
```
（`server.py:359-386`）

これが **エージェントループの心臓部**：
1. モデルの応答（`assistant` メッセージ）を履歴に追加
2. `tool_calls` が無ければ「タスク完了」とみなしループを抜ける
3. あれば `exec_tool()` で実際に実行し、結果を `role: "tool"` メッセージとして履歴に追加
4. `messages` が増えた状態で `for it in range(MAX_ITER)` の次周回に入り、
   再びOllamaに問い合わせる（モデルはツール結果を見て次の一手を判断する）

`tool_name` と `name` を両方入れているのは、Ollamaのバージョン間でtoolメッセージの
キー名が変わった経緯を吸収するため（`REBUILD.md` 設計上の注意2）。

`MAX_ITER` 回を超えてもツール呼び出しが続く場合はエラーを返す:

```python
else:
    self._sse({"type": "error",
               "message": f"最大ループ回数({MAX_ITER})に達しました"})
```
（`server.py:387-389`）

（この `else` は `for...else` 構文で、`break` されずにループが尽きた場合のみ実行される）

### 4-3. 終了処理

```python
self._sse({"type": "history", "messages": messages[1:]})
self._sse({"type": "all_done"})
```
（`server.py:392-393`）

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
（`server.py:406-412`）

---

## 5. ツール実行 — `exec_tool`

全ツールの実処理はこの1関数に集約されている:

```python
def exec_tool(name: str, args: dict, ws: Path) -> str:
    try:
        if name == "run_command":
            r = subprocess.run(["bash", "-lc", args["command"]], cwd=ws,
                               capture_output=True, text=True, timeout=CMD_TIMEOUT)
            out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
            if len(out) > 12000:
                out = out[:6000] + "\n...[truncated]...\n" + out[-6000:]
            return f"exit_code={r.returncode}\n{out}"
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
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out ({CMD_TIMEOUT}s)"
    except Exception as e:  # noqa: BLE001 - report all tool errors to the model
        return f"ERROR: {type(e).__name__}: {e}"
```
（`server.py:194-226`）

設計上の要点:

- **`run_command`** は `bash -lc <command>` として作業フォルダ（`cwd=ws`）で実行。
  出力が12KBを超えると先頭6KB＋末尾6KBに切り詰める（`server.py:200-201`）。
  小型ローカルモデルはコンテキストが溢れると応答が破綻しやすいための防御策。
- **例外は握りつぶさずモデルに返す**（`except Exception as e: return f"ERROR: ..."`）。
  これにより、例えば `python` コマンドが無い環境でエラーが返ると、モデルが
  `python3` に自分で切り替えて再試行する、といった自己回復的な挙動が生まれる
  （`REBUILD.md` の検証手順に記載の期待動作）。
- **read_file / write_file / list_dir はすべて `resolve_path()` を経由** する。

### 5-1. パスサンドボックス — `resolve_path`

```python
def resolve_path(ws: Path, p: str) -> Path:
    full = Path(p) if os.path.isabs(p) else ws / p
    full = full.resolve()
    ws = ws.resolve()
    if not (str(full) == str(ws) or str(full).startswith(str(ws) + os.sep)):
        raise ValueError(f"path is outside the workspace: {p}")
    return full
```
（`server.py:185-191`）

相対パスはワークスペース基準で解決し、絶対パスもいったん `resolve()` して
シンボリックリンク経由の脱出も含めて正規化した上で、文字列前方一致で
ワークスペース外へのアクセスを拒否する。**ファイル操作系ツールがワークスペースの
外に触れられないようにする唯一の防波堤**がこの関数。

一方、`run_command` はこのサンドボックスの対象外（`cwd=ws` を渡すのみ）であり、
`cd ..` や絶対パス指定で任意の場所を触れてしまう。README/REBUILD.mdにも
「サンドボックスなし・ユーザー権限フル実行が設計方針」と明記されている通り、
セキュリティ境界はあくまで **「127.0.0.1バインドで外部から到達不可であること」**
に置かれている（`server.py:417` の `ThreadingHTTPServer(("127.0.0.1", PORT), ...)`）。

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
（`server.py:102-120`）

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
（`server.py:123-141`）

`script`/`style`/`svg`/`head` タグの中身は無視しつつ、テキストノードだけを
収集する。結果は1万文字で切り詰められる（`server.py:151-152`）。

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
（`server.py:415-423`）

`ThreadingHTTPServer` なので、複数タブ・複数セッションからの同時リクエストにも
スレッド単位で並行対応する。ポートが既に使用中（＝二重起動）の場合はエラーで
即終了するだけで、既存プロセスを奪ったり殺したりはしない
（README記載「サーバーが既に起動していれば二重起動しない」の実体）。

---

## 8. 処理フロー図（概略）

```
ブラウザ(index.html)
   │  POST /api/chat  { model, workspace, messages }
   ▼
handle_chat()
   │  messages = [system] + body.messages
   ▼
┌─ for it in range(MAX_ITER) ──────────────────────────┐
│  ollama_stream(payload) → SSE: think/token を逐次転送 │
│  tool_calls が無ければ break                          │
│  あれば: exec_tool(name,args,ws) を各tool_callで実行   │
│         → SSE: tool_start / tool_end                  │
│         → messages に role:"tool" として結果を追加     │
└────────────────────────────────────────────────────────┘
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
  `tool_start`/`tool_end`/`turn_done`/`history`/`all_done`/`error`）を受け取る
  GUI側の実装
