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


def _multi_response(responses: dict):
    """呼び出しURLに応じて異なる_FakeResponseを返すside_effect関数を作る
    (build_diagnostic_bundleは/api/version・/api/tags・(間接的に)/api/showの
    複数エンドポイントを叩くため、単一return_valueでは足りない)。
    """
    def _side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        for key, payload in responses.items():
            if key in url:
                return _FakeResponse(payload)
        raise OSError(f"unexpected url in test: {url}")
    return _side_effect


class TestBuildDiagnosticBundle(unittest.TestCase):
    def setUp(self):
        self.orig_roots = s.ALLOWED_ROOTS
        self.orig_history_dir = s.HISTORY_DIR
        self._tmpdir = tempfile.TemporaryDirectory()
        s.HISTORY_DIR = Path(self._tmpdir.name)

    def tearDown(self):
        s.ALLOWED_ROOTS = self.orig_roots
        s.HISTORY_DIR = self.orig_history_dir
        self._tmpdir.cleanup()

    def test_excludes_secrets_and_personal_paths(self):
        with patch("server.urllib.request.urlopen",
                  side_effect=_multi_response({"/api/version": {"version": "0.9.0"},
                                               "/api/tags": {"models": []}})):
            bundle = s.build_diagnostic_bundle()
        dumped = json.dumps(bundle, ensure_ascii=False)
        self.assertNotIn(s.TOKEN, dumped)
        for root in s.ALLOWED_ROOTS:
            self.assertNotIn(str(root), dumped)
        self.assertNotIn("messages", bundle)

    def test_includes_version_and_config(self):
        with patch("server.urllib.request.urlopen",
                  side_effect=_multi_response({"/api/version": {"version": "0.9.0"},
                                               "/api/tags": {"models": []}})):
            bundle = s.build_diagnostic_bundle()
        self.assertEqual(bundle["localcoder_version"], s.SERVER_VERSION)
        self.assertEqual(bundle["ollama_version"], "0.9.0")
        self.assertEqual(bundle["config"]["num_ctx"], s.NUM_CTX)
        self.assertIn("allowed_roots_count", bundle["config"])

    def test_includes_model_list(self):
        with patch("server.urllib.request.urlopen",
                  side_effect=_multi_response({
                      "/api/version": {"version": "0.9.0"},
                      "/api/tags": {"models": [{"name": "diagbundle-test-model:latest"}]}})):
            bundle = s.build_diagnostic_bundle()
        names = [m["name"] for m in bundle["models"]]
        self.assertIn("diagbundle-test-model:latest", names)

    def test_ollama_unreachable_reports_failure_string_not_exception(self):
        with patch("server.urllib.request.urlopen", side_effect=OSError("refused")):
            bundle = s.build_diagnostic_bundle()  # 例外を投げない
        self.assertIn("refused", bundle["ollama_version"])
        self.assertIn("refused", bundle["models"])

    def test_no_sid_omits_session_turns(self):
        with patch("server.urllib.request.urlopen",
                  side_effect=_multi_response({"/api/version": {}, "/api/tags": {"models": []}})):
            bundle = s.build_diagnostic_bundle()
        self.assertNotIn("session_turns", bundle)

    def test_sid_includes_only_turns_not_messages(self):
        data = {"sid": "abc123", "messages": [{"role": "user", "content": "秘密のプロンプト"}],
                "turns": [{"status": "completed", "tool_call_count": 3}]}
        (s.HISTORY_DIR / "abc123.json").write_text(json.dumps(data), encoding="utf-8")
        with patch("server.urllib.request.urlopen",
                  side_effect=_multi_response({"/api/version": {}, "/api/tags": {"models": []}})):
            bundle = s.build_diagnostic_bundle(sid="abc123")
        self.assertEqual(bundle["session_turns"], [{"status": "completed", "tool_call_count": 3}])
        self.assertNotIn("秘密のプロンプト", json.dumps(bundle, ensure_ascii=False))

    def test_error_param_is_truncated_and_included(self):
        with patch("server.urllib.request.urlopen",
                  side_effect=_multi_response({"/api/version": {}, "/api/tags": {"models": []}})):
            bundle = s.build_diagnostic_bundle(error="x" * 5000)
        self.assertEqual(len(bundle["reported_error"]), 2000)

    def test_no_error_param_omits_field(self):
        with patch("server.urllib.request.urlopen",
                  side_effect=_multi_response({"/api/version": {}, "/api/tags": {"models": []}})):
            bundle = s.build_diagnostic_bundle()
        self.assertNotIn("reported_error", bundle)


if __name__ == "__main__":
    unittest.main()
