"""可逆操作レイヤー第3段階(REVERSIBLE_OPERATIONS.md §7-8)の単体テスト。

run_commandの外部送信検出(classify_external_send)と、外部送信ポリシー
(deny/allow_recorded)の適用、外部送信台帳への記録を検証する。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402


class TestClassifyExternalSend(unittest.TestCase):
    def test_detects_git_push(self):
        self.assertTrue(s.classify_external_send("git push origin main"))
        self.assertTrue(s.classify_external_send("cd repo && git push"))

    def test_detects_curl_post_family(self):
        for cmd in ("curl -X POST https://api.example.com -d @body.json",
                    "curl --request PUT https://x/y --data foo",
                    "curl -F file=@a.zip https://upload.example.com",
                    "curl -T dump.sql ftp://host/",
                    "curl --upload-file x https://h/"):
            self.assertTrue(s.classify_external_send(cmd), cmd)

    def test_detects_uploads_and_publishes(self):
        for cmd in ("scp secret.txt user@host:/tmp/",
                    "sftp user@host",
                    "rsync -avz ./ user@host:/backup/",
                    "npm publish",
                    "yarn publish --tag beta",
                    "twine upload dist/*",
                    "gh release create v1 ./bin",
                    "aws s3 cp big.bin s3://bucket/key",
                    "gsutil cp f gs://bucket/f",
                    "docker push myrepo/img:tag",
                    "ssh host 'rm -rf /tmp/x'"):
            self.assertTrue(s.classify_external_send(cmd), cmd)

    def test_get_and_local_commands_are_not_flagged(self):
        # 取得(GET)・ローカル完結・ビルド/テストは外部送信ではない
        for cmd in ("curl https://example.com",
                    "curl -O https://example.com/file.tar.gz",
                    "wget https://example.com/file",
                    "git status",
                    "git diff",
                    "git commit -m 'x'",       # ローカルのcommitは外部送信ではない
                    "ls -la && grep foo *.py",
                    "cmake --build . && ctest",
                    "rsync -av ./src ./dst",   # ローカル間rsync(host:無し)
                    "python3 -m http.server"):
            self.assertEqual(s.classify_external_send(cmd), [], cmd)

    def test_reasons_are_human_readable_and_deduped_per_pattern(self):
        reasons = s.classify_external_send("git push && docker push img")
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("git push" in r for r in reasons))
        self.assertTrue(any("docker push" in r for r in reasons))


class TestExternalSendPolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self._orig_policy = s.EXTERNAL_SEND_POLICY
        self.addCleanup(lambda: setattr(s, "EXTERNAL_SEND_POLICY", self._orig_policy))

    def _call(self, cmd, txn):
        return s.BuiltinToolProvider().call_tool(
            "run_command", {"command": cmd}, s.ToolContext(ws=self.ws, txn=txn))

    def test_deny_blocks_and_records_not_executed(self):
        s.EXTERNAL_SEND_POLICY = "deny"
        t = s.Transaction(self.ws)
        # 実際に送信されないコマンドで、拒否されること自体を確認(git pushは
        # リモート不要でネットワークに出ないが、分類上は外部送信として弾かれる)
        r = self._call("git push origin main", t)
        self.assertTrue(r.startswith("ERROR"))
        self.assertIn("deny", r)
        self.assertEqual(len(t.external_sends), 1)
        self.assertFalse(t.external_sends[0]["executed"])
        self.assertTrue(t.has_ops)  # 外部送信だけでも台帳は残る

    def test_allow_recorded_runs_and_records_executed(self):
        s.EXTERNAL_SEND_POLICY = "allow_recorded"
        t = s.Transaction(self.ws)
        # 実際にネットへ出ない安全なコマンドで「実行された」経路を確認する。
        # git pushを無理やり実行するとネットワーク/認証に触れるため、代わりに
        # 分類だけ外部送信扱いになるがローカルで完結して失敗するコマンドを使う。
        r = self._call("scp /nonexistent/x nohost-localonly:/tmp/ 2>/dev/null || true", t)
        self.assertFalse(r.startswith("ERROR: この操作は外部への送信"))
        self.assertEqual(len(t.external_sends), 1)
        self.assertTrue(t.external_sends[0]["executed"])
        self.assertEqual(t.external_sends[0]["reasons"],
                         s.classify_external_send("scp /nonexistent/x nohost-localonly:/tmp/ 2>/dev/null || true"))

    def test_non_external_command_records_nothing(self):
        s.EXTERNAL_SEND_POLICY = "deny"
        t = s.Transaction(self.ws)
        r = self._call("echo hello", t)
        self.assertIn("exit_code=0", r)
        self.assertEqual(len(t.external_sends), 0)
        self.assertFalse(t.has_ops)

    def test_external_send_recorded_in_manifest(self):
        s.EXTERNAL_SEND_POLICY = "deny"
        t = s.Transaction(self.ws)
        self._call("git push", t)
        t.finalize("completed")
        m = json.loads((t.dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(m["external_sends"]), 1)
        self.assertEqual(m["external_sends"][0]["command"], "git push")
        self.assertEqual(m["external_sends"][0]["policy"], "deny")


if __name__ == "__main__":
    unittest.main()
