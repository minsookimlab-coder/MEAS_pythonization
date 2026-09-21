"""VNA Config 창의 섹션 명령 on/off 소유권과 재로드 규약.

배경: `VnaCommandEntry.enabled` 는 한 개의 필드인데 예전엔 두 창이 각자 고쳤다.
  - VNA Config  : Sections 탭의 [On/Off] 버튼
  - VNA Control : 섹션 명령마다 붙은 체크박스
Config 창은 보관형이라 옛 스냅샷을 들고 있다가 [Save & Apply] 로 Control 이
저장한 값을 통째로 되돌려 놓았다. 여기서 그 두 가지를 고정한다.
  1. 섹션 명령의 on/off 는 Control 만 갖는다 (Config 에는 버튼도 표시도 없음)
  2. Config 는 열릴 때마다 디스크를 다시 읽는다
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton      # noqa: E402

from pythonization.instruments.command_library import VisaLibraryRegistry  # noqa: E402
from pythonization.ui.modules.vna.config_window import VnaConfigWindow     # noqa: E402
from pythonization.ui.modules.vna.models import (                          # noqa: E402
    VnaCommandEntry,
    VnaConfigData,
    VnaSectionConfig,
    load_vna_config,
    save_vna_config,
)

_app = QApplication.instance() or QApplication([])


def _entry(desc: str, enabled: bool = True, cmd_type: str = "write") -> VnaCommandEntry:
    return VnaCommandEntry(alias="VNA", description=desc, cmd_type=cmd_type,
                           enabled=enabled, figure_axis=desc)


def _config(section_enabled: bool = True, read_enabled: bool = True) -> VnaConfigData:
    cfg = VnaConfigData()
    cfg.sections = [VnaSectionConfig(
        name="Preset", commands=[_entry("preset_cmd", enabled=section_enabled)])]
    cfg.acquire.read_cmds = [
        _entry("read_s21", enabled=read_enabled, cmd_type="query")]
    return cfg


def _button_labels(widget) -> list:
    return [b.text() for b in widget.findChildren(QPushButton)]


class VnaConfigTestCase(unittest.TestCase):
    """설정 파일을 임시 폴더에 두어 사용자 실제 설정을 건드리지 않는다."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "vna.yaml"
        # 라이브러리도 없는 임시 경로 → 실제 visa_libraries.yaml 을 읽지 않는다
        self.lib_reg = VisaLibraryRegistry(path=Path(self._tmp.name) / "lib.yaml")

    def tearDown(self):
        self._tmp.cleanup()

    def _open(self, cfg: VnaConfigData) -> VnaConfigWindow:
        save_vna_config(cfg, self.path)
        win = VnaConfigWindow(self.lib_reg, config_path=self.path)
        self.addCleanup(win.deleteLater)
        return win


class TestSectionOnOffRemoved(VnaConfigTestCase):
    def test_sections_list_has_no_onoff_button(self):
        win = self._open(_config())
        self.assertNotIn("On/Off", _button_labels(win._sec_cmd_list),
                         "섹션 on/off 는 VNA Control 체크박스가 단독으로 갖는다")

    def test_acquire_lists_keep_onoff_button(self):
        """acquire 명령은 Control 에 체크박스가 없으므로 여기 토글이 유일하다."""
        win = self._open(_config())
        for name, lst in (("read", win._acq_read), ("start", win._acq_start),
                          ("wait", win._acq_wait)):
            with self.subTest(list=name):
                self.assertIn("On/Off", _button_labels(lst))

    def test_sections_label_hides_off_marker(self):
        win = self._open(_config(section_enabled=False))
        label = win._sec_cmd_list._lst.item(0).text()
        self.assertNotIn("OFF", label,
                         "끌 수 없는 목록에 OFF 표시만 남으면 어디서 켜는지 알 수 없다")

    def test_acquire_label_keeps_off_marker(self):
        win = self._open(_config(read_enabled=False))
        self.assertIn("OFF", win._acq_read._lst.item(0).text())

    def test_save_preserves_disabled_section_command(self):
        """Config 에서 토글을 못 해도 기존 enabled=False 값은 그대로 보존된다."""
        win = self._open(_config(section_enabled=False))
        win._on_save()
        reloaded = load_vna_config(self.path)
        self.assertFalse(reloaded.sections[0].commands[0].enabled)


class TestConfigReloadsFromDisk(VnaConfigTestCase):
    """Config 는 보관형이라, 다시 열 때 디스크를 읽지 않으면 Control 을 덮어쓴다."""

    def _write_control_side_change(self):
        """VNA Control 이 체크박스를 끄고 저장한 상황을 파일에 직접 만든다.
        (VnaWindow._save_ui_state 가 하는 일과 같다.)"""
        cfg = load_vna_config(self.path)
        cfg.sections[0].commands[0] = cfg.sections[0].commands[0].model_copy(
            update={"enabled": False, "sweep_role": "start"})
        cfg.main_folder = "D:/set_by_control"
        save_vna_config(cfg, self.path)

    def test_reopen_picks_up_external_change(self):
        win = self._open(_config(section_enabled=True))
        self._write_control_side_change()
        win.set_config_path(self.path)          # 같은 경로로 '다시 열기'
        self.assertFalse(win._cfg.sections[0].commands[0].enabled)
        self.assertEqual("D:/set_by_control", win._cfg.main_folder)

    def test_save_after_reopen_does_not_revert_control_state(self):
        win = self._open(_config(section_enabled=True))
        self._write_control_side_change()
        win.set_config_path(self.path)          # 같은 경로로 '다시 열기'
        win._on_save()                          # Save & Apply

        reloaded = load_vna_config(self.path)
        self.assertFalse(reloaded.sections[0].commands[0].enabled,
                         "Config 저장이 Control 의 체크박스 상태를 되돌렸다")
        self.assertEqual("start", reloaded.sections[0].commands[0].sweep_role,
                         "Config 저장이 Control 의 sweep role 을 되돌렸다")
        self.assertEqual("D:/set_by_control", reloaded.main_folder,
                         "Config 저장이 Control 의 저장 폴더를 되돌렸다")

    def test_show_event_reloads(self):
        win = self._open(_config(section_enabled=True))
        self._write_control_side_change()
        win.show()
        self.addCleanup(win.hide)
        self.assertFalse(win._cfg.sections[0].commands[0].enabled)

    def test_reload_keeps_selected_section_commands_visible(self):
        """재로드 후 _cur_sec_idx 가 -1 로 남으면 그 섹션 편집분이 저장에서 빠진다."""
        cfg = _config()
        cfg.sections.append(VnaSectionConfig(name="Second",
                                             commands=[_entry("second_cmd")]))
        win = self._open(cfg)
        win._lst_sections.setCurrentRow(1)
        win.set_config_path(self.path)
        self.assertEqual(win._cur_sec_idx, win._lst_sections.currentRow())
        self.assertGreaterEqual(win._cur_sec_idx, 0)
        shown = [win._sec_cmd_list._lst.item(i).text()
                 for i in range(win._sec_cmd_list._lst.count())]
        expected = win._cfg.sections[win._cur_sec_idx].commands[0].description
        self.assertTrue(any(expected in s for s in shown),
                        f"선택된 섹션의 명령이 보이지 않는다: {shown}")


if __name__ == "__main__":
    unittest.main()
