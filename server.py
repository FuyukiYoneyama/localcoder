#!/usr/bin/env python3
"""LocalCoder — ローカルLLM(Ollama)で動くGUIコーディングエージェント。

依存ライブラリなし(Python標準ライブラリのみ)。
Windows側 Ollama (localhost:11434) に接続し、ツール(ファイル読み書き・
コマンド実行)を全自動で実行するエージェントループを提供する。
ブラウザで http://localhost:8765 を開いて使う。
"""
from __future__ import annotations  # `dict | None` 等の新型ヒント構文をPython 3.8/3.9でも使えるようにする

import base64
import hashlib
import json
import os
import platform
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from pathlib import Path

OLLAMA = os.environ.get("LOCALCODER_OLLAMA", "http://localhost:11434")
PORT = int(os.environ.get("LOCALCODER_PORT", "8765"))
ROOT = Path(__file__).resolve().parent


def _detect_version() -> str:
    """起動中のserver.pyが指すgitコミット(先頭7桁)を求める。

    「再起動したのに直したはずのバグが直っていない」は、直した後に再起動を
    忘れて古いプロセスのまま動かし続けていたことが実際に原因だった。画面上に
    今動いているコミットを出しておけば、`git log`の最新コミットと見比べるだけで
    最新版が動いているか一目で分かる。取得できない場合は"unknown"。
    """
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short=7", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        h = out.stdout.strip()
        if out.returncode != 0 or not h:
            return "unknown"
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return h + ("+dirty" if dirty else "")
    except Exception:
        return "unknown"


SERVER_VERSION = _detect_version()
MAX_ITER = 80          # 1リクエストあたりの最大ツールループ回数
TOOL_STUCK_LIMIT = 3   # 同じツール呼び出し(名前+引数)が同じエラーで連続失敗した回数の上限。
                       # 超えたら進展が見込めないと判断しMAX_ITERを待たずループを打ち切る
TOOL_NAME_TOKEN_RE = re.compile(r"<\|[^|]*\|>")  # モデルが漏らす特殊トークンの除去用
EMPTY_RETRY_LIMIT = 1  # モデルが本文なし・ツール呼び出しなしで終える"空応答"時、
                       # 自動で続行を促す回数の上限 (それでも空ならユーザーに通知して停止)
EMPTY_RESPONSE_NUDGE = ("(システム自動継続) 直前の応答が空でした。作業が完了して"
                        "いるなら結果を要約し、未完了ならツールを使って作業を"
                        "続けてください。")
UNFINISHED_RETRY_LIMIT = 1  # ツール呼び出し無し・本文ありだが未検証の変更が残った
                            # まま終えようとした時、自動で続行を促す回数の上限
UNFINISHED_RESPONSE_NUDGE = ("(システム自動継続) 未検証の変更が残ったままツール"
                             "呼び出し無しで終えようとしました。run_commandで"
                             "ビルド/テストを実行して検証するか、作業を最後まで"
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

# --- MCPクライアント (IMPROVEMENTS.md §13 / 第6段階) ---
MCP_CONFIG_PATH = ROOT / "mcp_servers.json"  # サーバー定義(個人パスを含むためgitignore)
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_INIT_TIMEOUT = 30    # initialize/tools/listの応答待ち上限(秒)
MCP_CALL_TIMEOUT = 120   # tools/callの応答待ち上限(秒)。索引の初回構築等を見込む
MCP_RETRY_INTERVAL = 60  # 起動に失敗したサーバーへ再起動を試みる最短間隔(秒)。
                         # これが無いと、壊れた設定のまま毎リクエストで起動失敗の
                         # 待ち時間が発生してチャット全体が遅くなる

# --- 自動方針再評価パス (METACOGNITIVE_REPLANNING.md 第1〜2段階) ---
# 「考え直せ」と割り込むのではなく、現在の方針が引き続き妥当かをツールなしの
# 専用LLM呼び出しで検査し、CONTINUE/ADJUST/CHANGE/STOPを明示判定させる。
# CONTINUEも「期限付きの継続許可」という情報価値のある判定として扱う。
STRATEGY_REVIEW_ENABLED = os.environ.get(
    "LOCALCODER_STRATEGY_REVIEW", "on").lower() not in ("off", "0", "false")
REVIEW_SCORE_THRESHOLD = int(os.environ.get(
    "LOCALCODER_REVIEW_SCORE_THRESHOLD", "4"))   # この合計点以上で発火
REVIEW_AFTER_TOOL_CALLS = int(os.environ.get(
    "LOCALCODER_REVIEW_AFTER_TOOL_CALLS", "12"))  # 前回評価からのツール数(+2点)
REVIEW_NO_PROGRESS_TOOLS = 8    # 進捗イベントなしのツール数(+2点)
REVIEW_MIN_INTERVAL_TOOLS = 6   # 再発火までに空ける最小ツール数(期限到達は除く)
REVIEW_MAX_PER_TURN = 3         # 1ターン(1リクエスト)の最大再評価回数
REVIEW_UNCHANGED_REREAD_LIMIT = 3  # 内容不変の同一ファイル再読がこの回数で+2点
REVIEW_ELAPSED_SECONDS = 600    # 経過時間の発火条件(進捗なし5ツール以上と併用で+1点)
REVIEW_VALID_DECISIONS = ("continue", "adjust", "change", "stop")
# read_fileのキャッシュヒット通知(差分中心の再読 §6.3)の先頭文字列。
# 再評価パスの「内容不変の再読」カウントもこれで機械的に判定する。
UNCHANGED_READ_NOTICE_PREFIX = "(内容は前回read_fileした時から変わっていません"

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
PINNED_SECTION_HEADER = "\n--- 固定指示(ユーザーの継続指示・自動検出・要約対象外) ---\n"
PIN_TRIGGER_RE = re.compile(r"覚えて|忘れないで")  # この語を含むユーザー発言を継続指示とみなす
GOAL_SECTION_HEADER = "\n--- 現在のゴール(要約対象外) ---\n"
GOAL_LINE_RE = re.compile(r"^GOAL:[ \t]*(.*)$", re.MULTILINE)  # 要約LLM出力の先頭行を抽出
# ([ \t]*であって\s*ではない点に注意: \sは改行にもマッチするため、GOAL値が空の
# 場合に次行の内容まで巻き込んで抽出してしまうバグが実際にあった)

# --- 可逆操作レイヤー (REVERSIBLE_OPERATIONS.md 第1段階: ファイル編集の可逆化) ---
LEDGER_DIR_NAME = ".localcoder"                    # ワークスペース配下の台帳置き場
TXN_SUBDIR = Path(LEDGER_DIR_NAME) / "transactions"
TXN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")  # ロールバックAPIのid検証用

# --- 外部送信ポリシー (REVERSIBLE_OPERATIONS.md 第3段階 §8) ---
# 外部送信=管理範囲外への不可逆なコピー/状態変更。「危険なのは取り消せない
# ネットへの書き込み」という設計原則の中核。既定は allow_recorded(従来通り無確認で
# 実行するが、送信内容を台帳に必ず記録する)。deny にすると外部送信コマンドを実行
# 前に拒否する。ask(実行前にUIで確認)はSSE往復の同期承認が必要なため現時点で未実装。
EXTERNAL_SEND_POLICY = os.environ.get("LOCALCODER_EXTERNAL_SEND_POLICY", "allow_recorded")

# run_command のコマンド文字列から「外部送信」を検出するヒューリスティック。
# GET(取得)は安全側として対象外——POST/PUT/PATCH・アップロード・push・リモート
# コピー/実行・パッケージ公開など、管理範囲外へ不可逆にデータを出す操作だけを拾う。
# 誤検出より取りこぼしを避ける方針だが、シェルは自由度が高いため完全ではない
# (難読化・変数展開・エイリアス等は捕捉しきれない。あくまで明白なケースの安全網)。
_EXTERNAL_SEND_PATTERNS = [
    (re.compile(r"\bgit\s+push\b"), "git push (リモートへのコミット反映)"),
    (re.compile(r"\bcurl\b(?=.*(?:-X\s*(?:POST|PUT|PATCH|DELETE)\b|--request\s*(?:POST|PUT|PATCH|DELETE)\b|--data\b|--data-\w+\b|(?<!\w)-d\b|--form\b|(?<!\w)-F\b|--upload-file\b|(?<!\w)-T\b))", re.I | re.S),
     "curl による送信/アップロード (POST/PUT/PATCH/DELETE/--data/--form/-T)"),
    (re.compile(r"\bwget\b(?=.*(?:--post-data\b|--post-file\b|--method\s*=?\s*(?:POST|PUT)\b|--body-data\b|--body-file\b))", re.I | re.S),
     "wget による送信 (--post-data/--post-file/--method=POST)"),
    (re.compile(r"\b(?:scp|sftp)\b"), "scp/sftp (リモートへのファイル転送)"),
    (re.compile(r"\brsync\b(?=.*\s[\w.-]+@?[\w.-]+:)", re.S), "rsync のリモート転送 (host:path 宛)"),
    (re.compile(r"\bssh\b\s+(?!-)[\w.-]+@?[\w.-]+\s+\S"), "ssh によるリモートコマンド実行"),
    (re.compile(r"\b(?:npm|yarn|pnpm)\s+publish\b"), "npm/yarn/pnpm publish (パッケージ公開)"),
    (re.compile(r"\btwine\s+upload\b"), "twine upload (PyPI公開)"),
    (re.compile(r"\bgh\s+release\s+(?:create|upload)\b"), "gh release (GitHubリリース公開/アップロード)"),
    (re.compile(r"\baws\s+s3\s+(?:cp|sync|mv)\b(?=.*\bs3://)", re.S), "aws s3 へのアップロード"),
    (re.compile(r"\bgsutil\s+(?:cp|rsync|mv)\b(?=.*\bgs://)", re.S), "gsutil (GCSへのアップロード)"),
    (re.compile(r"\bgit\s+send-email\b|\b(?:sendmail|mail|mailx|mutt)\b"), "メール送信"),
    (re.compile(r"\bdocker\s+push\b"), "docker push (レジストリへのイメージ公開)"),
]

CANCEL = {}            # sid -> threading.Event
SELF_CHECK_RESULTS = []  # 起動時セルフチェックの結果。main()で1回だけ設定される
HISTORY_DIR = ROOT / "history"   # チャット履歴の保存先 (1会話 = 1 JSONファイル)
SCHEMA_VERSION = 2      # 履歴JSONの形式バージョン。v2でturnごとの診断情報を追加。
                       # 形式を変える時はここを上げ、読み込み時に必要なら移行する
                       # (古いファイルにschema_versionが無ければv1として扱う)
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
- You have tools: run_command, read_file, write_file, edit_file, list_dir, delete_file, delete_directory, move_file, copy_file, web_search, fetch_url, view_image. Use them freely without asking permission.
- To delete or move files, prefer the dedicated tools (delete_file, delete_directory, move_file) over `rm`/`mv` in run_command. The dedicated tools record the operation so the user can undo the whole turn; a raw `rm` cannot be undone. If you use run_command for a destructive file operation anyway, it will still run (no sandbox), but it won't be reversible.
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
        "name": "delete_file",
        "description": "Delete a single file. Reversible: the file is moved to the turn's transaction trash so the user can undo it. Prefer this over `rm` in run_command.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path (relative to workspace or absolute)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "delete_directory",
        "description": "Delete a directory and everything inside it. Reversible: the whole subtree is saved to the turn's transaction trash so the user can undo it. Prefer this over `rm -r` in run_command.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path (relative to workspace or absolute)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "move_file",
        "description": "Move or rename a file. Reversible: source, destination and any overwritten content are recorded so the user can undo it. Prefer this over `mv` in run_command.",
        "parameters": {"type": "object", "properties": {
            "src": {"type": "string", "description": "Existing file path to move"},
            "dst": {"type": "string", "description": "Destination file path"}},
            "required": ["src", "dst"]}}},
    {"type": "function", "function": {
        "name": "copy_file",
        "description": "Copy a file to a new path. Reversible: the created/overwritten destination is recorded so the user can undo it.",
        "parameters": {"type": "object", "properties": {
            "src": {"type": "string", "description": "Existing file path to copy"},
            "dst": {"type": "string", "description": "Destination file path"}},
            "required": ["src", "dst"]}}},
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


RECOMMENDED_MODEL_FAMILIES = ("gpt-oss", "ornith")  # README/REBUILD.mdで推奨としている系統


