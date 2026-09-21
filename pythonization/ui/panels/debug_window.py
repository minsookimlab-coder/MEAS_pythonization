from typing import Callable, Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QTextEdit, QLineEdit, QPushButton, QWidget, QCheckBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

_MAX_LINES = 2000   # 각 패널 최대 보관 줄 수 (리소스 제한)


class DebugWindow(QDialog):
    """
    분리된 디버그 창.
    - 상단: VISA Log    — InstrumentSession을 통해 오가는 모든 명령어 실시간 표시
    - 중단: Sweep Log   — 스텝 단계별 컨텍스트 + 오류 표시 (verbose 토글 가능)
    - 하단: Console     — alias/VISA 명령어 입력
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Debug")
        self.resize(760, 700)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._submit_callback: Optional[Callable[[str], None]] = None
        self._mono = QFont("Consolas", 10)
        self._mono.setStyleHint(QFont.StyleHint.Monospace)
        # 줄 수 제한은 QTextDocument.maximumBlockCount 가 처리한다 (_append 참고)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter)
        splitter.addWidget(self._build_visa_panel())
        splitter.addWidget(self._build_sweep_panel())
        splitter.addWidget(self._build_console_panel())
        splitter.setSizes([220, 220, 220])

    def set_submit_callback(self, cb: Callable[[str], None]):
        self._submit_callback = cb

    # ------------------------------------------------------------------
    # Panel builders
    # ------------------------------------------------------------------
    def set_visa_log_callback(self, cb):
        """Show 토글 변경 시 호출할 콜백 등록"""
        self._on_visa_show_changed = cb
        # 초기 상태도 적용
        if cb:
            cb(self._cb_visa_show.isChecked())
        # 토글 변경 신호 연결
        self._cb_visa_show.stateChanged.connect(
            lambda: cb(self._cb_visa_show.isChecked()) if cb else None
        )


    def _build_visa_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(4)

        hdr = QHBoxLayout()
        lbl = QLabel("VISA Log")
        lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
        hdr.addWidget(lbl)

        self._cb_visa_show = QCheckBox("Show")
        self._cb_visa_show.setFont(self._mono)
        self._cb_visa_show.setStyleSheet("font-size: 10px; color: #888;")
        hdr.addWidget(self._cb_visa_show)
        self._on_visa_show_changed = None  # setter 추가 위한 placeholder
        hdr.addStretch()

        btn_clear = QPushButton("Clear")
        btn_clear.setFixedHeight(20)
        btn_clear.setFixedWidth(48)
        hdr.addWidget(btn_clear)
        layout.addLayout(hdr)

        self._visa_output = QTextEdit()
        self._visa_output.setReadOnly(True)
        self._visa_output.setFont(self._mono)
        self._visa_output.setStyleSheet(
            "background-color: #0d1117; color: #c9d1d9;"
            "border: 1px solid #30363d; border-radius: 4px;"
        )
        self._visa_output.document().setMaximumBlockCount(_MAX_LINES)
        btn_clear.clicked.connect(lambda: self._clear(self._visa_output))
        layout.addWidget(self._visa_output)
        return panel

    def _build_sweep_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(4)

        hdr = QHBoxLayout()
        lbl = QLabel("Sweep Log")
        lbl.setStyleSheet("font-weight: bold; font-size: 12px; color: #f78166;")
        hdr.addWidget(lbl)

        self._cb_verbose = QCheckBox("Verbose (매 스텝)")
        self._cb_verbose.setFont(self._mono)
        self._cb_verbose.setStyleSheet("font-size: 10px; color: #888;")
        hdr.addWidget(self._cb_verbose)
        hdr.addStretch()

        btn_clear = QPushButton("Clear")
        btn_clear.setFixedHeight(20)
        btn_clear.setFixedWidth(48)
        hdr.addWidget(btn_clear)
        layout.addLayout(hdr)

        self._sweep_output = QTextEdit()
        self._sweep_output.setReadOnly(True)
        self._sweep_output.setFont(self._mono)
        self._sweep_output.setStyleSheet(
            "background-color: #0d1117; color: #c9d1d9;"
            "border: 1px solid #30363d; border-radius: 4px;"
        )
        self._sweep_output.document().setMaximumBlockCount(_MAX_LINES)
        btn_clear.clicked.connect(lambda: self._clear(self._sweep_output))
        layout.addWidget(self._sweep_output)
        return panel

    def _build_console_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(4)

        hdr = QHBoxLayout()
        lbl = QLabel("Console")
        lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
        hdr.addWidget(lbl)
        hdr.addStretch()
        btn_clear = QPushButton("Clear")
        btn_clear.setFixedHeight(20)
        btn_clear.setFixedWidth(48)
        hdr.addWidget(btn_clear)
        layout.addLayout(hdr)

        self._console_output = QTextEdit()
        self._console_output.setReadOnly(True)
        self._console_output.setFont(self._mono)
        self._console_output.setStyleSheet(
            "background-color: #1e1e1e; color: #d4d4d4;"
            "border: 1px solid #444; border-radius: 4px;"
        )
        self._console_output.document().setMaximumBlockCount(_MAX_LINES)
        btn_clear.clicked.connect(lambda: self._clear(self._console_output))
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

    def log_visa(self, alias: str, address: str, cmd_type: str, cmd: str,
                 result: Optional[str] = None):
        """VISA 명령어를 로그에 추가합니다. Show 토글이 꺼져 있으면 무시합니다."""
        if not self._cb_visa_show.isChecked():
            return
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
        elif cmd_type == "write_err":
            msg = f"{hdr} ← {cmd}\n{hdr} ✗ {result}"
            color = "#f44747"
        elif cmd_type == "query_err":
            msg = f"{hdr} ← {cmd}\n{hdr} ✗ {result}"
            color = "#f44747"
        elif cmd_type == "read_err":
            msg = f"{hdr} ✗ {result}"
            color = "#f44747"
        else:
            msg = f"{hdr} {cmd_type}: {cmd}"
            color = "#e3b341"
        self._append(self._visa_output, msg, color)

    def log_sweep(self, text: str, color: str = "#c9d1d9", verbose: bool = False):
        """
        Sweep Log에 메시지 추가.
        verbose=True 인 항목은 Verbose 체크박스가 켜져 있을 때만 표시.
        오류/경고는 verbose=False로 항상 표시.
        """
        if verbose and not self._cb_verbose.isChecked():
            return
        self._append(self._sweep_output, text, color)

    def log_console(self, text: str, color: str = "#d4d4d4"):
        self._append(self._console_output, text, color)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _append(self, widget: QTextEdit, text: str, color: str):
        """로그 한 줄 추가.

        측정 중 매 스텝 불리는 경로다. 예전엔 moveCursor + insertHtml 에 직접
        만든 트림 루프까지 돌려서, 버퍼가 상한(_MAX_LINES)에 닿으면 한 줄당
        평균 3.2 ms(최대 15 ms)가 들었다. QTextDocument 의 maximumBlockCount 가
        낡은 줄을 알아서 버리므로 append() 한 번이면 되고, 실측 0.025 ms 다.
        """
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        widget.append(f'<span style="color:{color}; white-space:pre;">{safe}</span>')

    def _clear(self, widget: QTextEdit):
        widget.clear()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
