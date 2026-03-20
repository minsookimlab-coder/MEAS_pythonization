"""
DoubleSweepWindow: Double Sweep 전용 창.
Second sweep channel 값을 배열로 순회하며 각 값마다 DUMMY→TRACE→RETRACE를 실행합니다.

실행 흐름:
  PRE_INIT (first channel → start_point, 데이터 없음)
  → ADVANCING_SECOND (second channel 설정)
  → DUMMY  (first channel: start_point→start_point, dummy/ 저장)
  → TRACE  (first channel: start_point→stop_point,  trace/ 저장)
  → RETRACE(first channel: stop_point→start_point,  retrace/ 저장)
  → ADVANCING_SECOND (다음 array 값) → ... → IDLE
"""
import math
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Tuple, TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QFrame,
    QLabel, QLineEdit, QPushButton, QDoubleSpinBox, QSpinBox, QCheckBox,
    QButtonGroup, QRadioButton, QMessageBox, QWidget,
)

from config.config_models import (
    DoubleSweepConfig, InstantiatedSecondSweepChannel, SecondSweepAdvanceType,
)
from core.data_saver import DataSaver
from core.second_channel_worker import SecondChannelWorker, SecondChannelRequest
from core.sweep_worker import SweepWorker, StepRequest, StepResult

if TYPE_CHECKING:
    from gui.main_window import MainWindow

_MONO = QFont("Consolas", 10)


@dataclass
class DoubleSweepContext:
    """Double Sweep 시작 시 MainWindow에서 스냅샷한 sweep 실행 컨텍스트.

    MainWindow private 속성에 sweep 도중 반복 접근하는 대신
    시작 시점에 필요한 정보를 캡처해 사용합니다.
    """
    # First channel (SweepChannel 또는 TimeChannel)
    sweep_channel: object
    # First channel sweep value의 safety 파라미터 (없으면 0)
    sv_safety_steps: int
    sv_safety_interval_ms: float
    # 체크된 measurement 인덱스 + 실행에 필요한 resolved 정보
    active_meas_indices: List[int]
    active_measurements: List[Tuple[int, str, str, str]]  # (row, alias, desc, cmd)
    # DataSaver 열 헤더: (name, unit)
    sweep_col: Tuple[str, str]
    meas_cols: List[Tuple[str, str]]
    # DataSaver 저장 경로 설정
    main_folder: str
    custom_folder: str
    custom_word: str
    include_date: bool
    save_enabled: bool


class DoubleSweepPhase(Enum):
    IDLE             = auto()
    PRE_INIT         = auto()   # first channel → start_point (no data)
    ADVANCING_SECOND = auto()
    DUMMY            = auto()
    TRACE            = auto()
    RETRACE          = auto()


def _generate_array(cfg: DoubleSweepConfig) -> List[float]:
    """numpy 없이 arange 구현 (step 부호 자동 처리)."""
    if abs(cfg.array_step) < 1e-12:
        return [cfg.array_from]
    step = cfg.array_step
    eps = abs(step) * 1e-9
    result = []
    v = cfg.array_from
    while (step > 0 and v <= cfg.array_to + eps) or (step < 0 and v >= cfg.array_to - eps):
        result.append(round(v, 12))
        v += step
    return result if result else [cfg.array_from]


def _estimate_total_seconds(cfg: "DoubleSweepConfig", n_array: int) -> float:
    """Estimate total sweep time in seconds (excluding PRE_INIT and second-channel advance)."""
    tpp = cfg.time_per_point
    distance = abs(cfg.stop_point - cfg.start_point)

    def _n_steps(dist: float, rate: float) -> int:
        if rate <= 0 or tpp <= 0:
            return 1
        increment = (rate / 60.0) * tpp
        if increment <= 0:
            return 1
        return math.ceil(dist / increment) + 1   # +1 for the is_done step

    dummy_steps = 1                                    # already at start_point
    trace_steps   = _n_steps(distance, cfg.rate_trace)
    retrace_steps = _n_steps(distance, cfg.rate_retrace)
    steps_per_cycle = dummy_steps + trace_steps + retrace_steps
    return n_array * steps_per_cycle * tpp


