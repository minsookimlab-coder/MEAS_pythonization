"""AI 자동 측정 — 도구 실행기 (가짜 장비 + 가짜 메인 창).

실제 SweepWorker / SecondChannelWorker 로직이 가짜 세션 위에서 돈다. 확인하는 것:
  - 장비에 쓰이는 모든 값이 허용 범위 안 (안전 검사 통과 전에는 한 번도 쓰지 않음)
  - 기록 측정은 메인 창 경로(backend.run_main_sweep)로 넘어가고 이름이 정리된다
  - '게이트 -20→20 스윕' 요청이 시작점 이동 + 메인 sweep 으로 옮겨진다
  - 중지가 이동 도중에도 바로 듣는다
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pythonization.automation.executor import (  # noqa: E402
    TIME_CHANNEL_ID,
    LabExecutor,
    MainSweepOutcome,
)
from tests.automation_fakes import FakeLab, make_profile, make_settings  # noqa: E402


class FakeBackend:
    def __init__(self, tmpdir: Path, busy: str = "", reason: str = "complete"):
        self.specs = []
        self.busy = busy
        self.reason = reason
        self._tmp = tmpdir

    def run_main_sweep(self, spec):
        self.specs.append(spec)
        path = self._tmp / f"{spec.file_prefix or 'run'}X001.dat"
        path.write_text("Vg\tX\tIleak\nV\tV\tA\n-1\t-0.001\t1e-11\n0\t0\t0\n1\t0.001\t1e-11\n",
                        encoding="utf-8")
        return MainSweepOutcome(self.reason, "", str(path), 3)

    def busy_reason(self):
        return self.busy

    def main_state(self):
        return {"main_sweep_running": False}


class ExecutorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.lab = FakeLab()
        self.backend = FakeBackend(self.tmp)
        self.stop = threading.Event()
        self.events = []
        self.sent = []
        self.ex = self.make()

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, settings=None):
        ex = LabExecutor(
            session=self.lab, profile=make_profile(), settings=settings or make_settings(),
            backend=self.backend, stop_event=self.stop, emit=self.events.append,
            messenger=lambda text, urgent: self.sent.append((text, urgent)) or [],
            save_root=str(self.tmp))
        ex.deadline = time.monotonic() + 3600
        return ex

    def run_tool(self, name, **inp):
        return self.ex.execute(name, inp)


class TestMove(ExecutorCase):
    def test_moves_within_limits_and_arrives(self):
        out = self.run_tool("move_channel", channel=0, target=1.0, rate=120,
                            time_per_point=0.05, purpose="시작점")
        self.assertFalse(out.is_error, out.content)
        self.assertEqual("arrived", out.content["status"])
        writes = self.lab.source_writes()
        self.assertAlmostEqual(1.0, writes[-1])
        self.assertEqual(writes, sorted(writes), "한 방향으로만 움직인다")
        self.assertTrue(all(0 < w <= 1.0 for w in writes))
        self.assertAlmostEqual(0.1, writes[0], msg="첫 스텝 = rate/60 × tpp")

    def test_out_of_range_target_never_writes(self):
        out = self.run_tool("move_channel", channel=0, target=25, rate=60, purpose="x")
        self.assertTrue(out.is_error)
        self.assertEqual([], self.lab.writes)

    def test_rate_above_limit_never_writes(self):
        out = self.run_tool("move_channel", channel=0, target=1, rate=500, purpose="x")
        self.assertTrue(out.is_error)
        self.assertEqual([], self.lab.writes)

    def test_not_allowed_channel_never_writes(self):
        out = self.run_tool("move_channel", channel=1, target=0.5, rate=60, purpose="x")
        self.assertTrue(out.is_error)
        self.assertIn("허용되지 않았", out.content["reason"])
        self.assertEqual([], self.lab.writes)

    def test_current_outside_range_never_writes(self):
        self.lab.source = 30.0
        out = self.run_tool("move_channel", channel=0, target=1, rate=60, purpose="x")
        self.assertTrue(out.is_error)
        self.assertEqual([], self.lab.writes)

    def test_busy_lab_never_writes(self):
        self.backend.busy = "Cycle Sweep"
        out = self.run_tool("move_channel", channel=0, target=1, rate=60, purpose="x")
        self.assertTrue(out.is_error)
        self.assertIn("Cycle Sweep", out.content["reason"])
        self.assertEqual([], self.lab.writes)

    def test_stop_interrupts_move(self):
        threading.Timer(0.3, self.stop.set).start()
        t0 = time.monotonic()
        out = self.run_tool("move_channel", channel=0, target=20, rate=60,
                            time_per_point=0.1, purpose="x")
        self.assertLess(time.monotonic() - t0, 2.0, "중지가 바로 들어야 한다")
        self.assertEqual("stopped", out.content["status"])
        self.assertLess(self.lab.source, 1.0)

    def test_budget_blocks_long_move(self):
        self.ex.deadline = time.monotonic() + 5
        out = self.run_tool("move_channel", channel=0, target=20, rate=1, purpose="x")
        self.assertTrue(out.is_error)
        self.assertIn("예산", out.content["reason"])
        self.assertEqual([], self.lab.writes)


class TestRunSweep(ExecutorCase):
    def test_gate_sweep_request_maps_to_move_then_main_sweep(self):
        """'게이트를 -1 에서 1 까지 스윕' → 기록 없이 -1 로 이동 → 메인 sweep -1→1."""
        out = self.run_tool(
            "run_sweep", channel=0, start=-1.0, stop=1.0, rate=60, time_per_point=0.1,
            approach_rate=120, measurements=[0, 1],
            sub_folder="sampleA/gate sweep", file_prefix="Vg_m1_p1_", purpose="게이트 sweep")
        self.assertFalse(out.is_error, out.content)
        self.assertAlmostEqual(-1.0, self.lab.source, msg="시작점으로 먼저 이동")
        self.assertTrue(all(-1.0 <= w <= 0 for w in self.lab.source_writes()))
        spec = self.backend.specs[0]
        self.assertEqual((0, 1.0, 60.0, 0.1), (spec.channel, spec.sweep_to, spec.rate,
                                               spec.time_per_point))
        self.assertEqual([0, 1], spec.measurements)
        self.assertEqual("sampleA/gate_sweep", spec.sub_folder)
        self.assertEqual("Vg_m1_p1_", spec.file_prefix)
        self.assertEqual({1: 1e-9}, spec.abort_limits, "사용자의 누설전류 한계가 실린다")
        self.assertEqual(("SRC", "smua voltage"), spec.channel_key)
        self.assertEqual([("LIA", "X"), ("SRC", "leakage")], spec.measurement_keys)
        self.assertEqual(3, out.content["data"]["points"])
        self.assertIn(out.content["file"], self.ex.files)

    def test_start_none_sweeps_from_current(self):
        self.lab.source = 0.5
        out = self.run_tool("run_sweep", channel=0, start=None, stop=1.0, rate=60,
                            time_per_point=0.1, measurements=[0], sub_folder="",
                            file_prefix="", purpose="x")
        self.assertFalse(out.is_error)
        self.assertEqual([], self.lab.source_writes(), "이동 없이 바로 메인 sweep")
        self.assertEqual(0.5, out.content["from"])

    def test_out_of_range_stop_rejected_before_anything(self):
        out = self.run_tool("run_sweep", channel=0, start=-1, stop=30, rate=60,
                            time_per_point=0.1, measurements=[0], sub_folder="",
                            file_prefix="", purpose="x")
        self.assertTrue(out.is_error)
        self.assertEqual([], self.lab.writes)
        self.assertEqual([], self.backend.specs)

    def test_path_escape_rejected(self):
        out = self.run_tool("run_sweep", channel=0, start=None, stop=1, rate=60,
                            time_per_point=0.1, measurements=[0], sub_folder="../../etc",
                            file_prefix="x", purpose="x")
        self.assertTrue(out.is_error)
        self.assertEqual([], self.backend.specs)

    def test_aborted_sweep_reported_as_error(self):
        self.backend.reason = "abort"
        out = self.run_tool("run_sweep", channel=0, start=None, stop=1, rate=60,
                            time_per_point=0.1, measurements=[1], sub_folder="",
                            file_prefix="", purpose="x")
        self.assertTrue(out.is_error)
        self.assertEqual("abort", out.content["status"])

    def test_time_trace_uses_time_channel(self):
        out = self.run_tool("run_time_trace", duration_s=600, time_per_point=1.0,
                            measurements=[0], sub_folder="stab", file_prefix="T_",
                            stop_when_stable={"measurement": 0, "window": 10, "max_std": 0.01},
                            purpose="안정화")
        self.assertFalse(out.is_error, out.content)
        spec = self.backend.specs[0]
        self.assertEqual((TIME_CHANNEL_ID, 600.0, 60.0), (spec.channel, spec.sweep_to, spec.rate))
        self.assertEqual((0, 10, 0.01), spec.stable)

    def test_time_trace_stable_measurement_must_be_recorded(self):
        out = self.run_tool("run_time_trace", duration_s=60, time_per_point=1.0,
                            measurements=[0], sub_folder="", file_prefix="",
                            stop_when_stable={"measurement": 1, "window": 5, "max_std": 1},
                            purpose="x")
        self.assertTrue(out.is_error)


class TestOtherTools(ExecutorCase):
    def test_second_axis_simple_hop(self):
        out = self.run_tool("set_second_axis", channel=0, value=10, purpose="10 K")
        self.assertFalse(out.is_error, out.content)
        self.assertEqual("reached", out.content["status"])
        self.assertIn(("ITC", "TSET 10.0"), self.lab.writes)
        self.assertEqual(300.0, out.content["before"])
        self.assertEqual(10.0, out.content["after"])

    def test_second_axis_out_of_range(self):
        out = self.run_tool("set_second_axis", channel=0, value=500, purpose="x")
        self.assertTrue(out.is_error)
        self.assertEqual([], self.lab.writes)

    def test_write_command_allowed_and_value_checked(self):
        self.assertFalse(self.run_tool("send_write_command", command=0, purpose="on").is_error)
        self.assertIn(("SRC", "OUT 1"), self.lab.writes)
        self.assertTrue(self.run_tool("send_write_command", command=1, value=50,
                                      purpose="x").is_error)
        self.assertNotIn(("SRC", "RANGE 50.0"), self.lab.writes)

    def test_read_values(self):
        self.lab.source = 2.0
        out = self.run_tool("read_values", measurements=[0], sweep_channels=[0],
                            second_channels=[0])
        values = out.content["values"]
        self.assertAlmostEqual(0.002, values["measurement[0] LIA X (V)"])
        self.assertAlmostEqual(2.0, values["sweep_channel[0] SRC smua voltage (V)"])
        self.assertAlmostEqual(300.0, values["second_channel[0] ITC VTI temperature (K)"])
        self.assertEqual([], self.lab.writes, "읽기는 아무것도 쓰지 않는다")

    def test_send_message_uses_messenger(self):
        out = self.run_tool("send_message", text="게이트 sweep 시작", urgent=False)
        self.assertTrue(out.content["delivered"])
        self.assertEqual([("게이트 sweep 시작", False)], self.sent)
        self.assertTrue(any(e.kind == "message" for e in self.events))

    def test_send_message_respects_setting(self):
        self.ex = self.make(make_settings(report_to_messenger=False))
        out = self.run_tool("send_message", text="x")
        self.assertFalse(out.content["delivered"])
        self.assertEqual([], self.sent)

    def test_wait_is_interruptible(self):
        threading.Timer(0.2, self.stop.set).start()
        t0 = time.monotonic()
        out = self.run_tool("wait", seconds=30, reason="x")
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(out.content["stopped"])

    def test_lab_state(self):
        out = self.run_tool("get_lab_state")
        self.assertEqual("gate", out.content["lab"]["sweep_channels"][0]["role"])
        self.assertIn("remaining_budget_min", out.content["run"])

    def test_preview_names_channel_and_values(self):
        text = self.ex.preview("run_sweep", {
            "channel": 0, "start": -20, "stop": 20, "rate": 0.5, "time_per_point": 1,
            "measurements": [0, 1], "sub_folder": "g", "file_prefix": "Vg_", "purpose": "p"})
        self.assertIn("smua voltage (gate)", text)
        self.assertIn("-20 V", text)
        self.assertIn("LIA X", text)
        self.assertIn("80.0분", text)


if __name__ == "__main__":
    unittest.main()
