"""
VnaWindow: VNA 제어 창 (병렬 동작).

좌측: 섹션 Execute 패널 + Acquire 패널
우측: 두 개의 플롯 (좌우, 각각 x/y source 선택 가능, multi-y 지원)
"""
import os
import time as _time
from pathlib import Path
from typing import List, Optional, Tuple, TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSplitter, QVBoxLayout, QWidget,
)

from core.app_dirs import SETTINGS_DIR
from gui.vna_models import (
    VnaAcquireConfig, VnaCommandEntry, VnaConfigData,
    VnaPlotCurveConfig, VnaSectionConfig,
    build_cmd, format_label, get_figure_axis, get_template,
    load_vna_config, next_dat_path, next_sweep_folder,
    parse_vna_array, save_vna_config,
)

if TYPE_CHECKING:
    from core.instrument_session import InstrumentSession
    from core.visa_library_registry import VisaLibraryRegistry
    from core.profile_registry import ProfileRegistry

_MONO   = QFont("Consolas", 9)
_COLORS = ["#4ec9b0", "#ce9178", "#79c0ff", "#d2a8ff",
           "#ffa657", "#7ee787", "#f78166", "#a5d6ff"]
_DEFAULT_CONFIG_PATH = SETTINGS_DIR / "vna_config.yaml"

# unit_type → [(label, multiplier), ...]
_UNIT_OPTIONS: dict = {
    "Hz":  [("Hz", 1.0), ("kHz", 1e3), ("MHz", 1e6), ("GHz", 1e9)],
    "sec": [("ns", 1e-9), ("us", 1e-6), ("ms", 1e-3), ("s", 1.0)],
}
_UNIT_DEFAULT = {"Hz": "MHz", "sec": "ms"}

# Role button styles: key = role name (or "" for inactive)
_ROLE_BTN_INACTIVE = (
    "QPushButton{background:#252525;color:#555;border:1px solid #3a3a3a;"
    "border-radius:2px;font-size:8px;font-weight:bold;padding:0px;}"
    "QPushButton:hover{background:#333;color:#888;}"
)
_ROLE_BTN_STYLES = {
    "start":    ("QPushButton{background:#1f4e1b;color:#7ee787;border:1px solid #3d7a31;"
                 "border-radius:2px;font-size:8px;font-weight:bold;padding:0px;}"),
    "stop":     ("QPushButton{background:#4e1b1b;color:#f78166;border:1px solid #7a3131;"
                 "border-radius:2px;font-size:8px;font-weight:bold;padding:0px;}"),
    "n_points": ("QPushButton{background:#1b334e;color:#79c0ff;border:1px solid #315280;"
                 "border-radius:2px;font-size:8px;font-weight:bold;padding:0px;}"),
}


# ---------------------------------------------------------------------------
# Section execute worker
# ---------------------------------------------------------------------------

class _VnaWorker(QThread):
    done  = Signal(list)
    error = Signal(str)

    def __init__(self, session, commands: List[Tuple[str, str, bool]]):
        super().__init__()
        self._session  = session
        self._commands = commands

    def run(self):
        results = []
        try:
            for alias, cmd, is_query in self._commands:
                if not self._session.is_open(alias):
                    self._session.open(alias)
                if is_query:
                    r = self._session.query(alias, cmd)
                    results.append((True, str(r).strip()))
                else:
                    self._session.write(alias, cmd)
                    results.append((False, None))
            self.done.emit(results)
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Acquire worker  (Single & Sweep)
# ---------------------------------------------------------------------------

class _AcquireWorker(QObject):
    step_done   = Signal(int, list)   # (step_idx, [np.ndarray, ...])  — 정규화된 배열
    step_timing = Signal(int, float)  # (step_idx, remaining_sec)  양수=idle, 음수=overrun
    step_elapsed = Signal(int, float)  # (step_idx, elapsed_sec)  한 스텝 acquire 소요 시간
    finished    = Signal()
    error       = Signal(str)
    progress    = Signal(str)

    def __init__(self, session, lib_reg, acq_cfg: VnaAcquireConfig,
                 sweep_values=None,
                 sweep_cmd: Optional[VnaCommandEntry] = None,
                 time_mode: bool = False,
                 time_interval: float = 1.0,
                 time_count: int = 1):
        super().__init__()
        self._session      = session
        self._lib_reg      = lib_reg
        self._acq          = acq_cfg
        self._sweep_values = sweep_values
        self._sweep_cmd    = sweep_cmd   # 선택된 단일 sweep 명령어
        self._time_mode    = time_mode
        self._time_interval = time_interval
        self._time_count    = time_count
        self._stop_flag    = False
        self._ref_len: Optional[int] = None   # 첫 스텝에서 확정된 기준 array 길이

    def stop(self):
        self._stop_flag = True

    @Slot()
    def run(self):
        try:
            if self._time_mode:
                self._run_time_mode()
            else:
                self._run_value_mode()
        except Exception as e:
            self.error.emit(f"Fatal: {type(e).__name__}: {e}")
        finally:
            self.finished.emit()   # 항상 emit — 정상/에러/중단 모두

    def _run_value_mode(self):
        values = self._sweep_values if self._sweep_values is not None else [None]
        for i, sv in enumerate(values):
            if self._stop_flag:
                break
            sv_str = f"{sv:.6g}" if sv is not None else ""
            self.progress.emit(
                f"Step {i + 1}/{len(values)}"
                + (f"  val={sv_str}" if sv_str else ""))
            t0 = _time.perf_counter()
            try:
                sweep_list = ([self._sweep_cmd] if self._sweep_cmd is not None
                              else self._acq.sweep_cmds)
                self._exec_write_cmds(sweep_list, sv_str)
                self._exec_write_cmds(self._acq.start_cmds, sv_str)
                self._exec_wait_cmds(self._acq.wait_cmds)
                arrays = self._exec_read_cmds(self._acq.read_cmds)
                arrays = self._normalize_arrays(arrays, first_step=(i == 0))
                self.step_elapsed.emit(i, _time.perf_counter() - t0)
                self.step_done.emit(i, arrays)
            except Exception as e:
                self.error.emit(f"Step {i + 1}: {type(e).__name__}: {e}")
                break

    def _run_time_mode(self):
        """일정 간격마다 acquire 1회씩 time_count번 반복.

        각 스텝의 read 완료까지 소요 시간을 측정해:
          remaining = interval - elapsed
          remaining > 0 → idle (그만큼 대기), remaining < 0 → overrun(부족분)
        """
        n = max(1, self._time_count)
        interval = max(0.0, self._time_interval)
        for i in range(n):
            if self._stop_flag:
                break
            self.progress.emit(f"Time step {i + 1}/{n}")
            t0 = _time.perf_counter()
            try:
                # 단일 acquire와 동일한 cycle (sweep_cmds[빈값] → start → wait → read)
                self._exec_write_cmds(self._acq.sweep_cmds, "")
                self._exec_write_cmds(self._acq.start_cmds, "")
                self._exec_wait_cmds(self._acq.wait_cmds)
                arrays = self._exec_read_cmds(self._acq.read_cmds)
                arrays = self._normalize_arrays(arrays, first_step=(i == 0))
                self.step_done.emit(i, arrays)
            except Exception as e:
                self.error.emit(f"Time step {i + 1}: {type(e).__name__}: {e}")
                break
            elapsed   = _time.perf_counter() - t0
            remaining = interval - elapsed
            self.step_timing.emit(i, remaining)
            # 남는 시간만큼 대기 (마지막 스텝 제외). stop을 빠르게 반영하도록 분할 sleep.
            if i < n - 1 and remaining > 0:
                deadline = _time.perf_counter() + remaining
                while not self._stop_flag and _time.perf_counter() < deadline:
                    _time.sleep(min(0.05, deadline - _time.perf_counter()))

    def _normalize_arrays(self, arrays: List[np.ndarray],
                          first_step: bool) -> List[np.ndarray]:
        """단일값은 기준 길이로 broadcast, array 크기 불일치는 첫 스텝에서만 감지.

        - len>1 인 array들이 서로 크기가 다르면 (첫 스텝) → ValueError (측정 중단).
        - len<=1 (단일값) → 기준 길이로 복사 확장.
        - 기준 길이는 첫 스텝에서 확정되어 이후 스텝에 재사용 (재감지 안 함).
        """
        if first_step:
            multi = [len(a) for a in arrays if len(a) > 1]
            if multi:
                ref = multi[0]
                if any(m != ref for m in multi):
                    raise ValueError(
                        f"read array 크기 불일치: {sorted(set(multi))} — "
                        "array 컬럼들의 길이가 같아야 합니다.")
            else:
                ref = max((len(a) for a in arrays), default=1) or 1
            self._ref_len = ref
        ref = self._ref_len or 1
        out: List[np.ndarray] = []
        for a in arrays:
            if len(a) == ref:
                out.append(a)
            elif len(a) <= 1:
                val = float(a[0]) if len(a) == 1 else float("nan")
                out.append(np.full(ref, val))
            else:
                # 첫 스텝 이후 길이가 기준과 다른 multi array: 단순 자르기/패딩 없이 그대로 둠
                out.append(a)
        return out

    # ------------------------------------------------------------------
    def _exec_write_cmds(self, cmds: List[VnaCommandEntry], sv_str: str):
        for entry in cmds:
            if not entry.enabled:
                continue
            lib      = self._lib_reg.get_library(entry.alias)
            template = get_template(lib, entry)
            if template is None:
                raise ValueError(f"Template not found: '{entry.description}'")
            cmd = build_cmd(template, entry.params, sv_str)
            if not self._session.is_open(entry.alias):
                self._session.open(entry.alias)
            self._session.write(entry.alias, cmd)

    def _exec_wait_cmds(self, cmds: List[VnaCommandEntry]):
        """OPC 쿼리: 응답에 '1'이 포함될 때까지 polling."""
        for entry in cmds:
            if not entry.enabled:
                continue
            lib      = self._lib_reg.get_library(entry.alias)
            template = get_template(lib, entry)
            if template is None:
                raise ValueError(f"Template not found: '{entry.description}'")
            cmd = build_cmd(template, entry.params, "")
            if not self._session.is_open(entry.alias):
                self._session.open(entry.alias)
            while not self._stop_flag:
                response = str(self._session.query(entry.alias, cmd)).strip().lstrip('+')
                if response == "1":
                    break
                _time.sleep(0.1)

    def _exec_read_cmds(self, cmds: List[VnaCommandEntry]) -> List[np.ndarray]:
        arrays = []
        for entry in cmds:
            if not entry.enabled:
                arrays.append(np.array([]))
                continue
            lib      = self._lib_reg.get_library(entry.alias)
            template = get_template(lib, entry)
            if template is None:
                raise ValueError(f"Template not found: '{entry.description}'")
            cmd = build_cmd(template, entry.params, "")
            if not self._session.is_open(entry.alias):
                self._session.open(entry.alias)
            raw = self._session.query(entry.alias, cmd)
            arr = parse_vna_array(str(raw).strip())
            if entry.read_stride > 1:
                arr = arr[::entry.read_stride]
            arrays.append(arr)
        return arrays


