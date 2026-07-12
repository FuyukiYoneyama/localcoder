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
TOOL_STUCK_LIMIT = 3   # 同じツール呼び出し(名前+引数)が同じエラーで連続失敗した回数の上限。
                       # 超えたら進展が見込めないと判断しMAX_ITERを待たずループを打ち切る
TOOL_NAME_TOKEN_RE = re.compile(r"<\|[^|]*\|>")  # モデルが漏らす特殊トークンの除去用
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
COMPACT_TARGET_RATIO = 0.6  # 圧縮後に目指すサイズ(予算比)。予算超過のたびに天井
                            # ギリギリまでしか下げないと、数イテレーションごとに圧縮が
                            # 再発し、履歴前方の書き換えでollamaのプロンプトキャッシュも
                            # 毎回無効化されて激遅になる。一度超過したら大きく下げる
PROACTIVE_COMPACT_RATIO = 0.9  # 予算に対してこの割合を超えたら、まだ超過していなくても
                               # 先回りで圧縮する。天井ギリギリ(実測で予算の99%)まで
                               # 会話を伸ばした状態でモデルに応答を続けさせると、生成用に
                               # 確保したRESERVE_TOKENSの余白がほぼ無くなり、本文もツール
                               # 呼び出しも無い"空応答"を返して進まなくなる実例があった
DEDUPE_MIN_CHARS = 200  # この文字数を超える同一内容のツール結果だけ重複除去の対象にする
KEEP_RECENT_MSGS = 6    # 要約時に原文のまま残す直近メッセージ数
KEEP_RECENT_TOOLS = 4   # 切り詰めずに残す直近のツール結果数
TOOL_TRIM_CHARS = 500   # 古いツール結果の切り詰め後サイズ
MSG_EXCERPT_CHARS = 1000            # 要約入力で1メッセージから取る最大文字数
SUMMARIZE_INPUT_TOKENS = NUM_CTX // 2  # 要約1回の入力上限 (超えたら分割要約)
MARKER_SUMMARY = "【自動要約】"    # 圧縮マーカー: 要約に成功した場合
MARKER_OMIT = "【自動省略】"       # 圧縮マーカー: 要約に失敗し何も引き継げなかった場合
SUMMARY_BODY_SEP = "----\n"        # マーカーの説明文と要約本文の区切り
FILES_SECTION_HEADER = "\n--- 変更ファイル一覧(自動検出・要約対象外) ---\n"

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
    except KeyError as e:
        # 弱いローカルモデルほど引数を一部欠落させたツール呼び出しを返しがちなので、
        # 生のKeyErrorよりモデルが自己修正しやすい具体的な指示にする。
        return (f"ERROR: missing required argument {e} for tool '{name}'. "
                f"Call {name} again with all required arguments included.")
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


def dedupe_tool_results(messages: list) -> bool:
    """同一内容のツール結果の重複を除去する(最新の1件だけ残す。安価・LLM不使用)。

    弱いモデルは同じファイルを何度も読み直すことがあり、実測で履歴の半分が
    同一内容のツール結果だったセッションがあった(read_fileの同一結果×15など)。
    同じ内容なら最新の呼び出しの結果だけあれば十分で、古い方は短い参照に置き換える。
    """
    last_idx = {}   # content -> そのcontentを持つ最後のtoolメッセージindex
    for i, m in enumerate(messages):
        if m.get("role") == "tool":
            c = m.get("content") or ""
            if len(c) > DEDUPE_MIN_CHARS:
                last_idx[c] = i
    changed = False
    for i, m in enumerate(messages):
        if m.get("role") == "tool":
            c = m.get("content") or ""
            if len(c) > DEDUPE_MIN_CHARS and last_idx.get(c, i) > i:
                messages[i]["content"] = ("(同一内容の結果が後の呼び出しで再取得"
                                          "されているため省略。最新の結果を参照)")
                changed = True
    return changed


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


UPDATE_SUMMARIZE_PROMPT = """以下は「これまでの会話の要約」と「その後の新しい会話ログ」である。
両方の情報を過不足なく統合し、今後の作業を継続するために必要な情報を日本語で簡潔にまとめよ。
必ず守ること:
- 「これまでの要約」に含まれる事実(ファイルパス・ユーザーの指示・決定事項)は、明確に古くなった/
  上書きされた場合を除き、失わずに引き継ぐこと
- ユーザーの目的・指示・好み (「覚えておいて」と言われた事項) は一字一句そのまま残すこと
- 「新しい会話ログ」で判明した内容(変更ファイル・技術的事実・完了/未完了)を追加すること
- 短くまとめ直そうとして事実を削らないこと。多少冗長でも欠落より安全を優先せよ
出力は統合後の要約本文のみ。前置きや締めの文は不要。

--- これまでの要約 ---
{prev}
--- これまでの要約ここまで ---

--- 新しい会話ログ ---
{log}
--- ログここまで ---

上記を統合し、更新後の要約を日本語で出力せよ。出力は要約本文のみ。"""


