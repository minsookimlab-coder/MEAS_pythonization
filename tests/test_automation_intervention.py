"""AI 모드가 측정 도중의 오류·개입·추가 요구에 제대로 반응하는가.

  오류      — 도구 예외·안전 위반이 is_error 결과로 모델에게 전달된다
  개입      — 사람이 AI 실행 중 VNA Acquire 를 눌러도 시작되지 않는다
               (AI 자신은 전용 진입점으로 들어와 막히지 않는다)
             — 사람이 중지했거나 오류로 끝난 VNA 측정을 '성공' 으로 보지 않는다
  추가 요구 — 실행 중 들어온 지시가 다음 tool_result 와 함께 모델에게 간다
"""
import os
import queue
import threading
import unittest
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pythonization.automation.agent import ToolOutcome          # noqa: E402
from pythonization.automation.executor import LabExecutor       # noqa: E402
from pythonization.automation.safety import SafetyViolation     # noqa: E402


class FakeBackend:
    def __init__(self, acquire_result=None, busy=""):
        self.busy = busy
        self.acquire_result = acquire_result or {"ok": True, "mode": "sweep"}
        self.calls = []

    def busy_reason(self):
        return self.busy

    def main_state(self):
        return {}

    def run_main_sweep(self, spec):
        raise AssertionError("부르면 안 된다")

    def vna_acquire(self, mode, wait, timeout_s):
        self.calls.append(("acquire", mode))
        return self.acquire_result

    def vna_stop(self):
        self.calls.append(("stop",))
        return {"ok": True}

    def vna_state(self):
        return {}

    def vna_run_section(self, name):
        return {"ok": True}


def make_executor(backend):
    import time
    ex = LabExecutor.__new__(LabExecutor)
    ex._backend = backend
    ex._emit = lambda _ev: None
    ex.deadline = time.monotonic() + 36000
    ex.started_at = time.monotonic()
    return ex


class ErrorResponseTest(unittest.TestCase):
    """오류가 모델에게 '읽을 수 있는 실패' 로 전달되는가."""

    def test_user_stop_is_reported_as_failure(self):
        b = FakeBackend({"ok": True, "mode": "sweep", "steps": 7, "stopped": True})
        out = make_executor(b)._tool_vna_acquire({"mode": "sweep"})
        self.assertTrue(out.is_error, "중지를 성공으로 돌려주면 모델이 이어서 진행한다")
        self.assertIn("중지", out.content["vna_acquire"]["note"])
        self.assertIn("불완전", out.content["vna_acquire"]["note"])

    def test_errored_run_is_reported_as_failure(self):
        b = FakeBackend({"ok": True, "mode": "sweep", "steps": 3, "errored": True})
        out = make_executor(b)._tool_vna_acquire({"mode": "sweep"})
        self.assertTrue(out.is_error)
        self.assertIn("오류", out.content["vna_acquire"]["note"])

    def test_clean_finish_is_success(self):
        b = FakeBackend({"ok": True, "mode": "sweep", "steps": 1200,
                         "stopped": False, "errored": False})
        out = make_executor(b)._tool_vna_acquire({"mode": "sweep"})
        self.assertFalse(out.is_error)
        self.assertNotIn("note", out.content["vna_acquire"])

    def test_safety_violation_propagates(self):
        """안전 위반은 예외로 올라가고, 에이전트가 is_error 로 바꿔 모델에게 준다."""
        b = FakeBackend(busy="Main sweep")
        with self.assertRaises(SafetyViolation):
            make_executor(b)._tool_vna_acquire({"mode": "sweep"})

    def test_agent_converts_tool_exception_to_is_error(self):
        from pythonization.automation.agent import AgentLoop as Agent
        import inspect
        src = inspect.getsource(Agent._run_tool if hasattr(Agent, "_run_tool")
                                else Agent)
        self.assertIn("is_error", src)
        self.assertIn("ToolOutcome", src)


