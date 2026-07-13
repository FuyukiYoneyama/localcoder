"""作業状態ダッシュボード(build_work_state)の単体テスト。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402


def tool_call(name, args):
    return {"role": "assistant", "tool_calls": [
        {"function": {"name": name, "arguments": args}}]}


def tool_result(content):
    return {"role": "tool", "content": content}


class TestChangedFilesLine(unittest.TestCase):
    def test_lists_successful_writes(self):
        msgs = [tool_call("write_file", {"path": "a.py"}),
                tool_result("OK: wrote 1 chars to a.py")]
        out = s.build_work_state(msgs)
        self.assertIn("変更したファイル: a.py", out)

    def test_omits_failed_writes(self):
        msgs = [tool_call("write_file", {"path": "a.py"}),
                tool_result("ERROR: missing required argument 'content' for tool 'write_file'.")]
        out = s.build_work_state(msgs)
        self.assertNotIn("変更したファイル", out)


class TestRepeatedFailedCommandWarning(unittest.TestCase):
    def test_warns_on_n_consecutive_identical_failures(self):
        msgs = []
        for _ in range(s.FAIL_REPEAT_THRESHOLD):
            msgs += [tool_call("run_command", {"command": "make"}),
                     tool_result("exit_code=1\nerror")]
        out = s.build_work_state(msgs)
        self.assertIn("同じコマンド", out)
        self.assertIn("make", out)

    def test_no_warning_if_last_attempt_succeeded(self):
        msgs = []
        for _ in range(s.FAIL_REPEAT_THRESHOLD - 1):
            msgs += [tool_call("run_command", {"command": "make"}),
                     tool_result("exit_code=1\nerror")]
        msgs += [tool_call("run_command", {"command": "make"}),
                 tool_result("exit_code=0\nok")]
        out = s.build_work_state(msgs)
        self.assertNotIn("同じコマンド", out)


class TestRepeatedToolCallWarning(unittest.TestCase):
    """成功していても進展のない繰り返し(同じファイルの再読など)への警告。"""

    def test_warns_on_identical_repeated_calls_even_if_successful(self):
        msgs = []
        for _ in range(s.FAIL_REPEAT_THRESHOLD):
            msgs += [tool_call("read_file", {"path": "main.c"}),
                     tool_result("same content every time")]
        out = s.build_work_state(msgs)
        self.assertIn("繰り返しています", out)
        self.assertIn("read_file", out)

    def test_no_warning_for_varied_calls(self):
        msgs = []
        for p in ("a.c", "b.c", "c.c"):
            msgs += [tool_call("read_file", {"path": p}), tool_result("x")]
        out = s.build_work_state(msgs)
        self.assertNotIn("繰り返しています", out)


class TestEmptyDashboard(unittest.TestCase):
    def test_no_tool_calls_yields_empty_string(self):
        self.assertEqual(s.build_work_state([{"role": "user", "content": "hi"}]), "")


if __name__ == "__main__":
    unittest.main()
