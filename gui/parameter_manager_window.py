"""
Parameter Manager — Add/Delete/Copy based UI for building the MainUIProfile.

Each section (Sweep Values, Measurements, Second Sweep Channels) has an
independent table of instantiated entries with Add/Edit/Delete/Copy/Up/Down
buttons.  An Apply button saves the profile and emits selection_applied.
"""
import re
from typing import Dict, List, Optional, Tuple, Union

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QGroupBox, QScrollArea, QWidget, QCheckBox,
    QMessageBox, QFrame, QSpinBox, QDoubleSpinBox,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from config.config_models import (
    InstantiatedMeasurement, InstantiatedSweepValue, InstantiatedWriteCmd,
    InstantiatedSecondSweepChannel, SecondSweepAdvanceType, MainUIProfile,
    MeasurementParamDef, SweepValueDef, WriteCmdDef,
)
from core.visa_library_registry import VisaLibraryRegistry
from core.profile_registry import ProfileRegistry

_MONO = QFont("Consolas", 10)

_ADVANCE_LABELS = {
    SecondSweepAdvanceType.SIMPLE_HOP:    "Simple Hop",
    SecondSweepAdvanceType.SWEEP:         "Sweep",
    SecondSweepAdvanceType.FEEDBACK:      "Feedback",
    SecondSweepAdvanceType.WAIT_FOR_TIME: "Wait for Time",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _placeholders(text: str) -> List[str]:
    """Return unique ordered list of {name} placeholders in text."""
    return list(dict.fromkeys(re.findall(r"\{(\w+)\}", text)))


def _truncate(s: str, n: int = 60) -> str:
    return s[:n] + ("…" if len(s) > n else "")


# ---------------------------------------------------------------------------
# AddEntryDialog — pick from library, fill placeholders
# ---------------------------------------------------------------------------

class AddEntryDialog(QDialog):
    """
    Dialog for adding a new instantiated entry to one of the PM sections.

    section_type: 'sweep' | 'measurement' | 'write' | 'second'
    """

    def __init__(
        self,
        lib_registry: VisaLibraryRegistry,
        section_type: str,          # 'sweep' | 'measurement' | 'write' | 'second'
        parent=None,
        *,
        prefill: Optional[Union[
            InstantiatedSweepValue,
            InstantiatedMeasurement,
            InstantiatedWriteCmd,
            InstantiatedSecondSweepChannel,
        ]] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Add Entry" if prefill is None else "Edit Entry")
        self.setMinimumWidth(560)
        self._lib_reg = lib_registry
        self._section_type = section_type
        self._prefill = prefill
        self._result = None

        # per-placeholder widgets: {ph_name: (combo_or_none, line_edit)}
        self._ph_widgets: Dict[str, Tuple] = {}
        self._base_axis: str = ""       # library figure_axis, used for auto-axis
        self._axis_auto: bool = True    # False once user manually edits axis
        self._base_desc: str = ""
        self._desc_auto: bool = True

        self._build_ui()
        if prefill is not None:
            self._populate_from_prefill()

    # ------------------------------------------------------------------
    # UI build
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        # ── Alias + Entry selection ───────────────────────────────────
        sel_form = QFormLayout()
        sel_form.setHorizontalSpacing(12)

        self._combo_alias = QComboBox()
        aliases = self._lib_reg.list_aliases()
        for a in aliases:
            self._combo_alias.addItem(a)
        sel_form.addRow("Instrument:", self._combo_alias)

        self._combo_entry = QComboBox()
        self._combo_entry.setFont(_MONO)
        sel_form.addRow("Library entry:", self._combo_entry)
        outer.addLayout(sel_form)

        self._combo_alias.currentTextChanged.connect(self._refresh_entry_combo)
        self._combo_entry.currentIndexChanged.connect(self._refresh_placeholders)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #30363d;")
        outer.addWidget(sep)

        # ── Command preview ───────────────────────────────────────────
        self._lbl_cmd_preview = QLabel("")
        self._lbl_cmd_preview.setFont(_MONO)
        self._lbl_cmd_preview.setStyleSheet("color: #888888;")
        self._lbl_cmd_preview.setWordWrap(True)
        outer.addWidget(self._lbl_cmd_preview)

        # ── Placeholder section (dynamic) ─────────────────────────────
        self._ph_widget = QWidget()
        self._ph_layout = QFormLayout(self._ph_widget)
        self._ph_layout.setHorizontalSpacing(12)
        self._ph_layout.setContentsMargins(0, 4, 0, 4)
        outer.addWidget(self._ph_widget)

        # Sweep note
        if self._section_type in ('sweep', 'second'):
            note = QLabel("For sweep entries: exactly one placeholder must be set to [SWEEP].")
            note.setStyleSheet("color: #79c0ff; font-size: 11px;")
            note.setWordWrap(True)
            outer.addWidget(note)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color: #30363d;")
        outer.addWidget(sep2)

        # ── Metadata fields ───────────────────────────────────────────
        meta_form = QFormLayout()
        meta_form.setHorizontalSpacing(12)
        self._le_desc = QLineEdit()
        self._le_axis = QLineEdit()
        self._le_unit = QLineEdit()
        meta_form.addRow("Description:", self._le_desc)
        meta_form.addRow("Figure Axis:", self._le_axis)
        meta_form.addRow("Unit:", self._le_unit)
        outer.addLayout(meta_form)

        # Detect manual desc edits (suppress auto-update when user types)
        self._le_desc.textEdited.connect(lambda: setattr(self, '_desc_auto', False))

        # Detect manual axis edits (suppress auto-update when user types)
        self._le_axis.textEdited.connect(lambda: setattr(self, '_axis_auto', False))

        # ── Safety Ramp (sweep section only) ─────────────────────────
        self._safety_widget = QWidget()
        safety_vbox = QVBoxLayout(self._safety_widget)
        safety_vbox.setContentsMargins(0, 0, 0, 0)
        safety_vbox.setSpacing(4)

        sep_safety = QFrame()
        sep_safety.setFrameShape(QFrame.Shape.HLine)
        sep_safety.setStyleSheet("color: #30363d;")
        safety_vbox.addWidget(sep_safety)

        self._cb_safety = QCheckBox("Safety Ramp")
        self._cb_safety.setToolTip(
            "목표값으로 바로 이동하지 않고 지정된 스텝 수만큼\n"
            "균등 분할하여 천천히 이동합니다."
        )
        self._cb_safety.setStyleSheet("font-weight: bold; color: #ffa657;")
        safety_vbox.addWidget(self._cb_safety)

        self._safety_detail = QWidget()
        safety_form = QFormLayout(self._safety_detail)
        safety_form.setContentsMargins(12, 0, 0, 0)
        safety_form.setHorizontalSpacing(12)
        safety_form.setVerticalSpacing(4)

        self._sb_safety_steps = QSpinBox()
        self._sb_safety_steps.setRange(1, 10000)
        self._sb_safety_steps.setValue(10)
        self._sb_safety_steps.setToolTip("목표값까지 나눌 중간 스텝 수 (예: 10 → 10번에 나눠 이동)")
        safety_form.addRow("Steps:", self._sb_safety_steps)

        self._sb_safety_interval = QDoubleSpinBox()
        self._sb_safety_interval.setRange(0.0, 60000.0)
        self._sb_safety_interval.setDecimals(1)
        self._sb_safety_interval.setValue(0.0)
        self._sb_safety_interval.setSuffix("  ms")
        self._sb_safety_interval.setToolTip(
            "각 스텝 사이 최소 대기 시간 (밀리초)\n"
            "0 = 배치 전송 (권장): N번 write → 1번 write로 최적화,\n"
            "   인터벌 없이 기기 펌웨어 속도로 연속 실행됨\n"
            "> 0 = VISA write 소요 시간을 차감한 실제 settle 대기"
        )
        safety_form.addRow("Interval:", self._sb_safety_interval)

        safety_vbox.addWidget(self._safety_detail)

        self._cb_safety.toggled.connect(self._safety_detail.setVisible)
        self._safety_detail.setVisible(False)   # collapsed by default

        self._safety_widget.setVisible(self._section_type == 'sweep')
        outer.addWidget(self._safety_widget)

        # ── Second sweep advance type ─────────────────────────────────
        self._second_widget = QWidget()
        second_layout = QVBoxLayout(self._second_widget)
        second_layout.setContentsMargins(0, 0, 0, 0)

        sep3 = QFrame()
        sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setStyleSheet("color: #30363d;")
        second_layout.addWidget(sep3)

        adv_lbl = QLabel("Second Sweep Channel Settings")
        adv_lbl.setStyleSheet("font-weight: bold; color: #f78166;")
        second_layout.addWidget(adv_lbl)

        adv_form = QFormLayout()
        adv_form.setHorizontalSpacing(12)

        self._combo_source_type = QComboBox()
        self._combo_source_type.addItem("Sweep Value", "sweep_value")
        self._combo_source_type.addItem("Write Command", "write_cmd")
        adv_form.addRow("Source Type:", self._combo_source_type)

        self._combo_advance = QComboBox()
        for at, lbl in _ADVANCE_LABELS.items():
            self._combo_advance.addItem(lbl, at)
        adv_form.addRow("Advance Type:", self._combo_advance)
        second_layout.addLayout(adv_form)

        # FEEDBACK fields
        self._fb_widget = QWidget()
        fb_form = QFormLayout(self._fb_widget)
        fb_form.setContentsMargins(0, 0, 0, 0)
        self._le_fb_cmd  = QLineEdit()
        self._le_fb_cmd.setFont(_MONO)
        self._le_fb_cmd.setPlaceholderText("e.g. print(smua.measure.v())")
        self._le_fb_poll = QLineEdit("1.0")
        self._le_fb_tol  = QLineEdit("95.0")
        fb_form.addRow("Feedback Read Cmd:", self._le_fb_cmd)
        fb_form.addRow("Poll Interval (s):",  self._le_fb_poll)
        fb_form.addRow("Tolerance (%):",      self._le_fb_tol)
        second_layout.addWidget(self._fb_widget)

        # WAIT_FOR_TIME fields
        self._wait_widget = QWidget()
        wait_form = QFormLayout(self._wait_widget)
        wait_form.setContentsMargins(0, 0, 0, 0)
        self._le_wait = QLineEdit("1.0")
        wait_form.addRow("Wait Time (s):", self._le_wait)
        second_layout.addWidget(self._wait_widget)

        outer.addWidget(self._second_widget)

        self._combo_advance.currentIndexChanged.connect(self._update_advance_visibility)
        self._combo_source_type.currentIndexChanged.connect(self._update_advance_options)
        self._second_widget.setVisible(self._section_type == 'second')

        # ── OK / Cancel ───────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_cancel.setAutoDefault(False)
        btn_ok = QPushButton("OK")
        btn_ok.setStyleSheet(
            "background-color: #2e7d32; color: white; font-weight: bold; padding: 4px 16px;"
        )
        btn_ok.clicked.connect(self._on_ok)
        btn_ok.setDefault(True)   # Enter 키로 OK 활성화
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        outer.addLayout(btn_row)

        # Initial population
        if aliases:
            self._refresh_entry_combo(aliases[0])
        self._update_advance_visibility(0)

    def _refresh_entry_combo(self, alias: str):
        self._combo_entry.blockSignals(True)
        self._combo_entry.clear()
        lib = self._lib_reg.get_library(alias)
        entries: List[Union[MeasurementParamDef, SweepValueDef, WriteCmdDef]] = []
        if self._section_type == 'measurement':
            entries = lib.measurements
        elif self._section_type == 'sweep':
            entries = lib.sweep_values
        elif self._section_type == 'write':
            entries = lib.write_cmds
        else:  # 'second': sweep_values + write_cmds
            entries = list(lib.sweep_values) + list(lib.write_cmds)

        self._lib_entries = entries
        for e in entries:
            cmd = e.cmd_query if hasattr(e, 'cmd_query') else e.cmd_set
            label = f"{e.description}  [{_truncate(cmd, 50)}]"
            self._combo_entry.addItem(label)
        self._combo_entry.blockSignals(False)
        self._refresh_placeholders(self._combo_entry.currentIndex())

    def _refresh_placeholders(self, idx: int):
        # Clear existing placeholder rows
        while self._ph_layout.rowCount():
            self._ph_layout.removeRow(0)
        self._ph_widgets.clear()

        if not hasattr(self, '_lib_entries') or idx < 0 or idx >= len(self._lib_entries):
            self._lbl_cmd_preview.setText("")
            return

        entry = self._lib_entries[idx]
        # Collect cmd text(s)
        if hasattr(entry, 'cmd_query'):
            cmd_texts = [entry.cmd_query]
            preview = f"cmd_query: {entry.cmd_query}"
        else:
            cmd_texts = [entry.cmd_set]
            if hasattr(entry, 'paired_read_cmd') and entry.paired_read_cmd:
                cmd_texts.append(entry.paired_read_cmd)
                preview = f"cmd_set: {entry.cmd_set}\npaired_read_cmd: {entry.paired_read_cmd}"
            else:
                preview = f"cmd_set: {entry.cmd_set}"
        self._lbl_cmd_preview.setText(preview)

        # Collect all unique placeholders
        all_phs: List[str] = []
        for ct in cmd_texts:
            for ph in _placeholders(ct):
                if ph not in all_phs:
                    all_phs.append(ph)

        # Which placeholders come from cmd_set (can be [SWEEP])
        sweep_eligible: set = set()
        if hasattr(entry, 'cmd_set') and self._section_type in ('sweep', 'second'):
            sweep_eligible = set(_placeholders(entry.cmd_set))

        # Build rows
        self._ph_widgets = {}
        for ph in all_phs:
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)

            if ph in sweep_eligible:
                combo = QComboBox()
                combo.addItem("[SWEEP]")
                combo.addItem("fixed")
                le = QLineEdit()
                le.setFont(_MONO)
                le.setPlaceholderText("fixed value")

                def _toggle(index, _le=le):
                    _le.setVisible(index == 1)  # show only when "fixed" selected

                combo.currentIndexChanged.connect(_toggle)
                combo.currentIndexChanged.connect(lambda _: self._update_auto_axis())
                combo.currentIndexChanged.connect(lambda _: self._update_auto_desc())
                le.textChanged.connect(lambda _: self._update_auto_axis())
                le.textChanged.connect(lambda _: self._update_auto_desc())
                _toggle(combo.currentIndex())

                row_layout.addWidget(combo)
                row_layout.addWidget(le)
                self._ph_layout.addRow(f"  {{{ph}}}:", row_widget)
                self._ph_widgets[ph] = (combo, le)
            else:
                le = QLineEdit()
                le.setFont(_MONO)
                le.setPlaceholderText("value")
                le.textChanged.connect(lambda _: self._update_auto_axis())
                le.textChanged.connect(lambda _: self._update_auto_desc())
                row_layout.addWidget(le)
                self._ph_layout.addRow(f"  {{{ph}}}:", row_widget)
                self._ph_widgets[ph] = (None, le)

        # Pre-fill description from library; reset auto-axis and auto-desc state
        self._base_desc = entry.description
        self._desc_auto = True
        self._le_desc.blockSignals(True)
        self._le_desc.setText(entry.description)
        self._le_desc.blockSignals(False)
        self._base_axis = entry.figure_axis if hasattr(entry, 'figure_axis') else ""
        self._axis_auto = True
        if hasattr(entry, 'figure_axis'):
            self._le_axis.blockSignals(True)
            self._le_axis.setText(entry.figure_axis)
            self._le_axis.blockSignals(False)
        if hasattr(entry, 'unit'):
            self._le_unit.setText(entry.unit)

        self._update_auto_axis()
        self._update_auto_desc()

        # Update second sweep source type combo
        if self._section_type == 'second':
            is_sweep = hasattr(entry, 'paired_read_cmd')
            self._combo_source_type.blockSignals(True)
            self._combo_source_type.setCurrentIndex(0 if is_sweep else 1)
            self._combo_source_type.blockSignals(False)
            self._update_advance_options(self._combo_source_type.currentIndex())

        self.adjustSize()

    def _update_auto_axis(self):
        """Replace {p} placeholders in the library figure_axis with filled values.

        e.g. base='smua_current_{ch}', {ch}=1 → 'smua_current_1'
        [SWEEP] placeholders are left as-is in the axis string.
        Only runs when _axis_auto is True (user hasn't manually edited the field).
        """
        if not self._axis_auto:
            return
        axis = self._base_axis
        for ph, (combo, le) in self._ph_widgets.items():
            if combo is not None and combo.currentIndex() == 0:
                # [SWEEP] — leave {ph} unreplaced in figure_axis
                continue
            val = le.text().strip()
            if val:
                axis = axis.replace(f"{{{ph}}}", val)
        self._le_axis.blockSignals(True)
        self._le_axis.setText(axis)
        self._le_axis.blockSignals(False)

    def _update_auto_desc(self):
        """Replace {p} placeholders in the library description with filled values."""
        if not self._desc_auto:
            return
        desc = self._base_desc
        for ph, (combo, le) in self._ph_widgets.items():
            if combo is not None and combo.currentIndex() == 0:
                continue  # [SWEEP] — leave {ph} unreplaced
            val = le.text().strip()
            if val:
                desc = desc.replace(f"{{{ph}}}", val)
        self._le_desc.blockSignals(True)
        self._le_desc.setText(desc)
        self._le_desc.blockSignals(False)

    def _update_advance_visibility(self, idx: int):
        at = self._combo_advance.itemData(idx)
        self._fb_widget.setVisible(at == SecondSweepAdvanceType.FEEDBACK)
        self._wait_widget.setVisible(at == SecondSweepAdvanceType.WAIT_FOR_TIME)

    def _update_advance_options(self, idx: int):
        source_type = self._combo_source_type.itemData(idx)
        for i in range(self._combo_advance.count()):
            at = self._combo_advance.itemData(i)
            item = self._combo_advance.model().item(i)
            if item:
                if source_type == "write_cmd" and at in (
                    SecondSweepAdvanceType.SWEEP,
                    SecondSweepAdvanceType.FEEDBACK,
                ):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                else:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)

    # ------------------------------------------------------------------
    # Pre-fill from existing entry (Edit mode)
    # ------------------------------------------------------------------

    def _populate_from_prefill(self):
        p = self._prefill

        # Set alias combo
        if hasattr(p, 'alias'):
            idx = self._combo_alias.findText(p.alias)
            if idx >= 0:
                self._combo_alias.setCurrentIndex(idx)
                self._refresh_entry_combo(p.alias)

        # Find library entry by description
        if hasattr(self, '_lib_entries'):
            for i, e in enumerate(self._lib_entries):
                if e.description == p.description:
                    self._combo_entry.setCurrentIndex(i)
                    self._refresh_placeholders(i)
                    break

        # Fill metadata
        self._le_desc.setText(p.description)
        self._base_desc = p.description
        self._desc_auto = False  # editing existing entry — don't auto-update
        if hasattr(p, 'figure_axis'):
            self._le_axis.setText(p.figure_axis)
        if hasattr(p, 'unit'):
            self._le_unit.setText(p.unit)

        # Fill placeholders from fill_params / resolved data
        fill = getattr(p, 'fill_params', {})

        for ph, widgets in self._ph_widgets.items():
            combo, le = widgets
            if combo is not None:
                # sweep-eligible: check if {v} appears in cmd_set
                if '{v}' in getattr(p, 'cmd_set', ''):
                    # This placeholder was [SWEEP]
                    combo.setCurrentIndex(0)
                    le.setVisible(False)
                else:
                    val = fill.get(ph, '')
                    combo.setCurrentIndex(1)
                    le.setVisible(True)
                    le.setText(val)
            else:
                val = fill.get(ph, fill.get(f"paired.{ph}", ''))
                le.setText(val)

        # Safety ramp (sweep section only)
        if self._section_type == 'sweep' and isinstance(p, InstantiatedSweepValue):
            if p.safety_steps > 0:
                self._cb_safety.setChecked(True)
                self._sb_safety_steps.setValue(p.safety_steps)
                self._sb_safety_interval.setValue(p.safety_interval_ms)
            else:
                self._cb_safety.setChecked(False)

        # Second sweep advance type
        if self._section_type == 'second' and isinstance(p, InstantiatedSecondSweepChannel):
            # source type
            st_idx = 0 if p.source_type == 'sweep_value' else 1
            self._combo_source_type.setCurrentIndex(st_idx)
            self._update_advance_options(st_idx)

            for i in range(self._combo_advance.count()):
                if self._combo_advance.itemData(i) == p.advance_type:
                    self._combo_advance.setCurrentIndex(i)
                    break
            self._update_advance_visibility(self._combo_advance.currentIndex())
            self._le_fb_cmd.setText(p.feedback_read_cmd)
            self._le_fb_poll.setText(str(p.feedback_poll_interval))
            self._le_fb_tol.setText(str(p.feedback_tolerance_pct))
            self._le_wait.setText(str(p.wait_time))

    # ------------------------------------------------------------------
    # Validation & result
    # ------------------------------------------------------------------

    def _on_ok(self):
        alias = self._combo_alias.currentText()
        desc  = self._le_desc.text().strip()
        axis  = self._le_axis.text().strip()
        unit  = self._le_unit.text().strip()

        if not alias:
            QMessageBox.warning(self, "Validation", "Select an instrument alias.")
            return
        if not desc:
            QMessageBox.warning(self, "Validation", "Description is required.")
            return

        idx = self._combo_entry.currentIndex()
        if idx < 0 or not hasattr(self, '_lib_entries') or idx >= len(self._lib_entries):
            QMessageBox.warning(self, "Validation", "Select a library entry.")
            return

        entry = self._lib_entries[idx]

        # Validate and collect placeholder values
        filled: Dict[str, str] = {}   # ph -> value (or '[SWEEP]' for sweep placeholder)
        sweep_ph: Optional[str] = None

        for ph, (combo, le) in self._ph_widgets.items():
            if combo is not None:
                if combo.currentIndex() == 0:  # [SWEEP]
                    if sweep_ph is not None:
                        QMessageBox.warning(
                            self, "Validation",
                            "Exactly one placeholder must be [SWEEP]."
                        )
                        return
                    sweep_ph = ph
                    filled[ph] = '[SWEEP]'
                else:
                    val = le.text().strip()
                    if not val:
                        QMessageBox.warning(
                            self, "Validation",
                            f"Placeholder {{{ph}}} needs a fixed value."
                        )
                        return
                    filled[ph] = val
            else:
                val = le.text().strip()
                if not val:
                    QMessageBox.warning(
                        self, "Validation",
                        f"Placeholder {{{ph}}} needs a value."
                    )
                    return
                filled[ph] = val

        # Section-specific validation & instantiation
        if self._section_type == 'measurement':
            if not hasattr(entry, 'cmd_query'):
                QMessageBox.warning(self, "Validation", "Selected entry is not a measurement.")
                return
            try:
                fixed_filled = {k: v for k, v in filled.items()}
                resolved = entry.cmd_query.format(**fixed_filled) if fixed_filled else entry.cmd_query
            except KeyError as e:
                QMessageBox.warning(self, "Error", f"Placeholder error: {e}")
                return
            self._result = InstantiatedMeasurement(
                alias=alias,
                description=desc,
                resolved_cmd=resolved,
                figure_axis=axis,
                unit=unit,
                fill_params=filled,
            )

        elif self._section_type == 'sweep':
            if not hasattr(entry, 'cmd_set'):
                QMessageBox.warning(self, "Validation", "Selected entry is not a sweep value.")
                return
            if sweep_ph is None:
                QMessageBox.warning(
                    self, "Validation",
                    "Exactly one placeholder in cmd_set must be set to [SWEEP]."
                )
                return

            # Build cmd_set: replace sweep_ph with {v}, others with fixed values
            cmd_set = entry.cmd_set
            for ph, val in filled.items():
                if ph == sweep_ph:
                    cmd_set = cmd_set.replace(f"{{{ph}}}", "{v}")
                else:
                    cmd_set = cmd_set.replace(f"{{{ph}}}", val)

            # Build paired_read_cmd: replace all placeholders with fixed values
            paired_read_cmd = entry.paired_read_cmd
            for ph, val in filled.items():
                fixed_val = val if val != '[SWEEP]' else filled.get(ph, val)
                paired_read_cmd = paired_read_cmd.replace(f"{{{ph}}}", fixed_val)

            safety_on = self._cb_safety.isChecked()
            self._result = InstantiatedSweepValue(
                alias=alias,
                description=desc,
                cmd_set=cmd_set,
                paired_read_cmd=paired_read_cmd,
                figure_axis=axis,
                unit=unit,
                fill_params=filled,
                safety_steps=self._sb_safety_steps.value() if safety_on else 0,
                safety_interval_ms=self._sb_safety_interval.value() if safety_on else 0.0,
            )

        elif self._section_type == 'write':
            if not hasattr(entry, 'cmd_set'):
                QMessageBox.warning(self, "Validation", "Selected entry is not a write command.")
                return
            # For write: sweep_ph is optional (at most one)
            cmd_set = entry.cmd_set
            for ph, val in filled.items():
                if val == '[SWEEP]':
                    cmd_set = cmd_set.replace(f"{{{ph}}}", "{v}")
                else:
                    cmd_set = cmd_set.replace(f"{{{ph}}}", val)
            self._result = InstantiatedWriteCmd(
                alias=alias,
                description=desc,
                cmd_set=cmd_set,
                figure_axis=axis,
                unit=unit,
                fill_params=filled,
            )

        else:  # 'second'
            if sweep_ph is None and self._ph_widgets:
                QMessageBox.warning(
                    self, "Validation",
                    "Exactly one placeholder in cmd_set must be set to [SWEEP]."
                )
                return

            source_type = self._combo_source_type.currentData()
            at: SecondSweepAdvanceType = self._combo_advance.currentData()

            # Build cmd_set
            cmd_set = entry.cmd_set if hasattr(entry, 'cmd_set') else ""
            for ph, val in filled.items():
                if ph == sweep_ph:
                    cmd_set = cmd_set.replace(f"{{{ph}}}", "{v}")
                else:
                    cmd_set = cmd_set.replace(f"{{{ph}}}", val)

            # paired_read_cmd for sweep_value source
            paired_read_cmd = ""
            if source_type == 'sweep_value' and hasattr(entry, 'paired_read_cmd'):
                paired_read_cmd = entry.paired_read_cmd
                for ph, val in filled.items():
                    paired_read_cmd = paired_read_cmd.replace(f"{{{ph}}}", val if val != '[SWEEP]' else val)

            if at == SecondSweepAdvanceType.FEEDBACK:
                fb_cmd = self._le_fb_cmd.text().strip()
                if not fb_cmd:
                    QMessageBox.warning(self, "Validation", "Feedback read command is required.")
                    return
                try:
                    fb_poll = float(self._le_fb_poll.text().strip())
                    fb_tol  = float(self._le_fb_tol.text().strip())
                except ValueError:
                    QMessageBox.warning(self, "Validation", "Feedback parameters must be numbers.")
                    return
                self._result = InstantiatedSecondSweepChannel(
                    alias=alias, description=desc,
                    source_type=source_type, advance_type=at, cmd_set=cmd_set,
                    paired_read_cmd=paired_read_cmd,
                    feedback_read_cmd=fb_cmd,
                    feedback_poll_interval=fb_poll,
                    feedback_tolerance_pct=fb_tol,
                    figure_axis=axis, unit=unit,
                )
            elif at == SecondSweepAdvanceType.WAIT_FOR_TIME:
                try:
                    wait_time = float(self._le_wait.text().strip())
                except ValueError:
                    QMessageBox.warning(self, "Validation", "Wait time must be a number.")
                    return
                self._result = InstantiatedSecondSweepChannel(
                    alias=alias, description=desc,
                    source_type=source_type, advance_type=at, cmd_set=cmd_set,
                    paired_read_cmd=paired_read_cmd,
                    wait_time=wait_time,
                    figure_axis=axis, unit=unit,
                )
            else:  # SIMPLE_HOP or SWEEP
                self._result = InstantiatedSecondSweepChannel(
                    alias=alias, description=desc,
                    source_type=source_type, advance_type=at, cmd_set=cmd_set,
                    paired_read_cmd=paired_read_cmd,
                    figure_axis=axis, unit=unit,
                )

        self.accept()

    def get_result(self):
        return self._result


