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

**方法A（推奨）: mirroredネットワーク** — `C:\Users\<user>\.wslconfig` に:

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
   ├─ GET  /              → index.html (チャットGUI)
   ├─ GET  /api/models    → Ollamaのモデル一覧を中継
   ├─ POST /api/chat      → エージェントループ (SSEでイベント配信)
   └─ POST /api/stop      → 実行中断
        │ /api/chat (streaming, tools付き)
        ▼
[Windows: Ollama :11434]       ← ローカルLLM本体
```

**エージェントループの動作**: ユーザー入力 → LLMに tools 付きで問い合わせ →
LLMがツール呼び出しを返したら承認なしで即実行 → 結果を履歴に追加して再問い合わせ →
ツール呼び出しがなくなる（=タスク完了）まで最大40回繰り返す。

**ツール4種**: `run_command`（bash実行, cwd=作業フォルダ, 180秒タイムアウト）/
`read_file` / `write_file` / `list_dir`（ファイル系は作業フォルダ外へのアクセスを拒否）

**SSEイベントプロトコル** (`data: {json}\n\n` 形式):
`think`(思考トークン) / `token`(本文トークン) / `turn_done`(1応答完了) /
`tool_start`,`tool_end`(ツール実行) / `history`(全会話履歴→クライアントが保持し次回送信) /
`all_done` / `error`

**会話状態はクライアント側が保持**（サーバーはステートレス）。`history` イベントで
tool呼び出し含む完全履歴を返し、次のPOSTでそのまま送り返す方式。

---

## 3. ファイル一式

以下3ファイルを作成する。①②はWSL内 `~/localcoder/`、③はWindowsのデスクトップ。

### ① `~/localcoder/server.py`

```python
#!/usr/bin/env python3
"""LocalCoder — ローカルLLM(Ollama)で動くGUIコーディングエージェント。

依存ライブラリなし(Python標準ライブラリのみ)。
Windows側 Ollama (localhost:11434) に接続し、ツール(ファイル読み書き・
コマンド実行)を全自動で実行するエージェントループを提供する。
ブラウザで http://localhost:8765 を開いて使う。
"""
import json
import os
import subprocess
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OLLAMA = os.environ.get("LOCALCODER_OLLAMA", "http://localhost:11434")
PORT = int(os.environ.get("LOCALCODER_PORT", "8765"))
ROOT = Path(__file__).resolve().parent
MAX_ITER = 40          # 1リクエストあたりの最大ツールループ回数
CMD_TIMEOUT = 180      # コマンド実行タイムアウト(秒)
NUM_CTX = 32768

CANCEL = {}            # sid -> threading.Event

