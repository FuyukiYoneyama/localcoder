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

    def test_write_file_reports_resolved_absolute_path_not_raw_input(self):
        """相対パスの解釈違い(例: 先頭の"/"を落としてワークスペース起点で
        二重にネストしたパスになる)にモデル自身が次の応答で気づけるよう、
        成功メッセージは入力パスではなく解決済みの絶対パスを返す。実例:
        入力パスをそのままエコーしていた時は、この不一致に誰も気づかず
        意図しない場所へ書き込み続けた。"""
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ctx = s.ToolContext(ws=Path(d))
            # 先頭の"/"が無い、ワークスペース名を含む紛らわしい相対パス
            result = provider.call_tool(
                "write_file", {"path": "home/user/project/a.txt", "content": "hi"}, ctx)
        expected = str((Path(d) / "home/user/project/a.txt").resolve())
        self.assertIn(expected, result)
        self.assertNotIn("home/user/project/a.txt to home/user/project/a.txt", result)

    def test_edit_file_reports_resolved_absolute_path(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ctx = s.ToolContext(ws=Path(d))
            provider.call_tool("write_file", {"path": "a.txt", "content": "hi"}, ctx)
            result = provider.call_tool(
                "edit_file", {"path": "a.txt", "old_string": "hi", "new_string": "bye"}, ctx)
        expected = str((Path(d) / "a.txt").resolve())
        self.assertIn(expected, result)

    def test_list_dir_reports_resolved_absolute_path_header(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "sub").mkdir()
            (Path(d) / "sub" / "f.txt").write_text("x")
            result = provider.call_tool("list_dir", {"path": "sub"}, s.ToolContext(ws=Path(d)))
        expected = str((Path(d) / "sub").resolve())
        self.assertTrue(result.startswith(expected + ":"))
        self.assertIn("f.txt", result)

    def test_delete_and_move_report_resolved_absolute_paths(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ctx = s.ToolContext(ws=Path(d))
            provider.call_tool("write_file", {"path": "a.txt", "content": "hi"}, ctx)
            move_result = provider.call_tool(
                "move_file", {"src": "a.txt", "dst": "b.txt"}, ctx)
            self.assertIn(str((Path(d) / "a.txt").resolve()), move_result)
            self.assertIn(str((Path(d) / "b.txt").resolve()), move_result)
            delete_result = provider.call_tool("delete_file", {"path": "b.txt"}, ctx)
            self.assertIn(str((Path(d) / "b.txt").resolve()), delete_result)

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


class TestResolvePathBoundary(unittest.TestCase):
    """resolve_pathのboundary引数(読み取りは広く・書き込みは狭く、の実現手段)。"""

    def test_boundary_none_behaves_like_before(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "sub").mkdir()
            result = s.resolve_path(ws, "sub/a.txt")
        self.assertEqual(result, (ws / "sub" / "a.txt").resolve())

    def test_path_inside_boundary_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            sub = ws / "sub"
            sub.mkdir()
            result = s.resolve_path(ws, "sub/a.txt", boundary=sub)
        self.assertEqual(result, (sub / "a.txt").resolve())

    def test_path_outside_boundary_but_inside_ws_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            sub = ws / "sub"
            sub.mkdir()
            with self.assertRaises(ValueError):
                s.resolve_path(ws, "other.txt", boundary=sub)

    def test_boundary_equal_to_path_itself_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            sub = ws / "sub"
            sub.mkdir()
            result = s.resolve_path(ws, "sub", boundary=sub)
        self.assertEqual(result, sub.resolve())


class TestWriteRootRestriction(unittest.TestCase):
    """write_root(IMPROVEMENTS.md記載の読み取り広く・書き込み狭くの実装)の
    ツール単位の挙動確認。read系は常にws全体、write系だけがwrite_rootに従う。"""

    def _ctx(self, ws, write_root=None):
        return s.ToolContext(ws=ws, write_root=write_root)

    def test_write_file_outside_write_root_is_rejected(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool("write_file", {"path": "../outside.txt", "content": "x"}, ctx)
            self.assertTrue(result.startswith("ERROR:"))
            self.assertFalse((Path(d) / "outside.txt").exists())

    def test_write_file_inside_write_root_succeeds(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool("write_file", {"path": "sub/a.txt", "content": "x"}, ctx)
            self.assertTrue(result.startswith("OK:"))
            self.assertEqual((sub / "a.txt").read_text(), "x")

    def test_edit_file_outside_write_root_is_rejected(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "a.txt").write_text("hi")
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool(
                "edit_file", {"path": "a.txt", "old_string": "hi", "new_string": "bye"}, ctx)
            self.assertTrue(result.startswith("ERROR:"))
            self.assertEqual((ws / "a.txt").read_text(), "hi")

    def test_list_dir_is_unrestricted_even_with_write_root(self):
        """list_dir(読み取り系)はwrite_rootが設定されていてもws全体を見られる。"""
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "outside.txt").write_text("x")
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool("list_dir", {"path": "."}, ctx)
            self.assertIn("outside.txt", result)

    def test_read_file_is_unrestricted_even_with_write_root(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "outside.txt").write_text("secret")
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool("read_file", {"path": "outside.txt"}, ctx)
            self.assertEqual(result, "secret")

    def test_delete_file_outside_write_root_is_rejected(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "outside.txt").write_text("x")
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool("delete_file", {"path": "outside.txt"}, ctx)
            self.assertTrue(result.startswith("ERROR:"))
            self.assertTrue((ws / "outside.txt").exists())

    def test_delete_directory_outside_write_root_is_rejected(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            other = ws / "other"
            other.mkdir()
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool("delete_directory", {"path": "other"}, ctx)
            self.assertTrue(result.startswith("ERROR:"))
            self.assertTrue(other.is_dir())

    def test_delete_directory_refuses_write_root_itself(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool("delete_directory", {"path": "sub"}, ctx)
            self.assertTrue(result.startswith("ERROR:"))
            self.assertTrue(sub.is_dir())

    def test_copy_file_src_may_come_from_outside_write_root(self):
        """copy_fileは非対称: srcはws全体から読める(参考実装の取り込みワークフロー)。"""
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "reference.txt").write_text("template")
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool(
                "copy_file", {"src": "reference.txt", "dst": "sub/copy.txt"}, ctx)
            self.assertTrue(result.startswith("OK:"))
            self.assertEqual((sub / "copy.txt").read_text(), "template")

    def test_copy_file_dst_outside_write_root_is_rejected(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "reference.txt").write_text("template")
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool(
                "copy_file", {"src": "reference.txt", "dst": "copy.txt"}, ctx)
            self.assertTrue(result.startswith("ERROR:"))
            self.assertFalse((ws / "copy.txt").exists())

    def test_move_file_src_outside_write_root_is_rejected(self):
        """move_fileはcopy_fileと違い非対称にしない: srcを取り除く=書き込みの
        一種なので、srcもwrite_root限定にする。"""
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "outside.txt").write_text("x")
            sub = ws / "sub"
            sub.mkdir()
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool(
                "move_file", {"src": "outside.txt", "dst": "sub/moved.txt"}, ctx)
            self.assertTrue(result.startswith("ERROR:"))
            self.assertTrue((ws / "outside.txt").exists())
            self.assertFalse((sub / "moved.txt").exists())

    def test_move_file_within_write_root_succeeds(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            sub = ws / "sub"
            sub.mkdir()
            (sub / "a.txt").write_text("x")
            ctx = self._ctx(ws, write_root=sub)
            result = provider.call_tool(
                "move_file", {"src": "sub/a.txt", "dst": "sub/b.txt"}, ctx)
            self.assertTrue(result.startswith("OK:"))
            self.assertTrue((sub / "b.txt").exists())

    def test_write_root_none_means_ws_wide_as_before(self):
        provider = s.BuiltinToolProvider()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            ctx = self._ctx(ws, write_root=None)
            result = provider.call_tool("write_file", {"path": "a.txt", "content": "x"}, ctx)
            self.assertTrue(result.startswith("OK:"))


if __name__ == "__main__":
    unittest.main()