def run_self_check() -> list[dict]:
    """起動時セルフチェック(IMPROVEMENTS.md §9.2)。各項目を{name, ok, detail}で返す。

    全項目が診断用の警告に留まり、失敗してもサーバー起動は止めない。「Ollamaが
    後から起動する」「pdftotextが無くてもPDF以外は使える」といった部分的に
    使える状態を許容する既存方針(依存機能の段階的劣化、§9.4)に合わせている。
    """
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5) as r:
            data = json.loads(r.read())
        names = [m.get("name", "") for m in data.get("models", [])]
        add("Ollama接続", True, f"{OLLAMA}({len(names)}モデル)")
        recommended = [n for n in names if n.split(":")[0] in RECOMMENDED_MODEL_FAMILIES]
        add("推奨モデルの有無", bool(recommended),
            ", ".join(recommended) if recommended else "見つからず(動作は可能)")
    except Exception as e:
        add("Ollama接続", False, f"{OLLAMA} に接続できません: {type(e).__name__}: {e}")
        add("推奨モデルの有無", False, "Ollamaに接続できないため確認不可")

    pdftotext_path = shutil.which("pdftotext")
    add("pdftotext(poppler-utils)", bool(pdftotext_path),
        pdftotext_path or "見つからず(PDF以外の機能には影響なし)")

    bad_roots = [str(p) for p in ALLOWED_ROOTS if not p.is_dir()]
    add("allowed roots", not bad_roots,
        "全て有効" if not bad_roots else f"存在しないパス: {', '.join(bad_roots)}")

    # 外部送信ポリシー(§8)。既知の値でなければ警告(未知値はallow_recorded扱いで動く)
    add("外部送信ポリシー", EXTERNAL_SEND_POLICY in ("allow_recorded", "deny"),
        EXTERNAL_SEND_POLICY if EXTERNAL_SEND_POLICY in ("allow_recorded", "deny")
        else f"未知の値 '{EXTERNAL_SEND_POLICY}' (allow_recorded扱い)")

    try:
        probe = HISTORY_DIR / ".selfcheck_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("履歴ディレクトリへの書き込み", True, str(HISTORY_DIR))
    except Exception as e:
        add("履歴ディレクトリへの書き込み", False, f"{HISTORY_DIR}: {type(e).__name__}: {e}")

    # MCP設定は任意機能なので、設定ファイルが存在する時だけ検査する
    if MCP_CONFIG_PATH.is_file():
        try:
            servers = json.loads(
                MCP_CONFIG_PATH.read_text(encoding="utf-8")).get("mcpServers") or {}
            missing = [n for n, spec in servers.items()
                       if not isinstance(spec, dict)
                       or not shutil.which(str(spec.get("command", "")))]
            add("MCP設定", not missing,
                f"{len(servers)}サーバー定義"
                + (f"、コマンド不明: {', '.join(missing)}" if missing else ""))
        except Exception as e:
            add("MCP設定", False, f"{MCP_CONFIG_PATH.name}: {type(e).__name__}: {e}")

    return checks


def build_diagnostic_bundle(sid: str | None = None, error: str | None = None) -> dict:
    """問題報告用の診断パッケージを生成する(IMPROVEMENTS.md §9.3)。

    個人パス(ALLOWED_ROOTS/DEFAULT_WORKSPACE)・CSRFトークン・会話本文・
    認証情報は既定で含めない。sidを指定した場合のみ、そのセッションの
    turns(診断情報のみ。messagesは含めない)を追加する。
    """
    bundle: dict = {
        "localcoder_version": SERVER_VERSION,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "self_check": SELF_CHECK_RESULTS,
        "config": {
            "num_ctx": NUM_CTX,
            "max_iter": MAX_ITER,
            "cmd_timeout": CMD_TIMEOUT,
            "reserve_tokens": RESERVE_TOKENS,
            "keep_recent_msgs": KEEP_RECENT_MSGS,
            "keep_recent_tools": KEEP_RECENT_TOOLS,
            "proactive_compact_ratio": PROACTIVE_COMPACT_RATIO,
            "compact_target_ratio": COMPACT_TARGET_RATIO,
            "tool_stuck_limit": TOOL_STUCK_LIMIT,
            "empty_retry_limit": EMPTY_RETRY_LIMIT,
            "unfinished_retry_limit": UNFINISHED_RETRY_LIMIT,
            "http_retry_limit": HTTP_RETRY_LIMIT,
            "fail_repeat_threshold": FAIL_REPEAT_THRESHOLD,
            "allowed_roots_count": len(ALLOWED_ROOTS),  # パス自体は個人情報なので件数のみ
        },
    }

    # MCPサーバーの状態(名前・ツール数・生存のみ。command/argsは個人パスを
    # 含みうるため入れない)。プロセスの起動は伴わない(現在の状態を写すだけ)。
    bundle["mcp_servers"] = [
        {"name": p.name,
         "alive": p._proc is not None and p._proc.poll() is None,
         "tools": len(p._tools)}
        for p in TOOL_PROVIDERS if isinstance(p, McpToolProvider)]

    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=5) as r:
            bundle["ollama_version"] = json.loads(r.read()).get("version", "unknown")
    except Exception as e:
        bundle["ollama_version"] = f"取得失敗: {type(e).__name__}: {e}"

    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5) as r:
            data = json.loads(r.read())
        bundle["models"] = [{"name": m.get("name", ""),
                             "vision": "vision" in model_capabilities(m.get("name", ""))}
                            for m in data.get("models", [])]
    except Exception as e:
        bundle["models"] = f"取得失敗: {type(e).__name__}: {e}"

    if sid:
        try:
            f = HISTORY_DIR / f"{_safe_sid(sid)}.json"
            if f.is_file():
                bundle["session_turns"] = json.loads(f.read_text(encoding="utf-8")).get("turns", [])
            else:
                bundle["session_turns"] = "指定されたsidの履歴が見つかりません"
        except Exception as e:
            bundle["session_turns"] = f"取得失敗: {type(e).__name__}: {e}"

    if error:
        bundle["reported_error"] = error[:2000]  # 念のため上限を設ける

    return bundle


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


def derive_title(messages: list) -> str:
    """履歴一覧用のタイトルを最初のuserメッセージから作る。

    それが圧縮マーカー(【自動要約】/【自動省略】)だった場合は、生のマーカー文
    ではなく要約本文の冒頭を使う。マーカー文自体はどのセッションでも同じ
    書き出しなので、そのままだと履歴一覧で全部同じタイトルに見えて区別が
    つかなくなる(実際にユーザーから報告された)。
    """
    for m in messages:
        if m.get("role") == "user" and m.get("content"):
            c = m["content"]
            summary, _files, _pinned, _goal = _parse_marker(c)
            return summary if summary is not None else c
    return "(無題)"


def save_session(sid: str, model: str, workspace: str, messages: list,
                  turn: dict | None = None):
    sid = _safe_sid(sid)
    path = HISTORY_DIR / f"{sid}.json"
    # turns: プロンプト受信〜完了/中断までの時刻ログ。既存ファイルがあれば読み継ぐ
    # (save_sessionは毎回ファイル全体を上書きするため、ここで読まないと消えてしまう)。
    turns = []
    existing_title = None
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
            turns = prev.get("turns", [])
            existing_title = prev.get("title")
        except Exception:
            turns = []
    if turn is not None:
        turns.append(turn)
    # タイトルは初回保存時に一度だけ決め、以後は固定する。毎回作り直すと、
    # 圧縮が起きて先頭のuserメッセージが要約マーカーに置き換わった瞬間に
    # タイトルまで変わってしまう(本来のタイトルは既に生ログから失われている
    # ため復元できない)。既存タイトルがあればそれを常に優先する。
    title = existing_title or derive_title(messages)
    data = {"sid": sid, "schema_version": SCHEMA_VERSION, "title": title[:60],
            "updated_at": time.time(), "model": model, "workspace": workspace,
            "messages": messages, "turns": turns}
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


# ---------- 可逆操作レイヤー (REVERSIBLE_OPERATIONS.md 第1段階) ----------
def atomic_write(path: Path, content: str) -> None:
    """一時ファイルへ書いてから os.replace で置き換える原子的書き込み(§4.2)。

    プロセス停止や書き込みエラーで対象ファイルが中途半端な内容になるのを防ぐ。
    一時ファイルは同一ディレクトリに作る(os.replaceが同一ファイルシステム内で
    のみ原子的なため)。
    """
    temp = path.with_name(f".{path.name}.localcoder-tmp")
    with temp.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    temp = path.with_name(f".{path.name}.localcoder-tmp")
    with temp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def in_ledger_area(ws: Path, f: Path) -> bool:
    """fがワークスペースの台帳領域(.localcoder)配下か。

    write_file/edit_fileから保護する——モデルが台帳自体を書き換えられると
    「操作前の状態へ確実に戻せる」という可逆性の保証が壊れるため。
    """
    ledger = ws.resolve() / LEDGER_DIR_NAME
    fr = f.resolve()
    return fr == ledger or str(fr).startswith(str(ledger) + os.sep)


class Transaction:
    """1回の/api/chatリクエスト内のファイル操作を記録し、ターン単位で元に戻せる
    ようにする台帳 (REVERSIBLE_OPERATIONS.md §2〜§4)。

    - 置き場所はワークスペース配下 `.localcoder/transactions/<id>/`。最初の
      書き込みが起きるまで何も作らない(読み取りだけのターンでは痕跡ゼロ)。
    - 同じファイルを1ターン中に何度変更しても、変更前状態の保存は最初の1回だけ
      (§3)。これでトランザクション開始前の状態へ戻せる。
    - manifest.jsonは操作のたびに書き直す(ターン途中でプロセスが落ちても、
      そこまでの操作は台帳に残り、ロールバック可能なまま)。
    """

    def __init__(self, ws: Path):
        self.ws = ws.resolve()
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        self.dir = self.ws / TXN_SUBDIR / self.id
        self.started_at = time.time()
        self.status = "open"
        self.operations: list[dict] = []
        self.external_sends: list[dict] = []   # 第3段階(§8): 外部送信の台帳
        self._seen: set[str] = set()   # 記録済みrelpath(1ファイル=1オペレーション)

    @property
    def has_ops(self) -> bool:
        return bool(self.operations) or bool(self.external_sends)

    def _rel(self, f: Path) -> str:
        return str(f.resolve().relative_to(self.ws))

    def _ensure_dir(self) -> None:
        """台帳ディレクトリを作り、.localcoder自体を無視する.gitignoreを置く。
        (最初の記録時に1回だけ実行される。読み取りだけのターンでは呼ばれない)"""
        self.dir.mkdir(parents=True, exist_ok=True)
        gi = self.ws / LEDGER_DIR_NAME / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n")

    def _backup_bytes(self, subdir: str, rel: str, data: bytes) -> str:
        """dataを台帳の<subdir>/<rel>へ保存し、記録用の相対パス文字列を返す。"""
        dest = self.dir / subdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return str(Path(subdir) / rel)

    @staticmethod
    def _created_dirs_for(ws: Path, target: Path) -> list:
        """targetを作るために新規作成されることになる親ディレクトリを深い順に。
        (ロールバックで空になったら取り除くため)"""
        created = []
        d = target.parent
        while d != ws and not d.exists():
            created.append(str(d.resolve().relative_to(ws)))
            d = d.parent
        return created

    def record_before_write(self, f: Path) -> None:
        """write_file/edit_fileの書き込み直前(親ディレクトリ作成よりも前)に呼ぶ。
        変更前状態を保存する(§4.1)。"""
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

    def record_delete(self, f: Path) -> None:
        """delete_file/delete_directoryの実行直前に呼ぶ。削除対象を台帳へ退避する。

        ファイルはbefore/へ内容を保存し、ディレクトリは(空でなくても)配下の
        全ファイルを相対パスごと保存する。ロールバックで丸ごと復元できる。
        """
        self._ensure_dir()
        rel = self._rel(f)
        if f.is_dir():
            entries = []
            for child in sorted(f.rglob("*")):
                if child.is_file() or child.is_symlink():
                    crel = self._rel(child)
                    st = child.stat()
                    self._backup_bytes("trash", crel, child.read_bytes())
                    entries.append({"path": crel, "mode": st.st_mode})
                elif child.is_dir():
                    entries.append({"path": self._rel(child), "dir": True})
            op = {"type": "delete_dir", "path": rel,
                  "created_dirs": self._created_dirs_for(self.ws, f),
                  "entries": entries}
        else:
            st = f.stat()
            op = {"type": "delete", "path": rel, "before_mode": st.st_mode,
                  "backup_path": self._backup_bytes("trash", rel, f.read_bytes())}
        self.operations.append(op)
        self._write_manifest()

    def record_move(self, src: Path, dst: Path, dst_existed_data: bytes | None,
                     created_dirs: list) -> None:
        """move_file/copy_fileの完了直後に呼ぶ。移動元・移動先・上書きされた
        既存内容を記録する(§6)。dst_existed_dataはdstに元々あった内容(無ければNone)、
        created_dirsはdst親ディレクトリ作成のために新規作成された相対パス
        (呼び出し側がmkdir前に算出して渡す——mkdir後だと既存扱いになり検出できない)。"""
        self._ensure_dir()
        src_rel, dst_rel = self._rel(src), self._rel(dst)
        op = {"type": "move", "src": src_rel, "dst": dst_rel,
              "created_dirs": created_dirs}
        if dst_existed_data is not None:
            op["dst_overwritten_backup"] = self._backup_bytes(
                "before", dst_rel, dst_existed_data)
        self.operations.append(op)
        self._write_manifest()

    def record_copy(self, dst: Path, dst_existed_data: bytes | None,
                     created_dirs: list) -> None:
        """copy_fileの完了直後に呼ぶ。作られた/上書きされたdstだけを記録する
        (srcは変化しないので記録不要)。ロールバックはwrite/createと同じ扱い。"""
        self._ensure_dir()
        dst_rel = self._rel(dst)
        if dst_existed_data is not None:
            op = {"type": "write", "path": dst_rel, "existed_before": True,
                  "backup_path": self._backup_bytes("before", dst_rel, dst_existed_data)}
        else:
            op = {"type": "create", "path": dst_rel, "existed_before": False,
                  "created_dirs": created_dirs}
        self.operations.append(op)
        self._write_manifest()

    def created_dirs_for(self, target: Path) -> list:
        """公開版: mkdir前にtargetの親で新規作成される相対パス一覧を得る。"""
        return self._created_dirs_for(self.ws, target)

    def record_external_send(self, cmd: str, reasons: list, executed: bool) -> None:
        """run_commandで外部送信が検出された時に呼ぶ(§8)。送信内容(コマンド全文)・
        検出理由・ポリシーで実際に実行したかを台帳へ記録する。ファイル操作が無くても
        外部送信だけで台帳を残す(has_opsがexternal_sendsも見るのはこのため)。"""
        self._ensure_dir()
        self.external_sends.append({
            "at": time.time(), "command": cmd, "reasons": reasons,
            "policy": EXTERNAL_SEND_POLICY, "executed": executed})
        self._write_manifest()

    def _manifest(self) -> dict:
        return {"transaction_id": self.id,
                "started_at": self.started_at,
                "workspace": str(self.ws),
                "status": self.status,
                "operations": self.operations,
                "external_sends": self.external_sends}

    def _write_manifest(self) -> None:
        atomic_write(self.dir / "manifest.json",
                     json.dumps(self._manifest(), ensure_ascii=False, indent=1))

    def finalize(self, status: str) -> None:
        """ターン終了時に呼ぶ。書き込みが1件も無ければ(dir未作成のまま)何もしない。"""
        if not self.has_ops:
            return
        self.status = status
        self._write_manifest()


