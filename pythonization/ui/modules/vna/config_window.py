"""
VnaConfigWindow: VNA 섹션/명령어 설정 창.

탭 1 — Sections: 섹션 목록 + 명령어 목록 (write 전용) + 묶기/풀기
탭 2 — Acquire:  Sweep / Start / Wait / Read / Error Check 명령어 목록
"""
import copy
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QGroupBox, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QRadioButton, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from pythonization.app.paths import SETTINGS_DIR
from pythonization.config.models import SecondSweepAdvanceType
from pythonization.ui.modules.vna.models import (
    VnaAcquireConfig, VnaCommandEntry, VnaConfigData,
    VnaSectionConfig, VnaParamSpec, VnaAdvanceConfig, VnaPreAdvanceCmd,
    extract_params, load_vna_config, save_vna_config,
)

if TYPE_CHECKING:
    from pythonization.instruments.command_library import VisaLibraryRegistry

_MONO = QFont("Consolas", 9)
_DEFAULT_CONFIG_PATH = SETTINGS_DIR / "vna_config.yaml"

# Controlled Sweep advance type 선택지 (safety ramp 제외)
_ADV_TYPES = [
    (SecondSweepAdvanceType.SIMPLE_HOP,     "Simple Hop — 값만 설정하고 즉시 진행"),
    (SecondSweepAdvanceType.SWEEP,          "Sweep — 점진적으로 이동"),
    (SecondSweepAdvanceType.FEEDBACK,       "Feedback — 목표 도달 + std 안정화 대기"),
    (SecondSweepAdvanceType.WAIT_FOR_TIME,  "Wait for Time — 값 설정 후 시간 대기"),
    (SecondSweepAdvanceType.THRESHOLD_TIME, "Threshold + Time — 목표 도달 후 고정 시간 대기"),
]