# ---------------------------------------------------------------------------
# SectionPanel — table + CRUD buttons for one section
# ---------------------------------------------------------------------------

class SectionPanel(QGroupBox):
    """
    A group box containing a table and Add/Edit/Delete/Copy/Up/Down buttons
    for one section of the Parameter Manager.
    """

    def __init__(
        self,
        title: str,
        headers: List[str],
        color: str,
        lib_registry: VisaLibraryRegistry,
        section_type: str,
        parent=None,
    ):
        super().__init__(title, parent)
        self.setStyleSheet(f"QGroupBox {{ font-weight: bold; color: {color}; }}")
        self._lib_reg = lib_registry
        self._section_type = section_type
        self._headers = headers
        self._items: List = []   # list of instantiated entries

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 14, 6, 6)
        layout.setSpacing(4)

        # Table
        self._table = QTableWidget()
        self._table.setColumnCount(len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        hdr = self._table.horizontalHeader()
        for i in range(len(headers)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        # Let the last column stretch
        hdr.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setFont(QFont("Consolas", 9))
        self._table.setStyleSheet(
            "background-color: #0d1117; color: #c9d1d9; gridline-color: #21262d;"
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setMinimumHeight(100)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        layout.addWidget(self._table)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._btn_add    = QPushButton("Add")
        self._btn_edit   = QPushButton("Edit")
        self._btn_delete = QPushButton("Delete")
        self._btn_copy   = QPushButton("Copy")
        self._btn_up     = QPushButton("↑")
        self._btn_down   = QPushButton("↓")
        for btn in (self._btn_add, self._btn_edit, self._btn_delete,
                    self._btn_copy, self._btn_up, self._btn_down):
            btn.setFixedHeight(24)
        self._btn_up.setFixedWidth(28)
        self._btn_down.setFixedWidth(28)

        self._btn_add.clicked.connect(self._add)
        self._btn_edit.clicked.connect(self._edit)
        self._btn_delete.clicked.connect(self._delete)
        self._btn_copy.clicked.connect(self._copy)
        self._btn_up.clicked.connect(self._move_up)
        self._btn_down.clicked.connect(self._move_down)

        btn_row.addWidget(self._btn_add)
        btn_row.addWidget(self._btn_edit)
        btn_row.addWidget(self._btn_delete)
        btn_row.addWidget(self._btn_copy)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_up)
        btn_row.addWidget(self._btn_down)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def load_items(self, items: List):
        self._items = list(items)
        self._refresh_table()

    def get_items(self) -> List:
        return list(self._items)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_cell_double_clicked(self, *_) -> None:
        self._edit()

    def _refresh_table(self):
        self._table.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            self._set_row(row, item)

    def _set_row(self, row: int, item):
        vals = self._row_values(item)
        for col, text in enumerate(vals):
            cell = QTableWidgetItem(str(text))
            cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._table.setItem(row, col, cell)

    def _row_values(self, item) -> List[str]:
        """Map an instantiated entry to table column values."""
        if self._section_type == 'measurement':
            return [
                item.alias,
                item.description,
                _truncate(item.resolved_cmd, 60),
                item.unit,
            ]
        elif self._section_type == 'sweep':
            if item.safety_steps > 0:
                safety_str = f"{item.safety_steps} steps / {item.safety_interval_ms:.0f} ms"
            else:
                safety_str = "—"
            return [
                item.alias,
                item.description,
                _truncate(item.cmd_set, 50),
                _truncate(item.paired_read_cmd, 50),
                item.unit,
                safety_str,
            ]
        elif self._section_type == 'write':
            return [
                item.alias,
                item.description,
                _truncate(item.cmd_set, 60),
                item.unit,
            ]
        else:  # 'second'
            at_label = _ADVANCE_LABELS.get(item.advance_type, str(item.advance_type))
            return [
                item.alias,
                item.description,
                _truncate(item.cmd_set, 50),
                at_label,
                item.unit,
            ]

    def _add(self):
        dlg = AddEntryDialog(self._lib_reg, self._section_type, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.get_result()
            if result is not None:
                self._items.append(result)
                self._refresh_table()
                self._table.selectRow(len(self._items) - 1)

    def _edit(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._items):
            return
        dlg = AddEntryDialog(
            self._lib_reg, self._section_type, self,
            prefill=self._items[row],
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.get_result()
            if result is not None:
                self._items[row] = result
                self._refresh_table()
                self._table.selectRow(row)

    def _delete(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._items):
            return
        self._items.pop(row)
        self._refresh_table()

    def _copy(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._items):
            return
        copy = self._items[row].model_copy(deep=True)
        self._items.insert(row + 1, copy)
        self._refresh_table()
        self._table.selectRow(row + 1)

    def _move_up(self):
        row = self._table.currentRow()
        if row <= 0:
            return
        self._items[row - 1], self._items[row] = self._items[row], self._items[row - 1]
        self._refresh_table()
        self._table.selectRow(row - 1)

    def _move_down(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._items) - 1:
            return
        self._items[row], self._items[row + 1] = self._items[row + 1], self._items[row]
        self._refresh_table()
        self._table.selectRow(row + 1)


# ---------------------------------------------------------------------------
# ParameterManagerWindow
# ---------------------------------------------------------------------------

class ParameterManagerWindow(QDialog):
    """
    Parameter Manager — Add/Delete/Copy based UI.

    Three sections: Sweep Values, Measurements, Second Sweep Channels.
    Apply button saves to registry and emits selection_applied(MainUIProfile).
    """

    selection_applied = Signal(MainUIProfile)

    def __init__(
        self,
        lib_registry: VisaLibraryRegistry,
        param_manager_reg: ProfileRegistry,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Parameter Manager")
        self.resize(900, 680)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._lib_reg = lib_registry
        self._reg     = param_manager_reg

        self._build_ui()
        self._load_from_profile()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(10)
        content_layout.setContentsMargins(0, 0, 0, 0)

        self._panel_sweep = SectionPanel(
            "Sweep Values",
            ["Alias", "Description", "cmd_set", "paired_read", "Unit", "Safety"],
            "#79c0ff",
            self._lib_reg,
            'sweep',
        )
        content_layout.addWidget(self._panel_sweep)

        self._panel_meas = SectionPanel(
            "Measurements",
            ["Alias", "Description", "resolved_cmd", "Unit"],
            "#56d364",
            self._lib_reg,
            'measurement',
        )
        content_layout.addWidget(self._panel_meas)

        self._panel_second = SectionPanel(
            "Second Sweep Channels",
            ["Alias", "Description", "cmd_set", "Advance Type", "Unit"],
            "#f78166",
            self._lib_reg,
            'second',
        )
        content_layout.addWidget(self._panel_second)

        content_layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        # Bottom buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.hide)
        btn_apply = QPushButton("Apply")
        btn_apply.setStyleSheet(
            "background-color: #2e7d32; color: white; font-weight: bold; padding: 4px 20px;"
        )
        btn_apply.clicked.connect(self._on_apply)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_apply)
        outer.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Load / Apply
    # ------------------------------------------------------------------

    def _load_from_profile(self):
        mui = self._reg.main_ui_profile
        self._panel_sweep.load_items(mui.sweep_values)
        self._panel_meas.load_items(mui.measurements)
        self._panel_second.load_items(mui.second_sweep_channels)

    def _on_apply(self):
        profile = MainUIProfile(
            sweep_values=self._panel_sweep.get_items(),
            measurements=self._panel_meas.get_items(),
            write_cmds=[],
            second_sweep_channels=self._panel_second.get_items(),
        )
        self._reg.save_main_ui(profile)
        self.selection_applied.emit(profile)
        self.hide()

    # ------------------------------------------------------------------

    def showEvent(self, event):
        self._load_from_profile()
        super().showEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._on_apply()   # save + emit signal
            self.hide()
            event.accept()
            return
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_S):
            self._on_apply()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
