"""ツール呼び出しの正規化(sanitize_tool_name)と連続失敗検出(track_tool_repeat)、
および引数欠落時のexec_toolエラーメッセージの単体テスト。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as s  # noqa: E402

from _helpers import load_fixture_messages  # noqa: E402


class TestSanitizeToolName(unittest.TestCase):
    def test_strips_leaked_special_token(self):
        self.assertEqual(
            s.sanitize_tool_name("read_file<|tool_call_argument_begin|>"), "read_file")

    def test_leaves_normal_names_unchanged(self):
        self.assertEqual(s.sanitize_tool_name("write_file"), "write_file")

    def test_regression_leaked_special_token_fixture(self):
        """実際に<|tool_call_argument_begin|>がname欄に混入し、ツール呼び出しが
        「unknown tool」で永遠に失敗し続けたセッションからの回帰テスト。"""
        msgs = load_fixture_messages("leaked_special_token.json")
        corrupted = [
            tc["function"]["name"]
            for m in msgs for tc in (m.get("tool_calls") or [])
            if "<|" in tc["function"]["name"]
        ]
        self.assertTrue(corrupted, "fixtureに破損したツール名が含まれているはず")
        for raw in corrupted:
            self.assertEqual(s.sanitize_tool_name(raw), "read_file")


class TestTrackToolRepeat(unittest.TestCase):
    def test_resets_on_success(self):
        sig, count, stuck = s.track_tool_repeat("read_file", {"path": "a"}, "OK", None, 0)
        self.assertIsNone(sig)
        self.assertEqual(count, 0)
        self.assertFalse(stuck)

    def test_counts_consecutive_identical_failures(self):
        """TOOL_STUCK_LIMIT回目の同一失敗でちょうどstuckになる(オフバイワン回帰)。"""
        sig, count, stuck = None, 0, False
        args = {"path": "main.c"}
        for _ in range(s.TOOL_STUCK_LIMIT - 1):
            sig, count, stuck = s.track_tool_repeat(
                "write_file", args, "ERROR: missing required argument 'content'", sig, count)
            self.assertFalse(stuck)
        sig, count, stuck = s.track_tool_repeat(
            "write_file", args, "ERROR: missing required argument 'content'", sig, count)
        self.assertTrue(stuck)
        self.assertEqual(count, s.TOOL_STUCK_LIMIT)

    def test_single_failure_does_not_trigger(self):
        _, count, stuck = s.track_tool_repeat(
            "write_file", {"path": "a"}, "ERROR: x", None, 0)
        self.assertEqual(count, 1)
        self.assertFalse(stuck)

    def test_different_args_reset_the_counter(self):
        """異なる引数への失敗は既存の連続失敗を打ち切るが、それ自体は新たな
        失敗の1回目としてカウントされる(0にはならない)。"""
        sig, count, _ = s.track_tool_repeat(
            "read_file", {"path": "a"}, "ERROR: x", None, 0)
        sig, count, stuck = s.track_tool_repeat(
            "read_file", {"path": "b"}, "ERROR: x", sig, count)
        self.assertEqual(count, 1)
        self.assertFalse(stuck)

    def test_success_after_failures_resets_to_zero(self):
        sig, count, _ = s.track_tool_repeat(
            "read_file", {"path": "a"}, "ERROR: x", None, 0)
        sig, count, stuck = s.track_tool_repeat(
            "read_file", {"path": "a"}, "OK", sig, count)
        self.assertEqual(count, 0)
        self.assertFalse(stuck)

    def test_regression_stuck_write_file_fixture(self):
        """write_fileがcontent欠落で3回連続失敗した実セッションを再生し、
        TOOL_STUCK_LIMIT到達で正しく打ち切り判定されることを確認する。"""
        msgs = load_fixture_messages("stuck_write_file.json")
        calls = [
            (tc["function"]["name"], tc["function"].get("arguments") or {})
            for m in msgs for tc in (m.get("tool_calls") or [])
            if tc["function"]["name"] == "write_file"
        ]
        results = [
            m["content"] for m in msgs
            if m.get("role") == "tool" and "write_file" in str(m.get("tool_name", ""))
        ]
        self.assertEqual(len(calls), len(results))

        sig, count = None, 0
        stuck_at = None
        for i, ((name, args), result) in enumerate(zip(calls, results)):
            sig, count, stuck = s.track_tool_repeat(name, args, result, sig, count)
            if stuck and stuck_at is None:
                stuck_at = i
        self.assertIsNotNone(stuck_at, "実際のセッションでは'stuck'判定が発生している")


class TestExecToolMissingArgument(unittest.TestCase):
    def test_missing_content_gives_actionable_message(self):
        with tempfile.TemporaryDirectory() as d:
            result = s.exec_tool("write_file", {"path": "foo.txt"}, Path(d))
        self.assertTrue(result.startswith("ERROR: missing required argument"))
        self.assertIn("'content'", result)
        self.assertIn("write_file", result)

    def test_unknown_tool_name_reports_clearly(self):
        with tempfile.TemporaryDirectory() as d:
            result = s.exec_tool("no_such_tool", {}, Path(d))
        self.assertEqual(result, "ERROR: unknown tool no_such_tool")


if __name__ == "__main__":
    unittest.main()
