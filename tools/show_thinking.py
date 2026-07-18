#!/usr/bin/env python3
"""history/thinking/<sid>.jsonl(モデルのthink/推論ストリームの分析専用ログ)
を読みやすく一覧表示する。

会話履歴(history/<sid>.json)にはthinkingを一切含めない設計なので
(含めると独り言自体が次の文脈を圧迫する)、これが独り言を確認できる
唯一の場所になる。異常に長い・同じ言い回しを繰り返す独り言(実例:
PIOかGPIOビットバンギングかを何十往復も「FINAL DECISION」と言っては
覆す)が無いかを見るために使う。

使い方:
    python3 tools/show_thinking.py <sid>              # 一覧(長さ・冒頭のみ)
    python3 tools/show_thinking.py <sid> --full        # 全文表示
    python3 tools/show_thinking.py <sid> --iteration 5 # 指定イテレーションの全文
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THINKING_LOG_DIR = ROOT / "history" / "thinking"

HEAD_CHARS = 200


def load(sid: str) -> list:
    path = THINKING_LOG_DIR / f"{sid}.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sid")
    ap.add_argument("--full", action="store_true", help="全イテレーションの全文を表示")
    ap.add_argument("--iteration", type=int, default=None, help="このイテレーションだけ全文表示")
    args = ap.parse_args(argv)

    records = load(args.sid)
    if not records:
        print(f"(記録なし: history/thinking/{args.sid}.jsonl)")
        return 0

    if args.iteration is not None:
        matches = [r for r in records if r["iteration"] == args.iteration]
        if not matches:
            print(f"iteration={args.iteration} の記録がありません")
            return 1
        for r in matches:
            print(r["thinking"])
        return 0

    total_chars = sum(r["thinking_len"] for r in records)
    print(f"=== {args.sid}: {len(records)}件、独り言合計 {total_chars:,}文字 ===\n")
    for r in records:
        if args.full:
            print(f"--- iteration {r['iteration']} ({r['thinking_len']}文字、"
                  f"本回答{r['content_len']}文字) ---")
            print(r["thinking"])
            print()
        else:
            head = r["thinking"][:HEAD_CHARS].replace("\n", " ")
            more = "…" if r["thinking_len"] > HEAD_CHARS else ""
            print(f"[{r['iteration']:3d}] {r['thinking_len']:6d}文字  {head}{more}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
