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

    def test_schema_version_is_written(self):
        s.save_session("sid4", "m", "/ws", [{"role": "user", "content": "x"}])
        data = json.loads((s.HISTORY_DIR / "sid4.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], s.SCHEMA_VERSION)

    def test_diagnostic_turn_fields_round_trip(self):
        """IMPROVEMENTS.md §2.3の診断情報(est_tokens/compact_count/tool_call_count等)を
        含むturn辞書がそのまま保存・復元できることを確認する。実際の収集ロジックは
        handle_chat(HTTPハンドラ)内にあり、ここではsave_session側の受け皿を検証する。
        """
        turn = {"started_at": 1.0, "ended_at": 2.0, "status": "stuck",
                "est_tokens_start": 100, "est_tokens_end": 150,
                "compact_count": 1, "http_retries": 0, "empty_retries": 1,
                "tool_call_count": 5, "tool_exec_seconds": 3.2, "iterations_used": 7}
        s.save_session("sid5", "m", "/ws", [{"role": "user", "content": "x"}], turn=turn)
        data = json.loads((s.HISTORY_DIR / "sid5.json").read_text(encoding="utf-8"))
        self.assertEqual(data["turns"][0], turn)


if __name__ == "__main__":
    unittest.main()
