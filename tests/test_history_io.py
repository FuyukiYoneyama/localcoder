"""履歴の保存(save_session)・タイトル導出(derive_title)の単体テスト。

圧縮でmessages[0]がマーカーに置き換わっても履歴一覧のタイトルが変わらない
(=区別できなくならない)ことを保証する回帰テスト。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402


class TestDeriveTitle(unittest.TestCase):
    def test_normal_user_message(self):
        self.assertEqual(s.derive_title([{"role": "user", "content": "hello"}]), "hello")

    def test_marker_message_uses_summary_body(self):
        marker = s.build_marker("PicoCalc向けテキストエディタを作成中", ["a.c"])
        title = s.derive_title([{"role": "user", "content": marker}])
        self.assertEqual(title, "PicoCalc向けテキストエディタを作成中")
        self.assertFalse(title.startswith(s.MARKER_SUMMARY))

    def test_no_user_message_returns_placeholder(self):
        self.assertEqual(s.derive_title([{"role": "assistant", "content": "hi"}]), "(無題)")


class TestSaveSessionTitlePersistence(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.orig_history_dir = s.HISTORY_DIR
        s.HISTORY_DIR = Path(self._tmpdir.name)

    def tearDown(self):
        s.HISTORY_DIR = self.orig_history_dir
        self._tmpdir.cleanup()

    def test_title_is_fixed_on_first_save(self):
        s.save_session("sid1", "model-x", "/ws", [{"role": "user", "content": "元の依頼"}])
        data = json.loads((s.HISTORY_DIR / "sid1.json").read_text(encoding="utf-8"))
        self.assertEqual(data["title"], "元の依頼")

    def test_title_survives_compaction_on_later_saves(self):
        """圧縮でmessages[0]がマーカーに置き換わっても、初回に確定したタイトルは
        変わらない(実際に「履歴一覧がどれも同じタイトルになる」不具合があった)。
        """
        s.save_session("sid2", "model-x", "/ws", [{"role": "user", "content": "元の依頼"}])
        marker = s.build_marker("要約された内容", [])
        compacted = [{"role": "user", "content": marker}, {"role": "user", "content": "続き"}]
        s.save_session("sid2", "model-x", "/ws", compacted)
        data = json.loads((s.HISTORY_DIR / "sid2.json").read_text(encoding="utf-8"))
        self.assertEqual(data["title"], "元の依頼")

    def test_turns_accumulate_across_saves(self):
        s.save_session("sid3", "m", "/ws", [{"role": "user", "content": "x"}],
                       turn={"status": "completed"})
        s.save_session("sid3", "m", "/ws", [{"role": "user", "content": "x"}],
                       turn={"status": "max_iter"})
        data = json.loads((s.HISTORY_DIR / "sid3.json").read_text(encoding="utf-8"))
        self.assertEqual([t["status"] for t in data["turns"]], ["completed", "max_iter"])


if __name__ == "__main__":
    unittest.main()
