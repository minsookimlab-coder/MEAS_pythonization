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
from datetime import (
    date as _date,
    datetime,
    datetime as _datetime,
    timedelta as _timedelta,
)
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, TYPE_CHECKING, Tuple

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QDoubleValidator, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
import os
import subprocess
from pathlib import Path

from pythonization.config.models import (
    AlarmConfig,
    DoubleSweepConfig,
    InstantiatedSecondSweepChannel,
    SecondSweepAdvanceType,
)
from pythonization.notify.alarm_manager import AlarmManager
from pythonization.measurement.data_saver import DataSaver
from pythonization.measurement.second_channel_worker import (
    SecondChannelRequest,
    SecondChannelWorker,
)
from pythonization.measurement.sweep_worker import StepRequest, StepResult, SweepWorker
from pythonization.ui.dialogs.alarm_config import AlarmConfigWindow, MeasCondPanel
from pythonization.instruments.errors import humanize_error, is_comm_error
from pythonization.measurement.resume_log import ResumeLog, ResumePoint
from pythonization.measurement.second_channel_model import SecondChannelModel
from pythonization.ui.dialogs.resume import ResumePickerDialog
from pythonization.ui.panels.graph_window import GraphDataPoint
from pythonization.ui.widgets.help_button import make_help_button

if TYPE_CHECKING:
    from pythonization.ui.main_window import MainWindow

_MONO = QFont("Consolas", 10)

# FEEDBACK 구획의 '?' 배지 툴팁. 판정이 2단계(근접 → 안정화)라 설명이 길어
# 빌더 안에 두면 코드가 파묻힌다.
_FEEDBACK_HELP_HTML = (
    "<html><head/><body style='white-space:normal;'>"
    "<b>FEEDBACK Channel Settings 도움말</b><hr>"
    "<table cellspacing='4' cellpadding='2'>"
    "<tr valign='top'><td><b>Read&nbsp;Cmd</b></td>"
    "<td>안정화 감지용 VISA 쿼리 명령어.<br>"
    "예&nbsp;①&nbsp;<code>print(smua.measure.v())</code>&nbsp;(Keithley TSP)<br>"
    "예&nbsp;②&nbsp;<code>READ:DEV:GRPZ:PSU:SIG:FLD</code>&nbsp;(Mercury iPS)<br>"
    "응답에 단위 접미사(T, A…)가 붙어도 자동 파싱됩니다.<br>"
    "Profile에 이미 설정돼 있으면 자동 비활성화.</td></tr>"
    "<tr valign='top'><td><b>Poll&nbsp;Interval</b></td>"
    "<td>Read Cmd를 반복 실행하는 간격 (초).<br>"
    "짧을수록 빠른 감지, 길수록 장비 부하 감소.<br>"
    "권장: 0.5 ~ 5 s</td></tr>"
    "<tr valign='top'><td><b>Tolerance</b></td>"
    "<td><b>Phase 1</b> 조건 — 목표 근접도 판정.<br>"
    "ratio = |읽은값 − 이전값| / |목표값 − 이전값|<br>"
    "ratio ≥ tolerance/100 이면 안정화 체크(Phase 2) 시작.<br>"
    "기본값: 95 %  ·  <b>자기장 권장: 98 %</b></td></tr>"
    "<tr valign='top'><td><b>Std&nbsp;Window</b></td>"
    "<td><b>Phase 2</b> 안정화 판정에 쓸 샘플 수.<br>"
    "0 → 비활성화: Tolerance 통과만으로 다음 단계 진행.<br>"
    "N&gt;0 → 최근 N개 샘플의 표준편차로 안정성 평가.</td></tr>"
    "<tr valign='top'><td><b>Noise&nbsp;Floor</b></td>"
    "<td>정규화 분모 하한 (측정값과 동일한 단위).<br>"
    "<b>목표값이 0 근처이면 반드시 설정해야 합니다.</b><br>"
    "<b>자기장 권장: 0.001 T</b>  ·  전류 → 1e-9 A</td></tr>"
    "<tr valign='top'><td><b>Std&nbsp;Threshold</b></td>"
    "<td>안정화 판정 임계값 (무차원).<br>"
    "metric = std / (|목표값| + Noise Floor)<br>"
    "metric &lt; Std Threshold 이면 안정화 완료 → 다음 단계 진행.<br>"
    "기본값: 0.01 (목표값 대비 1 %)  ·  <b>자기장 권장: 0.0003</b><br>"
    "옆의 <i>now</i> 숫자가 실시간으로 현재 metric을 표시합니다.</td></tr>"
    "</table></body></html>"
)


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
    RETURNING_ZERO   = auto()   # second channel → 0 after all array steps (no measurement)
    ADVANCING_SECOND = auto()
    DUMMY            = auto()
    TRACE            = auto()
    RETRACE          = auto()
    CYCLE            = auto()


#: 그래프에서 곡선을 구분하는 phase 이름. 여기 없는 phase 는 데이터를 남기지 않는다.
_GRAPH_PHASE = {
    DoubleSweepPhase.DUMMY:   "dummy",
    DoubleSweepPhase.TRACE:   "trace",
    DoubleSweepPhase.RETRACE: "retrace",
    DoubleSweepPhase.CYCLE:   "cycle",
}


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


def _estimate_total_seconds(
    cfg: "DoubleSweepConfig",
    n_array: int,
    second_ch: "Optional[InstantiatedSecondSweepChannel]" = None,
) -> float:
    """Estimate total sweep time in seconds.

    Rules:
    - time = distance * 60 / rate  (tpp is measurement interval only, does not affect travel time)
    - retrace_to_zero: retrace goes to 0; dummy goes from 0 to start_point
    - second channel advance time: SWEEP → step * 60 / sweep_rate; WAIT_FOR_TIME → wait_time; others → 0
    """
    if n_array == 0:
        return 0.0

    def _sweep_time(dist: float, rate: float) -> float:
        if rate <= 0 or dist <= 0:
            return 0.0
        return dist * 60.0 / rate

    trace_dist   = abs(cfg.stop_point - cfg.start_point)
    if cfg.retrace_to_zero:
        retrace_dist = abs(cfg.stop_point)
        dummy_dist   = abs(cfg.start_point)
    else:
        retrace_dist = trace_dist
        dummy_dist   = 0.0

    trace_time   = _sweep_time(trace_dist,   cfg.rate_trace)
    retrace_time = _sweep_time(retrace_dist, cfg.rate_retrace)
    dummy_time   = _sweep_time(dummy_dist,   cfg.rate_dummy)

    def _feedback_min(ch) -> float:
        # feedback 최소 소요 ≈ poll_interval × (Phase1 1회 + Phase2 std_window회)
        poll = getattr(ch, "feedback_poll_interval", 1.0) or 0.0
        win  = getattr(ch, "feedback_std_window", 0) or 0
        return poll * (1 + max(0, win))

    advance_time = 0.0
    if second_ch is not None:
        if second_ch.advance_type == SecondSweepAdvanceType.SWEEP and second_ch.sweep_rate > 0:
            step = abs(cfg.array_step) if abs(cfg.array_step) > 1e-12 else 0.0
            advance_time = _sweep_time(step, second_ch.sweep_rate)
        elif second_ch.advance_type == SecondSweepAdvanceType.WAIT_FOR_TIME:
            advance_time = getattr(second_ch, "wait_time", 0.0)
        elif second_ch.advance_type == SecondSweepAdvanceType.FEEDBACK:
            advance_time = _feedback_min(second_ch)

    time_per_cycle = advance_time + dummy_time + trace_time + retrace_time
    total = n_array * time_per_cycle

    # to_zero_at_last: add time for final second channel → 0 move
    if cfg.to_zero_at_last and second_ch is not None:
        last_val = cfg.array_from + (n_array - 1) * cfg.array_step
        if second_ch.advance_type == SecondSweepAdvanceType.SWEEP and second_ch.sweep_rate > 0:
            total += _sweep_time(abs(last_val), second_ch.sweep_rate)
        elif second_ch.advance_type == SecondSweepAdvanceType.WAIT_FOR_TIME:
            total += getattr(second_ch, "wait_time", 0.0)
        elif second_ch.advance_type == SecondSweepAdvanceType.FEEDBACK:
            total += _feedback_min(second_ch)

    return total


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


# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
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
        self.setMinimumWidth(900)
        self.resize(1200, 680)
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
        # Feature 1: second(array) 값 테이블 모델 + 편집 창 (VNA double sweep과 동일한 창 재사용)
        self._second_table_model = SecondChannelModel()
        self._second_table_win = None
        self._last_write_value: Optional[float] = None
        self._second_channel: Optional[InstantiatedSecondSweepChannel] = None
        self._ctx: Optional[DoubleSweepContext] = None   # set at sweep start
        self._cfg: Optional[DoubleSweepConfig] = None   # set at sweep start
        self._last_meas_values: dict = {}  # {description: float} 최신 측정값 캐시
        self._trace_filepath = None        # TRACE 파일 경로 (meta data JSON 저장용)
        self._alarm_manager = AlarmManager()
        # 알람 전달·텔레그램·고정트리거 설정 (Alarm Config 창에서 편집).
        # 측정값 조건 트리거는 MeasCondPanel이 별도 관리.
        self._alarm_cfg: AlarmConfig = AlarmConfig()

        # 통신 오류 자동 재개 상태
        self._resume_log = ResumeLog()
        self._auto_retry_used = False        # 연속 자동 재개 1회 제한 (성공 시 리셋)
        self._last_emit = None               # ("step"|"advance", request) 자동 재개 재전송용
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._auto_resume)

        # DataSaver
        self._data_saver = DataSaver()
        self._data_saver.set_error_callback(
            lambda msg: main_win._log(f"  [DoubleSweep DataSaver] {msg}", color="#f44747")
        )

        # Sweep worker
        self._sweep_worker = SweepWorker()
        self._sweep_worker.set_session(main_win._session)
        # 전역 설정(임계값·병렬 측정) 동기화 — main worker와 동일하게
        self._sweep_worker.set_threshold(main_win._app_config.global_threshold)
        self._sweep_worker.set_parallel(main_win._app_config.parallel_measurement)
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
        self._second_worker.advance_failed.connect(self._on_advance_timeout)
        # bound 슬롯으로 연결 (워커 스레드에서 GUI(_log→debug console) 접근 시 크래시 방지)
        self._second_worker.status.connect(self._on_second_status)
        self._second_worker.feedback_progress.connect(self._on_feedback_metric)
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
        self._rebuild_second_channel_radios()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        """창 전체 조립. 각 구획은 아래 _build_*_box() 가 만들어 돌려준다.

        좌측 = 측정 제어(위에서 아래로 진행 순서대로), 우측 = 알람 조건 패널.
        second channel 의 advance type 에 따라 SWEEP/FEEDBACK/WAIT 구획 중 하나만
        보이므로, 셋 다 만들어 두고 숨긴 채 시작한다.
        """
        dialog_layout = QVBoxLayout(self)
        dialog_layout.setContentsMargins(0, 0, 0, 0)
        self._glow_frame = QFrame()
        self._glow_frame.setObjectName("dsGlowFrame")
        self._glow_frame.setStyleSheet(
            "QFrame#dsGlowFrame { border: 3px solid transparent; border-radius: 6px; }"
        )
        dialog_layout.addWidget(self._glow_frame)

        glow_h = QHBoxLayout(self._glow_frame)
        glow_h.setContentsMargins(0, 0, 0, 0)
        glow_h.setSpacing(0)

        left_widget = QWidget()
        outer = QVBoxLayout(left_widget)
        outer.setContentsMargins(10, 10, 6, 10)
        outer.setSpacing(8)
        glow_h.addWidget(left_widget, stretch=1)

        self._right_alarm_scroll = self._build_alarm_panel()
        glow_h.addWidget(self._right_alarm_scroll)

        outer.addLayout(self._build_title_row())
        outer.addWidget(self._build_second_channel_box())
        outer.addWidget(self._build_sweep_params_box())
        outer.addWidget(self._build_cycle_box())
        outer.addWidget(self._build_sweep_advance_box())
        outer.addWidget(self._build_feedback_box())
        outer.addWidget(self._build_wait_time_box())
        outer.addWidget(self._build_array_box())
        outer.addLayout(self._build_array_table_row())
        outer.addLayout(self._build_estimate_row())
        self._connect_estimate_inputs()
        outer.addWidget(self._build_status_box())
        outer.addLayout(self._build_action_row())
        outer.addLayout(self._build_save_path_row())
        outer.addStretch()

    # ── _build_ui 가 쓰는 공용 입력 위젯 ──────────────────────────────────

    def _make_value_field(self, width: int = 160):
        """QLineEdit + 단위 QLabel + 둘을 담은 컨테이너. 반환 (입력, 단위라벨, 컨테이너)."""
        le = QLineEdit()
        le.setFont(_MONO)
        le.setFixedWidth(width)
        validator = QDoubleValidator()
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        le.setValidator(validator)

        unit = QLabel("")
        unit.setFont(_MONO)
        unit.setStyleSheet("color: #888888;")
        unit.setMinimumWidth(60)

        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(le)
        row.addWidget(unit)
        row.addStretch()
        return le, unit, container

    def _make_inline_field(self, width: int = 90):
        """QLineEdit + 단위 QLabel만 (컨테이너 없음 — parent-child GC 문제 회피)."""
        le = QLineEdit()
        le.setFont(_MONO)
        le.setFixedWidth(width)
        validator = QDoubleValidator()
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        le.setValidator(validator)

        unit = QLabel("")
        unit.setFont(_MONO)
        unit.setStyleSheet("color: #888888;")
        unit.setMinimumWidth(50)
        return le, unit

    def _build_cycle_box(self) -> QFrame:
        """Cycle Sweep 설정 — Double Sweep에서 first 축을 cycle sequence로 사용."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("Cycle Sweep (per-second step)")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        row = QHBoxLayout()
        self._cb_cycle_enable = QCheckBox("Enable Cycle Sweep")
        self._cb_cycle_enable.setFont(_MONO)
        row.addWidget(self._cb_cycle_enable)
        row.addStretch()
        layout.addLayout(row)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)

        self._le_cycle_targets = QLineEdit()
        self._le_cycle_targets.setFont(_MONO)
        self._le_cycle_targets.setPlaceholderText("e.g. 30,-30,30,0")
        form.addRow("Targets (comma):", self._le_cycle_targets)

        self._sb_cycle_repeats = QSpinBox()
        self._sb_cycle_repeats.setRange(1, 1000)
        self._sb_cycle_repeats.setValue(1)
        self._sb_cycle_repeats.setFont(_MONO)
        self._sb_cycle_repeats.setFixedWidth(100)
        form.addRow("Repeats:", self._sb_cycle_repeats)

        layout.addLayout(form)
        return frame

    # ── 구획별 빌더 ───────────────────────────────────────────────────────

    def _build_title_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title = QLabel("Double Sweep")
        title.setStyleSheet("font-weight: bold; font-size: 13px; color: #f78166;")
        row.addWidget(title)
        row.addStretch()
        row.addWidget(make_help_button(self._ds_help_html(), "Double Sweep 도움말"))
        return row

    def _build_alarm_panel(self) -> QScrollArea:
        """우측 패널 — 측정값 조건 + Alarm Config 버튼.

        전달 수단(사운드·이메일·텔레그램) 설정은 별도 창에 있다.
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(260)
        scroll.setMaximumWidth(320)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        btn = QPushButton("⚙ Alarm Config…")
        btn.setToolTip("알람 활성화 / 사운드·이메일·텔레그램 / 고정 트리거 설정")
        btn.clicked.connect(self._open_alarm_config)
        layout.addWidget(btn)

        self._meascond_panel = MeasCondPanel()
        layout.addWidget(self._meascond_panel)
        layout.addStretch()

        scroll.setWidget(panel)
        return scroll

    def _build_second_channel_box(self) -> QFrame:
        """second channel 선택 라디오 목록. 항목은 프로파일 로드 후 채워진다."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("Second Channel")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #f78166;")
        layout.addWidget(title)

        self._second_ch_layout = QVBoxLayout()
        layout.addLayout(self._second_ch_layout)
        self._second_radio_group = QButtonGroup(self)
        self._second_radio_group.setExclusive(True)
        self._second_radio_group.idToggled.connect(self._on_second_radio_toggled)

        self._ch_frame = frame
        return frame

    def _build_sweep_params_box(self) -> QFrame:
        """주 sweep 파라미터 — 시작/끝, phase별 rate, 포인트당 시간."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("Sweep Parameters")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        layout.addLayout(form)

        self._le_start,  self._lbl_start_unit,  cnt_start  = self._make_value_field()
        self._le_stop,   self._lbl_stop_unit,   cnt_stop   = self._make_value_field()
        self._le_rate_t, self._lbl_rate_t_unit, cnt_rate_t = self._make_value_field()
        self._le_rate_r, self._lbl_rate_r_unit, cnt_rate_r = self._make_value_field()
        self._le_rate_d, self._lbl_rate_d_unit, cnt_rate_d = self._make_value_field()
        self._le_tpp,    lbl_tpp_unit,          cnt_tpp    = self._make_value_field()
        lbl_tpp_unit.setText("sec")

        self._cb_retrace_to_zero = QCheckBox("Retrace to 0")
        self._cb_retrace_to_zero.setFont(_MONO)
        self._cb_retrace_to_zero.setToolTip(
            "When checked, RETRACE sweeps to 0 instead of Start Point")

        form.addRow("Start Point:",    cnt_start)
        form.addRow("Stop Point:",     cnt_stop)
        form.addRow("Rate (trace):",   cnt_rate_t)
        form.addRow("Rate (retrace):", cnt_rate_r)
        form.addRow("",                self._cb_retrace_to_zero)
        form.addRow("Rate (dummy):",   cnt_rate_d)
        form.addRow("Time / Point:",   cnt_tpp)

        self._sp_frame = frame
        return frame

    def _build_sweep_advance_box(self) -> QFrame:
        """advance_type == SWEEP 일 때만 보이는 구획 (safety ramp 포함)."""
        self._sweep_ch_frame = QFrame()
        self._sweep_ch_frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self._sweep_ch_frame)
        layout.setContentsMargins(8, 6, 8, 6)

        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("SWEEP Channel Settings")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #c586c0;")
        header.addWidget(title)
        header.addWidget(make_help_button(self._safety_ramp_help_html(),
                                          "Safety Ramp 도움말"))
        header.addStretch()
        layout.addLayout(header)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        layout.addLayout(form)

        self._le_second_rate, self._lbl_second_rate_unit, cnt_rate = self._make_value_field()
        form.addRow("Sweep Rate:", cnt_rate)

        self._cb_second_safety = QCheckBox("Use Safety Ramp")
        self._cb_second_safety.setFont(_MONO)
        form.addRow("", self._cb_second_safety)

        self._sb_second_steps = QSpinBox()
        self._sb_second_steps.setRange(0, 100000)
        self._sb_second_steps.setValue(0)
        self._sb_second_steps.setFont(_MONO)
        self._sb_second_steps.setFixedWidth(100)
        self._sb_second_steps.setEnabled(False)
        form.addRow("Safety Steps:", self._sb_second_steps)

        self._le_second_interval, lbl_interval_unit, cnt_interval = self._make_value_field()
        lbl_interval_unit.setText("ms")
        self._le_second_interval.setEnabled(False)
        form.addRow("Safety Interval:", cnt_interval)

        # safety ramp 를 켤 때만 스텝/간격 입력을 연다
        self._cb_second_safety.toggled.connect(self._sb_second_steps.setEnabled)
        self._cb_second_safety.toggled.connect(self._le_second_interval.setEnabled)

        self._sweep_ch_frame.setVisible(False)
        return self._sweep_ch_frame

    def _build_feedback_box(self) -> QFrame:
        """advance_type == FEEDBACK 일 때만 보이는 구획.

        2단계 판정이다: Tolerance(목표 근접) 통과 후 Std Window/Threshold(안정화).
        자세한 의미는 _FEEDBACK_HELP_HTML 참고.
        """
        self._feedback_frame = QFrame()
        self._feedback_frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self._feedback_frame)
        layout.setContentsMargins(8, 6, 8, 6)

        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("FEEDBACK Channel Settings")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #4ec9b0;")
        header.addWidget(title)
        help_badge = QLabel("?")
        help_badge.setFixedSize(16, 16)
        help_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        help_badge.setStyleSheet(
            "background-color: #4ec9b0; color: #1e1e1e; border-radius: 8px;"
            "font-weight: bold; font-size: 10px;"
        )
        help_badge.setToolTip(_FEEDBACK_HELP_HTML)
        header.addWidget(help_badge)
        header.addStretch()
        layout.addLayout(header)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        layout.addLayout(form)

        # Read Cmd — 프로파일에 이미 설정돼 있으면 _update_feedback_read_cmd_state 가 잠근다
        read_row = QWidget()
        read_lay = QHBoxLayout(read_row)
        read_lay.setContentsMargins(0, 0, 0, 0)
        read_lay.setSpacing(6)
        self._le_fb_read_cmd = QLineEdit()
        self._le_fb_read_cmd.setFont(_MONO)
        self._le_fb_read_cmd.setPlaceholderText("예: print(smua.measure.v())")
        read_lay.addWidget(self._le_fb_read_cmd)
        self._lbl_fb_read_hint = QLabel("")
        self._lbl_fb_read_hint.setFont(_MONO)
        self._lbl_fb_read_hint.setStyleSheet("color: #888888; font-size: 9px;")
        read_lay.addWidget(self._lbl_fb_read_hint)
        form.addRow("Read Cmd:", read_row)

        self._le_fb_poll, lbl_poll_unit, cnt_poll = self._make_value_field(100)
        lbl_poll_unit.setText("s")
        form.addRow("Poll Interval:", cnt_poll)

        self._le_fb_tol, lbl_tol_unit, cnt_tol = self._make_value_field(100)
        lbl_tol_unit.setText("%")
        form.addRow("Tolerance:", cnt_tol)

        self._sb_fb_std_window = QSpinBox()
        self._sb_fb_std_window.setRange(0, 10000)
        self._sb_fb_std_window.setValue(0)
        self._sb_fb_std_window.setFont(_MONO)
        self._sb_fb_std_window.setFixedWidth(100)
        self._sb_fb_std_window.setToolTip(
            "0 = stability check 비활성화; N>0 = 최근 N개 샘플의 std 검사")
        form.addRow("Std Window:", self._sb_fb_std_window)

        self._le_fb_noisefloor = QLineEdit()
        self._le_fb_noisefloor.setFont(_MONO)
        self._le_fb_noisefloor.setFixedWidth(100)
        self._le_fb_noisefloor.setToolTip("next_v ≈ 0 일 때 반드시 설정. 예: 1e-6")
        form.addRow("Noise Floor:", self._le_fb_noisefloor)

        form.addRow("Std Threshold:", self._build_std_threshold_row())

        self._feedback_frame.setVisible(False)
        return self._feedback_frame

    def _build_std_threshold_row(self) -> QWidget:
        """목표 임계값 입력 옆에 실시간 현재 metric 을 나란히 보여 준다."""
        self._le_fb_std_thresh, lbl_unit, cnt_thresh = self._make_value_field(100)
        lbl_unit.setText("(dimensionless)")

        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(cnt_thresh)
        lay.addWidget(QLabel("now:"))
        self._lbl_fb_metric = QLabel("—")
        self._lbl_fb_metric.setFont(_MONO)
        self._lbl_fb_metric.setMinimumWidth(70)
        self._lbl_fb_metric.setStyleSheet("color: #888888;")
        lay.addWidget(self._lbl_fb_metric)
        lay.addStretch()
        return row

    def _build_wait_time_box(self) -> QFrame:
        """advance_type == WAIT_FOR_TIME 일 때만 보이는 구획."""
        self._wait_frame = QFrame()
        self._wait_frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self._wait_frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("WAIT_FOR_TIME Channel Settings")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #dcdcaa;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        layout.addLayout(form)

        self._le_wait_time, lbl_unit, cnt_wait = self._make_value_field(100)
        lbl_unit.setText("s")
        form.addRow("Wait Time:", cnt_wait)

        self._wait_frame.setVisible(False)
        return self._wait_frame

    def _build_array_box(self) -> QFrame:
        """second channel 이 훑을 값 목록 — From/To/Step."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("Second Channel Array")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        self._le_arr_from, self._lbl_arr_from_unit = self._make_inline_field()
        self._le_arr_to,   self._lbl_arr_to_unit   = self._make_inline_field()
        self._le_arr_step, self._lbl_arr_step_unit = self._make_inline_field()

        row = QHBoxLayout()
        row.addWidget(QLabel("From:"))
        row.addWidget(self._le_arr_from)
        row.addWidget(self._lbl_arr_from_unit)
        row.addWidget(QLabel("To:"))
        row.addWidget(self._le_arr_to)
        row.addWidget(self._lbl_arr_to_unit)
        row.addWidget(QLabel("Step:"))
        row.addWidget(self._le_arr_step)
        row.addWidget(self._lbl_arr_step_unit)
        self._lbl_n_points = QLabel("→ — pts")
        self._lbl_n_points.setStyleSheet("color: #888888;")
        row.addWidget(self._lbl_n_points)
        layout.addLayout(row)

        self._cb_to_zero_at_last = QCheckBox("to 0 at last step")
        self._cb_to_zero_at_last.setFont(_MONO)
        self._cb_to_zero_at_last.setToolTip(
            "마지막 array step 완료 후 second channel을 0으로 전송합니다.\n"
            "SWEEP type: advance type 그대로 0까지 sweep\n"
            "기타 type: VISA write 명령어로 즉시 0 전송"
        )
        layout.addWidget(self._cb_to_zero_at_last)

        for field in (self._le_arr_from, self._le_arr_to, self._le_arr_step):
            field.textChanged.connect(self._update_n_points)

        self._arr_frame = frame
        return frame

    def _build_array_table_row(self) -> QHBoxLayout:
        """array 값 테이블 편집 창 버튼.

        _arr_frame 바깥에 둔다 — 측정 중에는 _arr_frame 이 잠기지만, 아직 측정하지
        않은 행은 계속 편집할 수 있어야 하기 때문이다.
        """
        row = QHBoxLayout()
        self._btn_ds_second_table = QPushButton("Array 값 테이블…")
        self._btn_ds_second_table.setToolTip(
            "Second channel array 값을 표로 편집하는 창을 엽니다.\n"
            "측정 중에도 아직 측정 안 한(대기) 행은 값 수정·추가·삭제할 수 있습니다.")
        self._btn_ds_second_table.clicked.connect(self._open_second_table)
        row.addWidget(self._btn_ds_second_table)

        self._cb_ds_keep_table = QCheckBox("테이블 초기화 안 함")
        self._cb_ds_keep_table.setFont(_MONO)
        self._cb_ds_keep_table.setToolTip(
            "체크 시 시작할 때 From/To/Step으로 테이블을 새로 만들지 않고,\n"
            "테이블 창에서 직접 넣은 값 목록 그대로 측정합니다.")
        row.addWidget(self._cb_ds_keep_table)
        row.addStretch()
        return row

    def _build_estimate_row(self) -> QHBoxLayout:
        """예상 소요 시간 + 종료 예상 시각."""
        row = QHBoxLayout()
        row.addWidget(QLabel("Estimated:"))
        self._lbl_est_time = QLabel("—")
        self._lbl_est_time.setFont(_MONO)
        self._lbl_est_time.setStyleSheet("color: #79c0ff; font-weight: bold;")
        row.addWidget(self._lbl_est_time)

        row.addSpacing(10)
        eta_label = QLabel("종료 예상:")
        eta_label.setStyleSheet("color: #888888;")
        row.addWidget(eta_label)
        self._lbl_eta = QLabel("—")
        self._lbl_eta.setFont(_MONO)
        self._lbl_eta.setStyleSheet("color: #56d364; font-weight: bold;")
        row.addWidget(self._lbl_eta)
        row.addStretch()
        return row

    def _connect_estimate_inputs(self):
        """예상 시간에 영향을 주는 입력이 바뀌면 다시 계산한다.

        앞선 구획들이 위젯을 다 만든 뒤에 불러야 한다.
        """
        for field in (self._le_start, self._le_stop,
                      self._le_rate_t, self._le_rate_r, self._le_rate_d,
                      self._le_tpp,
                      self._le_arr_from, self._le_arr_to, self._le_arr_step,
                      self._le_second_rate):
            field.textChanged.connect(self._update_est_time)
        self._cb_retrace_to_zero.stateChanged.connect(self._update_est_time)
        self._cb_to_zero_at_last.stateChanged.connect(self._update_est_time)

    def _build_status_box(self) -> QFrame:
        """진행 상태 한 줄 — phase / second 값 / array 진척 / 마지막 알람."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 4, 8, 4)

        self._lbl_phase = QLabel("IDLE")
        self._lbl_phase.setFont(_MONO)
        self._lbl_phase.setStyleSheet("color: #888888;")
        layout.addWidget(QLabel("Phase:"))
        layout.addWidget(self._lbl_phase)

        layout.addSpacing(12)
        layout.addWidget(QLabel("Second:"))
        self._lbl_second_val = QLabel("—")
        self._lbl_second_val.setFont(_MONO)
        self._lbl_second_val.setStyleSheet("color: #f78166;")
        layout.addWidget(self._lbl_second_val)

        layout.addSpacing(12)
        layout.addWidget(QLabel("Array:"))
        self._lbl_array_progress = QLabel("—/—")
        self._lbl_array_progress.setFont(_MONO)
        self._lbl_array_progress.setStyleSheet("color: #79c0ff;")
        layout.addWidget(self._lbl_array_progress)
        layout.addStretch()

        layout.addSpacing(12)
        layout.addWidget(QLabel("Alarm:"))
        self._lbl_last_alarm = QLabel("(없음)")
        self._lbl_last_alarm.setFont(_MONO)
        self._lbl_last_alarm.setStyleSheet("color: #888888; font-size: 10px;")
        self._lbl_last_alarm.setMaximumWidth(200)
        layout.addWidget(self._lbl_last_alarm)
        return frame

    def _build_action_row(self) -> QHBoxLayout:
        """Start / Stop / Resume."""
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
        self._btn_stop.clicked.connect(self._on_stop_clicked)

        # 통신 오류로 중단된 Double Sweep 을 저장된 array 지점부터 재개
        self._btn_resume = QPushButton("Resume")
        self._btn_resume.setMinimumHeight(40)
        self._btn_resume.setToolTip(
            "통신 오류로 중단된 Double Sweep을 저장된 array 지점부터 재개")
        self._btn_resume.setStyleSheet(
            "QPushButton { font-weight: bold; font-size: 13px;"
            "background-color: #b8860b; color: white; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #3a3010; color: #55502d; border-radius: 4px; }"
        )
        self._btn_resume.clicked.connect(self._on_resume_clicked)

        row = QHBoxLayout()
        row.addWidget(self._btn_start)
        row.addWidget(self._btn_stop)
        row.addWidget(self._btn_resume)
        self._update_resume_btn_enabled()
        return row

    def _build_save_path_row(self) -> QHBoxLayout:
        """trace 폴더 기준 저장 경로 미리보기 + 폴더 열기 버튼."""
        row = QHBoxLayout()
        label = QLabel("Trace →")
        label.setFont(_MONO)
        label.setStyleSheet("color: #888888;")
        row.addWidget(label)

        self._lbl_ds_save_path = QLabel("—")
        self._lbl_ds_save_path.setFont(QFont("Consolas", 8))
        self._lbl_ds_save_path.setStyleSheet("color: #555555;")
        self._lbl_ds_save_path.setWordWrap(True)
        # 긴 경로가 잘리지 않도록 약 2줄 높이 확보 + 위쪽 정렬
        self._lbl_ds_save_path.setMinimumHeight(28)
        self._lbl_ds_save_path.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        row.addWidget(self._lbl_ds_save_path, stretch=1)

        btn_open = QPushButton("📂")
        btn_open.setFixedSize(28, 20)
        btn_open.setFont(_MONO)
        btn_open.setToolTip("저장 폴더 열기")
        btn_open.clicked.connect(self._open_ds_folder)
        row.addWidget(btn_open, alignment=Qt.AlignmentFlag.AlignTop)
        return row

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
            self._feedback_frame.setVisible(False)
            self._wait_frame.setVisible(False)
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
            adv = self._second_channel.advance_type
            # THRESHOLD_TIME = 도달(feedback 필드) + 고정 시간 대기(wait_time) → 두 프레임 모두
            is_tt = adv == SecondSweepAdvanceType.THRESHOLD_TIME
            self._sweep_ch_frame.setVisible(adv == SecondSweepAdvanceType.SWEEP)
            self._feedback_frame.setVisible(adv == SecondSweepAdvanceType.FEEDBACK or is_tt)
            self._wait_frame.setVisible(adv == SecondSweepAdvanceType.WAIT_FOR_TIME or is_tt)
            self._update_feedback_read_cmd_state()
            self._update_unit_labels()

    @staticmethod
    def _safety_ramp_help_html() -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>Safety Ramp — 값을 한 번에 확 바꾸지 않고 천천히 올리기</b><hr>"
            "두 번째 축(예: 전압·전류)을 다음 목표값으로 옮길 때, 보통은 <b>한 번에 훌쩍</b> "
            "바뀝니다. 이 기능을 켜면 그 변화를 <b>여러 개의 작은 계단</b>으로 쪼개서 "
            "한 칸씩 천천히 올리거나 내립니다.<hr>"
            "<b>왜 쓰나요?</b><br>"
            "값이 갑자기 크게 튀면 시료(샘플)나 장비에 충격이 가거나, 순간적으로 큰 전류가 "
            "흘러 손상될 수 있습니다. 천천히 바꾸면 이런 위험을 줄여줍니다.<hr>"
            "<b>설정 항목</b><br>"
            "&nbsp;&nbsp;• <b>Safety Steps</b>: 목표값까지 가는 데 몇 개의 계단으로 나눌지. "
            "예를 들어 0V→1V로 갈 때 10으로 두면 0.1V씩 10번에 걸쳐 올립니다. "
            "숫자가 클수록 더 부드럽지만 그만큼 느려집니다.<br>"
            "&nbsp;&nbsp;• <b>Safety Interval</b>: 계단 한 칸과 다음 칸 사이에 기다리는 시간(ms). "
            "장비와 시료가 안정될 시간을 줍니다.<hr>"
            "<b>권장 사용처</b><br>"
            "<b>Keithley 2636A</b> SMU(전압·전류원)처럼 시료에 직접 전압·전류를 거는 장비에는 "
            "이 기능을 <b>켜는 것을 권장</b>합니다. 자기장·온도처럼 장비 자체가 이미 천천히 "
            "변하는 경우에는 보통 꺼두어도 됩니다.<hr>"
            "<b>참고</b><br>"
            "• 이 설정은 두 번째 축의 <b>Advance Type이 SWEEP</b>일 때만 보입니다.<br>"
            "• 체크를 끄면 Steps·Interval 칸이 비활성화되고, 값은 예전처럼 한 번에 바뀝니다.<br>"
            "• Sweep Rate(쓰는 속도)와 함께 적용됩니다."
            "</body></html>"
        )

    @staticmethod
    def _ds_help_html() -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>Double Sweep — 2차원(지도) 측정</b><hr>"
            "한 축만 쓸어가는 일반 측정과 달리, <b>두 번째 축</b>(예: 자기장·게이트)을 "
            "한 칸씩 옮길 때마다 <b>1차 축 sweep을 통째로 한 번씩</b> 수행합니다. "
            "결과를 모으면 2차원 지도(2D map)가 됩니다.<hr>"

            "<b>■ 진행 방식</b><br>"
            "Array(2차 축 값 목록)의 각 값마다: ① 2차 축을 그 값으로 옮김 → "
            "② 그 자리에서 1차 축을 한 번 sweep → ③ 다음 array 값으로.<hr>"

            "<b>■ Phase: DUMMY / TRACE / RETRACE (헷갈리기 쉬움)</b><br>"
            "각 array 값에서 1차 축을 세 번 쓸어갑니다:<br>"
            "&nbsp;• <b>DUMMY</b>: 시작점→시작점 예열 주행 (dummy 폴더에 저장).<br>"
            "&nbsp;• <b>TRACE</b>: 시작점→끝점 (정방향) — 보통 주 데이터.<br>"
            "&nbsp;• <b>RETRACE</b>: 끝점→시작점(또는 0) (역방향) — 이력현상 확인용.<br>"
            "각 phase는 trace/retrace/dummy <b>폴더로 나뉘어</b> 저장됩니다.<hr>"

            "<b>■ 2차 축 이동 방식 (Advance Type)</b><br>"
            "값을 '어떻게' 옮길지 — 이 선택은 <b>Parameter Manager에서 Second Sweep "
            "Channel을 추가할 때</b> 정합니다. 종류:<br>"
            "<table cellspacing='3' cellpadding='2'>"
            "<tr valign='top'><td><b>simple_hop</b></td>"
            "<td>값을 <b>즉시 한 번에</b> 바꾸고 바로 다음으로. 가장 단순.</td></tr>"
            "<tr valign='top'><td><b>sweep</b></td>"
            "<td>목표값까지 <b>정해진 속도로 천천히</b> 이동(급변 방지). 이동 중 측정 안 함.</td></tr>"
            "<tr valign='top'><td><b>feedback</b></td>"
            "<td>값을 보낸 뒤 실제로 <b>도달·안정될 때까지 기다림</b>. 자기장처럼 "
            "도달에 시간이 걸리고 출렁이는 값에 적합. (세부는 아래 FEEDBACK 설정의 ? 참고)</td></tr>"
            "<tr valign='top'><td><b>wait_for_time</b></td>"
            "<td>값을 보낸 뒤 <b>정해진 시간만큼 기다림</b>.</td></tr>"
            "</table>"

            "<b>■ 주요 설정</b><br>"
            "&nbsp;• <b>Array</b>(from/to/step): 2차 축이 훑을 값 목록.<br>"
            "&nbsp;• <b>Start/Stop, Rate</b>: 1차 축의 시작·끝과 속도(phase별).<br>"
            "&nbsp;• <b>Time/Point</b>: 1차 축 한 점당 시간 간격.<br>"
            "&nbsp;• <b>측정값 조건</b>(우측): 특정 측정이 임계값을 넘으면 알람. 알람 켜기·텔레그램은 "
            "<b>⚙ Alarm Config</b> 창에서.<hr>"

            "<b>■ 참고</b><br>"
            "&nbsp;• 1차 축·측정 항목은 Main 화면 설정을 그대로 씁니다.<br>"
            "&nbsp;• 통신 오류 시 10초 뒤 자동 재시도, 다시 실패하면 멈추고 그 지점을 저장 → "
            "<b>Resume</b>로 재개.<br>"
            "&nbsp;• Stop으로 멈춰도 그 지점이 저장되어 나중에 Resume할 수 있습니다."
            "</body></html>"
        )

    # ------------------------------------------------------------------
    # Config load / save
    # ------------------------------------------------------------------

    def _load_config(self):
        cfg = self._param_reg.double_sweep_config
        self._le_start.setText(f"{cfg.start_point:g}")
        self._le_stop.setText(f"{cfg.stop_point:g}")
        self._le_rate_t.setText(f"{cfg.rate_trace:g}")
        self._le_rate_r.setText(f"{cfg.rate_retrace:g}")
        self._le_rate_d.setText(f"{cfg.rate_dummy:g}")
        self._le_tpp.setText(f"{cfg.time_per_point:g}")
        self._le_arr_from.setText(f"{cfg.array_from:g}")
        self._le_arr_to.setText(f"{cfg.array_to:g}")
        self._le_arr_step.setText(f"{cfg.array_step:g}")
        self._cb_retrace_to_zero.setChecked(cfg.retrace_to_zero)
        self._cb_to_zero_at_last.setChecked(cfg.to_zero_at_last)
        self._le_second_rate.setText(f"{cfg.second_sweep_rate:g}")
        self._cb_second_safety.setChecked(cfg.second_use_safety)
        self._sb_second_steps.setValue(cfg.second_safety_steps)
        self._le_second_interval.setText(f"{cfg.second_safety_interval_ms:g}")
        # FEEDBACK params — profile cmd가 있으면 상태 먼저 반영 후 저장값 덮어쓰지 않음
        self._update_feedback_read_cmd_state()
        if self._le_fb_read_cmd.isEnabled():
            self._le_fb_read_cmd.setText(cfg.second_feedback_read_cmd)
        self._le_fb_poll.setText(f"{cfg.second_feedback_poll_interval:g}")
        self._le_fb_tol.setText(f"{cfg.second_feedback_tolerance_pct:g}")
        self._sb_fb_std_window.setValue(cfg.second_feedback_std_window)
        self._le_fb_noisefloor.setText(f"{cfg.second_feedback_noisefloor:g}")
        self._le_fb_std_thresh.setText(f"{cfg.second_feedback_std_threshold:g}")
        # WAIT_FOR_TIME params
        self._le_wait_time.setText(f"{cfg.second_wait_time:g}")
        # Cycle fields
        try:
            self._cb_cycle_enable.setChecked(bool(cfg.cycle_enabled))
            self._le_cycle_targets.setText(cfg.cycle_targets_str or "")
            self._sb_cycle_repeats.setValue(getattr(cfg, 'cycle_repeats', 1))
        except Exception:
            pass
        self._update_n_points()
        self._update_est_time()
        # 알람: 전달·텔레그램·고정트리거는 _alarm_cfg에 보관, 측정값 조건은 패널에 로드
        self._alarm_cfg = cfg.alarm.model_copy(deep=True)
        alarm_meas = self._param_reg.main_ui_profile.alarm_measurements
        self._meascond_panel.refresh_measurements(alarm_meas)
        self._meascond_panel.load_triggers(cfg.alarm)

    def reload_from_profile(self):
        """활성 프로파일이 바뀌면 이 창 설정도 새 프로파일 값으로 다시 읽는다.

        이 창은 보관형(한 번 만들어 재사용)이고 showEvent에서 _load_config를
        다시 부르지 않으므로, main_window가 프로파일 전환 시 이 메서드를 호출해
        동기화하지 않으면 처음 만들어진 프로파일 값에 고정되어 다른 프로파일을
        덮어쓰는 문제가 생긴다. 측정 중에는 설정이 꼬이지 않도록 무시한다.
        """
        if self._phase != DoubleSweepPhase.IDLE:
            return
        self._load_config()                  # 시작/정지/rate/feedback/alarm 등 전체 재로딩
        self._rebuild_second_channel_radios()
        self._update_unit_labels()
        self._update_ds_save_path()

    def _open_alarm_config(self):
        """Alarm Config 다이얼로그 — 전달·텔레그램·고정트리거 편집 (측정값 조건은 보존)."""
        # 현재 측정값 조건을 합쳐서 창에 넘김 → 창은 measurement 트리거를 보존
        merged = self._alarm_cfg.model_copy(update={
            "triggers": [t for t in self._alarm_cfg.triggers if t.kind != "measurement"]
                        + self._meascond_panel.measurement_triggers()
        })
        dlg = AlarmConfigWindow(merged, self._alarm_manager, parent=self,
                                title="Double Sweep — Alarm Config")
        dlg.apply_requested.connect(self._on_alarm_cfg_applied)
        dlg.exec()

    def _on_alarm_cfg_applied(self, cfg: AlarmConfig):
        self._alarm_cfg = cfg
        self._save_config()

    def _build_alarm_config(self) -> AlarmConfig:
        """저장용 최종 AlarmConfig = 전달·텔레그램·고정트리거(_alarm_cfg) + 측정값 조건(패널)."""
        non_meas = [t for t in self._alarm_cfg.triggers if t.kind != "measurement"]
        return self._alarm_cfg.model_copy(update={
            "triggers": non_meas + self._meascond_panel.measurement_triggers()
        })

    def _save_config(self):
        cfg = DoubleSweepConfig(
            start_point=self._parse_ds_float(self._le_start.text(), 0.0),
            stop_point=self._parse_ds_float(self._le_stop.text(), 1.0),
            rate_trace=self._parse_ds_float(self._le_rate_t.text(), 1.0),
            rate_retrace=self._parse_ds_float(self._le_rate_r.text(), 1.0),
            rate_dummy=self._parse_ds_float(self._le_rate_d.text(), 1.0),
            time_per_point=self._parse_ds_float(self._le_tpp.text(), 1.0),
            array_from=self._parse_ds_float(self._le_arr_from.text(), 0.0),
            array_to=self._parse_ds_float(self._le_arr_to.text(), 1.0),
            array_step=self._parse_ds_float(self._le_arr_step.text(), 0.1),
            selected_channel_idx=max(0, self._second_radio_group.checkedId()),
            retrace_to_zero=self._cb_retrace_to_zero.isChecked(),
            to_zero_at_last=self._cb_to_zero_at_last.isChecked(),
            second_sweep_rate=self._parse_ds_float(self._le_second_rate.text(), 1.0),
            second_use_safety=self._cb_second_safety.isChecked(),
            second_safety_steps=self._sb_second_steps.value(),
            second_safety_interval_ms=self._parse_ds_float(self._le_second_interval.text(), 0.0),
            second_feedback_read_cmd=self._le_fb_read_cmd.text().strip(),
            second_feedback_poll_interval=self._parse_ds_float(self._le_fb_poll.text(), 1.0),
            second_feedback_tolerance_pct=self._parse_ds_float(self._le_fb_tol.text(), 95.0),
            second_feedback_std_window=self._sb_fb_std_window.value(),
            second_feedback_noisefloor=self._parse_ds_float(self._le_fb_noisefloor.text(), 0.0),
            second_feedback_std_threshold=self._parse_ds_float(self._le_fb_std_thresh.text(), 0.01),
            second_wait_time=self._parse_ds_float(self._le_wait_time.text(), 1.0),
            cycle_enabled=bool(self._cb_cycle_enable.isChecked()),
            cycle_targets_str=self._le_cycle_targets.text().strip(),
            cycle_repeats=int(self._sb_cycle_repeats.value()),
            alarm=self._build_alarm_config(),
        )
        self._param_reg.save_double_sweep_config(cfg)

    def _current_cfg(self) -> DoubleSweepConfig:
        return DoubleSweepConfig(
            start_point=self._parse_ds_float(self._le_start.text(), 0.0),
            stop_point=self._parse_ds_float(self._le_stop.text(), 1.0),
            rate_trace=self._parse_ds_float(self._le_rate_t.text(), 1.0),
            rate_retrace=self._parse_ds_float(self._le_rate_r.text(), 1.0),
            rate_dummy=self._parse_ds_float(self._le_rate_d.text(), 1.0),
            time_per_point=self._parse_ds_float(self._le_tpp.text(), 1.0),
            array_from=self._parse_ds_float(self._le_arr_from.text(), 0.0),
            array_to=self._parse_ds_float(self._le_arr_to.text(), 1.0),
            array_step=self._parse_ds_float(self._le_arr_step.text(), 0.1),
            retrace_to_zero=self._cb_retrace_to_zero.isChecked(),
            to_zero_at_last=self._cb_to_zero_at_last.isChecked(),
            second_sweep_rate=self._parse_ds_float(self._le_second_rate.text(), 1.0),
            second_use_safety=self._cb_second_safety.isChecked(),
            second_safety_steps=self._sb_second_steps.value(),
            second_safety_interval_ms=self._parse_ds_float(self._le_second_interval.text(), 0.0),
            second_feedback_read_cmd=self._le_fb_read_cmd.text().strip(),
            second_feedback_poll_interval=self._parse_ds_float(self._le_fb_poll.text(), 1.0),
            second_feedback_tolerance_pct=self._parse_ds_float(self._le_fb_tol.text(), 95.0),
            second_feedback_std_window=self._sb_fb_std_window.value(),
            second_feedback_noisefloor=self._parse_ds_float(self._le_fb_noisefloor.text(), 0.0),
            second_feedback_std_threshold=self._parse_ds_float(self._le_fb_std_thresh.text(), 0.01),
            second_wait_time=self._parse_ds_float(self._le_wait_time.text(), 1.0),
        )

    @staticmethod
    def _parse_ds_float(text: str, default: float = 0.0) -> float:
        try:
            return float(text.strip())
        except (ValueError, AttributeError):
            return default

    def _get_first_channel_unit(self) -> str:
        try:
            profile = self._main_win._active_profile
            idx = profile.active_sweep_channel_idx
            if 0 <= idx < len(profile.main_ui.sweep_values):
                return profile.main_ui.sweep_values[idx].unit
        except Exception:
            pass
        return ""

    def _update_unit_labels(self):
        """Refresh unit labels based on current first/second channel selections."""
        u1 = self._get_first_channel_unit()
        self._lbl_start_unit.setText(u1)
        self._lbl_stop_unit.setText(u1)
        rate_u1 = f"{u1}/min" if u1 else "units/min"
        self._lbl_rate_t_unit.setText(rate_u1)
        self._lbl_rate_r_unit.setText(rate_u1)
        self._lbl_rate_d_unit.setText(rate_u1)

        u2 = self._second_channel.unit if self._second_channel else ""
        self._lbl_arr_from_unit.setText(u2)
        self._lbl_arr_to_unit.setText(u2)
        self._lbl_arr_step_unit.setText(u2)
        rate_u2 = f"{u2}/min" if u2 else "units/min"
        self._lbl_second_rate_unit.setText(rate_u2)

    def _update_n_points(self):
        cfg = self._current_cfg()
        arr = _generate_array(cfg)
        self._lbl_n_points.setText(f"→ {len(arr)} pts")

    def _update_est_time(self):
        cfg = self._current_cfg()
        arr = _generate_array(cfg)
        second_ch = None
        if self._second_channel is not None:
            second_ch = self._make_effective_second_channel()
        sec = _estimate_total_seconds(cfg, len(arr), second_ch)
        # Add cycle time per-array if cycle enabled
        try:
            if getattr(cfg, 'cycle_enabled', False):
                text = (getattr(cfg, 'cycle_targets_str', '') or '').strip()
                parts = [p.strip() for p in text.split(',') if p.strip()]
                targets = []
                valid = True
                for p in parts:
                    try:
                        v = float(p)
                    except Exception:
                        valid = False
                        break
                    if math.isnan(v) or math.isinf(v):
                        valid = False
                        break
                    targets.append(v)
                if valid and targets:
                    # Estimate single cycle time: initial -> first + chain
                    def _chain(start: float) -> float:
                        total = 0.0
                        prev = start
                        for t in targets:
                            total += abs(t - prev) * 60.0 / max(1e-12, cfg.rate_trace)
                            prev = t
                        return total
                    per_cycle = _chain(cfg.start_point) + (max(0, int(getattr(cfg, 'cycle_repeats', 1)) - 1) * _chain(targets[-1]))
                    sec += len(arr) * per_cycle
        except Exception:
            pass
        self._lbl_est_time.setText(_fmt_hms(sec))
        eta = _datetime.now() + _timedelta(seconds=sec)
        # 24h 이내면 HH:MM, 그 이상이면 날짜 포함
        if sec < 86400:
            self._lbl_eta.setText(eta.strftime("%H:%M"))
        else:
            self._lbl_eta.setText(eta.strftime("%m/%d %H:%M"))

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def lock_ui(self, locked: bool) -> None:
        """측정 중 설정 UI 잠금/해제 (Start/Stop 버튼 제외)."""
        self._ch_frame.setEnabled(not locked)
        self._sp_frame.setEnabled(not locked)
        self._arr_frame.setEnabled(not locked)
        self._sweep_ch_frame.setEnabled(not locked)
        self._feedback_frame.setEnabled(not locked)
        self._wait_frame.setEnabled(not locked)
        self._right_alarm_scroll.setEnabled(not locked)
        # Feature 1: 테이블 창의 '재생성'·'테이블 유지' 잠금(값 편집·행 추가/삭제는 유지).
        #            테이블 열기 버튼 자체는 측정 중에도 계속 눌러 편집할 수 있어야 하므로
        #            _arr_frame 밖에 두었고 여기서 비활성화하지 않는다.
        self._set_table_win_running(locked)

    def _set_table_win_running(self, running: bool) -> None:
        win = self._second_table_win
        if win is not None:
            try:
                win.set_running(running)
            except Exception:
                pass
        if hasattr(self, "_cb_ds_keep_table"):
            self._cb_ds_keep_table.setEnabled(not running)

    def _unlock_main_ui(self) -> None:
        """Double Sweep 종료 시 Main Window UI 복원."""
        self.lock_ui(False)
        mw = self._main_win
        mw._btn_start.setEnabled(True)
        mw._sweep_channel_panel.setEnabled(True)
        mw._meas_panel.setEnabled(True)
        mw._set_save_inputs_enabled(True)
        for _sfx in ("", "2", "3"):
            cb = getattr(mw, f"_cb_deriv{_sfx}_enable")
            cb.setEnabled(True)
            for _w in getattr(mw, f"_deriv{_sfx}_setting_widgets"):
                _w.setEnabled(cb.isChecked())
        if mw._meta_data_window is not None:
            mw._meta_data_window.lock_ui(False)

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

        # 저장 비활성 경고 — 장시간 측정이 저장 없이 진행되는 사고 방지
        if not self._main_win._cb_save_enable.isChecked():
            ans = QMessageBox.question(
                self, "Auto-save 비활성화",
                "Auto-save가 꺼져 있습니다. Double Sweep 데이터가 파일로 저장되지 않습니다.\n\n"
                "저장 없이 진행하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        # 연결 상태 확인 (second channel 포함) — 실패 시 측정 중단
        if not self._main_win._run_connection_test(include_second=True, show_success=False):
            return

        if not self._prepare_run():
            return

        self._main_win._log("Double Sweep started. Pre-positioning first channel → start_point.", color="#4ec9b0")
        self._start_pre_init()

    def _prepare_run(self) -> bool:
        """측정 실행 준비 — _on_start(신규)와 _resume_from_point(재개) 공통 셋업.

        array 생성에 실패하면 아무것도 잠그지 않고 False 를 돌려준다.
        """
        self._save_config()
        self._cfg = self._current_cfg()
        if not self._prepare_second_array():
            return False

        self._reset_run_state()
        self._prepare_derivative_channels()
        self._ctx = self._build_context()
        self._configure_metadata()
        # Parse cycle targets (if enabled)
        try:
            if getattr(self._cfg, 'cycle_enabled', False):
                text = (self._cfg.cycle_targets_str or "").strip()
                targets = []
                for part in text.split(','):
                    p = part.strip()
                    if not p:
                        continue
                    try:
                        v = float(p)
                    except Exception:
                        v = None
                    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                        targets = None
                        break
                    targets.append(v)
                if targets is None or not targets:
                    QMessageBox.warning(self, "Cycle Targets 오류",
                                        "Cycle Targets에 올바른 숫자값을 하나 이상 입력하세요.")
                    return False
                self._cycle_targets = targets
                self._cycle_repeats = int(getattr(self._cfg, 'cycle_repeats', 1))
        except Exception:
            pass

        self._lock_ui_for_run()
        self._sync_data_window_columns()
        self._prepare_graph_session()
        return True

    # ── _prepare_run 의 단계별 처리 ───────────────────────────────────────

    def _prepare_second_array(self) -> bool:
        """second 채널이 훑을 값 목록을 확정한다.

        테이블 모델이 source of truth 다 — '테이블 유지'가 켜져 있고 모델에 값이
        있으면 사용자가 직접 넣은 목록을 그대로 쓰고, 아니면 From/To/Step 으로
        새로 만든다.
        """
        keep_table = (self._cb_ds_keep_table.isChecked()
                      and self._second_table_model.count() > 0)
        if keep_table:
            self._second_table_model.rearm()   # 값은 두고 상태만 PENDING 으로
        else:
            self._second_table_model.reset_from(_generate_array(self._cfg))

        self._array = self._second_table_model.values()
        if not self._array:
            QMessageBox.warning(self, "Array 오류",
                                "Array 생성 실패: 포인트 수가 0입니다.")
            return False
        return True

    def _reset_run_state(self):
        """이전 측정의 잔여 상태를 지운다."""
        self._array_idx = 0
        self._last_write_value = None
        self._last_meas_values = {}
        self._trace_filepath = None
        self._auto_retry_used = False
        self._retry_timer.stop()
        self._btn_resume.setEnabled(False)
        self._lbl_last_alarm.setText("(없음)")
        self._lbl_last_alarm.setStyleSheet("color: #888888; font-size: 10px;")
        # Cycle phase state (per-array step)
        self._cycle_targets = []
        self._cycle_repeats = 1
        self._cycle_current_repeat = 0
        self._cycle_seg_idx = 0

    def _prepare_derivative_channels(self):
        """미분 채널을 현재 UI 설정으로 다시 만들고 버퍼를 비운다 (단일 sweep 과 동일)."""
        main_win = self._main_win
        for order, (channel, _key) in enumerate(main_win._deriv_channels(), start=1):
            channel.reconfigure(main_win._build_deriv_config(order))
            channel.reset()

    def _configure_metadata(self):
        """T/B 버퍼 설정 — double sweep 전체에서 공유한다."""
        labels = [label for label, _unit in self._ctx.meas_cols]
        self._main_win._meta_manager.configure(
            self._ctx.active_meas_indices,
            self._main_win._active_profile.measurements,
            labels,
        )

    def _lock_ui_for_run(self):
        """측정 중 구성 변경 차단 — 이 창과 메인 창 양쪽."""
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._glow_phase = 0.0
        self._glow_timer.start()
        self.sweep_started.emit()
        self.lock_ui(True)

        main_win = self._main_win
        main_win._btn_start.setEnabled(False)
        main_win._sweep_channel_panel.setEnabled(False)
        main_win._meas_panel.setEnabled(False)
        main_win._set_save_inputs_enabled(False)
        for suffix in ("", "2", "3"):
            getattr(main_win, f"_cb_deriv{suffix}_enable").setEnabled(False)
            for widget in getattr(main_win, f"_deriv{suffix}_setting_widgets"):
                widget.setEnabled(False)
        if main_win._meta_data_window is not None:
            main_win._meta_data_window.lock_ui(True)

    def _sync_data_window_columns(self):
        """double sweep 측정값을 메인 창의 DataWindow 에 표시하기 위한 열 구성."""
        ctx = self._ctx
        columns = [(ctx.sweep_col[0], ctx.sweep_col[1])]
        columns.extend(ctx.meas_cols)
        self._main_win._data_window.configure_columns(columns)
        self._main_win._data_window.clear_values()

    def _prepare_graph_session(self):
        """그래프 세션을 새로 연다. 창이 떠 있지 않아도 history/columns 는 갱신한다."""
        self._update_graph_map_base()
        main_win = self._main_win
        try:
            columns = [("__sweep__", self._ctx.sweep_col[0], self._ctx.sweep_col[1])]
            for (_row, _alias, desc, _cmd), (fig_axis, unit) in zip(
                    self._ctx.active_measurements, self._ctx.meas_cols):
                columns.append((desc, fig_axis, unit))
            for channel, _key in main_win._deriv_channels():
                if channel._cfg.enabled:
                    columns.append(channel.col_info())

            main_win._graph_history.clear()
            main_win._graph_columns = columns
            if main_win._graph_window is not None:
                main_win._graph_window.begin_session(columns)
        except Exception as exc:
            main_win._log(f"  [Graph] begin_session failed: {exc}", color="#f44747")

        # 새 sweep 이므로 미분 슬라이딩 윈도우를 비운다
        for channel, _key in main_win._deriv_channels():
            channel.reset()

    def _update_graph_map_base(self):
        """2D map 패널이 스캔할 최상위 폴더를 알려 준다."""
        main_win = self._main_win
        if main_win._graph_window is None:
            return
        base = main_win._le_main_folder.text().strip()
        if not base:
            return
        sub = main_win._le_custom_folder.text().strip()
        main_win._graph_window.update_map_base(f"{base}/{sub}" if sub else base)

    def _on_stop_clicked(self):
        """사용자가 Stop 버튼을 누른 경우 — 현재 array 지점을 resume 로그에 저장 후 중단."""
        if self._phase != DoubleSweepPhase.IDLE and self._array:
            self._save_resume_point("사용자 중단(Stop)")
        self._on_stop()

    def _on_stop(self):
        if self._phase == DoubleSweepPhase.IDLE:
            return
        self._sweep_timer.stop()
        self._retry_timer.stop()
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
        self._unlock_main_ui()
        # Feature 1: CURRENT로 멈춘 행을 PENDING으로 되돌리고 테이블 갱신
        self._second_table_model.clear_running()
        self._refresh_second_table_win()
        self._set_table_win_running(False)
        self._update_resume_btn_enabled()

    def _finish(self):
        # 모든 측정 완료 알람
        self._check_alarm(is_sweep_complete=True)
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
        self._unlock_main_ui()
        # Feature 1: 완료 — 테이블 실행 상태 해제·갱신
        self._second_table_model.clear_running()
        self._refresh_second_table_win()
        self._set_table_win_running(False)
        self._update_resume_btn_enabled()

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
            (mw._meas_label_for(row, m), m.unit)
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
        """Return second channel with UI-overridden params for SWEEP / FEEDBACK / WAIT_FOR_TIME."""
        ch = self._second_channel
        if ch is None:
            # 실행 중 second 채널이 사라진 경우(파라미터 매니저에서 비움 등) — None을
            # 역참조해 크래시하지 말고 명확한 오류로 끊는다.
            raise RuntimeError("Second sweep channel이 없습니다 (구성이 비었거나 변경됨).")
        if ch.advance_type == SecondSweepAdvanceType.FEEDBACK:
            read_cmd = self._le_fb_read_cmd.text().strip()
            return ch.model_copy(update={
                "feedback_read_cmd":       read_cmd if read_cmd else ch.feedback_read_cmd,
                "feedback_poll_interval":  self._parse_ds_float(self._le_fb_poll.text(), ch.feedback_poll_interval),
                "feedback_tolerance_pct":  self._parse_ds_float(self._le_fb_tol.text(), ch.feedback_tolerance_pct),
                "feedback_std_window":     self._sb_fb_std_window.value(),
                "feedback_noisefloor":     self._parse_ds_float(self._le_fb_noisefloor.text(), ch.feedback_noisefloor),
                "feedback_std_threshold":  self._parse_ds_float(self._le_fb_std_thresh.text(), ch.feedback_std_threshold),
            })
        if ch.advance_type == SecondSweepAdvanceType.WAIT_FOR_TIME:
            return ch.model_copy(update={
                "wait_time": self._parse_ds_float(self._le_wait_time.text(), ch.wait_time),
            })
        if ch.advance_type == SecondSweepAdvanceType.THRESHOLD_TIME:
            # 도달 판정(feedback 필드) + 고정 시간 대기(wait_time) 둘 다 UI에서 반영.
            read_cmd = self._le_fb_read_cmd.text().strip()
            return ch.model_copy(update={
                "feedback_read_cmd":      read_cmd if read_cmd else ch.feedback_read_cmd,
                "feedback_poll_interval": self._parse_ds_float(self._le_fb_poll.text(), ch.feedback_poll_interval),
                "feedback_tolerance_pct": self._parse_ds_float(self._le_fb_tol.text(), ch.feedback_tolerance_pct),
                "feedback_noisefloor":    self._parse_ds_float(self._le_fb_noisefloor.text(), ch.feedback_noisefloor),
                "wait_time":              self._parse_ds_float(self._le_wait_time.text(), ch.wait_time),
            })
        if ch.advance_type != SecondSweepAdvanceType.SWEEP:
            return ch
        use_safety = self._cb_second_safety.isChecked()
        return ch.model_copy(update={
            "sweep_rate": self._parse_ds_float(self._le_second_rate.text(), 1.0),
            "safety_steps": self._sb_second_steps.value() if use_safety else 0,
            "safety_interval_ms": self._parse_ds_float(self._le_second_interval.text(), 0.0) if use_safety else 0.0,
        })

    def _build_meta_extra(self) -> dict:
        """메타 데이터 JSON에 기록할 second channel advance 설정을 구성한다.

        측정에 실제로 사용된(UI override 반영된) 파라미터를 advance type별로 저장.
        """
        ch = self._make_effective_second_channel()
        second_value = (
            self._array[self._array_idx]
            if self._array and self._array_idx < len(self._array) else None
        )
        info: dict = {
            "alias": ch.alias,
            "description": ch.description,
            "advance_type": ch.advance_type.value,
            "unit": ch.unit,
            "value": second_value,
        }
        at = ch.advance_type
        if at == SecondSweepAdvanceType.SWEEP:
            info["sweep_rate"] = ch.sweep_rate
            info["safety_steps"] = ch.safety_steps
            info["safety_interval_ms"] = ch.safety_interval_ms
        elif at == SecondSweepAdvanceType.FEEDBACK:
            info["feedback_read_cmd"] = ch.feedback_read_cmd
            info["feedback_poll_interval"] = ch.feedback_poll_interval
            info["feedback_tolerance_pct"] = ch.feedback_tolerance_pct
            info["feedback_std_window"] = ch.feedback_std_window
            info["feedback_noisefloor"] = ch.feedback_noisefloor
            info["feedback_std_threshold"] = ch.feedback_std_threshold
        elif at == SecondSweepAdvanceType.WAIT_FOR_TIME:
            info["wait_time"] = ch.wait_time
        elif at == SecondSweepAdvanceType.THRESHOLD_TIME:
            info["feedback_read_cmd"] = ch.feedback_read_cmd
            info["feedback_poll_interval"] = ch.feedback_poll_interval
            info["feedback_tolerance_pct"] = ch.feedback_tolerance_pct
            info["feedback_noisefloor"] = ch.feedback_noisefloor
            info["wait_time"] = ch.wait_time
        return {"second_channel": info}

    # ------------------------------------------------------------------
    # Second 값 테이블 (Feature 1)
    # ------------------------------------------------------------------
    def _begin_array_index(self, idx: int) -> bool:
        """모델에서 최신 array를 다시 읽어 idx 행을 CURRENT로 표시하고 second advance 시작.

        측정 중 편집/추가된 미래 행을 반영한다(완료 행은 앞쪽 prefix로 고정이라 안전).
        범위를 벗어나면(모든 값 완료) False."""
        self._array = self._second_table_model.values()
        if idx < 0 or idx >= len(self._array):
            return False
        self._array_idx = idx
        self._second_table_model.mark_current(idx)
        self._refresh_second_table_win()
        prev = self._array[idx - 1] if idx > 0 else None
        self._advance_second(self._array[idx], prev=prev)
        return True

    def _refresh_second_table_win(self):
        win = self._second_table_win
        if win is not None:
            try:
                win.refresh()
            except Exception:
                pass

    def _open_second_table(self):
        """Array 값 테이블 편집 창을 연다(없으면 생성)."""
        win = self._second_table_win
        if win is None:
            from pythonization.ui.panels.second_channel_table_window import SecondChannelTableWindow
            win = SecondChannelTableWindow(self._second_table_model, self, parent=self)
            self._second_table_win = win
        busy = self._phase != DoubleSweepPhase.IDLE
        if not busy and self._second_table_model.count() == 0:
            self._reset_second_table_from_controls()
        win.set_running(busy)
        win.refresh()
        win.show()
        win.raise_()
        win.activateWindow()

    def _reset_second_table_from_controls(self):
        """From/To/Step 입력값으로 array 테이블을 재생성한다(측정 중이 아닐 때만 의미)."""
        arr = _generate_array(self._current_cfg())
        if not arr:
            QMessageBox.warning(self, "Array 오류", "From/To/Step으로 값을 만들 수 없습니다.")
            return
        self._second_table_model.reset_from(arr)
        self._refresh_second_table_win()

    def _advance_second(self, next_val: float, prev: Optional[float]):
        ch = self._make_effective_second_channel()
        self._lbl_second_val.setText(f"{next_val:.4g} {ch.unit}")
        self._lbl_array_progress.setText(f"{self._array_idx + 1}/{len(self._array)}")
        self._lbl_fb_metric.setText("—")
        self._lbl_fb_metric.setStyleSheet("color: #888888;")
        self._set_phase(DoubleSweepPhase.ADVANCING_SECOND)

        # 새 step 시작 — T/B 버퍼 초기화
        self._trace_filepath = None
        self._main_win._meta_manager.clear()

        # Clear graph and reset derivative buffers when advancing to next array step
        if prev is not None:
            for _dc in (self._main_win._deriv_channel, self._main_win._deriv_channel2, self._main_win._deriv_channel3):
                _dc.reset()
            try:
                cols = [("__sweep__", self._ctx.sweep_col[0], self._ctx.sweep_col[1])]
                for (row, alias, desc, cmd), (fig_ax, unit) in zip(
                    self._ctx.active_measurements, self._ctx.meas_cols
                ):
                    cols.append((desc, fig_ax, unit))
                for deriv_ch in (self._main_win._deriv_channel, self._main_win._deriv_channel2, self._main_win._deriv_channel3):
                    if deriv_ch._cfg.enabled:
                        cols.append(deriv_ch.col_info())
                mw = self._main_win
                mw._graph_history.clear()
                mw._graph_columns = cols
                if mw._graph_window is not None:
                    mw._graph_window.begin_session(cols)
            except Exception as _e:
                self._main_win._log(f"  [Graph] clear failed: {_e}", color="#f44747")

        self._emit_advance(SecondChannelRequest(
            channel=ch,
            next_value=next_val,
            prev_value=prev,
            time_per_point=self._cfg.time_per_point,
        ))

    def _return_second_to_zero(self):
        """모든 array step 완료 후 second channel을 0으로 전송."""
        ch = self._make_effective_second_channel()
        last_val = self._array[-1] if self._array else None
        self._set_phase(DoubleSweepPhase.RETURNING_ZERO)
        self._lbl_phase.setText("RETURNING 0")

        if ch.advance_type == SecondSweepAdvanceType.SWEEP:
            # SWEEP type: use full advance worker so safety ramp / rate are respected
            self._emit_advance(SecondChannelRequest(
                channel=ch,
                next_value=0.0,
                prev_value=last_val,
                time_per_point=self._cfg.time_per_point,
            ))
        else:
            # Other types: single VISA write to 0, then finish immediately
            ch_simple = ch.model_copy(update={"advance_type": SecondSweepAdvanceType.SIMPLE_HOP})
            self._emit_advance(SecondChannelRequest(
                channel=ch_simple,
                next_value=0.0,
                prev_value=last_val,
                time_per_point=self._cfg.time_per_point,
            ))

    @Slot(float)
    @Slot(str)
    def _on_second_status(self, m: str) -> None:
        """second 워커 진행 메시지를 콘솔에 기록 (메인 스레드 보장용 bound 슬롯)."""
        self._main_win._log(f"  [DoubleSweep] {m}", color="#d7ba7d")

    def _on_feedback_metric(self, metric: float) -> None:
        """Phase 2 metric 값을 실시간으로 Std Threshold 옆 레이블에 표시."""
        threshold = self._parse_ds_float(self._le_fb_std_thresh.text(), 0.01)
        if metric >= 1e30:
            self._lbl_fb_metric.setText("∞")
            self._lbl_fb_metric.setStyleSheet("color: #f44747;")
        else:
            color = "#4ec9b0" if metric < threshold else "#f78166"
            self._lbl_fb_metric.setText(f"{metric:.4g}")
            self._lbl_fb_metric.setStyleSheet(f"color: {color};")

    def _update_feedback_read_cmd_state(self) -> None:
        """profile에 Read Cmd가 이미 설정된 경우 입력칸 비활성화."""
        if self._second_channel is None:
            return
        if self._second_channel.advance_type != SecondSweepAdvanceType.FEEDBACK:
            return
        profile_cmd = self._second_channel.feedback_read_cmd.strip()
        if profile_cmd:
            self._le_fb_read_cmd.setEnabled(False)
            self._le_fb_read_cmd.setText(profile_cmd)
            self._lbl_fb_read_hint.setText("(profile 설정)")
        else:
            self._le_fb_read_cmd.setEnabled(True)
            self._lbl_fb_read_hint.setText("")

    @Slot()
    def _on_advance_done(self):
        self._lbl_fb_metric.setText("—")
        self._lbl_fb_metric.setStyleSheet("color: #888888;")
        if self._phase == DoubleSweepPhase.RETURNING_ZERO:
            self._finish()
            return
        if self._phase != DoubleSweepPhase.ADVANCING_SECOND:
            return
        # If cycle sweep is enabled, start CYCLE phase instead of DUMMY/TRACE/RETRACE
        if getattr(self._cfg, 'cycle_enabled', False):
            self._cycle_current_repeat = 0
            self._cycle_seg_idx = 0
            self._start_sweep_phase(DoubleSweepPhase.CYCLE)
        else:
            self._start_sweep_phase(DoubleSweepPhase.DUMMY)

    @Slot(str)
    def _on_advance_error(self, msg: str):
        self._main_win._log(
            f"  [DoubleSweep] Second channel error — {humanize_error(msg)}  [상세] {msg}",
            color="#f44747")
        if is_comm_error(msg):
            self._handle_comm_error(f"Second channel error: {msg}")
        else:
            self._check_alarm(is_comm_error=True, extra_reason=f"Second channel error: {msg}")
            self._on_stop()

    @Slot(str)
    def _on_advance_timeout(self, msg: str):
        """second advance 워치독 타임아웃 — 자동재개 없이 즉시 측정 중지 + 알람.

        threshold 미도달(20분, 재전송 1회 후)·feedback 안정화 지연(5분) 모두 여기로 온다.
        comm 오류와 달리 재시도하지 않는다(이미 worker가 재전송을 시도함).
        """
        self._main_win._log(
            f"  [DoubleSweep] Second channel 타임아웃 — 측정 중지: {msg}",
            color="#f44747")
        self._check_alarm(is_comm_error=True, extra_reason=f"Second channel timeout: {msg}")
        self._on_stop()

    # ------------------------------------------------------------------
    # 통신 오류 자동 재개 / 수동 재개
    # ------------------------------------------------------------------

    _AUTO_RESUME_DELAY_MS = 10_000   # 통신 오류 후 자동 재개 대기 (10초)

    def _handle_comm_error(self, reason: str):
        """통신 오류: 1회는 10초 후 자동 재개, 재차 발생 시 중단 + 재개 지점 저장."""
        if self._phase == DoubleSweepPhase.IDLE:
            return
        if not self._auto_retry_used:
            self._auto_retry_used = True
            self._sweep_timer.stop()
            self._main_win._log(
                f"  ⏳ [DoubleSweep] 통신 오류 — {self._AUTO_RESUME_DELAY_MS // 1000}초 후 자동 재개합니다.  ({reason})",
                color="#d7ba7d",
            )
            self._retry_timer.start(self._AUTO_RESUME_DELAY_MS)
        else:
            self._save_resume_point(reason)
            self._check_alarm(is_comm_error=True, extra_reason=reason)
            cause = humanize_error(reason)
            self._main_win._log(
                "  ✗ [DoubleSweep] 자동 재개 후 재차 통신 오류 — 측정 중단. "
                "[Resume] 버튼으로 저장된 지점부터 재개하세요.",
                color="#f44747",
            )
            self._main_win._log(f"     원인: {cause}", color="#f44747")
            self._on_stop()
            QMessageBox.warning(
                self, "통신 오류 — Double Sweep 중단",
                "자동 재개 후에도 통신 오류가 반복되어 측정을 중단했습니다.\n\n"
                f"원인: {cause}\n\n"
                "현재 array 지점이 재개 로그에 저장되었습니다.\n"
                "[Resume] 버튼으로 해당 지점부터 다시 시작할 수 있습니다.",
            )

    def _auto_resume(self):
        """10초 경과 후 마지막 요청을 재전송하여 측정을 이어간다."""
        if self._phase == DoubleSweepPhase.IDLE or self._last_emit is None:
            return
        kind, req = self._last_emit
        self._main_win._log("  ▶ [DoubleSweep] 자동 재개 — 측정을 재시작합니다.", color="#4ec9b0")
        if kind == "step":
            self.request_step.emit(req)
        else:
            self.request_advance.emit(req)

    def _save_resume_point(self, reason: str):
        """현재 array index 위치를 재개 로그에 저장 (array index 단위 재개)."""
        ch = self._second_channel
        second_val = (
            self._array[self._array_idx]
            if self._array and self._array_idx < len(self._array) else None
        )
        unit = ch.unit if ch else ""
        label = (
            f"array {self._array_idx + 1}/{len(self._array)}  "
            f"2nd={second_val:.6g}{unit}  phase={self._phase.name}  ({reason[:40]})"
            if second_val is not None else
            f"array {self._array_idx + 1}/{len(self._array)}  phase={self._phase.name}"
        )
        point = ResumePoint(
            sweep_type="double",
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            label=label,
            data_filepath="",   # double sweep은 phase별 파일을 재생성하므로 미사용
            payload={
                "array_idx": self._array_idx,
                "phase": self._phase.name,
                "second_value": second_val,
            },
        )
        self._resume_log.add(point)
        self._update_resume_btn_enabled()

    def _update_resume_btn_enabled(self):
        """재개 로그에 double 지점이 있고 IDLE 상태이면 Resume 활성화."""
        if not hasattr(self, "_btn_resume"):
            return
        has_point = self._resume_log.latest("double") is not None
        self._btn_resume.setEnabled(has_point and self._phase == DoubleSweepPhase.IDLE)

    def _on_resume_clicked(self):
        """[Resume] 버튼: 저장된 지점 목록에서 선택 후 해당 array index부터 재개."""
        if self._phase != DoubleSweepPhase.IDLE:
            return
        dlg = ResumePickerDialog(self._resume_log.all(), parent=self, sweep_type="double")
        if dlg.exec() and dlg.selected_point is not None:
            self._resume_from_point(dlg.selected_point)

    def _resume_from_point(self, point):
        """선택된 array index부터 Double Sweep을 재개한다.

        해당 index의 DUMMY→TRACE→RETRACE를 처음부터 다시 수행 (phase별 파일 재생성).
        """
        if self._main_win._running:
            QMessageBox.warning(self, "Sweep 실행 중", "Main sweep이 실행 중입니다. 먼저 중단하세요.")
            return
        if self._main_win._sweep_channel is None or self._second_channel is None:
            QMessageBox.warning(self, "재개 불가",
                "Sweep Channel / Second Channel이 선택되어 있어야 합니다.")
            return
        if not self._main_win._run_connection_test(include_second=True, show_success=False):
            return

        if not self._prepare_run():
            return

        idx = int(point.payload.get("array_idx", 0))
        idx = max(0, min(idx, len(self._array) - 1))
        # Feature 1: 재개 지점 이전 행을 DONE으로 표시(테이블 색상·잠금 일관성).
        self._second_table_model.reset_from(self._array, done_prefix=idx)
        self._main_win._log(
            f"  ▶ [DoubleSweep] 수동 재개 — array {idx + 1}/{len(self._array)} 부터 재시작.",
            color="#4ec9b0",
        )
        self._begin_array_index(idx)

    def _start_sweep_phase(self, phase: DoubleSweepPhase):
        self._set_phase(phase)
        if not self._setup_datasaver_for_phase(phase.name.lower()):
            return   # 저장 실패 → 측정 중단 (데이터 유실 방지). _on_stop은 setup에서 호출됨.
        # 각 페이즈 시작 시 초기 상태 측정 (이동 없이 현재 위치에서 measurement만)
        ctx = self._ctx
        cfg = self._cfg
        self._emit_step(StepRequest(
            sweep_channel=ctx.sweep_channel,
            sweep_to=0.0,
            sweep_rate=1.0,
            time_per_point=cfg.time_per_point,
            t_emit=time.perf_counter(),
            last_write_value=self._last_write_value,
            active_measurements=ctx.active_measurements,
            measure_only=True,
        ))

    def _advance_phase(self):
        if self._phase == DoubleSweepPhase.DUMMY:
            self._start_sweep_phase(DoubleSweepPhase.TRACE)
        elif self._phase == DoubleSweepPhase.TRACE:
            self._start_sweep_phase(DoubleSweepPhase.RETRACE)
        elif self._phase == DoubleSweepPhase.RETRACE:
            # ── RETRACE 완료: 알람 체크 → 메타 데이터 저장 → 다음 step ──
            self._check_alarm(is_comm_error=False)
            # 메타 데이터 JSON 저장 (TRACE 파일과 같은 이름, 확장자 .json)
            # second channel advance 설정(feedback/wait/sweep 파라미터)도 함께 기록
            self._main_win._meta_manager.save(
                self._main_win._param_manager_reg.meta_data_config,
                self._trace_filepath,
                extra=self._build_meta_extra(),
            )
            # Feature 1: 현재 array 값 완료 표시 → 모델에서 다음 (편집/추가 반영) 행으로.
            self._second_table_model.mark_done(self._array_idx)
            self._refresh_second_table_win()
            if not self._begin_array_index(self._array_idx + 1):
                if self._cfg.to_zero_at_last and self._second_channel is not None:
                    self._return_second_to_zero()
                else:
                    self._finish()

    # ------------------------------------------------------------------
    # DataSaver
    # ------------------------------------------------------------------

    def _setup_datasaver_for_phase(self, phase_name: str) -> bool:
        """phase별 .dat 세션을 시작한다.

        반환: 측정을 계속해도 되는지 여부.
          - 저장이 의도적으로 비활성: True (저장 없이 진행)
          - 저장 활성 + 성공: True
          - 저장 활성 + 실패: False (경고 후 측정 중단 — 데이터 유실 방지)
        """
        ctx = self._ctx
        base = ctx.custom_folder
        phase_subfolder = f"{base}/{phase_name}" if base else phase_name

        # Double sweep 파일명: X{N:03d} 대신 _{fig_axis}_{step_value} 사용
        ch = self._second_channel
        step_val = self._array[self._array_idx] if self._array_idx < len(self._array) else 0.0
        fig_ax = (ch.figure_axis or "").strip()
        val_str = f"{step_val:.6g}"
        id_str = f"_{fig_ax}_{val_str}" if fig_ax else f"_{val_str}"
        # stem = custom_word + date (동일한 DataSaver 규칙 유지)
        stem_parts = []
        if ctx.custom_word.strip():
            stem_parts.append(ctx.custom_word.strip())
        if ctx.include_date:
            stem_parts.append(_date.today().strftime("%Y%m%d"))
        fixed_name = "".join(stem_parts) + id_str + ".dat"

        self._data_saver.set_main_folder(ctx.main_folder)
        self._data_saver.set_custom_folder(phase_subfolder)
        self._data_saver.set_custom_word(ctx.custom_word)
        self._data_saver.set_include_date(ctx.include_date)
        self._data_saver.set_enabled(ctx.save_enabled)
        self._data_saver.set_fixed_name(fixed_name)
        deriv_cols = [
            (ch.col_info()[1], ch.col_info()[2])
            for ch in (self._main_win._deriv_channel, self._main_win._deriv_channel2, self._main_win._deriv_channel3)
            if ch._cfg.enabled
        ]
        self._data_saver.set_columns([ctx.sweep_col] + ctx.meas_cols + deriv_cols)
        filepath = self._data_saver.start_session()
        if filepath:
            self._main_win._log(f"  [{phase_name}] Data → {filepath}", color="#888888")
        elif self._data_saver.start_error() is not None:
            # 저장이 활성인데 실패 → 데이터 유실 위험. 즉시 경고 + 측정 중단.
            err = self._data_saver.start_error()
            self._main_win._log(
                f"  ✗ [{phase_name}] 데이터 저장 시작 실패 — 측정 중단: {err}",
                color="#f44747",
            )
            self._on_stop()
            QMessageBox.critical(
                self, "데이터 저장 실패 — 측정 중단",
                f"[{phase_name}] 단계에서 데이터 파일을 시작할 수 없어 측정을 중단했습니다.\n\n"
                f"사유: {err}\n\n"
                "데이터 유실을 막기 위해 저장이 정상화될 때까지 측정하지 않습니다.\n"
                "Main Folder 경로·권한·디스크 공간을 확인하세요.",
            )
            return False
        # TRACE 파일 경로를 메타 데이터 JSON 저장에 사용
        if phase_name == "trace":
            self._trace_filepath = self._data_saver.get_filepath()
        self._update_ds_save_path()
        return True

    # ------------------------------------------------------------------
    # Sweep tick (same pattern as main_window)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Request emit helpers (자동 재개 시 재전송 위해 마지막 요청 기록)
    # ------------------------------------------------------------------

    def _emit_step(self, req: StepRequest):
        self._last_emit = ("step", req)
        self.request_step.emit(req)

    def _emit_advance(self, req: SecondChannelRequest):
        self._last_emit = ("advance", req)
        self.request_advance.emit(req)

    def _sweep_tick(self):
        if self._phase not in (DoubleSweepPhase.PRE_INIT,
                               DoubleSweepPhase.DUMMY,
                               DoubleSweepPhase.TRACE,
                               DoubleSweepPhase.RETRACE,
                               DoubleSweepPhase.CYCLE):
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
        elif self._phase == DoubleSweepPhase.RETRACE:
            sweep_to = 0.0 if cfg.retrace_to_zero else cfg.start_point
            sweep_rate = cfg.rate_retrace
            active_meas = ctx.active_measurements
        else:  # CYCLE
            # perform one segment of cycle targets per tick
            active_meas = ctx.active_measurements
            # fallback: use start_point if no targets parsed
            targets = getattr(self, '_cycle_targets', []) or []
            if not targets:
                sweep_to = cfg.start_point
            else:
                # current segment target
                idx = max(0, min(self._cycle_seg_idx, len(targets) - 1))
                sweep_to = targets[idx]
            sweep_rate = cfg.rate_trace

        t_emit = time.perf_counter()
        self._emit_step(StepRequest(
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
        """워커 스텝 완료 → 기록·표시 후 다음 스텝 또는 다음 phase 로 넘어간다.

        MainWindow._on_step_done 과 같은 순서를 따른다: 파일 기록 → 메타데이터 →
        그래프. 기록에 실패하면 뒤 단계로 가지 않고 측정을 멈춘다.
        """
        if self._phase == DoubleSweepPhase.PRE_INIT:
            self._handle_pre_init_step(result)
            return
        if self._phase not in (DoubleSweepPhase.DUMMY,
                       DoubleSweepPhase.TRACE,
                       DoubleSweepPhase.RETRACE,
                       DoubleSweepPhase.CYCLE):
            return
        # is_done 인데 측정값이 없다 = 이미 목표에 있었다 → 기록 없이 다음 phase
        if result.is_done and not result.meas_results:
            self._advance_phase()
            return

        t_recv = time.perf_counter()
        meas_map = {row: val for row, val in result.meas_results}
        row_vals, failed = self._format_measurement_row(result, meas_map)
        if failed:
            self._handle_measurement_failure(result, failed)
            return

        self._auto_retry_used = False      # 정상 스텝 — 자동 재개 예산 리셋

        # 미분은 한 번만 계산한다. 채널에 값을 push 하므로 두 번 부르면 같은 점이
        # 슬라이딩 윈도우에 두 번 들어가 미분값이 틀어진다.
        deriv_vals = self._push_derivatives(result, meas_map)
        row_vals += self._derivative_row_cells(deriv_vals)

        if not self._record_row(row_vals):
            return

        main_win = self._main_win
        main_win._meta_manager.record_step(result.meas_results)
        main_win._data_window.update_values(row_vals)
        self._push_graph_point(result, meas_map, deriv_vals)

        self._last_write_value = result.next_v
        self._cache_measured_values(meas_map)

        main_win._timing_window.update_timing(result.timing, t_recv,
                                              time.perf_counter())

        if self._phase == DoubleSweepPhase.CYCLE:
            # record row as usual
            # schedule next segment or finish cycle
            if result.is_done:
                # advance to next segment
                self._cycle_seg_idx += 1
                if self._cycle_seg_idx >= len(getattr(self, '_cycle_targets', []) or []):
                    # one cycle completed
                    self._cycle_current_repeat += 1
                    if self._cycle_current_repeat < max(1, int(getattr(self, '_cycle_repeats', 1))):
                        # start next repeat from first target
                        self._cycle_seg_idx = 0
                        self._sweep_timer.start(0)
                    else:
                        # all repeats done for this array point
                        # save metadata and mark done, then advance array
                        self._check_alarm(is_comm_error=False)
                        self._main_win._meta_manager.save(
                            self._main_win._param_manager_reg.meta_data_config,
                            self._data_saver.get_filepath(),
                            extra=self._build_meta_extra(),
                        )
                        self._second_table_model.mark_done(self._array_idx)
                        self._refresh_second_table_win()
                        if not self._begin_array_index(self._array_idx + 1):
                            if self._cfg.to_zero_at_last and self._second_channel is not None:
                                self._return_second_to_zero()
                            else:
                                self._finish()
                else:
                    # next segment within same cycle
                    self._sweep_timer.start(0)
            else:
                # continue current segment with regular interval
                self._sweep_timer.start(self._next_interval_ms(result))
        else:
            self._schedule_next_step(result)

    # ── _on_step_done 의 단계별 처리 ───────────────────────────────────────

    def _handle_pre_init_step(self, result: StepResult):
        """First 채널을 시작점으로 옮기는 구간 — 데이터는 기록하지 않는다."""
        self._last_write_value = result.next_v
        if result.is_done:
            self._main_win._log(
                "  First channel at start_point. Advancing second channel.",
                color="#4ec9b0")
            self._begin_array_index(0)
        else:
            # dummy phase 와 같은 time_per_point 간격으로 진행
            self._sweep_timer.start(self._next_interval_ms(result))

    def _next_interval_ms(self, result: StepResult) -> int:
        """이번 스텝 처리에 쓴 시간을 빼서 time_per_point 주기를 맞춘다."""
        elapsed_ms = int((time.perf_counter() - result.timing.t_emit) * 1000)
        return max(0, int(self._cfg.time_per_point * 1000) - elapsed_ms)

    def _measurement_description(self, row: int) -> str:
        """측정 row 인덱스 → 사람이 읽을 이름."""
        for _row, _alias, desc, _cmd in self._ctx.active_measurements:
            if _row == row:
                return desc
        return ""

    def _format_measurement_row(self, result: StepResult, meas_map: dict) -> tuple:
        """저장용 한 행을 만든다. 반환: (셀 목록, 실패한 measurement 인덱스 목록)"""
        row_vals = [f"{result.next_v:.6g}"]
        failed = []
        for idx in self._ctx.active_meas_indices:
            val = meas_map.get(idx)
            if val is None:
                failed.append(idx)
            row_vals.append(f"{val:.6g}" if val is not None else "ERR")
        return row_vals, failed

    def _handle_measurement_failure(self, result: StepResult, failed: list):
        """통신 오류면 자동 재개 경로로, 그 외(파싱 등)는 알람 후 즉시 중단."""
        descs = []
        for idx in failed:
            desc = self._measurement_description(idx)
            detail = result.meas_errors.get(idx, "")
            descs.append(f"{desc}: {detail}" if detail else desc)

        err_msg = (f"✗ ERR @ phase={self._phase.name}\n"
                   f"실패 채널: {', '.join(descs)}")
        self._main_win._log(f"  [DoubleSweep] {err_msg}", color="#f44747")

        if any(is_comm_error(result.meas_errors.get(idx, "")) for idx in failed):
            self._handle_comm_error("; ".join(descs))
            return

        self._check_alarm(is_meas_error=True,
                          extra_reason=f"측정값 ERR — {', '.join(descs)}")
        self._on_stop()
        QMessageBox.critical(self, "Measurement Error", err_msg)

    def _push_derivatives(self, result: StepResult, meas_map: dict) -> list:
        """1·2·3차 미분값 계산. 미분 채널은 MainWindow 가 들고 있다."""
        main_win = self._main_win
        return [main_win._deriv_val_for_order(result, meas_map, ch)
                for ch, _key in main_win._deriv_channels()]

    def _derivative_row_cells(self, deriv_vals: list) -> list:
        """활성화된 미분 채널만 저장 행에 덧붙인다."""
        return [f"{val:.6g}" if val is not None else "—"
                for (ch, _key), val in zip(self._main_win._deriv_channels(), deriv_vals)
                if ch._cfg.enabled]

    def _record_row(self, row_vals: list) -> bool:
        """.dat 에 한 줄 기록. 저장이 켜져 있는데 실패하면 측정을 멈추고 False."""
        if self._data_saver.append_row(row_vals) or not self._data_saver.is_enabled():
            return True
        self._main_win._log(
            "  ✗ [DoubleSweep] 데이터 기록 실패 — 측정 중단 (디스크/권한 확인).",
            color="#f44747")
        self._on_stop()
        QMessageBox.critical(
            self, "데이터 기록 실패 — 측정 중단",
            "측정값을 파일에 기록하지 못해 측정을 중단했습니다.\n"
            "디스크 공간·파일 권한을 확인한 뒤 다시 시작하세요.",
        )
        return False

    def _push_graph_point(self, result: StepResult, meas_map: dict, deriv_vals: list):
        """그래프 히스토리에 한 점 추가. 창이 떠 있지 않아도 계속 쌓아 둔다.

        그래프 갱신 실패가 측정을 멈추면 안 되므로 통째로 감싼다.
        """
        try:
            main_win = self._main_win
            values = {"__sweep__": result.next_v}
            for row, _alias, desc, _cmd in self._ctx.active_measurements:
                val = meas_map.get(row)
                if val is not None:
                    values[desc] = val
            for (ch, key), val in zip(main_win._deriv_channels(), deriv_vals):
                if ch._cfg.enabled:
                    values[key] = val if val is not None else float("nan")

            point = GraphDataPoint(values=values, phase=_GRAPH_PHASE.get(self._phase, ""))
            main_win._graph_history.append(point)
            if main_win._graph_window is not None:
                main_win._graph_window.append_point(point)
        except Exception as exc:
            self._main_win._log(f"  [Graph] append_point failed: {exc}", color="#f44747")

    def _cache_measured_values(self, meas_map: dict):
        """알람 트리거 평가에 쓰는 최신 측정값 캐시."""
        for row, _alias, desc, _cmd in self._ctx.active_measurements:
            val = meas_map.get(row)
            if val is not None:
                self._last_meas_values[desc] = val

    def _schedule_next_step(self, result: StepResult):
        if result.is_done:
            self._advance_phase()
        elif result.measure_only:
            self._sweep_timer.start(0)   # 초기 상태 측정 완료 → 바로 sweep 시작
        else:
            self._sweep_timer.start(self._next_interval_ms(result))

    @Slot(str)
    def _on_step_error(self, msg: str):
        self._main_win._log(
            f"  [DoubleSweep] Step error — {humanize_error(msg)}  [상세] {msg}",
            color="#f44747")
        if is_comm_error(msg):
            self._handle_comm_error(f"Step error: {msg}")
        else:
            self._check_alarm(is_comm_error=True, extra_reason=f"Step error: {msg}")
            self._on_stop()

    # ------------------------------------------------------------------
    # Alarm
    # ------------------------------------------------------------------

    def _check_alarm(self, *, is_comm_error: bool = False, is_meas_error: bool = False,
                     is_sweep_complete: bool = False, extra_reason: str = ""):
        """
        알람 조건 평가 및 발동.

        is_comm_error=True    : 통신 오류/타임아웃 발생 시
        is_meas_error=True    : 측정 채널 ERR (None 반환) 발생 시
        is_sweep_complete=True: 모든 array 포인트 완료 시
        그 외 (기본)           : RETRACE 완료 후 measurement 트리거 체크
        """
        alarm_cfg = self._param_reg.double_sweep_config.alarm
        if not alarm_cfg.enabled:
            return

        reasons = []

        if is_sweep_complete:
            if alarm_cfg.fire_on_complete:
                reasons.append("Double Sweep 완료")
        elif is_comm_error:
            if self._alarm_manager.has_comm_error_trigger(alarm_cfg.triggers):
                reasons.append(extra_reason or "통신 오류 / Timeout")
        elif is_meas_error:
            if self._alarm_manager.has_meas_error_trigger(alarm_cfg.triggers):
                reasons.append(extra_reason or "측정값 ERR — 채널 실패")
        else:
            fired = self._alarm_manager.check_measurement_triggers(
                alarm_cfg.triggers, self._last_meas_values
            )
            reasons.extend(fired)

        if not reasons:
            return

        reason_str = " | ".join(reasons)
        self._alarm_manager.fire(alarm_cfg, reason_str)
        self._lbl_last_alarm.setText(reason_str)
        self._lbl_last_alarm.setStyleSheet("color: #ffa657; font-size: 10px; font-weight: bold;")
        self._main_win._log(
            f"  [Alarm] 트리거 발동: {reason_str}", color="#ffa657"
        )

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

    def _update_ds_save_path(self):
        """Trace 폴더 기준 실제 저장 경로 프리뷰 갱신."""
        mw = self._main_win
        main_folder = mw._le_main_folder.text().strip()
        custom_folder = mw._le_custom_folder.text().strip()
        include_date = mw._cb_save_date.isChecked()

        if not main_folder:
            self._lbl_ds_save_path.setText("(Main Folder 미지정)")
            return

        # Second channel 정보
        channels = self._param_reg.main_ui_profile.second_sweep_channels
        cfg_idx = self._cfg.selected_channel_idx if self._cfg is not None else max(0, self._second_radio_group.checkedId())
        ch = channels[cfg_idx] if (0 <= cfg_idx < len(channels)) else None
        fig_ax = (ch.figure_axis or "").strip() if ch else ""

        # 경로 조립
        d = Path(main_folder)
        subfolder = f"{custom_folder}/trace" if custom_folder else "trace"
        d = d / subfolder
        if include_date:
            d = d / _date.today().strftime("%Y-%m-%d")

        # 파일명 템플릿 (step_val 자리는 placeholder)
        custom_word = mw._le_custom_word.text().strip()
        stem_parts = []
        if custom_word:
            stem_parts.append(custom_word)
        if include_date:
            stem_parts.append(_date.today().strftime("%Y%m%d"))
        stem = "".join(stem_parts)
        id_part = f"_{fig_ax}_<step>" if fig_ax else "_<step>"
        self._lbl_ds_save_path.setText(str(d / f"{stem}{id_part}.dat"))

    def _open_ds_folder(self):
        path_text = self._lbl_ds_save_path.text()
        if not path_text or path_text.startswith("("):
            return
        folder = str(Path(path_text).parent)
        p = Path(folder)
        while p and not p.is_dir():
            p = p.parent
        if p and p.is_dir():
            subprocess.Popen(f'explorer "{p}"')

    def showEvent(self, event):
        # Worker threads are quit in closeEvent. Restart them if needed.
        if not self._worker_thread.isRunning():
            self._worker_thread.start()
        if not self._second_thread.isRunning():
            self._second_thread.start()
        self._update_ds_save_path()
        # 실행 중에는 라디오를 재빌드하지 않는다 — 상태머신이 self._second_channel을
        # 라이브로 읽는데, 재빌드가 이를 None으로 만들면 다음 advance에서 AttributeError로
        # run이 꼬인다. (reload_from_profile과 동일하게 IDLE에서만 재빌드)
        if self._phase == DoubleSweepPhase.IDLE:
            self._rebuild_second_channel_radios()
        self._update_unit_labels()
        # Refresh measurement-condition combo in case alarm_measurements changed
        alarm_meas = self._param_reg.main_ui_profile.alarm_measurements
        self._meascond_panel.refresh_measurements(alarm_meas)
        if self._phase == DoubleSweepPhase.IDLE:
            self._btn_start.setEnabled(not self._main_win._running)
        super().showEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._save_config()      # 저장만 하고 창은 닫지 않음 (실수로 닫힘 방지)
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
