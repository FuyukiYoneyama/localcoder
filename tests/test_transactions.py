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


if __name__ == "__main__":
    unittest.main()
