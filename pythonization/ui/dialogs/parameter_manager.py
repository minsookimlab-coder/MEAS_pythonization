"""
Parameter Manager — Add/Delete/Copy based UI for building the MainUIProfile.

Each section (Sweep Values, Measurements, Second Sweep Channels) has an
independent table of instantiated entries with Add/Edit/Delete/Copy/Up/Down
buttons.  An Apply button saves the profile and emits selection_applied.
"""
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHBoxLayout as _QHBox,
    QHeaderView,
    QLabel,
    QLabel as _QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from pythonization.config.models import (
    InstantiatedMeasurement,
    InstantiatedSecondSweepChannel,
    InstantiatedSweepValue,
    InstantiatedWriteCmd,
    MainUIProfile,
    MeasurementParamDef,
    MetaDataConfig,
    MetaDataEntry,
    SecondSweepAdvanceType,
    SweepValueDef,
    WriteCmdDef,
)
from pythonization.instruments.command_library import VisaLibraryRegistry
from pythonization.profiles.registry import ProfileRegistry
from pythonization.ui.widgets.help_button import make_help_button

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

#: sweep 축으로 지정한 placeholder 자리에 넣어 두는 표식.
#: 프로파일에 이대로 저장되어, 나중에 다시 편집할 때 어느 자리가 축이었는지 복원한다.
_SWEEP_MARKER = "[SWEEP]"


class _ValidationError(Exception):
    """입력이 유효하지 않다 — _on_ok 가 잡아서 경고 창으로 보여 준다."""

    def __init__(self, message: str, title: str = "Validation"):
        super().__init__(message)
        self.title = title


@dataclass
class _EntryMeta:
    """섹션 종류와 무관하게 공통으로 들어가는 필드."""
    alias: str
    description: str
    figure_axis: str
    unit: str


def _fill_command(template: str, filled: dict, sweep_placeholder: Optional[str]) -> str:
    """명령 템플릿의 placeholder 를 채운다.

    sweep 축으로 지정한 자리는 `{v}` 로 남긴다 — 측정 중 매 스텝의 값이 여기 들어간다.
    나머지는 사용자가 입력한 고정값으로 바꾼다.
    """
    result = template
    for name, value in filled.items():
        replacement = "{v}" if name == sweep_placeholder else value
        result = result.replace(f"{{{name}}}", replacement)
    return result


def _fill_read_command(template: str, filled: dict) -> str:
    """읽기 명령에는 sweep 축이 없다 — 모든 placeholder 를 입력값 그대로 채운다."""
    result = template
    for name, value in filled.items():
        result = result.replace(f"{{{name}}}", value)
    return result


