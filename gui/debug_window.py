from typing import Callable, Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QTextEdit, QLineEdit, QPushButton, QWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor


class DebugWindow(QDialog):
    """
    분리된 디버그 창.
    - 상단: VISA Log  — InstrumentSession을 통해 오가는 모든 명령어 실시간 표시
    - 하단: Console   — alias/VISA 명령어 입력
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Debug")
        self.resize(720, 580)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._submit_callback: Optional[Callable[[str], None]] = None

        self._mono = QFont("Consolas", 10)
        self._mono.setStyleHint(QFont.StyleHint.Monospace)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter)
        splitter.addWidget(self._build_visa_panel())
        splitter.addWidget(self._build_console_panel())
        splitter.setSizes([280, 280])

    def set_submit_callback(self, cb: Callable[[str], None]):
        self._submit_callback = cb

    # ------------------------------------------------------------------
    # Panel builders
    # ------------------------------------------------------------------

    def _build_visa_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(4)

        lbl = QLabel("VISA Log")
        lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(lbl)

        self._visa_output = QTextEdit()
        self._visa_output.setReadOnly(True)
        self._visa_output.setFont(self._mono)
        self._visa_output.setStyleSheet(
            "background-color: #0d1117; color: #c9d1d9;"
            "border: 1px solid #30363d; border-radius: 4px;"
        )
        layout.addWidget(self._visa_output)
        return panel

    def _build_console_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(4)

        lbl = QLabel("Console")
        lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(lbl)

        self._console_output = QTextEdit()
        self._console_output.setReadOnly(True)
        self._console_output.setFont(self._mono)
        self._console_output.setStyleSheet(
            "background-color: #1e1e1e; color: #d4d4d4;"
            "border: 1px solid #444; border-radius: 4px;"
        )
        layout.addWidget(self._console_output)

        input_row = QHBoxLayout()
        prompt = QLabel(">")
        prompt.setFont(self._mono)
        prompt.setStyleSheet("color: #569cd6; padding-right: 4px;")

        self._console_input = QLineEdit()
        self._console_input.setFont(self._mono)
        self._console_input.setPlaceholderText("alias 입력 (예: M81)  |  'help' 로 명령어 확인")
        self._console_input.setStyleSheet(
            "background-color: #252526; color: #d4d4d4;"
            "border: 1px solid #444; border-radius: 4px; padding: 4px 6px;"
        )
        self._console_input.returnPressed.connect(self._on_submit)

        btn = QPushButton("Send")
        btn.setFixedWidth(60)
        btn.clicked.connect(self._on_submit)

        input_row.addWidget(prompt)
        input_row.addWidget(self._console_input)
        input_row.addWidget(btn)
        layout.addLayout(input_row)
        return panel

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_submit(self):
        text = self._console_input.text().strip()
        if not text:
            return
        self._console_input.clear()
        self.log_console(f"> {text}", color="#569cd6")
        if self._submit_callback:
            self._submit_callback(text)

    # ------------------------------------------------------------------
    # Public logging API
    # ------------------------------------------------------------------

    def log_visa(self, alias: str, address: str, cmd_type: str, cmd: str, result: Optional[str] = None):
        """VISA 명령어를 로그에 추가합니다."""
        hdr = f"[{alias} @ {address}]"
        if cmd_type == "write":
            msg = f"{hdr} ← {cmd}"
            color = "#79c0ff"
        elif cmd_type == "query":
            msg = f"{hdr} ← {cmd}\n{hdr} → {result}"
            color = "#56d364"
        elif cmd_type == "read":
            msg = f"{hdr} → {result}"
            color = "#56d364"
        else:
            msg = f"{hdr} {cmd_type}: {cmd}"
            color = "#e3b341"
        self._append(self._visa_output, msg, color)

    def log_console(self, text: str, color: str = "#d4d4d4"):
        self._append(self._console_output, text, color)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _append(self, widget: QTextEdit, text: str, color: str):
        widget.moveCursor(QTextCursor.MoveOperation.End)
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        widget.insertHtml(f'<span style="color:{color}; white-space:pre;">{safe}</span><br>')
        widget.moveCursor(QTextCursor.MoveOperation.End)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
