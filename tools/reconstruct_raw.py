#!/usr/bin/env python3
"""history/raw/<sid>.jsonl(圧縮で捨てられる直前の生ログ)と
history/<sid>.json(圧縮され続ける現在のセッション本体)を連結し、
そのセッションの完全な非圧縮ログを1つのJSONに再構成する。

RAW_HISTORY.mdの分析用アーカイブ(archive_raw_messages)を読む側。
「最後のセッションを検討して」と言われたら、まずこれで完全ログを
組み立ててから読む/tools/replay_review.pyに渡すこと。

出力はhistory/<sid>.jsonと同じ形式({"sid","model","workspace","turns","messages"})
なので、replay_review.pyにそのまま渡せる。

使い方:
    python3 tools/reconstruct_raw.py <sid>              # 標準出力にJSONを書く
    python3 tools/reconstruct_raw.py <sid> -o out.json  # ファイルに書く
    python3 tools/replay_review.py <(python3 tools/reconstruct_raw.py <sid>)
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "history"
RAW_HISTORY_DIR = HISTORY_DIR / "raw"

MARKER_SUMMARY = "【自動要約】"
MARKER_OMIT = "【自動省略】"


def load_raw(sid: str) -> list:
    path = RAW_HISTORY_DIR / f"{sid}.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def is_marker_message(m: dict) -> bool:
    c = m.get("content")
    return (m.get("role") == "user" and isinstance(c, str)
            and (c.startswith(MARKER_SUMMARY) or c.startswith(MARKER_OMIT)))


def reconstruct(sid: str) -> dict:
    session_path = HISTORY_DIR / f"{sid}.json"
    if not session_path.is_file():
        raise SystemExit(f"session not found: {session_path}")
    data = json.loads(session_path.read_text(encoding="utf-8"))
    current = data.get("messages", [])
    raw = load_raw(sid)

    # 現在のセッション本体の先頭が圧縮マーカーなら、その内容はraw archive側で
    # 完全な形で既に持っているので重複させない(マーカーは要約=劣化コピー)。
    tail = current[1:] if current and is_marker_message(current[0]) else current

    full_messages = raw + tail
    return {
        "sid": data.get("sid", sid),
        "schema_version": data.get("schema_version"),
        "title": data.get("title"),
        "model": data.get("model"),
        "workspace": data.get("workspace"),
        "turns": data.get("turns", []),
        "messages": full_messages,
        "reconstructed_from": {
            "raw_jsonl_messages": len(raw),
            "session_tail_messages": len(tail),
            "had_compaction_marker": tail is not current,
        },
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sid")
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args(argv)

    result = reconstruct(args.sid)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