class HumanInterventionTest(unittest.TestCase):
    """AI 가 측정하는 동안 사람이 VNA 를 눌렀을 때."""

    def _win(self, automation_busy=False, busy=""):
        from pythonization.ui.modules.vna.window import VnaWindow
        w = VnaWindow.__new__(VnaWindow)
        w._busy_guard = (lambda: busy)
        w._automation_guard = (lambda: automation_busy)
        w._status = []
        w._set_status = lambda msg, color=None: w._status.append(msg)
        return w

    def test_human_blocked_while_ai_runs(self):
        from pythonization.ui.modules.vna.window import VnaWindow
        w = self._win(automation_busy=True)
        self.assertTrue(VnaWindow._refuse_if_busy(w))
        self.assertIn("AI 자동 측정", w._status[0])

    def test_human_blocked_while_other_measurement_runs(self):
        from pythonization.ui.modules.vna.window import VnaWindow
        w = self._win(busy="Cycle Sweep")
        self.assertTrue(VnaWindow._refuse_if_busy(w))
        self.assertIn("Cycle Sweep", w._status[0])

    def test_vna_itself_is_not_a_conflict(self):
        """busy 가 'VNA' 면 그건 자기 자신 — 창 안에서 따로 막는다."""
        from pythonization.ui.modules.vna.window import VnaWindow
        w = self._win(busy="VNA")
        self.assertFalse(VnaWindow._refuse_if_busy(w))

    def test_idle_passes(self):
        from pythonization.ui.modules.vna.window import VnaWindow
        w = self._win()
        self.assertFalse(VnaWindow._refuse_if_busy(w))

    def test_guard_failure_does_not_block(self):
        """가드가 터져도 사람 조작을 막아 버리면 안 된다."""
        from pythonization.ui.modules.vna.window import VnaWindow
        w = self._win()
        w._automation_guard = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        w._busy_guard = lambda: (_ for _ in ()).throw(RuntimeError("y"))
        self.assertFalse(VnaWindow._refuse_if_busy(w))

    def test_standalone_window_has_no_guard(self):
        """메인 창 없이 띄운 VNA 창은 가드가 없어도 동작한다."""
        from pythonization.ui.modules.vna.window import VnaWindow
        w = VnaWindow.__new__(VnaWindow)
        w._set_status = lambda *a, **k: None
        self.assertFalse(VnaWindow._refuse_if_busy(w))

    def test_ai_uses_unattended_entry(self):
        """브리지는 사람 가드를 타지 않는 진입점을 쓴다."""
        import inspect
        from pythonization.ui.modules.auto_measure import bridge
        src = inspect.getsource(bridge)
        self.assertIn("start_acquire_unattended", src)
        self.assertNotIn("win._on_single_acquire()", src)

    def test_main_window_attaches_guards(self):
        import inspect
        from pythonization.ui import main_window
        src = inspect.getsource(main_window)
        self.assertIn("_busy_guard = self.measurement_busy_reason", src)
        self.assertIn("_automation_guard = self._automation_busy", src)


class ExtraRequestTest(unittest.TestCase):
    """실행 중 들어온 추가 지시가 모델에게 전달되는가."""

    def _agent(self):
        from pythonization.automation.agent import AgentLoop as Agent
        a = Agent.__new__(Agent)
        a._steer = queue.Queue()
        a._emit = lambda _ev: None
        a._journal = lambda _r: None
        return a

    def test_steer_queues_and_drains(self):
        from pythonization.automation.agent import AgentLoop as Agent
        a = self._agent()
        Agent.steer(a, "자기장 2 T 에서 한 번 더 측정해줘")
        blocks = Agent._drain_steer(a)
        self.assertEqual(len(blocks), 1)
        self.assertIn("사용자 추가 지시", blocks[0]["text"])
        self.assertIn("2 T", blocks[0]["text"])
        self.assertEqual(Agent._drain_steer(a), [], "두 번 전달되면 안 된다")

    def test_blank_steer_ignored(self):
        from pythonization.automation.agent import AgentLoop as Agent
        a = self._agent()
        Agent.steer(a, "   ")
        self.assertEqual(Agent._drain_steer(a), [])

    def test_multiple_requests_all_delivered(self):
        from pythonization.automation.agent import AgentLoop as Agent
        a = self._agent()
        for s in ("첫째", "둘째", "셋째"):
            Agent.steer(a, s)
        blocks = Agent._drain_steer(a)
        self.assertEqual(len(blocks), 3)

    def test_stop_event_is_shared(self):
        """중지는 큐가 아니라 이벤트 — 도구 대기 중에도 즉시 전달된다."""
        import inspect
        from pythonization.ui.modules.auto_measure import bridge
        src = inspect.getsource(bridge.MainWindowBridge.vna_acquire)
        self.assertIn("self._stop.is_set()", src)
        self.assertIn("_vna_stop", src)


if __name__ == "__main__":
    unittest.main()