class _PreAdvanceCmdDialog(QDialog):
    """Controlled Sweep advance 직전에 실행할 명령 추가/편집.

    라이브러리의 write 명령을 불러오고, {} 파라미터 중 1개를 '조절 파라미터'(◉)로
    선택한다. 조절 파라미터는 Control에서 sweep/dummy 값으로 채워지고, 나머지는 여기서
    고정값을 입력해야 한다.
    """

    def __init__(self, lib_reg, entry: Optional[VnaPreAdvanceCmd] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advance 전 실행 명령")
        self.setMinimumWidth(480)
        self._lib_reg = lib_reg
        self._editing = entry
        self._param_widgets: List[tuple] = []
        self._btn_group = QButtonGroup(self); self._btn_group.setExclusive(True)
        self._result: Optional[VnaPreAdvanceCmd] = None

        lay = QVBoxLayout(self)
        form = QFormLayout()
        self._combo_alias = QComboBox(); self._combo_alias.setFont(_MONO)
        for a in lib_reg.list_aliases():
            self._combo_alias.addItem(a)
        self._combo_alias.currentIndexChanged.connect(self._on_alias_changed)
        form.addRow("Instrument:", self._combo_alias)
        self._combo_cmd = QComboBox(); self._combo_cmd.setFont(_MONO)
        self._combo_cmd.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._combo_cmd.currentIndexChanged.connect(self._on_cmd_changed)
        form.addRow("Command (write):", self._combo_cmd)
        self._le_label = QLineEdit(); self._le_label.setPlaceholderText("표시 이름 (선택)")
        form.addRow("Label:", self._le_label)
        lay.addLayout(form)

        self._params_group = QGroupBox("Parameters  (◉ = advance 전 조절할 파라미터, 나머지는 값 입력)")
        self._params_lay = QFormLayout(self._params_group)
        lay.addWidget(self._params_group)

        self._lbl_preview = QLabel(""); self._lbl_preview.setFont(_MONO)
        self._lbl_preview.setStyleSheet("color:#79c0ff; font-size:10px;")
        self._lbl_preview.setWordWrap(True)
        lay.addWidget(self._lbl_preview)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        if entry:
            self._load(entry)
        else:
            self._on_alias_changed()

    def _on_alias_changed(self):
        alias = self._combo_alias.currentText()
        self._combo_cmd.blockSignals(True)
        self._combo_cmd.clear()
        self._combo_cmd.addItem("— select —", None)
        if alias:
            try:
                for wc in self._lib_reg.get_library(alias).write_cmds:
                    self._combo_cmd.addItem(f"[W] {wc.description}", wc)
            except Exception:
                pass
        self._combo_cmd.blockSignals(False)
        self._on_cmd_changed()

    def _on_cmd_changed(self):
        wc = self._combo_cmd.currentData()
        self._rebuild_params(extract_params(wc.cmd_set) if wc else [])

    def _rebuild_params(self, names: List[str]):
        while self._params_lay.rowCount():
            self._params_lay.removeRow(0)
        self._param_widgets = []
        for b in list(self._btn_group.buttons()):
            self._btn_group.removeButton(b)
        existing: dict = {}; existing_user = None
        if self._editing:
            for p in self._editing.params:
                existing[p.name] = p.value
                if p.is_user_input:
                    existing_user = p.name
        for name in names:
            row = QWidget(); rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(6)
            le = QLineEdit(); le.setFont(_MONO)
            le.setPlaceholderText(f"{{{name}}} 고정값")
            le.setText(existing.get(name, ""))
            le.textChanged.connect(self._update_preview)
            rb = QRadioButton("조절"); rb.toggled.connect(self._update_preview)
            self._btn_group.addButton(rb)
            rl.addWidget(le, 1); rl.addWidget(rb)
            self._params_lay.addRow(f"{{{name}}}:", row)
            self._param_widgets.append((name, le, rb))
        if self._param_widgets:   # 기본: 첫 파라미터(또는 복원된 것)를 조절 대상으로
            target = existing_user or self._param_widgets[0][0]
            for name, le, rb in self._param_widgets:
                if name == target:
                    rb.setChecked(True)
        self._params_group.setVisible(bool(names))
        self._update_preview()

    def _update_preview(self, *_):
        wc = self._combo_cmd.currentData()
        if wc is None:
            self._lbl_preview.setText("")
            return
        d = {}
        for name, le, rb in self._param_widgets:
            d[name] = "[조절]" if rb.isChecked() else (le.text().strip() or f"{{{name}}}")
        try:
            self._lbl_preview.setText("→ " + wc.cmd_set.format(**d))
        except Exception:
            self._lbl_preview.setText("→ " + wc.cmd_set)

    def _ok(self):
        wc = self._combo_cmd.currentData()
        if wc is None:
            return
        from PySide6.QtWidgets import QMessageBox
        for name, le, rb in self._param_widgets:
            if not rb.isChecked() and not le.text().strip():
                QMessageBox.warning(self, "확인", f"파라미터 {{{name}}} 의 고정값을 채워주세요.")
                return
        params = [VnaParamSpec(name=name, value=le.text().strip(),
                               is_user_input=rb.isChecked())
                  for name, le, rb in self._param_widgets]
        self._result = VnaPreAdvanceCmd(
            alias=self._combo_alias.currentText(),
            description=wc.description, params=params,
            label=self._le_label.text().strip())
        self.accept()

    def _load(self, entry: VnaPreAdvanceCmd):
        i = self._combo_alias.findText(entry.alias)
        if i >= 0:
            self._combo_alias.blockSignals(True)
            self._combo_alias.setCurrentIndex(i)
            self._combo_alias.blockSignals(False)
        self._on_alias_changed()
        for j in range(self._combo_cmd.count()):
            wc = self._combo_cmd.itemData(j)
            if wc and wc.description == entry.description:
                self._combo_cmd.setCurrentIndex(j)
                break
        self._le_label.setText(entry.label)

    def result_cmd(self) -> Optional[VnaPreAdvanceCmd]:
        return self._result


class _AdvanceConfigWidget(QGroupBox):
    """Controlled Sweep의 advance 설정 — advance type별 필드 + pre-advance 명령 목록.

    Parameter Manager의 second sweep channel과 동일한 방식을 재사용하되,
    safety ramp 관련 설정은 VNA에서는 사용하지 않으므로 제외한다.
    """

    def __init__(self, lib_reg, parent=None):
        super().__init__("Controlled Sweep — 안정화(advance) 설정", parent)
        self._lib_reg = lib_reg
        self._pre_cmds: List[VnaPreAdvanceCmd] = []
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        top = QFormLayout()
        self._combo_adv = QComboBox()
        for t, label in _ADV_TYPES:
            self._combo_adv.addItem(label, t.value)
        self._combo_adv.currentIndexChanged.connect(self._update_visibility)
        top.addRow("Advance Type:", self._combo_adv)
        lay.addLayout(top)

        # SWEEP fields
        self._sweep_box = QWidget()
        sf = QFormLayout(self._sweep_box); sf.setContentsMargins(0, 0, 0, 0)
        self._le_rate = QLineEdit("1.0"); self._le_rate.setFont(_MONO)
        self._le_paired = QLineEdit(); self._le_paired.setFont(_MONO)
        self._le_paired.setPlaceholderText("현재값 확인 read 명령 (선택)")
        sf.addRow("Sweep Rate:", self._le_rate)
        sf.addRow("Paired Read:", self._le_paired)
        lay.addWidget(self._sweep_box)

        # THRESHOLD fields (Feedback & Threshold+Time 공통): 목표 도달 판정
        #   band = max((1-tol%)·denom, noisefloor). noisefloor가 절대 허용오차 하한.
        self._thr_box = QWidget()
        ff = QFormLayout(self._thr_box); ff.setContentsMargins(0, 0, 0, 0)
        self._le_fb_read = QLineEdit(); self._le_fb_read.setFont(_MONO)
        self._le_fb_read.setPlaceholderText("도달 감지용 read 명령")
        self._le_fb_poll = QLineEdit("1.0")
        self._le_fb_tol = QLineEdit("95.0")
        self._le_fb_noise = QLineEdit("0.0")
        ff.addRow("Read Cmd:", self._le_fb_read)
        ff.addRow("Poll Interval(s):", self._le_fb_poll)
        ff.addRow("Tolerance(%):", self._le_fb_tol)
        ff.addRow("Noise Floor:", self._le_fb_noise)
        lay.addWidget(self._thr_box)

        # STD stabilization fields (Feedback 전용): 도달 후 std 안정화 대기
        self._std_box = QWidget()
        sf2 = QFormLayout(self._std_box); sf2.setContentsMargins(0, 0, 0, 0)
        self._sb_fb_win = QSpinBox(); self._sb_fb_win.setRange(0, 100000)
        self._le_fb_std = QLineEdit("0.01")
        sf2.addRow("Std Window:", self._sb_fb_win)
        sf2.addRow("Std Threshold:", self._le_fb_std)
        lay.addWidget(self._std_box)

        # WAIT fields (Wait for Time & Threshold+Time 공통): 고정 시간 대기
        self._wait_box = QWidget()
        wf = QFormLayout(self._wait_box); wf.setContentsMargins(0, 0, 0, 0)
        self._le_wait = QLineEdit("1.0")
        wf.addRow("Wait Time(s):", self._le_wait)
        lay.addWidget(self._wait_box)

        # Pre-advance commands
        pre_lbl = QLabel("Advance 전 실행 명령 (예: 기기 내부 속도 조절 — 여러 개 가능)")
        pre_lbl.setStyleSheet("color: #79c0ff; font-size: 10px;")
        lay.addWidget(pre_lbl)
        self._pre_list = QListWidget(); self._pre_list.setFont(_MONO)
        self._pre_list.setMaximumHeight(80)
        self._pre_list.itemDoubleClicked.connect(lambda _: self._edit_pre())
        lay.addWidget(self._pre_list)
        pre_bar = QHBoxLayout()
        b_add = QPushButton("+ 명령"); b_add.clicked.connect(self._add_pre)
        b_edit = QPushButton("편집"); b_edit.clicked.connect(self._edit_pre)
        b_del = QPushButton("✕"); b_del.setFixedWidth(26); b_del.clicked.connect(self._del_pre)
        pre_bar.addWidget(b_add); pre_bar.addWidget(b_edit); pre_bar.addWidget(b_del)
        pre_bar.addStretch()
        lay.addLayout(pre_bar)

        self._update_visibility()

    def _update_visibility(self):
        t = self._combo_adv.currentData()
        A = SecondSweepAdvanceType
        is_fb = t == A.FEEDBACK.value
        is_tt = t == A.THRESHOLD_TIME.value
        self._sweep_box.setVisible(t == A.SWEEP.value)
        self._thr_box.setVisible(is_fb or is_tt)         # 도달 판정: Feedback & Threshold+Time
        self._std_box.setVisible(is_fb)                  # std 안정화: Feedback 전용
        self._wait_box.setVisible(t == A.WAIT_FOR_TIME.value or is_tt)  # 시간 대기: Wait & Threshold+Time

    def apply_paired_read(self, cmd: str):
        """선택한 sweep value에 Paired Read 명령이 있으면 비어있는 read 칸을 자동 채운다.
        (Feedback Read / Sweep Paired Read 모두 — 사용자가 이미 입력한 값은 보존)"""
        if not cmd:
            return
        if not self._le_paired.text().strip():
            self._le_paired.setText(cmd)
        if not self._le_fb_read.text().strip():
            self._le_fb_read.setText(cmd)

    def _refresh_pre(self):
        self._pre_list.clear()
        for c in self._pre_cmds:
            self._pre_list.addItem(f"{c.alias}  {c.label or c.description}")

    def _add_pre(self):
        dlg = _PreAdvanceCmdDialog(self._lib_reg, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_cmd():
            self._pre_cmds.append(dlg.result_cmd()); self._refresh_pre()

    def _edit_pre(self):
        i = self._pre_list.currentRow()
        if not (0 <= i < len(self._pre_cmds)):
            return
        dlg = _PreAdvanceCmdDialog(self._lib_reg, entry=self._pre_cmds[i], parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_cmd():
            self._pre_cmds[i] = dlg.result_cmd(); self._refresh_pre()

    def _del_pre(self):
        i = self._pre_list.currentRow()
        if 0 <= i < len(self._pre_cmds):
            self._pre_cmds.pop(i); self._refresh_pre()

    @staticmethod
    def _f(le, default=0.0) -> float:
        try:
            return float(le.text())
        except ValueError:
            return default

    def get_config(self) -> VnaAdvanceConfig:
        return VnaAdvanceConfig(
            advance_type=SecondSweepAdvanceType(self._combo_adv.currentData()),
            paired_read_cmd=self._le_paired.text().strip(),
            sweep_rate=self._f(self._le_rate, 1.0),
            feedback_read_cmd=self._le_fb_read.text().strip(),
            feedback_poll_interval=self._f(self._le_fb_poll, 1.0),
            feedback_tolerance_pct=self._f(self._le_fb_tol, 95.0),
            feedback_std_window=self._sb_fb_win.value(),
            feedback_noisefloor=self._f(self._le_fb_noise, 0.0),
            feedback_std_threshold=self._f(self._le_fb_std, 0.01),
            wait_time=self._f(self._le_wait, 1.0),
            pre_cmds=list(self._pre_cmds),
        )

    def load_config(self, adv: VnaAdvanceConfig):
        idx = self._combo_adv.findData(adv.advance_type.value)
        if idx >= 0:
            self._combo_adv.setCurrentIndex(idx)
        self._le_paired.setText(adv.paired_read_cmd)
        self._le_rate.setText(f"{adv.sweep_rate:g}")
        self._le_fb_read.setText(adv.feedback_read_cmd)
        self._le_fb_poll.setText(f"{adv.feedback_poll_interval:g}")
        self._le_fb_tol.setText(f"{adv.feedback_tolerance_pct:g}")
        self._sb_fb_win.setValue(adv.feedback_std_window)
        self._le_fb_noise.setText(f"{adv.feedback_noisefloor:g}")
        self._le_fb_std.setText(f"{adv.feedback_std_threshold:g}")
        self._le_wait.setText(f"{adv.wait_time:g}")
        self._pre_cmds = [c.model_copy() for c in adv.pre_cmds]
        self._refresh_pre()
        self._update_visibility()


# ---------------------------------------------------------------------------
# Command editor dialog  (mode="write" | "read" | "sweep")
# ---------------------------------------------------------------------------

class _CommandEditorDialog(QDialog):
    """단일 VnaCommandEntry 추가/편집 다이얼로그."""

    def __init__(self, lib_reg: "VisaLibraryRegistry",
                 entry: Optional[VnaCommandEntry] = None,
                 mode: str = "write", parent=None):
        super().__init__(parent)
        self._lib_reg = lib_reg
        self._editing = entry
        self._mode    = mode
        self._param_widgets: List[tuple] = []
        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)
        self._result: Optional[VnaCommandEntry] = None

        self.setWindowTitle("Edit Command" if entry else "Add Command")
        self.setMinimumWidth(500)
        self._build_ui()
        if entry:
            self._load(entry)
        else:
            self._on_alias_changed()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)

        self._combo_alias = QComboBox()
        self._combo_alias.setFont(_MONO)
        for a in self._lib_reg.list_aliases():
            self._combo_alias.addItem(a)
        self._combo_alias.currentIndexChanged.connect(self._on_alias_changed)
        form.addRow("Instrument:", self._combo_alias)

        self._combo_cmd = QComboBox()
        self._combo_cmd.setFont(_MONO)
        self._combo_cmd.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._combo_cmd.currentIndexChanged.connect(self._on_cmd_changed)
        form.addRow("Command:", self._combo_cmd)

        self._lbl_type = QLabel("")
        self._lbl_type.setStyleSheet("color: #79c0ff; font-size: 10px;")
        form.addRow("Type:", self._lbl_type)

        # Write/sweep mode 전용: unit_type 선택
        self._combo_unit_type = QComboBox()
        self._combo_unit_type.addItem("없음", "")
        self._combo_unit_type.addItem("주파수  Hz / kHz / MHz / GHz", "Hz")
        self._combo_unit_type.addItem("시간  ns / us / ms / s", "sec")
        self._combo_unit_type.addItem("자기장  uT / mT / T", "T")
        self._combo_unit_type.addItem("온도  mK / K", "K")
        self._combo_unit_type.setVisible(self._mode in ("write", "sweep"))
        form.addRow("Unit Type:", self._combo_unit_type)

        # Figure Axis: read 모드와 sweep 모드 모두 사용
        self._le_figure_axis = QLineEdit()
        self._le_figure_axis.setFont(_MONO)
        self._le_figure_axis.setPlaceholderText(
            "레이블 (예: Frequency, Time …)")
        self._le_figure_axis.setVisible(self._mode in ("read", "sweep"))
        form.addRow("Figure Axis:", self._le_figure_axis)

        # Read-mode 전용 필드
        self._sb_stride = QSpinBox()
        self._sb_stride.setRange(1, 100)
        self._sb_stride.setValue(1)
        self._sb_stride.setToolTip(
            "Data Stride — 장비가 돌려준 숫자들 중 몇 칸마다 하나씩 골라 쓸지.\n"
            "한 번에 여러 값이 오는 응답(예: 실수,허수 쌍)에서 일부만 추릴 때 씁니다.\n"
            "  1 = 모든 값 사용 (기본)\n"
            "  2 = 두 개마다 한 개 (예: 실수/허수 쌍에서 실수만)\n"
            "  3 = 세 개마다 한 개 …\n"
            "온도·자기장처럼 값 하나만 읽는 명령은 1로 두세요.")
        self._sb_stride.setVisible(self._mode == "read")
        form.addRow("Data Stride:", self._sb_stride)

        self._le_units = QLineEdit()
        self._le_units.setFont(_MONO)
        self._le_units.setPlaceholderText("예: Hz, dB, A, T …")
        self._le_units.setVisible(self._mode == "read")
        form.addRow("Units:", self._le_units)

        # sweep 모드 전용: General vs Controlled Sweep
        self._combo_sweep_kind = QComboBox()
        self._combo_sweep_kind.addItem("General Sweep (단순 — 값만 바꿔가며)", "general")
        self._combo_sweep_kind.addItem("Controlled Sweep (PID 안정화 필요)", "controlled")
        self._combo_sweep_kind.setVisible(self._mode == "sweep")
        self._combo_sweep_kind.currentIndexChanged.connect(self._on_sweep_kind_changed)
        if self._mode == "sweep":
            form.addRow("Sweep Kind:", self._combo_sweep_kind)

        lay.addLayout(form)

        # Controlled Sweep advance 설정 (sweep + controlled 일 때만 표시)
        self._adv_widget = _AdvanceConfigWidget(self._lib_reg)
        self._adv_widget.setVisible(False)
        lay.addWidget(self._adv_widget)

        self._params_group = QGroupBox("Parameters")
        self._params_lay = QFormLayout(self._params_group)
        self._params_lay.setHorizontalSpacing(8)
        self._params_lay.setVerticalSpacing(4)
        lay.addWidget(self._params_group)

        self._lbl_preview = QLabel("")
        self._lbl_preview.setFont(_MONO)
        self._lbl_preview.setStyleSheet("color: #79c0ff; font-size: 10px;")
        self._lbl_preview.setWordWrap(True)
        lay.addWidget(self._lbl_preview)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _on_sweep_kind_changed(self):
        controlled = (self._mode == "sweep"
                      and self._combo_sweep_kind.currentData() == "controlled")
        self._adv_widget.setVisible(controlled)

    def _on_alias_changed(self):
        alias = self._combo_alias.currentText()
        self._combo_cmd.blockSignals(True)
        self._combo_cmd.clear()
        self._combo_cmd.addItem("— select —", None)
        if alias:
            try:
                lib = self._lib_reg.get_library(alias)
                if self._mode in ("write", "sweep"):
                    for wc in lib.write_cmds:
                        self._combo_cmd.addItem(f"[W] {wc.description}", ("write", wc))
                    for sv in lib.sweep_values:
                        self._combo_cmd.addItem(f"[P] {sv.description}", ("sweep", sv))
                else:  # "read"
                    for m in lib.measurements:
                        self._combo_cmd.addItem(f"[R] {m.description}", ("query", m))
            except Exception:
                pass
        self._combo_cmd.blockSignals(False)
        self._on_cmd_changed()

    def _on_cmd_changed(self):
        data = self._combo_cmd.currentData()
        if data is None:
            self._lbl_type.setText("")
            self._rebuild_params([])
            return
        kind, entry = data
        if kind == "query":
            self._lbl_type.setText("query (measurement)")
            template = entry.cmd_query
        else:
            self._lbl_type.setText("write" if kind == "write" else "sweep / write")
            template = entry.cmd_set
        self._rebuild_params(extract_params(template), template)
        # Controlled Sweep: sweep value의 Paired Read가 있으면 feedback read 자동 채움
        if (self._mode == "sweep" and kind == "sweep"
                and hasattr(self, "_adv_widget")
                and getattr(entry, "paired_read_cmd", "")):
            self._adv_widget.apply_paired_read(entry.paired_read_cmd)

    def _rebuild_params(self, names: List[str], template: str = ""):
        while self._params_lay.rowCount():
            self._params_lay.removeRow(0)
        self._param_widgets.clear()
        for btn in list(self._btn_group.buttons()):
            self._btn_group.removeButton(btn)

        existing_vals: dict = {}
        existing_user: Optional[str] = None
        if self._editing:
            for p in self._editing.params:
                existing_vals[p.name] = p.value
                if p.is_user_input:
                    existing_user = p.name

        for name in names:
            row_w = QWidget()
            row_lay = QHBoxLayout(row_w)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(6)

            le = QLineEdit()
            le.setFont(_MONO)
            le.setPlaceholderText(f"{{{name}}}")
            le.setText(existing_vals.get(name, ""))
            le.textChanged.connect(self._update_preview)
            row_lay.addWidget(le, stretch=1)

            _rb_label = "[SWEEP]" if self._mode == "sweep" else "User Input"
            rb = QRadioButton(_rb_label)
            rb.setVisible(self._mode in ("write", "sweep"))
            rb.setChecked(name == existing_user)
            rb.toggled.connect(self._update_preview)
            self._btn_group.addButton(rb)
            row_lay.addWidget(rb)

            self._params_lay.addRow(f"  {{{name}}}:", row_w)
            self._param_widgets.append((name, le, rb))

        self._params_group.setVisible(bool(names))
        self._update_preview()

    def _update_preview(self):
        data = self._combo_cmd.currentData()
        if data is None:
            self._lbl_preview.setText("")
            return
        kind, entry = data
        template = entry.cmd_query if kind == "query" else entry.cmd_set
        d = {}
        for name, le, rb in self._param_widgets:
            val = le.text().strip()
            d[name] = f"[{name}]" if rb.isChecked() else (val or f"{{{name}}}")
        try:
            self._lbl_preview.setText(f"→ {template.format(**d)}")
        except Exception:
            self._lbl_preview.setText(f"→ {template}")

    def _on_ok(self):
        data = self._combo_cmd.currentData()
        if data is None:
            return
        kind, entry = data
        alias    = self._combo_alias.currentText()
        cmd_type = "query" if kind == "query" else "write"
        params   = [
            VnaParamSpec(name=name, value=le.text().strip(),
                         is_user_input=rb.isChecked())
            for name, le, rb in self._param_widgets
        ]
        unit_type = (self._combo_unit_type.currentData()
                     if self._mode in ("write", "sweep") else "")
        sweep_kind = "general"
        advance = None
        if self._mode == "sweep":
            sweep_kind = self._combo_sweep_kind.currentData() or "general"
            if sweep_kind == "controlled":
                advance = self._adv_widget.get_config()
        self._result = VnaCommandEntry(
            alias=alias, description=entry.description,
            cmd_type=cmd_type, params=params, enabled=True,
            figure_axis=self._le_figure_axis.text().strip(),
            read_stride=self._sb_stride.value(),
            units=self._le_units.text().strip(),
            unit_type=unit_type,
            sweep_kind=sweep_kind, advance=advance,
        )
        self.accept()

    def _load(self, entry: VnaCommandEntry):
        idx = self._combo_alias.findText(entry.alias)
        if idx >= 0:
            self._combo_alias.blockSignals(True)
            self._combo_alias.setCurrentIndex(idx)
            self._combo_alias.blockSignals(False)
        self._on_alias_changed()
        for i in range(self._combo_cmd.count()):
            d = self._combo_cmd.itemData(i)
            if d and d[1].description == entry.description:
                self._combo_cmd.setCurrentIndex(i)
                break
        self._le_figure_axis.setText(entry.figure_axis)
        self._sb_stride.setValue(max(1, entry.read_stride))
        self._le_units.setText(entry.units)
        idx = self._combo_unit_type.findData(entry.unit_type)
        if idx >= 0:
            self._combo_unit_type.setCurrentIndex(idx)
        # sweep kind + controlled advance 복원
        if self._mode == "sweep":
            ki = self._combo_sweep_kind.findData(entry.sweep_kind or "general")
            if ki >= 0:
                self._combo_sweep_kind.setCurrentIndex(ki)
            if entry.advance is not None:
                self._adv_widget.load_config(entry.advance)
            self._on_sweep_kind_changed()

    def result_entry(self) -> Optional[VnaCommandEntry]:
        return self._result


# ---------------------------------------------------------------------------
# Reusable command list widget
# ---------------------------------------------------------------------------

class _CmdListWidget(QWidget):
    def __init__(self, lib_reg: "VisaLibraryRegistry",
                 mode: str = "write", with_bind: bool = False,
                 with_onoff: bool = True, parent=None):
        super().__init__(parent)
        self._lib_reg    = lib_reg
        self._mode       = mode
        self._with_bind  = with_bind
        self._with_onoff = with_onoff
        self._cmds: List[VnaCommandEntry] = []
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        self._lst = QListWidget()
        self._lst.setFont(_MONO)
        if self._with_bind:
            self._lst.setSelectionMode(
                QListWidget.SelectionMode.ExtendedSelection)
        self._lst.itemDoubleClicked.connect(lambda _: self._edit())
        lay.addWidget(self._lst, stretch=1)

        btn_bar = QHBoxLayout()
        _buttons = [("Add", self._add), ("Edit", self._edit), ("Copy", self._copy)]
        if self._with_onoff:
            _buttons.append(("On/Off", self._toggle_enabled))
        _buttons += [("✕", self._remove), ("↑", self._move_up), ("↓", self._move_down)]
        for label, slot in _buttons:
            b = QPushButton(label)
            b.setFixedHeight(22)
            if label in ("↑", "↓", "✕"):
                b.setFixedWidth(26)
            if label == "On/Off":
                b.setToolTip("선택 명령을 지우지 않고 활성/비활성 전환 "
                             "(비활성은 측정에서 제외, 회색·✗OFF 표시)")
            b.clicked.connect(slot)
            btn_bar.addWidget(b)

        if self._with_bind:
            btn_bar.addSpacing(8)
            b_bind = QPushButton("묶기")
            b_bind.setFixedHeight(22)
            b_bind.setToolTip("선택한 명령어들의 user_input 파라미터를 묶기")
            b_bind.clicked.connect(self._bind_selected)
            btn_bar.addWidget(b_bind)

            b_unbind = QPushButton("풀기")
            b_unbind.setFixedHeight(22)
            b_unbind.setToolTip("선택한 명령어의 묶기 해제")
            b_unbind.clicked.connect(self._unbind_selected)
            btn_bar.addWidget(b_unbind)

        btn_bar.addStretch()
        lay.addLayout(btn_bar)

    def get_commands(self) -> List[VnaCommandEntry]:
        return list(self._cmds)

    def set_commands(self, cmds: List[VnaCommandEntry]):
        self._cmds = list(cmds)
        self._refresh()

    def _refresh(self):
        self._lst.clear()
        for e in self._cmds:
            item = QListWidgetItem(self._label(e))
            if not e.enabled:
                item.setForeground(QColor("#6e7681"))   # 비활성: 회색 표시
            self._lst.addItem(item)

    def _label(self, entry: VnaCommandEntry) -> str:
        tag = "[R]" if entry.cmd_type == "query" else "[W]"
        state = "" if entry.enabled else "✗OFF "
        name = entry.figure_axis or entry.description
        extras = []
        if entry.bind_id > 0:
            extras.append(f"[묶기:{entry.bind_id}]")
        if entry.units:
            extras.append(f"[{entry.units}]")
        if entry.read_stride > 1:
            extras.append(f"stride={entry.read_stride}")
        if entry.sweep_kind == "controlled" and entry.advance is not None:
            extras.append(f"[CTRL:{entry.advance.advance_type.value}]")
        param_parts = []
        for p in entry.params:
            if p.is_user_input:
                param_parts.append(f"[{p.name}]")
            else:
                param_parts.append(
                    f"{{{p.name}}}={p.value}" if p.value else f"{{{p.name}}}")
        if param_parts:
            extras.append(", ".join(param_parts))
        suffix = "  " + "  ".join(extras) if extras else ""
        return f"{state}{tag}  {entry.alias}  {name}{suffix}"

    def _add(self):
        dlg = _CommandEditorDialog(self._lib_reg, mode=self._mode, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_entry():
            self._cmds.append(dlg.result_entry())
            self._refresh()
            self._lst.setCurrentRow(len(self._cmds) - 1)

    def _edit(self):
        idx = self._lst.currentRow()
        if not (0 <= idx < len(self._cmds)):
            return
        dlg = _CommandEditorDialog(self._lib_reg, entry=self._cmds[idx],
                                    mode=self._mode, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_entry():
            self._cmds[idx] = dlg.result_entry()
            self._refresh()
            self._lst.setCurrentRow(idx)

    def _copy(self):
        idx = self._lst.currentRow()
        if 0 <= idx < len(self._cmds):
            self._cmds.insert(idx + 1, copy.deepcopy(self._cmds[idx]))
            self._refresh()
            self._lst.setCurrentRow(idx + 1)

    def _toggle_enabled(self):
        """선택(또는 현재) 명령들의 enabled를 토글한다 — 삭제 없이 활성/비활성."""
        idxs = self._selected_indices()
        if not idxs:
            cur = self._lst.currentRow()
            if cur < 0:
                return
            idxs = [cur]
        for i in idxs:
            if 0 <= i < len(self._cmds):
                self._cmds[i] = self._cmds[i].model_copy(
                    update={"enabled": not self._cmds[i].enabled})
        self._refresh()

    def _remove(self):
        idx = self._lst.currentRow()
        if 0 <= idx < len(self._cmds):
            self._cmds.pop(idx)
            self._refresh()
            self._lst.setCurrentRow(max(0, idx - 1))

    def _move_up(self):
        idx = self._lst.currentRow()
        if idx > 0:
            self._cmds[idx - 1], self._cmds[idx] = \
                self._cmds[idx], self._cmds[idx - 1]
            self._refresh()
            self._lst.setCurrentRow(idx - 1)

    def _move_down(self):
        idx = self._lst.currentRow()
        if 0 <= idx < len(self._cmds) - 1:
            self._cmds[idx], self._cmds[idx + 1] = \
                self._cmds[idx + 1], self._cmds[idx]
            self._refresh()
            self._lst.setCurrentRow(idx + 1)

    def _selected_indices(self) -> List[int]:
        return sorted(self._lst.row(item)
                      for item in self._lst.selectedItems())

    def _bind_selected(self):
        indices = self._selected_indices()
        if len(indices) < 2:
            return
        # Next unused bind_id in this list
        used = {c.bind_id for c in self._cmds if c.bind_id > 0}
        new_id = 1
        while new_id in used:
            new_id += 1
        for i in indices:
            self._cmds[i] = self._cmds[i].model_copy(
                update={"bind_id": new_id})
        self._refresh()

    def _unbind_selected(self):
        for i in self._selected_indices():
            self._cmds[i] = self._cmds[i].model_copy(update={"bind_id": 0})
        self._refresh()


# ---------------------------------------------------------------------------
# VnaConfigWindow
# ---------------------------------------------------------------------------

class VnaConfigWindow(QDialog):
    saved = Signal()

    def __init__(self, lib_reg: "VisaLibraryRegistry",
                 config_path: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("VNA Config")
        self.resize(860, 600)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self._lib_reg    = lib_reg
        self._config_path: Path = config_path or _DEFAULT_CONFIG_PATH
        self._cfg: VnaConfigData = load_vna_config(self._config_path)
        self._cur_sec_idx: int = -1

        self._build_ui()
        self._populate_sections()
        self._populate_acquire()

    def set_config_path(self, path: Path):
        """프로파일 변경 시 VnaWindow에서 호출 — 경로 업데이트 후 재로드."""
        if path != self._config_path:
            self._config_path = path
            self._cfg = load_vna_config(self._config_path)
            self._cur_sec_idx = -1
            self._populate_sections()
            self._populate_acquire()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    @staticmethod
    def _help_html() -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>VNA Config — 측정 순서와 명령 정하기</b><hr>"
            "VNA Control이 '한 번 측정(acquire)'할 때 어떤 명령을 어떤 순서로 보낼지 여기서 정합니다.<hr>"

            "<b>■ Sections 탭 (자유 실행 묶음)</b><br>"
            "버튼 한 번에 실행할 명령 묶음을 만들어 둡니다. 측정 전 장비 준비(초기화·설정 등)나 "
            "수동 조작에 씁니다. 여러 묶음을 만들어 필요할 때 각각 실행할 수 있습니다.<hr>"

            "<b>■ Acquire 탭 (한 번 측정의 4단계)</b><br>"
            "한 번 측정할 때 아래 순서대로 명령이 실행됩니다:<br>"
            "<table cellspacing='3' cellpadding='2'>"
            "<tr valign='top'><td><b>Sweep</b></td>"
            "<td>측정 전 보낼 설정값(예: 시작/끝 주파수). Sweep Acquire에서는 여기에 "
            "단계별 값이 들어갑니다.</td></tr>"
            "<tr valign='top'><td><b>Start</b></td>"
            "<td>측정을 <b>시작</b>시키는 명령 (트리거).</td></tr>"
            "<tr valign='top'><td><b>Wait (OPC)</b></td>"
            "<td>측정이 <b>끝날 때까지 기다리는</b> 명령. 장비에 '다 끝났니?'를 반복해 묻고, "
            "끝났다는 응답이 올 때까지 다음으로 넘어가지 않습니다.<br>"
            "이게 없으면 측정이 끝나기 전에 값을 읽어 엉뚱한 결과가 나올 수 있습니다.</td></tr>"
            "<tr valign='top'><td><b>Read</b></td>"
            "<td>결과(곡선 S11·S21 등)를 <b>읽어 오는</b> 명령. 자기장·온도 같은 "
            "한 개짜리 값도 함께 넣을 수 있고, 그 값은 곡선 길이에 맞춰 자동으로 채워집니다.<br>"
            "<b>Stride</b>: 점이 너무 많을 때 몇 개에 하나씩만 추리는 간격(1=전부).</td></tr>"
            "</table>"

            "<b>■ 각 명령 칸</b><br>"
            "&nbsp;• <b>Alias</b>: 어느 장비로 보낼지.<br>"
            "&nbsp;• <b>VISA Command</b>: 장비에 보낼 명령 글자.<br>"
            "&nbsp;• <b>Figure Axis / Unit</b>: 그래프·파일에 쓸 이름과 단위.<hr>"

            "<b>■ 참고</b><br>"
            "&nbsp;• 명령은 미리 <b>VISA Library</b>에 등록한 것 중에서 고릅니다.<br>"
            "&nbsp;• <b>Save &amp; Apply</b>로 저장하면 VNA Control에 바로 반영됩니다.<br>"
            "&nbsp;• 그래프에 무엇을 그릴지는 VNA Control 창에서 고릅니다."
            "</body></html>"
        )

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        from pythonization.ui.widgets.help_button import make_help_button
        hdr = QHBoxLayout()
        _t = QLabel("VNA Config")
        _t.setStyleSheet("font-weight: bold; font-size: 13px; color: #79c0ff;")
        hdr.addWidget(_t)
        hdr.addStretch()
        hdr.addWidget(make_help_button(self._help_html(), "VNA Config 도움말"))
        root.addLayout(hdr)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_sections_tab(), "Sections")
        self._tabs.addTab(self._build_acquire_tab(),  "Acquire")
        root.addWidget(self._tabs)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_save = QPushButton("Save && Apply")
        btn_save.setMinimumHeight(30)
        btn_save.setStyleSheet(
            "QPushButton{background:#1f4e8c;color:white;"
            "border-radius:4px;font-weight:bold;}"
        )
        btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(btn_save)
        root.addLayout(btn_row)

    # ---- Sections tab ------------------------------------------------

    def _build_sections_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_section_panel())
        splitter.addWidget(self._build_command_panel())
        splitter.setSizes([240, 560])

        lay.addWidget(splitter)
        return w

    def _build_section_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(4)

        lbl = QLabel("Sections")
        lbl.setStyleSheet("font-weight: bold;")
        lay.addWidget(lbl)

        self._lst_sections = QListWidget()
        self._lst_sections.setFont(_MONO)
        self._lst_sections.currentRowChanged.connect(self._on_section_selected)
        lay.addWidget(self._lst_sections, stretch=1)

        btn_bar = QHBoxLayout()
        for label, slot in [
            ("+", self._on_add_section),
            ("−", self._on_remove_section),
            ("✎", self._on_rename_section),
            ("↑", self._on_section_up),
            ("↓", self._on_section_down),
        ]:
            b = QPushButton(label)
            b.setFixedSize(28, 24)
            b.clicked.connect(slot)
            btn_bar.addWidget(b)
        btn_bar.addStretch()
        lay.addLayout(btn_bar)
        return w

    def _build_command_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(4)

        lbl = QLabel("Commands  (write only, double-click to edit)")
        lbl.setStyleSheet("font-weight: bold;")
        lay.addWidget(lbl)

        self._sec_cmd_list = _CmdListWidget(
            self._lib_reg, mode="write", with_bind=True)
        lay.addWidget(self._sec_cmd_list)
        return w

    # ---- Acquire tab -------------------------------------------------

    def _build_acquire_tab(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # Sweep Command
        sw_grp = QGroupBox(
            "Sweep Command  (write, [SWEEP] param → 스텝 값으로 대체)")
        sw_lay = QVBoxLayout(sw_grp)
        sw_lay.setContentsMargins(4, 8, 4, 4)
        self._acq_sweep = _CmdListWidget(self._lib_reg, mode="sweep", with_onoff=False)
        self._acq_sweep.setMaximumHeight(130)
        sw_lay.addWidget(self._acq_sweep)
        root.addWidget(sw_grp)

        # Start / Wait / Read / Error Check (4 columns)
        bottom = QWidget()
        bot_lay = QHBoxLayout(bottom)
        bot_lay.setContentsMargins(0, 0, 0, 0)
        bot_lay.setSpacing(8)

        for title, mode, attr in [
            ("Start Sweep  (write)", "write", "_acq_start"),
            ("Wait  (OPC query)",    "read",  "_acq_wait"),
            ("Read  (query)",        "read",  "_acq_read"),
        ]:
            grp = QGroupBox(title)
            grp_lay = QVBoxLayout(grp)
            grp_lay.setContentsMargins(4, 8, 4, 4)
            lst = _CmdListWidget(self._lib_reg, mode=mode)
            setattr(self, attr, lst)
            grp_lay.addWidget(lst)
            bot_lay.addWidget(grp, stretch=1)

        root.addWidget(bottom, stretch=1)
        return w

    # ------------------------------------------------------------------
    # Sections tab helpers
    # ------------------------------------------------------------------

    def _populate_sections(self):
        self._lst_sections.blockSignals(True)
        prev_row = self._lst_sections.currentRow()
        self._lst_sections.clear()
        for sec in self._cfg.sections:
            self._lst_sections.addItem(sec.name)
        self._lst_sections.blockSignals(False)
        target = prev_row if 0 <= prev_row < len(self._cfg.sections) else (
            0 if self._cfg.sections else -1)
        self._lst_sections.setCurrentRow(target)
        if target == self._cur_sec_idx:
            self._show_section_commands(target)

    def _on_section_selected(self, row: int):
        if 0 <= self._cur_sec_idx < len(self._cfg.sections):
            self._cfg.sections[self._cur_sec_idx].commands = \
                self._sec_cmd_list.get_commands()
        self._cur_sec_idx = row
        self._show_section_commands(row)

    def _show_section_commands(self, idx: int):
        if 0 <= idx < len(self._cfg.sections):
            self._sec_cmd_list.set_commands(
                self._cfg.sections[idx].commands)
        else:
            self._sec_cmd_list.set_commands([])

    def _flush_section_commands(self):
        if 0 <= self._cur_sec_idx < len(self._cfg.sections):
            self._cfg.sections[self._cur_sec_idx].commands = \
                self._sec_cmd_list.get_commands()

    # --- Section buttons ---

    def _on_add_section(self):
        name, ok = QInputDialog.getText(self, "Add Section", "Section name:")
        if ok and name.strip():
            self._flush_section_commands()
            self._cfg.sections.append(VnaSectionConfig(name=name.strip()))
            self._populate_sections()
            self._lst_sections.setCurrentRow(len(self._cfg.sections) - 1)

    def _on_remove_section(self):
        idx = self._lst_sections.currentRow()
        if 0 <= idx < len(self._cfg.sections):
            self._cfg.sections.pop(idx)
            self._cur_sec_idx = -1
            self._populate_sections()
            self._lst_sections.setCurrentRow(
                max(0, idx - 1) if self._cfg.sections else -1)

    def _on_rename_section(self):
        idx = self._lst_sections.currentRow()
        if not (0 <= idx < len(self._cfg.sections)):
            return
        name, ok = QInputDialog.getText(
            self, "Rename Section", "New name:",
            text=self._cfg.sections[idx].name)
        if ok and name.strip():
            self._cfg.sections[idx].name = name.strip()
            self._populate_sections()
            self._lst_sections.setCurrentRow(idx)

    def _on_section_up(self):
        self._flush_section_commands()
        idx = self._lst_sections.currentRow()
        if idx > 0:
            s = self._cfg.sections
            s[idx - 1], s[idx] = s[idx], s[idx - 1]
            self._cur_sec_idx = idx - 1
            self._populate_sections()
            self._lst_sections.setCurrentRow(idx - 1)

    def _on_section_down(self):
        self._flush_section_commands()
        idx = self._lst_sections.currentRow()
        if 0 <= idx < len(self._cfg.sections) - 1:
            s = self._cfg.sections
            s[idx], s[idx + 1] = s[idx + 1], s[idx]
            self._cur_sec_idx = idx + 1
            self._populate_sections()
            self._lst_sections.setCurrentRow(idx + 1)

    # ------------------------------------------------------------------
    # Acquire tab helpers
    # ------------------------------------------------------------------

    def _populate_acquire(self):
        self._acq_sweep.set_commands(self._cfg.acquire.sweep_cmds)
        self._acq_start.set_commands(self._cfg.acquire.start_cmds)
        self._acq_wait.set_commands(self._cfg.acquire.wait_cmds)
        self._acq_read.set_commands(self._cfg.acquire.read_cmds)

    def _flush_acquire(self):
        self._cfg.acquire.sweep_cmds = self._acq_sweep.get_commands()
        self._cfg.acquire.start_cmds = self._acq_start.get_commands()
        self._cfg.acquire.wait_cmds  = self._acq_wait.get_commands()
        self._cfg.acquire.read_cmds  = self._acq_read.get_commands()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _on_save(self):
        self._flush_section_commands()
        self._flush_acquire()
        save_vna_config(self._cfg, self._config_path)
        self.saved.emit()

    # ------------------------------------------------------------------

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)
