"""앱 기동 순서.

  1. 로깅/크래시 캡처를 먼저 건다 — 이후 단계에서 죽어도 로그가 남도록.
  2. QApplication 생성.
  3. 프로파일 선택 다이얼로그 → 취소하면 조용히 종료.
  4. MainWindow 표시 후 이벤트 루프.

루트의 main.py 와 `python -m pythonization` 이 모두 여기 main() 을 부른다.
"""
import sys

from PySide6.QtWidgets import QApplication, QDialog

from pythonization.app.logging_setup import setup_logging
from pythonization.app.paths import SETTINGS_DIR


def main(argv: list[str] | None = None) -> int:
    """앱을 실행하고 프로세스 종료 코드를 반환한다."""
    # 무거운 GUI 모듈은 로깅을 건 뒤에 import 한다 — import 단계에서 터져도
    # 콘솔 없는 exe 에서 원인이 app.log 에 남는다.
    setup_logging(SETTINGS_DIR / "logs")

    from pythonization.profiles.registry import ProfileRegistry
    from pythonization.ui.dialogs.profile_launch import ProfileLaunchDialog
    from pythonization.ui.main_window import MainWindow

    app = QApplication(argv if argv is not None else sys.argv)

    # 창을 끌거나 크기 조절하는 동안 Windows 모달 루프가 Qt 이벤트 전달을 막아
    # 측정이 멈추는 것을 방지한다 (Windows 전용, 실패해도 조용히 넘어간다).
    from pythonization.app.win_modal_pump import install as install_size_move_pump
    install_size_move_pump(app)

    registry = ProfileRegistry()

    if ProfileLaunchDialog(registry).exec() != QDialog.DialogCode.Accepted:
        return 0   # 사용자가 프로파일 선택을 취소 — 정상 종료

    window = MainWindow(profile_registry=registry)
    window.show()
    return app.exec()