def _fmt_hms(total_sec: float) -> str:
    total_sec = max(0.0, total_sec)
    h = int(total_sec) // 3600
    m = (int(total_sec) % 3600) // 60
    s = int(total_sec) % 60
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class DoubleSweepWindow(QDialog):
    """
    Double Sweep 창.

    - main_win._sweep_channel / _active_profile / _meas_checkboxes 사용 (읽기 전용)
    - 자체 SweepWorker + QThread + QTimer
    - 자체 SecondChannelWorker + QThread
    - 자체 DataSaver (phase별 subfolder)
    """

    # main_window에 sweep lock 신호
    sweep_started  = Signal()
    sweep_finished = Signal()

    # 워커에 요청
    request_step    = Signal(object)   # StepRequest → _sweep_worker
    request_advance = Signal(object)   # SecondChannelRequest → _second_worker

    def __init__(self, main_win: "MainWindow", param_reg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Double Sweep")
        self.setMinimumWidth(400)
        self.resize(420, 560)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._main_win  = main_win
        self._param_reg = param_reg

        self._phase: DoubleSweepPhase = DoubleSweepPhase.IDLE
        self._array: List[float] = []
        self._array_idx: int = 0
        self._last_write_value: Optional[float] = None
        self._second_channel: Optional[InstantiatedSecondSweepChannel] = None
        self._ctx: Optional[DoubleSweepContext] = None   # set at sweep start

        # DataSaver
        self._data_saver = DataSaver()
        self._data_saver.set_error_callback(
            lambda msg: main_win._log(f"  [DoubleSweep DataSaver] {msg}", color="#f44747")
        )

        # Sweep worker
        self._sweep_worker = SweepWorker()
        self._sweep_worker.set_session(main_win._session)
        self._worker_thread = QThread(self)
        self._sweep_worker.moveToThread(self._worker_thread)
        self.request_step.connect(self._sweep_worker.run_step)
        self._sweep_worker.step_done.connect(self._on_step_done)
        self._sweep_worker.step_error.connect(self._on_step_error)
        self._worker_thread.start()

        # Second channel worker
        self._second_worker = SecondChannelWorker()
        self._second_worker.set_session(main_win._session)
        self._second_thread = QThread(self)
        self._second_worker.moveToThread(self._second_thread)
        self.request_advance.connect(self._second_worker.advance)
        self._second_worker.done.connect(self._on_advance_done)
        self._second_worker.error.connect(self._on_advance_error)
        self._second_thread.start()

        # Step timer (single-shot)
        self._sweep_timer = QTimer(self)
        self._sweep_timer.setSingleShot(True)
        self._sweep_timer.timeout.connect(self._sweep_tick)

        # Glow animation
        self._glow_phase = 0.0
        self._glow_timer = QTimer(self)
        self._glow_timer.setInterval(30)
        self._glow_timer.timeout.connect(self._update_glow)

        self._build_ui()
        self._load_config()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        dialog_layout = QVBoxLayout(self)
        dialog_layout.setContentsMargins(0, 0, 0, 0)
        self._glow_frame = QFrame()
        self._glow_frame.setObjectName("dsGlowFrame")
        self._glow_frame.setStyleSheet(
            "QFrame#dsGlowFrame { border: 3px solid transparent; border-radius: 6px; }"
        )
        dialog_layout.addWidget(self._glow_frame)

        outer = QVBoxLayout(self._glow_frame)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # Second Channel selector
        ch_frame = QFrame()
        ch_frame.setFrameShape(QFrame.Shape.StyledPanel)
        ch_layout = QVBoxLayout(ch_frame)
        ch_layout.setContentsMargins(8, 6, 8, 6)
        ch_title = QLabel("Second Channel")
        ch_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #f78166;")
        ch_layout.addWidget(ch_title)
        self._second_ch_layout = QVBoxLayout()
        ch_layout.addLayout(self._second_ch_layout)
        self._second_radio_group = QButtonGroup(self)
        self._second_radio_group.setExclusive(True)
        self._second_radio_group.idToggled.connect(self._on_second_radio_toggled)
        outer.addWidget(ch_frame)

        # Sweep Parameters
        sp_frame = QFrame()
        sp_frame.setFrameShape(QFrame.Shape.StyledPanel)
        sp_layout = QVBoxLayout(sp_frame)
        sp_layout.setContentsMargins(8, 6, 8, 6)
        sp_title = QLabel("Sweep Parameters")
        sp_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        sp_layout.addWidget(sp_title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        sp_layout.addLayout(form)

        def _dsb(lo=-1e9, hi=1e9, dec=4, val=0.0, suffix=""):
            w = QDoubleSpinBox()
            w.setRange(lo, hi)
            w.setDecimals(dec)
            w.setValue(val)
            w.setFont(_MONO)
            if suffix:
                w.setSuffix(f"  {suffix}")
            w.setFixedWidth(160)
            return w

        self._sb_start  = _dsb(val=0.0)
        self._sb_stop   = _dsb(val=1.0)
        self._sb_rate_t = _dsb(lo=1e-9, hi=1e9, val=1.0, suffix="units/min")
        self._sb_rate_r = _dsb(lo=1e-9, hi=1e9, val=1.0, suffix="units/min")
        self._sb_rate_d = _dsb(lo=1e-9, hi=1e9, val=1.0, suffix="units/min")
        self._sb_tpp    = _dsb(lo=0.001, hi=3600, dec=3, val=1.0, suffix="sec")

        self._cb_retrace_to_zero = QCheckBox("Retrace to 0")
        self._cb_retrace_to_zero.setFont(_MONO)
        self._cb_retrace_to_zero.setToolTip("When checked, RETRACE sweeps to 0 instead of Start Point")

        form.addRow("Start Point:", self._sb_start)
        form.addRow("Stop Point:",  self._sb_stop)
        form.addRow("Rate (trace):",    self._sb_rate_t)
        form.addRow("Rate (retrace):",  self._sb_rate_r)
        form.addRow("",                 self._cb_retrace_to_zero)
        form.addRow("Rate (dummy):",    self._sb_rate_d)
        form.addRow("Time / Point:",    self._sb_tpp)
        outer.addWidget(sp_frame)

        # SWEEP Channel Settings (only visible when second channel advance_type == SWEEP)
        self._sweep_ch_frame = QFrame()
        self._sweep_ch_frame.setFrameShape(QFrame.Shape.StyledPanel)
        sc_layout = QVBoxLayout(self._sweep_ch_frame)
        sc_layout.setContentsMargins(8, 6, 8, 6)
        sc_title = QLabel("SWEEP Channel Settings")
        sc_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #c586c0;")
        sc_layout.addWidget(sc_title)

        sc_form = QFormLayout()
        sc_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        sc_form.setHorizontalSpacing(12)
        sc_layout.addLayout(sc_form)

        self._sb_second_rate = _dsb(lo=1e-9, hi=1e9, val=1.0, suffix="units/min")
        sc_form.addRow("Sweep Rate:", self._sb_second_rate)

        self._cb_second_safety = QCheckBox("Use Safety Ramp")
        self._cb_second_safety.setFont(_MONO)
        sc_form.addRow("", self._cb_second_safety)

        self._sb_second_steps = QSpinBox()
        self._sb_second_steps.setRange(0, 100000)
        self._sb_second_steps.setValue(0)
        self._sb_second_steps.setFont(_MONO)
        self._sb_second_steps.setFixedWidth(100)
        self._sb_second_steps.setEnabled(False)
        sc_form.addRow("Safety Steps:", self._sb_second_steps)

        self._sb_second_interval = _dsb(lo=0.0, hi=60000, dec=1, val=0.0, suffix="ms")
        self._sb_second_interval.setEnabled(False)
        sc_form.addRow("Safety Interval:", self._sb_second_interval)

        self._cb_second_safety.toggled.connect(self._sb_second_steps.setEnabled)
        self._cb_second_safety.toggled.connect(self._sb_second_interval.setEnabled)

        self._sweep_ch_frame.setVisible(False)
        outer.addWidget(self._sweep_ch_frame)

        # Second Channel Array
        arr_frame = QFrame()
        arr_frame.setFrameShape(QFrame.Shape.StyledPanel)
        arr_layout = QVBoxLayout(arr_frame)
        arr_layout.setContentsMargins(8, 6, 8, 6)
        arr_title = QLabel("Second Channel Array")
        arr_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        arr_layout.addWidget(arr_title)

        arr_row = QHBoxLayout()
        arr_row.addWidget(QLabel("From:"))
        self._sb_arr_from = _dsb(val=0.0)
        self._sb_arr_from.setFixedWidth(90)
        arr_row.addWidget(self._sb_arr_from)
        arr_row.addWidget(QLabel("To:"))
        self._sb_arr_to = _dsb(val=1.0)
        self._sb_arr_to.setFixedWidth(90)
        arr_row.addWidget(self._sb_arr_to)
        arr_row.addWidget(QLabel("Step:"))
        self._sb_arr_step = _dsb(lo=-1e9, hi=1e9, val=0.1)
        self._sb_arr_step.setFixedWidth(90)
        arr_row.addWidget(self._sb_arr_step)
        self._lbl_n_points = QLabel("→ — pts")
        self._lbl_n_points.setStyleSheet("color: #888888;")
        arr_row.addWidget(self._lbl_n_points)
        arr_layout.addLayout(arr_row)
        outer.addWidget(arr_frame)

        # Connect array spinboxes to point count update
        for sb in (self._sb_arr_from, self._sb_arr_to, self._sb_arr_step):
            sb.valueChanged.connect(self._update_n_points)

        # Estimated time row
        est_row = QHBoxLayout()
        est_row.addWidget(QLabel("Estimated time:"))
        self._lbl_est_time = QLabel("—")
        self._lbl_est_time.setFont(_MONO)
        self._lbl_est_time.setStyleSheet("color: #79c0ff; font-weight: bold;")
        est_row.addWidget(self._lbl_est_time)
        est_row.addStretch()
        outer.addLayout(est_row)

        # Connect all params that affect the estimate
        for sb in (self._sb_start, self._sb_stop,
                   self._sb_rate_t, self._sb_rate_r, self._sb_rate_d,
                   self._sb_tpp,
                   self._sb_arr_from, self._sb_arr_to, self._sb_arr_step):
            sb.valueChanged.connect(self._update_est_time)

        # Status row
        status_frame = QFrame()
        status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(8, 4, 8, 4)

        self._lbl_phase = QLabel("IDLE")
        self._lbl_phase.setFont(_MONO)
        self._lbl_phase.setStyleSheet("color: #888888;")
        status_layout.addWidget(QLabel("Phase:"))
        status_layout.addWidget(self._lbl_phase)

        status_layout.addSpacing(12)
        status_layout.addWidget(QLabel("Second:"))
        self._lbl_second_val = QLabel("—")
        self._lbl_second_val.setFont(_MONO)
        self._lbl_second_val.setStyleSheet("color: #f78166;")
        status_layout.addWidget(self._lbl_second_val)

        status_layout.addSpacing(12)
        status_layout.addWidget(QLabel("Array:"))
        self._lbl_array_progress = QLabel("—/—")
        self._lbl_array_progress.setFont(_MONO)
        self._lbl_array_progress.setStyleSheet("color: #79c0ff;")
        status_layout.addWidget(self._lbl_array_progress)
        status_layout.addStretch()
        outer.addWidget(status_frame)

        # Start / Stop
        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("Start Double Sweep")
        self._btn_start.setMinimumHeight(40)
        self._btn_start.setStyleSheet(
            "QPushButton { font-weight: bold; font-size: 13px;"
            "background-color: #0d6830; color: white; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #0a2b1a; color: #2d4d35; border-radius: 4px; }"
        )
        self._btn_start.clicked.connect(self._on_start)

        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setMinimumHeight(40)
        self._btn_stop.setStyleSheet(
            "QPushButton { font-weight: bold; font-size: 13px;"
            "background-color: #c62828; color: white; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #3a1a1a; color: #553d3d; border-radius: 4px; }"
        )
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)

        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_stop)
        outer.addLayout(btn_row)
        outer.addStretch()

    # ------------------------------------------------------------------
    # Second channel radio buttons
    # ------------------------------------------------------------------

    def _rebuild_second_channel_radios(self):
        for btn in self._second_radio_group.buttons():
            self._second_radio_group.removeButton(btn)
        while self._second_ch_layout.count():
            item = self._second_ch_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        channels = self._param_reg.main_ui_profile.second_sweep_channels
        if not channels:
            lbl = QLabel("(Parameter Manager에서 Second Sweep Channel을 등록하세요)")
            lbl.setStyleSheet("color: #555555; font-size: 11px;")
            self._second_ch_layout.addWidget(lbl)
            self._second_channel = None
            self._sweep_ch_frame.setVisible(False)
            return

        for idx, ch in enumerate(channels):
            rb = QRadioButton(f"[{ch.alias}]  {ch.description}  ({ch.unit})  — {ch.advance_type.value}")
            rb.setFont(_MONO)
            rb.setStyleSheet("color: #f78166;")
            self._second_radio_group.addButton(rb, idx)
            self._second_ch_layout.addWidget(rb)

        cfg_idx = min(self._param_reg.double_sweep_config.selected_channel_idx, len(channels) - 1)
        btn = self._second_radio_group.button(cfg_idx)
        if btn:
            btn.setChecked(True)
        else:
            self._second_radio_group.buttons()[0].setChecked(True)

    def _on_second_radio_toggled(self, btn_id: int, checked: bool):
        if not checked:
            return
        channels = self._param_reg.main_ui_profile.second_sweep_channels
        if 0 <= btn_id < len(channels):
            self._second_channel = channels[btn_id]
            self._sweep_ch_frame.setVisible(
                self._second_channel.advance_type == SecondSweepAdvanceType.SWEEP
            )

    # ------------------------------------------------------------------
    # Config load / save
    # ------------------------------------------------------------------

    def _load_config(self):
        cfg = self._param_reg.double_sweep_config
        self._sb_start.setValue(cfg.start_point)
        self._sb_stop.setValue(cfg.stop_point)
        self._sb_rate_t.setValue(cfg.rate_trace)
        self._sb_rate_r.setValue(cfg.rate_retrace)
        self._sb_rate_d.setValue(cfg.rate_dummy)
        self._sb_tpp.setValue(cfg.time_per_point)
        self._sb_arr_from.setValue(cfg.array_from)
        self._sb_arr_to.setValue(cfg.array_to)
        self._sb_arr_step.setValue(cfg.array_step)
        self._cb_retrace_to_zero.setChecked(cfg.retrace_to_zero)
        self._sb_second_rate.setValue(cfg.second_sweep_rate)
        self._cb_second_safety.setChecked(cfg.second_use_safety)
        self._sb_second_steps.setValue(cfg.second_safety_steps)
        self._sb_second_interval.setValue(cfg.second_safety_interval_ms)
        self._update_n_points()
        self._update_est_time()

    def _save_config(self):
        cfg = DoubleSweepConfig(
            start_point=self._sb_start.value(),
            stop_point=self._sb_stop.value(),
            rate_trace=self._sb_rate_t.value(),
            rate_retrace=self._sb_rate_r.value(),
            rate_dummy=self._sb_rate_d.value(),
            time_per_point=self._sb_tpp.value(),
            array_from=self._sb_arr_from.value(),
            array_to=self._sb_arr_to.value(),
            array_step=self._sb_arr_step.value(),
            selected_channel_idx=max(0, self._second_radio_group.checkedId()),
            retrace_to_zero=self._cb_retrace_to_zero.isChecked(),
            second_sweep_rate=self._sb_second_rate.value(),
            second_use_safety=self._cb_second_safety.isChecked(),
            second_safety_steps=self._sb_second_steps.value(),
            second_safety_interval_ms=self._sb_second_interval.value(),
        )
        self._param_reg.save_double_sweep_config(cfg)

    def _current_cfg(self) -> DoubleSweepConfig:
        return DoubleSweepConfig(
            start_point=self._sb_start.value(),
            stop_point=self._sb_stop.value(),
            rate_trace=self._sb_rate_t.value(),
            rate_retrace=self._sb_rate_r.value(),
            rate_dummy=self._sb_rate_d.value(),
            time_per_point=self._sb_tpp.value(),
            array_from=self._sb_arr_from.value(),
            array_to=self._sb_arr_to.value(),
            array_step=self._sb_arr_step.value(),
            retrace_to_zero=self._cb_retrace_to_zero.isChecked(),
        )

    def _update_n_points(self):
        cfg = self._current_cfg()
        arr = _generate_array(cfg)
        self._lbl_n_points.setText(f"→ {len(arr)} pts")

    def _update_est_time(self):
        cfg = self._current_cfg()
        arr = _generate_array(cfg)
        sec = _estimate_total_seconds(cfg, len(arr))
        self._lbl_est_time.setText(_fmt_hms(sec))

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def _on_start(self):
        if self._phase != DoubleSweepPhase.IDLE:
            return
        if self._main_win._running:
            QMessageBox.warning(self, "Sweep 실행 중",
                "Main sweep이 실행 중입니다. 먼저 중단하세요.")
            return
        if self._main_win._sweep_channel is None:
            QMessageBox.warning(self, "No Sweep Channel",
                "Main UI에서 Sweep Channel을 선택하세요.")
            return
        if self._second_channel is None:
            QMessageBox.warning(self, "No Second Channel",
                "Second Sweep Channel을 선택하세요.\n"
                "Parameter Manager에서 Second Sweep Channel을 등록하세요.")
            return

        # 연결 상태 확인 (second channel 포함) — 실패 시 측정 중단
        if not self._main_win._run_connection_test(include_second=True, show_success=False):
            return

        self._save_config()
        self._cfg = self._current_cfg()
        self._array = _generate_array(self._cfg)
        if not self._array:
            QMessageBox.warning(self, "Array 오류", "Array 생성 실패: 포인트 수가 0입니다.")
            return

        self._array_idx = 0
        self._last_write_value = None
        self._ctx = self._build_context()

        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._glow_phase = 0.0
        self._glow_timer.start()
        self.sweep_started.emit()

        # Graph: begin fresh session with the same columns as the sweep context
        if self._main_win._graph_window is not None:
            try:
                cols = [("__sweep__", self._ctx.sweep_col[0], self._ctx.sweep_col[1])]
                for (row, alias, desc, cmd), (fig_ax, unit) in zip(
                    self._ctx.active_measurements, self._ctx.meas_cols
                ):
                    cols.append((desc, fig_ax, unit))
                deriv_cfg = self._main_win._deriv_channel._cfg
                if deriv_cfg.enabled:
                    cols.append(self._main_win._deriv_channel.col_info())
                self._main_win._graph_window.begin_session(cols)
            except Exception as _e:
                self._main_win._log(f"  [Graph] begin_session failed: {_e}", color="#f44747")

        # Reset derivative sliding window for a clean new sweep
        self._main_win._deriv_channel.reset()

        self._main_win._log("Double Sweep started. Pre-positioning first channel → start_point.", color="#4ec9b0")
        self._start_pre_init()

    def _on_stop(self):
        if self._phase == DoubleSweepPhase.IDLE:
            return
        self._sweep_timer.stop()
        self._sweep_worker.request_stop()
        self._second_worker.request_stop()
        self._set_phase(DoubleSweepPhase.IDLE)
        self._lbl_second_val.setText("—")
        self._lbl_array_progress.setText("—/—")
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._stop_glow()
        self.sweep_finished.emit()
        self._main_win._log("Double Sweep stopped.", color="#ce9178")

    def _finish(self):
        self._set_phase(DoubleSweepPhase.IDLE)
        self._lbl_second_val.setText("—")
        self._lbl_array_progress.setText(f"{len(self._array)}/{len(self._array)}")
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._stop_glow()
        self.sweep_finished.emit()
        self._main_win._log(
            f"Double Sweep complete. ({len(self._array)} array points)", color="#4ec9b0"
        )

    # ------------------------------------------------------------------
    # Context snapshot
    # ------------------------------------------------------------------

    def _build_context(self) -> DoubleSweepContext:
        """Double Sweep 시작 시 MainWindow 상태를 스냅샷합니다."""
        mw = self._main_win
        profile = mw._active_profile

        # Active measurements
        active_meas_indices = [
            i for i, (_, cb) in enumerate(zip(profile.measurements, mw._meas_checkboxes))
            if cb.isChecked()
        ]
        active_measurements = [
            (row, m.alias, m.description, m.resolved_cmd)
            for row, m in enumerate(profile.measurements)
            if row in active_meas_indices
        ]
        meas_cols = [
            (m.figure_axis or m.description, m.unit)
            for row, m in enumerate(profile.measurements)
            if row in active_meas_indices
        ]

        # First channel sweep column header + safety params
        sv_id = mw._sweep_radio_group.checkedId()
        sv_list = profile.sweep_values
        sv = sv_list[sv_id] if 0 <= sv_id < len(sv_list) else None
        if sv_id == mw._TIME_ID:
            sweep_col: Tuple[str, str] = ("time", "")
        elif sv:
            sweep_col = (sv.figure_axis or "target", sv.unit)
        else:
            sweep_col = ("target", "")

        return DoubleSweepContext(
            sweep_channel=mw._sweep_channel,
            sv_safety_steps=sv.safety_steps if sv else 0,
            sv_safety_interval_ms=sv.safety_interval_ms if sv else 0.0,
            active_meas_indices=active_meas_indices,
            active_measurements=active_measurements,
            sweep_col=sweep_col,
            meas_cols=meas_cols,
            main_folder=mw._le_main_folder.text(),
            custom_folder=mw._le_custom_folder.text(),
            custom_word=mw._le_custom_word.text(),
            include_date=mw._cb_save_date.isChecked(),
            save_enabled=mw._cb_save_enable.isChecked(),
        )

    # ------------------------------------------------------------------
    # Phase management
    # ------------------------------------------------------------------

    def _set_phase(self, phase: DoubleSweepPhase):
        self._phase = phase
        self._lbl_phase.setText(phase.name)

    def _start_pre_init(self):
        """First channel을 start_point로 이동 (데이터 저장 없음)."""
        self._set_phase(DoubleSweepPhase.PRE_INIT)
        self._sweep_timer.start(0)

    def _make_effective_second_channel(self) -> InstantiatedSecondSweepChannel:
        """Return second channel with UI-overridden SWEEP params when advance_type is SWEEP."""
        ch = self._second_channel
        if ch.advance_type != SecondSweepAdvanceType.SWEEP:
            return ch
        use_safety = self._cb_second_safety.isChecked()
        return ch.model_copy(update={
            "sweep_rate": self._sb_second_rate.value(),
            "safety_steps": self._sb_second_steps.value() if use_safety else 0,
            "safety_interval_ms": self._sb_second_interval.value() if use_safety else 0.0,
        })

    def _advance_second(self, next_val: float, prev: Optional[float]):
        ch = self._make_effective_second_channel()
        self._lbl_second_val.setText(f"{next_val:.4g} {ch.unit}")
        self._lbl_array_progress.setText(f"{self._array_idx + 1}/{len(self._array)}")
        self._set_phase(DoubleSweepPhase.ADVANCING_SECOND)

        # Clear graph and reset derivative buffer when advancing to next array step
        if prev is not None:
            self._main_win._deriv_channel.reset()
            if self._main_win._graph_window is not None:
                try:
                    cols = [("__sweep__", self._ctx.sweep_col[0], self._ctx.sweep_col[1])]
                    for (row, alias, desc, cmd), (fig_ax, unit) in zip(
                        self._ctx.active_measurements, self._ctx.meas_cols
                    ):
                        cols.append((desc, fig_ax, unit))
                    if self._main_win._deriv_channel._cfg.enabled:
                        cols.append(self._main_win._deriv_channel.col_info())
                    self._main_win._graph_window.begin_session(cols)
                except Exception as _e:
                    self._main_win._log(f"  [Graph] clear failed: {_e}", color="#f44747")

        self.request_advance.emit(SecondChannelRequest(
            channel=ch,
            next_value=next_val,
            prev_value=prev,
            time_per_point=self._cfg.time_per_point,
        ))

    @Slot()
    def _on_advance_done(self):
        # Always start DUMMY after advance
        self._start_sweep_phase(DoubleSweepPhase.DUMMY)

    @Slot(str)
    def _on_advance_error(self, msg: str):
        self._main_win._log(f"  [DoubleSweep] Second channel error: {msg}", color="#f44747")
        self._on_stop()

    def _start_sweep_phase(self, phase: DoubleSweepPhase):
        self._set_phase(phase)
        self._setup_datasaver_for_phase(phase.name.lower())
        self._sweep_timer.start(0)

    def _advance_phase(self):
        if self._phase == DoubleSweepPhase.DUMMY:
            self._start_sweep_phase(DoubleSweepPhase.TRACE)
        elif self._phase == DoubleSweepPhase.TRACE:
            self._start_sweep_phase(DoubleSweepPhase.RETRACE)
        elif self._phase == DoubleSweepPhase.RETRACE:
            self._array_idx += 1
            if self._array_idx < len(self._array):
                self._advance_second(
                    self._array[self._array_idx],
                    prev=self._array[self._array_idx - 1],
                )
            else:
                self._finish()

    # ------------------------------------------------------------------
    # DataSaver
    # ------------------------------------------------------------------

    def _setup_datasaver_for_phase(self, phase_name: str):
        ctx = self._ctx
        base = ctx.custom_folder
        phase_subfolder = f"{base}/{phase_name}" if base else phase_name
        self._data_saver.set_main_folder(ctx.main_folder)
        self._data_saver.set_custom_folder(phase_subfolder)
        self._data_saver.set_custom_word(ctx.custom_word)
        self._data_saver.set_include_date(ctx.include_date)
        self._data_saver.set_enabled(ctx.save_enabled)
        self._data_saver.set_columns([ctx.sweep_col] + ctx.meas_cols)
        filepath = self._data_saver.start_session()
        if filepath:
            self._main_win._log(f"  [{phase_name}] Data → {filepath}", color="#888888")

    # ------------------------------------------------------------------
    # Sweep tick (same pattern as main_window)
    # ------------------------------------------------------------------

    def _sweep_tick(self):
        if self._phase not in (DoubleSweepPhase.PRE_INIT,
                               DoubleSweepPhase.DUMMY,
                               DoubleSweepPhase.TRACE,
                               DoubleSweepPhase.RETRACE):
            return

        cfg = self._cfg
        ctx = self._ctx

        if self._phase == DoubleSweepPhase.PRE_INIT:
            sweep_to, sweep_rate = cfg.start_point, cfg.rate_dummy
            active_meas = []                        # 데이터 수집 없음
        elif self._phase == DoubleSweepPhase.DUMMY:
            sweep_to, sweep_rate = cfg.start_point, cfg.rate_dummy
            active_meas = ctx.active_measurements
        elif self._phase == DoubleSweepPhase.TRACE:
            sweep_to, sweep_rate = cfg.stop_point, cfg.rate_trace
            active_meas = ctx.active_measurements
        else:  # RETRACE
            sweep_to = 0.0 if cfg.retrace_to_zero else cfg.start_point
            sweep_rate = cfg.rate_retrace
            active_meas = ctx.active_measurements

        t_emit = time.perf_counter()
        self.request_step.emit(StepRequest(
            sweep_channel=ctx.sweep_channel,
            sweep_to=sweep_to,
            sweep_rate=sweep_rate,
            time_per_point=cfg.time_per_point,
            t_emit=t_emit,
            last_write_value=self._last_write_value,
            safety_steps=ctx.sv_safety_steps,
            safety_interval_ms=ctx.sv_safety_interval_ms,
            active_measurements=active_meas,
        ))

    @Slot(object)
    def _on_step_done(self, result: StepResult):
        if self._phase == DoubleSweepPhase.PRE_INIT:
            self._last_write_value = result.next_v
            if result.is_done:
                self._main_win._log("  First channel at start_point. Advancing second channel.", color="#4ec9b0")
                self._advance_second(self._array[0], prev=None)
            else:
                # no time_per_point delay during pre-init
                self._sweep_timer.start(0)
            return

        if self._phase not in (DoubleSweepPhase.DUMMY,
                               DoubleSweepPhase.TRACE,
                               DoubleSweepPhase.RETRACE):
            return

        # Append to data saver
        meas_map = {row: val for row, val in result.meas_results}
        row_vals = [f"{result.next_v:.6g}"]
        for idx in self._ctx.active_meas_indices:
            val = meas_map.get(idx)
            row_vals.append(f"{val:.6g}" if val is not None else "ERR")
        self._data_saver.append_row(row_vals)

        # Graph update (includes derivative if enabled in main window)
        if self._main_win._graph_window is not None:
            try:
                from gui.graph_window import GraphDataPoint
                from core.derivative_channel import OUTPUT_KEY as _DERIV_KEY
                _phase_str = {
                    DoubleSweepPhase.DUMMY:   "dummy",
                    DoubleSweepPhase.TRACE:   "trace",
                    DoubleSweepPhase.RETRACE: "retrace",
                }.get(self._phase, "")
                gvals = {"__sweep__": result.next_v}
                # Use context snapshot to avoid stale profile reference
                for row, alias, desc, cmd in self._ctx.active_measurements:
                    val = meas_map.get(row)
                    if val is not None:
                        gvals[desc] = val
                # Derivative from shared channel (uses main_win's deriv_channel instance)
                deriv_val = self._main_win._deriv_val_for_step(result, meas_map)
                if self._main_win._deriv_channel._cfg.enabled:
                    # Always append (NaN when not computable) to keep X/Y arrays aligned
                    gvals[_DERIV_KEY] = deriv_val if deriv_val is not None else float("nan")
                self._main_win._graph_window.append_point(GraphDataPoint(values=gvals, phase=_phase_str))
            except Exception as _e:
                self._main_win._log(f"  [Graph] append_point failed: {_e}", color="#f44747")

        self._last_write_value = result.next_v

        if result.is_done:
            self._advance_phase()
        else:
            t_before_timer = time.perf_counter()
            elapsed_ms = int((t_before_timer - result.timing.t_emit) * 1000)
            interval_ms = max(0, int(self._cfg.time_per_point * 1000) - elapsed_ms)
            self._sweep_timer.start(interval_ms)

    @Slot(str)
    def _on_step_error(self, msg: str):
        self._main_win._log(f"  [DoubleSweep] Step error: {msg}", color="#f44747")
        self._on_stop()

    # ------------------------------------------------------------------
    # Glow animation
    # ------------------------------------------------------------------

    def _update_glow(self):
        self._glow_phase += 0.07
        intensity = (math.sin(self._glow_phase) + 1) / 2
        alpha = int(80 + intensity * 140)
        green = int(140 + intensity * 80)
        self._glow_frame.setStyleSheet(
            f"QFrame#dsGlowFrame {{"
            f"border: 3px solid rgba(40, {green}, 70, {alpha});"
            f"border-radius: 6px; }}"
        )

    def _stop_glow(self):
        self._glow_timer.stop()
        self._glow_frame.setStyleSheet(
            "QFrame#dsGlowFrame { border: 3px solid transparent; border-radius: 6px; }"
        )

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def showEvent(self, event):
        # Worker threads are quit in closeEvent. Restart them if needed.
        if not self._worker_thread.isRunning():
            self._worker_thread.start()
        if not self._second_thread.isRunning():
            self._second_thread.start()
        self._rebuild_second_channel_radios()
        if self._phase == DoubleSweepPhase.IDLE:
            self._btn_start.setEnabled(not self._main_win._running)
        super().showEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._save_config()
            self.hide()
            event.accept()
            return
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_S):
            self._save_config()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._phase != DoubleSweepPhase.IDLE:
            reply = QMessageBox.question(
                self, "종료 확인",
                "Double Sweep이 실행 중입니다. 중단하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            self._on_stop()
        self._save_config()
        self._worker_thread.quit()
        self._worker_thread.wait()
        self._second_thread.quit()
        self._second_thread.wait()
        event.accept()
