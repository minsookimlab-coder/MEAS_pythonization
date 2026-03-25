from typing import List, Optional, Tuple, Union

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QInputDialog, QFrame
)

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont

from config.config_models import MeasurementParamDef, SweepValueDef, WriteCmdDef, InstrumentCmdLibrary
from core.visa_library_registry import VisaLibraryRegistry
from core.instrument_registry import InstrumentRegistry

_MONO = QFont("Consolas", 10)
_COL_MEAS  = QColor("#56d364")
_COL_SWEEP = QColor("#79c0ff")
_COL_WRITE = QColor("#e3b341")


# ===========================================================================
# Add / Edit dialogs
# ===========================================================================

class MeasurementParamDialog(QDialog):
    """Measurement parameter 추가/편집 다이얼로그.
    Description, cmd_query, Figure Axis, Unit 필드만 포함합니다.
    """

    def __init__(self, parent=None, entry: Optional[MeasurementParamDef] = None):
        super().__init__(parent)
        self.setWindowTitle("Measurement Parameter")
        self.setMinimumWidth(480)

        self._outer = QVBoxLayout(self)
        self._outer.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self._le_desc  = QLineEdit()
        self._le_desc.setPlaceholderText("e.g. meas_smua_{ch}_current")
        self._le_query = QLineEdit()
        self._le_query.setFont(_MONO)
        self._le_query.setPlaceholderText(
            'e.g. print(smua.measure.i())  or  FETCh:SENSe{m}:LIA:X?'
        )
        self._le_axis  = QLineEdit()
        self._le_axis.setPlaceholderText("e.g. smua_{ch}_current  (supports {p})")
        self._le_unit  = QLineEdit()
        self._le_unit.setPlaceholderText("e.g. A")
        form.addRow("Description:", self._le_desc)
        form.addRow("cmd_query:", self._le_query)
        form.addRow("Figure Axis:", self._le_axis)
        form.addRow("Unit:", self._le_unit)
        self._outer.addLayout(form)

        if entry:
            self._le_desc.setText(entry.description)
            self._le_query.setText(entry.cmd_query)
            self._le_axis.setText(entry.figure_axis)
            self._le_unit.setText(entry.unit)

        btn_row = QHBoxLayout()
        btn_ok = QPushButton("OK")
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        self._outer.addLayout(btn_row)

    def _on_ok(self):
        if not self._le_desc.text().strip():
            QMessageBox.warning(self, "Validation", "Description is required.")
            return
        if not self._le_query.text().strip():
            QMessageBox.warning(self, "Validation", "cmd_query is required.")
            return
        self.accept()

    def get_entry(self) -> MeasurementParamDef:
        return MeasurementParamDef(
            description=self._le_desc.text().strip(),
            cmd_query=self._le_query.text().strip(),
            figure_axis=self._le_axis.text().strip(),
            unit=self._le_unit.text().strip(),
        )


