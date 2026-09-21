"""Double Sweep+ (Cycle) 모듈의 순수 로직 + 창 배선 검증.

여기서 잡으려는 것:
  - second array 생성 (내려가는 방향은 step 이 음수 — 부호를 놓치면 한 점만 돈다)
  - ITC/IPS 장비 필터 (드라이버 클래스로 판정, 구 경로도 인식)
  - 예상 소요 시간에 Settle Wait 가 array 점 개수만큼 들어가는지
    (1시간 × 10점을 빼먹으면 '30분'이라 표시하고 10시간을 돈다)
  - 첫 값 대기 건너뛰기가 대기 횟수를 하나 줄이는지
  - 설정이 프로파일에 저장/복원되는지
  - second 값 × cycle 번호가 파일명에 모두 들어가는지 (하나만 들어가면 덮어쓴다)
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication            # noqa: E402

from pythonization.config.models import (             # noqa: E402
    CycleDoubleSweepConfig,
    InstrumentConfig,
)
from pythonization.profiles.registry import ProfileRegistry                 # noqa: E402
from pythonization.ui.modules.cycle_double_sweep.window import (            # noqa: E402
    CycleDoubleSweepWindow,
    Phase,
    estimate_total_seconds,
    generate_array,
    matches_device_kind,
)

_app = QApplication.instance() or QApplication([])


class _FakeRegistry:
    """InstrumentRegistry.get_config 만 흉내 내는 최소 스텁."""

    def __init__(self, configs):
        self._configs = configs

    def get_config(self, alias):
        return self._configs.get(alias)


def _inst(alias, class_name):
    return InstrumentConfig(alias=alias, class_name=class_name,
                            interface_type="LAN", address="1.2.3.4")


class TestGenerateArray(unittest.TestCase):
    def test_descending_needs_negative_step(self):
        cfg = CycleDoubleSweepConfig(array_from=300, array_to=260, array_step=-20)
        self.assertEqual([300.0, 280.0, 260.0], generate_array(cfg))

    def test_ascending(self):
        cfg = CycleDoubleSweepConfig(array_from=0, array_to=1, array_step=0.5)
        self.assertEqual([0.0, 0.5, 1.0], generate_array(cfg))

    def test_endpoint_included_despite_float_error(self):
        cfg = CycleDoubleSweepConfig(array_from=0, array_to=0.3, array_step=0.1)
        self.assertEqual(4, len(generate_array(cfg)),
                         "0.1 누적 오차로 끝점이 빠지면 안 된다")

    def test_zero_step_is_single_point(self):
        cfg = CycleDoubleSweepConfig(array_from=5, array_to=100, array_step=0)
        self.assertEqual([5.0], generate_array(cfg))

    def test_wrong_sign_step_falls_back_to_single_point(self):
        # 300 → 100 인데 step 이 +20 이면 한 점도 못 만든다 → 시작점 하나
        cfg = CycleDoubleSweepConfig(array_from=300, array_to=100, array_step=20)
        self.assertEqual([300.0], generate_array(cfg))


class TestDeviceKindFilter(unittest.TestCase):
    def setUp(self):
        self.reg = _FakeRegistry({
            "ITC": _inst("ITC",
                         "pythonization.instruments.drivers.oxford_itc.OxfordITC"),
            "IPS": _inst("IPS",
                         "pythonization.instruments.drivers.oxford_ips.OxfordIPS"),
            "K":   _inst("K",
                         "pythonization.instruments.drivers.keithley_2636a.Keithley2636A"),
            "OLD": _inst("OLD", "driver.oxford_itc.OxfordITC"),   # 구 레이아웃 경로
        })

    def test_itc_matches_only_itc(self):
        self.assertTrue(matches_device_kind(self.reg, "ITC", "itc"))
        self.assertFalse(matches_device_kind(self.reg, "IPS", "itc"))
        self.assertFalse(matches_device_kind(self.reg, "K", "itc"))

    def test_ips_matches_only_ips(self):
        self.assertTrue(matches_device_kind(self.reg, "IPS", "ips"))
        self.assertFalse(matches_device_kind(self.reg, "ITC", "ips"))

    def test_legacy_class_path_matches(self):
        self.assertTrue(matches_device_kind(self.reg, "OLD", "itc"))

    def test_all_accepts_everything_including_unknown(self):
        self.assertTrue(matches_device_kind(self.reg, "K", "all"))
        self.assertTrue(matches_device_kind(self.reg, "없는거", "all"))

    def test_unknown_alias_rejected_by_specific_kind(self):
        self.assertFalse(matches_device_kind(self.reg, "없는거", "itc"))


class TestEstimate(unittest.TestCase):
    """이동 시간 = 거리 × 60 / rate. rate=60 이면 1 unit = 1 초."""

    def _cfg(self, **kw):
        base = dict(initial_value=0.0, targets=[10.0], sweep_rate=60.0,
                    cycles=1, settle_wait_s=0.0,
                    array_from=0.0, array_to=0.0, array_step=0.0)
        base.update(kw)
        return CycleDoubleSweepConfig(**base)

    def test_single_point_single_cycle(self):
        # 첫 점은 '현재값 → initial' 을 알 수 없어 빼고, cycle = 0→10 = 10s
        cfg = self._cfg()
        self.assertAlmostEqual(10.0, estimate_total_seconds(cfg, cfg.targets, 1))

    def test_array_points_add_a_return_leg_each(self):
        # 점마다 cycle 10s, 점 사이마다 마지막 target 10 → initial 0 복귀 10s
        cfg = self._cfg()
        self.assertAlmostEqual(3 * 10.0 + 2 * 10.0,
                               estimate_total_seconds(cfg, cfg.targets, 3))

    def test_settle_wait_counted_once_per_array_point(self):
        cfg = self._cfg(settle_wait_s=3600.0)
        moves = 5 * 10.0 + 4 * 10.0
        self.assertAlmostEqual(moves + 5 * 3600.0,
                               estimate_total_seconds(cfg, cfg.targets, 5),
                               msg="Settle Wait 가 점 개수만큼 들어가야 한다")

    def test_skip_first_wait_removes_one_wait(self):
        cfg = self._cfg(settle_wait_s=3600.0, skip_first_wait=True)
        moves = 5 * 10.0 + 4 * 10.0
        self.assertAlmostEqual(moves + 4 * 3600.0,
                               estimate_total_seconds(cfg, cfg.targets, 5))

    def test_cycles_multiply_within_a_point(self):
        # cycle 2 는 직전 cycle 의 마지막 target(10)에서 시작 → 0 거리... 는 아니고
        # targets=[10] 이므로 10→10 = 0. 대신 targets 를 두 개 준다.
        cfg = self._cfg(targets=[10.0, 0.0], cycles=2)
        # 복귀 0 (마지막 target 0 == initial 0) + cycle1(10+10) + cycle2(10+10) = 40
        self.assertAlmostEqual(40.0, estimate_total_seconds(cfg, cfg.targets, 1))

    def test_return_to_zero_adds_last_leg(self):
        cfg = self._cfg(targets=[10.0], return_to_zero=True)
        self.assertAlmostEqual(20.0, estimate_total_seconds(cfg, cfg.targets, 1))

    def test_no_targets_is_zero_not_crash(self):
        cfg = self._cfg(targets=[])
        self.assertEqual(0.0, estimate_total_seconds(cfg, [], 3))

    def test_zero_rate_is_zero_not_crash(self):
        cfg = self._cfg(sweep_rate=0.0)
        self.assertEqual(0.0, estimate_total_seconds(cfg, cfg.targets, 3))


class TestStepFileName(unittest.TestCase):
    """second 값과 cycle 번호가 둘 다 파일명에 들어가야 한다.

    하나라도 빠지면 같은 이름으로 계속 덮어써서 측정이 통째로 사라진다.
    """

    name = staticmethod(CycleDoubleSweepWindow._step_file_name)

    def test_contains_second_value_and_cycle(self):
        out = self.name(280.0, 3, "T", "sampleA", False)
        self.assertEqual("sampleA_T_280_cycle003.dat", out)

    def test_distinct_per_second_value(self):
        a = self.name(300.0, 1, "T", "s", False)
        b = self.name(280.0, 1, "T", "s", False)
        self.assertNotEqual(a, b)

    def test_distinct_per_cycle(self):
        a = self.name(300.0, 1, "T", "s", False)
        b = self.name(300.0, 2, "T", "s", False)
        self.assertNotEqual(a, b)

    def test_negative_value_kept_plus_stripped(self):
        self.assertIn("-1.5", self.name(-1.5, 1, "B", "s", False))
        self.assertNotIn("+", self.name(1e30, 1, "B", "s", False))

    def test_no_axis_no_stem_still_valid(self):
        self.assertEqual("300_cycle001.dat", self.name(300.0, 1, "", "", False))

    def test_placeholder_preview(self):
        self.assertEqual("s_T_<2nd>_cycleNNN.dat",
                         self.name("<2nd>", "NNN", "T", "s", False))


class TestCycleDoubleSweepWindow(unittest.TestCase):
    """창이 계측기 없이 생성되고 설정이 왕복되는지.

    프로파일 저장이 사용자의 실제 settings 를 건드리지 않도록 임시 디렉토리에
    ProfileRegistry 를 따로 만든다.
    """

    @classmethod
    def setUpClass(cls):
        from pythonization.ui.main_window import MainWindow
        cls._tmp = tempfile.TemporaryDirectory()
        cls.reg = ProfileRegistry(settings_dir=Path(cls._tmp.name))
        cls.main = MainWindow(profile_registry=cls.reg)
        cls.main._open_cycle_double_sweep()
        cls.win = cls.main._cycle_double_sweep_window

    @classmethod
    def tearDownClass(cls):
        cls.main.deleteLater()
        cls._tmp.cleanup()

    def test_window_created_and_idle(self):
        self.assertIsNotNone(self.win)
        self.assertTrue(self.win.is_idle())
        self.assertEqual(Phase.IDLE, self.win._phase)

    def test_reopen_reuses_instance(self):
        self.main._open_cycle_double_sweep()
        self.assertIs(self.win, self.main._cycle_double_sweep_window)

    def test_settle_fields_round_trip_through_seconds(self):
        self.win._set_settle_fields(5400.0)          # 1시간 30분
        self.assertEqual("1", self.win._le_settle_h.text())
        self.assertEqual("30", self.win._le_settle_m.text())
        self.assertAlmostEqual(5400.0, self.win._settle_seconds())

    def test_settle_seconds_ignores_garbage(self):
        self.win._le_settle_h.setText("")
        self.win._le_settle_m.setText("몇분")
        self.assertEqual(0.0, self.win._settle_seconds())

    def _set_targets(self, values):
        self.win._clear_target_rows()
        for v in values:
            self.win._add_target_row(v)
        self.win._on_cycle_inputs_changed()

    def test_blank_target_row_skipped_non_numeric_is_error(self):
        self._set_targets([30.0])
        self.win._add_target_row()                   # 빈 칸
        self.assertEqual([30.0], self.win._collect_targets())
        self.win._target_rows[0][1].setText("1e")    # 입력 중인 미완성 값
        self.assertIsNone(self.win._collect_targets())

    def test_config_round_trip_through_profile(self):
        self._set_targets([30.0, -30.0, 30.0, 0.0])
        self.win._le_arr_from.setText("300")
        self.win._le_arr_to.setText("260")
        self.win._le_arr_step.setText("-20")
        self.win._set_settle_fields(3600.0)
        self.win._cb_skip_first_wait.setChecked(True)
        self.win._sb_cycles.setValue(2)
        self.win._le_rate.setText("5")
        self.win._le_tpp.setText("0.5")
        self.win._le_file_name.setText("sampleA")
        self.win._save_config()

        cfg = self.reg.cycle_double_sweep_config
        self.assertEqual([30.0, -30.0, 30.0, 0.0], cfg.targets)
        self.assertEqual(300.0, cfg.array_from)
        self.assertEqual(-20.0, cfg.array_step)
        self.assertEqual(3600.0, cfg.settle_wait_s)
        self.assertTrue(cfg.skip_first_wait)
        self.assertEqual(2, cfg.cycles)
        self.assertEqual(5.0, cfg.sweep_rate)
        self.assertEqual("sampleA", cfg.file_name)

        # 위젯을 흐트러뜨린 뒤 다시 읽어도 같은 값으로 돌아온다
        self.win._le_arr_from.setText("0")
        self.win._sb_cycles.setValue(1)
        self.win._load_config()
        self.assertEqual("300", self.win._le_arr_from.text())
        self.assertEqual(2, self.win._sb_cycles.value())
        self.assertEqual([30.0, -30.0, 30.0, 0.0], self.win._collect_targets())

    def test_n_points_label_follows_array_inputs(self):
        self.win._le_arr_from.setText("300")
        self.win._le_arr_to.setText("260")
        self.win._le_arr_step.setText("-20")
        self.win._update_n_points()
        self.assertIn("3", self.win._lbl_n_points.text())

    def test_second_table_seeded_from_controls(self):
        self.win._le_arr_from.setText("300")
        self.win._le_arr_to.setText("280")
        self.win._le_arr_step.setText("-10")
        self.win._reset_second_table_from_controls()
        self.assertEqual([300.0, 290.0, 280.0], self.win._second_table_model.values())

    def test_lock_ui_leaves_table_button_usable(self):
        """측정 중에도 아직 측정 안 한 array 행은 고칠 수 있어야 한다."""
        self.win.lock_ui(True)
        self.addCleanup(self.win.lock_ui, False)
        self.assertFalse(self.win._le_arr_from.isEnabled())
        self.assertTrue(self.win._btn_second_table.isEnabled())


class TestSettleGate(unittest.TestCase):
    """second 설정 후 대기 판정 — 이 모듈의 핵심 동작.

    대기를 건너뛰면 아직 목표 온도에 닿지 않은 상태에서 cycle 이 돌아 데이터가
    조용히 틀어진다. 반대로 건너뛰기가 안 먹으면 이미 도달해 있는데도 1시간을 버린다.
    _begin_cycle 은 DataSaver/워커를 건드리므로 여기서는 호출만 기록한다.
    """

    @classmethod
    def setUpClass(cls):
        from pythonization.ui.main_window import MainWindow
        cls._tmp = tempfile.TemporaryDirectory()
        cls.main = MainWindow(
            profile_registry=ProfileRegistry(settings_dir=Path(cls._tmp.name)))
        cls.main._open_cycle_double_sweep()
        cls.win = cls.main._cycle_double_sweep_window

    @classmethod
    def tearDownClass(cls):
        cls.main.deleteLater()
        cls._tmp.cleanup()

    def setUp(self):
        self.calls = []
        self.win._begin_cycle = lambda idx: self.calls.append(idx)
        self.addCleanup(self.win.__dict__.pop, "_begin_cycle", None)
        self.addCleanup(self.win._settle_timer.stop)
        self.addCleanup(setattr, self.win, "_phase", Phase.IDLE)

    def _run(self, *, wait_s, skip_first, array_idx):
        self.win._cfg = CycleDoubleSweepConfig(settle_wait_s=wait_s,
                                               skip_first_wait=skip_first)
        self.win._array_idx = array_idx
        self.win._start_settling()

    def test_waits_and_defers_cycle(self):
        self._run(wait_s=3600.0, skip_first=False, array_idx=0)
        self.assertEqual(Phase.SETTLING, self.win._phase)
        self.assertEqual([], self.calls, "대기 중에는 cycle 이 시작되면 안 된다")
        self.assertTrue(self.win._settle_timer.isActive())
        self.assertIn("남음", self.win._lbl_countdown.text())

    def test_skip_first_only_applies_to_first_point(self):
        self._run(wait_s=3600.0, skip_first=True, array_idx=0)
        self.assertEqual([0], self.calls, "첫 점은 대기 없이 바로 cycle")
        self.assertNotEqual(Phase.SETTLING, self.win._phase)

        self.calls.clear()
        self._run(wait_s=3600.0, skip_first=True, array_idx=1)
        self.assertEqual([], self.calls, "두 번째 점부터는 대기해야 한다")
        self.assertEqual(Phase.SETTLING, self.win._phase)

    def test_zero_wait_starts_immediately(self):
        self._run(wait_s=0.0, skip_first=False, array_idx=2)
        self.assertEqual([0], self.calls)

    def test_tick_starts_cycle_once_deadline_passed(self):
        self._run(wait_s=3600.0, skip_first=False, array_idx=0)
        self.win._settle_deadline = 0.0        # 이미 지난 시각
        self.win._on_settle_tick()
        self.assertEqual([0], self.calls)
        self.assertFalse(self.win._settle_timer.isActive())
        self.assertEqual("", self.win._lbl_countdown.text())

    def test_tick_after_stop_does_nothing(self):
        """Stop 으로 IDLE 이 된 뒤 남아 있던 tick 이 측정을 되살리면 안 된다."""
        self._run(wait_s=3600.0, skip_first=False, array_idx=0)
        self.win._phase = Phase.IDLE
        self.win._settle_deadline = 0.0
        self.win._on_settle_tick()
        self.assertEqual([], self.calls)
        self.assertFalse(self.win._settle_timer.isActive())


if __name__ == "__main__":
    unittest.main()
