"""VnaWindow 의 double sweep 계획 수립.

여기서 정한 값이 그대로 계측기 이동 순서와 저장 파일 이름이 된다. 특히
'다중방향'은 second 스텝마다 first 진행 방향을 뒤집으므로, 방향이 틀리면
데이터는 저장되지만 축이 뒤집힌 채로 쌓인다 — 나중에 알아채기 어렵다.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication              # noqa: E402

from pythonization.profiles.registry import ProfileRegistry   # noqa: E402
from pythonization.ui.main_window import MainWindow           # noqa: E402
from pythonization.ui.modules.vna.window import (             # noqa: E402
    _FirstChannel,
    _INVALID,
    _SecondChannel,
)

_app = QApplication.instance() or QApplication([])


class VnaTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = MainWindow(profile_registry=ProfileRegistry())
        cls.main._open_vna_window()
        cls.win = cls.main._vna_window

    @classmethod
    def tearDownClass(cls):
        cls.main.deleteLater()

    def set_direction(self, mode: str):
        idx = self.win._combo_direction.findData(mode)
        self.assertGreaterEqual(idx, 0)
        self.win._combo_direction.setCurrentIndex(idx)


class TestStepLabels(VnaTestCase):
    def first(self, values):
        return _FirstChannel(cmd=None, values=values, advance=None, field_time=False)

    def second(self, values):
        return _SecondChannel(enabled=bool(values), cmd=object(),
                              values=values, advance=None)

    def test_single_direction_repeats_same_order(self):
        self.set_direction("uni")
        labels = self.win._build_step_labels(
            self.first([1.0, 2.0, 3.0]), self.second([10.0, 20.0]))
        self.assertEqual(["1", "2", "3", "1", "2", "3"], labels)

    def test_multi_direction_alternates(self):
        self.set_direction("multi")
        labels = self.win._build_step_labels(
            self.first([1.0, 2.0, 3.0]), self.second([10.0, 20.0]))
        self.assertEqual(["1", "2", "3", "3", "2", "1"], labels,
                         "두 번째 second 스텝은 first 를 역순으로 훑는다")

    def test_without_second_channel_single_pass(self):
        self.set_direction("uni")
        labels = self.win._build_step_labels(
            self.first([1.0, 2.0]),
            _SecondChannel(enabled=False, cmd=None, values=[], advance=None))
        self.assertEqual(["1", "2"], labels)

    def test_plus_sign_is_stripped_from_exponent(self):
        # 파일명에 '+' 가 들어가면 경로가 지저분해진다
        self.set_direction("uni")
        labels = self.win._build_step_labels(
            self.first([1e9]),
            _SecondChannel(enabled=False, cmd=None, values=[], advance=None))
        self.assertEqual(["1e09"], labels)
        self.assertNotIn("+", labels[0])


class TestResolveFirstChannel(VnaTestCase):
    """일반(power 모드 아님) First 축 해석.

    사용자 프로파일에 Power Sweep 모드가 켜져 저장돼 있으면 First 축이 power 로
    대체되므로, 이 클래스가 보는 경로를 타지 않는다 — 전제를 명시적으로 끈다.
    """

    def setUp(self):
        self.win._cb_power_mode.setChecked(False)

    def set_range(self, start: str, stop: str, count: str):
        self.win._le_sw_start.setText(start)
        self.win._le_sw_stop.setText(stop)
        self.win._le_sw_n.setText(count)

    def test_rejects_non_numeric_range(self):
        self.set_range("빠르게", "10", "5")
        self.assertIsNone(self.win._resolve_first_channel())

    def test_rejects_zero_points(self):
        self.set_range("0", "10", "0")
        self.assertIsNone(self.win._resolve_first_channel())

    def test_field_time_ignores_point_count(self):
        """field-time 모드는 스텝 수를 미리 알 수 없어 N 을 보지 않는다."""
        self.set_range("0", "1", "쓰레기값")
        self.win._cb_field_time.setChecked(True)
        self.addCleanup(self.win._cb_field_time.setChecked, False)
        first = self.win._resolve_first_channel()
        self.assertIsNotNone(first, "N 이 잘못돼도 field-time 은 시작할 수 있어야 한다")
        self.assertTrue(first.field_time)
        self.assertEqual(2, len(first.values))


class TestResolveSecondChannel(VnaTestCase):
    def test_disabled_returns_inactive_channel(self):
        self.win._cb_second_enable.setChecked(False)
        second = self.win._resolve_second_channel(0, None, None)
        self.assertIsNot(second, _INVALID)
        self.assertFalse(second.enabled)
        self.assertFalse(second.is_active)
        self.assertEqual([], second.values)

    def test_invalid_range_is_distinguishable_from_disabled(self):
        """'입력 오류'와 '채널 안 씀'은 다르게 처리돼야 한다.

        둘 다 None 으로 돌려주면 오류인데도 조용히 단일 sweep 이 시작된다.
        """
        self.win._cb_second_enable.setChecked(True)
        self.addCleanup(self.win._cb_second_enable.setChecked, False)
        if self.win._combo_second_cmd.count() == 0:
            self.skipTest("프로파일에 second sweep 명령이 없다")
        self.win._combo_second_cmd.setCurrentIndex(0)
        self.win._le_2_start.setText("영")
        self.assertIs(_INVALID, self.win._resolve_second_channel(0, None, None))


class TestPowerSweepMode(VnaTestCase):
    """Power Sweep 모드 — First 축 자리를 power 가 대신한다.

    여기서 어긋나면 자기장은 맞는데 안쪽 축이 엉뚱한 값(기존 First)으로 도는,
    데이터만 보고는 알아채기 어려운 사고가 난다.
    """

    def enable_power(self, start="-20", stop="0", n="5"):
        cmds = self.win._cfg.acquire.sweep_cmds
        if not cmds:
            self.skipTest("프로파일에 sweep 명령이 없다")
        # 비활성(OFF) 명령은 거부되므로 활성 항목을 고른다
        idx = next((i for i, c in enumerate(cmds) if c.enabled), None)
        if idx is None:
            self.skipTest("활성 sweep 명령이 없다")
        self.win._combo_power_cmd.setCurrentIndex(idx)
        self.win._le_pw_start.setText(start)
        self.win._le_pw_stop.setText(stop)
        self.win._le_pw_n.setText(n)
        self.win._cb_power_mode.setChecked(True)
        self.addCleanup(self.win._cb_power_mode.setChecked, False)
        return cmds[idx]

    def test_first_channel_becomes_power_with_inclusive_endpoints(self):
        cmd = self.enable_power(start="-20", stop="0", n="5")
        first = self.win._resolve_first_channel()
        self.assertIsNotNone(first)
        self.assertIs(cmd, first.cmd, "First 축이 power 명령으로 바뀌어야 한다")
        self.assertEqual([-20.0, -15.0, -10.0, -5.0, 0.0], first.values,
                         "처음과 끝을 포함한 N 점이어야 한다")
        self.assertFalse(first.field_time)

    def test_single_point_is_allowed(self):
        self.enable_power(start="-3", stop="7", n="1")
        first = self.win._resolve_first_channel()
        self.assertEqual([-3.0], first.values)

    def test_rejects_bad_range(self):
        self.enable_power(start="세게", stop="0", n="5")
        self.assertIsNone(self.win._resolve_first_channel())

    def test_rejects_zero_points(self):
        self.enable_power(start="-20", stop="0", n="0")
        self.assertIsNone(self.win._resolve_first_channel())

    def test_ignores_first_axis_inputs(self):
        """power 모드에서는 First Start/Stop/N 을 쳐다보지 않는다."""
        self.enable_power(start="-10", stop="-6", n="3")
        self.win._le_sw_start.setText("999")
        self.win._le_sw_stop.setText("쓰레기값")
        self.win._le_sw_n.setText("쓰레기값")
        first = self.win._resolve_first_channel()
        self.assertEqual([-10.0, -8.0, -6.0], first.values)

    def test_mutually_exclusive_with_field_time(self):
        """둘 다 First 축을 대체하므로 함께 켜지면 안 된다."""
        self.win._cb_field_time.setChecked(True)
        self.addCleanup(self.win._cb_field_time.setChecked, False)
        self.enable_power()
        self.assertFalse(self.win._cb_field_time.isChecked())
        self.assertFalse(self.win._cb_field_time.isEnabled())

    def test_survives_save_and_restore(self):
        self.enable_power(start="-25", stop="-5", n="9")
        self.win._cfg.ds_control = self.win._collect_ds_control()
        self.win._cb_power_mode.setChecked(False)
        self.win._le_pw_start.setText("0")
        self.win._apply_ds_control()
        self.assertTrue(self.win._cb_power_mode.isChecked())
        self.assertEqual("-25", self.win._le_pw_start.text())
        self.assertEqual("9", self.win._le_pw_n.text())


class TestFinishReturn(VnaTestCase):
    """측정 완료 후 축 복귀 — 켜지 않으면 마지막 값에 그대로 멈춰야 한다.

    여기가 틀리면 측정이 끝난 뒤 자기장이 최대값에 남거나(끄고 싶었는데 켜짐),
    반대로 복귀 지시가 계획에 안 실려 조용히 무시된다.
    """

    def setUp(self):
        for cb in (self.win._cb_ret_first, self.win._cb_ret_second):
            cb.setChecked(False)
            self.addCleanup(cb.setChecked, False)

    def _plan(self):
        first = _FirstChannel(cmd=object(), values=[0.0], advance=None,
                              field_time=False)
        second = _SecondChannel(enabled=False, cmd=None, values=[], advance=None)
        return self.win._build_ds_plan(first, second, 0)

    def test_off_by_default_so_axes_stay_put(self):
        ret = self._plan()["finish_return"]
        self.assertFalse(ret["first_enabled"])
        self.assertFalse(ret["second_enabled"])

    def test_values_reach_the_plan(self):
        self.win._cb_ret_first.setChecked(True)
        self.win._le_ret_first.setText("-30")
        self.win._cb_ret_second.setChecked(True)
        self.win._le_ret_second.setText("0")
        ret = self._plan()["finish_return"]
        self.assertTrue(ret["first_enabled"])
        self.assertEqual(-30.0, ret["first_value"])
        self.assertTrue(ret["second_enabled"])
        self.assertEqual(0.0, ret["second_value"])

    def test_value_field_follows_checkbox(self):
        self.assertFalse(self.win._le_ret_first.isEnabled())
        self.win._cb_ret_first.setChecked(True)
        self.assertTrue(self.win._le_ret_first.isEnabled())

    def test_survives_save_and_restore(self):
        self.win._cb_ret_second.setChecked(True)
        self.win._le_ret_second.setText("0.5")
        self.win._cfg.ds_control = self.win._collect_ds_control()
        self.win._cb_ret_second.setChecked(False)
        self.win._le_ret_second.setText("99")
        self.win._apply_ds_control()
        self.assertTrue(self.win._cb_ret_second.isChecked())
        self.assertEqual("0.5", self.win._le_ret_second.text())


def _make_worker():
    """VISA 를 타지 않는 _AcquireWorker (테스트에서 메서드를 갈아끼워 쓴다)."""
    from pythonization.ui.modules.vna.window import _AcquireWorker
    from pythonization.ui.modules.vna.models import VnaAcquireConfig
    return _AcquireWorker(session=None, lib_reg=None, acq_cfg=VnaAcquireConfig())


class TestPowerSweepSequence(unittest.TestCase):
    """Power 모드 한 사이클의 순서 — 여기가 어긋나면 측정값이 조용히 틀어진다.

    자기장이 아직 램프 중인데 측정하거나, power 를 옮기자마자 읽으면 데이터는
    정상적으로 저장되지만 값이 틀린다. 나중에 알아채기 가장 어려운 종류라 순서를
    통째로 고정한다.
    """

    POWER = [-20.0, -15.0, -10.0]
    FIELD = [0.0, 0.5, 1.0]

    def _run(self, *, direction="uni", power=None, second_enabled=True):
        w = _make_worker()
        self.log = []
        w._advance = lambda cmd, adv, v, prev: self.log.append(("move", cmd, v))
        w._run_cmd_list = lambda cmds, v: self.log.append(("cmd", tuple(cmds), v))
        w._pw_wait_arrival = lambda p, t, prev, ctx="": self.log.append(("arrive", t))
        w._sleep_progress = lambda s, label: self.log.append(("wait", s))
        w._acquire_once = lambda i, first_step, extra_values=(): self.log.append(
            ("meas", extra_values[0], extra_values[1] if len(extra_values) > 1 else None))
        w._run_pre_cmds = lambda phase: self.log.append(("pre_cmds", phase))
        if power is None:
            power = {"pre_measure_s": 5.0, "post_measure_s": 5.0,
                     "field_rate": 0.3, "rate_cmds": ["RFST"], "go_cmds": ["RTOS"],
                     "field_settle_s": 60.0}
        w._ds_plan = {
            "first_cmd": "POWER", "first_values": self.POWER, "first_adv": None,
            "second_enabled": second_enabled, "second_cmd": "FIELD",
            "second_values": self.FIELD, "second_adv": None,
            "direction": direction, "pre_specs": [], "field_time": None,
            "second_index_offset": 0, "second_table": None,
            "field_initial_sweep": False, "dummy_measure": False,
            "power": power,
            "finish_return": {"first_enabled": False, "first_value": 0.0,
                              "second_enabled": False, "second_value": 0.0},
        }
        w._run_double_sweep()
        return self.log

    def test_field_point_sequence(self):
        """자기장 한 점: 속도 → 목표 → ramp 시작 → 도달 → 안정화 대기."""
        log = self._run()
        self.assertEqual(
            [("cmd", ("RFST",), 0.3), ("move", "FIELD", 0.0),
             ("cmd", ("RTOS",), 0.0), ("arrive", 0.0), ("wait", 60.0)],
            log[:5],
            "속도를 먼저 보내고, 목표 뒤에 ramp 트리거, 도달 확인 후 안정화 대기")

    def test_power_point_sequence(self):
        """power 한 점: 이동 → 측정 전 대기 → 측정 → 측정 후 대기."""
        log = self._run()
        self.assertEqual(
            [("move", "POWER", -20.0), ("wait", 5.0),
             ("meas", -20.0, 0.0), ("wait", 5.0)],
            log[5:9])

    def test_every_power_point_measured_at_every_field_point(self):
        log = self._run()
        meas = [(x[1], x[2]) for x in log if x[0] == "meas"]
        self.assertEqual([(p, f) for f in self.FIELD for p in self.POWER], meas,
                         "자기장 한 점마다 power 를 Start→Stop 으로 전부 훑어야 한다")

    def test_power_restarts_from_start_even_in_multi_direction(self):
        """방향 콤보가 '다중방향'이어도 power 는 항상 Start→Stop 이다."""
        log = self._run(direction="multi")
        meas = [(x[1], x[2]) for x in log if x[0] == "meas"]
        self.assertEqual([(p, f) for f in self.FIELD for p in self.POWER], meas)

    def test_pre_cmds_never_fire_in_power_mode(self):
        """pre_cmds 는 first 축이 자기장일 때를 위한 것 — power 모드에선 나오면 안 된다."""
        log = self._run()
        self.assertEqual([], [x for x in log if x[0] == "pre_cmds"])

    def test_optional_commands_are_skipped_when_empty(self):
        """속도·ramp 명령을 등록하지 않았으면 전송 시도조차 하지 않는다."""
        log = self._run(power={"pre_measure_s": 0.0, "post_measure_s": 0.0,
                               "field_rate": 0.3, "rate_cmds": [], "go_cmds": [],
                               "field_settle_s": 60.0})
        self.assertEqual([], [x for x in log if x[0] == "cmd"])
        self.assertEqual(("move", "FIELD", 0.0), log[0])

    def test_no_field_moves_when_second_disabled(self):
        log = self._run(second_enabled=False)
        self.assertEqual([], [x for x in log if x[0] == "arrive"])
        self.assertEqual(len(self.POWER), len([x for x in log if x[0] == "meas"]))


class TestBlankSweepValueNotWritten(unittest.TestCase):
    """sweep 값이 없을 때 sweep 명령을 장비로 내보내면 안 된다.

    Single Acquire·Time 모드는 sweep 명령 목록 **전체**를 빈 값으로 부른다. 그대로
    write 하면 ':CALC1:FILT:TIME:STAR ' 나 'SET:…:FSET:;…:ACTN:RTOS' 처럼 인자가 빠진
    명령이 나가서, 측정과 무관하게 게이팅 시작점이나 자기장 목표가 조용히 바뀐다.
    """

    def _worker(self):
        from pythonization.ui.modules.vna.window import _AcquireWorker
        from pythonization.ui.modules.vna.models import VnaAcquireConfig

        class Lib:
            write_cmds = []
            sweep_values = []
            measurements = []

        class Reg:
            def get_library(self, alias): return Lib()

        class Sess:
            def __init__(self): self.writes = []
            def is_open(self, a): return True
            def open(self, a): pass
            def write(self, a, c): self.writes.append((a, c))

        w = _AcquireWorker(session=Sess(), lib_reg=Reg(), acq_cfg=VnaAcquireConfig())
        # 템플릿 조회를 우회해 명령 문자열만 본다
        import pythonization.ui.modules.vna.window as mod
        self._orig = mod.get_template
        mod.get_template = lambda lib, e: self._templates[e.description]
        self.addCleanup(setattr, mod, "get_template", self._orig)
        return w

    def _entry(self, desc, template, user_param=None, fixed=None):
        from pythonization.ui.modules.vna.models import VnaCommandEntry, VnaParamSpec
        params = []
        for name, val in (fixed or {}).items():
            params.append(VnaParamSpec(name=name, value=val, is_user_input=False))
        if user_param:
            params.append(VnaParamSpec(name=user_param, value="", is_user_input=True))
        self._templates[desc] = template
        return VnaCommandEntry(alias="VNA", description=desc, cmd_type="write",
                               params=params, enabled=True)

    def setUp(self):
        self._templates = {}

    def test_value_needing_command_is_skipped_when_blank(self):
        w = self._worker()
        gating = self._entry("tdstart", ":CALC{ch}:FILT:TIME:STAR {start}",
                             user_param="start", fixed={"ch": "1"})
        w._exec_write_cmds([gating], "")
        self.assertEqual([], w._session.writes,
                         "빈 값으로 게이팅 명령을 보내면 안 된다")

    def test_same_command_is_sent_when_a_value_exists(self):
        w = self._worker()
        gating = self._entry("tdstart", ":CALC{ch}:FILT:TIME:STAR {start}",
                             user_param="start", fixed={"ch": "1"})
        w._exec_write_cmds([gating], "-20")
        self.assertEqual([("VNA", ":CALC1:FILT:TIME:STAR -20")], w._session.writes)

    def test_command_without_user_input_still_runs(self):
        """고정 명령(예: :INIT1)은 값과 무관하므로 그대로 보내야 한다."""
        w = self._worker()
        init = self._entry("start_sweep", ":INIT{ch}", fixed={"ch": "1"})
        w._exec_write_cmds([init], "")
        self.assertEqual([("VNA", ":INIT1")], w._session.writes)

    def test_skip_is_announced_once_per_command(self):
        w = self._worker()
        msgs = []
        w.progress.connect(msgs.append)
        gating = self._entry("tdstart", ":CALC{ch}:FILT:TIME:STAR {start}",
                             user_param="start", fixed={"ch": "1"})
        for _ in range(3):                     # Time 모드처럼 여러 번 불러도
            w._exec_write_cmds([gating], "")
        self.assertEqual(1, len([m for m in msgs if "tdstart" in m]),
                         "매 스텝 같은 안내를 반복하면 안 된다")

    def test_disabled_command_never_runs(self):
        w = self._worker()
        e = self._entry("off", ":INIT{ch}", fixed={"ch": "1"})
        e.enabled = False
        w._exec_write_cmds([e], "1")
        self.assertEqual([], w._session.writes)


class TestPowerFieldArrival(unittest.TestCase):
    """자기장 도달 판정 — Phase 1(도달) 뒤에 Phase 2(안정화)까지 거쳐야 한다.

    band 안에 들어온 직후는 아직 출렁이는 구간이라, 여기서 바로 측정에 들어가면
    데이터는 정상으로 보이지만 자기장이 미세하게 흔들리는 상태에서 찍힌다.
    """

    class _Sec:
        """SecondChannelWorker 대역 — 어느 Phase 가 불렸는지만 기록한다."""
        def __init__(self, target_ok=True):
            self.calls = []
            self._target_ok = target_ok
        def _await_feedback_target(self, alias, ch, target, distance):
            self.calls.append(("phase1", target, round(distance, 6)))
            return self._target_ok
        def _await_feedback_stability(self, alias, ch, target):
            self.calls.append(("phase2", target))

    def _worker(self, std_window=10, std_threshold=0.005, read_cmd="READ:FLD",
                target_ok=True):
        from pythonization.ui.modules.vna.window import _AcquireWorker
        from pythonization.ui.modules.vna.models import (
            VnaAcquireConfig, VnaAdvanceConfig, VnaCommandEntry, VnaParamSpec,
        )
        from pythonization.config.models import SecondSweepAdvanceType

        w = _AcquireWorker(session=None, lib_reg=None, acq_cfg=VnaAcquireConfig())
        sec = self._Sec(target_ok=target_ok)
        w._ensure_sec = lambda: sec
        w._make_sec_channel = lambda cmd, adv: type("Ch", (), {
            "feedback_std_window": std_window,
            "feedback_std_threshold": std_threshold})()
        w._read_current = lambda cmd, adv: 0.0
        w._session = type("S", (), {"is_open": lambda s, a: True,
                                    "open": lambda s, a: None})()
        adv = VnaAdvanceConfig(advance_type=SecondSweepAdvanceType.FEEDBACK,
                               feedback_read_cmd=read_cmd,
                               feedback_std_window=std_window,
                               feedback_std_threshold=std_threshold)
        cmd = VnaCommandEntry(alias="IPS", description="field", cmd_type="write",
                              params=[VnaParamSpec(name="v", is_user_input=True)],
                              sweep_kind="controlled", advance=adv)
        return w, sec, {"second_cmd": cmd, "second_adv": adv}

    def test_arrival_then_stabilization(self):
        w, sec, p = self._worker()
        w._pw_wait_arrival(p, target=1.0, prev=0.0)
        self.assertEqual([("phase1", 1.0, 1.0), ("phase2", 1.0)], sec.calls)

    def test_stabilization_runs_even_when_already_at_target(self):
        """도달했다고 안정된 것은 아니다 — 이동 거리가 0이어도 흔들림은 확인한다."""
        w, sec, p = self._worker()
        w._pw_wait_arrival(p, target=1.0, prev=1.0)
        self.assertEqual([("phase2", 1.0)], sec.calls)

    def test_stabilization_skipped_when_channel_has_no_std_settings(self):
        w, sec, p = self._worker(std_window=0, std_threshold=0.0)
        w._pw_wait_arrival(p, target=1.0, prev=0.0)
        self.assertEqual([("phase1", 1.0, 1.0)], sec.calls)

    def test_stop_during_arrival_skips_stabilization(self):
        """Phase 1 이 Stop 으로 끝났으면 Phase 2 로 넘어가면 안 된다."""
        w, sec, p = self._worker(target_ok=False)
        w._pw_wait_arrival(p, target=1.0, prev=0.0)
        self.assertEqual([("phase1", 1.0, 1.0)], sec.calls)

    def test_no_read_cmd_means_no_waiting_at_all(self):
        w, sec, p = self._worker(read_cmd="")
        w._pw_wait_arrival(p, target=1.0, prev=0.0)
        self.assertEqual([], sec.calls)


class TestSleepProgress(unittest.TestCase):
    """고정 대기는 Stop 을 눌렀을 때 바로 빠져나와야 한다 (1분씩 붙잡히면 안 된다)."""

    def test_zero_or_negative_returns_immediately(self):
        w = _make_worker()
        emitted = []
        w.progress.connect(emitted.append)
        w._sleep_progress(0, "x")
        w._sleep_progress(-5, "x")
        self.assertEqual([], emitted)

    def test_stop_flag_breaks_out(self):
        import time as _t
        w = _make_worker()
        w._stop_flag = True
        t0 = _t.perf_counter()
        w._sleep_progress(30, "x")            # 30초짜리 대기
        self.assertLess(_t.perf_counter() - t0, 1.0, "Stop 이면 즉시 반환해야 한다")

    def test_actually_waits_when_running(self):
        import time as _t
        w = _make_worker()
        t0 = _t.perf_counter()
        w._sleep_progress(0.2, "x")
        self.assertGreaterEqual(_t.perf_counter() - t0, 0.15)


class TestPowerModeWarnings(VnaTestCase):
    """시작 전 경고 — 잡아 주지 않으면 '램프 중 측정'을 모르고 지나간다.

    사용자 프로파일에 무엇이 등록돼 있든 결과가 같도록, second 명령 목록을 이
    테스트가 직접 만들어 넣는다.
    """

    def setUp(self):
        from pythonization.ui.modules.vna.models import (
            VnaAdvanceConfig, VnaCommandEntry, VnaParamSpec,
        )
        from pythonization.config.models import SecondSweepAdvanceType

        def field(kind, read_cmd, pre_cmds=()):
            return VnaCommandEntry(
                alias="IPS", description="field", cmd_type="write",
                params=[VnaParamSpec(name="v", is_user_input=True)],
                figure_axis="field", sweep_kind=kind,
                advance=VnaAdvanceConfig(
                    advance_type=SecondSweepAdvanceType.FEEDBACK,
                    feedback_read_cmd=read_cmd, pre_cmds=list(pre_cmds)))

        self._field = field
        self._orig_cmds = self.win._cfg.acquire.sweep_cmds
        self.addCleanup(setattr, self.win._cfg.acquire, "sweep_cmds", self._orig_cmds)
        self.win._cb_second_enable.setChecked(True)
        self.addCleanup(self.win._cb_second_enable.setChecked, False)
        self.win._pw_rate_cmds = []
        self.win._le_pw_rate.setText("0.3")

    def _use(self, cmd):
        self.win._cfg.acquire.sweep_cmds = [cmd]
        self.win._combo_second_cmd.setCurrentIndex(0)

    def test_warns_when_second_channel_disabled(self):
        self.win._cb_second_enable.setChecked(False)
        w = self.win._power_mode_warnings()
        self.assertEqual(1, len(w))
        self.assertIn("자기장", w[0])

    def test_warns_without_arrival_read_cmd(self):
        self._use(self._field("controlled", ""))     # Read Cmd 없음
        w = self.win._power_mode_warnings()
        self.assertTrue(any("Read Cmd" in x for x in w),
                        "도달 확인 수단이 없으면 반드시 알려야 한다")

    def test_no_arrival_warning_when_read_cmd_present(self):
        self._use(self._field("controlled", "READ:DEV:GRPZ:PSU:SIG:FLD"))
        w = self.win._power_mode_warnings()
        self.assertFalse(any("Read Cmd" in x for x in w))

    def test_general_kind_has_no_advance_so_it_warns(self):
        """Controlled 로 등록하지 않으면 도달 확인 자체가 불가능하다."""
        self._use(self._field("general", "READ:DEV:GRPZ:PSU:SIG:FLD"))
        w = self.win._power_mode_warnings()
        self.assertTrue(any("Read Cmd" in x for x in w))

    def test_warns_when_no_rate_command_anywhere(self):
        self._use(self._field("controlled", "READ:X"))
        w = self.win._power_mode_warnings()
        self.assertTrue(any("속도 명령" in x for x in w))

    def test_second_channel_pre_cmds_count_as_the_rate_command(self):
        """속도 명령은 보통 그 채널의 'Advance 전 명령'에 이미 달려 있다."""
        from pythonization.ui.modules.vna.models import VnaPreAdvanceCmd
        speed = VnaPreAdvanceCmd(alias="IPS", description="write_IPS_z_Bfield_speed",
                                 label="Bfield_speed")
        self._use(self._field("controlled", "READ:X", pre_cmds=[speed]))
        w = self.win._power_mode_warnings()
        self.assertFalse(any("속도 명령" in x for x in w))
        self.assertEqual([speed], self.win._pw_effective_rate_cmds())

    def test_own_rate_command_overrides_channel_pre_cmds(self):
        from pythonization.ui.modules.vna.models import VnaPreAdvanceCmd
        channel_cmd = VnaPreAdvanceCmd(alias="IPS", description="from_channel")
        mine = VnaPreAdvanceCmd(alias="IPS", description="from_window")
        self._use(self._field("controlled", "READ:X", pre_cmds=[channel_cmd]))
        self.win._pw_rate_cmds = [mine]
        self.assertEqual([mine], self.win._pw_effective_rate_cmds())

    def test_warns_above_ips_limit_only_when_rate_is_sent(self):
        from pythonization.ui.modules.vna.models import VnaPreAdvanceCmd
        self._use(self._field("controlled", "READ:X"))
        self.win._le_pw_rate.setText("0.5")
        self.assertFalse(any("0.3" in x for x in self.win._power_mode_warnings()),
                         "속도 명령이 없으면 값이 안 나가므로 한계 경고도 의미 없다")
        self.win._pw_rate_cmds = [VnaPreAdvanceCmd(alias="IPS", description="RFST")]
        self.assertTrue(any("0.3" in x for x in self.win._power_mode_warnings()))


class TestPowerPlanFromUi(VnaTestCase):
    """UI 에 넣은 타이밍 값이 실제 계획에 실리는지."""

    def test_timings_reach_the_plan(self):
        self.win._le_pw_pre.setText("3")
        self.win._le_pw_post.setText("7")
        self.win._le_pw_rate.setText("0.25")
        self.win._le_pw_settle.setText("90")
        plan = self.win._build_power_plan()
        self.assertEqual(3.0, plan["pre_measure_s"])
        self.assertEqual(7.0, plan["post_measure_s"])
        self.assertEqual(0.25, plan["field_rate"])
        self.assertEqual(90.0, plan["field_settle_s"])

    def test_plan_omits_power_block_when_mode_off(self):
        self.win._cb_power_mode.setChecked(False)
        first = _FirstChannel(cmd=object(), values=[0.0], advance=None,
                              field_time=False)
        second = _SecondChannel(enabled=False, cmd=None, values=[], advance=None)
        self.assertIsNone(self.win._build_ds_plan(first, second, 0)["power"],
                          "모드가 꺼져 있으면 기존 double sweep 동작이 그대로여야 한다")

    def test_rate_warning_label_tracks_input(self):
        self.win._le_pw_rate.setText("0.5")
        self.assertIn("초과", self.win._lbl_pw_rate_warn.text())
        self.win._le_pw_rate.setText("0.3")
        self.assertEqual("", self.win._lbl_pw_rate_warn.text())
        self.win._le_pw_rate.setText("0")
        self.assertIn("0 보다", self.win._lbl_pw_rate_warn.text())


class TestFinishReturnRun(unittest.TestCase):
    """워커가 복귀를 '언제' 부르는지 — Stop 으로 끊겼으면 부르면 안 된다.

    Stop 상황에서는 사용자가 등록한 Stop 명령(예: 자기장 HOLD)이 나가야 하는데,
    여기서 복귀까지 겹치면 멈추라고 해 놓고 다시 움직이는 꼴이 된다.
    """

    def _worker(self, stop_flag: bool):
        from pythonization.ui.modules.vna.window import _AcquireWorker
        from pythonization.ui.modules.vna.models import VnaAcquireConfig
        w = _AcquireWorker(session=None, lib_reg=None, acq_cfg=VnaAcquireConfig())
        w._stop_flag = stop_flag
        self.moved = []
        w._advance = lambda cmd, adv, value, prev: self.moved.append((cmd, value))
        return w

    def _plan(self, **ret):
        base = {"first_enabled": False, "first_value": 0.0,
                "second_enabled": False, "second_value": 0.0}
        base.update(ret)
        return {"first_cmd": "POWER", "first_adv": None,
                "second_cmd": "FIELD", "second_adv": None, "second_enabled": True,
                "finish_return": base}

    def test_first_moves_before_second(self):
        w = self._worker(stop_flag=False)
        w._run_finish_return(self._plan(first_enabled=True, first_value=-30.0,
                                        second_enabled=True, second_value=0.0),
                             first_pos=0.0, second_prev=2.0)
        self.assertEqual([("POWER", -30.0), ("FIELD", 0.0)], self.moved,
                         "power 를 먼저 내린 뒤 자기장을 옮겨야 한다")

    def test_nothing_moves_when_disabled(self):
        w = self._worker(stop_flag=False)
        w._run_finish_return(self._plan(), first_pos=0.0, second_prev=2.0)
        self.assertEqual([], self.moved)

    def test_stop_during_return_aborts_remaining_axes(self):
        """복귀 도중 Stop 을 누르면 남은 축은 건드리지 않는다.

        자기장 하강은 몇 분씩 걸리므로 그 사이 Stop 이 들어올 수 있다.
        """
        w = self._worker(stop_flag=False)
        plan = self._plan(first_enabled=True, first_value=-30.0,
                          second_enabled=True, second_value=0.0)

        def stop_after_first(cmd, adv, value, prev):
            self.moved.append((cmd, value))
            w._stop_flag = True          # power 복귀 직후 Stop
        w._advance = stop_after_first
        w._run_finish_return(plan, first_pos=0.0, second_prev=2.0)
        self.assertEqual([("POWER", -30.0)], self.moved,
                         "Stop 이후 자기장까지 옮기면 안 된다")

    def test_second_skipped_when_second_channel_unused(self):
        w = self._worker(stop_flag=False)
        plan = self._plan(second_enabled=True, second_value=0.0)
        plan["second_enabled"] = False          # second 채널 자체를 안 씀
        w._run_finish_return(plan, first_pos=0.0, second_prev=None)
        self.assertEqual([], self.moved)

    def test_failure_warns_but_does_not_error(self):
        """복귀 실패는 warn 으로 — error 면 정상 완료가 '중단됨'이 되어 resume 이 남는다."""
        w = self._worker(stop_flag=False)
        def boom(cmd, adv, value, prev):
            raise RuntimeError("timeout")
        w._advance = boom
        warns, errors = [], []
        w.warn.connect(warns.append)
        w.error.connect(errors.append)
        w._run_finish_return(self._plan(first_enabled=True, first_value=0.0),
                             first_pos=1.0, second_prev=None)
        self.assertEqual(1, len(warns))
        self.assertIn("복귀 실패", warns[0])
        self.assertEqual([], errors)


class TestSecondChannelModel(unittest.TestCase):
    def test_is_active_needs_both_enabled_and_cmd(self):
        self.assertFalse(_SecondChannel(True, None, [], None).is_active)
        self.assertFalse(_SecondChannel(False, object(), [], None).is_active)
        self.assertTrue(_SecondChannel(True, object(), [1.0], None).is_active)


if __name__ == "__main__":
    unittest.main()
