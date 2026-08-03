"""_on_step_done 에서 분해해 낸 스텝 처리 단계들.

이 경로가 틀리면 .dat 파일에 잘못된 값이 들어간다 — 측정을 다시 돌리기 전까지
발견되지 않는 종류의 실패라, 셀 포맷과 실패 판정을 여기서 고정한다.

세 가지 값 상태를 구분하는 게 핵심이다:
  실제 값  → "%.6g" 로 기록
  nan     → threshold 초과. 'nan' 을 기록하고 sweep 은 계속한다.
  None    → 측정 실패. sweep 을 멈춰야 한다.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                     # noqa: E402

from pythonization.config.models import (                      # noqa: E402
    InstantiatedMeasurement,
    MainUIProfile,
)
from pythonization.measurement.derivative import DerivativeConfig   # noqa: E402
from pythonization.measurement.sweep_worker import StepResult   # noqa: E402
from pythonization.profiles.registry import ProfileRegistry     # noqa: E402
from pythonization.ui.main_window import MainWindow             # noqa: E402

_app = QApplication.instance() or QApplication([])


def _measurement(desc: str) -> InstantiatedMeasurement:
    return InstantiatedMeasurement(
        alias="dev", description=desc, resolved_cmd="READ?",
        figure_axis=desc, unit="V")


class StepPipelineTestCase(unittest.TestCase):
    """측정 채널 2개(R, T)를 활성화한 MainWindow 를 만든다."""

    @classmethod
    def setUpClass(cls):
        cls.win = MainWindow(profile_registry=ProfileRegistry())

    @classmethod
    def tearDownClass(cls):
        cls.win.deleteLater()

    def setUp(self):
        self.win._active_profile = MainUIProfile(
            measurements=[_measurement("R"), _measurement("T")])
        self.win._active_meas_indices = [0, 1]

    def result(self, meas, **kw):
        return StepResult(
            current=kw.pop("current", 1.0),
            next_v=kw.pop("next_v", 2.0),
            is_done=kw.pop("is_done", False),
            meas_results=meas,
            **kw,
        )


class TestFormatMeasurementRow(StepPipelineTestCase):
    def test_normal_values(self):
        res = self.result([(0, 1.5), (1, 300.0)])
        row, failed = self.win._format_measurement_row(res, dict(res.meas_results))
        self.assertEqual(["2", "1.5", "300"], row, "첫 셀은 sweep 값(next_v)")
        self.assertEqual([], failed)

    def test_nan_is_recorded_and_not_a_failure(self):
        # threshold 초과 → 'nan' 기록하고 측정은 계속되어야 한다
        res = self.result([(0, float("nan")), (1, 300.0)])
        row, failed = self.win._format_measurement_row(res, dict(res.meas_results))
        self.assertEqual(["2", "nan", "300"], row)
        self.assertEqual([], failed, "nan 은 실패가 아니다")

    def test_none_is_a_failure(self):
        res = self.result([(0, None), (1, 300.0)])
        row, failed = self.win._format_measurement_row(res, dict(res.meas_results))
        self.assertEqual(["2", "ERR", "300"], row)
        self.assertEqual([0], failed)

    def test_missing_channel_counts_as_failure(self):
        # 워커가 아예 값을 안 준 채널도 실패로 잡아야 한다
        res = self.result([(0, 1.5)])
        row, failed = self.win._format_measurement_row(res, dict(res.meas_results))
        self.assertEqual(["2", "1.5", "ERR"], row)
        self.assertEqual([1], failed)

    def test_significant_digits(self):
        res = self.result([(0, 1.23456789), (1, 1.2e-13)])
        row, _ = self.win._format_measurement_row(res, dict(res.meas_results))
        self.assertEqual(["2", "1.23457", "1.2e-13"], row)

    def test_only_active_channels_are_written(self):
        self.win._active_meas_indices = [1]
        res = self.result([(0, 1.5), (1, 300.0)])
        row, _ = self.win._format_measurement_row(res, dict(res.meas_results))
        self.assertEqual(["2", "300"], row, "체크 해제한 채널은 열이 없어야 한다")


class TestDerivativeRowCells(StepPipelineTestCase):
    def test_disabled_channels_add_no_columns(self):
        for ch, _key in self.win._deriv_channels():
            ch._cfg.enabled = False
        self.assertEqual([], self.win._derivative_row_cells([1.0, 2.0, 3.0]))

    def test_enabled_channel_adds_one_column(self):
        channels = self.win._deriv_channels()
        for ch, _key in channels:
            ch._cfg.enabled = False
        channels[0][0]._cfg.enabled = True
        self.addCleanup(setattr, channels[0][0]._cfg, "enabled", False)
        self.assertEqual(["1.5"], self.win._derivative_row_cells([1.5, None, None]))

    def test_none_becomes_dash(self):
        channels = self.win._deriv_channels()
        for ch, _key in channels:
            ch._cfg.enabled = False
        channels[0][0]._cfg.enabled = True
        self.addCleanup(setattr, channels[0][0]._cfg, "enabled", False)
        self.assertEqual(["—"], self.win._derivative_row_cells([None, None, None]),
                         "값이 아직 없으면 빈칸이 아니라 — 로 표시")

    def test_channel_order_is_stable(self):
        keys = [key for _ch, key in self.win._deriv_channels()]
        self.assertEqual(3, len(keys))
        self.assertEqual(3, len(set(keys)), "세 채널의 컬럼 키가 겹치면 안 된다")


class TestGraphPoint(StepPipelineTestCase):
    def test_failed_and_threshold_values_become_nan(self):
        import math

        for ch, _key in self.win._deriv_channels():
            ch._cfg.enabled = False
        res = self.result([(0, None), (1, float("nan"))])
        before = len(self.win._graph_history)
        self.win._push_graph_point(res, dict(res.meas_results), [None, None, None])

        self.assertEqual(before + 1, len(self.win._graph_history))
        values = self.win._graph_history[-1].values
        self.assertEqual(2.0, values["__sweep__"])
        self.assertTrue(math.isnan(values["R"]), "측정 실패는 그래프에서 끊긴 선")
        self.assertTrue(math.isnan(values["T"]))


class TestRemainingTime(StepPipelineTestCase):
    def test_shows_seconds_under_a_minute(self):
        self.win._sweep_config.sweep_to = 10.0
        self.win._sweep_config.sweep_rate = 60.0      # 1 unit/s
        self.win._sweep_config.time_per_point = 1.0
        self.win._update_remaining_time(self.result([], next_v=5.0))
        self.assertEqual("5.0 s", self.win._lbl_remaining.text())

    def test_shows_minutes_over_a_minute(self):
        self.win._sweep_config.sweep_to = 100.0
        self.win._sweep_config.sweep_rate = 60.0
        self.win._sweep_config.time_per_point = 1.0
        self.win._update_remaining_time(self.result([], next_v=0.0))
        self.assertEqual("1 min 40 s", self.win._lbl_remaining.text())

    def test_done_clears_estimate(self):
        self.win._update_remaining_time(self.result([], is_done=True))
        self.assertEqual("—", self.win._lbl_remaining.text())

    def test_zero_rate_does_not_divide_by_zero(self):
        self.win._sweep_config.sweep_rate = 0.0
        self.win._update_remaining_time(self.result([], next_v=0.0))
        self.assertEqual("—", self.win._lbl_remaining.text())


class TestStepStatus(StepPipelineTestCase):
    def test_zero_rate_is_absorbed(self):
        """calculate_next_step 의 ValueError 가 슬롯 밖으로 나가면 sweep 이 조용히 멈춘다."""
        self.win._sweep_config.sweep_to = 10.0
        self.win._sweep_config.sweep_rate = 0.0
        self.win._sweep_config.time_per_point = 0.0
        self.win._update_step_status(self.result([], next_v=1.0))   # 예외가 나면 실패


if __name__ == "__main__":
    unittest.main()
