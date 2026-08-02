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
    reg = ProfileRegistry()
    dlg = ProfileLaunchDialog(reg)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        sys.exit(0)
    window = MainWindow(profile_registry=reg)
    window.show()
    sys.exit(app.exec())
