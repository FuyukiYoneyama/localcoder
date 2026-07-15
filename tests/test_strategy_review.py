"""自動方針再評価パス(METACOGNITIVE_REPLANNING.md 第1〜2段階)の単体テスト。

Ollamaは使わず、run_review_passは_helpers.FakeOllamaで差し替える。
実障害セッション(74ツール呼び出し・72イテレーションでmain.cpp未作成のまま
ユーザーが手動停止)で観測された「エラーゼロのまま確認だけを繰り返す」停滞
パターンが発火条件に乗ることを、合成した同型シーケンスで検証する。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as s  # noqa: E402

from _helpers import FakeOllama  # noqa: E402


def valid_continue():
    return {
        "decision": "continue",
        "assessment": "方針は妥当",
        "evidence": ["原因候補の切り分けが進んでいる"],
        "counterevidence": [],
        "next_step": {"action": "残る依存候補を検証する",
                      "expected_result": "不一致の有無が確定する",
                      "failure_means": "依存以外の原因だと分かる"},
        "review_after": {"tool_calls": 4, "max_seconds": 300},
    }


class TestReviewStateProgress(unittest.TestCase):
    def test_mutating_tool_success_is_progress(self):
        st = s.ReviewState(turn_started_at=0)
        for _ in range(5):
            st.note_tool_result("read_file", "some content", 0)
        self.assertEqual(st.tools_since_last_progress, 5)
        st.note_tool_result("write_file", "OK: wrote 10 chars to a.py", 0)
        self.assertEqual(st.tools_since_last_progress, 0)

    def test_failed_mutating_tool_is_not_progress(self):
        st = s.ReviewState(turn_started_at=0)
        st.note_tool_result("write_file", "ERROR: missing required argument", 0)
        self.assertEqual(st.tools_since_last_progress, 1)

    def test_reading_is_never_progress(self):
        st = s.ReviewState(turn_started_at=0)
        for name in ("read_file", "list_dir", "web_search", "fetch_url"):
            st.note_tool_result(name, "content", 0)
        self.assertEqual(st.tools_since_last_progress, 4)

    def test_command_recovering_from_failure_is_progress(self):
        st = s.ReviewState(turn_started_at=0)
        st.note_tool_result("run_command", "ERROR: command failed (exit 1)\nboom", 0)
        st.note_tool_result("run_command", "exit_code=0\nok", 0)
        self.assertEqual(st.tools_since_last_progress, 0)

    def test_command_success_without_prior_failure_is_not_progress(self):
        st = s.ReviewState(turn_started_at=0)
        st.note_tool_result("run_command", "exit_code=0\nls output", 0)
        self.assertEqual(st.tools_since_last_progress, 1)

    def test_unchanged_reread_is_counted(self):
        st = s.ReviewState(turn_started_at=0)
        notice = s.UNCHANGED_READ_NOTICE_PREFIX + "。SHA256=abc、10文字)"
        st.note_tool_result("read_file", notice, 0)
        st.note_tool_result("read_file", "fresh content", 0)
        st.note_tool_result("read_file", notice, 0)
        self.assertEqual(st.unchanged_reread_count, 2)


class TestReviewScore(unittest.TestCase):
    def test_zero_for_fresh_state(self):
        st = s.ReviewState(turn_started_at=1000)
        score, reasons = s.review_score(st, now=1001)
        self.assertEqual(score, 0)
        self.assertEqual(reasons, [])

    def test_many_tool_calls_and_no_progress(self):
        st = s.ReviewState(turn_started_at=1000)
        st.tool_calls_since_review = s.REVIEW_AFTER_TOOL_CALLS
        st.tools_since_last_progress = s.REVIEW_NO_PROGRESS_TOOLS
        score, reasons = s.review_score(st, now=1001)
        self.assertEqual(score, 4)
        self.assertEqual(set(reasons), {"many_tool_calls", "no_progress"})

    def test_same_tool_failure_scores_three(self):
        st = s.ReviewState(turn_started_at=1000)
        st.same_tool_failure_count = 2
        score, reasons = s.review_score(st, now=1001)
        self.assertEqual(score, 3)
        self.assertIn("same_tool_failure", reasons)

    def test_empty_response_recovery_scores_three(self):
        st = s.ReviewState(turn_started_at=1000)
        st.note_empty_recovery()
        score, reasons = s.review_score(st, now=1001)
        self.assertEqual(score, 3)
        self.assertIn("empty_response_recovered", reasons)

    def test_long_elapsed_needs_no_progress_too(self):
        st = s.ReviewState(turn_started_at=1000)
        score, _ = s.review_score(st, now=1000 + s.REVIEW_ELAPSED_SECONDS)
        self.assertEqual(score, 0)  # 進捗なし5ツール以上が無ければ加点しない
        st.tools_since_last_progress = 5
        score, reasons = s.review_score(st, now=1000 + s.REVIEW_ELAPSED_SECONDS)
        # tools_since_last_progress=5はno_progress(>=8)には届かないため、
        # long_elapsedの+1点だけが付く
        self.assertEqual(score, 1)
        self.assertEqual(reasons, ["long_elapsed"])

    def test_incident_pattern_reaches_threshold(self):
        """実障害と同型: エラーゼロ・書き込みなしの確認ループ+内容不変の再読。"""
        st = s.ReviewState(turn_started_at=1000)
        notice = s.UNCHANGED_READ_NOTICE_PREFIX + "。SHA256=x、9文字)"
        for i in range(8):
            st.note_tool_result("read_file", notice if i < 3 else "content", 0)
        score, reasons = s.review_score(st, now=1005)
        self.assertGreaterEqual(score, s.REVIEW_SCORE_THRESHOLD)
        self.assertIn("no_progress", reasons)
        self.assertIn("unchanged_reread", reasons)


class TestShouldReviewStrategy(unittest.TestCase):
    def _stalled_state(self):
        st = s.ReviewState(turn_started_at=1000)
        st.tool_calls_since_review = s.REVIEW_AFTER_TOOL_CALLS
        st.tools_since_last_progress = s.REVIEW_NO_PROGRESS_TOOLS
        return st

    def test_fires_at_threshold(self):
        fire, reasons = s.should_review_strategy(self._stalled_state(), now=1001)
        self.assertTrue(fire)
        self.assertIn("no_progress", reasons)

    def test_does_not_fire_below_threshold(self):
        st = s.ReviewState(turn_started_at=1000)
        st.tools_since_last_progress = s.REVIEW_NO_PROGRESS_TOOLS  # 2点のみ
        fire, _ = s.should_review_strategy(st, now=1001)
        self.assertFalse(fire)

    def test_max_reviews_per_turn(self):
        st = self._stalled_state()
        st.reviews_done = s.REVIEW_MAX_PER_TURN
        fire, _ = s.should_review_strategy(st, now=1001)
        self.assertFalse(fire)

    def test_min_interval_after_a_review(self):
        st = self._stalled_state()
        st.note_review(valid_continue(), ["no_progress"])
        # review_after期限は最小間隔より優先されるため、ここでは期限発火を
        # 除外して最小間隔そのものを検証する
        del st.last_review["review_after"]
        st.tool_calls_since_review = s.REVIEW_MIN_INTERVAL_TOOLS - 1
        st.tools_since_last_progress = 100
        st.unchanged_reread_count = 100
        fire, _ = s.should_review_strategy(st, now=1001)
        self.assertFalse(fire)

    def test_same_reasons_do_not_refire(self):
        st = self._stalled_state()
        st.note_review(valid_continue(), ["many_tool_calls", "no_progress"])
        del st.last_review["review_after"]  # 期限発火を無効化して理由比較だけを見る
        st.tool_calls_since_review = s.REVIEW_AFTER_TOOL_CALLS
        st.tools_since_last_progress = s.REVIEW_NO_PROGRESS_TOOLS
        fire, _ = s.should_review_strategy(st, now=1001)
        self.assertFalse(fire)  # 前回と同じ理由の組では再発火しない
        st.unchanged_reread_count = s.REVIEW_UNCHANGED_REREAD_LIMIT  # 新しい兆候
        fire, reasons = s.should_review_strategy(st, now=1001)
        self.assertTrue(fire)
        self.assertIn("unchanged_reread", reasons)

    def test_review_after_tool_calls_deadline_overrides_interval(self):
        st = s.ReviewState(turn_started_at=1000)
        st.note_review(valid_continue(), ["no_progress"])  # review_after.tool_calls=4
        st.tool_calls_since_review = 4  # 最小間隔(6)未満でも期限到達で発火
        fire, reasons = s.should_review_strategy(st, now=1001)
        self.assertTrue(fire)
        self.assertEqual(reasons, ["review_after_due"])

    def test_review_after_max_seconds_deadline(self):
        st = s.ReviewState(turn_started_at=1000)
        st.note_review(valid_continue(), ["no_progress"])
        st.last_review_at = 1000
        fire, reasons = s.should_review_strategy(st, now=1000 + 301)
        self.assertTrue(fire)
        self.assertEqual(reasons, ["review_after_due"])


class TestParseReviewOutput(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(s.parse_review_output('{"decision": "continue"}'),
                         {"decision": "continue"})

    def test_json_in_code_fence_with_preamble(self):
        raw = "了解しました。\n```json\n{\"decision\": \"adjust\"}\n```\n以上です。"
        self.assertEqual(s.parse_review_output(raw), {"decision": "adjust"})

    def test_garbage_returns_none(self):
        self.assertIsNone(s.parse_review_output("考え中です..."))
        self.assertIsNone(s.parse_review_output(""))
        self.assertIsNone(s.parse_review_output("{broken"))

    def test_non_object_json_returns_none(self):
        self.assertIsNone(s.parse_review_output('["continue"]'))


class TestValidateReviewDecision(unittest.TestCase):
    def test_valid_continue_accepted(self):
        ok, problem = s.validate_review_decision(valid_continue())
        self.assertTrue(ok, problem)

    def test_decision_is_normalized_to_lowercase(self):
        r = valid_continue()
        r["decision"] = "CONTINUE"
        ok, _ = s.validate_review_decision(r)
        self.assertTrue(ok)
        self.assertEqual(r["decision"], "continue")

    def test_continue_without_evidence_rejected(self):
        r = valid_continue()
        r["evidence"] = []
        ok, problem = s.validate_review_decision(r)
        self.assertFalse(ok)
        self.assertIn("evidence", problem)

    def test_continue_without_review_after_rejected(self):
        r = valid_continue()
        r["review_after"] = {}
        ok, problem = s.validate_review_decision(r)
        self.assertFalse(ok)
        self.assertIn("review_after", problem)

    def test_continue_without_failure_means_rejected(self):
        r = valid_continue()
        del r["next_step"]["failure_means"]
        ok, problem = s.validate_review_decision(r)
        self.assertFalse(ok)
        self.assertIn("failure_means", problem)

    def test_adjust_requires_action(self):
        ok, _ = s.validate_review_decision(
            {"decision": "adjust", "assessment": "手順を変える",
             "next_step": {"action": "先にテストを書く"}})
        self.assertTrue(ok)
        ok, problem = s.validate_review_decision(
            {"decision": "adjust", "assessment": "手順を変える"})
        self.assertFalse(ok)
        self.assertIn("action", problem)

    def test_stop_needs_only_assessment(self):
        ok, _ = s.validate_review_decision(
            {"decision": "stop", "assessment": "必要な情報が無く推測でしか進められない"})
        self.assertTrue(ok)

    def test_unknown_decision_rejected(self):
        ok, problem = s.validate_review_decision(
            {"decision": "maybe", "assessment": "x"})
        self.assertFalse(ok)
        self.assertIn("decision", problem)


class TestRunReviewPass(unittest.TestCase):
    def setUp(self):
        self.orig_ask = s.ollama_ask

    def tearDown(self):
        s.ollama_ask = self.orig_ask

    def test_valid_first_try(self):
        s.ollama_ask = FakeOllama([json.dumps(valid_continue(), ensure_ascii=False)])
        review, retries = s.run_review_pass("m", "context")
        self.assertEqual(review["decision"], "continue")
        self.assertEqual(retries, 0)

    def test_broken_then_valid_costs_one_retry(self):
        fake = FakeOllama(["考えてみます...",
                           json.dumps(valid_continue(), ensure_ascii=False)])
        s.ollama_ask = fake
        review, retries = s.run_review_pass("m", "context")
        self.assertEqual(review["decision"], "continue")
        self.assertEqual(retries, 1)
        self.assertIn("問題:", fake.calls[1])  # 修正要求に問題点を含める

    def test_twice_broken_gives_up(self):
        s.ollama_ask = FakeOllama(default="やはり考え中です")
        review, retries = s.run_review_pass("m", "context")
        self.assertIsNone(review)
        self.assertEqual(retries, 1)


class TestBuildReviewContext(unittest.TestCase):
    def test_includes_counters_and_last_user_instruction(self):
        msgs = [
            {"role": "user", "content": "PicoCalc向けテキストエディタを作成してください"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a.h"}}}]},
            {"role": "tool", "content": "header content"},
            {"role": "user", "content": s.WORK_STATE_PREFIX + "(ダッシュボード)"},
        ]
        st = s.ReviewState(turn_started_at=1000)
        st.tools_since_last_progress = 9
        ctx = s.build_review_context(msgs, st, now=1010)
        self.assertIn("テキストエディタ", ctx)
        self.assertNotIn("ダッシュボード", ctx)  # 使い捨てメッセージは指示扱いしない
        self.assertIn("進捗イベントなしのツール呼び出し: 9", ctx)
        self.assertIn("read_file", ctx)
        self.assertIn("(まだ無い)", ctx)  # 変更ファイルなしを明示

    def test_includes_previous_review_and_outcome(self):
        st = s.ReviewState(turn_started_at=1000)
        st.note_review(valid_continue(), ["no_progress"])
        st.tool_calls_since_review = 4
        ctx = s.build_review_context([], st, now=1010)
        self.assertIn("前回の判定: CONTINUE", ctx)
        self.assertIn("不一致の有無が確定する", ctx)
        self.assertIn("ツール呼び出し: 4回", ctx)


class TestFindLastStrategyReview(unittest.TestCase):
    def test_none_when_no_meta(self):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"}]
        self.assertIsNone(s.find_last_strategy_review(msgs))

    def test_returns_last_of_multiple(self):
        m1 = s.make_review_meta(valid_continue(), ["no_progress"], 4)
        stop = {"decision": "stop", "assessment": "情報不足",
                "plan": ["CMakeLists.txtとmain.cを1回で作成する"]}
        m2 = s.make_review_meta(stop, ["many_tool_calls"], 4)
        msgs = [{"role": "user", "content": "x"}, m1,
                {"role": "assistant", "content": "y"}, m2,
                {"role": "user", "content": "続けて"}]
        found = s.find_last_strategy_review(msgs)
        self.assertIs(found, m2)

    def test_ignores_other_meta_types(self):
        msgs = [{"role": "localcoder_meta", "meta_type": "something_else"}]
        self.assertIsNone(s.find_last_strategy_review(msgs))


class TestSeedReviewStateFromHistory(unittest.TestCase):
    """ターン跨ぎの前回判定引き継ぎ(§8)。実障害: 前ターンのSTOPが具体的な計画
    (CMakeLists.txt+main.cを1回で作成)を残したのに、次ターンでReviewStateが
    真っさらに戻り、モデルから判定が見えず同じ調査ループを繰り返した。"""

    def test_seeds_last_review_and_marks_seeded(self):
        stop = {"decision": "stop", "assessment": "情報収集は完了、コード未作成",
                "plan": ["CMakeLists.txtとmain.cの最小構成を1回で作成する"],
                "review_after": {"tool_calls": 0, "max_seconds": 0}}
        msgs = [{"role": "user", "content": "作って"},
                s.make_review_meta(stop, ["many_tool_calls", "no_progress"], 4),
                {"role": "user", "content": "実装を開始してください"}]
        rev = s.ReviewState(turn_started_at=5000)
        s.seed_review_state_from_history(rev, msgs)
        self.assertTrue(rev.last_review_seeded)
        self.assertEqual(rev.last_review["decision"], "stop")
        # 期限の時計はこのターンの開始から数え直す(created_atではない)
        self.assertEqual(rev.last_review_at, 5000)
        # 同一理由の再発火抑止は引き継がない(新ターンでの正当な再発火を塞がない)
        self.assertEqual(rev.last_fire_reasons, [])
        self.assertEqual(rev.reviews_done, 0)

    def test_no_meta_leaves_state_untouched(self):
        rev = s.ReviewState(turn_started_at=5000)
        s.seed_review_state_from_history(rev, [{"role": "user", "content": "x"}])
        self.assertIsNone(rev.last_review)
        self.assertFalse(rev.last_review_seeded)

    def test_seeded_deadline_fires_across_turns(self):
        """前ターンのCONTINUEのreview_after(tool_calls=4)が、新ターンでも
        4ツール後に強制発火する。"""
        msgs = [s.make_review_meta(valid_continue(), ["no_progress"], 4),
                {"role": "user", "content": "続けてください"}]
        rev = s.ReviewState(turn_started_at=5000)
        s.seed_review_state_from_history(rev, msgs)
        for _ in range(3):
            rev.note_tool_result("read_file", "content", 0)
        fire, _ = s.should_review_strategy(rev, now=5001)
        self.assertFalse(fire)  # まだ3ツール(期限4未満)、スコアも0
        rev.note_tool_result("read_file", "content", 0)
        fire, reasons = s.should_review_strategy(rev, now=5001)
        self.assertTrue(fire)
        self.assertEqual(reasons, ["review_after_due"])

    def test_stop_with_zero_review_after_does_not_force_fire(self):
        stop = {"decision": "stop", "assessment": "停止",
                "review_after": {"tool_calls": 0, "max_seconds": 0}}
        msgs = [s.make_review_meta(stop, ["no_progress"], 4)]
        rev = s.ReviewState(turn_started_at=5000)
        s.seed_review_state_from_history(rev, msgs)
        rev.note_tool_result("read_file", "content", 0)
        fire, _ = s.should_review_strategy(rev, now=5001)
        self.assertFalse(fire)

    def test_seeded_max_seconds_counts_from_turn_start(self):
        """古いセッションを何時間も後に再開しても、max_seconds期限は
        ターン開始からの経過で判定される(即時発火しない)。"""
        old = valid_continue()  # max_seconds=300
        meta = s.make_review_meta(old, ["no_progress"], 4)
        meta["created_at"] = 1000.0  # 実際の判定はずっと昔
        rev = s.ReviewState(turn_started_at=100000.0)
        s.seed_review_state_from_history(rev, [meta])
        fire, _ = s.should_review_strategy(rev, now=100010.0)  # ターン開始10秒後
        self.assertFalse(fire)
        fire, reasons = s.should_review_strategy(rev, now=100000.0 + 301)
        self.assertTrue(fire)
        self.assertEqual(reasons, ["review_after_due"])

    def test_new_review_in_turn_clears_seeded_flag(self):
        msgs = [s.make_review_meta(valid_continue(), ["no_progress"], 4)]
        rev = s.ReviewState(turn_started_at=5000)
        s.seed_review_state_from_history(rev, msgs)
        self.assertTrue(rev.last_review_seeded)
        rev.note_review({"decision": "adjust", "assessment": "調整"}, ["no_progress"])
        self.assertFalse(rev.last_review_seeded)


class TestMetaAndFormatting(unittest.TestCase):
    def test_make_review_meta_shape(self):
        meta = s.make_review_meta(valid_continue(), ["no_progress"], 4)
        self.assertEqual(meta["role"], "localcoder_meta")
        self.assertEqual(meta["meta_type"], "strategy_review")
        self.assertEqual(meta["trigger"], {"score": 4, "reasons": ["no_progress"]})
        self.assertIn("CONTINUE", meta["content"])

    def test_dashboard_format_contains_decision_and_deadline(self):
        text = s.format_review_for_dashboard(valid_continue())
        self.assertIn("判定: CONTINUE", text)
        self.assertIn("次に行うこと: 残る依存候補を検証する", text)
        self.assertIn("あと4ツール", text)
        self.assertIn("300秒後", text)
        self.assertIn("現在の実行方針", text)
        self.assertNotIn("前ターンの判定", text)

    def test_dashboard_format_shows_plan_and_completion_criteria(self):
        """実障害でSTOPのplan(CMakeLists.txt+main.cを1回で作成)が表示対象外
        だったため、モデルに伝わらなかった。planと完了条件も表示する。"""
        r = valid_continue()
        r["plan"] = ["CMakeLists.txtを作成する", "main.cを作成しビルドする"]
        r["completion_criteria"] = ["*.uf2が生成される"]
        text = s.format_review_for_dashboard(r)
        self.assertIn("計画:", text)
        self.assertIn("1. CMakeLists.txtを作成する", text)
        self.assertIn("2. main.cを作成しビルドする", text)
        self.assertIn("完了条件:", text)
        self.assertIn("- *.uf2が生成される", text)

    def test_dashboard_format_labels_previous_turn_review(self):
        text = s.format_review_for_dashboard(valid_continue(),
                                             from_previous_turn=True)
        self.assertIn("前ターンの判定", text)
        self.assertIn("新しい指示を優先", text)
        self.assertNotIn("現在の実行方針", text)

    def test_to_ollama_messages_filters_meta(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            s.make_review_meta(valid_continue(), [], 4),
            {"role": "assistant", "content": "a"},
            {"role": "tool", "content": "t"},
        ]
        filtered = s.to_ollama_messages(msgs)
        self.assertEqual([m["role"] for m in filtered],
                         ["system", "user", "assistant", "tool"])


if __name__ == "__main__":
    unittest.main()
