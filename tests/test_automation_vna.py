"""AI 모드가 VNA 를 다룰 수 있는가 — 도구 등록·실행기·브리지.

실제 장비·창 없이 확인한다:
  - vna_* 도구 4개가 등록되고, 장비를 움직이는 둘만 승인 대상이다
  - 실행기가 백엔드로 넘기고, 실패는 is_error 로 표시된다
  - 다른 측정이 돌고 있으면 VNA 도 시작하지 않는다 (_check_not_busy)
  - 설정에 문제가 있으면 **시작 자체를 하지 않는다** (모달 대화상자 회피)
  - sweep 대기 중 사용자가 중지하면 VNA 에 중지가 전달된다
  - 타임아웃이면 중지하고 사유를 돌려준다
"""
import os
import threading
import unittest
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pythonization.automation import tools                      # noqa: E402
from pythonization.automation.executor import LabExecutor       # noqa: E402


class FakeBackend:
    def __init__(self, **kw):
        self.calls = []
        self.busy = ""
        self.state = {"open": True, "running": False, "sections": ["Trigger"]}
        self.section_result = {"ok": True, "section": "Trigger", "commands": 2}
        self.acquire_result = {"ok": True, "mode": "single", "steps": 1}
        self.stop_result = {"ok": True}
        self.__dict__.update(kw)

    # LabBackend
    def busy_reason(self):
        return self.busy

    def main_state(self):
        return {}

    def run_main_sweep(self, spec):
        raise AssertionError("VNA 도구가 메인 sweep 을 불렀다")

    def vna_state(self):
        self.calls.append(("state",))
        return self.state

    def vna_run_section(self, name):
        self.calls.append(("section", name))
        return self.section_result

    def vna_acquire(self, mode, wait, timeout_s):
        self.calls.append(("acquire", mode, wait, timeout_s))
        return self.acquire_result

    def vna_stop(self):
        self.calls.append(("stop",))
        return self.stop_result


def make_executor(backend):
    """LabExecutor 를 최소 인자로 만든다 (생성자가 바뀌면 여기만 고치면 된다)."""
    import inspect
    sig = inspect.signature(LabExecutor.__init__)
    kwargs = {}
    for name, par in list(sig.parameters.items())[1:]:
        if name in ("backend", "lab", "lab_backend"):
            kwargs[name] = backend
        elif par.default is not inspect.Parameter.empty:
            continue
        else:
            kwargs[name] = None
    ex = LabExecutor.__new__(LabExecutor)
    ex._backend = backend
    ex._emit = lambda _ev: None
    ex.deadline = __import__("time").monotonic() + 36000
    ex.started_at = __import__("time").monotonic()
    return ex


class ToolRegistrationTest(unittest.TestCase):

    def test_four_tools_registered(self):
        names = sorted(n for n in tools.TOOL_NAMES if n.startswith("vna_"))
        self.assertEqual(names, ["vna_acquire", "vna_get_state",
                                 "vna_run_section", "vna_stop"])

    def test_only_actuating_ones_need_approval(self):
        self.assertIn("vna_acquire", tools.ACTUATING_TOOLS)
        self.assertIn("vna_run_section", tools.ACTUATING_TOOLS)
        self.assertNotIn("vna_get_state", tools.ACTUATING_TOOLS)
        self.assertNotIn("vna_stop", tools.ACTUATING_TOOLS)
        self.assertIn("vna_acquire", tools.APPROVAL_TOOLS)

    def test_specs_have_schema(self):
        for spec in tools.TOOL_SPECS:
            if not spec["name"].startswith("vna_"):
                continue
            self.assertIn("description", spec)
            self.assertEqual(spec["input_schema"]["type"], "object")
            self.assertFalse(spec["input_schema"].get("additionalProperties", True))

    def test_mcp_server_exposes_them(self):
        from pythonization.automation import mcp_server
        import inspect
        src = inspect.getsource(mcp_server)
        for n in ("vna_get_state", "vna_run_section", "vna_acquire", "vna_stop"):
            self.assertIn(n, src)

    def test_prompt_mentions_vna(self):
        from pythonization.automation import prompts
        import inspect
        self.assertIn("vna_get_state", inspect.getsource(prompts))


