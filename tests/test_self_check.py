"""起動時セルフチェック(run_self_check, IMPROVEMENTS.md §9.2)の単体テスト。

Ollamaへの実接続は行わず、urllib.request.urlopenを差し替えて検証する。
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _names(checks):
    return {c["name"]: c for c in checks}


class TestRunSelfCheck(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.orig_history_dir = s.HISTORY_DIR
        s.HISTORY_DIR = Path(self._tmpdir.name)
        self.orig_roots = s.ALLOWED_ROOTS

    def tearDown(self):
        s.HISTORY_DIR = self.orig_history_dir
        s.ALLOWED_ROOTS = self.orig_roots
        self._tmpdir.cleanup()

    def test_ollama_reachable_with_recommended_model(self):
        payload = {"models": [{"name": "gpt-oss:20b"}, {"name": "qwen3:8b"}]}
        with patch("server.urllib.request.urlopen", return_value=_FakeResponse(payload)):
            checks = _names(s.run_self_check())
        self.assertTrue(checks["Ollama接続"]["ok"])
        self.assertTrue(checks["推奨モデルの有無"]["ok"])
        self.assertIn("gpt-oss:20b", checks["推奨モデルの有無"]["detail"])

    def test_ollama_reachable_without_recommended_model(self):
        payload = {"models": [{"name": "qwen3:8b"}]}
        with patch("server.urllib.request.urlopen", return_value=_FakeResponse(payload)):
            checks = _names(s.run_self_check())
        self.assertTrue(checks["Ollama接続"]["ok"])
        self.assertFalse(checks["推奨モデルの有無"]["ok"])

    def test_ollama_unreachable(self):
        with patch("server.urllib.request.urlopen", side_effect=OSError("refused")):
            checks = _names(s.run_self_check())
        self.assertFalse(checks["Ollama接続"]["ok"])
        self.assertFalse(checks["推奨モデルの有無"]["ok"])
        self.assertIn("refused", checks["Ollama接続"]["detail"])

    def test_history_dir_writable(self):
        with patch("server.urllib.request.urlopen", side_effect=OSError("x")):
            checks = _names(s.run_self_check())
        self.assertTrue(checks["履歴ディレクトリへの書き込み"]["ok"])

    def test_history_dir_not_writable(self):
        s.HISTORY_DIR = Path(self._tmpdir.name) / "no" / "such" / "dir"
        with patch("server.urllib.request.urlopen", side_effect=OSError("x")):
            checks = _names(s.run_self_check())
        self.assertFalse(checks["履歴ディレクトリへの書き込み"]["ok"])

    def test_allowed_roots_all_valid(self):
        s.ALLOWED_ROOTS = [Path(self._tmpdir.name)]
        with patch("server.urllib.request.urlopen", side_effect=OSError("x")):
            checks = _names(s.run_self_check())
        self.assertTrue(checks["allowed roots"]["ok"])

    def test_allowed_roots_missing_path_reported(self):
        missing = Path(self._tmpdir.name) / "does_not_exist"
        s.ALLOWED_ROOTS = [Path(self._tmpdir.name), missing]
        with patch("server.urllib.request.urlopen", side_effect=OSError("x")):
            checks = _names(s.run_self_check())
        self.assertFalse(checks["allowed roots"]["ok"])
        self.assertIn(str(missing), checks["allowed roots"]["detail"])

    def test_never_raises_even_if_everything_fails(self):
        s.ALLOWED_ROOTS = [Path(self._tmpdir.name) / "gone"]
        s.HISTORY_DIR = Path(self._tmpdir.name) / "also" / "gone"
        with patch("server.urllib.request.urlopen", side_effect=OSError("x")):
            checks = s.run_self_check()  # 例外を投げず、全項目ok=Falseで返る
        self.assertTrue(all(not c["ok"] for c in checks
                            if c["name"] != "pdftotext(poppler-utils)"))


if __name__ == "__main__":
    unittest.main()
