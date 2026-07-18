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


class TestLogThinking(unittest.TestCase):
    """モデルのthink(推論)ストリームを、会話履歴とは別にhistory/thinking/
    <sid>.jsonlへ残すlog_thinkingの単体テスト。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.orig_dir = s.THINKING_LOG_DIR
        s.THINKING_LOG_DIR = Path(self._tmpdir.name)

    def tearDown(self):
        s.THINKING_LOG_DIR = self.orig_dir
        self._tmpdir.cleanup()

    def test_appends_one_line_per_call(self):
        s.log_thinking("sid1", 0, "うーん、どうしよう", 10)
        s.log_thinking("sid1", 1, "やっぱりこうする", 20)
        path = s.THINKING_LOG_DIR / "sid1.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        r0 = json.loads(lines[0])
        self.assertEqual(r0["iteration"], 0)
        self.assertEqual(r0["thinking"], "うーん、どうしよう")
        self.assertEqual(r0["content_len"], 10)
        self.assertEqual(r0["thinking_len"], len("うーん、どうしよう"))
        self.assertIn("ts", r0)

    def test_empty_thinking_is_not_logged(self):
        s.log_thinking("sid2", 0, "", 5)
        self.assertFalse((s.THINKING_LOG_DIR / "sid2.jsonl").exists())

    def test_separate_sids_go_to_separate_files(self):
        s.log_thinking("a", 0, "x", 1)
        s.log_thinking("b", 0, "y", 1)
        self.assertTrue((s.THINKING_LOG_DIR / "a.jsonl").exists())
        self.assertTrue((s.THINKING_LOG_DIR / "b.jsonl").exists())


class TestReconstructRaw(unittest.TestCase):
    """history/raw/<sid>.jsonl(圧縮で捨てられた生ログ)とhistory/<sid>.json
    (現在の圧縮され続けるセッション本体)を連結し、完全な非圧縮ログを
    組み立てるtools/reconstruct_raw.pyの単体テスト。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self.history_dir = root
        self.raw_dir = root / "raw"
        self.raw_dir.mkdir()

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import reconstruct_raw as rr
        self.rr = rr
        self.orig_history_dir = rr.HISTORY_DIR
        self.orig_raw_dir = rr.RAW_HISTORY_DIR
        rr.HISTORY_DIR = self.history_dir
        rr.RAW_HISTORY_DIR = self.raw_dir

    def tearDown(self):
        self.rr.HISTORY_DIR = self.orig_history_dir
        self.rr.RAW_HISTORY_DIR = self.orig_raw_dir
        self._tmpdir.cleanup()

    def _write_raw(self, sid, msgs):
        with open(self.raw_dir / f"{sid}.jsonl", "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    def _write_session(self, sid, messages, **extra):
        data = {"sid": sid, "model": "m", "workspace": "/ws",
                "turns": [], "messages": messages, **extra}
        (self.history_dir / f"{sid}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_concatenates_raw_archive_and_tail_when_no_marker(self):
        """まだ一度も圧縮されていないセッションは、raw archiveが空で
        現在のmessagesがそのまま完全ログになる。"""
        self._write_session("s1", [{"role": "user", "content": "hello"}])
        out = self.rr.reconstruct("s1")
        self.assertEqual(out["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(out["reconstructed_from"]["raw_jsonl_messages"], 0)

    def test_prepends_raw_archive_and_drops_compaction_marker(self):
        old_raw = [{"role": "user", "content": "元の依頼"},
                   {"role": "assistant", "content": "了解"}]
        self._write_raw("s2", old_raw)
        marker = self.rr.MARKER_SUMMARY + "\n要約本文"
        tail = [{"role": "assistant", "content": "続きの返答"}]
        self._write_session("s2", [{"role": "user", "content": marker}] + tail)

        out = self.rr.reconstruct("s2")
        self.assertEqual(out["messages"], old_raw + tail)
        self.assertTrue(out["reconstructed_from"]["had_compaction_marker"])

    def test_missing_session_file_raises(self):
        with self.assertRaises(SystemExit):
            self.rr.reconstruct("does-not-exist")


if __name__ == "__main__":
    unittest.main()
