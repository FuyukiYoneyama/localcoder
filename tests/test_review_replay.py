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
    実際に8ファイル作成した「成功」セッション。序盤の探索(12ツール目まで)には
    介入しない。

    §14(探索対象の再訪判定)導入後、実際のツール#1〜13は一貫して新規の
    パス・topicを読んでおり(list_projects→get_reference×2→read_source×2→
    get_reference×2→read_source×3→search→read_source→list_dir)、これは
    もう足踏みとして数えない。一方#14・#16・#17は同じ`list_dir
    /home/user/project/test/demo2`(空フォルダ)への完全な再訪であり、これは
    従来通りno_progressに積み上がる。結果、最初の発火は元の履歴(#12)より
    後ろの#25にずれるが、「序盤の探索には介入しない」という主旨自体は
    変わらず、むしろより正確に「本当に足踏みしている箇所」で発火する
    ようになった(2026-07-18、ユーザーからの実セッション指摘を受けた
    チューニング)。"""

    def setUp(self):
        self.events = s.replay_review_triggers(
            _fixture("runaway_no_progress.json"), turn_started_at=0.0)

    def test_no_fire_before_warmup(self):
        self.assertTrue(all(e["tool_call_index"] >= s.REVIEW_WARMUP_TOOLS
                            for e in self.events))

    def test_first_fire_is_after_the_repeated_empty_dir_check(self):
        """#14/#16/#17で同じ空フォルダを3回list_dirしている(再訪=足踏み)。
        その後の最初のno_progress到達点で発火する(元の履歴の#12より後ろ)。"""
        first = self.events[0]
        self.assertGreater(first["tool_call_index"], 12)
        self.assertEqual(set(first["reasons"]), {"many_tool_calls", "no_progress"})

    def test_at_least_one_fire(self):
        """§14導入前は2回発火していたが、序盤の新規探索の分がno_progress
        から除外されるようになり、後半の1回(同じ空フォルダの再訪+
        run_command)にまとまった。実際に問題があった箇所自体が検知
        されなくなったわけではないことを確認する。"""
        self.assertGreaterEqual(len(self.events), 1)


class TestEarlyStopWeakerModel(unittest.TestCase):
    """mrmkx5xnep2b1c.json: ornith:35bが17ツール・8イテレーションで
    CONTINUE→STOPと素早く介入されたセッション。

    実際のツール列を見直すと、17回すべてが新規の対象(list_projects→
    list_dir→get_reference×複数トピック・複数セクション→search→
    read_source→類似プロジェクトの参照実装の下見)で、エラーも再訪も
    0件——まさに「序盤の全体像把握」そのものだった。§14(探索対象の
    再訪判定)導入前は、これが当時の実障害(74回の生の足踏み)と
    同列に扱われ、CONTINUE→STOPで打ち切られていた。§14導入後は
    一切発火しない(2026-07-18、ユーザーからの実セッション指摘を
    受けたチューニング)。"""

    def setUp(self):
        self.events = s.replay_review_triggers(
            _fixture("early_stop_weaker_model.json"), turn_started_at=0.0)

    def test_no_intervention_during_pure_exploration(self):
        self.assertEqual(self.events, [])


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
    独立に加点され、結局12ツール目で(理由は変わるが)発火していた。

    §14(探索対象の再訪判定)導入後は、12ツール目までの中に含まれていた
    新規対象の探索がno_progressに数えられなくなったため、発火は当初の
    見積もり通りsame_tool_failure/same_error(病的シグナル、探索猶予の対象
    外)が効く15ツール目まで遅延する。「机上の見積もりと実際の挙動が
    食い違うことをこのツールが検出した」という当初の教訓と、
    「その後の追加チューニングで見積もりに近づいた」という結果の両方を
    記録として残す。
    """

    def setUp(self):
        self.events = s.replay_review_triggers(
            _fixture("early_intervention.json"), turn_started_at=0.0)

    def test_old_eleven_call_firing_is_gone(self):
        self.assertTrue(all(e["tool_call_index"] != 11 for e in self.events))

    def test_fires_via_pathological_signal_not_plain_no_progress(self):
        """新規対象の探索(no_progress)ではなく、same_tool_failure/same_error
        (探索猶予の対象外の病的シグナル)によって発火することを固定化する。"""
        self.assertEqual(len(self.events), 1)
        fire = self.events[0]
        self.assertGreaterEqual(fire["tool_call_index"], s.REVIEW_WARMUP_TOOLS)
        self.assertIn("same_tool_failure", fire["reasons"])
        self.assertIn("same_error", fire["reasons"])


class TestStuckRelativePath(unittest.TestCase):
    """mrnfwve8nnr3t2.json: 実際にターンを止めたのは方針再評価とは無関係な
    既存のTOOL_STUCK_LIMIT(相対パスの解釈違いで同一エラーが3回連続)だった。
    write_file等の結果メッセージを解決済み絶対パスにする修正はこのセッション
    が動機だが、それ自体は方針再評価と無関係なのでtest_tool_provider.pyで
    別途検証する。

    §14(探索対象の再訪判定)導入前は#12で(many_tool_calls+no_progress)、
    その後#16・#20と3回連続で発火していた。実際の呼び出し列を見ると、
    #1〜12は`mcp`配下の場所・検索語を変えながらの新規探索(list_projects→
    search×複数語→list_dir×複数パス)で、これはno_progressの対象外になる。
    #13・#14のwrite_file成功後、#16/#18/#20は同じ`test/demo2`を、
    #23〜25は同じ`~/pico`を繰り返しlist_dirしており(再訪)、この部分は
    従来通りno_progressに積み上がる。加えて実際のエラー(FileNotFoundError
    5回)によりsame_errorも効くため、最終的に1回(#23、many_tool_calls+
    same_error)だけ発火する——探索猶予の対象外である病的シグナルは
    健在なことを確認する。"""

    def setUp(self):
        self.events = s.replay_review_triggers(
            _fixture("stuck_relative_path.json"), turn_started_at=0.0)

    def test_fires_once_on_the_real_error_repetition(self):
        self.assertEqual(len(self.events), 1)
        fire = self.events[0]
        self.assertGreater(fire["tool_call_index"], 12)
        self.assertIn("same_error", fire["reasons"])


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
        # §14(探索対象の再訪判定)導入後: #1〜12の大半は新規パスの探索
        # (相対/絶対/`./`表記ゆれを正規化しても異なる対象)だったため
        # no_progressは外れる。実際のエラー反復によるsame_errorは従来通り
        # 効き、位置(#12)・採用状況は変わらない。
        self.assertEqual(set(self.events[0]["reasons"]),
                         {"many_tool_calls", "same_error"})

    def test_oversight_continues_past_the_old_three_shot_cap(self):
        """旧上限(3回)では発生し得なかった、tool_call_index 20超の追加発火。"""
        later_fires = [e for e in self.events if e["tool_call_index"] > 20]
        self.assertTrue(later_fires, "20回目より後に一切発火していない(旧上限のまま)")

    def test_hits_the_raised_per_turn_cap_not_left_unbounded(self):
        """上限自体は依然として有効(無制限ではない)。"""
        self.assertEqual(len(self.events), s.REVIEW_MAX_PER_TURN)


if __name__ == "__main__":
    unittest.main()
