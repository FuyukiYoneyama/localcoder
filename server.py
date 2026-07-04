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
HOME = Path.home().resolve()     # ワークスペースはこの配下のみ許可
# 画面初期表示時の作業フォルダ。個人の作業パスをリポジトリに埋め込まないよう
# 環境変数で指定する(未設定ならHOME)。index.html配信時にwindow変数として埋め込む。
DEFAULT_WORKSPACE = os.environ.get("LOCALCODER_DEFAULT_WORKSPACE", str(HOME))

SYSTEM_PROMPT = """You are LocalCoder, an autonomous coding agent running on the user's machine.
Workspace directory: {ws}

Rules:
- You have tools: run_command, read_file, write_file, edit_file, list_dir, web_search, fetch_url. Use them freely without asking permission.
- To change part of an existing file, prefer edit_file (exact find & replace) instead of rewriting the whole file with write_file. Use write_file only for new files or complete rewrites.
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


def under_home(p: Path) -> bool:
    p = p.resolve()
    return p == HOME or str(p).startswith(str(HOME) + os.sep)


def list_subdirs(path: str) -> dict:
    """作業フォルダ選択ダイアログ用。$HOME配下のサブディレクトリのみ一覧する。"""
    p = Path(path or DEFAULT_WORKSPACE).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = HOME
    if not p.is_dir() or not under_home(p):
        p = HOME
    dirs = sorted((e.name for e in p.iterdir()
                  if e.is_dir() and not e.name.startswith(".")),
                  key=str.lower)
    parent = str(p.parent) if p != HOME and under_home(p.parent) else None
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
                self._json({"models": [m["name"] for m in data.get("models", [])]})
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
                # 予算超過時は自動圧縮 (リクエスト開始時とツール結果肥大時の両方を守る)
                messages = compact_history(messages, model, self._sse)
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
