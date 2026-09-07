"""메뉴에서 열리는 모든 창이 실제로 생성되는지 확인한다.

이 테스트가 있는 이유: 리팩터링 중 함수 내부 import 를 정리하다 MainWindow 의
ResumeLog import 가 사라졌는데, 모듈 import 테스트는 통과했다. 창을 실제로
만들어 보지 않으면 '메뉴를 눌러야 터지는' 회귀를 못 잡는다.

MainWindow 는 사용자 설정(instruments.yaml, 프로파일)을 읽지만 쓰지는 않는다.
계측기 연결은 하지 않으므로 장비가 없어도 돌아간다.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication            # noqa: E402

from pythonization.profiles.registry import ProfileRegistry   # noqa: E402
from pythonization.ui.main_window import MainWindow           # noqa: E402

_app = QApplication.instance() or QApplication([])

#: MainWindow 의 메뉴/버튼이 호출하는 창 열기 메서드
OPENERS = [
    "_open_vna_window",
    "_open_mfli_window",
    "_open_double_sweep",
    "_open_cycle_sweep",
    "_open_cycle_double_sweep",
    "_open_graph_window",
    "_open_command_window",
    "_open_parameter_manager",
    "_open_visa_library",
    "_open_instrument_settings",
    "_open_meta_data_config",
    "_open_config",
]


class TestWindowsOpen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.win = MainWindow(profile_registry=ProfileRegistry())

    @classmethod
    def tearDownClass(cls):
        cls.win.deleteLater()

    def test_main_window_built(self):
        self.assertEqual("Measurement System", self.win.windowTitle())
        self.assertTrue(self.win.menuBar().actions(), "메뉴가 비어 있다")

    def test_every_opener_exists(self):
        for name in OPENERS:
            with self.subTest(opener=name):
                self.assertTrue(hasattr(self.win, name),
                                f"{name} 이 사라졌다 — 메뉴 연결이 끊긴다")

    def test_every_window_constructs(self):
        failures = []
        for name in OPENERS:
            try:
                getattr(self.win, name)()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        self.assertEqual([], failures,
                         "창 생성 실패:\n  " + "\n  ".join(failures))

    def test_opening_twice_reuses_instance(self):
        # 창을 다시 열 때 새로 만들면 워커/설정이 중복된다
        self.win._open_vna_window()
        first = self.win._vna_window
        self.win._open_vna_window()
        self.assertIs(first, self.win._vna_window)


if __name__ == "__main__":
    unittest.main()
