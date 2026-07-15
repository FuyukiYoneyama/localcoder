"""可逆操作レイヤー第1段階(REVERSIBLE_OPERATIONS.md §13)の単体テスト。

原子的書き込み(atomic_write)・トランザクション台帳(Transaction)・
ロールバック/再適用(rollback_transaction/reapply_transaction)・
write_file/edit_fileツールとの結合を、Ollama無しで検証する。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402


class _WsTestCase(unittest.TestCase):
    """一時ディレクトリをワークスペースとして使う共通土台。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def txn(self) -> "s.Transaction":
        return s.Transaction(self.ws)


class TestAtomicWrite(_WsTestCase):
    def test_creates_file_with_content(self):
        f = self.ws / "a.txt"
        s.atomic_write(f, "hello")
        self.assertEqual(f.read_text(), "hello")

    def test_replaces_existing_content(self):
        f = self.ws / "a.txt"
        f.write_text("old")
        s.atomic_write(f, "new")
        self.assertEqual(f.read_text(), "new")

    def test_leaves_no_temp_file(self):
        f = self.ws / "a.txt"
        s.atomic_write(f, "x")
        leftovers = [p.name for p in self.ws.iterdir() if "localcoder-tmp" in p.name]
        self.assertEqual(leftovers, [])


class TestTransactionRecording(_WsTestCase):
    def test_lazy_no_ledger_dir_until_first_write(self):
        t = self.txn()
        self.assertFalse((self.ws / s.LEDGER_DIR_NAME).exists())
        t.finalize("completed")  # 操作ゼロならfinalizeしても何も作らない
        self.assertFalse((self.ws / s.LEDGER_DIR_NAME).exists())

    def test_existing_file_is_backed_up_with_sha(self):
        f = self.ws / "a.txt"
        f.write_text("before-content")
        t = self.txn()
        t.record_before_write(f)
        self.assertEqual(len(t.operations), 1)
        op = t.operations[0]
        self.assertEqual(op["type"], "write")
        self.assertTrue(op["existed_before"])
        backup = t.dir / "before" / "a.txt"
        self.assertEqual(backup.read_text(), "before-content")
        import hashlib
        self.assertEqual(op["before_sha256"],
                         hashlib.sha256(b"before-content").hexdigest())

    def test_same_file_recorded_only_once(self):
        f = self.ws / "a.txt"
        f.write_text("v1")
        t = self.txn()
        t.record_before_write(f)
        f.write_text("v2")
        t.record_before_write(f)
        self.assertEqual(len(t.operations), 1)
        # バックアップは最初の1回時点の内容のまま(§3)
        self.assertEqual((t.dir / "before" / "a.txt").read_text(), "v1")

    def test_new_file_records_created_dirs_deepest_first(self):
        f = self.ws / "x" / "y" / "new.txt"
        t = self.txn()
        t.record_before_write(f)
        op = t.operations[0]
        self.assertEqual(op["type"], "create")
        self.assertFalse(op["existed_before"])
        self.assertEqual(op["created_dirs"], [str(Path("x") / "y"), "x"])

    def test_manifest_written_and_finalized(self):
        f = self.ws / "a.txt"
        f.write_text("v1")
        t = self.txn()
        t.record_before_write(f)
        m = json.loads((t.dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["status"], "open")
        self.assertEqual(m["transaction_id"], t.id)
        t.finalize("completed")
        m = json.loads((t.dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["status"], "completed")

    def test_ledger_gitignore_is_self_ignoring(self):
        f = self.ws / "a.txt"
        f.write_text("v1")
        self.txn().record_before_write(f)
        gi = self.ws / s.LEDGER_DIR_NAME / ".gitignore"
        self.assertEqual(gi.read_text().strip(), "*")


class TestRollbackAndReapply(_WsTestCase):
    def _one_turn(self, path="a.txt", before="before", after="after"):
        """1ターン分の変更(既存ファイルの上書き)をシミュレートして台帳を確定する。"""
        f = self.ws / path
        f.write_text(before)
        t = self.txn()
        t.record_before_write(f)
        s.atomic_write(f, after)
        t.finalize("completed")
        return t, f

    def test_rollback_restores_previous_content(self):
        t, f = self._one_turn()
        r = s.rollback_transaction(self.ws, t.id)
        self.assertEqual(f.read_text(), "before")
        self.assertEqual(r["restored"], 1)
        m = json.loads((t.dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["status"], "rolled_back")

    def test_rollback_removes_created_file_and_empty_dirs(self):
        f = self.ws / "x" / "y" / "new.txt"
        t = self.txn()
        t.record_before_write(f)
        f.parent.mkdir(parents=True)
        s.atomic_write(f, "brand new")
        t.finalize("completed")
        r = s.rollback_transaction(self.ws, t.id)
        self.assertEqual(r["removed"], 1)
        self.assertFalse(f.exists())
        self.assertFalse((self.ws / "x").exists())  # 空になった新規親dirも消える

    def test_rollback_keeps_created_dir_if_not_empty(self):
        f = self.ws / "x" / "new.txt"
        t = self.txn()
        t.record_before_write(f)
        f.parent.mkdir(parents=True)
        s.atomic_write(f, "new")
        (self.ws / "x" / "other.txt").write_text("誰かが後から置いた別ファイル")
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertFalse(f.exists())
        self.assertTrue((self.ws / "x" / "other.txt").exists())  # 巻き添えにしない

    def test_double_rollback_is_rejected(self):
        t, _ = self._one_turn()
        s.rollback_transaction(self.ws, t.id)
        with self.assertRaises(ValueError):
            s.rollback_transaction(self.ws, t.id)

    def test_reapply_restores_the_rolled_back_change(self):
        t, f = self._one_turn()
        s.rollback_transaction(self.ws, t.id)
        r = s.reapply_transaction(self.ws, t.id)
        self.assertEqual(f.read_text(), "after")
        self.assertEqual(r["reapplied"], 1)
        m = json.loads((t.dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["status"], "reapplied")

    def test_undo_redo_roundtrip_multiple_times(self):
        t, f = self._one_turn()
        for _ in range(2):  # undo→redoを2往復しても壊れない
            s.rollback_transaction(self.ws, t.id)
            self.assertEqual(f.read_text(), "before")
            s.reapply_transaction(self.ws, t.id)
            self.assertEqual(f.read_text(), "after")

    def test_reapply_requires_rolled_back_status(self):
        t, _ = self._one_turn()
        with self.assertRaises(ValueError):
            s.reapply_transaction(self.ws, t.id)

    def test_rollback_of_created_file_can_be_reapplied(self):
        f = self.ws / "new.txt"
        t = self.txn()
        t.record_before_write(f)
        s.atomic_write(f, "created")
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertFalse(f.exists())
        s.reapply_transaction(self.ws, t.id)
        self.assertEqual(f.read_text(), "created")

    def test_invalid_txn_id_is_rejected(self):
        for bad in ("../../../etc", "abc", "20260715-120000-XYZW", ""):
            with self.assertRaises(ValueError):
                s.rollback_transaction(self.ws, bad)

    def test_tampered_manifest_path_cannot_escape_workspace(self):
        t, _ = self._one_turn()
        mpath = t.dir / "manifest.json"
        m = json.loads(mpath.read_text(encoding="utf-8"))
        m["operations"][0]["path"] = "../../outside.txt"
        mpath.write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(ValueError):
            s.rollback_transaction(self.ws, t.id)

    def test_unknown_txn_id_raises_not_found(self):
        with self.assertRaises(FileNotFoundError):
            s.rollback_transaction(self.ws, "20990101-000000-dead")


class TestToolIntegration(_WsTestCase):
    """write_file/edit_fileツール経由での台帳記録とロールバック。"""

    def _call(self, name, args, txn):
        ctx = s.ToolContext(ws=self.ws, txn=txn)
        return s.BuiltinToolProvider().call_tool(name, args, ctx)

    def test_write_file_records_and_rolls_back(self):
        f = self.ws / "a.txt"
        f.write_text("original")
        t = self.txn()
        r = self._call("write_file", {"path": "a.txt", "content": "modified"}, t)
        self.assertTrue(r.startswith("OK"))
        self.assertEqual(f.read_text(), "modified")
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertEqual(f.read_text(), "original")

    def test_edit_file_records_and_rolls_back(self):
        f = self.ws / "a.txt"
        f.write_text("line1\nline2\n")
        t = self.txn()
        r = self._call("edit_file", {"path": "a.txt", "old_string": "line2",
                                     "new_string": "LINE2"}, t)
        self.assertTrue(r.startswith("OK"))
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertEqual(f.read_text(), "line1\nline2\n")

    def test_failed_edit_records_nothing(self):
        f = self.ws / "a.txt"
        f.write_text("content")
        t = self.txn()
        r = self._call("edit_file", {"path": "a.txt", "old_string": "存在しない",
                                     "new_string": "x"}, t)
        self.assertTrue(r.startswith("ERROR"))
        self.assertFalse(t.has_ops)  # 実際に書かない失敗は台帳に載せない

    def test_write_without_txn_still_works(self):
        # 後方互換: txn=None(既存テスト・レガシー呼び出し)では従来通り動く
        r = self._call("write_file", {"path": "b.txt", "content": "x"}, None)
        self.assertTrue(r.startswith("OK"))
        self.assertEqual((self.ws / "b.txt").read_text(), "x")
        self.assertFalse((self.ws / s.LEDGER_DIR_NAME).exists())

    def test_ledger_area_is_write_protected(self):
        t = self.txn()
        for name, args in (
                ("write_file", {"path": ".localcoder/transactions/x/manifest.json",
                                "content": "{}"}),
                ("edit_file", {"path": ".localcoder/hack.txt",
                               "old_string": "a", "new_string": "b"})):
            r = self._call(name, args, t)
            self.assertTrue(r.startswith("ERROR"), f"{name} should be blocked: {r}")
        self.assertFalse(t.has_ops)

    def test_exec_tool_passes_txn_through(self):
        f = self.ws / "c.txt"
        f.write_text("v0")
        t = self.txn()
        r = s.exec_tool("write_file", {"path": "c.txt", "content": "v1"},
                        self.ws, txn=t)
        self.assertTrue(r.startswith("OK"))
        self.assertTrue(t.has_ops)


class TestDeleteMoveCopy(_WsTestCase):
    """第2段階: 削除・移動・コピーの可逆化 (REVERSIBLE_OPERATIONS.md §5-6)。"""

    def _call(self, name, args, txn):
        return s.BuiltinToolProvider().call_tool(name, args, s.ToolContext(ws=self.ws, txn=txn))

    def test_delete_file_is_reversible(self):
        f = self.ws / "gone.txt"
        f.write_text("precious")
        t = self.txn()
        r = self._call("delete_file", {"path": "gone.txt"}, t)
        self.assertTrue(r.startswith("OK"))
        self.assertFalse(f.exists())
        t.finalize("completed")
        res = s.rollback_transaction(self.ws, t.id)
        self.assertEqual(res["undeleted"], 1)
        self.assertEqual(f.read_text(), "precious")

    def test_delete_file_rejects_directory(self):
        (self.ws / "d").mkdir()
        r = self._call("delete_file", {"path": "d"}, self.txn())
        self.assertTrue(r.startswith("ERROR"))
        self.assertTrue((self.ws / "d").is_dir())

    def test_delete_directory_restores_whole_subtree(self):
        d = self.ws / "pkg"
        (d / "sub").mkdir(parents=True)
        (d / "a.txt").write_text("A")
        (d / "sub" / "b.txt").write_text("B")
        t = self.txn()
        r = self._call("delete_directory", {"path": "pkg"}, t)
        self.assertTrue(r.startswith("OK"))
        self.assertFalse(d.exists())
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertEqual((d / "a.txt").read_text(), "A")
        self.assertEqual((d / "sub" / "b.txt").read_text(), "B")

    def test_delete_directory_refuses_workspace_root(self):
        r = self._call("delete_directory", {"path": "."}, self.txn())
        self.assertTrue(r.startswith("ERROR"))

    def test_move_file_is_reversible(self):
        src = self.ws / "old.txt"
        src.write_text("data")
        t = self.txn()
        r = self._call("move_file", {"src": "old.txt", "dst": "new.txt"}, t)
        self.assertTrue(r.startswith("OK"))
        self.assertFalse(src.exists())
        self.assertEqual((self.ws / "new.txt").read_text(), "data")
        t.finalize("completed")
        res = s.rollback_transaction(self.ws, t.id)
        self.assertEqual(res["moved_back"], 1)
        self.assertEqual(src.read_text(), "data")
        self.assertFalse((self.ws / "new.txt").exists())

    def test_move_over_existing_dst_restores_both(self):
        src = self.ws / "src.txt"; src.write_text("SRC")
        dst = self.ws / "dst.txt"; dst.write_text("DST-ORIGINAL")
        t = self.txn()
        self._call("move_file", {"src": "src.txt", "dst": "dst.txt"}, t)
        self.assertEqual(dst.read_text(), "SRC")
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertEqual(src.read_text(), "SRC")
        self.assertEqual(dst.read_text(), "DST-ORIGINAL")  # 上書きされた既存も戻る

    def test_move_into_new_dir_removes_empty_dir_on_rollback(self):
        src = self.ws / "f.txt"; src.write_text("x")
        t = self.txn()
        self._call("move_file", {"src": "f.txt", "dst": "sub/f.txt"}, t)
        self.assertTrue((self.ws / "sub" / "f.txt").is_file())
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertEqual(src.read_text(), "x")
        self.assertFalse((self.ws / "sub").exists())

    def test_copy_file_new_dst_is_reversible(self):
        src = self.ws / "a.txt"; src.write_text("orig")
        t = self.txn()
        r = self._call("copy_file", {"src": "a.txt", "dst": "b.txt"}, t)
        self.assertTrue(r.startswith("OK"))
        self.assertEqual((self.ws / "b.txt").read_text(), "orig")
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertEqual(src.read_text(), "orig")   # src unchanged
        self.assertFalse((self.ws / "b.txt").exists())  # copy removed

    def test_copy_over_existing_restores_original(self):
        src = self.ws / "a.txt"; src.write_text("NEW")
        dst = self.ws / "b.txt"; dst.write_text("ORIGINAL")
        t = self.txn()
        self._call("copy_file", {"src": "a.txt", "dst": "b.txt"}, t)
        self.assertEqual(dst.read_text(), "NEW")
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertEqual(dst.read_text(), "ORIGINAL")

    def test_delete_then_reapply_deletes_again(self):
        f = self.ws / "x.txt"; f.write_text("v")
        t = self.txn()
        self._call("delete_file", {"path": "x.txt"}, t)
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertTrue(f.exists())
        s.reapply_transaction(self.ws, t.id)
        self.assertFalse(f.exists())

    def test_move_then_reapply_moves_again(self):
        src = self.ws / "a.txt"; src.write_text("d")
        t = self.txn()
        self._call("move_file", {"src": "a.txt", "dst": "b.txt"}, t)
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertTrue(src.exists())
        s.reapply_transaction(self.ws, t.id)
        self.assertFalse(src.exists())
        self.assertEqual((self.ws / "b.txt").read_text(), "d")

    def test_mixed_ops_rollback_in_reverse_order(self):
        # write→delete→move を1ターンで行い、全て戻ることを確認
        (self.ws / "keep.txt").write_text("original")
        (self.ws / "trash.txt").write_text("bye")
        (self.ws / "from.txt").write_text("moved")
        t = self.txn()
        self._call("write_file", {"path": "keep.txt", "content": "edited"}, t)
        self._call("delete_file", {"path": "trash.txt"}, t)
        self._call("move_file", {"src": "from.txt", "dst": "to.txt"}, t)
        t.finalize("completed")
        s.rollback_transaction(self.ws, t.id)
        self.assertEqual((self.ws / "keep.txt").read_text(), "original")
        self.assertEqual((self.ws / "trash.txt").read_text(), "bye")
        self.assertEqual((self.ws / "from.txt").read_text(), "moved")
        self.assertFalse((self.ws / "to.txt").exists())

    def test_ledger_area_cannot_be_deleted_or_moved(self):
        t = self.txn()
        (self.ws / "real.txt").write_text("x")
        for name, args in (
                ("delete_file", {"path": ".localcoder/x"}),
                ("delete_directory", {"path": ".localcoder"}),
                ("move_file", {"src": "real.txt", "dst": ".localcoder/x"})):
            r = self._call(name, args, t)
            self.assertTrue(r.startswith("ERROR"), f"{name}: {r}")

    def test_changed_files_includes_delete_and_move(self):
        (self.ws / "a.txt").write_text("x")
        (self.ws / "b.txt").write_text("y")
        msgs = [
            {"role": "assistant", "tool_calls": [{"function": {"name": "delete_file",
             "arguments": {"path": "a.txt"}}}]},
            {"role": "tool", "content": "OK: deleted a.txt (reversible for this turn)"},
            {"role": "assistant", "tool_calls": [{"function": {"name": "move_file",
             "arguments": {"src": "b.txt", "dst": "c.txt"}}}]},
            {"role": "tool", "content": "OK: moved b.txt -> c.txt (reversible for this turn)"},
        ]
        self.assertEqual(s.extract_changed_files(msgs), ["a.txt", "c.txt"])


if __name__ == "__main__":
    unittest.main()
