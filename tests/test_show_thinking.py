"""tools/show_thinking.py(独り言分析ログの一覧表示ツール)の単体テスト。

CLI(main)の出力整形はここでは検証せず、load()の読み込みロジックだけを
対象にする(他のtools/*配下のCLIツールと同じテスト範囲の方針)。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import show_thinking as st  # noqa: E402


class TestLoad(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmpdir.name)
        self.orig_dir = st.THINKING_LOG_DIR
        st.THINKING_LOG_DIR = self.dir

    def tearDown(self):
        st.THINKING_LOG_DIR = self.orig_dir
        self._tmpdir.cleanup()

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(st.load("no-such-sid"), [])

    def test_loads_records_in_order(self):
        path = self.dir / "sid1.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"iteration": 0, "thinking": "a", "thinking_len": 1,
                                "content_len": 0, "ts": 1.0}) + "\n")
            f.write(json.dumps({"iteration": 1, "thinking": "b", "thinking_len": 1,
                                "content_len": 0, "ts": 2.0}) + "\n")
        records = st.load("sid1")
        self.assertEqual([r["iteration"] for r in records], [0, 1])

    def test_skips_blank_lines(self):
        path = self.dir / "sid2.jsonl"
        path.write_text('{"iteration": 0, "thinking": "a", "thinking_len": 1, '
                        '"content_len": 0, "ts": 1.0}\n\n', encoding="utf-8")
        records = st.load("sid2")
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
