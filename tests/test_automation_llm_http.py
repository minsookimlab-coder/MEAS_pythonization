"""AI 자동 측정 — 실제 anthropic SDK 가 보내는 HTTP 요청 검증 (앱 내장 실행 경로).

API 키·비용 없이, 127.0.0.1 의 가짜 Messages API 서버로 SDK 요청을 받아 본다.
  - beta 헤더(서버측 fallback, 진행 메모 표시)와 본문 필드가 실제로 실린다
  - SDK 응답 객체(thinking·text·tool_use)를 에이전트 루프가 그대로 처리한다
  - thinking 블록이 서명째 다음 요청에 되돌아간다 (preserved thinking)
anthropic 패키지가 없으면 건너뛴다 (선택 의존성).
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pythonization.automation.agent import AgentLoop, ApprovalDecision, ToolOutcome
from pythonization.automation.llm import (
    FALLBACK_BETA,
    UPDATES_BETA,
    describe_api_error,
    make_client,
    sdk_available,
)
from pythonization.automation.tools import TOOL_SPECS

_RESPONSES = [
    {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
     "content": [
         {"type": "thinking", "thinking": "게이트 채널을 확인한다", "signature": "sig-1"},
         {"type": "text", "text": "현황을 확인하겠습니다."},
         {"type": "tool_use", "id": "toolu_1", "name": "get_lab_state", "input": {}}],
     "stop_reason": "tool_use", "stop_sequence": None,
     "usage": {"input_tokens": 1200, "output_tokens": 40,
               "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0}},
    {"id": "msg_2", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
     "content": [{"type": "text", "text": "확인 완료."}],
     "stop_reason": "end_turn", "stop_sequence": None,
     "usage": {"input_tokens": 50, "output_tokens": 10,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1000}},
]


class _FakeApi(BaseHTTPRequestHandler):
    requests = []
    responses = []
    status = 200

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "body": body,
                                    "headers": {k.lower(): v for k, v in self.headers.items()}})
        if type(self).status != 200:
            payload = json.dumps({"type": "error", "error": {
                "type": "authentication_error", "message": "invalid x-api-key"}}).encode()
            self.send_response(type(self).status)
        else:
            payload = json.dumps(type(self).responses.pop(0)).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@unittest.skipUnless(sdk_available(), "anthropic 패키지 없음 (선택 의존성)")
class TestRealSdkRequests(unittest.TestCase):
    def setUp(self):
        _FakeApi.requests = []
        _FakeApi.responses = [dict(r) for r in _RESPONSES]
        _FakeApi.status = 200
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApi)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.client = make_client("test-key", timeout=10,
                                  base_url=f"http://127.0.0.1:{self.httpd.server_port}")

    def make_loop(self, executed):
        return AgentLoop(
            client=self.client, model="claude-opus-5-5", effort="high", max_tokens=16000,
            system_prompt="SYS", tools=TOOL_SPECS,
            execute=lambda n, a: executed.append(n) or ToolOutcome({"ok": True}),
            preview=lambda n, a: n, emit=lambda e: None,
            approve=lambda n, a, p: ApprovalDecision(True))

    def test_request_shape_and_round_trip(self):
        executed = []
        loop = self.make_loop(executed)
        self.assertEqual("finished", loop.run("게이트 sweep 준비"))
        self.assertEqual(["get_lab_state"], executed)
        self.assertEqual("확인 완료.", loop.final_text)

        first = _FakeApi.requests[0]
        self.assertTrue(first["path"].startswith("/v1/messages"))
        betas = first["headers"].get("anthropic-beta", "")
        self.assertIn(FALLBACK_BETA, betas)
        self.assertIn(UPDATES_BETA, betas)
        # SDK 버전에 따라 x-api-key 또는 Authorization 으로 보낸다
        auth = first["headers"].get("x-api-key", "") + first["headers"].get("authorization", "")
        self.assertIn("test-key", auth)
        body = first["body"]
        self.assertEqual("claude-opus-5-5", body["model"])
        self.assertEqual("default", body["fallbacks"])
        self.assertEqual({"type": "adaptive", "display": "updates"}, body["thinking"])
        self.assertEqual({"effort": "high"}, body["output_config"])
        self.assertEqual({"type": "ephemeral"}, body["cache_control"])
        self.assertEqual(len(TOOL_SPECS), len(body["tools"]))
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("temperature", body)

        second = _FakeApi.requests[1]["body"]["messages"]
        assistant = second[1]
        self.assertEqual("assistant", assistant["role"])
        thinking = assistant["content"][0]
        self.assertEqual(("thinking", "sig-1"), (thinking["type"], thinking["signature"]),
                         "thinking 블록은 서명째 그대로 되돌아가야 한다")
        result = second[2]["content"][0]
        self.assertEqual(("tool_result", "toolu_1"), (result["type"], result["tool_use_id"]))
        self.assertEqual((2, 1000), (loop.usage.calls, loop.usage.cache_read_input_tokens))

    def test_auth_error_is_described_and_not_retried(self):
        _FakeApi.status = 401
        loop = self.make_loop([])
        events = []
        loop._emit = events.append
        self.assertEqual("error", loop.run("x"))
        self.assertEqual(1, len(_FakeApi.requests), "401 은 재시도하지 않는다")
        self.assertTrue(any("API 키" in e.text for e in events if e.kind == "error"))

    def test_describe_connection_error_is_retryable(self):
        import anthropic
        import httpx2
        exc = anthropic.APIConnectionError(request=httpx2.Request("POST", "http://x"))
        text, retryable = describe_api_error(exc)
        self.assertTrue(retryable)


if __name__ == "__main__":
    unittest.main()