def _txn_manifest_path(ws: Path, txn_id: str) -> Path:
    if not TXN_ID_RE.fullmatch(txn_id):
        raise ValueError(f"不正なトランザクションID: {txn_id!r}")
    return ws / TXN_SUBDIR / txn_id / "manifest.json"


def _remove_empty_created_dirs(ws: Path, created_dirs: list) -> None:
    """作成された親ディレクトリのうち、空になったものだけ深い順に取り除く。"""
    for rel in created_dirs:
        try:
            resolve_path(ws, rel).rmdir()  # 空の場合だけ消える
        except OSError:
            pass


def _snapshot_after(tdir: Path, ws: Path, rel: str) -> None:
    """ロールバックで戻す直前の状態をafter/へ退避する(再適用=redo用)。"""
    f = resolve_path(ws, rel)
    if f.is_file():
        dest = tdir / "after" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(dest, f.read_bytes())


def rollback_transaction(ws: Path, txn_id: str) -> dict:
    """トランザクションの操作を逆順に取り消し、開始前の状態へ復元する(§12)。

    write/create/delete/delete_dir/move の各操作型を扱う。ロールバック自体も
    可逆にするため、戻す直前の各ファイルの内容を after/ へ退避してから復元する
    (誤ロールバックはreapply_transactionで再適用できる)。manifest内のパスは
    resolve_pathで検証するため、台帳が改竄されていてもワークスペース外の
    ファイルには触れない。
    """
    ws = ws.resolve()
    mpath = _txn_manifest_path(ws, txn_id)
    if not mpath.is_file():
        raise FileNotFoundError(f"トランザクションが見つかりません: {txn_id}")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    if manifest.get("status") == "rolled_back":
        raise ValueError("このトランザクションは既にロールバック済みです")
    tdir = mpath.parent
    counts = {"restored": 0, "removed": 0, "moved_back": 0, "undeleted": 0}
    for op in reversed(manifest.get("operations", [])):
        typ = op.get("type", "write")
        if typ in ("write", "create"):
            f = resolve_path(ws, op["path"])
            _snapshot_after(tdir, ws, op["path"])
            if op.get("existed_before"):
                f.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(f, (tdir / op["backup_path"]).read_bytes())
                if op.get("before_mode") is not None:
                    try:
                        os.chmod(f, op["before_mode"])
                    except OSError:
                        pass
                counts["restored"] += 1
            else:
                if f.is_file():
                    f.unlink()
                counts["removed"] += 1
                _remove_empty_created_dirs(ws, op.get("created_dirs", []))
        elif typ == "delete":
            # 削除したファイルをtrash/から復元する
            f = resolve_path(ws, op["path"])
            f.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(f, (tdir / op["backup_path"]).read_bytes())
            if op.get("before_mode") is not None:
                try:
                    os.chmod(f, op["before_mode"])
                except OSError:
                    pass
            counts["undeleted"] += 1
        elif typ == "delete_dir":
            # 削除したディレクトリ配下を丸ごと復元する
            for e in op.get("entries", []):
                p = resolve_path(ws, e["path"])
                if e.get("dir"):
                    p.mkdir(parents=True, exist_ok=True)
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(p, (tdir / "trash" / e["path"]).read_bytes())
                    if e.get("mode") is not None:
                        try:
                            os.chmod(p, e["mode"])
                        except OSError:
                            pass
            counts["undeleted"] += 1
        elif typ == "move":
            # dstを消してsrcへ戻す。dstに上書きされた既存があれば復元する
            src, dst = resolve_path(ws, op["src"]), resolve_path(ws, op["dst"])
            _snapshot_after(tdir, ws, op["dst"])
            if dst.is_file():
                src.parent.mkdir(parents=True, exist_ok=True)
                os.replace(dst, src)
            if op.get("dst_overwritten_backup"):
                dst.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(dst, (tdir / op["dst_overwritten_backup"]).read_bytes())
            _remove_empty_created_dirs(ws, op.get("created_dirs", []))
            counts["moved_back"] += 1
    manifest["status"] = "rolled_back"
    atomic_write(mpath, json.dumps(manifest, ensure_ascii=False, indent=1))
    return {"ok": True, **counts}


def reapply_transaction(ws: Path, txn_id: str) -> dict:
    """ロールバック済みトランザクションの変更を再適用する(§11のredo)。

    write/create/copyはafter/へ退避した「戻す直前の状態」を書き戻す。
    delete/delete_dirは再度削除し、moveは再度srcからdstへ動かす。
    再適用後にもう一度rollback_transactionを呼べば再び戻せる(undo/redoの往復)。
    """
    ws = ws.resolve()
    mpath = _txn_manifest_path(ws, txn_id)
    if not mpath.is_file():
        raise FileNotFoundError(f"トランザクションが見つかりません: {txn_id}")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    if manifest.get("status") != "rolled_back":
        raise ValueError("ロールバック済みのトランザクションだけ再適用できます")
    tdir = mpath.parent
    reapplied = 0
    for op in manifest.get("operations", []):
        typ = op.get("type", "write")
        if typ in ("write", "create"):
            after = tdir / "after" / op["path"]
            if after.is_file():
                f = resolve_path(ws, op["path"])
                f.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(f, after.read_bytes())
                reapplied += 1
        elif typ in ("delete", "delete_dir"):
            # 再度削除する(ロールバックで復元されたものを消し戻す)
            target = resolve_path(ws, op["path"])
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                reapplied += 1
            elif target.is_file():
                target.unlink()
                reapplied += 1
        elif typ == "move":
            src, dst = resolve_path(ws, op["src"]), resolve_path(ws, op["dst"])
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
                reapplied += 1
    manifest["status"] = "reapplied"
    atomic_write(mpath, json.dumps(manifest, ensure_ascii=False, indent=1))
    return {"ok": True, "reapplied": reapplied}


def classify_external_send(cmd: str) -> list:
    """run_commandのコマンド文字列から外部送信の兆候を検出する(§7-C/§8)。

    マッチした理由(人間可読)のリストを返す。空なら外部送信は検出されなかった
    (=ローカル完結とみなす)。GET等の取得系は安全側として拾わない。ヒューリスティック
    のため、難読化・変数展開・エイリアスは捕捉できない(明白なケースの安全網)。
    """
    reasons = []
    for pat, label in _EXTERNAL_SEND_PATTERNS:
        if pat.search(cmd):
            reasons.append(label)
    return reasons


