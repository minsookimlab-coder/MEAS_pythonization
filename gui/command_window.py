"""
CommandWindow: VISA 라이브러리에서 명령어를 골라 파라미터를 채우고 즉시 실행.

구성:
  Instrument  — 등록된 alias 선택
  VISA        — 선택 기기의 measurements / sweep_values / write_cmds 목록
  Parameters  — 명령어 템플릿의 {placeholder} 입력칸 자동 생성
  Output      — query / read 결과 표시 (sweep_value는 paired_read_cmd 자동 입력)
"""
import re
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QFrame,
    QLabel, QLineEdit, QPushButton, QComboBox, QTextEdit, QWidget,
)

if TYPE_CHECKING:
    from core.instrument_session import InstrumentSession
    from core.visa_library_registry import VisaLibraryRegistry

_MONO = QFont("Consolas", 10)
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


# ---------------------------------------------------------------------------
# Worker: VISA 명령어 실행 (worker thread)
# ---------------------------------------------------------------------------

class _CmdWorker(QThread):
    result_ready = Signal(str)   # 결과 문자열 (오류 포함)

    def __init__(self, session, alias: str, write_cmd: str,
                 read_cmd: Optional[str] = None, read_only: bool = False):
        super().__init__()
        self._session   = session
        self._alias     = alias
        self._write_cmd = write_cmd
        self._read_cmd  = read_cmd
        self._read_only = read_only

    def run(self):
        try:
            if not self._session.is_open(self._alias):
                self._session.open(self._alias)
            if self._read_only:
                # Read only: 아무것도 쓰지 않고 버퍼에서 읽기만 합니다.
                out = self._session.read(self._alias)
                self.result_ready.emit(str(out).strip())
            elif self._read_cmd:
                if self._read_cmd == self._write_cmd:
                    # Pure query (measurement): write_cmd itself returns a response
                    out = self._session.query(self._alias, self._write_cmd)
                else:
                    # Write then separate read (sweep_value: set → readback)
                    self._session.write(self._alias, self._write_cmd)
                    out = self._session.query(self._alias, self._read_cmd)
                self.result_ready.emit(str(out).strip())
            else:
                self._session.write(self._alias, self._write_cmd)
                self.result_ready.emit("(write 완료)")
        except Exception as e:
            self.result_ready.emit(f"[ERROR] {e}")


# ---------------------------------------------------------------------------
# CommandWindow
# ---------------------------------------------------------------------------