class SweepValueDialog(QDialog):
    """Sweep value 추가/편집 다이얼로그.
    paired_read_cmd는 직접 입력하는 텍스트 필드로 처리합니다.
    """

    def __init__(
        self,
        parent=None,
        entry: Optional[SweepValueDef] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Sweep Value")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self._le_w_desc  = QLineEdit()
        self._le_w_desc.setPlaceholderText("e.g. sweep_smua_{ch}_voltage")
        self._le_cmd_set = QLineEdit()
        self._le_cmd_set.setFont(_MONO)
        self._le_cmd_set.setPlaceholderText("e.g. smua.source.levelv = {v}")
        self._le_paired  = QLineEdit()
        self._le_paired.setFont(_MONO)
        self._le_paired.setPlaceholderText("e.g. print(smua.measure.i())")
        self._le_w_axis  = QLineEdit()
        self._le_w_axis.setPlaceholderText("e.g. smua_{ch}_voltage  (supports {p})")
        self._le_w_unit  = QLineEdit()
        form.addRow("Description:", self._le_w_desc)
        form.addRow("cmd_set:", self._le_cmd_set)
        form.addRow("paired_read_cmd:", self._le_paired)
        form.addRow("Figure Axis:", self._le_w_axis)
        form.addRow("Unit:", self._le_w_unit)
        layout.addLayout(form)

        if entry:
            self._le_w_desc.setText(entry.description)
            self._le_cmd_set.setText(entry.cmd_set)
            self._le_paired.setText(entry.paired_read_cmd)
            self._le_w_axis.setText(entry.figure_axis)
            self._le_w_unit.setText(entry.unit)

        btn_row = QHBoxLayout()
        btn_ok = QPushButton("OK")
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def _on_ok(self):
        if not self._le_w_desc.text().strip():
            QMessageBox.warning(self, "Validation", "Description is required.")
            return
        if not self._le_cmd_set.text().strip():
            QMessageBox.warning(self, "Validation", "cmd_set is required.")
            return
        self.accept()

    def get_entry(self) -> SweepValueDef:
        return SweepValueDef(
            description=self._le_w_desc.text().strip(),
            cmd_set=self._le_cmd_set.text().strip(),
            paired_read_cmd=self._le_paired.text().strip(),
            figure_axis=self._le_w_axis.text().strip(),
            unit=self._le_w_unit.text().strip(),
        )


class WriteCmdDialog(QDialog):
    """Write-only 명령어 추가/편집 다이얼로그. paired read 없이 단순 쓰기 명령만 등록합니다."""

    def __init__(self, parent=None, entry: Optional[WriteCmdDef] = None):
        super().__init__(parent)
        self.setWindowTitle("Write Command")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self._le_desc    = QLineEdit()
        self._le_desc.setPlaceholderText("e.g. write_smua_{ch}_output")
        self._le_cmd_set = QLineEdit()
        self._le_cmd_set.setFont(_MONO)
        self._le_cmd_set.setPlaceholderText("예: smua.source.output = smua.OUTPUT_ON")
        self._le_axis    = QLineEdit()
        self._le_unit    = QLineEdit()
        form.addRow("Description:", self._le_desc)
        form.addRow("cmd_set:", self._le_cmd_set)
        form.addRow("Figure Axis:", self._le_axis)
        form.addRow("Unit:", self._le_unit)
        layout.addLayout(form)

        if entry:
            self._le_desc.setText(entry.description)
            self._le_cmd_set.setText(entry.cmd_set)
            self._le_axis.setText(entry.figure_axis)
            self._le_unit.setText(entry.unit)

        btn_row = QHBoxLayout()
        btn_ok = QPushButton("OK")
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def _on_ok(self):
        if not self._le_desc.text().strip():
            QMessageBox.warning(self, "Validation", "Description은 필수입니다.")
            return
        if not self._le_cmd_set.text().strip():
            QMessageBox.warning(self, "Validation", "cmd_set은 필수입니다.")
            return
        self.accept()

    def get_entry(self) -> WriteCmdDef:
        return WriteCmdDef(
            description=self._le_desc.text().strip(),
            cmd_set=self._le_cmd_set.text().strip(),
            figure_axis=self._le_axis.text().strip(),
            unit=self._le_unit.text().strip(),
        )


# ===========================================================================
# Main window
# ===========================================================================

class VisaLibraryWindow(QDialog):
    """
    VISA 명령어 라이브러리 관리 창.
    기기(alias)별로 Measurement Parameter / Sweep Value 명령어를 등록·편집·저장합니다.
    """

    library_saved = Signal()   # 라이브러리 저장 완료 시 emit → 하위 계층 재인스턴스화 트리거

    _HEADERS = ["Description", "VISA Command", "Figure Axis", "Unit", "Type", "Paired Measurement"]

    def __init__(
        self,
        lib_registry: VisaLibraryRegistry,
        inst_registry: InstrumentRegistry,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("VISA Library")
        self.resize(960, 520)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._lib_reg  = lib_registry
        self._inst_reg = inst_registry
        self._current_alias: Optional[str] = None
        # flat list: ('measurement', MeasurementParamDef) | ('sweep value', SweepValueDef) | ('write', WriteCmdDef)
        self._entries: List[Tuple[str, Union[MeasurementParamDef, SweepValueDef, WriteCmdDef]]] = []

        self._build_ui()
        self._populate_combo()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Top row: instrument selector
        top = QHBoxLayout()
        top.addWidget(QLabel("Instrument:"))
        self._combo = QComboBox()
        self._combo.setMinimumWidth(140)
        self._combo.currentTextChanged.connect(self._on_instrument_changed)
        top.addWidget(self._combo)
        top.addSpacing(8)
        btn_add_inst = QPushButton("+ Instrument")
        btn_add_inst.clicked.connect(self._add_instrument)
        top.addWidget(btn_add_inst)
        top.addStretch()
        layout.addLayout(top)

        # Table
        self._table = QTableWidget()
        self._table.setColumnCount(len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setFont(_MONO)
        self._table.setStyleSheet(
            "background-color: #0d1117; color: #c9d1d9; gridline-color: #21262d;"
        )
        self._table.cellDoubleClicked.connect(lambda row, _col: self._edit())
        layout.addWidget(self._table)

        # Bottom buttons
        bottom = QHBoxLayout()
        btn_meas = QPushButton("+ Measurement")
        btn_meas.clicked.connect(self._add_measurement)
        btn_sweep = QPushButton("+ Sweep Value")
        btn_sweep.clicked.connect(self._add_sweep_value)
        btn_write = QPushButton("+ Write")
        btn_write.clicked.connect(self._add_write)
        btn_edit = QPushButton("Edit")
        btn_edit.clicked.connect(self._edit)
        btn_dup = QPushButton("Duplicate")
        btn_dup.clicked.connect(self._duplicate)
        btn_delete = QPushButton("Delete")
        btn_delete.clicked.connect(self._delete)
        bottom.addWidget(btn_meas)
        bottom.addWidget(btn_sweep)
        bottom.addWidget(btn_write)
        bottom.addSpacing(12)
        bottom.addWidget(btn_edit)
        bottom.addWidget(btn_dup)
        bottom.addWidget(btn_delete)
        bottom.addStretch()
        btn_save = QPushButton("Save")
        btn_save.setStyleSheet(
            "background-color: #2e7d32; color: white; font-weight: bold; padding: 4px 16px;"
        )
        btn_save.clicked.connect(self._save)
        bottom.addWidget(btn_save)
        layout.addLayout(bottom)

    # ------------------------------------------------------------------
    # Instrument combo
    # ------------------------------------------------------------------

    def _populate_combo(self):
        lib_aliases  = set(self._lib_reg.list_aliases())
        inst_aliases = set(self._inst_reg.list_aliases() or [])
        aliases = sorted(lib_aliases | inst_aliases)

        self._combo.blockSignals(True)
        prev = self._combo.currentText()
        self._combo.clear()
        for a in aliases:
            self._combo.addItem(a)
        self._combo.blockSignals(False)

        idx = self._combo.findText(prev)
        self._combo.setCurrentIndex(max(idx, 0))
        if self._combo.count():
            self._on_instrument_changed(self._combo.currentText())

    def _add_instrument(self):
        text, ok = QInputDialog.getText(self, "Add Instrument", "Instrument alias:")
        if ok and text.strip():
            self._lib_reg.add_instrument(text.strip())
            self._populate_combo()
            idx = self._combo.findText(text.strip())
            if idx >= 0:
                self._combo.setCurrentIndex(idx)

    # ------------------------------------------------------------------
    # Table population
    # ------------------------------------------------------------------

    def _save_current_silent(self):
        """Save the current instrument's entries to the registry (no dialog)."""
        if not self._current_alias:
            return
        measurements = [e for t, e in self._entries if t == "measurement"]
        sweep_values = [e for t, e in self._entries if t == "sweep value"]
        write_cmds   = [e for t, e in self._entries if t == "write"]
        self._lib_reg.set_library(
            self._current_alias,
            InstrumentCmdLibrary(
                measurements=measurements,
                sweep_values=sweep_values,
                write_cmds=write_cmds,
            )
        )
        self._lib_reg.save()
        self.library_saved.emit()

    def _on_instrument_changed(self, alias: str):
        self._save_current_silent()   # save previous instrument first
        self._current_alias = alias
        lib = self._lib_reg.get_library(alias)
        self._entries = (
            [("measurement", e) for e in lib.measurements] +
            [("sweep value", e) for e in lib.sweep_values] +
            [("write", e) for e in lib.write_cmds]
        )
        self._populate_table()

    def _populate_table(self):
        self._table.setRowCount(len(self._entries))
        for row, (typ, entry) in enumerate(self._entries):
            if typ == "measurement":
                e: MeasurementParamDef = entry
                self._set_row(row, e.description, e.cmd_query,
                              e.figure_axis, e.unit, "measurement", "—")
            elif typ == "sweep value":
                e: SweepValueDef = entry
                paired_display = e.paired_read_cmd[:60] + ("…" if len(e.paired_read_cmd) > 60 else "")
                self._set_row(row, e.description, e.cmd_set,
                              e.figure_axis, e.unit, "sweep value", paired_display)
            else:
                e: WriteCmdDef = entry
                self._set_row(row, e.description, e.cmd_set,
                              e.figure_axis, e.unit, "write", "—")

    def _set_row(self, row, desc, cmd, axis, unit, typ, paired):
        values = [desc, cmd, axis, unit, typ, paired]
        if typ == "measurement":
            color = _COL_MEAS
        elif typ == "sweep value":
            color = _COL_SWEEP
        else:
            color = _COL_WRITE
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if col == 4:            # Type 열만 색상 강조
                item.setForeground(color)
            self._table.setItem(row, col, item)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def _add_measurement(self):
        if not self._current_alias:
            return
        dlg = MeasurementParamDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._entries.append(("measurement", dlg.get_entry()))
            self._populate_table()

    def _add_sweep_value(self):
        if not self._current_alias:
            return
        dlg = SweepValueDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._entries.append(("sweep value", dlg.get_entry()))
            self._populate_table()

    def _add_write(self):
        if not self._current_alias:
            return
        dlg = WriteCmdDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._entries.append(("write", dlg.get_entry()))
            self._populate_table()

    def _edit(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._entries):
            return
        typ, entry = self._entries[row]
        if typ == "measurement":
            dlg = MeasurementParamDialog(self, entry)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._entries[row] = ("measurement", dlg.get_entry())
        elif typ == "sweep value":
            dlg = SweepValueDialog(self, entry)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._entries[row] = ("sweep value", dlg.get_entry())
        else:
            dlg = WriteCmdDialog(self, entry)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._entries[row] = ("write", dlg.get_entry())
        self._populate_table()

    def _duplicate(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._entries):
            return
        typ, entry = self._entries[row]
        copy = entry.model_copy(deep=True)
        self._entries.insert(row + 1, (typ, copy))
        self._populate_table()
        self._table.selectRow(row + 1)

    def _delete(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._entries):
            return
        self._entries.pop(row)
        self._populate_table()

    def _save(self):
        if not self._current_alias:
            return
        measurements = [e for t, e in self._entries if t == "measurement"]
        sweep_values = [e for t, e in self._entries if t == "sweep value"]
        write_cmds   = [e for t, e in self._entries if t == "write"]
        self._lib_reg.set_library(
            self._current_alias,
            InstrumentCmdLibrary(
                measurements=measurements,
                sweep_values=sweep_values,
                write_cmds=write_cmds,
            )
        )
        self._lib_reg.save()
        self.library_saved.emit()
        QMessageBox.information(self, "Saved", f"'{self._current_alias}' 라이브러리가 저장되었습니다.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _current_measurements(self) -> List[MeasurementParamDef]:
        return [e for t, e in self._entries if t == "measurement"]

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._save_current_silent()
            self.hide()
            event.accept()
            return
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_S):
            self._save()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._current_alias:
            measurements = [e for t, e in self._entries if t == "measurement"]
            sweep_values = [e for t, e in self._entries if t == "sweep value"]
            write_cmds   = [e for t, e in self._entries if t == "write"]
            self._lib_reg.set_library(
                self._current_alias,
                InstrumentCmdLibrary(
                    measurements=measurements,
                    sweep_values=sweep_values,
                    write_cmds=write_cmds,
                )
            )
            self._lib_reg.save()
            self.library_saved.emit()
        event.ignore()
        self.hide()