class AddEntryDialog(QDialog):
    """
    Dialog for adding a new instantiated entry to one of the PM sections.

    section_type: 'sweep' | 'measurement' | 'write' | 'second'
    """

    @staticmethod
    def _advance_help_html() -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>Advance Type — 2차 축(자기장 등)을 '어떻게' 옮길지</b><hr>"
            "Double Sweep에서 2차 축 값을 다음 칸으로 옮기는 방식입니다. "
            "값의 성질(즉시 적용되는지, 도달에 시간이 걸리는지)에 맞게 고르세요.<hr>"

            "<b>Simple Hop — 즉시 한 번에</b><br>"
            "값을 그냥 한 번 보내고 바로 다음 단계로 갑니다. 가장 단순.<br>"
            "&nbsp;&nbsp;적합: 게이트 전압처럼 <b>보내면 바로 적용</b>되는 값.<hr>"

            "<b>Sweep — 정해진 속도로 천천히</b><br>"
            "목표값까지 일정 속도(rate)로 조금씩 이동합니다(이동 중 측정은 안 함). "
            "급격한 변화로 인한 충격·스파이크를 피합니다.<br>"
            "&nbsp;&nbsp;설정: Sweep Rate, (선택) Safety 단계.<br>"
            "&nbsp;&nbsp;적합: 큰 전압 변화 등 <b>천천히 바꿔야 하는</b> 값.<hr>"

            "<b>Feedback — 도달·안정될 때까지 기다림</b><br>"
            "값을 보낸 뒤, 실제 값을 반복해 읽어 <b>목표에 도달하고 출렁임이 잦아들 때까지</b> "
            "기다린 뒤 다음으로 갑니다.<br>"
            "&nbsp;&nbsp;설정: 실제값을 읽는 명령(Feedback Read Cmd), 읽는 간격(Poll), "
            "도달 판정 기준(Tolerance %), (선택) 안정 판정.<br>"
            "&nbsp;&nbsp;적합: <b>자기장</b>처럼 명령을 줘도 실제 도달까지 시간이 걸리고 흔들리는 값.<br>"
            "&nbsp;&nbsp;※ Tolerance·안정 판정 세부값 권장치는 Double Sweep 창의 FEEDBACK 설정 "
            "도움말(?)에 있습니다.<hr>"

            "<b>Wait for Time — 정해진 시간만큼 기다림</b><br>"
            "값을 보낸 뒤 입력한 시간(초)만큼 무조건 기다린 다음 진행합니다.<br>"
            "&nbsp;&nbsp;적합: 안정에 걸리는 시간이 <b>대략 일정</b>해서 시간만 정해두면 되는 경우. "
            "Feedback보다 설정이 간단합니다.<hr>"

            "<small>요약: 즉시 적용=Simple Hop, 천천히=Sweep, 도달 확인 필요=Feedback, "
            "시간만 기다림=Wait for Time</small>"
            "</body></html>"
        )

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
        """섹션 타입에 따라 구획을 켜고 끈다.

        sweep   → Safety Ramp 구획
        second  → Second Sweep Channel 구획 (advance type 별 하위 필드 포함)
        나머지  → 둘 다 숨김
        위젯은 항상 모두 만들어 두고 보이기만 조절한다 — 타입이 바뀔 일이 없어서
        조건부로 만들면 나중에 참조할 때 AttributeError 가 나기 쉽다.
        """
        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        aliases = self._lib_reg.list_aliases()
        outer.addLayout(self._build_selection_form(aliases))
        outer.addWidget(self._hline())
        outer.addWidget(self._build_command_preview())
        outer.addWidget(self._build_placeholder_area())

        if self._section_type in ('sweep', 'second'):
            note = QLabel(
                "For sweep entries: exactly one placeholder must be set to [SWEEP].")
            note.setStyleSheet("color: #79c0ff; font-size: 11px;")
            note.setWordWrap(True)
            outer.addWidget(note)

        outer.addWidget(self._hline())
        outer.addLayout(self._build_metadata_form())
        outer.addWidget(self._build_safety_section())
        outer.addWidget(self._build_second_section())
        outer.addLayout(self._build_button_row())

        self._connect_signals()

        if aliases:
            self._refresh_entry_combo(aliases[0])
            self._refresh_fb_cmd_combo(aliases[0])
        self._update_advance_visibility(0)

    @staticmethod
    def _hline() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #30363d;")
        return line

    def _build_selection_form(self, aliases) -> QFormLayout:
        """장비 + 라이브러리 항목 선택."""
        form = QFormLayout()
        form.setHorizontalSpacing(12)

        self._combo_alias = QComboBox()
        for alias in aliases:
            self._combo_alias.addItem(alias)
        form.addRow("Instrument:", self._combo_alias)

        self._combo_entry = QComboBox()
        self._combo_entry.setFont(_MONO)
        form.addRow("Library entry:", self._combo_entry)
        return form

    def _build_command_preview(self) -> QLabel:
        """플레이스홀더를 채운 결과 명령을 미리 보여 준다."""
        self._lbl_cmd_preview = QLabel("")
        self._lbl_cmd_preview.setFont(_MONO)
        self._lbl_cmd_preview.setStyleSheet("color: #888888;")
        self._lbl_cmd_preview.setWordWrap(True)
        return self._lbl_cmd_preview

    def _build_placeholder_area(self) -> QWidget:
        """선택한 명령의 `{이름}` 개수만큼 _refresh_placeholders 가 채우는 빈 영역."""
        self._ph_widget = QWidget()
        self._ph_layout = QFormLayout(self._ph_widget)
        self._ph_layout.setHorizontalSpacing(12)
        self._ph_layout.setContentsMargins(0, 4, 0, 4)
        return self._ph_widget

    def _build_metadata_form(self) -> QFormLayout:
        """설명·그래프 축 이름·단위. 사용자가 직접 고치면 자동 갱신을 멈춘다."""
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self._le_desc = QLineEdit()
        self._le_axis = QLineEdit()
        self._le_unit = QLineEdit()
        form.addRow("Description:", self._le_desc)
        form.addRow("Figure Axis:", self._le_axis)
        form.addRow("Unit:", self._le_unit)
        return form

    def _build_safety_section(self) -> QWidget:
        """Safety Ramp — 목표값까지 나눠서 천천히 이동 (sweep 섹션 전용)."""
        self._safety_widget = QWidget()
        layout = QVBoxLayout(self._safety_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._hline())

        self._cb_safety = QCheckBox("Safety Ramp")
        self._cb_safety.setToolTip(
            "목표값으로 바로 이동하지 않고 지정된 스텝 수만큼\n"
            "균등 분할하여 천천히 이동합니다."
        )
        self._cb_safety.setStyleSheet("font-weight: bold; color: #ffa657;")
        layout.addWidget(self._cb_safety)

        self._safety_detail = QWidget()
        detail = QFormLayout(self._safety_detail)
        detail.setContentsMargins(12, 0, 0, 0)
        detail.setHorizontalSpacing(12)
        detail.setVerticalSpacing(4)

        self._sb_safety_steps = QSpinBox()
        self._sb_safety_steps.setRange(1, 10000)
        self._sb_safety_steps.setValue(10)
        self._sb_safety_steps.setToolTip(
            "목표값까지 나눌 중간 스텝 수 (예: 10 → 10번에 나눠 이동)")
        detail.addRow("Steps:", self._sb_safety_steps)

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
        detail.addRow("Interval:", self._sb_safety_interval)
        layout.addWidget(self._safety_detail)

        self._safety_detail.setVisible(False)   # 체크하기 전까지 접어 둔다
        self._safety_widget.setVisible(self._section_type == 'sweep')
        return self._safety_widget

    def _build_second_section(self) -> QWidget:
        """Second Sweep Channel — advance type 과 그에 딸린 필드들 (second 섹션 전용)."""
        self._second_widget = QWidget()
        layout = QVBoxLayout(self._second_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._hline())

        title = QLabel("Second Sweep Channel Settings")
        title.setStyleSheet("font-weight: bold; color: #f78166;")
        layout.addWidget(title)

        # Source Type(sweep value / write command)은 선택한 라이브러리 명령의 종류로
        # 자동 판별하므로 별도 선택 UI를 두지 않는다. (_current_source_type 참고)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self._combo_advance = QComboBox()
        for advance_type, label in _ADVANCE_LABELS.items():
            self._combo_advance.addItem(label, advance_type)

        advance_row = QHBoxLayout()
        advance_row.setContentsMargins(0, 0, 0, 0)
        advance_row.addWidget(self._combo_advance, stretch=1)
        advance_row.addWidget(make_help_button(self._advance_help_html(),
                                               "Advance Type 도움말"))
        advance_container = QWidget()
        advance_container.setLayout(advance_row)
        form.addRow("Advance Type:", advance_container)
        layout.addLayout(form)

        layout.addWidget(self._build_feedback_fields())
        layout.addWidget(self._build_wait_fields())

        self._second_widget.setVisible(self._section_type == 'second')
        return self._second_widget

    def _build_feedback_fields(self) -> QWidget:
        """FEEDBACK advance type 전용 필드.

        판정이 2단계다: Tolerance 로 목표 도달을 보고, 그 뒤 Std Window 로 안정화를 본다.
        """
        self._fb_widget = QWidget()
        form = QFormLayout(self._fb_widget)
        form.setContentsMargins(0, 0, 0, 0)

        self._combo_fb_cmd = QComboBox()
        self._combo_fb_cmd.setFont(_MONO)
        self._combo_fb_cmd.setMinimumWidth(200)
        self._le_fb_poll = QLineEdit("1.0")
        self._le_fb_tol = QLineEdit("95.0")
        form.addRow("Feedback Read Cmd:", self._combo_fb_cmd)
        form.addRow("Poll Interval (s):", self._le_fb_poll)
        form.addRow("Tolerance (%):", self._le_fb_tol)

        form.addRow(self._hline())
        stability_label = QLabel("Stability Check (after threshold):")
        stability_label.setStyleSheet("color: #79c0ff; font-size: 11px;")
        form.addRow(stability_label)

        self._sb_fb_std_window = QSpinBox()
        self._sb_fb_std_window.setRange(0, 1000)
        self._sb_fb_std_window.setValue(0)
        self._sb_fb_std_window.setSpecialValueText("disabled")
        self._sb_fb_std_window.setToolTip(
            "Threshold 도달 후 추가 폴링 샘플 수.\n"
            "0 = 비활성화 (기존 동작).\n"
            "N > 0 이면 최근 N개 측정값의 std dev < Threshold가 될 때 진행."
        )
        self._le_fb_noisefloor = QLineEdit("0.0")
        self._le_fb_noisefloor.setToolTip(
            "측정값과 같은 단위의 기기 노이즈 하한선.\n"
            "분모 = max(|mean|, |next_v|) + noisefloor 에서 /0 방지 및 zero 신호 보호."
        )
        self._le_fb_std_threshold = QLineEdit("0.01")
        self._le_fb_std_threshold.setToolTip(
            "무차원 안정성 기준 (예: 0.01 = 1%).\n"
            "metric = SD / (max(|mean|, |next_v|) + noisefloor) < 이 값이면 안정 판정."
        )
        form.addRow("Std Window (n):", self._sb_fb_std_window)
        form.addRow("Noise Floor:", self._le_fb_noisefloor)
        form.addRow("Stability Threshold:", self._le_fb_std_threshold)
        return self._fb_widget

    def _build_wait_fields(self) -> QWidget:
        """WAIT_FOR_TIME advance type 전용 필드."""
        self._wait_widget = QWidget()
        form = QFormLayout(self._wait_widget)
        form.setContentsMargins(0, 0, 0, 0)
        self._le_wait = QLineEdit("1.0")
        form.addRow("Wait Time (s):", self._le_wait)
        return self._wait_widget

    def _build_button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch()

        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_cancel.setAutoDefault(False)
        row.addWidget(btn_cancel)

        btn_ok = QPushButton("OK")
        btn_ok.setStyleSheet(
            "background-color: #2e7d32; color: white; font-weight: bold; padding: 4px 16px;"
        )
        btn_ok.clicked.connect(self._on_ok)
        btn_ok.setDefault(True)   # Enter 키로 OK
        row.addWidget(btn_ok)
        return row

    def _connect_signals(self):
        """위젯이 모두 만들어진 뒤 한곳에서 연결한다."""
        self._combo_alias.currentTextChanged.connect(self._refresh_entry_combo)
        self._combo_alias.currentTextChanged.connect(self._refresh_fb_cmd_combo)
        self._combo_entry.currentIndexChanged.connect(self._refresh_placeholders)
        # 선택한 명령 종류(sweep value / write command)에 따라 advance 옵션이 달라진다
        self._combo_entry.currentIndexChanged.connect(self._update_advance_options)
        self._combo_advance.currentIndexChanged.connect(self._update_advance_visibility)
        self._cb_safety.toggled.connect(self._safety_detail.setVisible)
        # 사용자가 직접 고치면 자동 채움을 멈춘다
        self._le_desc.textEdited.connect(lambda: setattr(self, '_desc_auto', False))
        self._le_axis.textEdited.connect(lambda: setattr(self, '_axis_auto', False))

    def _refresh_fb_cmd_combo(self, alias: str = ""):
        """Feedback read cmd 콤보박스를 선택된 alias의 measurement 목록으로 갱신."""
        if not hasattr(self, '_combo_fb_cmd'):
            return
        alias = alias or self._combo_alias.currentText()
        self._combo_fb_cmd.blockSignals(True)
        self._combo_fb_cmd.clear()
        lib = self._lib_reg.get_library(alias)
        for m in lib.measurements:
            label = f"{m.description}  [{_truncate(m.cmd_query, 50)}]"
            self._combo_fb_cmd.addItem(label, m.cmd_query)
        self._combo_fb_cmd.blockSignals(False)

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

        # 두 번째 축: 선택 명령 종류(sweep value/write command)에 따라 advance 옵션 갱신
        if self._section_type == 'second':
            self._update_advance_options()

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
        self._update_fb_cmd_state()

    def _current_source_type(self) -> str:
        """선택된 라이브러리 명령의 실제 종류로 판별.
        sweep value(되읽기 Paired Read 있음) → 'sweep_value', 아니면 'write_cmd'."""
        idx = self._combo_entry.currentIndex()
        if hasattr(self, "_lib_entries") and 0 <= idx < len(self._lib_entries):
            return ("sweep_value"
                    if hasattr(self._lib_entries[idx], "paired_read_cmd")
                    else "write_cmd")
        return "write_cmd"

    def _update_advance_options(self, *_):
        source_type = self._current_source_type()
        for i in range(self._combo_advance.count()):
            at = self._combo_advance.itemData(i)
            item = self._combo_advance.model().item(i)
            if item:
                # write_cmd에서는 SWEEP만 비활성 (FEEDBACK은 허용)
                if source_type == "write_cmd" and at == SecondSweepAdvanceType.SWEEP:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                else:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
        self._update_fb_cmd_state()

    def _update_fb_cmd_state(self):
        """source_type에 따라 feedback read cmd 콤보박스 활성/비활성 전환."""
        if not hasattr(self, '_combo_fb_cmd'):
            return
        source_type = self._current_source_type()
        at = self._combo_advance.currentData()
        if at != SecondSweepAdvanceType.FEEDBACK:
            return
        is_sweep_val = (source_type == "sweep_value")
        self._combo_fb_cmd.setEnabled(not is_sweep_val)
        if is_sweep_val:
            self._combo_fb_cmd.setToolTip(
                "Paired Command의 되읽기(Paired Read) 명령이 자동으로 사용됩니다."
            )
        else:
            self._combo_fb_cmd.setToolTip("")

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
            self._update_advance_options()   # source type은 선택 명령으로 자동 판별

            for i in range(self._combo_advance.count()):
                if self._combo_advance.itemData(i) == p.advance_type:
                    self._combo_advance.setCurrentIndex(i)
                    break
            self._update_advance_visibility(self._combo_advance.currentIndex())
            # Feedback read cmd: select by stored cmd_query
            self._refresh_fb_cmd_combo(p.alias)
            for i in range(self._combo_fb_cmd.count()):
                if self._combo_fb_cmd.itemData(i) == p.feedback_read_cmd:
                    self._combo_fb_cmd.setCurrentIndex(i)
                    break
            self._le_fb_poll.setText(str(p.feedback_poll_interval))
            self._le_fb_tol.setText(str(p.feedback_tolerance_pct))
            self._sb_fb_std_window.setValue(p.feedback_std_window)
            self._le_fb_noisefloor.setText(str(p.feedback_noisefloor))
            self._le_fb_std_threshold.setText(str(p.feedback_std_threshold))
            self._le_wait.setText(str(p.wait_time))

    # ------------------------------------------------------------------
    # Validation & result
    # ------------------------------------------------------------------

    def _on_ok(self):
        """입력을 검증해 항목을 만든다. 검증 실패는 예외로 올라와 경고 창이 된다."""
        try:
            self._result = self._build_entry()
        except _ValidationError as err:
            QMessageBox.warning(self, err.title, str(err))
            return
        self.accept()

    def _build_entry(self):
        """섹션 타입에 맞는 Instantiated* 항목을 만든다.

        검증에 실패하면 _ValidationError 를 던진다 — 실패 지점마다 경고 창을 띄우고
        return 하던 것을 한 곳(_on_ok)으로 모으기 위함이다.
        """
        meta = self._collect_meta()
        entry = self._selected_library_entry()
        filled, sweep_placeholder = self._collect_placeholder_values()

        builders = {
            'measurement': self._build_measurement,
            'sweep':       self._build_sweep_value,
            'write':       self._build_write_cmd,
            'second':      self._build_second_channel,
        }
        build = builders[self._section_type]
        return build(entry, meta, filled, sweep_placeholder)

    # ── 입력 수집 / 검증 ──────────────────────────────────────────────────

    def _collect_meta(self) -> "_EntryMeta":
        alias = self._combo_alias.currentText()
        description = self._le_desc.text().strip()
        if not alias:
            raise _ValidationError("Select an instrument alias.")
        if not description:
            raise _ValidationError("Description is required.")
        return _EntryMeta(
            alias=alias,
            description=description,
            figure_axis=self._le_axis.text().strip(),
            unit=self._le_unit.text().strip(),
        )

    def _selected_library_entry(self):
        idx = self._combo_entry.currentIndex()
        entries = getattr(self, '_lib_entries', None)
        if idx < 0 or entries is None or idx >= len(entries):
            raise _ValidationError("Select a library entry.")
        return entries[idx]

    def _collect_placeholder_values(self) -> tuple:
        """placeholder 입력을 모은다. 반환: ({이름: 값}, sweep 축 이름 또는 None)

        sweep 축으로 지정한 자리는 값 대신 '[SWEEP]' 을 넣어 둔다 — 나중에 다시
        편집할 때 어느 자리가 축이었는지 복원하기 위해 프로파일에도 이대로 저장된다.
        """
        filled: Dict[str, str] = {}
        sweep_placeholder: Optional[str] = None

        for name, (combo, line_edit) in self._ph_widgets.items():
            is_sweep = combo is not None and combo.currentIndex() == 0
            if is_sweep:
                if sweep_placeholder is not None:
                    raise _ValidationError("Exactly one placeholder must be [SWEEP].")
                sweep_placeholder = name
                filled[name] = _SWEEP_MARKER
                continue

            value = line_edit.text().strip()
            if not value:
                suffix = " a fixed" if combo is not None else " a"
                raise _ValidationError(
                    f"Placeholder {{{name}}} needs{suffix} value.")
            filled[name] = value

        return filled, sweep_placeholder

    # ── 섹션별 생성 ───────────────────────────────────────────────────────

    def _build_measurement(self, entry, meta, filled, sweep_placeholder):
        if not hasattr(entry, 'cmd_query'):
            raise _ValidationError("Selected entry is not a measurement.")
        try:
            resolved = entry.cmd_query.format(**filled) if filled else entry.cmd_query
        except KeyError as e:
            raise _ValidationError(f"Placeholder error: {e}", title="Error")
        return InstantiatedMeasurement(
            alias=meta.alias,
            description=meta.description,
            resolved_cmd=resolved,
            figure_axis=meta.figure_axis,
            unit=meta.unit,
            fill_params=filled,
        )

    def _build_sweep_value(self, entry, meta, filled, sweep_placeholder):
        if not hasattr(entry, 'cmd_set'):
            raise _ValidationError("Selected entry is not a paired command.")
        if sweep_placeholder is None:
            raise _ValidationError(
                "Exactly one placeholder in cmd_set must be set to [SWEEP].")

        safety_on = self._cb_safety.isChecked()
        return InstantiatedSweepValue(
            alias=meta.alias,
            description=meta.description,
            cmd_set=_fill_command(entry.cmd_set, filled, sweep_placeholder),
            paired_read_cmd=_fill_read_command(entry.paired_read_cmd, filled),
            figure_axis=meta.figure_axis,
            unit=meta.unit,
            fill_params=filled,
            safety_steps=self._sb_safety_steps.value() if safety_on else 0,
            safety_interval_ms=self._sb_safety_interval.value() if safety_on else 0.0,
        )

    def _build_write_cmd(self, entry, meta, filled, sweep_placeholder):
        if not hasattr(entry, 'cmd_set'):
            raise _ValidationError("Selected entry is not a write command.")
        # write 는 sweep 축이 없어도 된다 (있으면 최대 하나)
        return InstantiatedWriteCmd(
            alias=meta.alias,
            description=meta.description,
            cmd_set=_fill_command(entry.cmd_set, filled, sweep_placeholder),
            figure_axis=meta.figure_axis,
            unit=meta.unit,
            fill_params=filled,
        )

    def _build_second_channel(self, entry, meta, filled, sweep_placeholder):
        if sweep_placeholder is None and self._ph_widgets:
            raise _ValidationError(
                "Exactly one placeholder in cmd_set must be set to [SWEEP].")

        source_type = self._current_source_type()
        advance_type: SecondSweepAdvanceType = self._combo_advance.currentData()

        cmd_set = _fill_command(getattr(entry, 'cmd_set', ""), filled, sweep_placeholder)
        paired_read_cmd = ""
        if source_type == 'sweep_value' and hasattr(entry, 'paired_read_cmd'):
            paired_read_cmd = _fill_read_command(entry.paired_read_cmd, filled)

        common = dict(
            alias=meta.alias, description=meta.description,
            source_type=source_type, advance_type=advance_type,
            cmd_set=cmd_set, paired_read_cmd=paired_read_cmd,
            figure_axis=meta.figure_axis, unit=meta.unit,
        )

        if advance_type == SecondSweepAdvanceType.FEEDBACK:
            return InstantiatedSecondSweepChannel(
                **common, **self._collect_feedback_settings(source_type, paired_read_cmd))
        if advance_type == SecondSweepAdvanceType.WAIT_FOR_TIME:
            return InstantiatedSecondSweepChannel(
                **common, wait_time=self._collect_wait_time())
        # SIMPLE_HOP / SWEEP — 추가 설정 없음
        return InstantiatedSecondSweepChannel(**common)

    def _collect_feedback_settings(self, source_type: str, paired_read_cmd: str) -> dict:
        """FEEDBACK advance 에 필요한 값들.

        source 가 sweep value 면 짝꿍 read 명령을 그대로 feedback read 로 쓴다
        (같은 값을 읽는 명령이므로). write command 면 사용자가 따로 골라야 한다.
        """
        if source_type == "sweep_value":
            read_cmd = paired_read_cmd
        else:
            read_cmd = self._combo_fb_cmd.currentData() or ""
        if not read_cmd:
            raise _ValidationError(
                "Feedback read command is required.\n"
                "VISA 라이브러리에 measurement 항목을 추가한 후 선택하세요.")

        try:
            return {
                "feedback_read_cmd":      read_cmd,
                "feedback_poll_interval": float(self._le_fb_poll.text().strip()),
                "feedback_tolerance_pct": float(self._le_fb_tol.text().strip()),
                "feedback_std_window":    self._sb_fb_std_window.value(),
                "feedback_noisefloor":    float(self._le_fb_noisefloor.text().strip()),
                "feedback_std_threshold": float(self._le_fb_std_threshold.text().strip()),
            }
        except ValueError:
            raise _ValidationError("Feedback parameters must be numbers.")

    def _collect_wait_time(self) -> float:
        try:
            return float(self._le_wait.text().strip())
        except ValueError:
            raise _ValidationError("Wait time must be a number.")

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
# RightMeasPanel — 우측 패널용 compact 측정값 목록 패널
# ---------------------------------------------------------------------------