def save_full_tool_output(sid: str | None, call_id: str | None, content: str) -> None:
    """出力がモデル向けに切り詰められた場合、完全な内容を診断用に別途保存する
    (IMPROVEMENTS.md §4.2)。12KBを超える出力の中間にビルドエラー等の重要な
    情報があっても、切り詰め後は永久に失われていた(モデルへ渡す文字列が
    そのまま会話履歴にも保存されるため)。sid/call_idが無ければ何もしない
    (診断用の副次的保存であり、本処理を失敗させたくないので例外も握りつぶす)。
    保存先はHISTORY_DIR配下なので、.gitignoreの`history/`で自動的に除外される。
    """
    if not sid or not call_id:
        return
    try:
        safe_call_id = re.sub(r"[^A-Za-z0-9_-]", "", call_id)[:40] or "call"
        d = HISTORY_DIR / "tool_output" / _safe_sid(sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{safe_call_id}.txt").write_text(content, encoding="utf-8")
    except Exception:
        pass


def run_command(cmd: str, ws: Path, cancel, sid: str | None = None,
                 call_id: str | None = None) -> str:
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
        save_full_tool_output(sid, call_id, out)
        out = out[:6000] + "\n...[truncated]...\n" + out[-6000:]
    if killed:
        return f"ERROR: command {killed}\n{out}"
    return f"exit_code={p.returncode}\n{out}"


def parse_command_result(result: str) -> dict:
    """run_commandの文字列結果を構造化データに変換する(IMPROVEMENTS.md §4.1)。

    「モデルには読みやすい文字列を渡し、サーバー側では機械判定に構造化データを
    使う」という方針を、run_command/ToolProvider/exec_toolの契約(いずれも文字列
    を返す)を一切変えずに実現する——文字列表現から逆算するアダプタとして追加
    した。既存の`r.startswith("exit_code=0")`のような場当たり的な文字列チェックを
    ここに集約し、`build_work_state`側の判定をこちらへ置き換える。

    構造化ツール結果の完全な形(§4.1のJSON例、stdout/stderr分離やduration_ms)は
    見送った——現在のrun_commandはstdoutとstderrを1本の文字列へ結合済みで
    (`out = stdout + "\n[stderr]\n" + stderr`)、分離しようとすると出力自体に
    その区切り文字列が偶然含まれるケースで誤動作しうる。実際に必要としている
    消費先(build_work_state)はok/exit_codeだけなので、確実に導出できる範囲に
    留めた。
    """
    if result.startswith("ERROR: command "):
        first_line = result.splitlines()[0]
        return {"ok": False, "exit_code": None,
                "timed_out": "timed out" in first_line,
                "cancelled": "cancelled" in first_line}
    if result.startswith("exit_code="):
        first_line = result.splitlines()[0]
        try:
            code = int(first_line.split("=", 1)[1].strip())
        except ValueError:
            code = None
        return {"ok": code == 0, "exit_code": code,
                "timed_out": False, "cancelled": False}
    # run_command以外のツール結果、または未知の形式。okをNoneにして
    # 「成功とも失敗とも判定できない」ことを明示する(Falseにすると誤って
    # 失敗扱いされてしまう)。
    return {"ok": None, "exit_code": None, "timed_out": False, "cancelled": False}


def sanitize_tool_name(raw_name: str) -> str:
    """ツール呼び出しのnameから、モデルが漏らす特殊トークンを除去する。

    一部のモデル(実例: laguna-xs-2.1)は<|tool_call_argument_begin|>のような
    内部特殊トークンをname欄に混入させて返すことがあり、完全一致ディスパッチが
    永遠に失敗し続ける(=ループを自己修正できないまま浪費する)原因になっていた。
    """
    return TOOL_NAME_TOKEN_RE.sub("", raw_name).strip()


def track_tool_repeat(name: str, args: dict, result: str,
                       last_failed_sig, repeat_count: int):
    """同じツール呼び出し(名前+引数)が同じ結果で連続失敗した回数を追跡する。

    戻り値: (新しいlast_failed_sig, 新しいrepeat_count, TOOL_STUCK_LIMITに達したか)
    達した場合、進展が見込めないと判断してMAX_ITERを待たず打ち切るために使う
    (ツール名破損やモデルの引数生成不良で80回丸ごと浪費するのを防ぐ)。
    """
    sig = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
    is_error = result.startswith("ERROR")
    if is_error and sig == last_failed_sig:
        repeat_count += 1
    else:
        repeat_count = 1 if is_error else 0
    new_sig = sig if is_error else None
    return new_sig, repeat_count, repeat_count >= TOOL_STUCK_LIMIT


class ToolContext:
    """ツール実行に必要な状態をまとめて渡すためのコンテナ(IMPROVEMENTS.md §13.2)。

    個々のツール実装は元exec_toolの引数を直接見るのではなく、これ経由でアクセス
    する。将来MCPツールプロバイダを追加する際も同じ形で渡せる。
    """
    __slots__ = ("ws", "cancel", "model", "pending_images", "sid", "call_id",
                 "messages", "txn")

    def __init__(self, ws: Path, cancel=None, model: str | None = None,
                 pending_images: list | None = None, sid: str | None = None,
                 call_id: str | None = None, messages: list | None = None,
                 txn: Transaction | None = None):
        self.ws = ws
        self.cancel = cancel
        self.model = model
        self.pending_images = pending_images
        self.sid = sid
        self.call_id = call_id
        # 差分中心の再読(IMPROVEMENTS.md §6.3)用。read_fileが「同じパスを前回
        # 読んだ時と内容が変わっていないか」を調べるために会話履歴を参照する。
        # Noneなら(exec_toolの既存呼び出し元・テスト等)チェックを単純に省略する。
        self.messages = messages
        # 可逆操作レイヤー(REVERSIBLE_OPERATIONS.md)のターン台帳。Noneなら
        # 記録なしで従来通り動く(テスト・レガシー呼び出し元の後方互換)。
        self.txn = txn


class ToolProvider(Protocol):
    """組み込みツールと将来のMCPツールを同じ形で扱うための共通インターフェース
    (IMPROVEMENTS.md §13.2)。ディスパッチ側(exec_tool)はこのプロトコルだけを見て
    呼び出す先を決めるため、新しいプロバイダ(例: McpToolProvider)を
    TOOL_PROVIDERSへ追加するだけで組み込みツールと同列に扱えるようになる。
    """

    def list_tools(self) -> list[dict]: ...  # Ollamaのtools引数に渡す定義そのもの

    def call_tool(self, name: str, args: dict, ctx: ToolContext) -> str: ...


class BuiltinToolProvider:
    """組み込みツール(run_command/read_file/write_file/...)のToolProvider実装。

    ロジック自体は元のexec_tool関数の中身をそのまま移しただけで、挙動は
    変えていない(IMPROVEMENTS.md §8.1のツール実行部分の分離)。
    """

    def list_tools(self) -> list[dict]:
        return TOOLS

    def call_tool(self, name: str, args: dict, ctx: ToolContext) -> str:
        ws, cancel, model = ctx.ws, ctx.cancel, ctx.model
        pending_images = ctx.pending_images
        try:
            if name == "run_command":
                cmd = args["command"]
                # 外部送信ポリシー(§8): 取り消せないネットへの書き込みだけを別扱いする
                reasons = classify_external_send(cmd)
                if reasons:
                    if EXTERNAL_SEND_POLICY == "deny":
                        if ctx.txn is not None:
                            ctx.txn.record_external_send(cmd, reasons, executed=False)
                        return ("ERROR: この操作は外部への送信(取り消せないネットワーク"
                                "書き込み)を含むため、現在のポリシー(deny)では実行"
                                f"できません。検出理由: {'; '.join(reasons)}。"
                                "ユーザーに実行を依頼するか、内容を確認の上で手動実行"
                                "してもらってください。")
                    # allow_recorded(既定): 従来通り実行するが、送信内容を台帳に必ず残す
                    if ctx.txn is not None:
                        ctx.txn.record_external_send(cmd, reasons, executed=True)
                return run_command(cmd, ws, cancel,
                                   sid=ctx.sid, call_id=ctx.call_id)
            if name == "read_file":
                f = resolve_path(ws, args["path"])
                if f.suffix.lower() == ".pdf":
                    return pdf_to_text(f)
                t = f.read_text(errors="replace")
                if len(t) > 60000:
                    t = t[:60000] + "\n...[truncated]..."
                if ctx.messages is not None:
                    prev = find_previous_read(ctx.messages, args["path"])
                    if prev is not None and prev == t:
                        digest = hashlib.sha256(t.encode()).hexdigest()[:16]
                        return (f"{UNCHANGED_READ_NOTICE_PREFIX}。"
                                f"SHA256={digest}、{len(t)}文字。前回の内容をそのまま"
                                "参照してください)")
                return t
            if name == "write_file":
                f = resolve_path(ws, args["path"])
                if in_ledger_area(ws, f):
                    return ("ERROR: .localcoder/ is LocalCoder's transaction ledger "
                            "and must not be modified by tools")
                if ctx.txn is not None:
                    ctx.txn.record_before_write(f)  # mkdirより前(新規親dir検出のため)
                f.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(f, args["content"])
                return f"OK: wrote {len(args['content'])} chars to {args['path']}"
            if name == "edit_file":
                f = resolve_path(ws, args["path"])
                if in_ledger_area(ws, f):
                    return ("ERROR: .localcoder/ is LocalCoder's transaction ledger "
                            "and must not be modified by tools")
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
                if ctx.txn is not None:
                    ctx.txn.record_before_write(f)
                atomic_write(f, t.replace(old, new))
                return (f"OK: replaced {n if args.get('replace_all') else 1} "
                        f"occurrence(s) in {args['path']}")
            if name == "list_dir":
                f = resolve_path(ws, args.get("path") or ".")
                items = sorted(e.name + ("/" if e.is_dir() else "") for e in f.iterdir())
                return "\n".join(items)[:8000] or "(empty)"
            if name == "delete_file":
                f = resolve_path(ws, args["path"])
                if in_ledger_area(ws, f):
                    return "ERROR: .localcoder/ is LocalCoder's ledger and cannot be deleted"
                if f.is_dir():
                    return "ERROR: this is a directory; use delete_directory instead"
                if not f.is_file():
                    return f"ERROR: file not found: {args['path']}"
                if ctx.txn is not None:
                    ctx.txn.record_delete(f)   # trashへ退避してから消す
                f.unlink()
                return f"OK: deleted {args['path']} (reversible for this turn)"
            if name == "delete_directory":
                f = resolve_path(ws, args["path"])
                if in_ledger_area(ws, f):
                    return "ERROR: .localcoder/ is LocalCoder's ledger and cannot be deleted"
                if not f.is_dir():
                    return f"ERROR: directory not found: {args['path']}"
                if f.resolve() == ws.resolve():
                    return "ERROR: refusing to delete the workspace root itself"
                if ctx.txn is not None:
                    ctx.txn.record_delete(f)
                shutil.rmtree(f)
                return f"OK: deleted directory {args['path']} and its contents (reversible for this turn)"
            if name in ("move_file", "copy_file"):
                src = resolve_path(ws, args["src"])
                dst = resolve_path(ws, args["dst"])
                if in_ledger_area(ws, src) or in_ledger_area(ws, dst):
                    return "ERROR: .localcoder/ is LocalCoder's ledger and cannot be a move/copy target"
                if not src.is_file():
                    return f"ERROR: source file not found: {args['src']}"
                if dst.is_dir():
                    return f"ERROR: destination is a directory: {args['dst']}"
                dst_existed = dst.read_bytes() if dst.is_file() else None
                # mkdirより前に新規親dirを算出する(mkdir後だと既存扱いで検出漏れする)
                created_dirs = (ctx.txn.created_dirs_for(dst) if ctx.txn is not None else [])
                dst.parent.mkdir(parents=True, exist_ok=True)
                if name == "move_file":
                    os.replace(src, dst)
                    if ctx.txn is not None:
                        ctx.txn.record_move(src, dst, dst_existed, created_dirs)
                    return f"OK: moved {args['src']} -> {args['dst']} (reversible for this turn)"
                shutil.copy2(src, dst)
                if ctx.txn is not None:
                    ctx.txn.record_copy(dst, dst_existed, created_dirs)
                return f"OK: copied {args['src']} -> {args['dst']} (reversible for this turn)"
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


# 現時点ではBuiltinToolProviderのみ。McpToolProviderを追加する際はここに足すだけで、
# exec_tool側のディスパッチ処理は変更不要になる設計(IMPROVEMENTS.md §13.2/§13.3)。
class McpToolProvider:
    """外部MCPサーバーをToolProviderとして扱う(IMPROVEMENTS.md §13 / 第6段階)。

    対応トランスポートはstdio(サーバーを子プロセスとして起動し、改行区切りの
    JSON-RPC 2.0で通信)のみ。HTTP/SSEトランスポートは実装しない——stdioの
    子プロセスはこのマシン内で完結するため、REVERSIBLE_OPERATIONS.md §8の
    外部送信ポリシーの対象外として扱える(§13.4の未確定事項への回答)。

    起動は遅延(初回のlist_tools/call_toolまでプロセスを作らない)。起動や通信に
    失敗しても例外は外へ漏らさず、list_tools()は空リスト・call_tool()は
    "ERROR: ..."文字列を返す——サーバー全体の動作は従来通り続く(依存機能の
    段階的劣化、IMPROVEMENTS.md §9.4の方針)。
    """

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict | None = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue | None = None
        self._tools: list[dict] = []
        self._lock = threading.Lock()  # ThreadingHTTPServerの複数スレッドから直列化
        self._id = 0
        self._failed_at = 0.0

    # --- プロセス管理・JSON-RPC(すべて_lock保持中に呼ぶこと) ---
    @staticmethod
    def _reader(proc: subprocess.Popen, q: queue.Queue) -> None:
        """stdoutを読み続けてキューへ流す常駐スレッド。パイプの読み取りには
        タイムアウトを付けられないため、待ち時間の制御はキュー側で行う。"""
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        q.put(None)  # EOFマーカー

    def _rpc_locked(self, method: str, params: dict, timeout: float) -> dict:
        self._id += 1
        rid = self._id
        req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError(f"{method}が{timeout}秒以内に応答しませんでした")
            try:
                line = self._queue.get(timeout=remain)
            except queue.Empty:
                raise TimeoutError(f"{method}が{timeout}秒以内に応答しませんでした")
            if line is None:
                raise RuntimeError("MCPサーバーが終了しました(stdoutがEOF)")
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue  # JSON以外の行(行儀の悪いサーバーのログ等)は読み飛ばす
            if resp.get("id") == rid:
                return resp
            # 他idの遅延応答・サーバー発の通知は読み飛ばす

    def _notify_locked(self, method: str) -> None:
        note = {"jsonrpc": "2.0", "method": method}
        self._proc.stdin.write(json.dumps(note) + "\n")
        self._proc.stdin.flush()

    @staticmethod
    def _to_ollama_tool(t: dict) -> dict:
        """MCPのツール定義(name/description/inputSchema)をOllamaのtools形式へ。"""
        return {"type": "function", "function": {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
        }}

    def _shutdown_locked(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            for f in (self._proc.stdin, self._proc.stdout):
                try:
                    if f:
                        f.close()
                except Exception:
                    pass
            try:
                self._proc.wait(timeout=5)  # ゾンビプロセス化を防ぐ
            except Exception:
                pass
        self._proc = None
        self._tools = []

    def _start_locked(self) -> bool:
        try:
            env = None
            if self.env:
                env = dict(os.environ)
                env.update(self.env)
            self._proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, env=env)
            self._queue = queue.Queue()
            threading.Thread(target=self._reader, args=(self._proc, self._queue),
                             daemon=True).start()
            init = self._rpc_locked("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "LocalCoder", "version": SERVER_VERSION}},
                timeout=MCP_INIT_TIMEOUT)
            if "error" in init:
                raise RuntimeError(str(init["error"].get("message", init["error"])))
            self._notify_locked("notifications/initialized")
            listed = self._rpc_locked("tools/list", {}, timeout=MCP_INIT_TIMEOUT)
            raw = (listed.get("result") or {}).get("tools") or []
            self._tools = [self._to_ollama_tool(t) for t in raw]
            self._failed_at = 0.0
            return True
        except Exception as e:
            print(f"[MCP] {self.name}: 起動失敗: {type(e).__name__}: {e}")
            self._shutdown_locked()
            self._failed_at = time.time()
            return False

    def _ensure_started_locked(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        if self._failed_at and time.time() - self._failed_at < MCP_RETRY_INTERVAL:
            return False
        return self._start_locked()

    # --- ToolProviderインターフェース ---
    def list_tools(self) -> list[dict]:
        with self._lock:
            if not self._ensure_started_locked():
                return []
            return self._tools

    def call_tool(self, name: str, args: dict, ctx: ToolContext) -> str:
        with self._lock:
            if not self._ensure_started_locked():
                return f"ERROR: MCPサーバー '{self.name}' に接続できません(起動失敗)"
            try:
                resp = self._rpc_locked("tools/call",
                                        {"name": name, "arguments": args},
                                        timeout=MCP_CALL_TIMEOUT)
            except Exception as e:
                # ハング・切断したサーバーは殺して、次の呼び出しで再起動を試みる
                self._shutdown_locked()
                return (f"ERROR: MCPサーバー '{self.name}' の呼び出しに失敗: "
                        f"{type(e).__name__}: {e}")
            if "error" in resp:
                err = resp["error"]
                return f"ERROR: MCPサーバー '{self.name}': {err.get('message', err)}"
            result = resp.get("result") or {}
            parts = [c.get("text", "") for c in result.get("content") or []
                     if c.get("type") == "text"]
            text = "\n".join(p for p in parts if p) or "(空の応答)"
            if result.get("isError") and not text.startswith("ERROR"):
                text = "ERROR: " + text
            return text


def load_mcp_providers(config_path: Path | None = None) -> list[McpToolProvider]:
    """mcp_servers.jsonからMCPサーバー定義を読む。ファイルが無ければ空リスト
    (MCP機能は完全に無効で、従来と同じ動作)。形式は一般的なmcpServers規約:

        {"mcpServers": {"名前": {"command": "python3", "args": ["..."],
                                 "env": {"KEY": "VALUE"}}}}
    """
    path = config_path or MCP_CONFIG_PATH
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[MCP] {path.name} の読み込みに失敗: {type(e).__name__}: {e}")
        return []
    providers = []
    for name, spec in (data.get("mcpServers") or {}).items():
        if not isinstance(spec, dict) or not spec.get("command"):
            print(f"[MCP] {name}: command がありません — スキップ")
            continue
        providers.append(McpToolProvider(name, spec["command"],
                                         spec.get("args") or [], spec.get("env")))
    return providers


TOOL_PROVIDERS: list[ToolProvider] = [BuiltinToolProvider()]


def all_tools() -> list[dict]:
    """全ToolProviderのツール定義を集める(Ollamaのtools引数用)。名前が重複した
    場合は先に登録されたプロバイダ(組み込みが先頭)を優先して後発を除外する。
    _provider_for_toolも同じ順で先勝ちするため、モデルへ見せる定義と実際の
    ディスパッチ先が常に一致する。
    """
    tools, seen = [], set()
    for provider in TOOL_PROVIDERS:
        for t in provider.list_tools():
            n = t.get("function", {}).get("name")
            if n in seen:
                continue
            seen.add(n)
            tools.append(t)
    return tools


def _provider_for_tool(name: str) -> ToolProvider | None:
    """nameを提供しているToolProviderをTOOL_PROVIDERSから探す(list_tools()の
    定義済みツール名と突き合わせるデータ駆動な方式。if/elifの連鎖ではない)。
    """
    for provider in TOOL_PROVIDERS:
        if any(t.get("function", {}).get("name") == name for t in provider.list_tools()):
            return provider
    return None


def exec_tool(name: str, args: dict, ws: Path, cancel=None, model: str | None = None,
              pending_images: list | None = None, sid: str | None = None,
              call_id: str | None = None, messages: list | None = None,
              txn: Transaction | None = None) -> str:
    """後方互換の薄いエントリポイント。実際のディスパッチはToolProvider経由で行う
    (IMPROVEMENTS.md §13.2)。呼び出し側(handle_chat)・既存テストの引数はそのまま。
    messagesは差分中心の再読(§6.3)用、txnは可逆操作レイヤー
    (REVERSIBLE_OPERATIONS.md)用の追加引数で、省略時は従来通り動作する。
    """
    ctx = ToolContext(ws=ws, cancel=cancel, model=model, pending_images=pending_images,
                      sid=sid, call_id=call_id, messages=messages, txn=txn)
    provider = _provider_for_tool(name)
    if provider is None:
        return f"ERROR: unknown tool {name}"
    return provider.call_tool(name, args, ctx)


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

出力の1行目は必ず次の形式にすること:
GOAL: <ユーザーの現在の最終目標を1文で>
2行目以降に要約本文を続けること。GOAL行以外に前置きや締めの文は不要。

--- 会話ログ ---
{log}
--- ログここまで ---

上記ログを冒頭の指示に従って日本語で要約せよ。1行目はGOAL行、2行目以降が要約本文。"""


UPDATE_SUMMARIZE_PROMPT = """以下は「これまでの会話の要約」と「その後の新しい会話ログ」である。
両方の情報を過不足なく統合し、今後の作業を継続するために必要な情報を日本語で簡潔にまとめよ。
必ず守ること:
- 「これまでの要約」に含まれる事実(ファイルパス・ユーザーの指示・決定事項)は、明確に古くなった/
  上書きされた場合を除き、失わずに引き継ぐこと
- ユーザーの目的・指示・好み (「覚えておいて」と言われた事項) は一字一句そのまま残すこと
- 「新しい会話ログ」で判明した内容(変更ファイル・技術的事実・完了/未完了)を追加すること
- 短くまとめ直そうとして事実を削らないこと。多少冗長でも欠落より安全を優先せよ

出力の1行目は必ず次の形式にすること:
GOAL: <ユーザーの現在の最終目標を1文で。新しい会話ログで目標が変わった/具体化した場合は
それを反映し、変わっていなければこれまでの目標を維持する>
2行目以降に統合後の要約本文を続けること。GOAL行以外に前置きや締めの文は不要。

--- これまでの要約 ---
{prev}
--- これまでの要約ここまで ---

--- 新しい会話ログ ---
{log}
--- ログここまで ---

上記を統合し、1行目はGOAL行、2行目以降に更新後の要約本文を日本語で出力せよ。"""


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


def _extract_goal_line(text: str) -> tuple[str, str | None]:
    """要約LLM出力の先頭付近にある"GOAL: ..."行を取り出し、(残りの本文, ゴール文字列)
    を返す。無ければ(元のtext, None)。ゴールは機械抽出できず(IMPROVEMENTS.md §3.1で
    指摘)、要約LLM自身に判定させるしかないため、既存の要約プロンプトへの相乗り
    (追加のLLM呼び出しを増やさない)という形にした。
    """
    m = GOAL_LINE_RE.search(text)
    if not m:
        return text, None
    goal = m.group(1).strip()
    rest = (text[:m.start()] + text[m.end():]).strip()
    return rest, (goal or None)


def update_summary(prev: str, new_raw: list, model: str) -> tuple[str, str | None]:
    """既存の要約(prev)に新規分の生ログを直接統合する(通常はLLM呼び出し1回)。

    フルの再要約と違い、prevはLLMに「引き継ぐべき既存事実」として提示するだけで
    元の生ログには戻らない。これにより圧縮を繰り返すたびに要約を要約し直す
    (伝言ゲーム的に内容が薄まる)ことを避け、既存事実の維持を明示的に指示できる。
    以前は「新規分を要約→既存要約とマージ」の2回呼び出しだったが、遅いローカル
    ハードでは圧縮1回の停止時間が倍になるため1回に統合した。新規分が上限を超える
    場合のみチャンクごとに逐次統合する。

    戻り値: (更新後の要約本文, 抽出したゴール文字列またはNone)。複数チャンクに
    分かれた場合は最後のチャンク(＝最新の文脈を踏まえた判定)のゴールを採用する。
    """
    cur = prev
    goal = None
    for ch in _chunk_messages(new_raw):
        raw = ollama_ask(model, UPDATE_SUMMARIZE_PROMPT.format(
            prev=cur, log=render_transcript(ch))).strip()
        body, g = _extract_goal_line(raw)
        if body:
            cur = body
        if g:
            goal = g
    return cur, goal


def summarize_old(old: list, model: str) -> tuple[str, str | None]:
    """古いメッセージ群を要約する。入力が要約1回の上限を超える場合は分割して各々要約。

    戻り値: (要約本文, 抽出したゴール文字列またはNone)。update_summaryと同じく
    最後のチャンクのゴール判定を採用する。
    """
    parts = []
    goal = None
    for ch in _chunk_messages(old):
        raw = ollama_ask(model, SUMMARIZE_PROMPT.format(
            log=render_transcript(ch))).strip()
        body, g = _extract_goal_line(raw)
        if body:
            parts.append(body)
        if g:
            goal = g
    if not parts:
        raise ValueError("empty summary")
    return "\n\n".join(parts), goal


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

    def add(p):
        if p and p not in files:
            files.append(p)

    for name, args, result in _iter_tool_calls_with_results(messages):
        if result is None or result.startswith("ERROR"):
            continue
        if name in ("write_file", "edit_file"):
            add(args.get("path"))
        elif name in ("delete_file", "delete_directory"):
            add(args.get("path"))
        elif name in ("move_file", "copy_file"):
            add(args.get("dst"))
    return files


def find_previous_read(messages: list, path: str) -> str | None:
    """会話履歴から、同じpathを対象にした直近の成功したread_file呼び出しの結果を
    探す(IMPROVEMENTS.md §6.3「差分中心の再読」の第一歩)。無ければNoneを返す。

    完全な差分計算(ファイル間diff)はここでは行わず、「前回読んだ時と内容が
    一字一句変わっていなければ全文の再送を省略する」というキャッシュヒット
    判定だけをこの段階で実装した。60000文字を超えるファイルは表示用に
    切り詰められているため、切り詰め境界のちょうど外側だけが変化した場合は
    「変化なし」と誤判定しうる(見つかった場合は稀な既知の制約として許容する)。
    """
    last = None
    for name, args, result in _iter_tool_calls_with_results(messages):
        if name == "read_file" and args.get("path") == path and result is not None \
           and not result.startswith("ERROR"):
            last = result
    return last


def extract_pinned_instructions(messages: list) -> list[str]:
    """ユーザー発言から「覚えておいて」等の継続指示らしきものを機械的に抽出する
    (発生順・重複除去、LLM不使用)。SUMMARIZE_PROMPTは要約時に「一字一句そのまま
    残すこと」と指示しているが、それ自体がLLMの遵守頼みだった。圧縮マーカーの
    専用ブロックに常に引き継ぐことで、要約の質に関わらず原文を保持する
    (IMPROVEMENTS.md §3.2)。
    """
    pinned = []
    for m in messages:
        if m.get("role") == "user":
            c = m.get("content") or ""
            if PIN_TRIGGER_RE.search(c) and c not in pinned:
                pinned.append(c)
    return pinned


def find_unverified_changes(messages: list) -> list[str]:
    """write_file/edit_fileで変更したファイルのうち、その後に一度も
    run_commandが実行されていないものを機械的に検出する(発生順・重複除去、
    LLM不使用)。モデルが「完了した」と申告しただけで実際にはビルド/テストで
    検証していない状態を検知するために使う(IMPROVEMENTS.md §3.3)。

    run_commandの成否は問わない——ここで確認したいのは「検証を試みたか」
    であり、「検証に成功したか」は直近コマンドの結果(build_work_state側)で
    別途分かる。run_commandが1回でも実行されればそれ以前の変更は「検証試行
    済み」とみなし、以後の新しい変更だけを追跡し直す。

    圧縮済みの古い部分はtool_calls構造が失われているため対象外——他の
    ダッシュボード項目と同じく直近の非圧縮ウィンドウのみを反映する。
    """
    unverified = []
    for name, args, result in _iter_tool_calls_with_results(messages):
        if name in ("write_file", "edit_file") and result is not None and not result.startswith("ERROR"):
            path = args.get("path")
            if path and path not in unverified:
                unverified.append(path)
        elif name == "run_command":
            unverified = []
    return unverified


def _parse_marker(content: str) -> tuple[str | None, list[str], list[str], str | None]:
    """圧縮マーカーのcontentから (要約本文, 変更ファイル一覧, 固定指示一覧, 現在のゴール) を取り出す。

    マーカーでなければ (None, [], [], None) を返す。ファイル一覧・固定指示は機械抽出、
    ゴールは要約LLMの出力から抽出したものだが、いずれも要約本文とは別ブロックに保存
    されているため、次回以降の再要約でパラフレーズされず正確な値のまま読み戻せる。
    """
    if not (content.startswith(MARKER_SUMMARY) or content.startswith(MARKER_OMIT)):
        return None, [], [], None
    if SUMMARY_BODY_SEP not in content:
        return None, [], [], None
    _, _, rest = content.partition(SUMMARY_BODY_SEP)
    goal = None
    if GOAL_SECTION_HEADER in rest:
        rest, _, goal_part = rest.partition(GOAL_SECTION_HEADER)
        goal = goal_part.strip() or None
    pinned = []
    if PINNED_SECTION_HEADER in rest:
        rest, _, pinned_part = rest.partition(PINNED_SECTION_HEADER)
        pinned = [line[2:] for line in pinned_part.splitlines() if line.startswith("- ")]
    if FILES_SECTION_HEADER in rest:
        summary_part, _, files_part = rest.partition(FILES_SECTION_HEADER)
        files = [line[2:] for line in files_part.splitlines() if line.startswith("- ")]
    else:
        summary_part, files = rest, []
    return summary_part.strip(), files, pinned, goal


def build_marker(summary_text: str, files: list[str], pinned: list[str] | None = None,
                  goal: str | None = None, failed: bool = False) -> str:
    """圧縮結果メッセージのcontentを組み立てる。

    ファイル一覧・固定指示・ゴールはLLM要約の本文に混ぜず別ブロックとして常に付記
    する。機械的に検出した事実/原文なので、要約の質に関わらず正確な値のまま次回
    以降も引き継がれる(ゴールのみ要約LLM自身の判定だが、それでも本文から分離して
    おくことで、後続の再要約時にパラフレーズされにくくする)。ブロックの順序は
    要約本文→ファイル一覧→固定指示→ゴールで固定(_parse_markerは末尾から順に
    partitionするため、新しいブロックを追加する時は末尾に足す)。
    """
    if failed:
        prefix, desc = MARKER_OMIT, "以前の会話は長すぎたため省略された。必要な情報は改めて確認すること。"
    else:
        prefix, desc = MARKER_SUMMARY, "ここまでの会話が長くなったため、古い部分は以下の要約に置き換えられた:"
    body = f"{prefix}{desc}\n{SUMMARY_BODY_SEP}{summary_text.strip()}"
    if files:
        body += FILES_SECTION_HEADER + "\n".join(f"- {f}" for f in files)
    if pinned:
        body += PINNED_SECTION_HEADER + "\n".join(f"- {p}" for p in pinned)
    if goal:
        body += GOAL_SECTION_HEADER + goal
    return body


def extract_current_goal(messages: list) -> str | None:
    """会話履歴の先頭が圧縮マーカーであれば、そこに保持されている「現在のゴール」
    (IMPROVEMENTS.md §3.1)を取り出す。まだ一度も圧縮が起きていない(＝先頭が
    マーカーでない)場合はNoneを返す——その場合はユーザーの最初の依頼がまだ会話
    そのものに残っているため、専用フィールドで別途思い出させる必要はない。
    """
    if not messages:
        return None
    first = messages[0]
    if first.get("role") != "user":
        return None
    _, _files, _pinned, goal = _parse_marker(first.get("content") or "")
    return goal


def build_work_state(messages: list) -> str:
    """会話履歴(system除く)から、現在のゴール・変更ファイル一覧・直近コマンド結果・
    繰り返し失敗を機械的に(LLMを使わず)抽出して短いダッシュボード文字列にする。
    空なら""を返す。

    圧縮済み(compact_history で要約済み)の古い部分はtool_calls構造が失われているため
    対象外——直近の非圧縮ウィンドウのみを反映する。古い部分の変更ファイルは
    compact_history側でextract_changed_filesにより圧縮マーカーに機械的に引き継がれる。
    """
    goal = extract_current_goal(messages)
    changed_files = extract_changed_files(messages)
    pinned = extract_pinned_instructions(messages)
    commands = []  # (command, result_or_None) を発生順に
    all_calls = []  # 全ツール呼び出しの署名(名前+引数)を発生順に
    for name, args, result in _iter_tool_calls_with_results(messages):
        all_calls.append((name, json.dumps(args, sort_keys=True, ensure_ascii=False)))
        if name == "run_command":
            commands.append((args.get("command", ""), result))

    lines = []
    if goal:
        lines.append(f"現在のゴール: {goal}")
    if pinned:
        lines.append("ユーザーの固定指示(厳守):")
        for p in pinned:
            lines.append(f"  - {p}")
    if changed_files:
        lines.append("変更したファイル: " + ", ".join(changed_files))

    unverified = find_unverified_changes(messages)
    if unverified:
        lines.append(
            "⚠ 未検証の変更(ビルド/テストを一度も実行していない): "
            + ", ".join(unverified)
            + " — これらを検証するまで作業を完了したと報告しないこと。")

    recent = commands[-RECENT_COMMANDS_SHOWN:]
    if recent:
        lines.append("直近の実行コマンド:")
        for cmd, result in recent:
            r = result or ""
            ok = parse_command_result(r).get("ok")
            status = "OK" if ok else "失敗/要確認"
            first_line = r.splitlines()[0] if r else "(結果なし)"
            lines.append(f"  - `{cmd}` → {status} ({first_line[:80]})")

    if len(commands) >= FAIL_REPEAT_THRESHOLD:
        tail = commands[-FAIL_REPEAT_THRESHOLD:]
        same_cmd = len({c for c, _ in tail}) == 1
        all_failed = all(not parse_command_result(r or "").get("ok") for _, r in tail)
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


# ---------- 自動方針再評価パス (METACOGNITIVE_REPLANNING.md 第1〜2段階) ----------
# 「エラーは出ていないのに、"次にやる"と宣言し続けて実際にはやらない」タイプの
# 停滞(実例: 74ツール呼び出し・72イテレーションでmain.cpp未作成のままユーザーが
# 手動停止)は、既存の連続失敗検知(TOOL_STUCK_LIMIT)では捕まらない。観測した
# 機械的状態から発火を判定し、ツールなしの専用LLM呼び出しで方針を再評価させる。

MUTATING_TOOLS = {"write_file", "edit_file", "delete_file", "delete_directory",
                  "move_file", "copy_file"}

REVIEW_SYSTEM_PROMPT = """これは作業継続のための自動方針再評価ターンです。
このターンではツールを使用せず、与えられた現在状態だけを分析してください。

重要:
- 必ず方針を変更する必要はありません。
- 現在の方針が妥当ならcontinueと判定してください。
- continueも明示的な判断であり、具体的証拠と再評価条件が必要です。
- 基本方針を保ちながら手順だけ変える場合はadjustです。
- 前提や方針を変える場合はchangeです。
- 自律継続が不適切ならstopです。
- 「まず確認してから作成する」という宣言を繰り返すだけで実際の作成・編集が
  進んでいない場合、それは停滞です。continueの根拠にはなりません。
- 「前回の判定」が与えられている場合、前回の期待がその後の実績で実現したかを
  previous_reviewで必ず判定してください。期待が外れた場合、それ自体が
  counterevidenceです。前回と同じ理由でのcontinueを繰り返さないでください。
- 前回もcontinueで、その後に進捗イベントが1件も無い場合、continueは
  採用されません。adjust/change/stopのいずれかを選んでください。

事実と推測を分離してください。既に失敗した手順を理由なく繰り返さないでください。
変更済みファイルとユーザーの固定指示を維持してください。

出力は次のJSON形式だけにしてください(コードフェンスや説明文は不要):
{
  "decision": "continue | adjust | change | stop",
  "assessment": "現状の一文評価",
  "evidence": ["現在の方針を支持する具体的な証拠"],
  "counterevidence": ["現在の方針に反する証拠または懸念"],
  "previous_review": {"exists": true, "prediction_met": false,
                      "details": "前回の期待と実績の照合結果(前回判定が無ければexists: false)"},
  "next_step": {
    "action": "次に実行する最小の具体的な行動",
    "expected_result": "期待する結果",
    "failure_means": "失敗した場合に何が分かるか"
  },
  "plan": ["手順1", "手順2"],
  "completion_criteria": ["完了と判断する条件"],
  "review_after": {"tool_calls": 4, "max_seconds": 300}
}"""


_ERROR_SIG_PATH_RE = re.compile(r"(/|[A-Za-z]:\\)[^\s:'\"()]+")  # unix/Windowsパス
_ERROR_SIG_NUM_RE = re.compile(r"\d+")


def error_signature(name: str, result: str) -> str | None:
    """ツール結果がエラーなら、パス・行番号・数値などの可変部分を除いた正規化
    済みの署名を返す(§11.4)。エラーでなければNone。

    完全一致の連続失敗検知(track_tool_repeat)では、引数のパスが毎回違う同種
    エラーを捕まえられない。実障害では「path is outside the workspace: <毎回
    別のパス>」が1ターンに17回発生したが、書き込みが時々成功していたため
    no_progressにもならず、どの発火条件にも乗らなかった。
    """
    if not isinstance(result, str) or not result:
        return None
    line = None
    if result.startswith("ERROR"):
        line = result.splitlines()[0]
    elif name == "run_command" and parse_command_result(result).get("ok") is False:
        # run_commandの失敗はexit_code行が先頭で全部同じ署名になってしまうため、
        # 本文から最初のエラーらしい行を探す(無ければexit_code行)。
        for cand in result.splitlines():
            if "error" in cand.lower():
                line = cand.strip()
                break
        if line is None:
            line = result.splitlines()[0]
        line = f"run_command: {line}"
    if line is None:
        return None
    sig = _ERROR_SIG_PATH_RE.sub("<path>", line)
    sig = _ERROR_SIG_NUM_RE.sub("<n>", sig)
    return sig[:160]


class ReviewState:
    """1ターン(1リクエスト)内の方針再評価に使う機械的カウンタ群。

    進捗イベント(§10)はLLMの申告ではなくツール結果から機械的に導出する:
    ファイル変更系ツールの成功、および直前に失敗していたrun_commandの成功
    (=ビルド/テスト結果の改善)の2種類のみ。読むだけの操作は何回成功しても
    進捗に数えない——実障害セッションでは「確認」だけが延々と続いた。
    """

    def __init__(self, turn_started_at: float | None = None):
        self.turn_started_at = turn_started_at or time.time()
        self.tool_calls_since_review = 0
        self.tools_since_last_progress = 0
        self.same_tool_failure_count = 0
        self.compacted_since_review = False
        self.tools_after_compaction = 0
        self.empty_response_recovered = False
        self.unchanged_reread_count = 0
        self.reviews_done = 0       # 採用された再評価の数
        self.review_attempts = 0    # 発火した再評価の数(不採用含む)。1ターン上限はこちらで数える
        self.tools_since_failed_attempt = 10 ** 9  # 不採用に終わった試行からのツール数
        self.json_retries = 0
        self.decisions: list[str] = []
        self.last_review: dict | None = None
        self.last_review_at: float | None = None
        self.last_review_seeded = False  # 前ターンの判定を履歴から引き継いだ場合True
        self.last_fire_reasons: list[str] = []
        # 前回の採用済み再評価以降の進捗イベント(§10)とエラー署名(§11.4)
        self.progress_events: list[dict] = []
        self.error_sig_counts: dict[str, int] = {}
        self._last_cmd_ok: bool | None = None

    @property
    def progress_since_review(self) -> int:
        return len(self.progress_events)

    @property
    def same_error_signature_count(self) -> int:
        """前回の採用済み再評価以降で最も多く繰り返された同種エラーの回数。"""
        return max(self.error_sig_counts.values(), default=0)

    def top_error_signature(self) -> tuple[str, int] | None:
        if not self.error_sig_counts:
            return None
        sig = max(self.error_sig_counts, key=self.error_sig_counts.get)
        return sig, self.error_sig_counts[sig]

    def note_compaction(self) -> None:
        self.compacted_since_review = True
        self.tools_after_compaction = 0

    def note_empty_recovery(self) -> None:
        self.empty_response_recovered = True

    def note_tool_result(self, name: str, result: str, repeat_count: int) -> None:
        """ツール実行1回ごとに呼ぶ。進捗イベントとエラー署名の機械的判定もここで行う。"""
        self.tool_calls_since_review += 1
        self.tools_since_failed_attempt += 1
        if self.compacted_since_review:
            self.tools_after_compaction += 1
        self.same_tool_failure_count = repeat_count
        progress_type = None
        if name in MUTATING_TOOLS and isinstance(result, str) and result.startswith("OK"):
            progress_type = "file_changed"
        if name == "run_command":
            ok = parse_command_result(result or "").get("ok")
            if ok and self._last_cmd_ok is False:
                progress_type = "command_recovered"  # 失敗していたコマンドが通った=改善
            self._last_cmd_ok = ok
        if name == "read_file" and isinstance(result, str) \
           and result.startswith(UNCHANGED_READ_NOTICE_PREFIX):
            self.unchanged_reread_count += 1
        sig = error_signature(name, result)
        if sig:
            self.error_sig_counts[sig] = self.error_sig_counts.get(sig, 0) + 1
        if progress_type:
            self.tools_since_last_progress = 0
            self.progress_events.append({"type": progress_type, "tool": name,
                                         "at": self.tool_calls_since_review})
        else:
            self.tools_since_last_progress += 1

    def note_attempt(self) -> None:
        """再評価が発火したら(採用の成否が決まる前に)呼ぶ。"""
        self.review_attempts += 1

    def note_attempt_failed(self) -> None:
        """再評価が不採用(JSON不良・採用条件不成立)に終わったら呼ぶ。
        これが無いとreview_after期限が立ちっぱなしのまま毎ツール呼び出しで
        再発火し、失敗し続けるLLM呼び出しを延々と繰り返してしまう。"""
        self.tools_since_failed_attempt = 0

    def note_review(self, review: dict, reasons: list[str]) -> None:
        """再評価が採用されたら呼ぶ。発火系カウンタをリセットする
        (進捗カウンタはリセットしない——進捗が無いという事実は再評価しても変わらない)。"""
        self.reviews_done += 1
        self.decisions.append(review.get("decision", "?"))
        self.last_review = review
        self.last_review_at = time.time()
        self.last_review_seeded = False  # このターンで新たに採用された判定
        self.last_fire_reasons = list(reasons)
        self.tool_calls_since_review = 0
        self.compacted_since_review = False
        self.tools_after_compaction = 0
        self.empty_response_recovered = False
        self.unchanged_reread_count = 0
        # 「前回判定後の実績」(§8)の起点をここに置き直す
        self.progress_events = []
        self.error_sig_counts = {}


def find_last_strategy_review(messages: list) -> dict | None:
    """履歴から最後に採用された方針再評価(localcoder_metaメッセージ)を機械的に
    探す。他の抽出関数(extract_changed_files等)と同じくLLM不使用。無ければNone。"""
    last = None
    for m in messages:
        if m.get("role") == "localcoder_meta" \
           and m.get("meta_type") == "strategy_review":
            last = m
    return last


def seed_review_state_from_history(rev: ReviewState, messages: list) -> None:
    """前ターンの判定をReviewStateへ引き継ぐ(§8のターン跨ぎ対応)。

    localcoder_metaはOllamaへの入力から常に除外されるため、これをしないと
    前ターンのSTOP/ADJUSTが残した具体的な計画がモデルから完全に見えなくなり、
    新しいターンで同じ調査ループを最初からやり直す(実例: STOPが「CMakeLists.txt
    とmain.cを1回で作成する」という計画を残したのに、次ターンで同じ
    get_reference×13回の情報収集を繰り返した)。

    引き継ぐのはlast_reviewのみ:
    - 期限の時計(last_review_at)はこのターンの開始時刻から数え直す。実際の
      created_atを使うと、古いセッションを何時間も後に再開しただけで
      max_seconds期限が即時発火してしまう。
    - last_fire_reasonsは引き継がない。同一理由の再発火抑止(§13)は同一ターン
      内の連続発火対策であり、新しいユーザー指示の後の正当な再発火まで
      塞ぐべきではない(実例のターン1はまさに前ターンと同じ理由の組
      many_tool_calls+no_progressで発火する必要があった)。
    - reviews_done(1ターン最大3回の発火予算)も引き継がない。
    """
    meta = find_last_strategy_review(messages)
    if meta and isinstance(meta.get("review"), dict):
        rev.last_review = meta["review"]
        rev.last_review_at = rev.turn_started_at
        rev.last_review_seeded = True


def review_score(state: ReviewState, now: float | None = None) -> tuple[int, list[str]]:
    """発火スコア(§11〜12)。単一条件の即発火ではなく複数の兆候を合算する。
    エラー署名の正規化(§11.4)・節目発火(§11.9)は第4段階の対象で未実装。"""
    now = now or time.time()
    score, reasons = 0, []
    if state.tool_calls_since_review >= REVIEW_AFTER_TOOL_CALLS:
        score += 2
        reasons.append("many_tool_calls")
    if state.tools_since_last_progress >= REVIEW_NO_PROGRESS_TOOLS:
        score += 2
        reasons.append("no_progress")
    if state.same_tool_failure_count >= 2:
        score += 3
        reasons.append("same_tool_failure")
    if state.same_error_signature_count >= 3:
        # 同種エラーの反復(§11.4)。パス等の可変部分を正規化した署名で数える
        # ため、「毎回別のパスで同じ種類のエラー」も捕まる(完全一致の
        # same_tool_failureや、書き込み成功が混ざるとリセットされる
        # no_progressでは捕まらなかった実障害への対応)。
        score += 3
        reasons.append("same_error")
    if state.compacted_since_review and state.tools_after_compaction >= 4:
        score += 1
        reasons.append("after_compaction")
    if state.empty_response_recovered:
        score += 3
        reasons.append("empty_response_recovered")
    if now - state.turn_started_at >= REVIEW_ELAPSED_SECONDS \
       and state.tools_since_last_progress >= 5:
        score += 1
        reasons.append("long_elapsed")
    if state.unchanged_reread_count >= REVIEW_UNCHANGED_REREAD_LIMIT:
        score += 2
        reasons.append("unchanged_reread")
    return score, reasons


def _review_after_due(state: ReviewState, now: float | None = None) -> bool:
    """前回判定のreview_after期限(§11.10)。到達したらスコア・最小間隔に関係なく発火。"""
    if not state.last_review:
        return False
    ra = state.last_review.get("review_after") or {}
    now = now or time.time()
    tc = ra.get("tool_calls")
    if isinstance(tc, (int, float)) and tc > 0 and state.tool_calls_since_review >= tc:
        return True
    ms = ra.get("max_seconds")
    if isinstance(ms, (int, float)) and ms > 0 and state.last_review_at \
       and now - state.last_review_at >= ms:
        return True
    return False


def should_review_strategy(state: ReviewState, now: float | None = None) -> tuple[bool, list[str]]:
    """発火判定(§11〜13)。(発火するか, 発火理由リスト)を返す。"""
    if not STRATEGY_REVIEW_ENABLED:
        return False, []
    # 1ターンの上限は「採用数」ではなく「試行数」で数える。採用数で数えると、
    # JSON不良等で不採用が続くモデルの場合に上限が一生効かず、期限到達が
    # 立ちっぱなしのまま毎ツール呼び出しで再評価LLMを呼び続けてしまう。
    if state.review_attempts >= REVIEW_MAX_PER_TURN:
        return False, []
    if state.tools_since_failed_attempt < REVIEW_MIN_INTERVAL_TOOLS:
        return False, []  # 不採用に終わった試行の直後は(期限到達でも)少し空ける
    if _review_after_due(state, now):
        return True, ["review_after_due"]
    if state.reviews_done and state.tool_calls_since_review < REVIEW_MIN_INTERVAL_TOOLS:
        return False, []  # 連続発火の防止(§13)。期限到達は上で優先済み
    score, reasons = review_score(state, now)
    if score < REVIEW_SCORE_THRESHOLD:
        return False, []
    if state.last_fire_reasons and set(reasons) <= set(state.last_fire_reasons):
        return False, []  # 前回と同じ発火理由だけでは再発火しない(§13)
    return True, reasons


def build_review_context(messages: list, state: ReviewState,
                         now: float | None = None) -> str:
    """再評価へ渡すコンテキスト(§9)。履歴全体は再送せず、機械的に組み立てた
    状態だけを渡す。"""
    now = now or time.time()
    lines = ["以下は現在の作業状態の機械的な要約です。", ""]
    goal = extract_current_goal(messages)
    if goal:
        lines.append(f"現在のゴール: {goal}")
    last_user = None
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str) \
           and not m["content"].startswith(WORK_STATE_PREFIX) \
           and not m["content"].startswith(MARKER_SUMMARY) \
           and not m["content"].startswith(MARKER_OMIT) \
           and m["content"] != EMPTY_RESPONSE_NUDGE:
            last_user = m["content"]
    if last_user:
        lines.append(f"ユーザーの直近の指示: {last_user[:500]}")
    pinned = extract_pinned_instructions(messages)
    if pinned:
        lines.append("ユーザーの固定指示(厳守):")
        lines.extend(f"  - {p}" for p in pinned)
    changed = extract_changed_files(messages)
    lines.append("変更したファイル: " + (", ".join(changed) if changed else "(まだ無い)"))
    unverified = find_unverified_changes(messages)
    if unverified:
        lines.append("未検証の変更: " + ", ".join(unverified))

    recent = []
    for name, args, result in _iter_tool_calls_with_results(messages):
        a = json.dumps(args, ensure_ascii=False)[:100]
        r = (result or "").splitlines()[0][:80] if result else "(結果なし)"
        recent.append(f"  - {name} {a} → {r}")
    if recent:
        lines.append("直近のツール呼び出し:")
        lines.extend(recent[-10:])

    lines.append("")
    lines.append("機械的カウンタ:")
    lines.append(f"  - 前回評価からのツール呼び出し: {state.tool_calls_since_review}")
    lines.append(f"  - 進捗イベントなしのツール呼び出し: {state.tools_since_last_progress}")
    lines.append(f"  - 内容が変わっていない同一ファイルの再読: {state.unchanged_reread_count}")
    lines.append(f"  - 同一ツール呼び出しの連続失敗: {state.same_tool_failure_count}")
    top_err = state.top_error_signature()
    if top_err:
        lines.append(f"  - 最多の同種エラー: {top_err[1]}回 「{top_err[0]}」")
    lines.append(f"  - 経過時間: {int(now - state.turn_started_at)}秒")

    if state.last_review:
        prev = state.last_review
        lines.append("")
        lines.append(f"前回の判定: {prev.get('decision', '?').upper()}")
        for e in prev.get("evidence") or []:
            lines.append(f"  前回の理由: {e}")
        ns = prev.get("next_step") or {}
        if ns.get("expected_result"):
            lines.append(f"  前回の期待: {ns['expected_result']}")
        lines.append("  その後の実績:")
        lines.append(f"    - ツール呼び出し: {state.tool_calls_since_review}回")
        if state.progress_events:
            kinds = {}
            for ev in state.progress_events:
                kinds[ev["type"]] = kinds.get(ev["type"], 0) + 1
            detail = ", ".join(f"{k}×{v}" for k, v in kinds.items())
            lines.append(f"    - 進捗イベント: {len(state.progress_events)}件 ({detail})")
        else:
            lines.append("    - 進捗イベント: なし")
        if top_err and top_err[1] >= 2:
            lines.append(f"    - 同種エラーの反復: {top_err[1]}回 「{top_err[0]}」")
        lines.append("")
        lines.append("中心の質問: 前回この方針を継続または変更すると判断した根拠は、"
                     "その後の実績によって支持されましたか。前回の期待が外れた場合は、"
                     "それ自体をcounterevidenceとして扱ってください。")
    return "\n".join(lines)


def parse_review_output(raw: str) -> dict | None:
    """LLM出力からJSONオブジェクトを取り出す。コードフェンスや前置きは無視する。"""
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def validate_review_decision(review: dict,
                             state: ReviewState | None = None) -> tuple[bool, str | None]:
    """判定の採用条件(§7)。CONTINUEは根拠・具体的な次の行動・検証可能な期待・
    有限の再評価条件が全て揃っている場合だけ採用する。

    stateが与えられた場合は「進捗なしで同じCONTINUEを禁止」(第3段階)も機械的に
    強制する: 前回の採用済み判定がCONTINUEで、それ以降に進捗イベント(ファイル
    変更・失敗コマンドの回復)が1件も無いのに再びCONTINUEと判定した場合、
    LLMの自己申告(evidence等)がいくら揃っていても採用しない。実障害では
    2回目のCONTINUEが「前回もcontinueと判断された点」を根拠に挙げる自己強化に
    陥っていた。
    """
    decision = str(review.get("decision", "")).lower()
    if decision not in REVIEW_VALID_DECISIONS:
        return False, f"decisionは{'/'.join(REVIEW_VALID_DECISIONS)}のいずれかにしてください"
    review["decision"] = decision
    if not str(review.get("assessment", "")).strip():
        return False, "assessment(現状評価)が必要です"
    ns = review.get("next_step") or {}
    if decision == "continue":
        evidence = [e for e in (review.get("evidence") or []) if str(e).strip()]
        if not evidence:
            return False, "continueには継続を正当化する具体的なevidenceが1件以上必要です"
        if not str(ns.get("action", "")).strip():
            return False, "continueには具体的なnext_step.actionが必要です"
        if not str(ns.get("expected_result", "")).strip():
            return False, "continueには検証可能なnext_step.expected_resultが必要です"
        if not str(ns.get("failure_means", "")).strip():
            return False, "continueにはnext_step.failure_means(失敗時に分かること)が必要です"
        ra = review.get("review_after") or {}
        if not (isinstance(ra.get("tool_calls"), (int, float)) and ra["tool_calls"] > 0) \
           and not (isinstance(ra.get("max_seconds"), (int, float)) and ra["max_seconds"] > 0):
            return False, "continueには有限のreview_after(tool_callsまたはmax_seconds)が必要です"
        if state is not None and state.last_review \
           and str(state.last_review.get("decision", "")).lower() == "continue" \
           and state.progress_since_review == 0:
            return False, ("前回もCONTINUEと判定しましたが、その後に進捗イベント"
                           "(ファイルの作成・編集、失敗していたコマンドの成功)が"
                           "1件もありません。進捗のないまま同じCONTINUEは採用"
                           "できません。手順を変えるならadjust、方針を変えるなら"
                           "change、自律継続が不適切ならstopへ判定を修正してください")
    elif decision in ("adjust", "change"):
        if not str(ns.get("action", "")).strip():
            return False, f"{decision}には具体的なnext_step.actionが必要です"
    return True, None


def run_review_pass(model: str, context: str,
                    state: ReviewState | None = None) -> tuple[dict | None, int]:
    """ツールなしの再評価呼び出し(§4〜5)。(採用されたレビュー or None, リトライ回数)。

    JSONが壊れている・採用条件を満たさない場合は1回だけ修正要求を行い、
    それでも失敗したら採用しない(通常作業は壊さず続行する)。
    """
    prompt = REVIEW_SYSTEM_PROMPT + "\n\n" + context
    raw = ollama_ask(model, prompt)
    review = parse_review_output(raw)
    ok, problem = validate_review_decision(review, state) if review else \
        (False, "出力からJSONオブジェクトを解釈できません")
    if ok:
        return review, 0
    retry_prompt = (prompt + "\n\nあなたの前回の出力:\n" + (raw or "")[:2000]
                    + f"\n\n問題: {problem}\n指定されたJSON形式だけで出力し直してください。")
    raw2 = ollama_ask(model, retry_prompt)
    review2 = parse_review_output(raw2)
    ok2, _ = validate_review_decision(review2, state) if review2 else (False, None)
    return (review2 if ok2 else None), 1


def make_review_meta(review: dict, reasons: list[str], score: int) -> dict:
    """採用されたレビューを履歴保存用のlocalcoder_metaメッセージにする(§14)。
    通常のuserメッセージとしては保存しない。Ollamaへ送る際はto_ollama_messagesで
    除外される。contentには人間可読の短い一行を入れておく(古いUI等が未知roleを
    素朴に表示しても壊れないための保険)。"""
    return {"role": "localcoder_meta", "meta_type": "strategy_review",
            "created_at": time.time(),
            "trigger": {"score": score, "reasons": reasons},
            "review": review,
            "content": f"(方針再評価: {review.get('decision', '?').upper()} — "
                       f"{str(review.get('assessment', ''))[:100]})"}


def format_review_for_dashboard(review: dict, from_previous_turn: bool = False) -> str:
    """直近の採用レビューを、次の通常モデル呼び出しの作業状態ダッシュボードへ
    差し込むテキストに変換する(§14)。

    from_previous_turn=Trueは履歴から引き継いだ前ターンの判定
    (seed_review_state_from_history)。新しいユーザー指示が前ターンの作業と
    無関係な場合もあるため、その旨を明示して新しい指示を優先させる。
    """
    ns = review.get("next_step") or {}
    ra = review.get("review_after") or {}
    if from_previous_turn:
        lines = ["(自動方針再評価 - 前ターンの判定。現在の作業の続きであれば"
                 "この計画を踏まえること。新しい指示と矛盾する場合は新しい指示を優先)"]
    else:
        lines = ["(自動方針再評価 - 現在の実行方針)"]
    lines += [f"判定: {review.get('decision', '?').upper()}",
              f"評価: {review.get('assessment', '')}"]
    prev_check = review.get("previous_review") or {}
    if prev_check.get("exists"):
        met = prev_check.get("prediction_met")
        verdict = "実現した" if met else "実現しなかった"
        detail = str(prev_check.get("details") or "")[:120]
        lines.append(f"前回の期待の検証: {verdict}" + (f" ({detail})" if detail else ""))
    for e in review.get("evidence") or []:
        lines.append(f"根拠: {e}")
    for c in review.get("counterevidence") or []:
        lines.append(f"反証・懸念: {c}")
    if ns.get("action"):
        lines.append(f"次に行うこと: {ns['action']}")
    if ns.get("expected_result"):
        lines.append(f"期待結果: {ns['expected_result']}")
    if ns.get("failure_means"):
        lines.append(f"失敗時に分かること: {ns['failure_means']}")
    plan = [p for p in (review.get("plan") or []) if str(p).strip()]
    if plan:
        lines.append("計画:")
        lines.extend(f"  {i}. {p}" for i, p in enumerate(plan, 1))
    criteria = [c for c in (review.get("completion_criteria") or []) if str(c).strip()]
    if criteria:
        lines.append("完了条件:")
        lines.extend(f"  - {c}" for c in criteria)
    cond = []
    if isinstance(ra.get("tool_calls"), (int, float)) and ra["tool_calls"] > 0:
        cond.append(f"あと{int(ra['tool_calls'])}ツール")
    if isinstance(ra.get("max_seconds"), (int, float)) and ra["max_seconds"] > 0:
        cond.append(f"{int(ra['max_seconds'])}秒後")
    if cond:
        lines.append("再評価: " + " または ".join(cond))
    return "\n".join(lines)


def to_ollama_messages(messages: list) -> list:
    """Ollamaへ送れるroleだけに絞る。localcoder_meta(方針再評価の内部状態)は
    履歴には保存するがモデルへの入力からは除外する——再評価結果は
    format_review_for_dashboard経由で作業状態として差し込む(§3, §14)。"""
    return [m for m in messages
            if m.get("role") in ("system", "user", "assistant", "tool")]


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

    prev_summary, prev_files, prev_pinned, prev_goal = _parse_marker(old[0].get("content", ""))
    new_raw = old[1:] if prev_summary is not None else old
    new_files = extract_changed_files(new_raw)
    all_files = prev_files + [f for f in new_files if f not in prev_files]
    new_pinned = extract_pinned_instructions(new_raw)
    all_pinned = prev_pinned + [p for p in new_pinned if p not in prev_pinned]

    sse({"type": "compact", "message": "履歴が長いため古い部分を要約しています…"})
    try:
        if new_raw and prev_summary:
            summary_text, new_goal = update_summary(prev_summary, new_raw, model)
        elif new_raw:
            summary_text, new_goal = summarize_old(new_raw, model)
        else:
            summary_text, new_goal = prev_summary or "", None
        # ゴールは要約LLMが毎回判定し直すため、新しい判定が得られればそれを採用し、
        # 得られなければ(GOAL行を出力しなかった等)前回のゴールを引き継ぐ。
        goal = new_goal or prev_goal
        marker = build_marker(summary_text, all_files, all_pinned, goal)
    except Exception as e:
        # 新規分の要約に失敗しても、既存の要約(あれば)・ファイル一覧・固定指示・
        # ゴールは機械的に保持する。全て消すより情報を残す方が安全。
        note = f"(直近の新規会話部分の要約に失敗したため未反映: {type(e).__name__})"
        summary_text = f"{prev_summary}\n{note}" if prev_summary else note
        marker = build_marker(summary_text, all_files, all_pinned, prev_goal, failed=not prev_summary)
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
                     f'window.LC_VERSION={json.dumps(SERVER_VERSION)};'
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
        elif self.path == "/api/selfcheck":
            # 起動時に1回だけ実行した結果を返す(IMPROVEMENTS.md §9.2)。
            # Ollama接続の有無等は機密ではないためトークン不要(/api/healthと同様)。
            self._json({"checks": SELF_CHECK_RESULTS})
        elif self.path.startswith("/api/diagnostic_bundle"):
            # 個人パス・会話本文は含めないが、モデル一覧やセッション診断情報は
            # /api/sessions相当に扱いトークン必須にする(IMPROVEMENTS.md §9.3)。
            if not self._token_ok():
                self._json({"error": "forbidden"}, 403)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sid = q.get("sid", [""])[0] or None
            error = q.get("error", [""])[0] or None
            self._json(build_diagnostic_bundle(sid=sid, error=error))
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
        if self.path == "/api/transaction/rollback":
            self._handle_txn_action(rollback_transaction)
            return
        if self.path == "/api/transaction/reapply":
            self._handle_txn_action(reapply_transaction)
            return
        if self.path == "/api/chat":
            self.handle_chat()
            return
        self._json({"error": "not found"}, 404)

    def _handle_txn_action(self, fn):
        """「今回の操作を元に戻す/再適用」(REVERSIBLE_OPERATIONS.md §12)。

        workspaceはチャットと同じ許可検証(under_allowed)を通し、トランザクション
        IDは形式検証(TXN_ID_RE)される。台帳内のパスもワークスペース外なら拒否
        されるため、リクエストや台帳の改竄で任意ファイルには触れない。
        """
        body = self._read_body()
        ws = Path(str(body.get("workspace") or "")).expanduser()
        txn_id = str(body.get("txn_id") or "")
        if not ws.is_dir() or not under_allowed(ws.resolve()):
            self._json({"error": f"不正なワークスペース: {ws}"}, 400)
            return
        try:
            self._json(fn(ws.resolve(), txn_id))
        except (ValueError, FileNotFoundError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

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

        # ツール一覧は組み込み+接続済みMCPサーバーの合算(リクエスト開始時に1回
        # だけ確定させ、ターン途中で増減しないようにする)。MCP未設定ならTOOLSと同じ。
        tools = all_tools()
        sys_prompt = SYSTEM_PROMPT.format(ws=ws)
        mcp_lines = []
        for provider in TOOL_PROVIDERS:
            if isinstance(provider, McpToolProvider):
                for t in provider.list_tools():
                    fn = t.get("function", {})
                    desc = (fn.get("description") or "").split("\n")[0][:120]
                    mcp_lines.append(f"- {fn.get('name')}: {desc}")
        if mcp_lines:
            # ローカルの弱いモデルはtoolsスキーマだけだと追加ツールを使い忘れる
            # ことがあるため、システムプロンプトでも存在を明示する
            sys_prompt += ("\n\nAdditional tools provided by connected MCP servers "
                           "(call them exactly like the built-in tools):\n"
                           + "\n".join(mcp_lines))
        messages = [{"role": "system", "content": sys_prompt}]
        messages += body.get("messages", [])
        # 可逆操作レイヤー(REVERSIBLE_OPERATIONS.md): 1リクエスト=1トランザクション。
        # 書き込みが1件も無ければディスクには何も作られない。
        txn = Transaction(wsr)
        # 自動方針再評価(METACOGNITIVE_REPLANNING.md): 発火判定用の機械的カウンタ。
        # 前ターンの判定(STOP/ADJUSTが残した計画)があれば引き継ぐ(§8ターン跨ぎ)。
        rev = ReviewState(turn_started_at)
        seed_review_state_from_history(rev, messages[1:])
        empty_retries = 0
        unfinished_retries = 0
        http_retries = 0
        last_failed_sig = None  # 直前の失敗ツール呼び出し(名前+引数)の署名。連続失敗検出用
        tool_repeat = 0
        stuck = False
        # 診断情報: 障害調査のたびに履歴JSONから手計算していた値を、保存時に
        # 機械的に記録しておく(IMPROVEMENTS.md §2.3)。
        diag_est_tokens_start = estimate_tokens(messages)
        diag_compact_count = 0
        diag_tool_call_count = 0
        diag_tool_exec_seconds = 0.0
        diag_iterations_used = 0

        try:
            for it in range(MAX_ITER):
                diag_iterations_used = it + 1
                # 予算超過時は自動圧縮 (リクエスト開始時とツール結果肥大時の両方を守る)
                _before_compact = messages
                messages = compact_history(messages, model, self._sse)
                if messages is not _before_compact:
                    diag_compact_count += 1
                    rev.note_compaction()
                # 作業状態ダッシュボードはOllama呼び出し1回分にのみ差し込む使い捨て
                # メッセージ。保存される会話履歴(messages)自体には加えない。
                work_state = build_work_state(messages[1:])
                if rev.last_review:
                    # 直近の方針再評価の結果を「現在の実行方針」として毎回再提示する
                    review_block = format_review_for_dashboard(
                        rev.last_review, from_previous_turn=rev.last_review_seeded)
                    work_state = (work_state + "\n\n" + review_block) if work_state \
                        else review_block
                call_messages = messages
                if work_state:
                    call_messages = messages + [
                        {"role": "user", "content": WORK_STATE_PREFIX + work_state}]
                payload = {"model": model,
                           "messages": to_ollama_messages(call_messages),
                           "tools": tools,
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
                    unverified_now = find_unverified_changes(messages[1:])
                    if content.strip() and unverified_now \
                       and unfinished_retries < UNFINISHED_RETRY_LIMIT:
                        # ツール呼び出し無し・本文ありで終えようとしたが、書き込んだ
                        # ファイルの検証(ビルド/テスト)が一度も行われていない。
                        # 実例: 8ファイル作成、5ファイル未検証のまま「残りを作成して
                        # ビルドする」という平文だけを返してターンが終わっていた。
                        # 未検証の変更が既にダッシュボードで警告されていてもモデルが
                        # 見落とすことがあるため、空応答と同じ枠組みで1回だけ機械的に
                        # 続行を促す(圧縮は不要——空応答と違い文脈枯渇が原因ではない)。
                        unfinished_retries += 1
                        self._sse({"type": "notice",
                                   "message": "未検証の変更が残ったままツール呼び出し無しで"
                                              "終えようとしたため、続行を促しています…"})
                        messages.append({"role": "user",
                                         "content": UNFINISHED_RESPONSE_NUDGE})
                        continue
                    if not content.strip() and empty_retries < EMPTY_RETRY_LIMIT:
                        # 本文なし・ツール呼び出しなしで終える"空応答"は、ユーザーには
                        # 何も起きていないように見えて実質的に停止してしまう。
                        # 1回だけ自動で続行を促し、それでも空なら諦めて通知する。
                        #
                        # 「続けてください」を足すだけでは文脈量がほぼ変わらず、予算の
                        # 天井付近で空応答になった場合は同じ壁に再度当たるだけなので、
                        # リトライ前に強制的に圧縮して実際に余白を作る(force=True)。
                        empty_retries += 1
                        _before_compact = messages
                        messages = compact_history(messages, model, self._sse, force=True)
                        if messages is not _before_compact:
                            diag_compact_count += 1
                            rev.note_compaction()
                        rev.note_empty_recovery()
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
                    name = sanitize_tool_name(raw_name)
                    # 生ログにも正規化後の名前を残しておく(fnは元のtool_calls/messages
                    # と同一オブジェクトを参照しているため、ここでの変更が保存履歴にも反映される)。
                    if name != raw_name:
                        fn["name"] = name
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    self._sse({"type": "tool_start", "name": name, "args": args})
                    _tool_started = time.time()
                    result = exec_tool(name, args, ws, ev, model=model,
                                       pending_images=pending_images,
                                       sid=sid, call_id=tc.get("id"),
                                       messages=messages, txn=txn)
                    diag_tool_call_count += 1
                    diag_tool_exec_seconds += time.time() - _tool_started
                    self._sse({"type": "tool_end", "name": name,
                               "result": result if len(result) <= 4000
                               else result[:4000] + "\n...[truncated]..."})
                    tool_msg = {"role": "tool", "tool_name": name,
                               "name": name, "content": result}
                    if name == "run_command":
                        # 構造化データはcontent(モデル向け文字列)とは別のmetaフィールドに
                        # 付記するだけで、既存のcontent読み取り側には一切影響しない
                        # (IMPROVEMENTS.md §4.1)。
                        tool_msg["meta"] = parse_command_result(result)
                    messages.append(tool_msg)
                    last_failed_sig, tool_repeat, is_stuck = track_tool_repeat(
                        name, args, result, last_failed_sig, tool_repeat)
                    rev.note_tool_result(name, result, tool_repeat)
                    if is_stuck:
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
                # 自動方針再評価(METACOGNITIVE_REPLANNING.md)。ツール結果を反映した
                # 後に発火判定する。再評価パス自体の失敗は通常作業を壊さない(§4)。
                if not stuck:
                    fire, fire_reasons = should_review_strategy(rev)
                    if fire:
                        rev.note_attempt()
                        self._sse({"type": "notice",
                                   "message": "🧭 作業方針を自動的に見直しています…"})
                        score, _ = review_score(rev)
                        review = None
                        try:
                            context = build_review_context(messages[1:], rev)
                            review, retries = run_review_pass(model, context, rev)
                            rev.json_retries += retries
                        except Exception as e:  # noqa: BLE001
                            self._sse({"type": "notice",
                                       "message": f"方針再評価に失敗したため通常作業を続けます"
                                                  f" ({type(e).__name__})"})
                        if not review:
                            # 不採用(JSON不良・採用条件不成立)。次の発火まで最小間隔を
                            # 空け、期限到達の立ちっぱなしによる連続再発火を防ぐ
                            rev.note_attempt_failed()
                        if review:
                            meta = make_review_meta(review, fire_reasons, score)
                            messages.append(meta)
                            rev.note_review(review, fire_reasons)
                            self._sse({"type": "strategy_review", "data": meta})
                            if review["decision"] == "stop":
                                turn_status = "review_stop"
                                self._sse({"type": "notice",
                                           "message": "🧭 方針再評価の判定がSTOPのため停止"
                                                      "しました。上のカードの障害内容を確認"
                                                      "して指示してください。"})
                                break
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

            # ターン単位の作業サマリー(IMPROVEMENTS.md §7.1)。診断情報(§2.3)を
            # そのままUIに出す。stopped/エラー/切断時は(SSEが既に途切れている
            # 可能性が高いため)送らない——completed/max_iter/stuckのみ対象。
            self._sse({"type": "summary", "data": {
                "status": turn_status,
                "duration_seconds": round(time.time() - turn_started_at, 1),
                "changed_files": extract_changed_files(messages[1:]),
                "unverified_changes": find_unverified_changes(messages[1:]),
                "tool_call_count": diag_tool_call_count,
                "compact_count": diag_compact_count,
                # 可逆操作レイヤー: 台帳に操作があればUIに「元に戻す」を出せる
                "txn_id": txn.id if txn.has_ops else None,
                "txn_ops": len(txn.operations),
                "external_sends": len(txn.external_sends),  # 外部送信の件数(§8)
                "workspace": str(wsr),
            }})
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
            # トランザクション台帳を確定する(書き込みが無ければ何も残らない)。
            # 停止・エラー時も自動ロールバックはしない——途中までの変更が有益な
            # 場合があるため、ユーザーが保持/復元を選ぶ(REVERSIBLE_OPERATIONS.md §10)。
            try:
                txn.finalize(turn_status)
            except Exception:
                pass
            # 会話を自動保存 (エラーや途中停止でもそこまでの内容を残す)
            # あわせて「プロンプトを受けてから完了/中断するまで」の時刻も記録する
            turn = {"started_at": turn_started_at, "ended_at": time.time(),
                    "status": turn_status,
                    "est_tokens_start": diag_est_tokens_start,
                    "est_tokens_end": estimate_tokens(messages),
                    "compact_count": diag_compact_count,
                    "http_retries": http_retries,
                    "empty_retries": empty_retries,
                    "unfinished_retries": unfinished_retries,
                    "tool_call_count": diag_tool_call_count,
                    "tool_exec_seconds": round(diag_tool_exec_seconds, 1),
                    "iterations_used": diag_iterations_used,
                    "changed_files_count": len(extract_changed_files(messages[1:])),
                    "unverified_changes_count": len(find_unverified_changes(messages[1:])),
                    "txn_id": txn.id if txn.has_ops else None,
                    "txn_ops": len(txn.operations),
                    "external_sends": len(txn.external_sends),
                    # 自動方針再評価の診断情報(METACOGNITIVE_REPLANNING.md §16の一部)
                    "strategy_review_count": rev.reviews_done,
                    "strategy_review_attempts": rev.review_attempts,
                    "strategy_review_decisions": rev.decisions,
                    "review_json_retry_count": rev.json_retries}
            if len(messages) > 1:
                try:
                    save_session(sid, model, str(ws), messages[1:], turn=turn)
                except Exception:
                    pass


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
    # MCPサーバーは遅延起動だが、ここで一度list_tools()を呼んで先に立ち上げて
    # おく(初回チャットの待ち時間と、起動失敗の早期発見のため)。失敗しても
    # サーバー全体は通常通り動く。
    for p in load_mcp_providers():
        TOOL_PROVIDERS.append(p)
        n = len(p.list_tools())
        print(f"  [{'OK' if n else 'NG'}] MCP {p.name}: "
              f"{f'{n}ツール' if n else '接続失敗または0ツール'}")
    print(f"LocalCoder running: http://localhost:{PORT}  (ollama: {OLLAMA})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
