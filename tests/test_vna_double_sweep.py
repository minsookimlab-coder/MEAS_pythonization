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


class TestSecondChannelModel(unittest.TestCase):
    def test_is_active_needs_both_enabled_and_cmd(self):
        self.assertFalse(_SecondChannel(True, None, [], None).is_active)
        self.assertFalse(_SecondChannel(False, object(), [], None).is_active)
        self.assertTrue(_SecondChannel(True, object(), [1.0], None).is_active)


if __name__ == "__main__":
    unittest.main()
