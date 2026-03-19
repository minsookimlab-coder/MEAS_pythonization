import re
from typing import Dict, List, Optional, Tuple, Union

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QGroupBox, QScrollArea, QWidget, QCheckBox,
    QMessageBox, QFrame,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtGui import QColor, QFont

from config.config_models import (
    MeasurementParamDef, SweepValueDef, WriteCmdDef,
    SelectedEntry, ParameterManagerProfile,
    InstantiatedMeasurement, InstantiatedSweepValue, InstantiatedWriteCmd, MainUIProfile,
    InstantiatedSecondSweepChannel, SecondSweepAdvanceType,
)
from core.visa_library_registry import VisaLibraryRegistry
from core.parameter_manager_registry import ParameterManagerRegistry

_MONO = QFont("Consolas", 10)
_COL_MEAS  = QColor("#56d364")
_COL_SWEEP = QColor("#79c0ff")
_COL_WRITE = QColor("#e3b341")
_HDR = ["", "Instrument", "Description", "VISA Command", "Unit"]


# ===========================================================================
# ParamFillDialog — 선택된 항목의 파라미터 값을 채우는 다이얼로그
# ===========================================================================

class ParamFillDialog(QDialog):
    """
    Parameter Manager Apply 시 표시되는 파라미터 입력 다이얼로그.

    규칙:
    - Measurement: 모든 플레이스홀더를 임의 값으로 채워야 함 ([SWEEP] 불필요)
    - Sweep Value: cmd_set 플레이스홀더 중 정확히 하나에 [SWEEP] 입력 필수
    - Write: cmd_set 플레이스홀더 중 최대 하나에만 [SWEEP] 허용 (없어도 됨)

    빈 파라미터가 하나라도 있으면 오류.
    """

    def __init__(
        self,
        entries: List[Tuple[str, str, Union[MeasurementParamDef, SweepValueDef, WriteCmdDef]]],
        existing_main_ui: Optional[MainUIProfile] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Fill Parameters")
        self.setMinimumWidth(560)
        self.resize(560, 500)

        self._entries = entries   # (alias, type_str, entry_object)
        self._forms: List[dict] = []   # per-entry field tracking
        self._result: Optional[MainUIProfile] = None

        # Lookup maps for pre-filling from existing profile
        ex = existing_main_ui or MainUIProfile()
        self._ex_meas  = {(m.alias, m.description): m for m in ex.measurements}
        self._ex_sweep = {(s.alias, s.description): s for s in ex.sweep_values}
        self._ex_write = {(w.alias, w.description): w for w in ex.write_cmds}

        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        info = QLabel(
            "각 항목의 파라미터를 입력하세요.\n"
            "Sweep Value / Write는 sweep할 파라미터 칸에 [SWEEP] 또는 [SWEEP,SAFETY(a,b)]를 입력하세요.\n"
            "  예) [SWEEP,SAFETY(10,5)]  → 10단계로 5ms 간격으로 안전하게 이동"
        )
        info.setStyleSheet("color: #888888; font-size: 11px;")
        info.setWordWrap(True)
        outer.addWidget(info)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(10)
        content_layout.setContentsMargins(0, 0, 0, 0)

        for alias, typ, entry in self._entries:
            grp, cmd_fields, paired_fields = self._build_entry_group(alias, typ, entry)
            self._forms.append({
                "alias": alias,
                "type": typ,
                "entry": entry,
                "cmd_fields": cmd_fields,
                "paired_fields": paired_fields,
            })
            content_layout.addWidget(grp)

        content_layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("OK")
        btn_ok.setStyleSheet(
            "background-color: #2e7d32; color: white; font-weight: bold; padding: 4px 16px;"
        )
        btn_ok.clicked.connect(self._on_ok)
        QShortcut(QKeySequence(Qt.Key.Key_Return), self).activated.connect(self._on_ok)
        QShortcut(QKeySequence(Qt.Key.Key_Enter), self).activated.connect(self._on_ok)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        outer.addLayout(btn_row)

    def _build_entry_group(
        self, alias: str, typ: str,
        entry: Union[MeasurementParamDef, SweepValueDef, WriteCmdDef],
        # These are unused here but kept for future extensibility
    ) -> Tuple[QGroupBox, Dict[str, QLineEdit], Dict[str, QLineEdit]]:

        if typ == "measurement":
            color = "#56d364"
        elif typ == "sweep value":
            color = "#79c0ff"
        else:
            color = "#e3b341"

        grp = QGroupBox(f"[{alias}]  {entry.description}  ({typ})")
        grp.setStyleSheet(f"QGroupBox {{ font-weight: bold; color: {color}; }}")
        form = QFormLayout(grp)
        form.setHorizontalSpacing(12)
        form.setContentsMargins(10, 16, 10, 8)

        cmd_fields: Dict[str, QLineEdit] = {}
        paired_fields: Dict[str, QLineEdit] = {}

        if typ == "measurement":
            old = self._ex_meas.get((alias, entry.description))
            ph_list = list(dict.fromkeys(re.findall(r"\{(\w+)\}", entry.cmd_query)))
            if ph_list:
                cmd_lbl = QLabel(f"cmd_query: {entry.cmd_query}")
                cmd_lbl.setFont(_MONO)
                cmd_lbl.setStyleSheet("color: #888888;")
                form.addRow(cmd_lbl)
                for ph in ph_list:
                    # Pre-fill from saved fill_params, then library params, then empty
                    saved = old.fill_params.get(ph, "") if old else ""
                    default = saved or entry.params.get(ph, "")
                    le = QLineEdit(default)
                    le.setFont(_MONO)
                    le.setPlaceholderText("값 입력")
                    form.addRow(f"  {{{ph}}}:", le)
                    cmd_fields[ph] = le
            else:
                lbl = QLabel(f"cmd: {entry.cmd_query}  (파라미터 없음)")
                lbl.setFont(_MONO)
                lbl.setStyleSheet("color: #555555;")
                form.addRow(lbl)

        elif typ == "sweep value":
            old = self._ex_sweep.get((alias, entry.description))
            sv_ph = list(dict.fromkeys(re.findall(r"\{(\w+)\}", entry.cmd_set)))
            sweep_hint = QLabel(f"cmd_set: {entry.cmd_set}  ← sweep할 파라미터에 [SWEEP] 또는 [SWEEP,SAFETY(a,b)] 입력")
            sweep_hint.setFont(_MONO)
            sweep_hint.setStyleSheet("color: #79c0ff;")
            sweep_hint.setWordWrap(True)
            form.addRow(sweep_hint)
            for ph in sv_ph:
                saved = old.fill_params.get(ph, "") if old else ""
                le = QLineEdit(saved)
                le.setFont(_MONO)
                le.setPlaceholderText("[SWEEP] 또는 고정값")
                form.addRow(f"  {{{ph}}}:", le)
                cmd_fields[ph] = le

            # paired_read
            pr = entry.paired_read
            pr_ph = list(dict.fromkeys(re.findall(r"\{(\w+)\}", pr.cmd_query)))
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("color: #30363d;")
            form.addRow(sep)
            if pr_ph:
                pr_lbl = QLabel(f"paired_read: {pr.cmd_query}")
                pr_lbl.setFont(_MONO)
                pr_lbl.setStyleSheet("color: #56d364;")
                pr_lbl.setWordWrap(True)
                form.addRow(pr_lbl)
                for ph in pr_ph:
                    saved = old.fill_params.get(f"paired.{ph}", "") if old else ""
                    default = saved or pr.params.get(ph, "")
                    le = QLineEdit(default)
                    le.setFont(_MONO)
                    le.setPlaceholderText("값 입력")
                    form.addRow(f"  paired.{{{ph}}}:", le)
                    paired_fields[ph] = le
            else:
                pr_fixed = QLabel(f"paired_read: {pr.resolved_cmd()}  (파라미터 없음)")
                pr_fixed.setFont(_MONO)
                pr_fixed.setStyleSheet("color: #555555;")
                form.addRow(pr_fixed)

        else:  # write
            old = self._ex_write.get((alias, entry.description))
            w_ph = list(dict.fromkeys(re.findall(r"\{(\w+)\}", entry.cmd_set)))
            if w_ph:
                w_hint = QLabel(
                    f"cmd_set: {entry.cmd_set}  ← sweep할 파라미터에 [SWEEP] 입력 (선택)"
                )
                w_hint.setFont(_MONO)
                w_hint.setStyleSheet("color: #e3b341;")
                w_hint.setWordWrap(True)
                form.addRow(w_hint)
                for ph in w_ph:
                    saved = old.fill_params.get(ph, "") if old else ""
                    le = QLineEdit(saved)
                    le.setFont(_MONO)
                    le.setPlaceholderText("[SWEEP] 또는 고정값")
                    form.addRow(f"  {{{ph}}}:", le)
                    cmd_fields[ph] = le
            else:
                lbl = QLabel(f"cmd: {entry.cmd_set}  (파라미터 없음)")
                lbl.setFont(_MONO)
                lbl.setStyleSheet("color: #555555;")
                form.addRow(lbl)

        return grp, cmd_fields, paired_fields

    def _on_ok(self):
        measurements: List[InstantiatedMeasurement] = []
        sweep_values: List[InstantiatedSweepValue]  = []
        write_cmds:   List[InstantiatedWriteCmd]    = []

        for form in self._forms:
            alias       = form["alias"]
            typ         = form["type"]
            entry       = form["entry"]
            cmd_fields  = form["cmd_fields"]
            paired_flds = form["paired_fields"]
            tag = f"[{alias}] {entry.description}"

            # ── 빈 필드 검사 ──────────────────────────────────────────
            for ph, le in {**cmd_fields, **paired_flds}.items():
                if not le.text().strip():
                    QMessageBox.warning(
                        self, "입력 오류",
                        f"{tag}: 파라미터 {{{ph}}} 값을 입력하세요."
                    )
                    return

            if typ == "measurement":
                if cmd_fields:
                    try:
                        resolved = entry.cmd_query.format(
                            **{k: v.text().strip() for k, v in cmd_fields.items()}
                        )
                    except KeyError as e:
                        QMessageBox.warning(self, "오류", f"{tag}: 파라미터 오류 {e}")
                        return
                else:
                    resolved = entry.cmd_query
                measurements.append(InstantiatedMeasurement(
                    alias=alias,
                    description=entry.description,
                    resolved_cmd=resolved,
                    figure_axis=entry.figure_axis,
                    unit=entry.unit,
                    fill_params={k: v.text().strip() for k, v in cmd_fields.items()},
                ))

            elif typ == "sweep value":
                _safety_re = re.compile(
                    r"^\[SWEEP(?:,SAFETY\((\d+),(\d+(?:\.\d+)?)\))?\]$"
                )
                sweep_keys = [
                    k for k, le in cmd_fields.items()
                    if _safety_re.match(le.text().strip())
                ]
                if len(sweep_keys) == 0:
                    QMessageBox.warning(
                        self, "입력 오류",
                        f"{tag}: cmd_set에 [SWEEP] 파라미터가 없습니다.\n"
                        "정확히 하나의 파라미터에 [SWEEP] 또는 [SWEEP,SAFETY(a,b)]을 입력하세요."
                    )
                    return
                if len(sweep_keys) > 1:
                    QMessageBox.warning(
                        self, "입력 오류",
                        f"{tag}: [SWEEP] 파라미터가 {len(sweep_keys)}개입니다.\n"
                        "정확히 하나만 지정하세요."
                    )
                    return

                sweep_key = sweep_keys[0]
                sweep_text = cmd_fields[sweep_key].text().strip()
                m = _safety_re.match(sweep_text)
                if m.group(1):
                    safety_steps = int(m.group(1))
                    safety_interval_ms = float(m.group(2))
                    if safety_steps < 1:
                        QMessageBox.warning(
                            self, "입력 오류",
                            f"{tag}: SAFETY 단계 수(a)는 1 이상이어야 합니다."
                        )
                        return
                else:
                    safety_steps = 0
                    safety_interval_ms = 0.0

                cmd_set = entry.cmd_set
                for k, le in cmd_fields.items():
                    if k == sweep_key:
                        cmd_set = cmd_set.replace(f"{{{k}}}", "{v}")
                    else:
                        cmd_set = cmd_set.replace(f"{{{k}}}", le.text().strip())

                if paired_flds:
                    try:
                        paired_cmd = entry.paired_read.cmd_query.format(
                            **{k: v.text().strip() for k, v in paired_flds.items()}
                        )
                    except KeyError as e:
                        QMessageBox.warning(self, "오류", f"{tag}: paired_read 파라미터 오류 {e}")
                        return
                else:
                    paired_cmd = entry.paired_read.resolved_cmd()

                # fill_params: cmd placeholders as entered, paired read params prefixed with "paired."
                fill = {k: le.text().strip() for k, le in cmd_fields.items()}
                fill.update({f"paired.{k}": le.text().strip() for k, le in paired_flds.items()})
                sweep_values.append(InstantiatedSweepValue(
                    alias=alias,
                    description=entry.description,
                    cmd_set=cmd_set,
                    paired_read_cmd=paired_cmd,
                    figure_axis=entry.figure_axis,
                    unit=entry.unit,
                    safety_steps=safety_steps,
                    safety_interval_ms=safety_interval_ms,
                    fill_params=fill,
                ))

            else:  # write
                sweep_keys = [k for k, v in cmd_fields.items() if v.text().strip() == "[SWEEP]"]
                if len(sweep_keys) > 1:
                    QMessageBox.warning(
                        self, "입력 오류",
                        f"{tag}: [SWEEP] 파라미터가 {len(sweep_keys)}개입니다.\n"
                        "최대 하나만 지정하세요."
                    )
                    return

                cmd_set = entry.cmd_set
                for k, le in cmd_fields.items():
                    val = le.text().strip()
                    if val == "[SWEEP]":
                        cmd_set = cmd_set.replace(f"{{{k}}}", "{v}")
                    else:
                        cmd_set = cmd_set.replace(f"{{{k}}}", val)

                write_cmds.append(InstantiatedWriteCmd(
                    alias=alias,
                    description=entry.description,
                    cmd_set=cmd_set,
                    figure_axis=entry.figure_axis,
                    unit=entry.unit,
                    fill_params={k: le.text().strip() for k, le in cmd_fields.items()},
                ))

        self._result = MainUIProfile(
            sweep_values=sweep_values,
            measurements=measurements,
            write_cmds=write_cmds,
        )
        self.accept()

    def get_profile(self) -> Optional[MainUIProfile]:
        return self._result


# ===========================================================================
# SecondParamFillDialog — second sweep channel의 advance 파라미터를 채우는 다이얼로그
# ===========================================================================

_ADVANCE_LABELS = {
    SecondSweepAdvanceType.SIMPLE_HOP:    "Simple Hop (즉시 이동)",
    SecondSweepAdvanceType.SWEEP:         "Sweep (점진적 이동)",
    SecondSweepAdvanceType.FEEDBACK:      "Feedback (피드백 대기)",
    SecondSweepAdvanceType.WAIT_FOR_TIME: "Wait for Time (시간 대기)",
}


class SecondParamFillDialog(QDialog):
    """
    Second sweep channel 항목의 advance type과 관련 파라미터를 입력하는 다이얼로그.
    source_type == 'write_cmd'이면 SWEEP / FEEDBACK 옵션이 비활성화됩니다.
    """

    def __init__(
        self,
        entries: List[Tuple[str, Union[SweepValueDef, WriteCmdDef], str]],  # (alias, entry, source_type)
        existing: List[InstantiatedSecondSweepChannel],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Fill Second Sweep Channel Parameters")
        self.setMinimumWidth(520)
        self.resize(520, 480)

        self._entries = entries
        self._existing = {(c.alias, c.description): c for c in existing}
        self._forms: List[dict] = []
        self._result: Optional[List[InstantiatedSecondSweepChannel]] = None

        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setSpacing(10)
        cl.setContentsMargins(0, 0, 0, 0)

        for alias, entry, source_type in self._entries:
            old = self._existing.get((alias, entry.description))
            grp, form_data = self._build_entry_group(alias, entry, source_type, old)
            self._forms.append(form_data)
            cl.addWidget(grp)

        cl.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("OK")
        btn_ok.setStyleSheet(
            "background-color: #2e7d32; color: white; font-weight: bold; padding: 4px 16px;"
        )
        btn_ok.clicked.connect(self._on_ok)
        QShortcut(QKeySequence(Qt.Key.Key_Return), self).activated.connect(self._on_ok)
        QShortcut(QKeySequence(Qt.Key.Key_Enter), self).activated.connect(self._on_ok)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        outer.addLayout(btn_row)

    def _build_entry_group(self, alias, entry, source_type, old):
        color = "#79c0ff" if source_type == "sweep_value" else "#e3b341"
        grp = QGroupBox(f"[{alias}]  {entry.description}  (second sweep)")
        grp.setStyleSheet(f"QGroupBox {{ font-weight: bold; color: {color}; }}")
        form = QFormLayout(grp)
        form.setHorizontalSpacing(12)
        form.setContentsMargins(10, 16, 10, 8)

        # cmd_set display (read-only)
        cmd_lbl = QLabel(entry.cmd_set)
        cmd_lbl.setFont(_MONO)
        cmd_lbl.setStyleSheet("color: #888888;")
        form.addRow("cmd_set:", cmd_lbl)

        # Advance Type combobox
        combo = QComboBox()
        for at, label in _ADVANCE_LABELS.items():
            combo.addItem(label, at)
            # disable SWEEP / FEEDBACK for write_cmd sources
            if source_type == "write_cmd" and at in (
                SecondSweepAdvanceType.SWEEP, SecondSweepAdvanceType.FEEDBACK
            ):
                idx = combo.count() - 1
                item = combo.model().item(idx)
                if item:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)

        default_at = old.advance_type if old else SecondSweepAdvanceType.SIMPLE_HOP
        for i in range(combo.count()):
            if combo.itemData(i) == default_at:
                combo.setCurrentIndex(i)
                break
        form.addRow("Advance Type:", combo)

        # SWEEP fields
        sweep_widget = QWidget()
        sweep_form = QFormLayout(sweep_widget)
        sweep_form.setContentsMargins(0, 0, 0, 0)
        info_lbl = QLabel("Rate / Safety 설정은 Double Sweep 창에서 조정합니다.")
        info_lbl.setStyleSheet("color: #888888; font-size: 11px;")
        sweep_form.addRow("", info_lbl)

        # FEEDBACK fields
        feedback_widget = QWidget()
        feedback_form = QFormLayout(feedback_widget)
        feedback_form.setContentsMargins(0, 0, 0, 0)
        le_fb_cmd = QLineEdit(old.feedback_read_cmd if old else "")
        le_fb_cmd.setFont(_MONO)
        le_fb_cmd.setPlaceholderText("e.g. print(smua.measure.v())")
        le_fb_poll = QLineEdit(str(old.feedback_poll_interval if old else 1.0))
        le_fb_poll.setFont(_MONO)
        le_fb_tol = QLineEdit(str(old.feedback_tolerance_pct if old else 95.0))
        le_fb_tol.setFont(_MONO)
        feedback_form.addRow("Read Command:", le_fb_cmd)
        feedback_form.addRow("Poll Interval (sec):", le_fb_poll)
        feedback_form.addRow("Tolerance (%):", le_fb_tol)

        # WAIT_FOR_TIME fields
        wait_widget = QWidget()
        wait_form = QFormLayout(wait_widget)
        wait_form.setContentsMargins(0, 0, 0, 0)
        le_wait = QLineEdit(str(old.wait_time if old else 1.0))
        le_wait.setFont(_MONO)
        wait_form.addRow("Wait Time (sec):", le_wait)

        form.addRow(sweep_widget)
        form.addRow(feedback_widget)
        form.addRow(wait_widget)

        def _update_visibility(index):
            at = combo.itemData(index)
            sweep_widget.setVisible(at == SecondSweepAdvanceType.SWEEP)
            feedback_widget.setVisible(at == SecondSweepAdvanceType.FEEDBACK)
            wait_widget.setVisible(at == SecondSweepAdvanceType.WAIT_FOR_TIME)

        combo.currentIndexChanged.connect(_update_visibility)
        _update_visibility(combo.currentIndex())

        form_data = {
            "alias": alias,
            "entry": entry,
            "source_type": source_type,
            "combo": combo,
            "le_fb_cmd": le_fb_cmd,
            "le_fb_poll": le_fb_poll,
            "le_fb_tol": le_fb_tol,
            "le_wait": le_wait,
        }
        return grp, form_data

    def _on_ok(self):
        result: List[InstantiatedSecondSweepChannel] = []
        for fd in self._forms:
            alias = fd["alias"]
            entry = fd["entry"]
            source_type = fd["source_type"]
            at: SecondSweepAdvanceType = fd["combo"].currentData()
            tag = f"[{alias}] {entry.description}"

            # Build cmd_set with {v}
            lib_phs = re.findall(r"\{(\w+)\}", entry.cmd_set)
            if len(lib_phs) == 1:
                cmd_set = entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}")
            else:
                cmd_set = entry.cmd_set  # already has {v} or no placeholder

            paired_read_cmd = ""
            if source_type == "sweep_value":
                paired_read_cmd = entry.paired_read.resolved_cmd()

            if at == SecondSweepAdvanceType.SWEEP:
                # sweep_rate is managed in Double Sweep window — store default 1.0
                result.append(InstantiatedSecondSweepChannel(
                    alias=alias, description=entry.description,
                    source_type=source_type, advance_type=at, cmd_set=cmd_set,
                    paired_read_cmd=paired_read_cmd,
                    sweep_rate=1.0,
                    figure_axis=entry.figure_axis, unit=entry.unit,
                ))
            elif at == SecondSweepAdvanceType.FEEDBACK:
                fb_cmd = fd["le_fb_cmd"].text().strip()
                if not fb_cmd:
                    QMessageBox.warning(self, "입력 오류", f"{tag}: Read Command를 입력하세요.")
                    return
                try:
                    fb_poll = float(fd["le_fb_poll"].text().strip())
                    fb_tol = float(fd["le_fb_tol"].text().strip())
                except ValueError:
                    QMessageBox.warning(self, "입력 오류", f"{tag}: Feedback 파라미터 형식 오류")
                    return
                result.append(InstantiatedSecondSweepChannel(
                    alias=alias, description=entry.description,
                    source_type=source_type, advance_type=at, cmd_set=cmd_set,
                    paired_read_cmd=paired_read_cmd,
                    feedback_read_cmd=fb_cmd,
                    feedback_poll_interval=fb_poll,
                    feedback_tolerance_pct=fb_tol,
                    figure_axis=entry.figure_axis, unit=entry.unit,
                ))
            elif at == SecondSweepAdvanceType.WAIT_FOR_TIME:
                try:
                    wait_time = float(fd["le_wait"].text().strip())
                except ValueError:
                    QMessageBox.warning(self, "입력 오류", f"{tag}: Wait Time 형식 오류")
                    return
                result.append(InstantiatedSecondSweepChannel(
                    alias=alias, description=entry.description,
                    source_type=source_type, advance_type=at, cmd_set=cmd_set,
                    paired_read_cmd=paired_read_cmd,
                    wait_time=wait_time,
                    figure_axis=entry.figure_axis, unit=entry.unit,
                ))
            else:  # SIMPLE_HOP
                result.append(InstantiatedSecondSweepChannel(
                    alias=alias, description=entry.description,
                    source_type=source_type, advance_type=at, cmd_set=cmd_set,
                    paired_read_cmd=paired_read_cmd,
                    figure_axis=entry.figure_axis, unit=entry.unit,
                ))

        self._result = result
        self.accept()

    def get_channels(self) -> Optional[List[InstantiatedSecondSweepChannel]]:
        return self._result


