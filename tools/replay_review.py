#!/usr/bin/env python3
"""保存済みセッション(history/<sid>.json)を分析するCLIツール。

自動方針再評価(METACOGNITIVE_REPLANNING.md)のチューニングを検証・デバッグする
ために作った。server.pyの実際のロジック(review_score/should_review_strategy/
error_signature)へ、セッションの実データを機械的に通し直すだけで、以下を
一貫した形式で出す:

- ツール呼び出しのヒストグラムとエラー結果の一覧
- エラー署名(パス・数値を正規化)ごとの反復回数
- 現在のコードならいつ・どのスコア・どの理由で方針再評価が発火するか
  (§12「閾値や重みは実セッションの診断情報から調整する」の道具化)
- (参考)履歴に実際に残っているlocalcoder_metaの判定一覧

LLMは一切呼ばない。発火タイミングの妥当性(早すぎる/遅すぎる/検知できない)は
検証できるが、判定の中身(CONTINUE/ADJUSTどちらが妥当か)は分からない
——それは実Ollamaでのe2e検証が引き続き必要。

使い方:
    python3 tools/replay_review.py <history.jsonへのパス>
    python3 tools/replay_review.py tests/fixtures/review_incidents/*.json
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402


def analyze(path: Path) -> None:
    d = json.loads(path.read_text(encoding="utf-8"))
    msgs = d.get("messages", [])
    print(f"=== {path.name} ===")
    print(f"model: {d.get('model', '?')}  messages: {len(msgs)}  "
         f"turns: {len(d.get('turns', []))}")

    names = []
    errors = []
    for name, args, result in s._iter_tool_calls_with_results(msgs):
        names.append(name)
        if isinstance(result, str) and (result.startswith("ERROR")
                                        or s.parse_command_result(result).get("ok") is False):
            errors.append((name, result.splitlines()[0][:100] if result else ""))

    print("\nツール呼び出しヒストグラム:", dict(Counter(names)))
    print(f"エラー結果: {len(errors)}件 / 全{len(names)}回")

    sig_counts: dict[str, int] = {}
    for name, result in errors:
        sig = s.error_signature(name, result)
        if sig:
            sig_counts[sig] = sig_counts.get(sig, 0) + 1
    if sig_counts:
        print("\nエラー署名(正規化後)の反復回数:")
        for sig, cnt in sorted(sig_counts.items(), key=lambda x: -x[1]):
            print(f"  {cnt:3d}回  {sig}")

    changed = s.extract_changed_files(msgs)
    unverified = s.find_unverified_changes(msgs)
    print(f"\n変更ファイル: {len(changed)}件  未検証: {len(unverified)}件")

    print("\n--- 現在のコードでの方針再評価タイムライン ---")
    events = s.replay_review_triggers(msgs, turn_started_at=0.0)
    if not events:
        print("  (発火なし)")
    for e in events:
        hist = "履歴と一致" if e["adopted"] else \
            ("履歴に判定あり(現在は不採用)" if e["historical"] else "★現在のコードで新規検知")
        print(f"  ツール呼び出し#{e['tool_call_index']:3d} ({e['tool_name']}) "
             f"score={e['score']} reasons={e['reasons']}  [{hist}]")

    metas = [m for m in msgs if m.get("role") == "localcoder_meta"]
    if metas:
        print("\n--- 履歴に実際に残っている判定(参考) ---")
        for m in metas:
            r = m.get("review", {})
            print(f"  {r.get('decision', '?').upper():8s} "
                 f"trigger={m.get('trigger', {})}")
    print()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    for arg in argv:
        p = Path(arg)
        if not p.is_file():
            print(f"skip (not found): {arg}")
            continue
        analyze(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
