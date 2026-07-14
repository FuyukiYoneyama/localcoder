"""構造化ツール結果(IMPROVEMENTS.md §4.1)の単体テスト。

parse_command_resultはrun_commandの文字列結果から構造化データを逆算する
アダプタで、run_command/ToolProvider/exec_toolの契約(いずれも文字列を返す)は
変えていない。build_work_state側の判定(直近コマンドのOK/失敗表示、同一コマンド
連続失敗の警告)がこの関数を使うようになったことも合わせて検証する。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402


def tool_call(name, args):
    return {"role": "assistant", "tool_calls": [
        {"function": {"name": name, "arguments": args}}]}


def tool_result(content):
    return {"role": "tool", "content": content}


class TestParseCommandResult(unittest.TestCase):
    def test_success_exit_code_zero(self):
        meta = s.parse_command_result("exit_code=0\nhello")
        self.assertEqual(meta, {"ok": True, "exit_code": 0,
                                "timed_out": False, "cancelled": False})

    def test_failure_nonzero_exit_code(self):
        meta = s.parse_command_result("exit_code=1\nerror output")
        self.assertEqual(meta["ok"], False)
        self.assertEqual(meta["exit_code"], 1)

    def test_timed_out(self):
        meta = s.parse_command_result("ERROR: command timed out (180s)\npartial output")
        self.assertEqual(meta["ok"], False)
        self.assertIsNone(meta["exit_code"])
        self.assertTrue(meta["timed_out"])
        self.assertFalse(meta["cancelled"])

    def test_cancelled_by_user(self):
        meta = s.parse_command_result("ERROR: command cancelled by user\n")
        self.assertEqual(meta["ok"], False)
        self.assertTrue(meta["cancelled"])
        self.assertFalse(meta["timed_out"])

    def test_unknown_format_is_ok_none(self):
        """run_command以外のツール結果や壊れた形式はok=Noneで「不明」と扱う
        (Falseにすると誤って失敗扱いされてしまうため)。"""
        meta = s.parse_command_result("OK: wrote 3 chars to a.py")
        self.assertIsNone(meta["ok"])
        self.assertIsNone(meta["exit_code"])

    def test_empty_string(self):
        meta = s.parse_command_result("")
        self.assertIsNone(meta["ok"])


class TestBuildWorkStateUsesStructuredParsing(unittest.TestCase):
    """既存のrecent-commands表示・同一コマンド連続失敗の警告が、文字列prefixの
    素朴なチェックからparse_command_result経由になっても同じ結果を出すこと。
    """

    def test_recent_command_shows_ok(self):
        msgs = [tool_call("run_command", {"command": "make"}),
                tool_result("exit_code=0\nbuild succeeded")]
        out = s.build_work_state(msgs)
        self.assertIn("OK", out)
        self.assertNotIn("失敗/要確認", out)

    def test_recent_command_shows_failure(self):
        msgs = [tool_call("run_command", {"command": "make"}),
                tool_result("exit_code=1\nerror: undefined reference")]
        out = s.build_work_state(msgs)
        self.assertIn("失敗/要確認", out)

    def test_repeated_failure_warning_still_fires(self):
        msgs = []
        for _ in range(s.FAIL_REPEAT_THRESHOLD):
            msgs += [tool_call("run_command", {"command": "make"}),
                     tool_result("exit_code=1\nerror")]
        out = s.build_work_state(msgs)
        self.assertIn("同じコマンド", out)

    def test_repeated_failure_warning_not_triggered_by_timeout_then_success(self):
        msgs = ([tool_call("run_command", {"command": "make"}),
                tool_result("ERROR: command timed out (180s)\n")] * 2
               + [tool_call("run_command", {"command": "make"}),
                  tool_result("exit_code=0\nok")])
        out = s.build_work_state(msgs)
        self.assertNotIn("同じコマンド", out)


if __name__ == "__main__":
    unittest.main()