SYSTEM_PROMPT = """You are LocalCoder, an autonomous coding agent running on the user's machine.
Workspace directory: {ws}

Rules:
- You have tools: run_command, read_file, write_file, list_dir. Use them freely without asking permission.
- Inspect existing files before editing them. Never overwrite a file you have not read.
- After making changes, VERIFY them by running the code, build, or tests with run_command.
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
        "name": "list_dir",
        "description": "List files and directories at a path.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path, default is workspace root"}},
            "required": []}}},
]


def resolve_path(ws: Path, p: str) -> Path:
    full = Path(p) if os.path.isabs(p) else ws / p
    full = full.resolve()
    ws = ws.resolve()
    if not (str(full) == str(ws) or str(full).startswith(str(ws) + os.sep)):
        raise ValueError(f"path is outside the workspace: {p}")
    return full


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
        return f"ERROR: unknown tool {name}"
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out ({CMD_TIMEOUT}s)"
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

    # ---------- GET ----------
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/models":
            try:
                with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as r:
                    data = json.loads(r.read())
                self._json({"models": [m["name"] for m in data.get("models", [])]})
            except Exception as e:
                self._json({"error": f"Ollamaに接続できません: {e}"}, 502)
        elif self.path == "/api/health":
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    # ---------- POST ----------
    def do_POST(self):
        if self.path == "/api/stop":
            body = self._read_body()
            ev = CANCEL.get(body.get("sid", ""))
            if ev:
                ev.set()
            self._json({"ok": True})
            return
        if self.path == "/api/chat":
            self.handle_chat()
            return
        self._json({"error": "not found"}, 404)

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

        try:
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
            else:
                self._sse({"type": "error",
                           "message": f"最大ループ回数({MAX_ITER})に達しました"})

            # システムプロンプトを除いた全履歴を返す(次ターンで文脈維持)
            self._sse({"type": "history", "messages": messages[1:]})
            self._sse({"type": "all_done"})
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected
        except urllib.error.URLError as e:
            try:
                self._sse({"type": "error", "message": f"Ollama接続エラー: {e}"})
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            try:
                self._sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
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
.err{align-self:center;color:var(--red);font-size:13px}
footer{display:flex;gap:10px;padding:12px 16px;background:var(--panel);border-top:1px solid var(--border)}
#input{flex:1;background:var(--panel2);color:var(--text);border:1px solid var(--border);
  border-radius:8px;padding:10px;font-size:14px;resize:none;font-family:inherit;min-height:44px;max-height:200px}
#sendBtn{background:var(--accent);color:#fff;border:none;padding:0 22px;font-weight:600}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--dim);
  border-top-color:var(--accent);border-radius:50%;animation:sp 1s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<header>
  <h1>🛠 LocalCoder</h1>
  <label>モデル <select id="model"></select></label>
  <label>作業フォルダ <input type="text" id="workspace" value="/home/fuyuki/pico_dvl/codex"></label>
  <button id="newBtn">🗑 新規チャット</button>
  <button id="stopBtn">■ 停止</button>
  <span id="status"></span>
</header>
<div id="chat"></div>
<footer>
  <textarea id="input" placeholder="やりたいことを日本語で入力 (Shift+Enterで改行 / Enterで送信)"></textarea>
  <button id="sendBtn">送信</button>
</footer>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
const $=id=>document.getElementById(id);
const chat=$("chat"), input=$("input"), status=$("status");
const sid=Math.random().toString(36).slice(2);
let history=[];      // サーバに渡す完全な会話履歴(tool呼び出し含む)
let running=false;

function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;chat.appendChild(e);scroll();return e}
function scroll(){chat.scrollTop=chat.scrollHeight}
function md(e,text){
  if(window.marked){e.classList.add("md");e.innerHTML=marked.parse(text)}
  else e.textContent=text;
}
function setRunning(v){
  running=v;
  $("sendBtn").disabled=v;
  $("stopBtn").style.display=v?"":"none";
  status.innerHTML=v?'<span class="spin"></span> 実行中…':"";
}

async function loadModels(){
  try{
    const r=await fetch("/api/models"); const d=await r.json();
    if(d.error){el("div","err",d.error);return}
    const sel=$("model"); sel.innerHTML="";
    for(const m of d.models){const o=document.createElement("option");o.value=o.textContent=m;sel.appendChild(o)}
    const pref=["gpt-oss:20b","glm-4.7-flash:latest","qwen3:8b"];
    for(const p of pref){if(d.models.includes(p)){sel.value=p;break}}
  }catch(e){el("div","err","サーバに接続できません: "+e)}
}

async function send(){
  const text=input.value.trim();
  if(!text||running)return;
  input.value="";
  el("div","msg user",text);
  history.push({role:"user",content:text});
  setRunning(true);

  let curAssistant=null, curThink=null, curText="";
  try{
    const resp=await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({sid,model:$("model").value,workspace:$("workspace").value,messages:history})});
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
    }else if(ev.type==="tool_start"){
      const d=document.createElement("details"); d.className="tool"; d.dataset.name=ev.name;
      const argstr=ev.name==="run_command"?(ev.args.command||""):(ev.args.path||JSON.stringify(ev.args));
      d.innerHTML="<summary>🔧 "+ev.name+" — <code></code> <span class='spin'></span></summary><pre>実行中…</pre>";
      d.querySelector("code").textContent=argstr.slice(0,120);
      chat.appendChild(d); scroll();
    }else if(ev.type==="tool_end"){
      const tools=chat.querySelectorAll("details.tool");
      const d=tools[tools.length-1];
      if(d){d.querySelector(".spin")?.remove(); d.querySelector("pre").textContent=ev.result}
      scroll();
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
$("stopBtn").onclick=()=>fetch("/api/stop",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({sid})});
$("newBtn").onclick=()=>{history=[];chat.innerHTML="";};
loadModels();
</script>
</body>
</html>
```

### ③ デスクトップの `LocalCoder.bat`（Windows側）

```bat
@echo off
rem LocalCoder — ローカルLLM(Ollama)コーディングエージェント起動
rem サーバーが既に起動していれば二重起動しない(server.py側で処理)
start "LocalCoder Server" /min wsl -d ubuntu-24.04 -- bash -lc "python3 ~/localcoder/server.py"
ping -n 3 127.0.0.1 >nul
start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app=http://localhost:8765/
```

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

# 3. エージェントのエンドツーエンドテスト
mkdir -p /tmp/lc_test
cat > /tmp/lc_req.json <<'EOF'
{"sid":"test1","model":"gpt-oss:20b","workspace":"/tmp/lc_test",
 "messages":[{"role":"user","content":"hello.py というファイルを作って hello world と出力するようにして、実行して確認して"}]}
EOF
curl -s -N -X POST http://localhost:8765/api/chat \
  -H 'Content-Type: application/json' --data @/tmp/lc_req.json | tail -5
cat /tmp/lc_test/hello.py   # → print("hello world") ができていれば合格
```

期待される挙動: SSEで `tool_start`(write_file) → `tool_end` → `tool_start`(run_command) →
… → `all_done` が流れ、hello.py が実際に作成・実行される。
（初回はモデルロードで1〜3分かかる。`python`が無ければ`python3`に自動で切り替えるなど、
エラー自己回復が観察できれば完璧）

最後に LocalCoder.bat をダブルクリックし、GUIウィンドウが開いてモデル一覧が
表示されることを確認する。

---

## 5. 環境差分の調整（移植先で変わる箇所）

| 箇所 | このPCでの値 | 移植先での調整方法 |
|---|---|---|
| WSLディストロ名 | `ubuntu-24.04` | `wsl -l -v` で確認し bat の `-d` を変更 |
| デスクトップパス | `E:\desktop`（移動済み） | 通常は `%USERPROFILE%\Desktop`。PowerShellの `[Environment]::GetFolderPath('Desktop')` で確認 |
| index.html の作業フォルダ初期値 | `/home/fuyuki/pico_dvl/codex` | `#workspace` の `value=` を移植先のホームに変更 |
| Edgeのパス | `C:\Program Files (x86)\Microsoft\Edge\...` | 無ければ bat 最終行を `start "" http://localhost:8765/` に |
| Ollamaの場所 | Windows側 localhost:11434 | WSL内Ollamaでも同URLで可。別ホストなら環境変数 `LOCALCODER_OLLAMA` |
| ポート | 8765 | 競合時は環境変数 `LOCALCODER_PORT` |

## 6. カスタマイズポイント

- **ツール追加**: `TOOLS` にJSONスキーマを1個追加し、`exec_tool()` に分岐を1個追加するだけ
- **ループ上限**: `MAX_ITER = 40`、コマンドタイムアウト: `CMD_TIMEOUT = 180`
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
   全自動設計）。127.0.0.1バインドなので外部からはアクセス不可。LAN公開は不可。

---

## 付録: 周辺環境（LocalCoderとは独立だが同時に構築したもの）

- **codex CLI 0.142.5**（WSL `~/.local/bin/codex`、`npm install -g --prefix ~/.local @openai/codex`）
  `~/.codex/config.toml` で組み込み `ollama` プロバイダ + `approval_policy="never"` +
  `sandbox_mode="workspace-write"`。注意: codex 0.142以降は `wire_api="chat"` 廃止、
  カスタム `[model_providers.ollama]` 定義も禁止（組み込みを使う）。
- **aider 0.86.2**（`~/.aider.conf.yml`: `model: ollama_chat/gpt-oss:20b`, `yes-always: true`,
  `set-env: [OLLAMA_API_BASE=http://localhost:11434]`）
