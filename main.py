import sys
from PySide6.QtWidgets import QApplication, QDialog
from core.app_dirs import SETTINGS_DIR
from core.applog import setup_logging
from core.profile_registry import ProfileRegistry
from gui.profile_launch_dialog import ProfileLaunchDialog
from gui.main_window import MainWindow

if __name__ == "__main__":
    setup_logging(SETTINGS_DIR / "logs")   # 크래시/예외 캡처 (가벼움)
    app = QApplication(sys.argv)
    # 창을 끌거나 크기 조절하는 동안 Windows 모달 루프가 Qt 이벤트 전달을
    # 막아 측정이 멈추는 것을 방지 (Windows 전용, 실패해도 무시)
    from gui.win_modal_pump import install as _install_size_move_pump
    _install_size_move_pump(app)
    reg = ProfileRegistry()
    dlg = ProfileLaunchDialog(reg)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        sys.exit(0)
    window = MainWindow(profile_registry=reg)
    window.show()
    sys.exit(app.exec())
