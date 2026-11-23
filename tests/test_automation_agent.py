"""AI 자동 측정 — 도구 호출 루프 (가짜 API 클라이언트로).

지키는 계약:
  - 요청 형태 (adaptive thinking + 진행 메모, effort, 서버 fallback, 프롬프트 캐시)
  - tool_use 하나에 tool_result 하나 — 거부·중지·잘림에도 대화가 유효하게 남는다
  - 장비를 움직이는 도구는 '승인' 모드에서 승인 없이는 실행되지 않는다
  - 실행 중 추가 지시는 다음 결과와 함께 전달된다
  - 호출 수·시간 예산을 넘기면 멈춘다
"""
import threading
import unittest
from types import SimpleNamespace as NS

from pythonization.automation import agent as agent_mod
from pythonization.automation.agent import AgentLoop, ApprovalDecision, ToolOutcome
from pythonization.automation.llm import FALLBACK_BETA, UPDATES_BETA
from pythonization.automation.tools import ACTUATING_TOOLS, TOOL_SPECS


def text(t):
    return NS(type="text", text=t)


def tool(id_, name, inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def resp(content, stop="tool_use", **kw):
    return NS(content=content, stop_reason=stop,
              usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=3,
                       cache_creation_input_tokens=0), **kw)


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        # 이후 append 로 바뀌므로 그 시점 메시지 수와 마지막 메시지를 남긴다
        self.calls.append({**kwargs, "n_messages": len(kwargs["messages"]),
                           "last": kwargs["messages"][-1]})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Harness:
    def __init__(self, responses, approve=True, mode="approve", on_execute=None):
        self.client = FakeClient(responses)
        self.executed = []
        self.events = []
        self.approvals = []
        self._approve = approve
        self._on_execute = on_execute
        self.loop = AgentLoop(
            client=self.client, model="claude-opus-5-5", effort="high", max_tokens=16000,
            system_prompt="SYS", tools=TOOL_SPECS, execute=self.execute,
            preview=lambda n, a: f"preview {n}", emit=self.events.append,
            approve=self.approve)
        self.loop.approval_mode = mode

    def execute(self, name, inp):
        self.executed.append((name, inp))
        if self._on_execute:
            return self._on_execute(name, inp)
        return ToolOutcome({"ok": True, "name": name})

    def approve(self, name, inp, preview):
        self.approvals.append(name)
        if isinstance(self._approve, ApprovalDecision):
            return self._approve
        return ApprovalDecision(bool(self._approve))


def tool_results(message):
    return {b["tool_use_id"]: b for b in message["content"] if b.get("type") == "tool_result"}


class TestRequestShape(unittest.TestCase):
    def test_request_parameters(self):
        h = Harness([resp([text("끝")], stop="end_turn")])
        self.assertEqual("finished", h.loop.run("요청"))
        call = h.client.calls[0]
        self.assertEqual("claude-opus-5-5", call["model"])
        self.assertEqual({"type": "adaptive", "display": "updates"}, call["thinking"])
        self.assertEqual({"effort": "high"}, call["output_config"])
        self.assertEqual("default", call["fallbacks"])
        self.assertIn(FALLBACK_BETA, call["betas"])
        self.assertIn(UPDATES_BETA, call["betas"])
        self.assertEqual({"type": "ephemeral"}, call["cache_control"])
        self.assertEqual("SYS", call["system"])
        self.assertIs(TOOL_SPECS, call["tools"])
        self.assertNotIn("tool_choice", call, "강제 tool_choice 는 이 모델에서 400")
        self.assertEqual("끝", h.loop.final_text)

    def test_every_tool_has_schema_and_description(self):
        names = {t["name"] for t in TOOL_SPECS}
        self.assertTrue(ACTUATING_TOOLS <= names)
        for t in TOOL_SPECS:
            with self.subTest(tool=t["name"]):
                self.assertTrue(t["description"].strip())
                self.assertEqual("object", t["input_schema"]["type"])


