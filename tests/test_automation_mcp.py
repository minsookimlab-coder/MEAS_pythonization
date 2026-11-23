"""AI 자동 측정 — Claude Code 연결용 MCP 서버 (실제 HTTP, 127.0.0.1 임의 포트).

지키는 계약:
  - 토큰 없이는 아무것도 못 한다 (401), localhost 가 아닌 Origin 은 거부 (403)
  - initialize 가 서버 안내문(Claude Code 4,096자 제한 안)을 준다
  - tools/list 가 앱 내장 AI 와 같은 도구 + wait_job/stop_job 을 MCP 형식으로 준다
  - 오래 걸리는 도구는 running + job_id 로 먼저 돌아오고 wait_job 으로 결과를 받는다
  - 측정 작업이 도는 동안 다른 장비 작업은 거부되고, 지식·보고 도구는 바로 실행된다
  - 실제 LabExecutor 와 이어져 안전 검사·메인 창 sweep 경로를 그대로 탄다
"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pythonization.automation import mcp_auth                           # noqa: E402
from pythonization.automation.agent import ApprovalDecision, ToolOutcome  # noqa: E402
from pythonization.automation.executor import LabExecutor               # noqa: E402
from pythonization.automation.knowledge import KnowledgeStore           # noqa: E402
from pythonization.automation.mcp_server import (                       # noqa: E402
    McpLabServer,
    load_or_create_token,
    mcp_tool_list,
    regenerate_token,
)
from pythonization.automation.prompts import MCP_INSTRUCTIONS          # noqa: E402
from tests.automation_fakes import FakeLab, make_profile, make_settings  # noqa: E402
from tests.test_automation_executor import FakeBackend                  # noqa: E402

TOKEN = "t" * 40


class SlowExecutor:
    """도구마다 정해진 시간 걸리는 가짜 실행기."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []
        self.stop_event = threading.Event()
        self.confirm_knowledge_on_save = False

    def set_stop_event(self, ev):
        self.stop_event = ev

    def preview(self, name, args):
        return f"preview {name}"

    def execute(self, name, args):
        self.calls.append(name)
        if self.stop_event.wait(self.delay):
            return ToolOutcome({"status": "stopped"}, True)
        return ToolOutcome({"status": "complete", "tool": name})


