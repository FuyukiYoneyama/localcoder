"""履歴自動圧縮(compact_history一式)の回帰テスト。

固定入力での単体テストに加え、tests/fixtures/ の実障害セッション(パスのみ匿名化
した実データ)を使って、各バグの再発を検知する回帰テストを含む。Ollamaは一切
使わず、_helpers.FakeOllamaで置き換える。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as s  # noqa: E402

from _helpers import FakeOllama, load_fixture_messages  # noqa: E402


def user(content):
    return {"role": "user", "content": content}


def assistant(content=""):
    return {"role": "assistant", "content": content}


class TestDedupeToolResults(unittest.TestCase):
    def test_keeps_latest_occurrence_only(self):
        big = "X" * 600
        msgs = [
            {"role": "tool", "content": big},
            assistant("a"),
            {"role": "tool", "content": big},
        ]
        changed = s.dedupe_tool_results(msgs)
        self.assertTrue(changed)
        self.assertIn("省略", msgs[0]["content"])
        self.assertEqual(msgs[2]["content"], big)

    def test_short_results_are_left_alone(self):
        msgs = [{"role": "tool", "content": "short"}, {"role": "tool", "content": "short"}]
        changed = s.dedupe_tool_results(msgs)
        self.assertFalse(changed)
        self.assertEqual(msgs[0]["content"], "short")

    def test_regression_repeated_compaction_fixture(self):
        """実際に「同じファイルを読み直し続けて圧縮が頻発した」セッションで、
        重複除去だけでどれだけトークンが減るかを確認する。"""
        msgs = [{"role": "system", "content": "x" * 3000}] + \
            load_fixture_messages("repeated_compaction.json")
        before = s.estimate_tokens(msgs)
        s.dedupe_tool_results(msgs)
        after = s.estimate_tokens(msgs)
        self.assertLess(after, before * 0.8,
                         "重複除去による削減が想定より小さい(実測では約4割減)")


class TestTrimOldToolResults(unittest.TestCase):
    def test_trims_all_but_recent_tool_results(self):
        msgs = [{"role": "tool", "content": "y" * 1000} for _ in range(s.KEEP_RECENT_TOOLS + 2)]
        s.trim_old_tool_results(msgs)
        trimmed = [m for m in msgs if "切り詰め" in m["content"]]
        self.assertEqual(len(trimmed), 2)
        for m in msgs[-s.KEEP_RECENT_TOOLS:]:
            self.assertNotIn("切り詰め", m["content"])


class TestMarkerRoundtrip(unittest.TestCase):
    def test_build_and_parse_roundtrip(self):
        marker = s.build_marker("これまでの要約テキスト", ["a.py", "b.py"])
        self.assertTrue(marker.startswith(s.MARKER_SUMMARY))
        summary, files, pinned, goal = s._parse_marker(marker)
        self.assertEqual(summary, "これまでの要約テキスト")
        self.assertEqual(files, ["a.py", "b.py"])
        self.assertEqual(pinned, [])
        self.assertIsNone(goal)

    def test_build_and_parse_roundtrip_with_pinned(self):
        marker = s.build_marker("要約", ["a.py"], ["外部送信しないでください"])
        summary, files, pinned, goal = s._parse_marker(marker)
        self.assertEqual(summary, "要約")
        self.assertEqual(files, ["a.py"])
        self.assertEqual(pinned, ["外部送信しないでください"])
        self.assertIsNone(goal)

    def test_build_and_parse_roundtrip_with_goal(self):
        marker = s.build_marker("要約", ["a.py"], ["固定指示X"], "既存APIとの互換性を保つ")
        summary, files, pinned, goal = s._parse_marker(marker)
        self.assertEqual(summary, "要約")
        self.assertEqual(files, ["a.py"])
        self.assertEqual(pinned, ["固定指示X"])
        self.assertEqual(goal, "既存APIとの互換性を保つ")

    def test_goal_only_no_files_no_pinned(self):
        """ファイル一覧・固定指示が空でもゴールだけは正しくパースできる
        (partitionの順序: 要約→ファイル→固定指示→ゴール、途中が空の場合の分岐)。
        """
        marker = s.build_marker("要約", [], [], "ゴールだけのケース")
        summary, files, pinned, goal = s._parse_marker(marker)
        self.assertEqual(summary, "要約")
        self.assertEqual(files, [])
        self.assertEqual(pinned, [])
        self.assertEqual(goal, "ゴールだけのケース")

    def test_pinned_only_no_files(self):
        """ファイル一覧が空でも固定指示だけは正しくパースできる
        (partitionの順序: 要約→ファイル→固定指示、ファイルが無い場合の分岐)。
        """
        marker = s.build_marker("要約", [], ["覚えておいてください: XXX"])
        summary, files, pinned, goal = s._parse_marker(marker)
        self.assertEqual(summary, "要約")
        self.assertEqual(files, [])
        self.assertEqual(pinned, ["覚えておいてください: XXX"])
        self.assertIsNone(goal)

    def test_failed_marker_uses_omit_prefix(self):
        marker = s.build_marker("(失敗)", [], failed=True)
        self.assertTrue(marker.startswith(s.MARKER_OMIT))
        summary, files, pinned, goal = s._parse_marker(marker)
        self.assertEqual(summary, "(失敗)")
        self.assertEqual(files, [])
        self.assertEqual(pinned, [])
        self.assertIsNone(goal)

    def test_non_marker_content_returns_none(self):
        summary, files, pinned, goal = s._parse_marker("普通のメッセージです")
        self.assertIsNone(summary)
        self.assertEqual(files, [])
        self.assertEqual(pinned, [])
        self.assertIsNone(goal)


class TestExtractGoalLine(unittest.TestCase):
    """IMPROVEMENTS.md §3.1: 要約LLM出力先頭のGOAL行を抽出する。"""

    def test_extracts_goal_and_strips_it_from_body(self):
        text = "GOAL: 既存APIとの互換性を保つ\n本文の続き\n2行目"
        body, goal = s._extract_goal_line(text)
        self.assertEqual(goal, "既存APIとの互換性を保つ")
        self.assertEqual(body, "本文の続き\n2行目")

    def test_no_goal_line_returns_original_text_unchanged(self):
        body, goal = s._extract_goal_line("GOALの行が無い普通の要約本文")
        self.assertIsNone(goal)
        self.assertEqual(body, "GOALの行が無い普通の要約本文")

    def test_empty_goal_value_is_none(self):
        body, goal = s._extract_goal_line("GOAL: \n本文")
        self.assertIsNone(goal)
        self.assertEqual(body, "本文")


class TestExtractOriginalUserGoal(unittest.TestCase):
    """ゴールをユーザーの最初の発言そのものから機械的に(言い換えずに)取り出す。

    実障害: 圧縮のたびに要約LLMへゴールを判定し直させていたところ、探索中に
    見つけた無関係な既存プロジェクトが再要約のたびに「ビルド対象」として
    ゴールへ紛れ込み、存在しない対象を3時間近く探し続けた。原文をそのまま
    使うことで言い換えによるすり替えを構造的に防ぐ。
    """

    def test_returns_first_user_message_verbatim(self):
        msgs = [user("PicoCalc向けエディタを作ってください"), assistant("了解")]
        self.assertEqual(s.extract_original_user_goal(msgs),
                         "PicoCalc向けエディタを作ってください")

    def test_skips_empty_and_nudge_messages(self):
        msgs = [user(s.EMPTY_RESPONSE_NUDGE), user(s.UNFINISHED_RESPONSE_NUDGE),
                user("   "), user("本物の最初の指示")]
        self.assertEqual(s.extract_original_user_goal(msgs), "本物の最初の指示")

    def test_skips_existing_compaction_markers(self):
        marker = s.build_marker("要約", [], [], "以前のゴール")
        msgs = [{"role": "user", "content": marker}, user("新しい実質的な発言")]
        self.assertEqual(s.extract_original_user_goal(msgs), "新しい実質的な発言")

    def test_returns_none_when_no_real_user_message(self):
        msgs = [assistant("a"), {"role": "tool", "content": "x"}]
        self.assertIsNone(s.extract_original_user_goal(msgs))

    def test_truncates_very_long_original_message(self):
        long_text = "x" * (s.GOAL_MAX_CHARS + 500)
        msgs = [user(long_text)]
        goal = s.extract_original_user_goal(msgs)
        self.assertLessEqual(len(goal), s.GOAL_MAX_CHARS + 100)
        self.assertTrue(goal.startswith("x" * 100))


class TestCompactHistoryGoalImmutability(unittest.TestCase):
    """compact_historyがゴールをLLMに言い換えさせず、一度確定したら不変のまま
    引き継ぐことを直接検証する(TestCompactHistoryHysteresisの統合テストより
    ピンポイント)。"""

    def setUp(self):
        self._orig_estimate = s.estimate_tokens
        self._orig_ask = s.ollama_ask
        self._orig_keep = s.KEEP_RECENT_MSGS
        s.KEEP_RECENT_MSGS = 2

    def tearDown(self):
        s.estimate_tokens = self._orig_estimate
        s.ollama_ask = self._orig_ask
        s.KEEP_RECENT_MSGS = self._orig_keep

    def test_llm_goal_line_is_ignored_on_first_compaction(self):
        s.estimate_tokens = lambda m: 999999
        sysm = {"role": "system", "content": "sys"}
        original = "demo2にPicoCalc向けテキストエディタを新規作成してください"
        old = [user(original), assistant("a")] + [user("u"), assistant("a")] * 3
        recent = [user("r1"), assistant("r2")]
        s.ollama_ask = FakeOllama(default="GOAL: 別の無関係なプロジェクトを探す\n要約")
        out = s.compact_history([sysm] + old + recent, "model", lambda x: None)
        _, _, _, goal = s._parse_marker(out[1]["content"])
        self.assertEqual(goal, original)


class TestArchiveRawMessages(unittest.TestCase):
    """圧縮で要約に置き換えられる直前の生ログを、コンテキストとは別に
    history/raw/<sid>.jsonlへ残す(analyze-without-losing-detailの要望)。"""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_raw_dir = s.RAW_HISTORY_DIR
        s.RAW_HISTORY_DIR = Path(self._tmpdir.name)
        self._orig_estimate = s.estimate_tokens
        self._orig_ask = s.ollama_ask
        self._orig_keep = s.KEEP_RECENT_MSGS
        s.KEEP_RECENT_MSGS = 2

    def tearDown(self):
        s.RAW_HISTORY_DIR = self._orig_raw_dir
        s.estimate_tokens = self._orig_estimate
        s.ollama_ask = self._orig_ask
        s.KEEP_RECENT_MSGS = self._orig_keep
        self._tmpdir.cleanup()

    def test_archive_raw_messages_appends_jsonl(self):
        s.archive_raw_messages("mysid", [user("a"), assistant("b")])
        s.archive_raw_messages("mysid", [user("c")])
        path = s.RAW_HISTORY_DIR / "mysid.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        import json
        self.assertEqual(json.loads(lines[0]), user("a"))
        self.assertEqual(json.loads(lines[2]), user("c"))

    def test_archive_raw_messages_noop_for_empty_list(self):
        s.archive_raw_messages("mysid", [])
        self.assertFalse((s.RAW_HISTORY_DIR / "mysid.jsonl").exists())

    def test_compact_history_archives_the_portion_it_discards(self):
        s.estimate_tokens = lambda m: 999999
        sysm = {"role": "system", "content": "sys"}
        old = [user("original goal"), assistant("a")] + [user("u"), assistant("a")] * 3
        recent = [user("r1"), assistant("r2")]
        s.ollama_ask = FakeOllama(default="要約")
        s.compact_history([sysm] + old + recent, "model", lambda x: None, sid="mysid")

        path = s.RAW_HISTORY_DIR / "mysid.jsonl"
        self.assertTrue(path.is_file())
        import json
        archived = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        # recent(直近KEEP_RECENT_MSGS件)はまだ圧縮されていないのでアーカイブされず、
        # oldの部分(要約に置き換えられた分)だけがアーカイブされる
        self.assertEqual(archived, old)

    def test_compact_history_without_sid_does_not_archive(self):
        s.estimate_tokens = lambda m: 999999
        sysm = {"role": "system", "content": "sys"}
        old = [user("original goal"), assistant("a")] + [user("u"), assistant("a")] * 3
        recent = [user("r1"), assistant("r2")]
        s.ollama_ask = FakeOllama(default="要約")
        s.compact_history([sysm] + old + recent, "model", lambda x: None)
        self.assertEqual(list(s.RAW_HISTORY_DIR.iterdir()), [])


class TestExtractCurrentGoal(unittest.TestCase):
    """IMPROVEMENTS.md §3.1: 圧縮マーカーに保持された現在のゴールを取り出す。"""

    def test_returns_goal_from_marker_at_start(self):
        marker = s.build_marker("要約", [], [], "既存APIとの互換性を保つ")
        msgs = [{"role": "user", "content": marker}]
        self.assertEqual(s.extract_current_goal(msgs), "既存APIとの互換性を保つ")

    def test_none_when_no_compaction_has_happened_yet(self):
        msgs = [{"role": "user", "content": "PicoCalc向けエディタを作ってください"}]
        self.assertIsNone(s.extract_current_goal(msgs))

    def test_none_for_empty_messages(self):
        self.assertIsNone(s.extract_current_goal([]))


class TestExtractPinnedInstructions(unittest.TestCase):
    def test_detects_oboete_trigger(self):
        msgs = [{"role": "user", "content": "これは覚えておいてください: 外部送信禁止"}]
        self.assertEqual(s.extract_pinned_instructions(msgs),
                         ["これは覚えておいてください: 外部送信禁止"])

    def test_detects_wasurenaide_trigger(self):
        msgs = [{"role": "user", "content": "これは忘れないでね"}]
        self.assertEqual(s.extract_pinned_instructions(msgs), ["これは忘れないでね"])

    def test_ignores_messages_without_trigger(self):
        msgs = [{"role": "user", "content": "普通の指示です"}]
        self.assertEqual(s.extract_pinned_instructions(msgs), [])

    def test_ignores_non_user_roles(self):
        msgs = [{"role": "assistant", "content": "覚えておきます"}]
        self.assertEqual(s.extract_pinned_instructions(msgs), [])

    def test_dedups_identical_repeated_instructions(self):
        msgs = [{"role": "user", "content": "覚えておいて: X"},
                {"role": "user", "content": "覚えておいて: X"}]
        self.assertEqual(s.extract_pinned_instructions(msgs), ["覚えておいて: X"])


class TestExtractChangedFiles(unittest.TestCase):
    def _msgs(self, name, path, result):
        return [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": name, "arguments": {"path": path}}}]},
            {"role": "tool", "content": result},
        ]

    def test_successful_write_is_counted(self):
        msgs = self._msgs("write_file", "ok.py", "OK: wrote 3 chars to ok.py")
        self.assertEqual(s.extract_changed_files(msgs), ["ok.py"])

    def test_failed_write_is_not_counted(self):
        """成功と誤認させない: 圧縮マーカー・ダッシュボード双方が使う共通ロジック。
        失敗した書き込みを「変更済み」として報告すると、モデルに偽の成功情報を
        与えてしまい実際にハルシネーションを誘発した実例があった。
        """
        msgs = self._msgs("write_file", "fail.py",
                          "ERROR: missing required argument 'content' for tool 'write_file'.")
        self.assertEqual(s.extract_changed_files(msgs), [])

    def test_dedups_preserving_first_occurrence_order(self):
        msgs = (self._msgs("write_file", "a.py", "OK: wrote 1 chars to a.py")
               + self._msgs("edit_file", "b.py", "OK: replaced 1 occurrence(s) in b.py")
               + self._msgs("write_file", "a.py", "OK: wrote 2 chars to a.py"))
        self.assertEqual(s.extract_changed_files(msgs), ["a.py", "b.py"])


class TestFindPreviousRead(unittest.TestCase):
    """差分中心の再読(IMPROVEMENTS.md §6.3)のキャッシュヒット判定に使う
    find_previous_readの単体テスト。"""

    def _msgs(self, name, path, result):
        return [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": name, "arguments": {"path": path}}}]},
            {"role": "tool", "content": result},
        ]

    def test_no_prior_read_returns_none(self):
        self.assertIsNone(s.find_previous_read([], "a.py"))

    def test_returns_successful_read_result(self):
        msgs = self._msgs("read_file", "a.py", "hello world")
        self.assertEqual(s.find_previous_read(msgs, "a.py"), "hello world")

    def test_ignores_other_tools_and_other_paths(self):
        msgs = (self._msgs("write_file", "a.py", "OK: wrote 3 chars to a.py")
               + self._msgs("read_file", "b.py", "content of b"))
        self.assertIsNone(s.find_previous_read(msgs, "a.py"))

    def test_ignores_failed_read(self):
        msgs = self._msgs("read_file", "a.py", "ERROR: file not found: a.py")
        self.assertIsNone(s.find_previous_read(msgs, "a.py"))

    def test_returns_most_recent_read(self):
        msgs = (self._msgs("read_file", "a.py", "old content")
               + self._msgs("read_file", "a.py", "new content"))
        self.assertEqual(s.find_previous_read(msgs, "a.py"), "new content")


class TestFindUnverifiedChanges(unittest.TestCase):
    """IMPROVEMENTS.md §3.3: 変更後に一度もrun_commandを実行していないファイルを
    機械的に検出する(モデルが検証せず「完了」と申告する事故への対策)。
    """

    def _write(self, path, result="OK: wrote 1 chars"):
        return [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "write_file", "arguments": {"path": path}}}]},
            {"role": "tool", "content": result},
        ]

    def _run(self, cmd, result="exit_code=0\nok"):
        return [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "run_command", "arguments": {"command": cmd}}}]},
            {"role": "tool", "content": result},
        ]

    def test_write_without_followup_command_is_unverified(self):
        msgs = self._write("a.py")
        self.assertEqual(s.find_unverified_changes(msgs), ["a.py"])

    def test_write_followed_by_any_command_is_verified(self):
        """run_commandの成否は問わない。検証を試みたこと自体が重要。"""
        msgs = self._write("a.py") + self._run("make", result="exit_code=1\nerror")
        self.assertEqual(s.find_unverified_changes(msgs), [])

    def test_only_writes_after_last_command_are_unverified(self):
        msgs = self._write("a.py") + self._run("make") + self._write("b.py")
        self.assertEqual(s.find_unverified_changes(msgs), ["b.py"])

    def test_failed_write_is_not_tracked(self):
        msgs = self._write("a.py", result="ERROR: missing required argument 'content'")
        self.assertEqual(s.find_unverified_changes(msgs), [])

    def test_no_writes_means_nothing_unverified(self):
        msgs = self._run("ls")
        self.assertEqual(s.find_unverified_changes(msgs), [])

    def test_dedups_preserving_order(self):
        msgs = self._write("a.py") + self._write("b.py") + self._write("a.py")
        self.assertEqual(s.find_unverified_changes(msgs), ["a.py", "b.py"])


class TestUpdateSummary(unittest.TestCase):
    def test_single_ollama_call_for_small_input(self):
        fake = FakeOllama(default="MERGED")
        s.ollama_ask = fake
        summary, goal = s.update_summary("PREV", [user("new stuff")], "model")
        self.assertEqual(summary, "MERGED")
        self.assertIsNone(goal)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("PREV", fake.calls[0])
        self.assertIn("new stuff", fake.calls[0])

    def test_extracts_goal_line_from_response(self):
        fake = FakeOllama(default="GOAL: 既存APIとの互換性を保つ\n本文だけの要約")
        s.ollama_ask = fake
        summary, goal = s.update_summary("PREV", [user("new stuff")], "model")
        self.assertEqual(summary, "本文だけの要約")
        self.assertEqual(goal, "既存APIとの互換性を保つ")


class TestCompactHistoryHysteresis(unittest.TestCase):
    """世代劣化防止・ヒステリシス・先回り圧縮・強制圧縮の一式。

    estimate_tokensをモックして、budget/trigger/targetの境界を厳密に制御する。
    """

    def setUp(self):
        self.orig_estimate = s.estimate_tokens
        self.orig_ask = s.ollama_ask
        self.budget = s.NUM_CTX - s.RESERVE_TOKENS
        self.trigger = int(self.budget * s.PROACTIVE_COMPACT_RATIO)
        self.target = int(self.budget * s.COMPACT_TARGET_RATIO)

    def tearDown(self):
        s.estimate_tokens = self.orig_estimate
        s.ollama_ask = self.orig_ask

    def _messages(self, n_old=8, n_recent=None):
        n_recent = n_recent if n_recent is not None else s.KEEP_RECENT_MSGS
        sysm = {"role": "system", "content": "sys"}
        old = [user("u"), assistant("a")] * (n_old // 2)
        recent = [user("r"), assistant("r2")] * (max(n_recent, 2) // 2)
        return [sysm] + old + recent

    def test_below_trigger_is_a_no_op(self):
        s.estimate_tokens = lambda m: self.trigger - 100
        msgs = self._messages()
        out = s.compact_history(msgs, "model", lambda x: None)
        self.assertIs(out, msgs)

    def test_proactive_trigger_fires_under_hard_budget(self):
        """予算(budget)未満でもtrigger(90%)を超えていれば圧縮が発動する
        (空応答対策: 正式な超過を待つと手遅れになる実例があったため)。
        """
        calls = {"n": 0}

        def fake_estimate(m):
            calls["n"] += 1
            if calls["n"] == 1:
                return self.trigger + 500  # budget未満、trigger超え
            if calls["n"] == 2:
                return self.target + 500  # dedupe/trim後もtarget超え→要約へ
            return 10

        s.estimate_tokens = fake_estimate
        s.ollama_ask = FakeOllama(default="SUMMARY")
        out = s.compact_history(self._messages(), "model", lambda x: None)
        self.assertTrue(out[1]["content"].startswith(s.MARKER_SUMMARY))

    def test_force_bypasses_trigger(self):
        calls = {"n": 0}

        def fake_estimate(m):
            calls["n"] += 1
            return self.trigger - 100 if calls["n"] <= 2 else 10

        s.estimate_tokens = fake_estimate
        s.ollama_ask = FakeOllama(default="SUMMARY")
        out = s.compact_history(self._messages(), "model", lambda x: None, force=True)
        self.assertTrue(out[1]["content"].startswith(s.MARKER_SUMMARY))

    def test_no_generational_re_summarization(self):
        """圧縮済みマーカーを2回目以降に再要約しないことを確認する。
        (伝言ゲーム的な劣化を防ぐのが目的の機能)
        """
        s.KEEP_RECENT_MSGS = 2
        fake = FakeOllama(responses=["FIRST_SUMMARY"], default="MERGED")
        s.ollama_ask = fake
        calls = {"n": 0}

        def fake_estimate(m):
            calls["n"] += 1
            return 999999 if calls["n"] <= 2 else 10

        s.estimate_tokens = fake_estimate
        sysm = {"role": "system", "content": "sys"}
        old = [user("u"), assistant("a")] * 4
        recent = [user("r1"), assistant("r2")]

        out1 = s.compact_history([sysm] + old + recent, "model", lambda x: None)
        marker1 = out1[1]["content"]
        self.assertTrue(marker1.startswith(s.MARKER_SUMMARY))
        self.assertEqual(len(fake.calls), 1)  # 初回はsummarize 1回のみ

        # 2回目の圧縮: old[0]が前回のマーカーになる状況を再現する
        calls["n"] = 0
        old2 = [out1[1]] + [user("u2"), assistant("a2")] * 4
        out2 = s.compact_history([sysm] + old2 + recent, "model", lambda x: None)
        marker2 = out2[1]["content"]
        summary2, _files2, _pinned2, _goal2 = s._parse_marker(marker2)
        self.assertEqual(summary2, "MERGED")
        self.assertEqual(len(fake.calls), 2)  # 生ログの再要約ではなく統合1回だけ追加

    def test_pinned_instructions_survive_across_compactions(self):
        """「覚えておいて」等の発言は、要約本文とは別ブロックで複数回の圧縮を
        跨いで一字一句保持される(IMPROVEMENTS.md §3.2)。
        """
        s.KEEP_RECENT_MSGS = 2
        s.ollama_ask = FakeOllama(default="SUMMARY")
        calls = {"n": 0}

        def fake_estimate(m):
            calls["n"] += 1
            return 999999 if calls["n"] <= 2 else 10

        s.estimate_tokens = fake_estimate
        sysm = {"role": "system", "content": "sys"}
        old = [user("覚えておいて: 外部送信は絶対にしないでください"), assistant("a")] * 4
        recent = [user("r1"), assistant("r2")]

        out1 = s.compact_history([sysm] + old + recent, "model", lambda x: None)
        _summary1, _files1, pinned1, _goal1 = s._parse_marker(out1[1]["content"])
        self.assertEqual(pinned1, ["覚えておいて: 外部送信は絶対にしないでください"])

        # 2回目の圧縮でも消えない(新規分に固定指示が無くても前回分を引き継ぐ)
        calls["n"] = 0
        old2 = [out1[1]] + [user("u2"), assistant("a2")] * 4
        out2 = s.compact_history([sysm] + old2 + recent, "model", lambda x: None)
        _summary2, _files2, pinned2, _goal2 = s._parse_marker(out2[1]["content"])
        self.assertEqual(pinned2, ["覚えておいて: 外部送信は絶対にしないでください"])

    def test_goal_is_original_user_message_and_immutable_across_compactions(self):
        """ゴールはユーザーの最初の発言そのもの(機械抽出、言い換えなし)であり、
        以後の圧縮でLLMが別のGOAL:行を出力しても変わらない(IMPROVEMENTS.md §3.1)。

        実障害: 圧縮のたびにLLMへゴールを判定し直させていたところ、探索中に
        見つけた無関係な既存プロジェクトが再要約のたびに「ビルド対象」として
        ゴールへ紛れ込み、存在しない対象を3時間近く探し続ける事故があった。
        言い換え自体が原因だったため、原文を不変のまま保持する設計へ変更した。
        """
        s.KEEP_RECENT_MSGS = 2
        calls = {"n": 0}

        def fake_estimate(m):
            calls["n"] += 1
            return 999999 if calls["n"] <= 2 else 10

        s.estimate_tokens = fake_estimate
        sysm = {"role": "system", "content": "sys"}
        original_request = "既存APIとの互換性を保ったまま新機能を追加してほしい"
        old = [user(original_request), assistant("a")] + [user("u"), assistant("a")] * 3
        recent = [user("r1"), assistant("r2")]

        # LLMがGOAL:行に何を出力しても(あるいは出力しなくても)無視され、
        # ユーザーの最初の発言そのものが機械的にゴールとして採用される。
        s.ollama_ask = FakeOllama(default="GOAL: 無関係な別プロジェクトをビルドする\n要約本文")
        out1 = s.compact_history([sysm] + old + recent, "model", lambda x: None)
        _s1, _f1, _p1, goal1 = s._parse_marker(out1[1]["content"])
        self.assertEqual(goal1, original_request)

        # 2回目の圧縮でLLMが全く別のGOAL:行を出力しても、ゴールは最初の発言の
        # ままで変わらない(要約LLMには言い換えさせない)。
        calls["n"] = 0
        s.ollama_ask = FakeOllama(default="GOAL: 探索中に見つけた別の既存デモ\n要約本文2")
        old2 = [out1[1]] + [user("u2"), assistant("a2")] * 4
        out2 = s.compact_history([sysm] + old2 + recent, "model", lambda x: None)
        _s2, _f2, _p2, goal2 = s._parse_marker(out2[1]["content"])
        self.assertEqual(goal2, original_request)


class TestEmptyResponseNearBudgetFixture(unittest.TestCase):
    """実際に「予算の99%まで会話が伸び、空応答が2回連続した」セッションを使い、
    新しいproactiveしきい値なら空応答が起きる前に圧縮が発動していたことを検証する。
    """

    def test_proactive_trigger_fires_before_the_empty_response(self):
        msgs = load_fixture_messages("empty_response_near_budget.json")
        sysm = {"role": "system", "content": "x" * 3000}
        budget = s.NUM_CTX - s.RESERVE_TOKENS
        trigger = int(budget * s.PROACTIVE_COMPACT_RATIO)

        empty_idx = next(
            i for i, m in enumerate(msgs)
            if m.get("role") == "assistant" and not (m.get("content") or "").strip()
            and not m.get("tool_calls"))

        trigger_idx = next(
            (i for i in range(len(msgs))
             if s.estimate_tokens([sysm] + msgs[:i + 1]) > trigger),
            None)

        self.assertIsNotNone(trigger_idx, "このfixtureではtriggerを超えないはず(想定外)")
        self.assertLess(trigger_idx, empty_idx,
                         "先回り圧縮のtriggerが、実際に空応答が起きた地点より後になっている")


if __name__ == "__main__":
    unittest.main()