class TestToolLoop(unittest.TestCase):
    def test_result_paired_with_tool_use(self):
        h = Harness([
            resp([text("읽어 보겠습니다."), tool("t1", "read_values", {"measurements": [0]}),
                  tool("t2", "get_lab_state", {})]),
            resp([text("보고")], stop="end_turn"),
        ])
        self.assertEqual("finished", h.loop.run("요청"))
        self.assertEqual(["read_values", "get_lab_state"], [n for n, _ in h.executed])
        results = tool_results(h.client.calls[1]["last"])
        self.assertEqual({"t1", "t2"}, set(results))
        self.assertNotIn("is_error", results["t1"])
        self.assertEqual([], h.approvals, "읽기 도구는 승인이 필요 없다")

    def test_thinking_updates_are_progress_events(self):
        h = Harness([resp([NS(type="thinking", thinking="게이트 채널을 찾는 중"),
                           NS(type="thinking", thinking=""), text("끝")], stop="end_turn")])
        h.loop.run("요청")
        kinds = [(e.kind, e.text) for e in h.events if e.kind in ("progress", "assistant")]
        self.assertEqual([("progress", "게이트 채널을 찾는 중"), ("assistant", "끝")], kinds)

    def test_assistant_content_passed_back_unchanged(self):
        blocks = [NS(type="thinking", thinking="x", signature="sig"),
                  tool("t1", "get_lab_state", {})]
        h = Harness([resp(blocks), resp([text("끝")], stop="end_turn")])
        h.loop.run("요청")
        assistant = h.loop.messages[1]
        self.assertEqual("assistant", assistant["role"])
        self.assertEqual(blocks, assistant["content"], "응답 블록을 그대로 되돌려야 한다")

    def test_unknown_tool_is_error_result(self):
        h = Harness([resp([tool("t1", "format_disk", {})]),
                     resp([text("끝")], stop="end_turn")])
        h.loop.run("요청")
        self.assertEqual([], h.executed)
        self.assertTrue(tool_results(h.client.calls[1]["last"])["t1"]["is_error"])

    def test_executor_exception_becomes_error_result(self):
        def boom(name, inp):
            raise RuntimeError("장비 응답 없음")
        h = Harness([resp([tool("t1", "read_values", {})]),
                     resp([text("끝")], stop="end_turn")], on_execute=boom)
        self.assertEqual("finished", h.loop.run("요청"))
        r = tool_results(h.client.calls[1]["last"])["t1"]
        self.assertTrue(r["is_error"])
        self.assertIn("장비 응답 없음", r["content"])


class TestApproval(unittest.TestCase):
    def sweep(self):
        return tool("t1", "run_sweep", {"channel": 0})

    def test_approved_actuation_executes(self):
        h = Harness([resp([self.sweep()]), resp([text("끝")], stop="end_turn")])
        h.loop.run("요청")
        self.assertEqual(["run_sweep"], h.approvals)
        self.assertEqual(["run_sweep"], [n for n, _ in h.executed])

    def test_rejected_actuation_not_executed(self):
        h = Harness([resp([self.sweep()]), resp([text("알겠습니다")], stop="end_turn")],
                    approve=ApprovalDecision(False, "속도가 너무 빠름"))
        h.loop.run("요청")
        self.assertEqual([], h.executed)
        r = tool_results(h.client.calls[1]["last"])["t1"]
        self.assertTrue(r["is_error"])
        self.assertIn("속도가 너무 빠름", r["content"])

    def test_auto_mode_skips_approval(self):
        h = Harness([resp([self.sweep()]), resp([text("끝")], stop="end_turn")], mode="auto")
        h.loop.run("요청")
        self.assertEqual([], h.approvals)
        self.assertEqual(1, len(h.executed))

    def test_approval_comment_forwarded_to_model(self):
        h = Harness([resp([self.sweep()]), resp([text("끝")], stop="end_turn")],
                    approve=ApprovalDecision(True, "끝나면 0 V 로"))
        h.loop.run("요청")
        texts = [b["text"] for b in h.client.calls[1]["last"]["content"]
                 if b.get("type") == "text"]
        self.assertTrue(any("끝나면 0 V 로" in t for t in texts))