# ===========================================================================
# ParameterManagerWindow
# ===========================================================================

class ParameterManagerWindow(QDialog):
    """
    Parameter Manager — VISA 라이브러리에서 현재 실험에 사용할 항목을 선택합니다.
    선택 후 Apply → ParamFillDialog에서 파라미터 채우기 → Main UI에 등록.
    """

    selection_applied = Signal(MainUIProfile)

    def __init__(
        self,
        lib_registry: VisaLibraryRegistry,
        param_reg: ParameterManagerRegistry,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Parameter Manager")
        self.resize(800, 620)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._lib_reg   = lib_registry
        self._param_reg = param_reg

        # Row data per section: (alias, entry_object)
        self._sweep_rows:  List[Tuple[str, SweepValueDef]]       = []
        self._meas_rows:   List[Tuple[str, MeasurementParamDef]] = []
        # second sweep: (alias, entry, source_type)
        self._second_rows: List[Tuple[str, Union[SweepValueDef, WriteCmdDef], str]] = []

        self._build_ui()
        self._populate_sections()
        self._restore_selection()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # Instrument filter
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Instrument filter:"))
        self._combo_filter = QComboBox()
        self._combo_filter.setMinimumWidth(130)
        self._combo_filter.currentTextChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._combo_filter)
        filter_row.addStretch()
        outer.addLayout(filter_row)

        # Scroll area with three group boxes
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(10)
        content_layout.setContentsMargins(0, 0, 0, 0)

        grp_sweep = QGroupBox("Sweep Value")
        grp_sweep.setStyleSheet("QGroupBox { font-weight: bold; color: #79c0ff; }")
        sv_layout = QVBoxLayout(grp_sweep)
        self._tbl_sweep = self._make_table()
        sv_layout.addWidget(self._tbl_sweep)
        content_layout.addWidget(grp_sweep)

        grp_meas = QGroupBox("Measurement Parameter")
        grp_meas.setStyleSheet("QGroupBox { font-weight: bold; color: #56d364; }")
        m_layout = QVBoxLayout(grp_meas)
        self._tbl_meas = self._make_table()
        m_layout.addWidget(self._tbl_meas)
        content_layout.addWidget(grp_meas)

        grp_second = QGroupBox("Second Sweep Channel")
        grp_second.setStyleSheet("QGroupBox { font-weight: bold; color: #f78166; }")
        s_layout = QVBoxLayout(grp_second)
        s_info = QLabel("Sweep Value / Write Command 항목 중 second sweep channel로 사용할 항목을 선택합니다.")
        s_info.setStyleSheet("color: #888888; font-size: 11px;")
        s_info.setWordWrap(True)
        s_layout.addWidget(s_info)
        self._tbl_second = self._make_table()
        s_layout.addWidget(self._tbl_second)
        content_layout.addWidget(grp_second)

        content_layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

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

    def _make_table(self) -> QTableWidget:
        tbl = QTableWidget()
        tbl.setColumnCount(len(_HDR))
        tbl.setHorizontalHeaderLabels(_HDR)
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        tbl.setColumnWidth(0, 32)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tbl.setFont(_MONO)
        tbl.setStyleSheet(
            "background-color: #0d1117; color: #c9d1d9; gridline-color: #21262d;"
        )
        tbl.verticalHeader().setVisible(False)
        return tbl

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def _populate_filter_combo(self):
        aliases = sorted(self._lib_reg.list_aliases())
        self._combo_filter.blockSignals(True)
        prev = self._combo_filter.currentText()
        self._combo_filter.clear()
        self._combo_filter.addItem("All")
        for a in aliases:
            self._combo_filter.addItem(a)
        self._combo_filter.blockSignals(False)
        idx = self._combo_filter.findText(prev)
        self._combo_filter.setCurrentIndex(max(idx, 0))

    def _populate_sections(self, filter_alias: str = "All"):
        self._populate_filter_combo()
        self._sweep_rows.clear()
        self._meas_rows.clear()
        self._second_rows.clear()

        aliases = self._lib_reg.list_aliases()
        if filter_alias != "All":
            aliases = [a for a in aliases if a == filter_alias]

        for alias in aliases:
            lib = self._lib_reg.get_library(alias)
            for e in lib.sweep_values:
                self._sweep_rows.append((alias, e))
            for e in lib.measurements:
                self._meas_rows.append((alias, e))
            # second sweep: sweep_values + write_cmds combined
            for e in lib.sweep_values:
                self._second_rows.append((alias, e, "sweep_value"))
            for e in lib.write_cmds:
                self._second_rows.append((alias, e, "write_cmd"))

        self._fill_table(self._tbl_sweep,  self._sweep_rows)
        self._fill_table(self._tbl_meas,   self._meas_rows)
        self._fill_second_table()

    def _fill_table(self, tbl: QTableWidget, rows):
        tbl.setRowCount(len(rows))
        for row_idx, (alias, entry) in enumerate(rows):
            cb = QCheckBox()
            container = QWidget()
            h = QHBoxLayout(container)
            h.addWidget(cb)
            h.setAlignment(Qt.AlignmentFlag.AlignCenter)
            h.setContentsMargins(0, 0, 0, 0)
            tbl.setCellWidget(row_idx, 0, container)

            self._set_item(tbl, row_idx, 1, alias)
            self._set_item(tbl, row_idx, 2, entry.description)

            if hasattr(entry, "cmd_query"):
                cmd = entry.resolved_cmd() if entry.params else entry.cmd_query
            else:
                cmd = entry.cmd_set
            self._set_item(tbl, row_idx, 3, cmd)
            self._set_item(tbl, row_idx, 4, entry.unit)

        tbl.resizeRowsToContents()

    def _fill_second_table(self):
        tbl = self._tbl_second
        tbl.setRowCount(len(self._second_rows))
        for row_idx, (alias, entry, source_type) in enumerate(self._second_rows):
            cb = QCheckBox()
            container = QWidget()
            h = QHBoxLayout(container)
            h.addWidget(cb)
            h.setAlignment(Qt.AlignmentFlag.AlignCenter)
            h.setContentsMargins(0, 0, 0, 0)
            tbl.setCellWidget(row_idx, 0, container)

            self._set_item(tbl, row_idx, 1, alias)
            self._set_item(tbl, row_idx, 2, entry.description)
            self._set_item(tbl, row_idx, 3, entry.cmd_set)
            self._set_item(tbl, row_idx, 4, entry.unit)

            # Color-code by source type
            color = QColor("#79c0ff") if source_type == "sweep_value" else QColor("#e3b341")
            for col in range(1, 5):
                item = tbl.item(row_idx, col)
                if item:
                    item.setForeground(color)

        tbl.resizeRowsToContents()

    def _set_item(self, tbl, row, col, text):
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        tbl.setItem(row, col, item)

    # ------------------------------------------------------------------
    # Restore saved selection
    # ------------------------------------------------------------------

    def _restore_selection(self):
        sel = self._param_reg.selection

        sel_sweep = {(s.alias, s.description) for s in sel.sweep_values}
        for row_idx, (alias, entry) in enumerate(self._sweep_rows):
            if (alias, entry.description) in sel_sweep:
                self._check_row(self._tbl_sweep, row_idx, True)

        sel_meas = {(s.alias, s.description) for s in sel.measurements}
        for row_idx, (alias, entry) in enumerate(self._meas_rows):
            if (alias, entry.description) in sel_meas:
                self._check_row(self._tbl_meas, row_idx, True)

        sel_second = {(s.alias, s.description) for s in sel.second_sweep_channels}
        for row_idx, (alias, entry, _) in enumerate(self._second_rows):
            if (alias, entry.description) in sel_second:
                self._check_row(self._tbl_second, row_idx, True)

    def _check_row(self, tbl: QTableWidget, row: int, checked: bool):
        widget = tbl.cellWidget(row, 0)
        if widget:
            cb = widget.findChild(QCheckBox)
            if cb:
                cb.setChecked(checked)

    def _is_row_checked(self, tbl: QTableWidget, row: int) -> bool:
        widget = tbl.cellWidget(row, 0)
        if widget:
            cb = widget.findChild(QCheckBox)
            if cb:
                return cb.isChecked()
        return False

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def _on_apply(self):
        # Collect standard entries
        selected_entries: List[Tuple[str, str, Union[MeasurementParamDef, SweepValueDef, WriteCmdDef]]] = []

        for row_idx, (alias, entry) in enumerate(self._sweep_rows):
            if self._is_row_checked(self._tbl_sweep, row_idx):
                selected_entries.append((alias, "sweep value", entry))

        for row_idx, (alias, entry) in enumerate(self._meas_rows):
            if self._is_row_checked(self._tbl_meas, row_idx):
                selected_entries.append((alias, "measurement", entry))

        # Collect second sweep channel entries
        second_entries: List[Tuple[str, Union[SweepValueDef, WriteCmdDef], str]] = []
        for row_idx, (alias, entry, source_type) in enumerate(self._second_rows):
            if self._is_row_checked(self._tbl_second, row_idx):
                second_entries.append((alias, entry, source_type))

        if not selected_entries and not second_entries:
            QMessageBox.warning(self, "선택 없음", "최소 하나 이상의 항목을 선택하세요.")
            return

        # Save selection state
        sel = ParameterManagerProfile(
            sweep_values=[
                SelectedEntry(alias=a, description=e.description)
                for a, t, e in selected_entries if t == "sweep value"
            ],
            measurements=[
                SelectedEntry(alias=a, description=e.description)
                for a, t, e in selected_entries if t == "measurement"
            ],
            second_sweep_channels=[
                SelectedEntry(alias=a, description=e.description)
                for a, e, _ in second_entries
            ],
        )
        self._param_reg.save_selection(sel)

        # Standard entries: open ParamFillDialog (pre-filled from existing profile)
        if selected_entries:
            dlg = ParamFillDialog(selected_entries, self._param_reg.main_ui_profile, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            profile = dlg.get_profile()
            if profile is None:
                return
        else:
            existing = self._param_reg.main_ui_profile
            profile = MainUIProfile(
                sweep_values=existing.sweep_values,
                measurements=existing.measurements,
            )

        # Second sweep channels: open SecondParamFillDialog
        second_channels: List[InstantiatedSecondSweepChannel] = []
        if second_entries:
            dlg2 = SecondParamFillDialog(
                second_entries,
                self._param_reg.main_ui_profile.second_sweep_channels,
                self,
            )
            if dlg2.exec() != QDialog.DialogCode.Accepted:
                return
            chs = dlg2.get_channels()
            if chs is not None:
                second_channels = chs

        final_profile = MainUIProfile(
            sweep_values=profile.sweep_values,
            measurements=profile.measurements,
            write_cmds=profile.write_cmds,
            second_sweep_channels=second_channels,
        )
        self._param_reg.save_main_ui(final_profile)
        self.selection_applied.emit(final_profile)
        self.hide()

    # ------------------------------------------------------------------

    def _on_filter_changed(self, alias: str):
        self._populate_sections(alias)
        self._restore_selection()

    def showEvent(self, event):
        self._populate_sections(self._combo_filter.currentText())
        self._restore_selection()
        super().showEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