class CommandWindow(QDialog):

    def __init__(self, session: "InstrumentSession",
                 visa_lib_reg: "VisaLibraryRegistry", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Command Window")
        self.resize(560, 540)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._session      = session
        self._lib_reg      = visa_lib_reg
        self._worker: Optional[_CmdWorker] = None

        # (cmd_template, read_template_or_None, is_query_type)
        self._current_entry: tuple = ("", None, False)
        self._param_widgets: dict  = {}   # placeholder → QLineEdit

        self._build_ui()
        self._refresh_instruments()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        # --- Instrument / VISA selectors ---
        sel_frame = QFrame()
        sel_frame.setFrameShape(QFrame.Shape.StyledPanel)
        sel_lay = QFormLayout(sel_frame)
        sel_lay.setHorizontalSpacing(10)
        sel_lay.setVerticalSpacing(4)

        self._combo_inst = QComboBox()
        self._combo_inst.setFont(_MONO)
        self._combo_inst.currentIndexChanged.connect(self._on_inst_changed)
        sel_lay.addRow("Instrument:", self._combo_inst)

        self._combo_visa = QComboBox()
        self._combo_visa.setFont(_MONO)
        self._combo_visa.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._combo_visa.currentIndexChanged.connect(self._on_visa_changed)
        sel_lay.addRow("VISA:", self._combo_visa)

        lay.addWidget(sel_frame)

        # --- Command preview ---
        self._lbl_cmd_preview = QLabel("")
        self._lbl_cmd_preview.setFont(_MONO)
        self._lbl_cmd_preview.setStyleSheet("color: #79c0ff; font-size: 10px;")
        self._lbl_cmd_preview.setWordWrap(True)
        lay.addWidget(self._lbl_cmd_preview)

        # --- Parameters (dynamic) ---
        self._param_frame = QFrame()
        self._param_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self._param_outer = QVBoxLayout(self._param_frame)
        self._param_outer.setContentsMargins(8, 6, 8, 6)
        self._param_outer.setSpacing(4)
        self._param_title = QLabel("Parameters")
        self._param_title.setStyleSheet("font-weight: bold;")
        self._param_outer.addWidget(self._param_title)
        self._param_form_widget = QWidget()
        self._param_form = QFormLayout(self._param_form_widget)
        self._param_form.setHorizontalSpacing(10)
        self._param_form.setVerticalSpacing(4)
        self._param_outer.addWidget(self._param_form_widget)
        lay.addWidget(self._param_frame)

        # --- Read command (auto-fill for sweep_value) ---
        read_row = QHBoxLayout()
        read_row.addWidget(QLabel("Read cmd:"))
        self._le_read_cmd = QLineEdit()
        self._le_read_cmd.setFont(_MONO)
        self._le_read_cmd.setPlaceholderText("(자동 입력 또는 직접 입력)")
        read_row.addWidget(self._le_read_cmd, stretch=1)
        lay.addLayout(read_row)

        # --- Send button ---
        btn_row = QHBoxLayout()
        self._btn_send = QPushButton("Send")
        self._btn_send.setMinimumHeight(32)
        self._btn_send.setStyleSheet(
            "QPushButton { font-weight: bold; background-color: #1f4e8c; color: white; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #222; color: #555; }"
        )
        self._btn_send.clicked.connect(self._on_send)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_send)
        lay.addLayout(btn_row)

        # --- Manual VISA Command ---
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("color: #30363d;")
        lay.addWidget(divider)

        manual_title = QLabel("Manual VISA Command")
        manual_title.setStyleSheet("font-weight: bold;")
        lay.addWidget(manual_title)

        manual_row = QHBoxLayout()
        self._le_manual_cmd = QLineEdit()
        self._le_manual_cmd.setFont(_MONO)
        self._le_manual_cmd.setPlaceholderText("임의 VISA 명령어 입력  (예: OUTP? 1)")
        self._le_manual_cmd.returnPressed.connect(self._on_send_manual)
        manual_row.addWidget(self._le_manual_cmd, stretch=1)

        self._combo_manual_type = QComboBox()
        self._combo_manual_type.addItem("Query", "query")
        self._combo_manual_type.addItem("Write", "write")
        self._combo_manual_type.addItem("Read",  "read")
        self._combo_manual_type.setFont(_MONO)
        manual_row.addWidget(self._combo_manual_type)

        self._btn_send_manual = QPushButton("Send")
        self._btn_send_manual.setMinimumHeight(32)
        self._btn_send_manual.setStyleSheet(
            "QPushButton { font-weight: bold; background-color: #2d5a1b; color: white; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #222; color: #555; }"
        )
        self._btn_send_manual.clicked.connect(self._on_send_manual)
        manual_row.addWidget(self._btn_send_manual)
        lay.addLayout(manual_row)

        # --- Output ---
        out_lbl = QLabel("Output")
        out_lbl.setStyleSheet("font-weight: bold;")
        lay.addWidget(out_lbl)
        self._te_output = QTextEdit()
        self._te_output.setReadOnly(True)
        self._te_output.setFont(_MONO)
        self._te_output.setMaximumHeight(140)
        self._te_output.setStyleSheet(
            "background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; border-radius: 4px;"
        )
        lay.addWidget(self._te_output)

    # ------------------------------------------------------------------
    # Instrument / VISA population
    # ------------------------------------------------------------------

    def _refresh_instruments(self):
        self._combo_inst.blockSignals(True)
        self._combo_inst.clear()
        for alias in self._lib_reg.list_aliases():
            self._combo_inst.addItem(alias)
        self._combo_inst.blockSignals(False)
        self._on_inst_changed(self._combo_inst.currentIndex())

    def _on_inst_changed(self, _idx: int):
        alias = self._combo_inst.currentText()
        lib   = self._lib_reg.get_library(alias)

        self._combo_visa.blockSignals(True)
        self._combo_visa.clear()

        for m in lib.measurements:
            label = f"[Q] {m.description}"
            self._combo_visa.addItem(label, ("measurement", m))
        for sv in lib.sweep_values:
            label = f"[S] {sv.description}"
            self._combo_visa.addItem(label, ("sweep_value", sv))
        for wc in lib.write_cmds:
            label = f"[W] {wc.description}"
            self._combo_visa.addItem(label, ("write_cmd", wc))

        self._combo_visa.blockSignals(False)
        self._on_visa_changed(self._combo_visa.currentIndex())

    def _on_visa_changed(self, idx: int):
        if idx < 0:
            self._current_entry = ("", None, False)
            self._rebuild_params()
            return
        data = self._combo_visa.itemData(idx)
        if data is None:
            return
        kind, entry = data

        if kind == "measurement":
            cmd_tmpl  = entry.cmd_query
            read_tmpl = None
            is_query  = True
        elif kind == "sweep_value":
            cmd_tmpl  = entry.cmd_set
            read_tmpl = entry.paired_read_cmd or None
            is_query  = bool(read_tmpl)
        else:   # write_cmd
            cmd_tmpl  = entry.cmd_set
            read_tmpl = None
            is_query  = False

        self._current_entry = (cmd_tmpl, read_tmpl, is_query)
        self._le_read_cmd.setText(read_tmpl or "")
        self._rebuild_params()

    # ------------------------------------------------------------------
    # Parameter widgets
    # ------------------------------------------------------------------

    def _rebuild_params(self):
        # Clear old widgets
        while self._param_form.rowCount():
            self._param_form.removeRow(0)
        self._param_widgets.clear()

        cmd_tmpl = self._current_entry[0]
        placeholders = _PLACEHOLDER_RE.findall(cmd_tmpl)
        seen = set()
        for ph in placeholders:
            if ph in seen:
                continue
            seen.add(ph)
            le = QLineEdit()
            le.setFont(_MONO)
            le.setPlaceholderText(f"{{{ph}}}")
            le.textChanged.connect(self._update_preview)
            self._param_widgets[ph] = le
            self._param_form.addRow(f"{{{ph}}}:", le)

        self._param_frame.setVisible(bool(self._param_widgets))
        self._update_preview()

    def _update_preview(self):
        cmd_tmpl = self._current_entry[0]
        if not cmd_tmpl:
            self._lbl_cmd_preview.setText("")
            return
        resolved = cmd_tmpl
        for ph, le in self._param_widgets.items():
            val = le.text().strip() or f"{{{ph}}}"
            resolved = resolved.replace(f"{{{ph}}}", val)
        self._lbl_cmd_preview.setText(f"→ {resolved}")

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def _on_send(self):
        alias = self._combo_inst.currentText()
        if not alias:
            return
        cmd_tmpl, _, is_query = self._current_entry
        if not cmd_tmpl:
            return

        resolved = cmd_tmpl
        for ph, le in self._param_widgets.items():
            resolved = resolved.replace(f"{{{ph}}}", le.text().strip())

        read_cmd = self._le_read_cmd.text().strip() or None
        # If measurement (query type), use query directly
        if is_query and not read_cmd:
            read_cmd = resolved   # self-contained query command

        self._btn_send.setEnabled(False)
        self._te_output.setPlainText("실행 중...")

        if self._worker and self._worker.isRunning():
            self._worker.quit()

        if is_query and read_cmd == resolved:
            # Pure query: write = read = same cmd
            self._worker = _CmdWorker(self._session, alias, resolved, read_cmd=resolved)
        else:
            self._worker = _CmdWorker(self._session, alias, resolved, read_cmd=read_cmd)

        self._worker.result_ready.connect(self._on_result)
        self._worker.start()

    def _on_send_manual(self):
        alias = self._combo_inst.currentText()
        if not alias:
            return

        mode = self._combo_manual_type.currentData()  # "query" | "write" | "read"
        cmd  = self._le_manual_cmd.text().strip()

        # Read 모드는 커맨드 없이 버퍼만 읽으므로 cmd 비어있어도 허용
        if not cmd and mode != "read":
            return

        self._btn_send.setEnabled(False)
        self._btn_send_manual.setEnabled(False)
        self._te_output.setPlainText("실행 중...")

        if self._worker and self._worker.isRunning():
            self._worker.quit()

        if mode == "query":
            self._worker = _CmdWorker(self._session, alias, cmd, read_cmd=cmd)
        elif mode == "read":
            self._worker = _CmdWorker(self._session, alias, cmd, read_only=True)
        else:  # write
            self._worker = _CmdWorker(self._session, alias, cmd)

        self._worker.result_ready.connect(self._on_result)
        self._worker.start()

    @Slot(str)
    def _on_result(self, text: str):
        self._btn_send.setEnabled(True)
        self._btn_send_manual.setEnabled(True)
        self._te_output.setPlainText(text)

    # ------------------------------------------------------------------

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)