class TestStopAndLimits(unittest.TestCase):
    def test_stop_during_tools_keeps_conversation_valid(self):
        holder = {}

        def stop_after_first(name, inp):
            holder["loop"].stop_event.set()
            return ToolOutcome({"ok": True})

        h = Harness([resp([tool("t1", "read_values", {}), tool("t2", "get_lab_state", {})])],
                    on_execute=stop_after_first)
        holder["loop"] = h.loop
        self.assertEqual("stopped", h.loop.run("요청"))
        self.assertEqual(1, len(h.executed))
        results = tool_results(h.loop.messages[-1])
        self.assertEqual({"t1", "t2"}, set(results), "실행 안 한 tool_use 에도 결과가 있어야 한다")
        self.assertTrue(results["t2"]["is_error"])

    def test_stop_before_call(self):
        h = Harness([])
        h.loop.stop_event.set()
        self.assertEqual("stopped", h.loop.run("요청"))
        self.assertEqual([], h.client.calls)

    def test_max_tokens_does_not_run_truncated_tool(self):
        h = Harness([resp([tool("t1", "run_sweep", {"channel": 0})], stop="max_tokens"),
                     resp([text("끝")], stop="end_turn")])
        self.assertEqual("finished", h.loop.run("요청"))
        self.assertEqual([], h.executed)
        self.assertTrue(tool_results(h.client.calls[1]["last"])["t1"]["is_error"])

    def test_refusal_stops(self):
        h = Harness([resp([], stop="refusal", stop_details=NS(category="cyber"))])
        self.assertEqual("refusal", h.loop.run("요청"))
        self.assertTrue(any(e.kind == "error" and "cyber" in e.text for e in h.events))

    def test_turn_budget(self):
        h = Harness([resp([tool(f"t{i}", "get_lab_state", {})]) for i in range(5)])
        h.loop.max_turns = 2
        self.assertEqual("budget", h.loop.run("요청"))
        self.assertEqual(2, len(h.client.calls))

    def test_time_budget(self):
        h = Harness([resp([text("x")], stop="end_turn")])
        h.loop.deadline = 0.0
        self.assertEqual("budget", h.loop.run("요청"))

    def test_non_retryable_api_error(self):
        h = Harness([ValueError("bad")])
        self.assertEqual("error", h.loop.run("요청"))

    def test_retryable_api_error_retries(self):
        orig_desc, orig_base = agent_mod.describe_api_error, agent_mod._RETRY_BASE_S
        agent_mod.describe_api_error = lambda exc: ("잠시 오류", True)
        agent_mod._RETRY_BASE_S = 0.0
        try:
            h = Harness([ConnectionError("x"), resp([text("끝")], stop="end_turn")])
            self.assertEqual("finished", h.loop.run("요청"))
            self.assertEqual(2, len(h.client.calls))
        finally:
            agent_mod.describe_api_error, agent_mod._RETRY_BASE_S = orig_desc, orig_base

    def test_usage_accumulates(self):
        h = Harness([resp([tool("t1", "get_lab_state", {})]), resp([text("끝")], stop="end_turn")])
        h.loop.run("요청")
        self.assertEqual((2, 20, 10, 6), (h.loop.usage.calls, h.loop.usage.input_tokens,
                                          h.loop.usage.output_tokens,
                                          h.loop.usage.cache_read_input_tokens))


class TestSteering(unittest.TestCase):
    def test_steer_delivered_with_next_results(self):
        holder = {}

        def steer_once(name, inp):
            holder["loop"].steer("락인 Y 도 같이 기록해")
            return ToolOutcome({"ok": True})

        h = Harness([resp([tool("t1", "get_lab_state", {})]),
                     resp([text("끝")], stop="end_turn")], on_execute=steer_once)
        holder["loop"] = h.loop
        h.loop.run("요청")
        texts = [b.get("text", "") for b in h.client.calls[1]["last"]["content"]]
        self.assertTrue(any("락인 Y" in t for t in texts))

    def test_steer_after_final_answer_continues(self):
        h = Harness([resp([text("끝났습니다")], stop="end_turn"),
                     resp([text("추가 작업 완료")], stop="end_turn")])
        h.loop.steer("0 V 로 되돌려")
        self.assertEqual("finished", h.loop.run("요청"))
        self.assertEqual(2, len(h.client.calls))
        self.assertEqual("추가 작업 완료", h.loop.final_text)

    def test_continue_same_conversation(self):
        h = Harness([resp([text("첫 번째")], stop="end_turn"),
                     resp([text("두 번째")], stop="end_turn")])
        h.loop.run("요청 1")
        h.loop.run("요청 2")
        self.assertEqual(4, h.client.calls[1]["n_messages"] + 1,
                         "이전 대화(user, assistant) 뒤에 새 user 가 붙는다")


if __name__ == "__main__":
    unittest.main()
