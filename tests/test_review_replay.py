"""方針再評価の発火タイミングを、実障害セッションに対して回帰テストする。

`replay_review_triggers`(server.py)を使い、review_score/should_review_strategy
のロジックを実際のセッションデータへ機械的に通し直す。LLMは呼ばないため
判定の中身(CONTINUE/ADJUSTどちらが妥当か)は検証できないが、「いつ発火するか」
は完全に決定的に検証できる。

review_score/should_review_strategy/error_signature等のスコアリングロジックを
変更する時は、このファイルのテストが全て通ることを確認してから変更を確定する
——1つの実障害に合わせて閾値を調整した結果、別の実障害の検知が壊れる
(実際に「早すぎる介入」修正の直後に類似のことが起きかけた)ことを防ぐための
回帰スイート。新しい実障害セッションを分析したら、tests/fixtures/
review_incidents/へ追加してこのファイルにテストを足すのが標準の運用。

フィクスチャは実際に問題が起きたセッションの実データ(パスのみ匿名化)を含む
ため、他のtests/fixtures/同様.gitignore対象。無い環境ではスキップする。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as s  # noqa: E402

from _helpers import load_fixture_messages  # noqa: E402


def _fixture(name: str) -> list:
    return load_fixture_messages(f"review_incidents/{name}")


class TestRunawayNoProgress(unittest.TestCase):
    """mrm597jy640ydz.json: 資料収集後にモデルが前ターンのSTOPを踏まえて
    実際に8ファイル作成した「成功」セッション。今回のチューニング変更後も、
    序盤の探索(12ツール目まで)には介入せず、その後は履歴の実際の発火と
    一致することを固定化する。"""

    def setUp(self):
        self.events = s.replay_review_triggers(
            _fixture("runaway_no_progress.json"), turn_started_at=0.0)

    def test_no_fire_before_warmup(self):
        self.assertTrue(all(e["tool_call_index"] >= s.REVIEW_WARMUP_TOOLS
                            for e in self.events))

    def test_first_fire_matches_history(self):
        first = self.events[0]
        self.assertEqual(first["tool_call_index"], 12)
        self.assertEqual(set(first["reasons"]), {"many_tool_calls", "no_progress"})
        self.assertTrue(first["historical"])
        self.assertTrue(first["adopted"])

    def test_at_least_two_fires(self):
        self.assertGreaterEqual(len(self.events), 2)


class TestEarlyStopWeakerModel(unittest.TestCase):
    """mrmkx5xnep2b1c.json: ornith:35bが17ツール・8イテレーションで
    CONTINUE→STOPと素早く介入されたセッション。実障害(74回)と比べ
    一桁少ない段階での検知を維持できていることを固定化する。"""

    def setUp(self):
        self.events = s.replay_review_triggers(
            _fixture("early_stop_weaker_model.json"), turn_started_at=0.0)

    def test_fires_match_history_exactly(self):
        self.assertEqual(len(self.events), 2)
        first, second = self.events
        self.assertEqual(first["tool_call_index"], 12)
        self.assertTrue(first["adopted"])
        self.assertEqual(second["tool_call_index"], 16)
        self.assertEqual(second["reasons"], ["review_after_due"])
        self.assertTrue(second["adopted"])

    def test_detected_within_20_tool_calls(self):
        """介入が74回停滞と同レベルまで遅延していないことの目安。"""
        self.assertLess(self.events[-1]["tool_call_index"], 20)


class TestWorkspaceBoundaryErrors(unittest.TestCase):
    """mrmlsgf87glpgh.json: SDKパスがワークスペース外のため17回失敗し、
    合間の書き込み成功でno_progressがリセットされ続け、当時は
    max_iter(80イテレーション)まで検知されなかった実障害。エラー署名
    正規化(same_error)の追加で、現在のコードなら大幅に早く検知できる
    ことを固定化する——このテストはsame_error追加以前は失敗していたはず。"""

    def setUp(self):
        self.events = s.replay_review_triggers(
            _fixture("workspace_boundary_errors.json"), turn_started_at=0.0)

    def test_same_error_detected_well_before_max_iter(self):
        same_error_fires = [e for e in self.events if "same_error" in e["reasons"]]
        self.assertTrue(same_error_fires, "same_errorによる発火が無い")
        self.assertLess(same_error_fires[0]["tool_call_index"], 30,
                        "80イテレーションのmax_iterに対し十分早く検知できていない")

    def test_at_least_two_same_error_fires(self):
        """1回だけでは、既存の抑制(最小間隔・同一理由の再発火禁止)で
        止められてしまわないことの確認。"""
        same_error_fires = [e for e in self.events if "same_error" in e["reasons"]]
        self.assertGreaterEqual(len(same_error_fires), 2)


class TestEarlyIntervention(unittest.TestCase):
    """mrnej9juovu4a0.json: ターン序盤11ツール・約3分で2回介入した
    「早すぎる」実障害。ウォームアップ導入後、旧来の11ツール目の発火
    (no_progress+empty_response_recovered)は消える。

    注記: 当初「ウォームアップ導入によりsame_errorが効く15ツール目付近まで
    発火が遅延する」と見積もっていたが、実際にreplay_review_triggersで
    確認するとREVIEW_AFTER_TOOL_CALLSとREVIEW_WARMUP_TOOLSが偶然どちらも
    12であるため、ウォームアップ終了と同時にmany_tool_calls(+2)が
    独立に加点され、結局12ツール目で(理由は変わるが)発火する。
    机上の見積もりと実際の挙動が食い違うことをこの回帰テストとCLIツールが
    実際に検出した——これが本ツールを作った動機そのものである。
    """

    def setUp(self):
        self.events = s.replay_review_triggers(
            _fixture("early_intervention.json"), turn_started_at=0.0)

    def test_old_eleven_call_firing_is_gone(self):
        self.assertTrue(all(e["tool_call_index"] != 11 for e in self.events))

    def test_fires_at_warmup_boundary_not_optimistic_estimate(self):
        """現状: ウォームアップ境界(12)でmany_tool_calls+no_progressにより発火。
        当初の見積もり(15ツール目でsame_error)ではないことを明示的に固定化する
        ——閾値定数を独立に変えた場合、この整合はすぐ崩れうる。"""
        self.assertEqual(len(self.events), 1)
        fire = self.events[0]
        self.assertEqual(fire["tool_call_index"], s.REVIEW_WARMUP_TOOLS)
        self.assertIn("many_tool_calls", fire["reasons"])


class TestStuckRelativePath(unittest.TestCase):
    """mrnfwve8nnr3t2.json: 方針再評価自体は正しく機能した(最初の3回の発火が
    現在のロジックとも完全一致)例。実際にターンを止めたのは無関係な既存の
    TOOL_STUCK_LIMIT(相対パスの解釈違いで同一エラーが3回連続)だった。
    この回帰テストは「発火タイミングが妥当な良い例」を固定化し、以後の
    チューニングがこの正常系を壊していないかを確認する目的。write_file等の
    結果メッセージを解決済み絶対パスにする修正はこのセッションが動機だが、
    それ自体は方針再評価と無関係なのでtest_tool_provider.pyで別途検証する。

    REVIEW_MAX_PER_TURNを3→8へ引き上げた後は、当時の上限(3回)で打ち切られて
    いた4回目のチェックが#24で新たに発生する(採用実績は無いので不採用扱い)。
    これは意図した改善であり、当時存在しなかった追加チェック自体を固定化する。
    """

    def setUp(self):
        self.events = s.replay_review_triggers(
            _fixture("stuck_relative_path.json"), turn_started_at=0.0)

    def test_first_three_fires_match_history_exactly(self):
        self.assertGreaterEqual(len(self.events), 3)
        for e in self.events[:3]:
            self.assertTrue(e["historical"])
            self.assertTrue(e["adopted"])

    def test_fourth_fire_is_new_from_raised_per_turn_cap(self):
        """REVIEW_MAX_PER_TURN引き上げ前は3回で打ち切られていた。"""
        self.assertEqual(len(self.events), 4)
        fourth = self.events[3]
        self.assertEqual(fourth["tool_call_index"], 24)
        self.assertFalse(fourth["historical"])
        self.assertEqual(self.events[0]["tool_call_index"], 12)
        self.assertEqual(set(self.events[0]["reasons"]),
                         {"many_tool_calls", "no_progress"})


class TestUnmonitoredThrash(unittest.TestCase):
    """mrnnt9oripnrok.json: MAX_ITER(80)まで走った長いターンで、当時の上限
    (REVIEW_MAX_PER_TURN=3)により最初の20ツール呼び出し分しか監視されず、
    残り60回(同じ小さなmain.cppをrun_command検証なしで30回以上write_file
    し続ける停滞)が完全に無監視だった。上限を8へ引き上げ、かつ同一パスへの
    未検証な連続書き込みを進捗に数えないよう修正した後は、#20以降も
    review_after期限により複数回の追加チェックが発生することを固定化する。
    """

    def setUp(self):
        self.events = s.replay_review_triggers(
            _fixture("unmonitored_thrash.json"), turn_started_at=0.0)

    def test_first_three_fires_match_history(self):
        self.assertGreaterEqual(len(self.events), 3)
        for e in self.events[:3]:
            self.assertTrue(e["historical"])
            self.assertTrue(e["adopted"])
        self.assertEqual(self.events[0]["tool_call_index"], 12)
        self.assertEqual(set(self.events[0]["reasons"]),
                         {"many_tool_calls", "no_progress", "same_error"})

    def test_oversight_continues_past_the_old_three_shot_cap(self):
        """旧上限(3回)では発生し得なかった、tool_call_index 20超の追加発火。"""
        later_fires = [e for e in self.events if e["tool_call_index"] > 20]
        self.assertTrue(later_fires, "20回目より後に一切発火していない(旧上限のまま)")

    def test_hits_the_raised_per_turn_cap_not_left_unbounded(self):
        """上限自体は依然として有効(無制限ではない)。"""
        self.assertEqual(len(self.events), s.REVIEW_MAX_PER_TURN)


if __name__ == "__main__":
    unittest.main()