# ---------------------------------------------------------------------------
# Per-command row (control section panel)
# ---------------------------------------------------------------------------

class _CmdRowWidget(QWidget):
    role_toggle = Signal(object, str)   # (self, role: "start"|"stop"|"n_points")

    def __init__(self, entry: VnaCommandEntry, lib, parent=None):
        super().__init__(parent)
        self._entry        = entry
        self._lib          = lib
        self._le_user:     Optional[QLineEdit] = None
        self._cb_unit:     Optional[QComboBox] = None
        self._role_btns:   dict = {}
        self._current_role: str = entry.sweep_role
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._cb = QCheckBox()
        self._cb.setChecked(self._entry.enabled)
        self._cb.setFixedWidth(18)
        lay.addWidget(self._cb)

        lbl = QLabel(format_label(
            get_figure_axis(self._lib, self._entry), self._entry.params, ""))
        lbl.setFont(_MONO)
        lay.addWidget(lbl, stretch=1)

        if any(p.is_user_input for p in self._entry.params):
            self._le_user = QLineEdit()
            self._le_user.setFont(_MONO)
            self._le_user.setFixedWidth(80)
            up = next(p for p in self._entry.params if p.is_user_input)
            self._le_user.setPlaceholderText(f"[{up.name}]")
            # 저장된 값 복원
            if up.value:
                self._le_user.setText(up.value)
            lay.addWidget(self._le_user)

            # Unit combo (unit_type이 설정된 경우만)
            if self._entry.unit_type in _UNIT_OPTIONS:
                self._cb_unit = QComboBox()
                self._cb_unit.setFont(_MONO)
                self._cb_unit.setFixedWidth(54)
                for label, mult in _UNIT_OPTIONS[self._entry.unit_type]:
                    self._cb_unit.addItem(label, mult)
                # 저장된 단위 복원, 없으면 기본값
                restore_lbl = self._entry.unit_label or _UNIT_DEFAULT.get(self._entry.unit_type, "")
                idx = self._cb_unit.findText(restore_lbl)
                if idx >= 0:
                    self._cb_unit.setCurrentIndex(idx)
                lay.addWidget(self._cb_unit)

        # Role buttons — user_input이 있는 entry에만 표시
        if any(p.is_user_input for p in self._entry.params):
            _tooltips = {"start": "Start로 지정", "stop": "Stop으로 지정", "n_points": "N Points로 지정"}
            for role, label in [("start", "S"), ("stop", "E"), ("n_points", "N")]:
                btn = QPushButton(label)
                btn.setFixedSize(18, 18)
                btn.setToolTip(_tooltips[role])
                btn.clicked.connect(lambda _checked, r=role: self.role_toggle.emit(self, r))
                self._role_btns[role] = btn
                lay.addWidget(btn)
            self._update_role_buttons()

    # ------------------------------------------------------------------
    # Role helpers
    # ------------------------------------------------------------------

    def set_role(self, role: str):
        """외부에서 role 할당/해제 시 호출."""
        self._current_role = role
        self._update_role_buttons()

    def _update_role_buttons(self):
        for r, btn in self._role_btns.items():
            btn.setStyleSheet(
                _ROLE_BTN_STYLES[r] if r == self._current_role else _ROLE_BTN_INACTIVE
            )

    def get_command(self) -> Optional[Tuple[str, str, bool]]:
        if not self._cb.isChecked():
            return None
        template = get_template(self._lib, self._entry)
        if template is None:
            return None

        raw = self._le_user.text().strip() if self._le_user else ""
        if self._cb_unit is not None and raw:
            try:
                multiplier = self._cb_unit.currentData()
                user_val = f"{float(raw) * multiplier:.10g}"
            except ValueError:
                user_val = raw
        else:
            user_val = raw

        try:
            cmd = build_cmd(template, self._entry.params, user_val)
        except ValueError:
            return None
        return (self._entry.alias, cmd, self._entry.cmd_type == "query")

    def get_entry_state(self) -> "VnaCommandEntry":
        """현재 UI 상태(체크박스, 입력값, 단위)를 entry에 반영하여 반환."""
        raw = self._le_user.text().strip() if self._le_user else ""
        unit_label = self._cb_unit.currentText() if self._cb_unit else ""
        new_params = [
            p.model_copy(update={"value": raw}) if p.is_user_input else p
            for p in self._entry.params
        ]
        return self._entry.model_copy(update={
            "enabled":    self._cb.isChecked(),
            "params":     new_params,
            "unit_label": unit_label,
            "sweep_role": self._current_role,
        })


# ---------------------------------------------------------------------------
# Section widget (control panel) — supports bind_user_inputs
# ---------------------------------------------------------------------------

