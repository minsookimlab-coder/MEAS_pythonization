from typing import Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLabel, QFrame
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont


class SweepArrayWindow(QDialog):
    """
    Sweep 진행 상태를 실시간으로 보여주는 창.
    배열 전체 대신 현재값/다음 목표/스텝 카운터만 표시합니다.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sweep Status")
        self.resize(280, 200)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        mono = QFont("Consolas", 11)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel("Sweep Status")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #30363d;")
        layout.addWidget(sep)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)

        def _val_label() -> QLabel:
            lbl = QLabel("—")
            lbl.setFont(mono)
            lbl.setStyleSheet("color: #c9d1d9;")
            return lbl

        self._lbl_step   = _val_label()
        self._lbl_current = _val_label()
        self._lbl_next   = _val_label()
        self._lbl_status = _val_label()

        form.addRow("Step #:", self._lbl_step)
        form.addRow("Current:", self._lbl_current)
        form.addRow("Next Target:", self._lbl_next)
        form.addRow("Status:", self._lbl_status)

        layout.addLayout(form)
        layout.addStretch()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_step(self, step: int, current: float, next_target: Optional[float]):
        """현재 스텝 정보를 업데이트합니다."""
        self._lbl_step.setText(str(step))
        self._lbl_current.setText(f"{current:.6g}")
        if next_target is not None:
            self._lbl_next.setText(f"{next_target:.6g}")
            self._lbl_next.setStyleSheet("color: #56d364;")
            self._lbl_status.setText("Running")
            self._lbl_status.setStyleSheet("color: #56d364;")
        else:
            self._lbl_next.setText("—  (done)")
            self._lbl_next.setStyleSheet("color: #888888;")
            self._lbl_status.setText("Done")
            self._lbl_status.setStyleSheet("color: #e3b341;")

    def reset(self):
        """sweep 시작 전 초기화."""
        self._lbl_step.setText("—")
        self._lbl_current.setText("—")
        self._lbl_next.setText("—")
        self._lbl_next.setStyleSheet("color: #c9d1d9;")
        self._lbl_status.setText("—")
        self._lbl_status.setStyleSheet("color: #c9d1d9;")

    # ------------------------------------------------------------------

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
