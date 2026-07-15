"""ToolProvider共通インターフェース(IMPROVEMENTS.md §13.2)の単体テスト。

BuiltinToolProviderへのツール実行部分の分離(§8.1の一部)と、名前ベースの
データ駆動ディスパッチ(_provider_for_tool)を検証する。exec_tool自体の挙動は
既存のtest_tool_normalization.pyで(後方互換の入口として)引き続き検証している。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402


class TestBuiltinToolProviderListTools(unittest.TestCase):
    def test_returns_the_tools_definition(self):
        self.assertIs(s.BuiltinToolProvider().list_tools(), s.TOOLS)

    def test_every_builtin_tool_name_is_listed(self):
        names = {t["function"]["name"] for t in s.BuiltinToolProvider().list_tools()}
        expected = {"run_command", "read_file", "write_file", "edit_file",
                    "list_dir", "delete_file", "delete_directory", "move_file",
                    "copy_file", "web_search", "fetch_url", "view_image"}
        self.assertEqual(names, expected)


class TestProviderForTool(unittest.TestCase):
    def test_finds_builtin_provider_for_known_tool(self):
        provider = s._provider_for_tool("read_file")
        self.assertIsInstance(provider, s.BuiltinToolProvider)

    def test_returns_none_for_unknown_tool(self):
        self.assertIsNone(s._provider_for_tool("no_such_tool"))


class TestBuiltinToolProviderCallTool(unittest.TestCase):
    def test_write_and_read_file_via_provider(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ctx = s.ToolContext(ws=Path(d))
            write_result = provider.call_tool("write_file", {"path": "a.txt", "content": "hi"}, ctx)
            self.assertTrue(write_result.startswith("OK:"))
            read_result = provider.call_tool("read_file", {"path": "a.txt"}, ctx)
            self.assertEqual(read_result, "hi")

    def test_unknown_tool_reports_clearly(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            result = provider.call_tool("no_such_tool", {}, s.ToolContext(ws=Path(d)))
        self.assertEqual(result, "ERROR: unknown tool no_such_tool")

    def test_missing_argument_reports_clearly(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            result = provider.call_tool("write_file", {"path": "a.txt"}, s.ToolContext(ws=Path(d)))
        self.assertTrue(result.startswith("ERROR: missing required argument"))
        self.assertIn("'content'", result)


class TestReadFileCacheHit(unittest.TestCase):
    """差分中心の再読(IMPROVEMENTS.md §6.3)のキャッシュヒット短絡の挙動確認。
    messages未指定時は従来通り、指定時は前回同一内容なら短いメッセージを返す。
    """

    def _read_msgs(self, path, result):
        return [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": path}}}]},
            {"role": "tool", "content": result},
        ]

    def test_messages_none_returns_full_content_as_before(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("hi")
            ctx = s.ToolContext(ws=Path(d), messages=None)
            result = provider.call_tool("read_file", {"path": "a.txt"}, ctx)
        self.assertEqual(result, "hi")

    def test_unchanged_content_returns_short_notice(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("hi")
            msgs = self._read_msgs("a.txt", "hi")
            ctx = s.ToolContext(ws=Path(d), messages=msgs)
            result = provider.call_tool("read_file", {"path": "a.txt"}, ctx)
        self.assertIn("変わっていません", result)
        self.assertIn("SHA256=", result)

    def test_changed_content_returns_full_content(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("new content")
            msgs = self._read_msgs("a.txt", "old content")
            ctx = s.ToolContext(ws=Path(d), messages=msgs)
            result = provider.call_tool("read_file", {"path": "a.txt"}, ctx)
        self.assertEqual(result, "new content")

    def test_first_read_with_messages_returns_full_content(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("hi")
            ctx = s.ToolContext(ws=Path(d), messages=[])
            result = provider.call_tool("read_file", {"path": "a.txt"}, ctx)
        self.assertEqual(result, "hi")


class TestExecToolStillWorksAsThinWrapper(unittest.TestCase):
    """exec_toolの公開シグネチャ・挙動は変えていない(後方互換)ことの確認。
    実際の分岐ロジックはBuiltinToolProvider側でカバーする。
    """

    def test_exec_tool_delegates_to_builtin_provider(self):
        with tempfile.TemporaryDirectory() as d:
            result = s.exec_tool("write_file", {"path": "a.txt", "content": "x"}, Path(d))
        self.assertTrue(result.startswith("OK:"))

    def test_exec_tool_positional_and_keyword_args_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            result = s.exec_tool("read_file", {"path": "missing.txt"}, Path(d),
                                 cancel=None, model="gpt-oss:20b",
                                 pending_images=None, sid="s1", call_id="c1")
        self.assertTrue(result.startswith("ERROR:"))

    def test_exec_tool_accepts_optional_messages_kwarg(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("hi")
            msgs = [
                {"role": "assistant", "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}]},
                {"role": "tool", "content": "hi"},
            ]
            result = s.exec_tool("read_file", {"path": "a.txt"}, Path(d), messages=msgs)
        self.assertIn("変わっていません", result)


if __name__ == "__main__":
    unittest.main()