def _chunk_messages(old: list) -> list:
    """メッセージ群をSUMMARIZE_INPUT_TOKENS以下のチャンク列に分割する。

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
    return chunks


def update_summary(prev: str, new_raw: list, model: str) -> str:
    """既存の要約(prev)に新規分の生ログを直接統合する(通常はLLM呼び出し1回)。

    フルの再要約と違い、prevはLLMに「引き継ぐべき既存事実」として提示するだけで
    元の生ログには戻らない。これにより圧縮を繰り返すたびに要約を要約し直す
    (伝言ゲーム的に内容が薄まる)ことを避け、既存事実の維持を明示的に指示できる。
    以前は「新規分を要約→既存要約とマージ」の2回呼び出しだったが、遅いローカル
    ハードでは圧縮1回の停止時間が倍になるため1回に統合した。新規分が上限を超える
    場合のみチャンクごとに逐次統合する。
    """
    cur = prev
    for ch in _chunk_messages(new_raw):
        merged = ollama_ask(model, UPDATE_SUMMARIZE_PROMPT.format(
            prev=cur, log=render_transcript(ch))).strip()
        if merged:
            cur = merged
    return cur


def summarize_old(old: list, model: str) -> str:
    """古いメッセージ群を要約する。入力が要約1回の上限を超える場合は分割して各々要約。"""
    parts = []
    for ch in _chunk_messages(old):
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


def extract_changed_files(messages: list) -> list[str]:
    """write_file/edit_fileツール呼び出しから変更ファイルパスを機械的に抽出する
    (発生順・重複除去、LLM不使用)。圧縮時にLLM要約とは別ルートで確実に引き継ぐために使う。

    結果が"ERROR"で始まる(=実際には書き込みに失敗した)呼び出しは含めない。呼び出しが
    あっただけで成功とみなすと、モデルに「ファイルは変更済み」という誤った情報を
    与えてしまい、実際には一度も書き込みに成功していないのに完了したと誤認する
    (ダッシュボード経由でのハルシネーションを誘発する)ことが実際に確認された。
    """
    files = []
    for name, args, result in _iter_tool_calls_with_results(messages):
        if name in ("write_file", "edit_file") and result is not None and not result.startswith("ERROR"):
            path = args.get("path")
            if path and path not in files:
                files.append(path)
    return files


def _parse_marker(content: str) -> tuple[str | None, list[str]]:
    """圧縮マーカーのcontentから (これまでの要約本文, 変更ファイル一覧) を取り出す。

    マーカーでなければ (None, []) を返す。ファイル一覧は要約本文とは別ブロックに
    保存されているため、LLMによる再要約でパラフレーズされず正確な値のまま読み戻せる。
    """
    if not (content.startswith(MARKER_SUMMARY) or content.startswith(MARKER_OMIT)):
        return None, []
    if SUMMARY_BODY_SEP not in content:
        return None, []
    _, _, rest = content.partition(SUMMARY_BODY_SEP)
    if FILES_SECTION_HEADER in rest:
        summary_part, _, files_part = rest.partition(FILES_SECTION_HEADER)
        files = [line[2:] for line in files_part.splitlines() if line.startswith("- ")]
    else:
        summary_part, files = rest, []
    return summary_part.strip(), files


def build_marker(summary_text: str, files: list[str], failed: bool = False) -> str:
    """圧縮結果メッセージのcontentを組み立てる。

    ファイル一覧はLLM要約の本文に混ぜず別ブロックとして常に付記する。機械的に
    検出した事実なので、要約の質に関わらず正確な値のまま次回以降も引き継がれる。
    """
    if failed:
        prefix, desc = MARKER_OMIT, "以前の会話は長すぎたため省略された。必要な情報は改めて確認すること。"
    else:
        prefix, desc = MARKER_SUMMARY, "ここまでの会話が長くなったため、古い部分は以下の要約に置き換えられた:"
    body = f"{prefix}{desc}\n{SUMMARY_BODY_SEP}{summary_text.strip()}"
    if files:
        body += FILES_SECTION_HEADER + "\n".join(f"- {f}" for f in files)
    return body


def build_work_state(messages: list) -> str:
    """会話履歴(system除く)から、変更ファイル一覧・直近コマンド結果・繰り返し失敗を
    機械的に(LLMを使わず)抽出して短いダッシュボード文字列にする。空なら""を返す。

    圧縮済み(compact_history で要約済み)の古い部分はtool_calls構造が失われているため
    対象外——直近の非圧縮ウィンドウのみを反映する。古い部分の変更ファイルは
    compact_history側でextract_changed_filesにより圧縮マーカーに機械的に引き継がれる。
    """
    changed_files = extract_changed_files(messages)
    commands = []  # (command, result_or_None) を発生順に
    all_calls = []  # 全ツール呼び出しの署名(名前+引数)を発生順に
    for name, args, result in _iter_tool_calls_with_results(messages):
        all_calls.append((name, json.dumps(args, sort_keys=True, ensure_ascii=False)))
        if name == "run_command":
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

    # 成功していても同じ呼び出し(同名+同引数)の繰り返しは進展がない。弱いモデルが
    # 同じファイルを延々と読み直して履歴とループ回数を浪費する実例があったため警告する。
    if len(all_calls) >= FAIL_REPEAT_THRESHOLD:
        tail_calls = all_calls[-FAIL_REPEAT_THRESHOLD:]
        if len(set(tail_calls)) == 1:
            lines.append(
                f"⚠ 同じツール呼び出し「{tail_calls[-1][0]}」を同じ引数で直近"
                f"{FAIL_REPEAT_THRESHOLD}回連続で繰り返しています。結果は既に"
                "得られています。同じ呼び出しを繰り返さず、次の作業"
                "(ファイルの作成・編集・ビルドなど)に進んでください。")

    return "\n".join(lines)


def compact_history(messages: list, model: str, sse, force: bool = False) -> list:
    """messages(先頭はsystem)が予算に近づいていたら圧縮して返す。近づいていなければそのまま。

    第0段階: 同一内容のツール結果の重複除去 (安価・LLM不使用)
    第1段階: 古いツール結果の切り詰め (安価・LLM不使用)
    第2段階: 直近KEEP_RECENT_MSGS件を残して古い部分をLLMで要約し1メッセージに置換。
             古い部分の先頭に前回の圧縮マーカーが残っていれば、それを生ログとして
             再要約せず「これまでの要約」として引き継ぎ、新規分の生ログを1回の
             LLM呼び出しで直接統合する(世代劣化の防止)。変更ファイル一覧はLLMを
             介さず機械抽出して常にマーカーに引き継ぐ。
    要約失敗時: 既存の要約とファイル一覧があればそれだけは保持し、新規分のみ
               省略する (全損させない)。

    ヒステリシス: 発動判定は予算(budget)そのものではなく、より手前の
    trigger(=budget×PROACTIVE_COMPACT_RATIO)。実測で、会話が予算の99%まで
    伸びた状態(=正式な超過はしていない)でモデルが本文もツール呼び出しも無い
    "空応答"を繰り返し、生成用に確保したRESERVE_TOKENSの余白をほぼ使い切って
    何も produce できなくなるセッションがあった。予算に達してから動くのでは
    手遅れなので、天井の手前で先回りして縮める。発動したら目標(target=
    予算×COMPACT_TARGET_RATIO)まで一気に下げる。天井ギリギリで止めると数
    イテレーションごとに圧縮が再発し、しかも安価な切り詰めでも履歴前方の
    書き換えでollamaのプロンプトキャッシュが毎回無効化され、全プロンプトの
    再処理(CPUオフロード時は数分)が発生して実質作業が止まる実例もあったため。

    force=Trueの場合はtriggerを無視して必ず第0段階から実行する。空応答で
    自動リトライする直前に呼び、リトライ前に強制的に文脈を減らすために使う
    (リトライしても文脈がほぼ変わらなければ同じ壁にまた当たるだけなので)。
    """
    budget = NUM_CTX - RESERVE_TOKENS
    trigger = int(budget * PROACTIVE_COMPACT_RATIO)
    target = int(budget * COMPACT_TARGET_RATIO)
    est = estimate_tokens(messages)
    if not force and est <= trigger:
        return messages

    dedupe_tool_results(messages)
    trim_old_tool_results(messages)
    est2 = estimate_tokens(messages)
    if est2 <= target:
        sse({"type": "compact",
             "message": f"重複・古いツール結果を整理しました (推定 {est}→{est2} トークン)"})
        return messages

    body = messages[1:]
    split = len(body) - KEEP_RECENT_MSGS
    # toolメッセージは直前のassistant(tool_calls)とペアなので、境界がtoolなら手前へずらす
    while split > 0 and body[split].get("role") == "tool":
        split -= 1
    if split <= 0:
        return messages  # 直近メッセージだけで予算超過。これ以上は縮められない
    old, recent = body[:split], body[split:]

    prev_summary, prev_files = _parse_marker(old[0].get("content", ""))
    new_raw = old[1:] if prev_summary is not None else old
    new_files = extract_changed_files(new_raw)
    all_files = prev_files + [f for f in new_files if f not in prev_files]

    sse({"type": "compact", "message": "履歴が長いため古い部分を要約しています…"})
    try:
        if new_raw and prev_summary:
            summary_text = update_summary(prev_summary, new_raw, model)
        elif new_raw:
            summary_text = summarize_old(new_raw, model)
        else:
            summary_text = prev_summary or ""
        marker = build_marker(summary_text, all_files)
    except Exception as e:
        # 新規分の要約に失敗しても、既存の要約(あれば)とファイル一覧は機械的に
        # 保持する。全て消すより情報を残す方が安全。
        note = f"(直近の新規会話部分の要約に失敗したため未反映: {type(e).__name__})"
        summary_text = f"{prev_summary}\n{note}" if prev_summary else note
        marker = build_marker(summary_text, all_files, failed=not prev_summary)
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
        last_failed_sig = None  # 直前の失敗ツール呼び出し(名前+引数)の署名。連続失敗検出用
        tool_repeat = 0
        stuck = False

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
                        #
                        # 「続けてください」を足すだけでは文脈量がほぼ変わらず、予算の
                        # 天井付近で空応答になった場合は同じ壁に再度当たるだけなので、
                        # リトライ前に強制的に圧縮して実際に余白を作る(force=True)。
                        empty_retries += 1
                        messages = compact_history(messages, model, self._sse, force=True)
                        self._sse({"type": "notice",
                                   "message": "モデルが空の応答を返したため、文脈を圧縮してから"
                                              "続行を促しています…"})
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
                    raw_name = fn.get("name", "?")
                    # 一部のモデル(実例: laguna-xs-2.1)はツール呼び出しのname欄に
                    # <|tool_call_argument_begin|>のような内部特殊トークンを漏らして
                    # 返すことがあり、完全一致ディスパッチが永遠に失敗し続ける
                    # (=ループを自己修正できないまま浪費する)原因になっていた。
                    # 生ログにも正規化後の名前を残しておく(fnは元のtool_calls/messages
                    # と同一オブジェクトを参照しているため、ここでの変更が保存履歴にも反映される)。
                    name = TOOL_NAME_TOKEN_RE.sub("", raw_name).strip()
                    if name != raw_name:
                        fn["name"] = name
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
                    # 同じツール呼び出し(名前+引数)が同じ結果でTOOL_STUCK_LIMIT回連続
                    # 失敗したら、進展が見込めないと判断してMAX_ITERを待たず打ち切る
                    # (ツール名破損やモデルの引数生成不良で80回丸ごと浪費するのを防ぐ)。
                    sig = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                    if result.startswith("ERROR") and sig == last_failed_sig:
                        tool_repeat += 1
                    else:
                        tool_repeat = 0
                    last_failed_sig = sig if result.startswith("ERROR") else None
                    if tool_repeat >= TOOL_STUCK_LIMIT:
                        stuck = True
                        break
                if pending_images:
                    # view_imageで読み込んだ画像は、tool結果(テキストのみ)とは別に
                    # 合成のuserメッセージとして差し込み、次のOllama呼び出しで
                    # visionモデルに実際に見せる。ライブ表示用にSSEでも個別に送る。
                    messages.append({"role": "user",
                                     "content": "(view_imageで読み込んだ画像)",
                                     "images": pending_images})
                    for b64 in pending_images:
                        self._sse({"type": "image", "b64": b64})
                if stuck:
                    turn_status = "stuck"
                    self._sse({"type": "error",
                               "message": f"同じツール呼び出し「{name}」が同じ引数・同じ"
                                          f"エラーで{TOOL_STUCK_LIMIT}回連続失敗したため、"
                                          "これ以上繰り返さず停止しました。モデルの出力形式"
                                          "(ツール名や引数)に問題がある可能性があります。"})
                    break
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
