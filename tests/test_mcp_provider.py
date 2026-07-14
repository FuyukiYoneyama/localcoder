"""MCPクライアント(McpToolProvider, IMPROVEMENTS.md §13 / 第6段階)の単体テスト。

外部依存なし。フェイクMCPサーバー(このファイル内に埋め込んだ数十行のPython
スクリプト)を実際に子プロセスとして起動し、stdio上のJSON-RPC 2.0ハンドシェイク
(initialize → notifications/initialized → tools/list → tools/call)を本物の
プロセス間通信で検証する。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402

# 改行区切りJSON-RPC 2.0を話す最小のMCPサーバー。ツールはecho(成功)・
# fail(isError)・read_file(組み込みツールと同名=衝突テスト用)の3つ。
FAKE_SERVER = r'''
import json, sys
TOOLS = [
    {"name": "echo", "description": "echo the text back",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "fail", "description": "always returns isError",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read_file", "description": "colliding tool name",
     "inputSchema": {"type": "object", "properties": {}}},
]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    m, rid = req.get("method", ""), req.get("id")
    if m.startswith("notifications/"):
        continue
    if m == "initialize":
        r = {"protocolVersion": "2025-06-18",
             "capabilities": {"tools": {"listChanged": False}},
             "serverInfo": {"name": "fake", "version": "1"}}
    elif m == "tools/list":
        r = {"tools": TOOLS}
    elif m == "tools/call":
        p = req.get("params") or {}
        name, args = p.get("name"), p.get("arguments") or {}
        if name == "echo":
            r = {"content": [{"type": "text", "text": "echo: " + args.get("text", "")}],
                 "isError": False}
        else:
            r = {"content": [{"type": "text", "text": "boom"}], "isError": True}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": rid,
                          "error": {"code": -32601, "message": "method not found"}}),
              flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": r}), flush=True)
'''


def _write_fake_server(dirpath: str) -> str:
    p = Path(dirpath) / "fake_mcp_server.py"
    p.write_text(FAKE_SERVER, encoding="utf-8")
    return str(p)


class TestMcpToolProvider(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.provider = s.McpToolProvider(
            "fake", sys.executable, [_write_fake_server(self._tmpdir.name)])

    def tearDown(self):
        with self.provider._lock:
            self.provider._shutdown_locked()
        self._tmpdir.cleanup()

    def test_list_tools_returns_ollama_format(self):
        tools = self.provider.list_tools()
        names = [t["function"]["name"] for t in tools]
        self.assertEqual(names, ["echo", "fail", "read_file"])
        echo = tools[0]
        self.assertEqual(echo["type"], "function")
        self.assertEqual(echo["function"]["description"], "echo the text back")
        self.assertEqual(echo["function"]["parameters"]["required"], ["text"])

    def test_call_tool_success(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = s.ToolContext(ws=Path(d))
            result = self.provider.call_tool("echo", {"text": "hi"}, ctx)
        self.assertEqual(result, "echo: hi")

    def test_call_tool_is_error_gets_error_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.provider.call_tool("fail", {}, s.ToolContext(ws=Path(d)))
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("boom", result)

    def test_repeated_calls_reuse_the_same_process(self):
        self.provider.list_tools()
        pid1 = self.provider._proc.pid
        with tempfile.TemporaryDirectory() as d:
            self.provider.call_tool("echo", {"text": "a"}, s.ToolContext(ws=Path(d)))
        self.assertEqual(self.provider._proc.pid, pid1)


class TestMcpToolProviderFailure(unittest.TestCase):
    def test_broken_command_degrades_gracefully(self):
        """起動できないサーバーは空のツール一覧・ERROR文字列になり、例外は漏れない。"""
        provider = s.McpToolProvider("broken", sys.executable, ["-c", "pass"])
        self.assertEqual(provider.list_tools(), [])
        with tempfile.TemporaryDirectory() as d:
            result = provider.call_tool("echo", {}, s.ToolContext(ws=Path(d)))
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("broken", result)

    def test_failed_start_is_not_retried_immediately(self):
        provider = s.McpToolProvider("broken", sys.executable, ["-c", "pass"])
        provider.list_tools()
        first_failed_at = provider._failed_at
        self.assertGreater(first_failed_at, 0)
        provider.list_tools()  # MCP_RETRY_INTERVAL内の再試行はしない
        self.assertEqual(provider._failed_at, first_failed_at)


class TestDispatchIntegration(unittest.TestCase):
    """TOOL_PROVIDERSへ登録した時の名前衝突・ディスパッチの検証。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.provider = s.McpToolProvider(
            "fake", sys.executable, [_write_fake_server(self._tmpdir.name)])
        s.TOOL_PROVIDERS.append(self.provider)

    def tearDown(self):
        s.TOOL_PROVIDERS.remove(self.provider)
        with self.provider._lock:
            self.provider._shutdown_locked()
        self._tmpdir.cleanup()

    def test_all_tools_dedupes_colliding_names_builtin_wins(self):
        tools = s.all_tools()
        names = [t["function"]["name"] for t in tools]
        self.assertEqual(len(names), len(set(names)))  # 重複なし
        self.assertIn("echo", names)
        # read_fileは組み込みの定義が使われる(フェイク側のdescriptionではない)
        read_file = next(t for t in tools if t["function"]["name"] == "read_file")
        self.assertNotEqual(read_file["function"]["description"], "colliding tool name")

    def test_provider_for_tool_prefers_builtin_for_colliding_name(self):
        self.assertIsInstance(s._provider_for_tool("read_file"), s.BuiltinToolProvider)
        self.assertIs(s._provider_for_tool("echo"), self.provider)

    def test_exec_tool_dispatches_to_mcp_provider(self):
        with tempfile.TemporaryDirectory() as d:
            result = s.exec_tool("echo", {"text": "via exec_tool"}, Path(d))
        self.assertEqual(result, "echo: via exec_tool")


class TestLoadMcpProviders(unittest.TestCase):
    def test_missing_config_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(s.load_mcp_providers(Path(d) / "none.json"), [])

    def test_valid_config_is_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "mcp_servers.json"
            cfg.write_text(json.dumps({"mcpServers": {
                "one": {"command": "python3", "args": ["a.py"],
                        "env": {"K": "V"}},
                "no-command": {"args": ["b.py"]},
            }}), encoding="utf-8")
            providers = s.load_mcp_providers(cfg)
        self.assertEqual(len(providers), 1)  # commandの無い定義はスキップ
        self.assertEqual(providers[0].name, "one")
        self.assertEqual(providers[0].command, "python3")
        self.assertEqual(providers[0].args, ["a.py"])
        self.assertEqual(providers[0].env, {"K": "V"})

    def test_invalid_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "mcp_servers.json"
            cfg.write_text("{broken", encoding="utf-8")
            self.assertEqual(s.load_mcp_providers(cfg), [])


if __name__ == "__main__":
    unittest.main()
