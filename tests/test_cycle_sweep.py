"""Cycle Sweep 모듈의 순수 로직 + 창 배선 검증.

여기서 잡으려는 것:
  - targets 파싱 (사용자가 쉼표/공백 섞어 적는다)
  - 예상 소요 시간 계산 (구간 거리 합 x 반복 횟수, 0 복귀 포함)
  - 2636A 필터 (드라이버 클래스로 판정, 구 경로도 인식)
  - 구간 전환 계약: 클램프된 마지막 스텝이 목표값을 기록하고, 그 다음 요청이
    is_done=True + 측정값 없음으로 돌아온다 (코너 값 중복 없음)
  - 설정이 프로파일에 저장/복원되는지
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication            # noqa: E402

from pythonization.config.models import CycleSweepConfig, InstrumentConfig  # noqa: E402
from pythonization.measurement.sweep import calculate_next_step             # noqa: E402
from pythonization.profiles.registry import ProfileRegistry                 # noqa: E402
from pythonization.ui.modules.cycle_sweep.window import (                   # noqa: E402
    CycleSweepPhase,
    CycleSweepWindow,
    _is_keithley_2636a,
    estimate_total_seconds,
    parse_target_value,
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


class TestParseTargetValue(unittest.TestCase):
    def test_plain_number(self):
        self.assertEqual(-30.0, parse_target_value(" -30 "))

    def test_scientific_notation(self):
        self.assertEqual(1e-3, parse_target_value("1e-3"))

    def test_blank_is_none(self):
        self.assertIsNone(parse_target_value("   "))

    def test_non_numeric_is_none(self):
        self.assertIsNone(parse_target_value("abc"))

    def test_nan_and_inf_rejected(self):
        self.assertIsNone(parse_target_value("nan"))
        self.assertIsNone(parse_target_value("inf"))


class TestEstimate(unittest.TestCase):
    def test_closed_loop_distance(self):
        # 0 -> 30 -> -30 -> 0 = 30 + 60 + 30 = 120 units, 60 units/min => 2 min
        cfg = CycleSweepConfig(initial_value=0.0, targets=[30, -30, 0],
                               sweep_rate=60.0, cycles=1)
        self.assertAlmostEqual(120.0, estimate_total_seconds(cfg, cfg.targets))

    def test_scales_with_cycles(self):
        cfg = CycleSweepConfig(initial_value=0.0, targets=[30, -30, 0],
                               sweep_rate=60.0, cycles=3)
        self.assertAlmostEqual(360.0, estimate_total_seconds(cfg, cfg.targets))

    def test_initial_value_changes_first_cycle_only(self):
        # 초기값 10 에서 시작: cycle1 = |30-10| + 60 + 30 = 110,
        # cycle2 는 마지막 target 0 에서 시작하므로 120 => 합 230
        cfg = CycleSweepConfig(initial_value=10.0, targets=[30, -30, 0],
                               sweep_rate=60.0, cycles=2)
        self.assertAlmostEqual(230.0, estimate_total_seconds(cfg, cfg.targets))

    def test_return_to_zero_adds_last_leg(self):
        # 초기값 0, target 10 하나: cycle = 10 units, 복귀 = 10 units
        cfg = CycleSweepConfig(initial_value=0.0, targets=[10],
                               sweep_rate=60.0, cycles=1, return_to_zero=True)
        self.assertAlmostEqual(20.0, estimate_total_seconds(cfg, cfg.targets))

    def test_zero_rate_is_zero_not_crash(self):
        cfg = CycleSweepConfig(targets=[1.0], sweep_rate=0.0, cycles=1)
        self.assertEqual(0.0, estimate_total_seconds(cfg, cfg.targets))


class TestKeithleyFilter(unittest.TestCase):
    def test_current_class_path_matches(self):
        reg = _FakeRegistry({"2636A": _inst(
            "2636A", "pythonization.instruments.drivers.keithley_2636a.Keithley2636A")})
        self.assertTrue(_is_keithley_2636a(reg, "2636A"))

    def test_legacy_class_path_matches(self):
        # 구 레이아웃 경로도 resolve_class_path 를 거쳐 인식돼야 한다
        reg = _FakeRegistry({"K": _inst("K", "driver.keithley_2636a.Keithley2636A")})
        self.assertTrue(_is_keithley_2636a(reg, "K"))

    def test_other_driver_rejected(self):
        reg = _FakeRegistry({"Zurich": _inst(
            "Zurich", "pythonization.instruments.drivers.lakeshore_m81.M81Instrument")})
        self.assertFalse(_is_keithley_2636a(reg, "Zurich"))

    def test_unknown_alias_rejected(self):
        self.assertFalse(_is_keithley_2636a(_FakeRegistry({}), "nope"))


class TestSegmentTransitionContract(unittest.TestCase):
    """calculate_next_step 이 구간 전환을 어떻게 알려 주는지 고정한다.

    창 코드는 'is_done=True 이고 측정값이 없으면 다음 구간'이라는 규칙 하나로
    전환을 판정한다. 그 전제가 깨지면 코너 값이 중복 기록되거나 사라진다.
    """

    def test_clamped_last_step_reports_target_and_not_done(self):
        # 9 -> 10, 스텝 크기 3 => 목표를 넘으므로 10 으로 클램프되고 is_done 은 False
        value, done = calculate_next_step(9.0, 10.0, 180.0, 1.0)
        self.assertAlmostEqual(10.0, value)
        self.assertFalse(done, "클램프된 스텝은 기록돼야 하므로 is_done 이면 안 된다")

    def test_next_request_at_target_is_done(self):
        value, done = calculate_next_step(10.0, 10.0, 180.0, 1.0)
        self.assertAlmostEqual(10.0, value)
        self.assertTrue(done)


class TestCycleSweepWindow(unittest.TestCase):
    """창 자체가 계측기 없이 생성되고 설정이 왕복되는지.

    프로파일 저장이 사용자의 실제 settings 를 건드리지 않도록 임시 디렉토리에
    ProfileRegistry 를 따로 만든다.
    """

    @classmethod
    def setUpClass(cls):
        from pythonization.ui.main_window import MainWindow
        cls._tmp = tempfile.TemporaryDirectory()
        cls.reg = ProfileRegistry(settings_dir=Path(cls._tmp.name))
        cls.main = MainWindow(profile_registry=cls.reg)
        cls.main._open_cycle_sweep()
        cls.win = cls.main._cycle_sweep_window

    @classmethod
    def tearDownClass(cls):
        cls.main.deleteLater()
        cls._tmp.cleanup()

    def test_window_created_and_idle(self):
        self.assertIsNotNone(self.win)
        self.assertTrue(self.win.is_idle())
        self.assertEqual(CycleSweepPhase.IDLE, self.win._phase)

    def test_reopen_reuses_instance(self):
        self.main._open_cycle_sweep()
        self.assertIs(self.win, self.main._cycle_sweep_window)

    def _set_targets(self, values):
        """행을 values 개수만큼 맞추고 값을 채운다."""
        self.win._clear_target_rows()
        for v in values:
            self.win._add_target_row(v)
        self.win._on_cycle_inputs_changed()

    def test_add_button_appends_numbered_row(self):
        self._set_targets([30.0])
        before = len(self.win._target_rows)
        self.win._on_add_target_clicked()
        self.assertEqual(before + 1, len(self.win._target_rows))
        # 번호 라벨이 1., 2., … 로 다시 매겨진다
        self.assertEqual(["1.", "2."],
                         [no.text() for _r, _e, no, _u in self.win._target_rows])

    def test_remove_row_renumbers_the_rest(self):
        self._set_targets([1.0, 2.0, 3.0])
        second_row = self.win._target_rows[1][0]
        self.win._remove_target_row(second_row)
        self.assertEqual([1.0, 3.0], self.win._collect_targets())
        self.assertEqual(["1.", "2."],
                         [no.text() for _r, _e, no, _u in self.win._target_rows])

    def test_blank_row_is_skipped_not_an_error(self):
        self._set_targets([1.0])
        self.win._add_target_row()          # 빈 칸
        self.assertEqual([1.0], self.win._collect_targets())

    def test_non_numeric_row_is_an_error(self):
        self._set_targets([1.0])
        self.win._target_rows[0][1].setText("1e")   # 입력 중인 미완성 값
        self.assertIsNone(self.win._collect_targets())

    def test_current_cfg_reads_widgets(self):
        self._set_targets([30.0, -30.0, 0.0])
        self.win._le_initial.setText("5")
        self.win._le_rate.setText("2.5")
        self.win._le_tpp.setText("0.5")
        self.win._sb_cycles.setValue(4)
        self.win._cb_return_to_zero.setChecked(True)
        cfg = self.win._current_cfg()
        self.assertEqual([30.0, -30.0, 0.0], cfg.targets)
        self.assertAlmostEqual(5.0, cfg.initial_value)
        self.assertAlmostEqual(2.5, cfg.sweep_rate)
        self.assertAlmostEqual(0.5, cfg.time_per_point)
        self.assertEqual(4, cfg.cycles)
        self.assertTrue(cfg.return_to_zero)

    def test_config_round_trip_through_profile(self):
        self._set_targets([1.0, -1.0])
        self.win._le_initial.setText("-2")
        self.win._le_rate.setText("7")
        self.win._le_tpp.setText("3")
        self.win._sb_cycles.setValue(2)
        self.win._cb_return_to_zero.setChecked(False)
        self.win._save_config()

        # 위젯을 흐트러뜨린 뒤 프로파일에서 되읽는다
        self._set_targets([999.0])
        self.win._le_initial.setText("0")
        self.win._le_rate.setText("1")
        self.win._sb_cycles.setValue(1)
        self.win._load_config()

        self.assertEqual([1.0, -1.0], self.win._collect_targets())
        self.assertAlmostEqual(-2.0, float(self.win._le_initial.text()))
        self.assertAlmostEqual(7.0, float(self.win._le_rate.text()))
        self.assertAlmostEqual(3.0, float(self.win._le_tpp.text()))
        self.assertEqual(2, self.win._sb_cycles.value())
        self.assertFalse(self.win._cb_return_to_zero.isChecked())

    def test_load_config_leaves_one_blank_row_when_empty(self):
        from pythonization.config.models import CycleSweepConfig as _Cfg
        self.reg.save_cycle_sweep_config(_Cfg())     # targets 없음
        self.win._load_config()
        self.assertEqual(1, len(self.win._target_rows))
        self.assertEqual([], self.win._collect_targets())

    def test_cycle_file_name_has_cycle_index(self):
        name = self.win._cycle_file_name(7, "sampleA", include_date=False)
        self.assertEqual("sampleA_cycle007.dat", name)

    def test_cycle_file_name_without_stem_has_no_leading_underscore(self):
        self.assertEqual("cycle003.dat",
                         self.win._cycle_file_name(3, "", include_date=False))

    def test_save_path_preview_uses_own_settings(self):
        self.win._le_main_folder.setText(r"D:\data")
        self.win._le_sub_folder.setText("sampleA/cycle")
        self.win._le_file_name.setText("run1")
        self.win._cb_include_date.setChecked(False)
        self.win._update_save_path()
        preview = self.win._lbl_save_path.text().replace("\\", "/")
        self.assertEqual("D:/data/sampleA/cycle/run1_cycleNNN.dat", preview)

    def test_save_path_preview_warns_when_folder_missing(self):
        self.win._le_main_folder.setText("")
        self.win._update_save_path()
        self.assertIn("Main Folder", self.win._lbl_save_path.text())

    def test_save_settings_round_trip_through_profile(self):
        self.win._le_main_folder.setText(r"D:\cyc")
        self.win._le_sub_folder.setText("sub")
        self.win._le_file_name.setText("nameA")
        self.win._cb_include_date.setChecked(False)
        self.win._cb_save_enabled.setChecked(True)
        self.win._save_config()

        self.win._le_main_folder.setText("zzz")
        self.win._le_file_name.setText("zzz")
        self.win._cb_save_enabled.setChecked(False)
        self.win._load_config()

        self.assertEqual(r"D:\cyc", self.win._le_main_folder.text())
        self.assertEqual("sub", self.win._le_sub_folder.text())
        self.assertEqual("nameA", self.win._le_file_name.text())
        self.assertFalse(self.win._cb_include_date.isChecked())
        self.assertTrue(self.win._cb_save_enabled.isChecked())

    def test_copy_from_main_appends_cycle_subfolder(self):
        self.main._le_main_folder.setText(r"C:\main")
        self.main._le_custom_folder.setText("expA")
        self.main._le_custom_word.setText("word")
        self.win._copy_save_settings_from_main()
        self.assertEqual(r"C:\main", self.win._le_main_folder.text())
        self.assertEqual("expA/cycle", self.win._le_sub_folder.text())
        self.assertEqual("word", self.win._le_file_name.text())

    def test_bad_targets_show_inline_error_not_dialog(self):
        # 잘못된 입력은 미리보기 라벨로만 알린다 (모달을 띄우면 타이핑 중에 걸린다)
        self._set_targets([30.0])
        self.win._target_rows[0][1].setText("1e")
        self.assertIn("숫자", self.win._lbl_cycle_preview.text())

    def test_preview_shows_initial_value_first(self):
        self._set_targets([30.0, -30.0])
        self.win._le_initial.setText("5")
        self.win._on_cycle_inputs_changed()
        text = self.win._lbl_cycle_preview.text()
        self.assertIn("초기값", text)
        self.assertLess(text.index("5"), text.index("30"))


if __name__ == "__main__":
    unittest.main()