class RightMeasPanel(QGroupBox):
    """
    Parameter Manager 우측 패널용 compact 섹션.
    라이브러리에서 측정값(measurement)을 추가/삭제하는 일관된 UI.
    세부 설정(조건, 활성화 등)은 담당 창에서 관리.
    """

    def __init__(
        self,
        title: str,
        color: str,
        lib_registry: VisaLibraryRegistry,
        parent=None,
    ):
        super().__init__(title, parent)
        self.setStyleSheet(f"QGroupBox {{ font-weight: bold; color: {color}; }}")
        self._lib_reg = lib_registry
        self._items: List[InstantiatedMeasurement] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 14, 6, 6)
        layout.setSpacing(4)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Alias", "Description", "Unit"])
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setMinimumHeight(80)
        self._table.cellDoubleClicked.connect(self._edit)
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        btn_add  = QPushButton("Add")
        btn_edit = QPushButton("Edit")
        btn_del  = QPushButton("Delete")
        for btn in (btn_add, btn_edit, btn_del):
            btn.setFixedHeight(22)
        btn_add.clicked.connect(self._add)
        btn_edit.clicked.connect(self._edit)
        btn_del.clicked.connect(self._delete)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_edit)
        btn_row.addWidget(btn_del)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def load_items(self, items: List[InstantiatedMeasurement]):
        self._items = list(items)
        self._refresh_table()

    def get_items(self) -> List[InstantiatedMeasurement]:
        return list(self._items)

    def _refresh_table(self):
        self._table.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            for col, text in enumerate([item.alias, item.description, item.unit]):
                cell = QTableWidgetItem(str(text))
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self._table.setItem(row, col, cell)

    def _add(self):
        dlg = AddEntryDialog(self._lib_reg, 'measurement', self)
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
            self._lib_reg, 'measurement', self, prefill=self._items[row]
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
        self.resize(1180, 700)
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

    @staticmethod
    def _help_html() -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>Parameter Manager — 측정에 쓸 항목 고르기</b><hr>"
            "<b>VISA Library</b>에 적어둔 명령들 중에서, 이번 실험에 실제로 쓸 것들을 "
            "골라 '빈칸을 채워' 현재 프로파일에 등록하는 곳입니다.<br>"
            "여기서 등록한 항목들이 Main 화면·Double Sweep·메타데이터·알람에서 고를 수 있는 "
            "목록이 됩니다.<hr>"

            "<b>■ 전체 흐름 (3단계)</b><br>"
            "&nbsp;1) <b>VISA Library</b>: 장비에 보낼 명령을 '틀'로 적어둠 (빈칸 <code>{이름}</code> 포함 가능)<br>"
            "&nbsp;2) <b>Parameter Manager</b>(여기): 그 틀을 골라 빈칸을 실제 값으로 채워 등록<br>"
            "&nbsp;3) <b>Main 화면 / Double Sweep</b>: 등록된 항목을 체크해 실제 측정·sweep 실행<hr>"

            "<b>■ 여기서 등록하는 항목들 (좌측)</b><br>"
            "<table cellspacing='3' cellpadding='2'>"
            "<tr valign='top'><td><b>Sweep&nbsp;Values</b><br>(바꾸며 측정<br>하는 축)</td>"
            "<td>측정하면서 <b>쓸어가며 바꿀 값</b>(가로축). 예: 전압을 0→1V로.<br>"
            "값을 바꾸는 명령의 빈칸 하나를 <b>[SWEEP]</b>로 지정하면, 거기에 단계별 값이 들어갑니다.<br>"
            "<b>확인 읽기</b>: 값을 내보낸 뒤 실제 도달한 값을 다시 읽어 확인.<br>"
            "<b>Safety</b>: 값을 급히 바꾸지 않도록 잘게 나눠 보내는 안전 기능.<br>"
            "<i>역할: 측정의 가로축. Main 화면에서 어느 값을 쓸어갈지 고릅니다.</i></td></tr>"
            "<tr valign='top'><td><b>Measurements</b><br>(읽는 값)</td>"
            "<td>매 단계 <b>읽어올 값</b>(전류·전압·자기장·온도 등). 빈칸이 모두 채워진 완성된 명령입니다.<br>"
            "<i>역할: 가로축 값을 바꾼 뒤 읽는 값들. Main 화면의 Active Measurements 후보가 됩니다.</i></td></tr>"
            "<tr valign='top'><td><b>Second&nbsp;Sweep<br>Channels</b><br>(2차 축)</td>"
            "<td>Double Sweep에서 쓰는 <b>두 번째 축</b>(예: 자기장·게이트 전압).<br>"
            "값을 옮기는 방식(advance type)을 4가지 중 고릅니다 — 자세한 설명은 Double Sweep 창의 도움말 참고.<br>"
            "<i>역할: 2차 축 값을 한 칸 옮긴 뒤, 그 자리에서 1차 축 sweep을 수행 (지도처럼 2차원 측정).</i></td></tr>"
            "</table>"
            "<small>※ VISA Library의 <b>Write</b> 명령(켜기/끄기 등 준비용)은 측정 반복에 자동으로 들어가지 "
            "않으므로 여기 Paired Command·Read로 등록하지 않습니다.</small><br>"

            "<b>■ 우측 항목</b><br>"
            "&nbsp;• <b>Alarm Measurements</b>: Double Sweep 알람의 '측정값 조건'에서 고를 수 있는 측정 목록.<br>"
            "&nbsp;• <b>Meta Data Measurements</b>: 측정이 끝날 때 요약(.json)으로 저장할 후보 측정. "
            "여기 등록한 뒤 <b>Meta Data Config</b> 창에서 체크하면 저장됩니다.<hr>"

            "<b>■ 각 칸의 뜻</b><br>"
            "&nbsp;• <b>Alias</b>: 어느 장비로 보낼지 (장비 이름).<br>"
            "&nbsp;• <b>Description</b>: 알아보기 쉬운 이름 (목록·체크박스에 이 이름으로 보임).<br>"
            "&nbsp;• <b>Figure Axis</b>: 그래프 축·데이터 열에 표시될 이름.<br>"
            "&nbsp;• <b>Unit</b>: 단위(V/A/Ω/T …) — 그래프 눈금·파일 머리글에 사용.<br>"
            "&nbsp;• <b>[SWEEP]</b>: Paired Command에서 '쓸어갈 값'으로 쓸 빈칸 하나에만 표시.<hr>"

            "<b>■ 사용 팁</b><br>"
            "&nbsp;• 각 항목은 Add/Edit/Delete/Copy/▲▼ 버튼으로 추가·수정·정렬.<br>"
            "&nbsp;• 같은 명령을 채널만 바꿔 여러 개 쓸 땐 Copy한 뒤 빈칸 값만 고치면 됩니다.<br>"
            "&nbsp;• <b>Apply</b>: 프로파일에 저장하고 Main 화면을 새로 구성합니다. "
            "메타데이터 체크 상태는 그대로 유지됩니다.<br>"
            "&nbsp;• 명령의 글자(틀)는 여기가 아니라 <b>VISA Library</b>에서 만듭니다."
            "</body></html>"
        )

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # ── 상단 제목 + 도움말 ──
        hdr = _QHBox()
        _title = _QLabel("Parameter Manager")
        _title.setStyleSheet("font-weight: bold; font-size: 13px; color: #d2a8ff;")
        hdr.addWidget(_title)
        hdr.addStretch()
        hdr.addWidget(make_help_button(self._help_html(), "Parameter Manager 도움말"))
        outer.addLayout(hdr)

        # ── Horizontal splitter: 좌측(섹션 패널) / 우측(알람+메타데이터) ──
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── 좌측 패널 ────────────────────────────────────────────────
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        left_content = QWidget()
        content_layout = QVBoxLayout(left_content)
        content_layout.setSpacing(10)
        content_layout.setContentsMargins(0, 0, 0, 0)

        self._panel_sweep = SectionPanel(
            "Paired Command",
            ["Alias", "Description", "cmd_set", "paired_read", "Unit", "Safety"],
            "#79c0ff",
            self._lib_reg,
            'sweep',
        )
        content_layout.addWidget(self._panel_sweep)

        self._panel_meas = SectionPanel(
            "Read",
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
        left_scroll.setWidget(left_content)
        splitter.addWidget(left_scroll)

        # ── 우측 패널 ────────────────────────────────────────────────
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        right_scroll.setMinimumWidth(260)

        right_content = QWidget()
        right_layout = QVBoxLayout(right_content)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(10)

        self._alarm_panel = RightMeasPanel("Alarm Measurements", "#ffa657", self._lib_reg)
        right_layout.addWidget(self._alarm_panel)

        sep_v = QFrame()
        sep_v.setFrameShape(QFrame.Shape.HLine)
        sep_v.setStyleSheet("color: #30363d;")
        right_layout.addWidget(sep_v)

        self._meta_panel = RightMeasPanel("Meta Data Measurements", "#56d364", self._lib_reg)
        right_layout.addWidget(self._meta_panel)

        right_layout.addStretch()
        right_scroll.setWidget(right_content)
        splitter.addWidget(right_scroll)

        splitter.setSizes([780, 320])
        outer.addWidget(splitter, stretch=1)

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
        self._alarm_panel.load_items(mui.alarm_measurements)
        self._meta_panel.load_items(mui.meta_data_measurements)

    def _on_apply(self):
        alarm_meas = self._alarm_panel.get_items()
        meta_meas  = self._meta_panel.get_items()
        profile = MainUIProfile(
            sweep_values=self._panel_sweep.get_items(),
            measurements=self._panel_meas.get_items(),
            write_cmds=[],
            second_sweep_channels=self._panel_second.get_items(),
            alarm_measurements=alarm_meas,
            meta_data_measurements=meta_meas,
        )
        self._reg.save_main_ui(profile)

        # meta_data_measurements → MetaDataConfig.entries (기존 enabled 상태 보존)
        existing_meta = self._reg.meta_data_config
        existing_map  = {(e.alias, e.description): e for e in existing_meta.entries}
        new_entries = [
            MetaDataEntry(
                alias=m.alias,
                description=m.description,
                resolved_cmd=m.resolved_cmd,
                figure_axis=m.figure_axis,
                unit=m.unit,
                enabled=existing_map[(m.alias, m.description)].enabled
                        if (m.alias, m.description) in existing_map else True,
                meas_type=m.meas_type,
            )
            for m in meta_meas
        ]
        self._reg.save_meta_data_config(
            MetaDataConfig(enabled=existing_meta.enabled, entries=new_entries)
        )

        self.selection_applied.emit(profile)
        self.hide()

    def refresh_library(self):
        """라이브러리 저장 후 호출 — 재인스턴스화된 profile 데이터로 패널 갱신."""
        self._load_from_profile()

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
