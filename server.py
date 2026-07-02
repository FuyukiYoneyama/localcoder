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
