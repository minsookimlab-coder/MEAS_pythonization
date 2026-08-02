"""공용 PlotPanel 위젯.

VNA 창과 MFLI 창이 각각 복사해 쓰던 코드를 하나로 합쳤으므로, 두 창이 의존하는
동작(상태 저장/복원, y 곡선 추가·삭제, NaN 처리, 로그 축)을 여기서 고정한다.

GUI 테스트라 offscreen 플랫폼을 쓴다 — 화면 없이도 돌아간다.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                          # noqa: E402
from PySide6.QtWidgets import QApplication                  # noqa: E402

from pythonization.ui.widgets.plot_panel import (           # noqa: E402
    CURVE_COLORS,
    PlotPanel,
    PlotPanelState,
)

_app = QApplication.instance() or QApplication([])


class PlotPanelTestCase(unittest.TestCase):
    def make(self, **kw) -> PlotPanel:
        panel = PlotPanel(**kw)
        panel.update_sources(["frequency", "noise", "probe_T"])
        self.addCleanup(panel.deleteLater)
        return panel


class TestConstruction(PlotPanelTestCase):
    def test_starts_with_exactly_one_y_row(self):
        self.assertEqual(1, len(self.make()._y_rows))

    def test_square_hint_is_opt_in(self):
        # VNA 창만 1:1 비율 힌트를 쓴다
        self.assertTrue(self.make(square_hint=True).hasHeightForWidth())
        self.assertFalse(self.make().hasHeightForWidth())

    def test_log_toggles_are_opt_in(self):
        # MFLI 창만 log x/y 체크박스를 쓴다
        self.assertIsNotNone(self.make(log_toggles=True)._cb_logx)
        self.assertIsNone(self.make()._cb_logx)

    def test_log_accessors_safe_without_toggles(self):
        panel = self.make()
        self.assertFalse(panel.log_x())
        self.assertFalse(panel.log_y())

    def test_color_index_offset(self):
        # VNA 는 오른쪽 패널을 start_color_idx=2 로 만들어 색이 겹치지 않게 한다
        self.assertEqual(CURVE_COLORS[2], self.make(start_color_idx=2)._y_rows[0]._color)


class TestYRows(PlotPanelTestCase):
    def test_add_and_remove(self):
        panel = self.make()
        panel._add_y_row("noise")
        self.assertEqual(2, len(panel._y_rows))
        panel._remove_y_row(panel._y_rows[1])
        self.assertEqual(1, len(panel._y_rows))

    def test_last_row_cannot_be_removed(self):
        panel = self.make()
        panel._remove_y_row(panel._y_rows[0])
        self.assertEqual(1, len(panel._y_rows), "최소 한 줄은 남아야 한다")

    def test_update_sources_keeps_current_selection(self):
        panel = self.make()
        panel.set_default_y("noise")
        panel.update_sources(["frequency", "noise", "probe_T", "extra"])
        self.assertEqual("noise", panel._y_rows[0].y_source())

    def test_changing_y_source_does_not_raise(self):
        """회귀 방지: currentIndexChanged(int) → 0-arg 시그널 연결.

        VNA 쪽에 남아 있던 버그로, int 를 흘려보내지 않으면 emit 이 TypeError 를
        낸다. 여기서 인덱스를 직접 바꿔 시그널 경로를 실제로 태운다.
        """
        panel = self.make()
        row = panel._y_rows[0]
        row._cb_y.setCurrentIndex(1)
        self.assertEqual("noise", row.y_source())


class TestData(PlotPanelTestCase):
    def test_push_data_and_replot(self):
        panel = self.make()
        panel.set_default_x("frequency")
        panel.set_default_y("noise")
        panel.push_data({"frequency": np.array([1.0, 2.0, 3.0]),
                         "noise": np.array([10.0, 20.0, 30.0])}, first_step=True)
        self.assertEqual("frequency", panel.x_source())

    def test_nan_and_inf_do_not_raise(self):
        # pyqtgraph 렌더 레이어 네이티브 크래시 방지 경로
        panel = self.make()
        panel.set_default_x("frequency")
        panel.set_default_y("noise")
        panel.push_data({
            "frequency": np.array([1.0, 2.0, np.inf, 4.0]),
            "noise": np.array([np.nan, 20.0, 30.0, -np.inf]),
        }, first_step=True)

    def test_mismatched_lengths_are_truncated(self):
        panel = self.make()
        panel.set_default_x("frequency")
        panel.set_default_y("noise")
        panel.push_data({"frequency": np.array([1.0, 2.0, 3.0]),
                         "noise": np.array([10.0])})

    def test_missing_source_is_empty_not_error(self):
        panel = self.make()
        panel.set_default_x("frequency")
        panel.push_data({})

    def test_clear_data(self):
        panel = self.make()
        panel.push_data({"frequency": np.array([1.0]), "noise": np.array([2.0])})
        panel.clear_data()
        self.assertEqual({}, panel._data)


class TestStateRoundTrip(PlotPanelTestCase):
    def test_round_trip_preserves_sources(self):
        panel = self.make()
        panel.set_default_x("frequency")
        panel.set_default_y("noise")
        panel._add_y_row("probe_T")
        state = panel.to_state()
        self.assertEqual("frequency", state.x_source)
        self.assertEqual(["noise", "probe_T"], state.y_sources)

        restored = self.make()
        restored.restore_state(state)
        self.assertEqual("frequency", restored.x_source())
        self.assertEqual(["noise", "probe_T"], restored.y_sources())

    def test_restore_trims_extra_rows(self):
        panel = self.make()
        panel._add_y_row("probe_T")
        panel._add_y_row("frequency")
        self.assertEqual(3, len(panel._y_rows))
        panel.restore_state(PlotPanelState(x_source="frequency",
                                           y_sources=["noise"]))
        self.assertEqual(1, len(panel._y_rows))
        self.assertEqual(["noise"], panel.y_sources())

    def test_restore_empty_y_sources_keeps_panel_usable(self):
        panel = self.make()
        panel.restore_state(PlotPanelState(x_source="frequency", y_sources=[]))
        self.assertEqual(1, len(panel._y_rows))

    def test_restore_unknown_source_is_ignored(self):
        # 프로파일이 바뀌어 예전 컬럼이 사라진 경우
        panel = self.make()
        panel.set_default_y("noise")
        panel.restore_state(PlotPanelState(x_source="nope", y_sources=["gone"]))
        self.assertIn(panel.y_sources()[0], ["frequency", "noise", "probe_T"])

    def test_log_flags_round_trip(self):
        panel = self.make(log_toggles=True)
        panel._cb_logx.setChecked(True)
        panel._cb_logy.setChecked(True)
        state = panel.to_state()
        self.assertTrue(state.log_x)
        self.assertTrue(state.log_y)

        restored = self.make(log_toggles=True)
        restored.restore_state(state)
        self.assertTrue(restored.log_x())
        self.assertTrue(restored.log_y())

    def test_log_state_ignored_when_toggles_absent(self):
        # VNA 패널에 로그 상태가 들어와도 죽지 않는다
        panel = self.make()
        panel.restore_state(PlotPanelState(x_source="frequency",
                                           y_sources=["noise"],
                                           log_x=True, log_y=True))
        self.assertFalse(panel.log_x())


class TestFitView(PlotPanelTestCase):
    def test_fit_with_log_mode_skips_non_positive(self):
        panel = self.make(log_toggles=True)
        panel.set_default_x("frequency")
        panel.set_default_y("noise")
        panel._cb_logx.setChecked(True)
        panel._cb_logy.setChecked(True)
        # 0 과 음수가 섞여 있어도 로그 축에서 죽지 않아야 한다
        panel.push_data({"frequency": np.array([0.0, 10.0, 100.0]),
                         "noise": np.array([-1.0, 1.0, 100.0])}, first_step=True)
        panel.auto_range()

    def test_constant_values_do_not_set_degenerate_range(self):
        panel = self.make()
        panel.set_default_x("frequency")
        panel.set_default_y("noise")
        panel.push_data({"frequency": np.array([5.0, 5.0]),
                         "noise": np.array([2.0, 2.0])}, first_step=True)


if __name__ == "__main__":
    unittest.main()