class McpCase(unittest.TestCase):
    def start(self, executor, **kw):
        server = McpLabServer(executor_provider=lambda: executor, token=TOKEN, port=0, **kw)
        server.start()
        self.addCleanup(server.stop)
        self.server = server
        return server

    def post(self, body, token=TOKEN, origin=None, timeout=30):
        req = urllib.request.Request(self.server.url, data=json.dumps(body).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, None, dict(e.headers)

    def call(self, name, args=None, msg_id=1):
        status, body, _ = self.post({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                                     "params": {"name": name, "arguments": args or {}}})
        self.assertEqual(200, status)
        result = body["result"]
        return json.loads(result["content"][0]["text"]), result["isError"]


class TestProtocol(McpCase):
    def setUp(self):
        self.start(SlowExecutor())

    def test_auth_required(self):
        self.assertEqual(401, self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                        token=None)[0])
        self.assertEqual(401, self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                        token="wrong")[0])

    def test_foreign_origin_rejected(self):
        status = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           origin="http://evil.example.com")[0]
        self.assertEqual(403, status)
        status = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           origin="http://localhost:3000")[0]
        self.assertEqual(200, status)

    def test_initialize(self):
        status, body, headers = self.post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "claude-code", "version": "2.1"}}})
        self.assertEqual(200, status)
        result = body["result"]
        self.assertEqual("2025-06-18", result["protocolVersion"])
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(MCP_INSTRUCTIONS, result["instructions"])
        self.assertIn("Mcp-Session-Id", headers)
        self.assertEqual("claude-code 2.1", self.server.last_client)

    def test_unknown_protocol_version_falls_back(self):
        _, body, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual("2025-06-18", body["result"]["protocolVersion"])

    def test_notification_gets_202(self):
        status, body, _ = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((202, None), (status, body))

    def test_tools_list(self):
        _, body, _ = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in body["result"]["tools"]}
        for name in ("run_sweep", "move_channel", "send_message", "recall_knowledge",
                     "wait_job", "stop_job"):
            self.assertIn(name, tools)
        self.assertEqual("object", tools["run_sweep"]["inputSchema"]["type"])
        self.assertTrue(tools["run_sweep"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["read_values"]["annotations"]["readOnlyHint"])

    def test_unknown_method_and_tool(self):
        _, body, _ = self.post({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        self.assertEqual(-32601, body["error"]["code"])
        content, is_error = self.call("format_disk")
        self.assertTrue(is_error)

    def test_get_is_not_allowed(self):
        req = urllib.request.Request(self.server.url, method="GET")
        req.add_header("Authorization", f"Bearer {TOKEN}")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(405, ctx.exception.code)

    def test_instructions_and_names_fit_claude_code_limits(self):
        self.assertLess(len(MCP_INSTRUCTIONS), 4096)
        for t in mcp_tool_list():
            self.assertLess(len(f"mcp__pythonization-lab__{t['name']}"), 128)
            self.assertLess(len(t["description"]), 4096)


class TestJobs(McpCase):
    def test_long_job_returns_running_then_wait_job(self):
        ex = SlowExecutor(delay=1.0)
        self.start(ex, sync_wait_s=0.2)
        first, is_error = self.call("run_sweep", {"channel": 0})
        self.assertFalse(is_error)
        self.assertEqual("running", first["status"])
        job_id = first["job_id"]
        # 작업 중에는 다른 장비 작업이 거부된다
        other, is_error = self.call("move_channel", {"channel": 0})
        self.assertTrue(is_error)
        self.assertIn(job_id, other["error"])
        # 지식·보고 도구는 바로 실행된다
        quick, is_error = self.call("recall_knowledge", {"query": "x"})
        self.assertFalse(is_error)
        done, is_error = self.call("wait_job", {"job_id": job_id, "max_wait_s": 5})
        self.assertFalse(is_error)
        self.assertEqual("complete", done["status"])
        self.assertFalse(self.server.is_busy())

    def test_stop_job(self):
        ex = SlowExecutor(delay=30.0)
        self.start(ex, sync_wait_s=0.1)
        first, _ = self.call("run_time_trace", {})
        stopped, _ = self.call("stop_job")
        self.assertTrue(stopped["stopped"])
        done, is_error = self.call("wait_job", {"job_id": first["job_id"], "max_wait_s": 5})
        self.assertTrue(is_error)
        self.assertEqual("stopped", done["status"])

    def test_busy_reason_blocks_calls(self):
        self.start(SlowExecutor(), busy_reason=lambda: "앱 내장 AI 실행 중")
        content, is_error = self.call("read_values", {})
        self.assertTrue(is_error)
        self.assertIn("앱 내장", content["error"])

    def test_in_app_approval_applies_to_claude_code(self):
        ex = SlowExecutor()
        seen = []
        self.start(ex, approve=lambda n, a, p: seen.append(n) or ApprovalDecision(False, "아직"))
        content, is_error = self.call("run_sweep", {"channel": 0})
        self.assertTrue(is_error)
        self.assertIn("아직", content["error"])
        self.assertEqual(["run_sweep"], seen)
        self.assertEqual([], ex.calls, "거부되면 실행하지 않는다")
        self.call("read_values", {})
        self.assertEqual(["run_sweep"], seen, "읽기는 승인 대상이 아니다")


class TestRealExecutorOverMcp(McpCase):
    def test_gate_sweep_through_claude_code_path(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        backend = FakeBackend(Path(tmp.name))
        lab = FakeLab()
        ex = LabExecutor(session=lab, profile=make_profile(), settings=make_settings(),
                         backend=backend, stop_event=threading.Event(), emit=lambda e: None,
                         save_root=tmp.name,
                         knowledge=KnowledgeStore(Path(tmp.name) / "k"))
        ex.deadline = time.monotonic() + 600
        self.start(ex)
        content, is_error = self.call("run_sweep", {
            "channel": 0, "start": -1.0, "stop": 1.0, "rate": 60, "time_per_point": 0.1,
            "approach_rate": 120, "measurements": [0, 1], "sub_folder": "sampleA/gate",
            "file_prefix": "Vg_", "purpose": "Claude Code 요청"})
        self.assertFalse(is_error, content)
        self.assertEqual("complete", content["status"])
        self.assertAlmostEqual(-1.0, lab.source)
        self.assertEqual("sampleA/gate", backend.specs[0].sub_folder)
        # 안전 검사도 그대로
        content, is_error = self.call("move_channel", {"channel": 0, "target": 50, "rate": 1,
                                                       "purpose": "x"})
        self.assertTrue(is_error)
        self.assertIn("허용 범위", content["reason"])


class TestToken(unittest.TestCase):
    def test_token_file_created_once_and_regenerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            t1 = load_or_create_token(Path(tmp))
            self.assertGreaterEqual(len(t1), 32)
            self.assertEqual(t1, load_or_create_token(Path(tmp)))
            t2 = regenerate_token(Path(tmp))
            self.assertNotEqual(t1, t2)
            self.assertEqual(t2, load_or_create_token(Path(tmp)))

    def test_headers_helper_prints_authorization_json(self):
        import io
        out = io.StringIO()
        with mock.patch.object(mcp_auth, "load_or_create_token", return_value="abc" * 12), \
                mock.patch("sys.stdout", out):
            self.assertEqual(0, mcp_auth.main())
        self.assertEqual({"Authorization": "Bearer " + "abc" * 12}, json.loads(out.getvalue()))

    def test_repo_mcp_json_points_at_helper(self):
        cfg = json.loads((Path(__file__).resolve().parent.parent / ".mcp.json")
                         .read_text(encoding="utf-8"))
        server = cfg["mcpServers"]["pythonization-lab"]
        self.assertEqual("http", server["type"])
        self.assertIn("8765/mcp", server["url"])
        self.assertIn("pythonization.automation.mcp_auth", server["headersHelper"])
        self.assertNotIn("Bearer", json.dumps(cfg), "저장소에 토큰이 들어가면 안 된다")


if __name__ == "__main__":
    unittest.main()