class _SectionWidget(QGroupBox):
    execute_requested     = Signal(list)
    role_toggle_requested = Signal(object, str)   # (row, role) — _CmdRowWidget에서 pass-through

    def __init__(self, sec_cfg: VnaSectionConfig,
                 lib_reg: "VisaLibraryRegistry", parent=None):
        super().__init__(sec_cfg.name, parent)
        self._sec_cfg = sec_cfg
        self._lib_reg = lib_reg
        self._cmd_rows: List[_CmdRowWidget] = []
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(2)
        lay.setContentsMargins(6, 10, 6, 6)

        self._rows_lay = QVBoxLayout()
        self._rows_lay.setSpacing(2)
        lay.addLayout(self._rows_lay)

        # Build rows and apply bind_id grouping
        # bind_id > 0: first occurrence per group is leader (active),
        #              subsequent occurrences are followers (read-only, synced)
        leaders: dict = {}   # bind_id → _CmdRowWidget (leader)

        for entry in self._sec_cfg.commands:
            try:
                lib = self._lib_reg.get_library(entry.alias)
            except Exception:
                continue
            row = _CmdRowWidget(entry, lib, self)
            row.role_toggle.connect(self.role_toggle_requested)
            self._cmd_rows.append(row)
            self._rows_lay.addWidget(row)

            bid = entry.bind_id
            if bid > 0 and row._le_user is not None:
                if bid not in leaders:
                    leaders[bid] = row
                else:
                    # Follower — read-only input, synced from leader
                    row._le_user.setReadOnly(True)
                    row._le_user.setStyleSheet(
                        "color: #888; background: #1a1a1a;")
                    leader = leaders[bid]

                    def _make_sync(follower):
                        def _sync(text):
                            follower._le_user.setText(text)
                        return _sync
                    leader._le_user.textChanged.connect(_make_sync(row))

                    # Unit combo도 동기화 (둘 다 unit이 있는 경우)
                    if leader._cb_unit is not None and row._cb_unit is not None:
                        row._cb_unit.setEnabled(False)

                        def _make_unit_sync(follower_cb):
                            def _sync_unit(idx):
                                follower_cb.setCurrentIndex(idx)
                            return _sync_unit
                        leader._cb_unit.currentIndexChanged.connect(
                            _make_unit_sync(row._cb_unit))

        btn_bar = QHBoxLayout()
        btn_bar.addStretch()
        btn_exec = QPushButton("▶ Execute")
        btn_exec.setFixedHeight(24)
        btn_exec.setStyleSheet(
            "QPushButton{background:#1f4e8c;color:white;"
            "border-radius:3px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        btn_exec.clicked.connect(self._on_execute)
        btn_bar.addWidget(btn_exec)
        lay.addLayout(btn_bar)

    def _on_execute(self):
        cmds = [row.get_command() for row in self._cmd_rows]
        self.execute_requested.emit([c for c in cmds if c])

    def get_updated_config(self) -> VnaSectionConfig:
        """현재 UI 상태를 반영한 VnaSectionConfig 반환 (저장용)."""
        updated_cmds = [row.get_entry_state() for row in self._cmd_rows]
        return self._sec_cfg.model_copy(update={"commands": updated_cmds})


# ---------------------------------------------------------------------------
# Y-curve row (one y-axis selector + curve per row in _PlotPanel)
# ---------------------------------------------------------------------------

class _YCurveRow(QWidget):
    remove_requested = Signal(object)   # emits self
    source_changed   = Signal()

    def __init__(self, color: str, sources: List[str], y_src: str,
                 plot_widget: pg.PlotWidget, parent=None):
        super().__init__(parent)
        self._color = color
        self._plot  = plot_widget

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        # color swatch
        swatch = QLabel("●")
        swatch.setStyleSheet(f"color: {color}; font-size: 14px;")
        swatch.setFixedWidth(18)
        lay.addWidget(swatch)

        # y combo
        self._cb_y = QComboBox()
        self._cb_y.setFont(_MONO)
        for s in sources:
            self._cb_y.addItem(s, s)
        if y_src:
            idx = self._cb_y.findData(y_src)
            if idx >= 0:
                self._cb_y.setCurrentIndex(idx)
        self._cb_y.currentIndexChanged.connect(self.source_changed.emit)
        lay.addWidget(self._cb_y, stretch=1)

        # remove button
        btn_rm = QPushButton("✕")
        btn_rm.setFixedSize(20, 20)
        btn_rm.clicked.connect(lambda: self.remove_requested.emit(self))
        lay.addWidget(btn_rm)

        # pyqtgraph curve
        self._curve = plot_widget.plot(pen=pg.mkPen(color, width=1.5))
        self._curve.setDownsampling(auto=True, method='peak')
        self._curve.setClipToView(True)

    def y_source(self) -> str:
        return self._cb_y.currentData() or ""

    def update_sources(self, sources: List[str]):
        prev = self._cb_y.currentData()
        self._cb_y.blockSignals(True)
        self._cb_y.clear()
        for s in sources:
            self._cb_y.addItem(s, s)
        idx = self._cb_y.findData(prev)
        if idx >= 0:
            self._cb_y.setCurrentIndex(idx)
        self._cb_y.blockSignals(False)

    def set_data(self, x: np.ndarray, y: np.ndarray):
        mn = min(len(x), len(y))
        if mn > 0:
            self._curve.setData(x[:mn], y[:mn], skipFiniteCheck=True)
        else:
            self._curve.setData([], [])

    def clear_data(self):
        self._curve.setData([], [])

    def remove_from_plot(self):
        self._plot.removeItem(self._curve)


# ---------------------------------------------------------------------------
# Plot panel  (x-selector + multi-y rows + PlotWidget, 1:1 aspect hint)
# ---------------------------------------------------------------------------

class _PlotPanel(QFrame):
    """독립적인 x source 선택 + multi-y curve + pyqtgraph PlotWidget."""

    def __init__(self, start_color_idx: int = 0, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._data: dict = {}
        self._sources: List[str] = []
        self._y_rows: List[_YCurveRow] = []
        self._color_idx = start_color_idx
        self._build()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return w

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)

        # x selector + Add Y button
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(4)
        ctrl_row.addWidget(QLabel("x:"))
        self._cb_x = QComboBox()
        self._cb_x.setFont(_MONO)
        self._cb_x.setMinimumWidth(80)
        self._cb_x.currentIndexChanged.connect(self._replot_all)
        ctrl_row.addWidget(self._cb_x, stretch=1)

        btn_add = QPushButton("＋Y")
        btn_add.setFixedHeight(20)
        btn_add.setFixedWidth(36)
        btn_add.setToolTip("y 곡선 추가")
        btn_add.clicked.connect(lambda: self._add_y_row())
        ctrl_row.addWidget(btn_add)

        btn_fit = QPushButton("Fit")
        btn_fit.setFixedHeight(20)
        btn_fit.setFixedWidth(36)
        btn_fit.setToolTip("현재 표시된 데이터에 맞춰 X·Y 범위를 한 번 자동 맞춤\n"
                           "(sweep 중 신호가 화면 밖으로 나갔을 때 사용)")
        btn_fit.clicked.connect(self.auto_range)
        ctrl_row.addWidget(btn_fit)
        lay.addLayout(ctrl_row)

        # y rows container
        self._y_container = QWidget()
        self._y_lay = QVBoxLayout(self._y_container)
        self._y_lay.setContentsMargins(0, 0, 0, 0)
        self._y_lay.setSpacing(2)
        lay.addWidget(self._y_container)

        # plot
        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        lay.addWidget(self._plot, stretch=1)

        # start with one y row (after plot is created)
        self._add_y_row()

    def _next_color(self) -> str:
        c = _COLORS[self._color_idx % len(_COLORS)]
        self._color_idx += 1
        return c

    def _add_y_row(self, y_src: str = ""):
        color = self._next_color()
        row = _YCurveRow(color, self._sources, y_src, self._plot, self._y_container)
        row.remove_requested.connect(self._remove_y_row)
        row.source_changed.connect(self._replot_all)
        self._y_rows.append(row)
        self._y_lay.addWidget(row)
        self._replot_all()

    def _remove_y_row(self, row: _YCurveRow):
        if len(self._y_rows) <= 1:
            return   # keep at least one
        self._y_rows.remove(row)
        row.remove_from_plot()
        row.setParent(None)
        row.deleteLater()
        self._replot_all()

    # ------------------------------------------------------------------
    def update_sources(self, sources: List[str]):
        self._sources = list(sources)
        prev = self._cb_x.currentData()
        self._cb_x.blockSignals(True)
        self._cb_x.clear()
        for s in sources:
            self._cb_x.addItem(s, s)
        ix = self._cb_x.findData(prev)
        if ix >= 0:
            self._cb_x.setCurrentIndex(ix)
        self._cb_x.blockSignals(False)
        for row in self._y_rows:
            row.update_sources(sources)
        self._replot_all()

    def set_default_x(self, x_src: str):
        ix = self._cb_x.findData(x_src)
        if ix >= 0:
            self._cb_x.setCurrentIndex(ix)

    def set_default_y(self, y_src: str):
        """첫 번째 y row의 y source 설정."""
        if self._y_rows:
            idx = self._y_rows[0]._cb_y.findData(y_src)
            if idx >= 0:
                self._y_rows[0]._cb_y.setCurrentIndex(idx)

    def push_data(self, data: dict, first_step: bool = False):
        self._data = data
        if first_step:
            # 뷰 범위를 먼저 설정 → setData 시 ClipToView가 올바른 범위로 클리핑함
            self._fit_view()
        self._replot_all()

    def auto_range(self):
        self._fit_view()

    def _fit_view(self):
        """raw self._data에서 직접 min/max 계산 → x, y 독립적으로 뷰 범위 설정.
        enableAutoRange()는 ClipToView와 충돌(닭-달걀)하므로 사용하지 않음."""
        x_src = self._cb_x.currentData() or ""
        xd = self._data.get(x_src, np.array([]))

        all_y: List[np.ndarray] = []
        for row in self._y_rows:
            yd = self._data.get(row.y_source(), np.array([]))
            if len(yd):
                all_y.append(yd)

        if len(xd):
            xmin, xmax = float(np.nanmin(xd)), float(np.nanmax(xd))
            if xmin != xmax:
                self._plot.setXRange(xmin, xmax, padding=0.05)

        if all_y:
            yd_cat = np.concatenate(all_y)
            ymin, ymax = float(np.nanmin(yd_cat)), float(np.nanmax(yd_cat))
            if ymin != ymax:
                self._plot.setYRange(ymin, ymax, padding=0.05)

    def clear_data(self):
        self._data = {}
        for row in self._y_rows:
            row.clear_data()
        self._plot.setLabel("bottom", "")
        self._plot.setLabel("left", "")

    def _replot_all(self):
        x_src = self._cb_x.currentData() or ""
        xd = self._data.get(x_src, np.array([]))
        y_labels = []
        for row in self._y_rows:
            y_src = row.y_source()
            yd = self._data.get(y_src, np.array([]))
            row.set_data(xd, yd)
            if y_src:
                y_labels.append(y_src)
        self._plot.setLabel("bottom", x_src)
        if y_labels:
            self._plot.setLabel("left", y_labels[0])

    def x_source(self) -> str:
        return self._cb_x.currentData() or "index"

    def y_sources(self) -> List[str]:
        return [row.y_source() for row in self._y_rows]

    def to_config(self) -> VnaPlotCurveConfig:
        srcs = self.y_sources()
        return VnaPlotCurveConfig(
            x_source=self.x_source(),
            y_source=srcs[0] if srcs else "",
            y_sources=srcs,
        )

    def restore_config(self, cfg: VnaPlotCurveConfig):
        ix = self._cb_x.findData(cfg.x_source)
        if ix >= 0:
            self._cb_x.blockSignals(True)
            self._cb_x.setCurrentIndex(ix)
            self._cb_x.blockSignals(False)

        y_srcs = cfg.y_sources if cfg.y_sources else (
            [cfg.y_source] if cfg.y_source else [])
        if not y_srcs:
            self._replot_all()
            return

        # Remove excess rows (keep at least 1)
        while len(self._y_rows) > 1:
            row = self._y_rows.pop()
            row.remove_from_plot()
            row.setParent(None)
            row.deleteLater()

        # Set first row
        if self._y_rows:
            row0 = self._y_rows[0]
            row0.update_sources(self._sources)
            idx0 = row0._cb_y.findData(y_srcs[0])
            if idx0 >= 0:
                row0._cb_y.blockSignals(True)
                row0._cb_y.setCurrentIndex(idx0)
                row0._cb_y.blockSignals(False)

        # Add additional rows
        for src in y_srcs[1:]:
            self._add_y_row(src)

        self._replot_all()


# ---------------------------------------------------------------------------
# VNA Window
# ---------------------------------------------------------------------------

class VnaWindow(QDialog):

    def __init__(self, session: "InstrumentSession",
                 lib_reg: "VisaLibraryRegistry",
                 param_manager_reg: Optional["ProfileRegistry"] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("VNA Control")
        self.resize(1500, 780)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self._session            = session
        self._lib_reg            = lib_reg
        self._param_manager_reg  = param_manager_reg

        self._cfg      = load_vna_config(self._config_path())

        self._sec_worker: Optional[_VnaWorker]     = None
        self._acq_worker: Optional[_AcquireWorker] = None
        self._acq_thread: Optional[QThread]        = None
        self._section_widgets: List[_SectionWidget] = []
        self._current_sweep_values = None
        self._step_count: int = 0
        self._sweep_folder: Optional[Path] = None
        self._sweep_figure_axis: str = ""
        self._config_win = None
        # Section role management  (start / stop / n_points)
        self._role_rows: dict = {"start": None, "stop": None, "n_points": None}
        self._linspace_data: Optional[Tuple[str, str, np.ndarray]] = None
        # 알람 (VNA sweep 전용 — 텔레그램)
        from core.alarm_manager import AlarmManager
        self._alarm_manager = AlarmManager()
        self._time_mode_running: bool = False
        # Sweep 콤보에 '⏱ Time' 항목을 두고, 저장된 time_mode면 최초 1회 그 항목을 선택
        self._pending_time_select: bool = bool(self._cfg.acquire.time_mode)

        self._build_ui()
        self._rebuild_sections()
        self._restore_plot_config()

    # ------------------------------------------------------------------
    # Profile-based config path
    # ------------------------------------------------------------------

    def _config_path(self) -> Path:
        if self._param_manager_reg is not None:
            name = self._param_manager_reg.active_name
            vna_dir = SETTINGS_DIR / "profiles" / "vna"
            vna_dir.mkdir(parents=True, exist_ok=True)
            return vna_dir / f"{name}.yaml"
        return _DEFAULT_CONFIG_PATH

    def on_profile_changed(self):
        """main_window에서 프로파일 변경 시 호출 — 새 프로파일 로드.
        저장은 caller(_save_current_to_active_profile)에서 이미 처리함."""
        self._cfg = load_vna_config(self._config_path())
        self._le_filename.setText(self._cfg.acquire.filename)
        self._le_sw_start.setText(str(self._cfg.acquire.sweep_start))
        self._le_sw_stop.setText(str(self._cfg.acquire.sweep_stop))
        self._le_sw_n.setText(str(self._cfg.acquire.sweep_n))
        self._le_main.setText(self._cfg.main_folder)
        self._le_sub.setText(self._cfg.sub_folder)
        self._cb_save.setChecked(self._cfg.save_enabled)
        self._rebuild_sections()
        self._populate_sweep_cmds()
        self._refresh_plot_sources()
        self._restore_plot_config()
        # 그래프/데이터 초기화 (프로파일 전환 시)
        self._step_count = 0
        self._panel_L.clear_data()
        self._panel_R.clear_data()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        root.addWidget(self._build_top_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([340, 1160])
        root.addWidget(splitter)

    def _build_top_bar(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setMaximumHeight(34)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(6)

        btn_cfg = QPushButton("⚙ Config")
        btn_cfg.setFixedHeight(22)
        btn_cfg.clicked.connect(self._open_config)
        lay.addWidget(btn_cfg)

        btn_alarm = QPushButton("⚙ Alarm")
        btn_alarm.setFixedHeight(22)
        btn_alarm.setToolTip("VNA sweep 알람 설정 (텔레그램) — 측정 오류·완료 시 알림")
        btn_alarm.clicked.connect(self._open_alarm_config)
        lay.addWidget(btn_alarm)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #30363d;")
        lay.addWidget(sep)

        self._cb_save = QCheckBox("Save")
        self._cb_save.setChecked(self._cfg.save_enabled)
        lay.addWidget(self._cb_save)

        lay.addWidget(QLabel("Main:"))
        self._le_main = QLineEdit(self._cfg.main_folder)
        self._le_main.setFont(_MONO)
        self._le_main.setFixedHeight(22)
        self._le_main.setMinimumWidth(180)
        lay.addWidget(self._le_main, stretch=1)

        btn_browse = QPushButton("…")
        btn_browse.setFixedSize(24, 22)
        btn_browse.setToolTip("Browse main folder")
        btn_browse.clicked.connect(self._on_browse)
        lay.addWidget(btn_browse)

        lay.addWidget(QLabel("Sub:"))
        self._le_sub = QLineEdit(self._cfg.sub_folder)
        self._le_sub.setFont(_MONO)
        self._le_sub.setFixedHeight(22)
        self._le_sub.setFixedWidth(110)
        lay.addWidget(self._le_sub)

        btn_open = QPushButton("📂")
        btn_open.setFixedSize(24, 22)
        btn_open.setToolTip("Open save folder")
        btn_open.clicked.connect(self._on_open_folder)
        lay.addWidget(btn_open)

        from gui.help_button import make_help_button
        lay.addWidget(make_help_button(self._control_help_html(), "VNA Control 도움말"))
        return frame

    @staticmethod
    def _control_help_html() -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>VNA Control — 한 번에 여러 점(배열) 측정</b><hr>"
            "보통 측정은 한 점씩 읽지만, VNA처럼 <b>한 번에 곡선(여러 점)을 통째로</b> 받아오는 "
            "장비를 다루는 창입니다. 받은 곡선을 그래프로 보고 파일로 저장합니다.<hr>"

            "<b>■ 측정 한 번(acquire)의 진행</b><br>"
            "&nbsp;① 필요한 설정값을 장비에 보냄 → ② 측정 시작 명령 → "
            "③ <b>측정이 끝날 때까지 기다림</b>(OPC) → ④ 곡선(S11·S21 등)을 읽어 옴.<br>"
            "이 순서와 명령들은 <b>⚙ Config</b>에서 미리 정합니다.<hr>"

            "<b>■ Single / Sweep / Time 모드</b><br>"
            "&nbsp;• <b>Single Acquire</b>: 위 한 번만 실행.<br>"
            "&nbsp;• <b>Sweep Acquire</b>: <b>Sweep:</b> 칸에서 고른 값을 Start→Stop으로 N번 "
            "바꿔가며 매번 한 번씩 측정 (예: 자기장을 바꿔가며 곡선 여러 장).<br>"
            "&nbsp;• <b>Sweep: 칸에서 ⏱ Time 선택</b> 시: 값을 바꾸지 않고 <b>일정 시간 간격</b>마다 "
            "한 번씩 정해진 횟수(Count)만큼 측정 (버튼이 ‘▶ Time Sweep’으로 바뀜).<br>"
            "&nbsp;&nbsp;– 측정이 간격보다 빨리 끝나면 남는 시간을 <b>Idle</b>(초록)로,<br>"
            "&nbsp;&nbsp;– 더 오래 걸리면 부족분을 <b>Overrun</b>(빨강, 음수)으로 표시합니다.<hr>"

            "<b>■ 단일값 자동 맞춤</b><br>"
            "곡선(예: 201점)과 함께 자기장·온도 같은 <b>한 개짜리 값</b>을 같이 읽으면, "
            "그 값을 곡선 길이에 맞춰 자동으로 채워 길이를 맞춥니다.<br>"
            "단, 곡선이 여러 개인데 <b>서로 길이가 다르면</b> 첫 측정에서 오류로 알리고 멈춥니다.<hr>"

            "<b>■ 상단 버튼/입력</b><br>"
            "&nbsp;• <b>⚙ Config</b>: 측정 순서·명령·그래프 설정 (자세한 도움말은 그 창에 있음).<br>"
            "&nbsp;• <b>⚙ Alarm</b>: 측정 오류·완료 시 텔레그램 알림.<br>"
            "&nbsp;• <b>Save</b> / <b>Main·Sub</b>: 저장 켜기와 저장 폴더.<hr>"

            "<b>■ 그래프(우측)</b><br>"
            "왼쪽·오른쪽 두 개의 그래프에 각각 가로축·세로축을 골라 곡선을 그립니다. "
            "한 그래프에 여러 곡선을 겹쳐 볼 수 있습니다."
            "</body></html>"
        )

    # ---- Left panel ---------------------------------------------------

    def _build_left_panel(self) -> QWidget:
        outer = QWidget()
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._sections_content = QWidget()
        self._sections_lay = QVBoxLayout(self._sections_content)
        self._sections_lay.setSpacing(6)
        self._sections_lay.setContentsMargins(4, 4, 4, 4)
        self._sections_lay.addStretch()
        scroll.setWidget(self._sections_content)
        lay.addWidget(scroll, stretch=1)

        lay.addWidget(self._build_acquire_group())

        self._lbl_status = QLabel("")
        self._lbl_status.setFont(QFont("Consolas", 8))
        self._lbl_status.setStyleSheet("color: #888;")
        self._lbl_status.setWordWrap(True)
        lay.addWidget(self._lbl_status)
        return outer

    def _build_acquire_group(self) -> QGroupBox:
        grp = QGroupBox("Acquire")
        lay = QVBoxLayout(grp)
        lay.setSpacing(4)
        lay.setContentsMargins(8, 10, 8, 8)

        # Filename
        fn_row = QHBoxLayout()
        fn_row.addWidget(QLabel("Filename:"))
        self._le_filename = QLineEdit(self._cfg.acquire.filename)
        self._le_filename.setFont(_MONO)
        self._le_filename.setPlaceholderText("vna_data")
        fn_row.addWidget(self._le_filename, stretch=1)
        lay.addLayout(fn_row)

        # Single acquire button
        self._btn_single = QPushButton("▶ Single Acquire")
        self._btn_single.setFixedHeight(26)
        self._btn_single.setStyleSheet(
            "QPushButton{background:#2d5a1b;color:white;"
            "border-radius:3px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        self._btn_single.clicked.connect(self._on_single_acquire)
        lay.addWidget(self._btn_single)

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet("color: #30363d;")
        lay.addWidget(div)

        # Sweep command selector
        sw_cmd_row = QHBoxLayout()
        sw_cmd_row.addWidget(QLabel("Sweep:"))
        self._combo_sweep_cmd = QComboBox()
        self._combo_sweep_cmd.setFont(_MONO)
        self._combo_sweep_cmd.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._combo_sweep_cmd.currentIndexChanged.connect(
            self._on_sweep_cmd_changed)
        sw_cmd_row.addWidget(self._combo_sweep_cmd, stretch=1)
        lay.addLayout(sw_cmd_row)

        # Sweep params (Start / Stop / N) with optional unit combos
        sw_row = QHBoxLayout()
        sw_row.setSpacing(4)

        sw_row.addWidget(QLabel("Start:"))
        self._le_sw_start = QLineEdit(str(self._cfg.acquire.sweep_start))
        self._le_sw_start.setFont(_MONO)
        self._le_sw_start.setFixedWidth(60)
        sw_row.addWidget(self._le_sw_start)
        self._cb_sw_start_unit = QComboBox()
        self._cb_sw_start_unit.setFont(_MONO)
        self._cb_sw_start_unit.setFixedWidth(52)
        self._cb_sw_start_unit.setVisible(False)
        sw_row.addWidget(self._cb_sw_start_unit)

        sw_row.addWidget(QLabel("Stop:"))
        self._le_sw_stop = QLineEdit(str(self._cfg.acquire.sweep_stop))
        self._le_sw_stop.setFont(_MONO)
        self._le_sw_stop.setFixedWidth(60)
        sw_row.addWidget(self._le_sw_stop)
        self._cb_sw_stop_unit = QComboBox()
        self._cb_sw_stop_unit.setFont(_MONO)
        self._cb_sw_stop_unit.setFixedWidth(52)
        self._cb_sw_stop_unit.setVisible(False)
        sw_row.addWidget(self._cb_sw_stop_unit)

        sw_row.addWidget(QLabel("N:"))
        self._le_sw_n = QLineEdit(str(self._cfg.acquire.sweep_n))
        self._le_sw_n.setFont(_MONO)
        self._le_sw_n.setFixedWidth(44)
        sw_row.addWidget(self._le_sw_n)
        sw_row.addStretch()
        # Start/Stop/N 입력은 파라미터 sweep 선택 시에만 표시 (Time 선택 시 숨김)
        self._sweep_param_widget = QWidget()
        self._sweep_param_widget.setLayout(sw_row)
        lay.addWidget(self._sweep_param_widget)

        # 시간 기반 sweep: Sweep 콤보에서 '⏱ Time' 선택 시 사용
        #   → Interval(초)마다 VNA acquire를 1회씩 Count번 반복
        time_row = QHBoxLayout()
        time_row.setSpacing(4)
        time_row.addWidget(QLabel("Interval:"))
        self._le_time_interval = QLineEdit(str(self._cfg.acquire.time_interval))
        self._le_time_interval.setFont(_MONO)
        self._le_time_interval.setFixedWidth(56)
        time_row.addWidget(self._le_time_interval)
        time_row.addWidget(QLabel("s"))
        time_row.addSpacing(8)
        time_row.addWidget(QLabel("Count:"))
        self._le_time_count = QLineEdit(str(self._cfg.acquire.time_count))
        self._le_time_count.setFont(_MONO)
        self._le_time_count.setFixedWidth(44)
        time_row.addWidget(self._le_time_count)
        time_row.addStretch()
        self._time_row_widget = QWidget()
        self._time_row_widget.setLayout(time_row)
        lay.addWidget(self._time_row_widget)

        # Idle / Overrun 표시 (시간 모드 전용)
        self._lbl_idle = QLabel("Idle: —")
        self._lbl_idle.setFont(_MONO)
        self._lbl_idle.setStyleSheet("color: #555;")
        lay.addWidget(self._lbl_idle)
        # 시간 행/파라미터 행의 초기 표시 여부는 _populate_sweep_cmds() →
        # _on_sweep_cmd_changed()에서 콤보 선택에 따라 결정한다.

        sweep_row = QHBoxLayout()
        self._btn_sweep = QPushButton("▶ Sweep Acquire")
        self._btn_sweep.setFixedHeight(26)
        self._btn_sweep.setStyleSheet(
            "QPushButton{background:#4b2d5a;color:white;"
            "border-radius:3px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        self._btn_sweep.clicked.connect(self._on_sweep_acquire)
        sweep_row.addWidget(self._btn_sweep)

        self._btn_stop_acq = QPushButton("■ Stop")
        self._btn_stop_acq.setFixedHeight(26)
        self._btn_stop_acq.setMinimumWidth(90)
        self._btn_stop_acq.setEnabled(False)
        self._btn_stop_acq.clicked.connect(self._on_stop_acquire)
        sweep_row.addWidget(self._btn_stop_acq, stretch=1)
        sweep_row.addStretch(1)
        lay.addLayout(sweep_row)

        # Populate sweep command combo
        self._populate_sweep_cmds()
        return grp

    # ---- Sweep command helpers ---------------------------------------

    def _populate_sweep_cmds(self):
        """cfg.acquire.sweep_cmds로 콤보박스 갱신. 마지막에 '⏱ Time' 항목을 추가한다."""
        prev_idx  = self._combo_sweep_cmd.currentIndex()
        old_count = self._combo_sweep_cmd.count()
        # Time 항목은 항상 마지막 → 이전 선택이 마지막이면 Time이 선택돼 있던 것
        was_time = old_count > 0 and prev_idx == old_count - 1
        self._combo_sweep_cmd.blockSignals(True)
        self._combo_sweep_cmd.clear()
        for cmd in self._cfg.acquire.sweep_cmds:
            label = cmd.figure_axis or cmd.description or "(unnamed)"
            self._combo_sweep_cmd.addItem(label)
        self._combo_sweep_cmd.addItem("⏱ Time (반복 측정)")   # 시간 기반 sweep
        self._combo_sweep_cmd.setToolTip(
            "측정하며 쓸어갈 축을 고릅니다.\n"
            "• 등록된 sweep 명령어: 그 값을 Start→Stop으로 N단계 바꾸며 측정\n"
            "• ⏱ Time: 값을 바꾸지 않고 Interval(초)마다 Count번 반복 측정")
        self._combo_sweep_cmd.blockSignals(False)
        time_index = self._combo_sweep_cmd.count() - 1
        if self._pending_time_select:
            new_idx = time_index
            self._pending_time_select = False
        elif was_time:
            new_idx = time_index
        elif 0 <= prev_idx < self._combo_sweep_cmd.count():
            new_idx = prev_idx
        else:
            new_idx = 0
        self._combo_sweep_cmd.setCurrentIndex(new_idx)
        self._on_sweep_cmd_changed(new_idx)

    def _is_time_sweep_selected(self) -> bool:
        """Sweep 콤보에서 '⏱ Time' 항목(항상 마지막)이 선택됐는지."""
        return self._combo_sweep_cmd.currentIndex() == len(self._cfg.acquire.sweep_cmds)

    def _on_sweep_cmd_changed(self, idx: int):
        """선택에 따라 파라미터 sweep 행 / 시간 행을 전환하고 단위 콤보를 갱신."""
        is_time = self._is_time_sweep_selected()
        self._sweep_param_widget.setVisible(not is_time)
        self._time_row_widget.setVisible(is_time)
        self._lbl_idle.setVisible(is_time)
        self._btn_sweep.setText("▶ Time Sweep" if is_time else "▶ Sweep Acquire")
        if is_time:
            self._cb_sw_start_unit.setVisible(False)
            self._cb_sw_stop_unit.setVisible(False)
            return
        cmd = self._get_selected_sweep_cmd()
        ut  = cmd.unit_type if cmd else ""
        opts = _UNIT_OPTIONS.get(ut, [])
        default_lbl = _UNIT_DEFAULT.get(ut, "")
        for cb in (self._cb_sw_start_unit, self._cb_sw_stop_unit):
            cb.blockSignals(True)
            cb.clear()
            for label, mult in opts:
                cb.addItem(label, mult)
            idx2 = cb.findText(default_lbl)
            if idx2 >= 0:
                cb.setCurrentIndex(idx2)
            cb.blockSignals(False)
            cb.setVisible(bool(opts))

    def _get_selected_sweep_cmd(self) -> Optional[VnaCommandEntry]:
        idx = self._combo_sweep_cmd.currentIndex()
        if 0 <= idx < len(self._cfg.acquire.sweep_cmds):
            return self._cfg.acquire.sweep_cmds[idx]
        return None

    def _sweep_unit_mult(self, cb: QComboBox) -> float:
        """unit 콤보가 보이면 배수 반환, 아니면 1.0."""
        if cb.isVisible() and cb.count() > 0:
            return cb.currentData() or 1.0
        return 1.0

    # ---- Right panel: two plots side by side -------------------------

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(8)

        pg.setConfigOptions(antialias=False,
                            background="#0d1117", foreground="#c9d1d9")

        self._panel_L = _PlotPanel(start_color_idx=0)
        self._panel_R = _PlotPanel(start_color_idx=2)
        lay.addWidget(self._panel_L, stretch=1)
        lay.addWidget(self._panel_R, stretch=1)
        return w

    # ------------------------------------------------------------------
    # Section rebuilding
    # ------------------------------------------------------------------

    def _rebuild_sections(self):
        for sw in self._section_widgets:
            sw.setParent(None)
            sw.deleteLater()
        self._section_widgets.clear()
        self._role_rows  = {"start": None, "stop": None, "n_points": None}
        self._linspace_data = None

        count = self._sections_lay.count()
        for sec_cfg in self._cfg.sections:
            sw = _SectionWidget(sec_cfg, self._lib_reg)
            sw.execute_requested.connect(self._on_execute_section)
            sw.role_toggle_requested.connect(self._handle_role_toggle)
            self._section_widgets.append(sw)
            self._sections_lay.insertWidget(count - 1, sw)
            count += 1

        self._init_role_map()

    # ------------------------------------------------------------------
    # Role management
    # ------------------------------------------------------------------

    def _init_role_map(self):
        """섹션 재빌드 후 sweep_role이 저장된 rows를 찾아 _role_rows에 등록."""
        for sw in self._section_widgets:
            for row in sw._cmd_rows:
                role = row._current_role
                if not role:
                    continue
                if role in self._role_rows and self._role_rows[role] is None:
                    self._role_rows[role] = row
                    row.set_role(role)      # 버튼 시각 동기화
                else:
                    # 중복 저장된 role → 초기화
                    row._current_role = ""
                    row.set_role("")

    @Slot(object, str)
    def _handle_role_toggle(self, row: "_CmdRowWidget", role: str):
        """role 버튼 클릭 처리 — 전역 단일 할당 관리."""
        # 이미 이 row에 이 role이 있으면 → 해제 (토글 오프)
        if self._role_rows.get(role) is row:
            self._role_rows[role] = None
            row.set_role("")
            self._refresh_plot_sources()
            return

        # 다른 row에 이미 이 role이 할당된 경우 → 재할당 확인
        old_row = self._role_rows.get(role)
        if old_row is not None:
            role_name = {"start": "Start", "stop": "Stop", "n_points": "N Points"}[role]
            ret = QMessageBox.question(
                self, "역할 재할당",
                f"'{role_name}'이 이미 다른 항목에 할당되어 있습니다.\n이 항목으로 교체하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            old_row.set_role("")

        # 이 row가 다른 role을 갖고 있으면 먼저 해제
        old_role = row._current_role
        if old_role and old_role in self._role_rows and self._role_rows[old_role] is row:
            self._role_rows[old_role] = None

        self._role_rows[role] = row
        row.set_role(role)
        self._refresh_plot_sources()

    def _roles_complete(self) -> bool:
        """start / stop / n_points 3개 모두 할당됐는지 확인."""
        return all(self._role_rows.get(k) is not None
                   for k in ("start", "stop", "n_points"))

    def _get_role_value_si(self, row: "_CmdRowWidget") -> float:
        """row의 user_input 값을 SI 단위로 변환하여 반환."""
        if row._le_user is None:
            return 0.0
        raw = row._le_user.text().strip()
        try:
            val = float(raw)
        except ValueError:
            return 0.0
        if row._cb_unit is not None and row._cb_unit.isVisible():
            mult = row._cb_unit.currentData() or 1.0
            val *= mult
        return val

    def _compute_linspace(self) -> bool:
        """3개 role이 모두 할당된 경우 linspace를 계산하여 _linspace_data에 저장."""
        if not self._roles_complete():
            self._linspace_data = None
            return False

        start_row = self._role_rows["start"]
        stop_row  = self._role_rows["stop"]
        npts_row  = self._role_rows["n_points"]

        start_val = self._get_role_value_si(start_row)
        stop_val  = self._get_role_value_si(stop_row)
        npts_raw  = npts_row._le_user.text().strip() if npts_row._le_user else "2"
        try:
            n = max(2, int(float(npts_raw or "2")))
        except ValueError:
            n = 2

        arr  = np.linspace(start_val, stop_val, n)
        unit = (start_row._cb_unit.currentText()
                if (start_row._cb_unit and start_row._cb_unit.isVisible()) else "")
        self._linspace_data = ("time", unit, arr)
        return True

    # ------------------------------------------------------------------
    # Section execute
    # ------------------------------------------------------------------

    def _on_execute_section(self, cmds: list):
        if not cmds:
            self._set_status("No enabled commands.", color="#888")
            return
        if self._sec_worker and self._sec_worker.isRunning():
            self._set_status("Busy — please wait.", color="#888")
            return
        self._sec_worker = _VnaWorker(self._session, cmds)
        self._sec_worker.done.connect(
            lambda _: self._set_status(
                f"Section executed ({len(cmds)} cmd(s)).", color="#7ee787"))
        self._sec_worker.error.connect(
            lambda msg: self._set_status(f"Error: {msg}", color="#f78166"))
        self._set_status("Running...", color="#888")
        self._sec_worker.start()

    # ------------------------------------------------------------------
    # Acquire
    # ------------------------------------------------------------------

    def _on_single_acquire(self):
        self._start_acquire(sweep_values=None)

    def _on_sweep_acquire(self):
        if self._is_time_sweep_selected():
            self._on_time_sweep()
            return
        if not self._cfg.acquire.sweep_cmds:
            self._set_status("Sweep 명령어가 등록되지 않았습니다. Config를 확인하세요.",
                             color="#f78166")
            return
        try:
            start_raw = float(self._le_sw_start.text())
            stop_raw  = float(self._le_sw_stop.text())
            n         = int(self._le_sw_n.text())
            if n < 1:
                raise ValueError
        except ValueError:
            self._set_status("Invalid sweep parameters.", color="#f78166")
            return
        start = start_raw * self._sweep_unit_mult(self._cb_sw_start_unit)
        stop  = stop_raw  * self._sweep_unit_mult(self._cb_sw_stop_unit)
        values = ([start] if n == 1
                  else [start + (stop - start) * i / (n - 1)
                        for i in range(n)])
        self._start_acquire(sweep_values=values)

    def _on_time_sweep(self):
        """시간 기반 sweep 시작 — Interval(초)마다 acquire 1회씩 Count번."""
        try:
            interval = float(self._le_time_interval.text())
            count    = int(self._le_time_count.text())
            if interval < 0 or count < 1:
                raise ValueError
        except ValueError:
            self._set_status("Invalid time-sweep parameters.", color="#f78166")
            return
        self._lbl_idle.setText("Idle: —")
        self._lbl_idle.setStyleSheet("color: #555;")
        self._start_acquire(sweep_values=None,
                            time_mode=True, time_interval=interval, time_count=count)

    @Slot(int, float)
    def _on_step_elapsed(self, step_idx: int, elapsed: float):
        """각 acquire 스텝의 실제 소요 시간을 상태줄에 기록 (Single·Sweep 공통)."""
        self._set_status(f"Step {step_idx + 1} acquire 완료 — 소요 {elapsed:.3f} s", color="#7ee787")

    @Slot(int, float)
    def _on_step_timing(self, step_idx: int, remaining: float):
        """시간 모드: 스텝 read 완료 후 남은/부족 시간 표시."""
        if remaining >= 0:
            self._lbl_idle.setText(f"Idle: {remaining:.3f} s")
            self._lbl_idle.setStyleSheet("color: #7ee787;")
        else:
            self._lbl_idle.setText(f"Overrun: {remaining:.3f} s (부족)")
            self._lbl_idle.setStyleSheet("color: #f78166;")

    def _start_acquire(self, sweep_values, time_mode: bool = False,
                       time_interval: float = 1.0, time_count: int = 1):
        if self._acq_thread and self._acq_thread.isRunning():
            self._set_status("Acquire already running.", color="#888")
            return

        # section role → linspace 계산 (3개 모두 할당된 경우)
        _was_none = self._linspace_data is None
        if self._compute_linspace():
            self._refresh_plot_sources()
            # 최초 계산 시 x-source가 "index"이면 "time"으로 자동 전환
            if _was_none:
                for _panel in (self._panel_L, self._panel_R):
                    if _panel.x_source() == "index":
                        _panel.set_default_x("time")

        is_sweep = sweep_values is not None
        self._current_sweep_values = sweep_values
        self._step_count = 0

        # Sweep 전용: 저장 폴더 생성 + figure_axis 확보
        self._sweep_folder: Optional[Path] = None
        self._sweep_figure_axis: str = ""
        if is_sweep and self._cb_save.isChecked():
            selected_cmd = self._get_selected_sweep_cmd()
            self._sweep_figure_axis = (
                (selected_cmd.figure_axis or selected_cmd.description)
                if selected_cmd else "sweep")
            filename = self._le_filename.text().strip() or "vna_data"
            base_dir = self._get_save_dir()
            self._sweep_folder = next_sweep_folder(base_dir, filename)
            self._sweep_folder.mkdir(parents=True, exist_ok=True)

        selected_cmd = self._get_selected_sweep_cmd() if is_sweep else None
        worker = _AcquireWorker(
            self._session, self._lib_reg, self._cfg.acquire,
            sweep_values, sweep_cmd=selected_cmd,
            time_mode=time_mode, time_interval=time_interval, time_count=time_count)
        thread = QThread(self)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.step_done.connect(self._on_acq_step_done)
        worker.step_timing.connect(self._on_step_timing)
        worker.step_elapsed.connect(self._on_step_elapsed)
        worker.progress.connect(lambda msg: self._set_status(msg, color="#888"))
        worker.error.connect(self._on_acq_error)
        worker.finished.connect(lambda: self._on_acq_finished(is_sweep))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_acq_refs)

        self._acq_worker = worker
        self._acq_thread = thread
        self._time_mode_running = time_mode
        self._acq_errored = False
        self._acq_stopped = False
        self._set_acquire_busy(True)
        thread.start()

    def _clear_acq_refs(self):
        self._acq_worker = None
        self._acq_thread = None

    @Slot(int, list)
    def _on_acq_step_done(self, step_idx: int, arrays: List[np.ndarray]):
        self._step_count += 1
        self._update_plots(arrays, first_step=(self._step_count == 1))
        if self._cb_save.isChecked():
            is_sweep = self._current_sweep_values is not None
            self._save_step(step_idx, arrays, is_sweep)

    def _on_acq_error(self, msg: str):
        self._acq_errored = True
        self._set_status(f"Acquire error: {msg}", color="#f78166")
        # 알람: 측정 오류 트리거 (meas_error)
        self._fire_alarm_meas_error(msg)
        QMessageBox.critical(self, "Acquire Error", msg)

    # ------------------------------------------------------------------
    # Alarm (VNA sweep — 텔레그램)
    # ------------------------------------------------------------------

    def _open_alarm_config(self):
        from gui.alarm_config_window import AlarmConfigWindow
        dlg = AlarmConfigWindow(self._cfg.alarm, self._alarm_manager, parent=self,
                                title="VNA Sweep — Alarm Config")
        dlg.apply_requested.connect(self._on_alarm_cfg_applied)
        dlg.exec()

    def _on_alarm_cfg_applied(self, cfg):
        self._cfg.alarm = cfg
        save_vna_config(self._cfg, self._config_path())

    def _fire_alarm_meas_error(self, detail: str):
        cfg = self._cfg.alarm
        if not cfg.enabled:
            return
        if self._alarm_manager.has_meas_error_trigger(cfg.triggers):
            self._alarm_manager.fire(cfg, f"VNA 측정 오류 — {detail}")

    def _fire_alarm_complete(self, n: int):
        cfg = self._cfg.alarm
        if not cfg.enabled:
            return
        if cfg.fire_on_complete:
            self._alarm_manager.fire(cfg, f"VNA sweep 완료 — {n} step(s)")

    def _on_acq_finished(self, is_sweep: bool):
        n = self._step_count
        self._set_status(
            f"{'Sweep ' if is_sweep else ''}Acquire done — {n} step(s).",
            color="#7ee787")
        self._set_acquire_busy(False)
        # 알람: 정상 완료(오류·중단 아님)에만 완료 트리거
        if not self._acq_errored and not self._acq_stopped:
            self._fire_alarm_complete(n)

    def _set_acquire_busy(self, busy: bool):
        self._btn_single.setEnabled(not busy)
        self._btn_sweep.setEnabled(not busy)
        self._btn_stop_acq.setEnabled(busy)

    def _on_stop_acquire(self):
        self._acq_stopped = True
        if self._acq_worker:
            self._acq_worker.stop()
        self._set_status("Stopping…", color="#888")

    # ------------------------------------------------------------------
    # Status helper
    # ------------------------------------------------------------------

    def _set_status(self, msg: str, color: str = "#888"):
        self._lbl_status.setStyleSheet(f"color: {color};")
        self._lbl_status.setText(msg)

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def _source_labels(self) -> List[str]:
        names = ["index"]
        for i, cmd in enumerate(self._cfg.acquire.read_cmds):
            label = cmd.figure_axis or cmd.description or f"arr_{i}"
            names.append(label)
        # linspace: 3개 role 모두 할당됐거나 데이터가 이미 있으면 "time" 추가
        if self._roles_complete() or self._linspace_data is not None:
            if "time" not in names:
                names.append("time")
        return names

    def _build_data_dict(self, arrays: List[np.ndarray]) -> dict:
        read_cmds = self._cfg.acquire.read_cmds
        max_len   = max((len(a) for a in arrays), default=0)
        data = {"index": np.arange(max_len)}
        for i, arr in enumerate(arrays):
            if i < len(read_cmds):
                label = (read_cmds[i].figure_axis
                         or read_cmds[i].description
                         or f"arr_{i}")
            else:
                label = f"arr_{i}"
            data[label] = arr
        if self._linspace_data:
            _, _, arr = self._linspace_data
            data["time"] = arr
        return data

    def _update_plots(self, arrays: List[np.ndarray], first_step: bool = False):
        data = self._build_data_dict(arrays)
        self._panel_L.push_data(data, first_step=first_step)
        self._panel_R.push_data(data, first_step=first_step)

    def _restore_plot_config(self):
        sources = self._source_labels()
        self._panel_L.update_sources(sources)
        self._panel_R.update_sources(sources)

        curves = self._cfg.plot_curves
        if len(curves) >= 1:
            self._panel_L.restore_config(curves[0])
        if len(curves) >= 2:
            self._panel_R.restore_config(curves[1])

        # Sensible defaults when no saved config
        if len(sources) >= 2 and not curves:
            self._panel_L.set_default_x("index")
            self._panel_L.set_default_y(sources[1])
        if len(sources) >= 3 and len(curves) < 2:
            self._panel_R.set_default_x("index")
            self._panel_R.set_default_y(sources[2])
        elif len(sources) >= 2 and len(curves) < 2:
            self._panel_R.set_default_x("index")
            self._panel_R.set_default_y(sources[1])

    def _refresh_plot_sources(self):
        sources = self._source_labels()
        self._panel_L.update_sources(sources)
        self._panel_R.update_sources(sources)

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def _get_save_dir(self) -> Path:
        base = Path(self._le_main.text().strip() or ".")
        sub  = self._le_sub.text().strip()
        return base / sub if sub else base

    def _save_step(self, step_idx: int,
                   arrays: List[np.ndarray], is_sweep: bool):
        try:
            if is_sweep and self._sweep_folder is not None:
                # 파일명: figure_axis_stepvalue.dat
                sv = self._current_sweep_values[step_idx]
                sv_str = f"{sv:.6g}".replace('+', '')
                safe_fa = (self._sweep_figure_axis
                           .replace(' ', '_').replace('/', '_')
                           .replace('(', '').replace(')', ''))
                fname = f"{safe_fa}_{sv_str}.dat"
                path  = self._sweep_folder / fname
                self._write_dat(path, arrays)
            else:
                # Single acquire: filename_x001.dat 넘버링
                filename = self._le_filename.text().strip() or "vna_data"
                base_dir = self._get_save_dir()
                path = next_dat_path(base_dir, filename)
                self._write_dat(path, arrays)
        except Exception as e:
            self._set_status(f"Save error: {e}", color="#f78166")

    def _write_dat(self, path: Path, arrays: List[np.ndarray]):
        if not arrays:
            return
        read_cmds  = self._cfg.acquire.read_cmds
        long_names = []
        units_row  = []
        all_arrays = list(arrays)   # copy — 원본 변경 방지

        for i in range(len(arrays)):
            if i < len(read_cmds):
                fa = (read_cmds[i].figure_axis
                      or read_cmds[i].description
                      or f"col_{i}")
                u  = read_cmds[i].units
            else:
                fa, u = f"col_{i}", ""
            long_names.append(fa)
            units_row.append(u)

        # linspace 컬럼 추가 (3개 role 모두 할당된 경우)
        if self._linspace_data:
            _, unit, arr = self._linspace_data
            long_names.append("time")
            units_row.append(unit)
            all_arrays.append(arr)

        max_len = max(len(a) for a in all_arrays)
        lines   = [
            "\t".join(long_names),
            "\t".join(units_row),
        ]
        for row_i in range(max_len):
            vals = [f"{arr[row_i]:.10E}" if row_i < len(arr) else ""
                    for arr in all_arrays]
            lines.append("\t".join(vals))
        path.write_text("\n".join(lines), encoding="utf-8")

    # ------------------------------------------------------------------
    # Config reload
    # ------------------------------------------------------------------

    def _open_config(self):
        from gui.vna_config_window import VnaConfigWindow
        cfg_path = self._config_path()
        if self._config_win is None:
            self._config_win = VnaConfigWindow(
                self._lib_reg, config_path=cfg_path, parent=self)
            self._config_win.saved.connect(self._on_config_saved)
        else:
            # Update path in case profile changed
            self._config_win.set_config_path(cfg_path)
        self._config_win.show()
        self._config_win.raise_()

    def _on_config_saved(self):
        self._cfg = load_vna_config(self._config_path())
        self._le_filename.setText(self._cfg.acquire.filename)
        self._le_sw_start.setText(str(self._cfg.acquire.sweep_start))
        self._le_sw_stop.setText(str(self._cfg.acquire.sweep_stop))
        self._le_sw_n.setText(str(self._cfg.acquire.sweep_n))
        self._rebuild_sections()
        self._populate_sweep_cmds()
        self._refresh_plot_sources()
        self._set_status("Config reloaded.", color="#7ee787")

    # ------------------------------------------------------------------
    # File ops
    # ------------------------------------------------------------------

    def _on_browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Main Folder")
        if folder:
            self._le_main.setText(folder)

    def _on_open_folder(self):
        path = self._get_save_dir()
        if path.exists():
            os.startfile(str(path))
        else:
            self._set_status("Folder does not exist.", color="#888")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_ui_state(self):
        # 섹션 UI 상태(체크박스, 입력값, 단위) → _cfg에 반영
        # section_widget 수와 _cfg.sections 수가 불일치하면 유령 데이터 발생 가능 —
        # widget 기준으로만 flush하여 매칭 없는 항목은 자동 제거됨.
        if self._section_widgets:
            self._cfg.sections = [sw.get_updated_config()
                                  for sw in self._section_widgets]

        self._cfg.main_folder  = self._le_main.text().strip()
        self._cfg.sub_folder   = self._le_sub.text().strip()
        self._cfg.save_enabled = self._cb_save.isChecked()
        self._cfg.acquire.filename = self._le_filename.text().strip()
        try:
            self._cfg.acquire.sweep_start = float(self._le_sw_start.text())
            self._cfg.acquire.sweep_stop  = float(self._le_sw_stop.text())
            self._cfg.acquire.sweep_n     = int(self._le_sw_n.text())
        except ValueError:
            pass
        # 시간 기반 sweep 파라미터 영속화 (Sweep 콤보의 '⏱ Time' 선택 여부)
        self._cfg.acquire.time_mode = self._is_time_sweep_selected()
        try:
            self._cfg.acquire.time_interval = float(self._le_time_interval.text())
            self._cfg.acquire.time_count    = int(self._le_time_count.text())
        except ValueError:
            pass
        self._cfg.plot_curves = [
            self._panel_L.to_config(),
            self._panel_R.to_config(),
        ]
        save_vna_config(self._cfg, self._config_path())

    def _clear_graph_data(self):
        """그래프/데이터 초기화 (window 종료/숨김 시)."""
        self._step_count = 0
        self._panel_L.clear_data()
        self._panel_R.clear_data()

    def _save_and_hide(self):
        """저장 + 그래프 초기화 + 숨김 (닫기/Escape 공통)."""
        self._save_ui_state()
        self._clear_graph_data()
        self.hide()

    def closeEvent(self, event):
        event.ignore()
        self._save_and_hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            event.accept()           # VNA Control은 Esc로 닫지 않음 (실수로 닫힘 방지)
            return
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_S):
            self._save_ui_state()    # Ctrl+S = 설정 저장(닫지 않음)
            event.accept()
            return
        super().keyPressEvent(event)
