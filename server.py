#!/usr/bin/env python3
"""LocalCoder — ローカルLLM(Ollama)で動くGUIコーディングエージェント。

依存ライブラリなし(Python標準ライブラリのみ)。
Windows側 Ollama (localhost:11434) に接続し、ツール(ファイル読み書き・
コマンド実行)を全自動で実行するエージェントループを提供する。
ブラウザで http://localhost:8765 を開いて使う。
"""
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
MAX_ITER = 40          # 1リクエストあたりの最大ツールループ回数
CMD_TIMEOUT = 180      # コマンド実行タイムアウト(秒)
NUM_CTX = 32768

CANCEL = {}            # sid -> threading.Event
HISTORY_DIR = ROOT / "history"   # チャット履歴の保存先 (1会話 = 1 JSONファイル)
HISTORY_DIR.mkdir(exist_ok=True)

# CSRF対策: 起動ごとのランダムトークン。index.html配信時に埋め込み、
# 全POST APIで X-LocalCoder-Token ヘッダとして要求する。
# 外部サイトからの no-cors POST はこの値を知り得ないため全て拒否される。
TOKEN = secrets.token_hex(16)
HOME = Path.home().resolve()     # ワークスペースはこの配下のみ許可

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


def _safe_sid(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", sid)[:40] or "default"


def save_session(sid: str, model: str, workspace: str, messages: list):
    sid = _safe_sid(sid)
    title = next((m["content"] for m in messages
                  if m.get("role") == "user" and m.get("content")), "(無題)")
    data = {"sid": sid, "title": title[:60], "updated_at": time.time(),
            "model": model, "workspace": workspace, "messages": messages}
    (HISTORY_DIR / f"{sid}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


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
            body = body.replace(
                b"</head>",
                b'<script>window.LC_TOKEN="' + TOKEN.encode() + b'";</script></head>', 1)
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
                self._json({"models": [m["name"] for m in data.get("models", [])]})
            except Exception as e:
                self._json({"error": f"Ollamaに接続できません: {e}"}, 502)
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
                    result = exec_tool(name, args, ws, ev)
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
        finally:
            # 会話を自動保存 (エラーや途中停止でもそこまでの内容を残す)
            if len(messages) > 1:
                try:
                    save_session(sid, model, str(ws), messages[1:])
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