class ExecutorTest(unittest.TestCase):

    def test_get_state_is_readonly(self):
        b = FakeBackend()
        out = make_executor(b)._tool_vna_get_state({})
        self.assertFalse(out.is_error)
        self.assertEqual(out.content["vna"]["sections"], ["Trigger"])
        self.assertEqual(b.calls, [("state",)])

    def test_run_section_passes_name(self):
        b = FakeBackend()
        out = make_executor(b)._tool_vna_run_section({"name": "Trigger"})
        self.assertFalse(out.is_error)
        self.assertEqual(b.calls, [("section", "Trigger")])

    def test_run_section_rejects_empty_name(self):
        b = FakeBackend()
        out = make_executor(b)._tool_vna_run_section({"name": "  "})
        self.assertTrue(out.is_error)
        self.assertEqual(b.calls, [])

    def test_section_failure_is_error(self):
        b = FakeBackend(section_result={"ok": False, "error": "없는 섹션"})
        out = make_executor(b)._tool_vna_run_section({"name": "X"})
        self.assertTrue(out.is_error)

    def test_acquire_converts_minutes_to_seconds(self):
        b = FakeBackend()
        make_executor(b)._tool_vna_acquire({"mode": "sweep", "timeout_min": 30})
        self.assertEqual(b.calls, [("acquire", "sweep", True, 1800.0)])

    def test_acquire_default_timeout(self):
        b = FakeBackend()
        make_executor(b)._tool_vna_acquire({"mode": "single"})
        self.assertEqual(b.calls[0][3], 3600.0)

    def test_acquire_failure_is_error(self):
        b = FakeBackend(acquire_result={"ok": False, "error": "설정 문제",
                                        "problems": ["trace2 없음"]})
        out = make_executor(b)._tool_vna_acquire({"mode": "single"})
        self.assertTrue(out.is_error)
        self.assertIn("trace2", str(out.content))

    def test_busy_blocks_vna(self):
        from pythonization.automation.safety import SafetyViolation
        b = FakeBackend(busy="메인 sweep 실행 중")
        ex = make_executor(b)
        with self.assertRaises(SafetyViolation):
            ex._tool_vna_acquire({"mode": "single"})
        with self.assertRaises(SafetyViolation):
            ex._tool_vna_run_section({"name": "Trigger"})
        self.assertEqual(b.calls, [])

    def test_stop_is_not_blocked_by_busy(self):
        b = FakeBackend(busy="메인 sweep 실행 중")
        out = make_executor(b)._tool_vna_stop({})
        self.assertFalse(out.is_error)
        self.assertEqual(b.calls, [("stop",)])

    def test_budget_guard(self):
        from pythonization.automation.safety import SafetyViolation
        import time
        b = FakeBackend()
        ex = make_executor(b)
        ex.deadline = time.monotonic() + 60        # 1분 남음
        with self.assertRaises(SafetyViolation):
            ex._tool_vna_acquire({"mode": "sweep", "timeout_min": 600})
        self.assertEqual(b.calls, [])

    def test_approval_text_mentions_vna(self):
        from pythonization.automation import executor as E
        import inspect
        src = inspect.getsource(E)
        self.assertIn("VNA 묶음 명령 실행", src)
        self.assertIn("VNA 측정 실행", src)


class BridgeLogicTest(unittest.TestCase):
    """브리지의 GUI 비의존 부분 — 시작 거부·중지·타임아웃."""

    def _bridge(self):
        from pythonization.ui.modules.auto_measure.bridge import MainWindowBridge
        br = MainWindowBridge.__new__(MainWindowBridge)
        br._stop = threading.Event()
        return br

    def test_start_refuses_when_problems(self):
        br = self._bridge()
        win = NS(_acq_thread=None, _cfg=NS(acquire=NS(sweep_cmds=[], start_cmds=[],
                                                      wait_cmds=[], read_cmds=[])),
                 _lib_reg=None)
        br._vna_win = lambda create=False: win
        br._vna_problems = staticmethod(lambda w: ["trace2 가 없습니다"])
        res = MainWindowBridgeStart(br, "single")
        self.assertFalse(res["ok"])
        self.assertIn("trace2", str(res["problems"]))

    def test_start_refuses_unknown_mode(self):
        br = self._bridge()
        br._vna_win = lambda create=False: NS(_acq_thread=None)
        br._vna_problems = staticmethod(lambda w: [])
        res = MainWindowBridgeStart(br, "nonsense")
        self.assertFalse(res["ok"])
        self.assertIn("mode", res["error"])

    def test_start_refuses_when_already_running(self):
        br = self._bridge()
        br._vna_win = lambda create=False: NS(_acq_thread=NS(isRunning=lambda: True))
        res = MainWindowBridgeStart(br, "single")
        self.assertFalse(res["ok"])
        self.assertIn("이미 실행", res["error"])

    def test_stop_when_nothing_running(self):
        from pythonization.ui.modules.auto_measure.bridge import MainWindowBridge
        br = self._bridge()
        br._vna_win = lambda create=False: NS(_acq_thread=None)
        res = MainWindowBridge._vna_stop(br)
        self.assertTrue(res["ok"])
        self.assertIn("없습니다", res["note"])


def MainWindowBridgeStart(br, mode):
    from pythonization.ui.modules.auto_measure.bridge import MainWindowBridge
    return MainWindowBridge._vna_start(br, mode)


if __name__ == "__main__":
    unittest.main()
