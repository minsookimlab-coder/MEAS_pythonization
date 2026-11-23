"""리포트를 '언제' 보내는가 — 주기 판단, 비동기 전송, 측정 코드와의 연결.

  - 설정이 꺼져 있으면 아무 일도 하지 않는다 (네트워크도 안 건드린다)
  - ntfy_report_every_n 만큼 쌓일 때마다 1회, force 면 즉시
  - 전송은 워커 스레드에서 — 호출(GUI 스레드)은 기다리지 않는다
  - 보내는 중에 갱신이 또 와도 스레드가 쌓이지 않는다
  - 조립·전송이 실패해도 예외가 호출부로 새지 않는다
  - 측정 시작 reset() 으로 주기 카운터가 0 부터 다시 센다
"""
import os
import threading
import time
import unittest
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pythonization.config.models import AlarmDeliveryConfig     # noqa: E402
from pythonization.notify.report_sender import ReportSender     # noqa: E402


def delivery(enabled=True, every=1, ntfy=True):
    return AlarmDeliveryConfig(
        use_ntfy=ntfy, ntfy_server="http://127.0.0.1:1" if ntfy else "",
        ntfy_topic="t" if ntfy else "",
        ntfy_report_enabled=enabled, ntfy_report_every_n=every)


class FakeManager:
    """send_report 호출을 기록하는 가짜 AlarmManager."""

    def __init__(self, errors=None, block=None):
        self.calls = []
        self.errors = errors or []
        self.block = block          # threading.Event — 전송을 붙잡아 둘 때

    def send_report(self, report, urgent=False):
        if self.block is not None:
            self.block.wait(5.0)
        self.calls.append((report, urgent))
        return list(self.errors)


def wait_until(fn, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.01)
    return False


class ScheduleTest(unittest.TestCase):

    def test_disabled_does_nothing(self):
        mgr = FakeManager()
        s = ReportSender(mgr, delivery_loader=lambda: delivery(enabled=False))
        self.assertFalse(s.on_update(title="t"))
        self.assertEqual(mgr.calls, [])

    def test_ntfy_not_ready_does_nothing(self):
        mgr = FakeManager()
        s = ReportSender(mgr, delivery_loader=lambda: delivery(ntfy=False))
        self.assertFalse(s.on_update(title="t"))
        self.assertEqual(mgr.calls, [])

    def test_every_update(self):
        mgr = FakeManager()
        s = ReportSender(mgr, delivery_loader=lambda: delivery(every=1))
        for _ in range(3):
            self.assertTrue(s.on_update(title="t"))
            self.assertTrue(wait_until(lambda: not s.busy))
        self.assertEqual(len(mgr.calls), 3)

    def test_every_n(self):
        mgr = FakeManager()
        s = ReportSender(mgr, delivery_loader=lambda: delivery(every=3))
        sent = []
        for _ in range(7):
            sent.append(s.on_update(title="t"))
            wait_until(lambda: not s.busy)
        # 3, 6 번째만 간다
        self.assertEqual(sent, [False, False, True, False, False, True, False])
        self.assertEqual(len(mgr.calls), 2)

    def test_force_ignores_period(self):
        mgr = FakeManager()
        s = ReportSender(mgr, delivery_loader=lambda: delivery(every=10))
        self.assertTrue(s.on_update(title="t", force=True))
        self.assertTrue(wait_until(lambda: not s.busy))
        self.assertEqual(len(mgr.calls), 1)

    def test_reset_restarts_counter(self):
        mgr = FakeManager()
        s = ReportSender(mgr, delivery_loader=lambda: delivery(every=2))
        s.on_update(title="t")                 # 1 — 안 감
        s.reset()
        self.assertFalse(s.on_update(title="t"))   # reset 뒤 1 번째 → 안 감
        self.assertTrue(s.on_update(title="t"))    # 2 번째 → 간다
        self.assertTrue(wait_until(lambda: not s.busy))
        self.assertEqual(len(mgr.calls), 1)


class AsyncTest(unittest.TestCase):

    def test_call_returns_immediately(self):
        block = threading.Event()
        mgr = FakeManager(block=block)
        s = ReportSender(mgr, delivery_loader=lambda: delivery())
        t0 = time.monotonic()
        self.assertTrue(s.on_update(title="t"))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5, "GUI 스레드를 붙잡으면 안 된다")
        self.assertTrue(s.busy)
        block.set()
        self.assertTrue(wait_until(lambda: not s.busy))

    def test_overlapping_update_is_skipped(self):
        block = threading.Event()
        mgr = FakeManager(block=block)
        s = ReportSender(mgr, delivery_loader=lambda: delivery())
        self.assertTrue(s.on_update(title="t"))
        self.assertFalse(s.on_update(title="t"), "보내는 중엔 건너뛴다")
        self.assertIn("건너", s.last_error)
        block.set()
        self.assertTrue(wait_until(lambda: not s.busy))
        self.assertEqual(len(mgr.calls), 1)

    def test_send_errors_are_recorded_not_raised(self):
        seen = []
        mgr = FakeManager(errors=["ntfy: 503"])
        s = ReportSender(mgr, delivery_loader=lambda: delivery(),
                         on_error=seen.append)
        self.assertTrue(s.on_update(title="t"))
        self.assertTrue(wait_until(lambda: not s.busy))
        self.assertIn("503", s.last_error)
        self.assertTrue(seen)

    def test_exception_in_worker_does_not_escape(self):
        class Boom(FakeManager):
            def send_report(self, report, urgent=False):
                raise RuntimeError("폭발")

        seen = []
        s = ReportSender(Boom(), delivery_loader=lambda: delivery(),
                         on_error=seen.append)
        self.assertTrue(s.on_update(title="t"))
        self.assertTrue(wait_until(lambda: not s.busy))
        self.assertIn("폭발", s.last_error)
        self.assertTrue(seen)

    def test_broken_settings_do_not_raise(self):
        def bad():
            raise OSError("설정 파일 깨짐")

        seen = []
        s = ReportSender(FakeManager(), delivery_loader=bad, on_error=seen.append)
        self.assertFalse(s.on_update(title="t"))
        self.assertTrue(seen)


class VnaWiringTest(unittest.TestCase):
    """VNA 창이 리포트를 부르는 자리에 실제로 연결돼 있는가 (소스 수준 확인)."""

    def test_hooks_present(self):
        import inspect
        from pythonization.ui.modules.vna import window as W
        src = inspect.getsource(W)
        self.assertIn("self._report_sender = ReportSender(", src)
        self.assertIn("self._report_sender.reset()", src)
        self.assertIn("def _send_report(", src)
        self.assertIn("def _report_conditions(", src)
        # second 완료와 측정 완료 두 군데에서 부른다
        self.assertGreaterEqual(src.count("self._send_report("), 3)
        self.assertIn("self._last_saved_path = path", src)

    def test_conditions_builder_is_defensive(self):
        """설정이 비어 있어도 예외 없이 dict 를 돌려준다."""
        from pythonization.ui.modules.vna.window import VnaWindow
        fake = NS(_cfg=NS(ds_control=NS(power=NS(enabled=False),
                                        field_time=NS(enabled=False),
                                        first_idx=99, second_enabled=False,
                                        first_start=0.0, first_stop=1.0),
                          acquire=NS(sweep_cmds=[])))
        cond = VnaWindow._report_conditions(fake)
        self.assertIsInstance(cond, dict)
        self.assertEqual(cond.get("모드"), "Double sweep")


if __name__ == "__main__":
    unittest.main()
