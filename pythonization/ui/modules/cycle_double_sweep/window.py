"""
CycleDoubleSweepWindow: Double Sweep+ (Cycle) 전용 창.

Double Sweep 의 second 축(ITC 온도 / IPS 자기장)을 한 점씩 옮기면서, 각 점에서
first 축(Keithley SMU 등)으로 **Cycle Sweep 한 세트**를 돈다. second 값을 설정한 뒤
실제 물리값이 따라올 시간을 주기 위해 **설정 후 대기 시간**을 따로 둘 수 있다.

second 값 하나의 진행 순서 (모든 값에서 동일):
  PRE_INIT          first → Initial Value 로 이동 (데이터 없음)
  ADVANCING_SECOND  second → array[i]  (advance type 대로: hop/sweep/feedback/wait)
  SETTLING          Settle Wait 만큼 대기 (남은 시간 표시)
  CYCLING           cycle 1..N — 각 cycle 은 targets 를 위에서 아래로 훑는다

예) targets = 30 / -30 / 30 / 0, array = 300,280,260 K, wait = 1h 이면
    300K 설정 → 1h 대기 → 300K 에서 cycle → 280K 설정 → 1h 대기 → 280K 에서 cycle → …

전체 흐름:
  IDLE → [PRE_INIT → ADVANCING_SECOND → SETTLING → CYCLING] × array
       → RETURNING_SECOND_ZERO (옵션) → RETURNING_ZERO (옵션) → IDLE

측정 항목과 미분 채널은 메인 창 설정을 시작 시점에 스냅샷해서 쓰고(Cycle Sweep 과
같은 방식), 저장 폴더·파일명은 이 창의 Save Settings 구획이 따로 갖는다.
"""
import math
import subprocess
import time
from dataclasses import dataclass, field
from datetime import (
    date as _date,
    datetime,
    datetime as _datetime,
    timedelta as _timedelta,
)
from enum import Enum, auto
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING, Tuple

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QDoubleValidator, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
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

from pythonization.config.models import (
    CycleDoubleSweepConfig,
    InstantiatedSecondSweepChannel,
    SecondSweepAdvanceType,
)
from pythonization.instruments.errors import humanize_error, is_comm_error
from pythonization.instruments.factory import resolve_class_path
from pythonization.measurement.channel import sweep_channel_from_instantiated
from pythonization.measurement.data_saver import DataSaver
from pythonization.measurement.resume_log import ResumeLog, ResumePoint
from pythonization.measurement.second_channel_model import SecondChannelModel
from pythonization.measurement.second_channel_worker import (
    SecondChannelRequest,
    SecondChannelWorker,
)
from pythonization.measurement.sweep_worker import StepRequest, StepResult, SweepWorker
from pythonization.ui.dialogs.resume import ResumePickerDialog
from pythonization.ui.panels.graph_window import GraphDataPoint
from pythonization.ui.widgets.help_button import make_help_button

if TYPE_CHECKING:
    from pythonization.ui.main_window import MainWindow

_MONO = QFont("Consolas", 10)

#: 재개 로그에서 이 모듈의 지점을 구분하는 키.
_RESUME_TYPE = "cycle2d"

#: 장비 종류 필터 — (키, 표시 이름, 드라이버 클래스명). 클래스명이 빈 문자열이면 전체.
#: 저장된 class_name 은 구 레이아웃일 수 있으므로 resolve_class_path 로 옮긴 뒤
#: 마지막 조각(클래스명)만 비교한다 — 드라이버 파일이 옮겨져도 판정이 살아남는다.
_DEVICE_KINDS: List[Tuple[str, str, str]] = [
    ("itc", "ITC (온도)",   "OxfordITC"),
    ("ips", "IPS (자기장)", "OxfordIPS"),
    ("all", "전체",         ""),
]


def _driver_class_name(inst_registry, alias: str) -> str:
    """alias 에 지정된 드라이버 클래스 이름 (없으면 빈 문자열)."""
    cfg = inst_registry.get_config(alias)
    if cfg is None:
        return ""
    return resolve_class_path(cfg.class_name).rpartition(".")[2]


def matches_device_kind(inst_registry, alias: str, kind: str) -> bool:
    """alias 가 선택한 장비 종류에 해당하는지. 'all' 은 항상 True."""
    for key, _label, class_name in _DEVICE_KINDS:
        if key == kind:
            if not class_name:
                return True
            return _driver_class_name(inst_registry, alias) == class_name
    return True


def parse_target_value(text: str) -> Optional[float]:
    """target 한 칸의 값. 비었거나 숫자가 아니면 None (호출부가 구분해서 쓴다)."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        value = float(stripped)
    except ValueError:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def generate_array(cfg: CycleDoubleSweepConfig) -> List[float]:
    """second 축이 훑을 값 목록 (numpy 없이 arange, step 부호 자동 처리)."""
    if abs(cfg.array_step) < 1e-12:
        return [cfg.array_from]
    step = cfg.array_step
    eps = abs(step) * 1e-9
    result: List[float] = []
    v = cfg.array_from
    while (step > 0 and v <= cfg.array_to + eps) or (step < 0 and v >= cfg.array_to - eps):
        result.append(round(v, 12))
        v += step
    return result if result else [cfg.array_from]


def estimate_total_seconds(cfg: CycleDoubleSweepConfig, targets: List[float],
                           n_array: int) -> float:
    """예상 소요 시간(초).

    이동 시간 = 거리 * 60 / rate (Time/Point 는 측정 간격일 뿐 이동 속도가 아니다).
    한 second 값당 cycle 1 은 initial_value 에서, 2회차부터는 직전 cycle 의 마지막
    target 에서 시작한다. second 값을 넘어갈 때마다 마지막 target 에서 initial_value 로
    되돌아오는 구간이 한 번씩 더 들어간다 — 첫 값의 '현재값 → initial_value' 는
    현재값을 알 수 없어 빠져 있다.

    second 채널 자체의 이동·안정화 시간은 장비에 달려 있어 넣지 않고, 사용자가
    직접 정한 settle_wait 만 더한다.
    """
    if not targets or n_array <= 0 or cfg.sweep_rate <= 0 or cfg.cycles <= 0:
        return 0.0

    def _travel(distance: float) -> float:
        return abs(distance) * 60.0 / cfg.sweep_rate

    def _chain(start: float) -> float:
        total = 0.0
        prev = start
        for target in targets:
            total += _travel(target - prev)
            prev = target
        return total

    per_point = _chain(cfg.initial_value) + (cfg.cycles - 1) * _chain(targets[-1])
    returns = (n_array - 1) * _travel(cfg.initial_value - targets[-1])
    waits = max(0, n_array - (1 if cfg.skip_first_wait else 0))
    seconds = n_array * per_point + returns + waits * max(0.0, cfg.settle_wait_s)
    if cfg.return_to_zero:
        seconds += _travel(targets[-1])
    return seconds


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


@dataclass
class CycleDoubleSweepContext:
    """시작 시 MainWindow + 이 창에서 스냅샷한 실행 컨텍스트.

    측정 도중 위젯을 다시 읽지 않기 위해 시작 시점의 값을 고정한다.
    """
    sweep_channel: object
    sv_safety_steps: int
    sv_safety_interval_ms: float
    active_meas_indices: List[int]
    active_measurements: List[Tuple[int, str, str, str]]  # (row, alias, desc, cmd)
    sweep_col: Tuple[str, str]
    meas_cols: List[Tuple[str, str]]
    # 저장 설정 — 이 창의 Save Settings 구획에서 스냅샷한다 (메인 창과 무관)
    main_folder: str
    sub_folder: str
    file_name: str
    include_date: bool
    save_enabled: bool
    first_alias: str = ""
    targets: List[float] = field(default_factory=list)


class Phase(Enum):
    IDLE                  = auto()
    PRE_INIT              = auto()   # first → initial value (데이터 없음)
    ADVANCING_SECOND      = auto()   # second → array[i]
    SETTLING              = auto()   # second 설정 후 대기
    CYCLING               = auto()   # cycle 안의 한 구간 진행 중
    RETURNING_SECOND_ZERO = auto()   # 모든 array 종료 후 second → 0
    RETURNING_ZERO        = auto()   # 마지막으로 first → 0 (데이터 없음)


class CycleDoubleSweepWindow(QDialog):
    """
    Double Sweep+ (Cycle) 창.

    - 측정 항목·미분 채널은 main_win 설정을 읽어 쓴다 (읽기 전용)
    - first(cycle) 채널과 second 채널은 이 창이 직접 고른다
    - 자체 SweepWorker + SecondChannelWorker + QThread × 2 + DataSaver
    """

    # main_window 에 sweep lock 신호
    sweep_started  = Signal()
    sweep_finished = Signal()

    # 워커에 요청
    request_step    = Signal(object)   # StepRequest        → _sweep_worker
    request_advance = Signal(object)   # SecondChannelRequest → _second_worker

    def __init__(self, main_win: "MainWindow", param_reg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Double Sweep+ (Cycle)")
        self.setMinimumWidth(820)
        self.resize(940, 880)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._main_win  = main_win
        self._param_reg = param_reg

        self._phase: Phase = Phase.IDLE
        self._cfg: Optional[CycleDoubleSweepConfig] = None
        self._ctx: Optional[CycleDoubleSweepContext] = None

        # second 축 상태
        self._second_table_model = SecondChannelModel()
        self._second_table_win = None
        self._second_channel: Optional[InstantiatedSecondSweepChannel] = None
        self._array: List[float] = []
        self._array_idx: int = 0

        # first(cycle) 축 상태
        self._first_channel = None          # SweepChannel
        self._first_alias: str = ""
        self._targets: List[float] = []
        self._target_rows: List[tuple] = []  # (행 위젯, 값 입력칸, 번호 라벨, 단위 라벨)
        self._cycle_idx: int = 0
        self._seg_idx: int = 0
        self._seg_phase_name: str = ""      # 그래프 곡선 구분용 ("trace"/"retrace")
        self._last_write_value: Optional[float] = None
        self._cycle_filepath = None         # 현재 cycle 의 .dat 경로 (메타 JSON 기준)

        # Settle 대기 카운트다운
        self._settle_deadline: float = 0.0
        self._settle_timer = QTimer(self)
        self._settle_timer.setInterval(1000)
        self._settle_timer.timeout.connect(self._on_settle_tick)

        # 통신 오류 자동 재개 상태
        self._resume_log = ResumeLog()
        self._auto_retry_used = False
        self._last_emit = None              # ("step"|"advance", request)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._auto_resume)

        # DataSaver
        self._data_saver = DataSaver()
        self._data_saver.set_error_callback(
            lambda msg: main_win._log(f"  [DoubleSweep+ DataSaver] {msg}", color="#f44747")
        )

        # Sweep worker — 속성 이름은 _sweep_worker 로 고정한다
        # (main_window._on_config_applied 가 같은 이름으로 임계값·병렬 설정을 밀어 넣는다)
        self._sweep_worker = SweepWorker()
        self._sweep_worker.set_session(main_win._session)
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
        # bound 슬롯으로 연결 (워커 스레드에서 GUI 접근 시 크래시 방지)
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
        self._rebuild_first_channel_radios()
        self._rebuild_second_channel_radios()
        self._update_unit_labels()
        self._update_save_path()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        """창 전체 조립. 위에서 아래로 실행 순서대로 놓는다."""
        dialog_layout = QVBoxLayout(self)
        dialog_layout.setContentsMargins(0, 0, 0, 0)
        self._glow_frame = QFrame()
        self._glow_frame.setObjectName("cdsGlowFrame")
        self._glow_frame.setStyleSheet(
            "QFrame#cdsGlowFrame { border: 3px solid transparent; border-radius: 6px; }"
        )
        dialog_layout.addWidget(self._glow_frame)

        # 구획이 많아 창이 작아지면 잘리므로 전체를 스크롤 영역에 담는다
        glow_layout = QVBoxLayout(self._glow_frame)
        glow_layout.setContentsMargins(3, 3, 3, 3)
        page_scroll = QScrollArea()
        page_scroll.setWidgetResizable(True)
        page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        glow_layout.addWidget(page_scroll)

        page = QWidget()
        page_scroll.setWidget(page)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        outer.addLayout(self._build_title_row())
        outer.addWidget(self._build_second_channel_box())
        outer.addWidget(self._build_sweep_advance_box())
        outer.addWidget(self._build_feedback_box())
        outer.addWidget(self._build_wait_time_box())
        outer.addWidget(self._build_array_box())
        outer.addWidget(self._build_first_channel_box())
        outer.addWidget(self._build_targets_box())
        outer.addWidget(self._build_cycle_params_box())
        outer.addWidget(self._build_save_box())
        outer.addLayout(self._build_estimate_row())
        outer.addWidget(self._build_status_box())
        outer.addLayout(self._build_action_row())
        outer.addStretch()

    # ── 공용 입력 위젯 ───────────────────────────────────────────────────

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

    # ── 구획별 빌더 ──────────────────────────────────────────────────────

    def _build_title_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title = QLabel("Double Sweep+  (Second 축 × Cycle Sweep)")
        title.setStyleSheet("font-weight: bold; font-size: 13px; color: #f78166;")
        row.addWidget(title)
        row.addStretch()
        row.addWidget(make_help_button(self._help_html(), "Double Sweep+ 도움말"))
        return row

    def _build_second_channel_box(self) -> QFrame:
        """바깥 축 선택 — 장비 종류(ITC/IPS)로 목록을 거른 뒤 채널을 고른다."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("① Second Channel  (바깥 축 — ITC 온도 / IPS 자기장)")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #f78166;")
        layout.addWidget(title)

        kind_row = QHBoxLayout()
        kind_row.setSpacing(8)
        kind_row.addWidget(QLabel("장비:"))
        self._kind_radio_group = QButtonGroup(self)
        self._kind_radio_group.setExclusive(True)
        for idx, (_key, label, _cls) in enumerate(_DEVICE_KINDS):
            rb = QRadioButton(label)
            rb.setFont(_MONO)
            self._kind_radio_group.addButton(rb, idx)
            kind_row.addWidget(rb)
        kind_row.addStretch()
        self._kind_radio_group.idToggled.connect(self._on_device_kind_toggled)
        layout.addLayout(kind_row)

        self._second_ch_layout = QVBoxLayout()
        layout.addLayout(self._second_ch_layout)
        self._second_radio_group = QButtonGroup(self)
        self._second_radio_group.setExclusive(True)
        self._second_radio_group.idToggled.connect(self._on_second_radio_toggled)

        self._second_frame = frame
        return frame

    def _build_sweep_advance_box(self) -> QFrame:
        """advance_type == SWEEP 일 때만 보이는 구획 (safety ramp 포함)."""
        self._sweep_adv_frame = QFrame()
        self._sweep_adv_frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self._sweep_adv_frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("SWEEP Channel Settings")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #c586c0;")
        layout.addWidget(title)

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
        self._sb_second_steps.setFont(_MONO)
        self._sb_second_steps.setFixedWidth(100)
        self._sb_second_steps.setEnabled(False)
        form.addRow("Safety Steps:", self._sb_second_steps)

        self._le_second_interval, lbl_interval_unit, cnt_interval = self._make_value_field()
        lbl_interval_unit.setText("ms")
        self._le_second_interval.setEnabled(False)
        form.addRow("Safety Interval:", cnt_interval)

        self._cb_second_safety.toggled.connect(self._sb_second_steps.setEnabled)
        self._cb_second_safety.toggled.connect(self._le_second_interval.setEnabled)

        self._sweep_adv_frame.setVisible(False)
        return self._sweep_adv_frame

    def _build_feedback_box(self) -> QFrame:
        """advance_type == FEEDBACK / THRESHOLD_TIME 일 때 보이는 구획.

        2단계 판정이다: Tolerance(목표 근접) 통과 후 Std Window/Threshold(안정화).
        THRESHOLD_TIME 은 2단계 대신 아래 WAIT 구획의 고정 시간을 쓴다.
        """
        self._feedback_frame = QFrame()
        self._feedback_frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self._feedback_frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("FEEDBACK Channel Settings  (목표 도달 판정)")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #4ec9b0;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        layout.addLayout(form)

        read_row = QWidget()
        read_lay = QHBoxLayout(read_row)
        read_lay.setContentsMargins(0, 0, 0, 0)
        read_lay.setSpacing(6)
        self._le_fb_read_cmd = QLineEdit()
        self._le_fb_read_cmd.setFont(_MONO)
        self._le_fb_read_cmd.setPlaceholderText("예: READ:DEV:MB1.T1:TEMP:SIG:TEMP")
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
        self._sb_fb_std_window.setFont(_MONO)
        self._sb_fb_std_window.setFixedWidth(100)
        self._sb_fb_std_window.setToolTip(
            "0 = 안정화 검사 비활성 (도달만 판정); N>0 = 최근 N개 샘플의 std 검사")
        form.addRow("Std Window:", self._sb_fb_std_window)

        self._le_fb_noisefloor = QLineEdit()
        self._le_fb_noisefloor.setFont(_MONO)
        self._le_fb_noisefloor.setFixedWidth(100)
        self._le_fb_noisefloor.setToolTip("목표값이 0 근처일 때 반드시 설정. 예: 0.001")
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
        """advance_type == WAIT_FOR_TIME / THRESHOLD_TIME 일 때 보이는 구획."""
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
        self._le_wait_time.setToolTip(
            "advance type 자체의 대기 시간입니다.\n"
            "아래 Second Channel Array 의 'Settle Wait' 와는 별개로 둘 다 적용됩니다.")
        form.addRow("Wait Time:", cnt_wait)

        self._wait_frame.setVisible(False)
        return self._wait_frame

    def _build_array_box(self) -> QFrame:
        """second 축이 훑을 값 목록 + 설정 후 대기 시간."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("② Second Channel Array  +  설정 후 대기")
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
        row.addStretch()
        layout.addLayout(row)

        hint = QLabel("(내려가는 방향이면 Step 을 음수로 — 예: 300 → 100, Step −20)")
        hint.setStyleSheet("color: #888888; font-size: 10px;")
        layout.addWidget(hint)

        layout.addLayout(self._build_settle_row())

        table_row = QHBoxLayout()
        self._btn_second_table = QPushButton("Array 값 테이블…")
        self._btn_second_table.setToolTip(
            "Second channel array 값을 표로 편집하는 창을 엽니다.\n"
            "측정 중에도 아직 측정 안 한(대기) 행은 값 수정·추가·삭제할 수 있습니다.")
        self._btn_second_table.clicked.connect(self._open_second_table)
        table_row.addWidget(self._btn_second_table)

        self._cb_keep_table = QCheckBox("테이블 초기화 안 함")
        self._cb_keep_table.setFont(_MONO)
        self._cb_keep_table.setToolTip(
            "체크 시 시작할 때 From/To/Step 으로 테이블을 새로 만들지 않고,\n"
            "테이블 창에서 직접 넣은 값 목록 그대로 측정합니다.")
        table_row.addWidget(self._cb_keep_table)
        table_row.addStretch()
        layout.addLayout(table_row)

        self._cb_to_zero_at_last = QCheckBox("마지막에 second 를 0 으로")
        self._cb_to_zero_at_last.setFont(_MONO)
        self._cb_to_zero_at_last.setToolTip(
            "모든 array 값이 끝난 뒤 second channel 을 0 으로 보냅니다.\n"
            "SWEEP type: 같은 advance 방식으로 0 까지 이동\n"
            "그 외 type: 0 을 한 번 write")
        layout.addWidget(self._cb_to_zero_at_last)

        for field_ in (self._le_arr_from, self._le_arr_to, self._le_arr_step):
            field_.textChanged.connect(self._on_array_inputs_changed)

        self._arr_frame = frame
        return frame

    def _build_settle_row(self) -> QHBoxLayout:
        """Settle Wait — second 값을 설정한 뒤 cycle 시작까지 기다리는 시간."""
        row = QHBoxLayout()
        row.setSpacing(6)
        label = QLabel("Settle Wait:")
        label.setStyleSheet("font-weight: bold;")
        row.addWidget(label)

        self._le_settle_h = QLineEdit()
        self._le_settle_h.setFont(_MONO)
        self._le_settle_h.setFixedWidth(48)
        self._le_settle_h.setValidator(QDoubleValidator(0.0, 1e6, 3))
        row.addWidget(self._le_settle_h)
        row.addWidget(QLabel("시간"))

        self._le_settle_m = QLineEdit()
        self._le_settle_m.setFont(_MONO)
        self._le_settle_m.setFixedWidth(48)
        self._le_settle_m.setValidator(QDoubleValidator(0.0, 1e6, 3))
        row.addWidget(self._le_settle_m)
        row.addWidget(QLabel("분"))

        self._lbl_settle_total = QLabel("= 0s")
        self._lbl_settle_total.setFont(_MONO)
        self._lbl_settle_total.setStyleSheet("color: #79c0ff;")
        row.addWidget(self._lbl_settle_total)

        self._cb_skip_first_wait = QCheckBox("첫 값은 대기 건너뛰기")
        self._cb_skip_first_wait.setFont(_MONO)
        self._cb_skip_first_wait.setToolTip(
            "체크: 첫 array 값에서는 대기 없이 바로 cycle 을 시작합니다.\n"
            "이미 그 온도/자기장에 도달해 있을 때만 켜세요.")
        row.addWidget(self._cb_skip_first_wait)
        row.addStretch()

        for field_ in (self._le_settle_h, self._le_settle_m):
            field_.textChanged.connect(self._on_array_inputs_changed)
        self._cb_skip_first_wait.stateChanged.connect(self._update_est_time)
        return row

    def _build_first_channel_box(self) -> QFrame:
        """안쪽 축(cycle) 채널 선택 — 등록된 sweep value 중에서 고른다."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("③ First Channel  (안쪽 축 — cycle 을 돌릴 채널)")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #f78166;")
        layout.addWidget(title)

        self._first_ch_layout = QVBoxLayout()
        layout.addLayout(self._first_ch_layout)
        self._first_radio_group = QButtonGroup(self)
        self._first_radio_group.setExclusive(True)
        self._first_radio_group.idToggled.connect(self._on_first_radio_toggled)

        self._first_frame = frame
        return frame

    def _build_targets_box(self) -> QFrame:
        """한 cycle 의 목표값 목록 — 한 줄에 하나, '+' 로 아래에 계속 추가한다."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("④ Cycle Targets")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        header.addWidget(title)
        hint = QLabel("(위에서 아래 순서로 sweep 합니다)")
        hint.setStyleSheet("color: #888888; font-size: 10px;")
        header.addWidget(hint)
        header.addStretch()
        layout.addLayout(header)

        # 행이 많아져도 창이 무한히 커지지 않도록 스크롤 영역에 담는다
        self._targets_holder = QWidget()
        self._targets_layout = QVBoxLayout(self._targets_holder)
        self._targets_layout.setContentsMargins(0, 0, 0, 0)
        self._targets_layout.setSpacing(3)
        self._targets_layout.addStretch()      # 행은 항상 이 stretch 앞에 삽입한다

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(90)
        scroll.setMaximumHeight(200)
        scroll.setWidget(self._targets_holder)
        layout.addWidget(scroll)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("+  target 추가")
        btn_add.setFixedHeight(24)
        btn_add.setToolTip("목표값 칸을 아래에 하나 더 추가합니다.")
        btn_add.clicked.connect(self._on_add_target_clicked)
        btn_row.addWidget(btn_add)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._lbl_cycle_preview = QLabel("—")
        self._lbl_cycle_preview.setFont(_MONO)
        self._lbl_cycle_preview.setStyleSheet("color: #888888; font-size: 10px;")
        self._lbl_cycle_preview.setWordWrap(True)
        layout.addWidget(self._lbl_cycle_preview)

        self._targets_frame = frame
        return frame

    def _add_target_row(self, value: Optional[float] = None):
        """목표값 입력 행 하나를 목록 끝에 추가한다."""
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(6)

        lbl_no = QLabel("")
        lbl_no.setFont(_MONO)
        lbl_no.setStyleSheet("color: #888888;")
        lbl_no.setFixedWidth(24)
        row_lay.addWidget(lbl_no)

        edit = QLineEdit()
        edit.setFont(_MONO)
        edit.setFixedWidth(140)
        validator = QDoubleValidator()
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        edit.setValidator(validator)
        if value is not None:
            edit.setText(f"{value:g}")
        edit.textChanged.connect(self._on_cycle_inputs_changed)
        row_lay.addWidget(edit)

        lbl_unit = QLabel(self._first_unit())
        lbl_unit.setFont(_MONO)
        lbl_unit.setStyleSheet("color: #888888;")
        lbl_unit.setMinimumWidth(50)
        row_lay.addWidget(lbl_unit)

        btn_del = QPushButton("−")
        btn_del.setFixedSize(24, 22)
        btn_del.setToolTip("이 목표값 삭제")
        btn_del.clicked.connect(lambda _checked=False, w=row: self._remove_target_row(w))
        row_lay.addWidget(btn_del)
        row_lay.addStretch()

        # 항상 마지막 stretch 앞에 넣는다
        self._targets_layout.insertWidget(self._targets_layout.count() - 1, row)
        self._target_rows.append((row, edit, lbl_no, lbl_unit))
        self._renumber_target_rows()
        return edit

    def _remove_target_row(self, row_widget: QWidget):
        for i, (row, _edit, _no, _unit) in enumerate(self._target_rows):
            if row is row_widget:
                self._target_rows.pop(i)
                self._targets_layout.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
                break
        self._renumber_target_rows()
        self._on_cycle_inputs_changed()

    def _renumber_target_rows(self):
        for i, (_row, _edit, lbl_no, _unit) in enumerate(self._target_rows, start=1):
            lbl_no.setText(f"{i}.")

    def _clear_target_rows(self):
        for row, _edit, _no, _unit in self._target_rows:
            self._targets_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._target_rows = []

    def _on_add_target_clicked(self):
        edit = self._add_target_row()
        edit.setFocus()
        self._on_cycle_inputs_changed()

    def _collect_targets(self) -> Optional[List[float]]:
        """행 입력을 목표값 목록으로.

        빈 칸은 '아직 안 채운 행'으로 보고 건너뛴다. 숫자로 읽을 수 없는 값이
        하나라도 있으면 None (호출부가 오류로 처리).
        """
        values: List[float] = []
        for _row, edit, _no, _unit in self._target_rows:
            if not edit.text().strip():
                continue
            value = parse_target_value(edit.text())
            if value is None:
                return None
            values.append(value)
        return values

    def _build_cycle_params_box(self) -> QFrame:
        """cycle 공통 파라미터 — 초기값, rate/Time per point, 반복 횟수."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("⑤ Cycle Parameters")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        layout.addLayout(form)

        self._le_initial, self._lbl_initial_unit, cnt_initial = self._make_value_field()
        self._le_initial.setToolTip(
            "각 second 값에서 cycle 을 시작하기 전, first 채널을 먼저 이 값까지\n"
            "옮깁니다 (데이터를 기록하지 않습니다). second 를 바꾸는 동안 시료에\n"
            "걸리는 값을 안전한 지점에 두는 역할도 합니다.")
        form.addRow("Initial Value:", cnt_initial)

        self._le_rate, self._lbl_rate_unit, cnt_rate = self._make_value_field()
        form.addRow("Sweep Rate:", cnt_rate)

        self._le_tpp, lbl_tpp_unit, cnt_tpp = self._make_value_field()
        lbl_tpp_unit.setText("sec")
        form.addRow("Time / Point:", cnt_tpp)

        self._sb_cycles = QSpinBox()
        self._sb_cycles.setRange(1, 100000)
        self._sb_cycles.setValue(1)
        self._sb_cycles.setFont(_MONO)
        self._sb_cycles.setFixedWidth(100)
        self._sb_cycles.setToolTip(
            "second 값 하나당 cycle 을 몇 번 반복할지. cycle 마다 .dat 파일이 하나 생깁니다.")
        form.addRow("Cycles / point:", self._sb_cycles)

        self._cb_return_to_zero = QCheckBox("마지막에 first 를 0 으로")
        self._cb_return_to_zero.setFont(_MONO)
        self._cb_return_to_zero.setToolTip(
            "체크: 모든 측정이 끝난 뒤 같은 Sweep Rate 로 first 채널을 0 까지 되돌립니다 "
            "(이 구간은 데이터를 기록하지 않습니다).")
        form.addRow("", self._cb_return_to_zero)

        self._le_initial.textChanged.connect(self._on_cycle_inputs_changed)
        self._le_rate.textChanged.connect(self._update_est_time)
        self._le_tpp.textChanged.connect(self._update_est_time)
        self._sb_cycles.valueChanged.connect(self._update_est_time)
        self._cb_return_to_zero.stateChanged.connect(self._update_est_time)

        self._param_frame = frame
        return frame

    def _build_save_box(self) -> QFrame:
        """저장 위치·파일명 — 이 모듈이 직접 갖는다 (메인 창 설정과 별개)."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        header = QHBoxLayout()
        title = QLabel("⑥ Save Settings")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        header.addWidget(title)
        header.addStretch()
        btn_copy = QPushButton("Main 창 설정 가져오기")
        btn_copy.setFixedHeight(22)
        btn_copy.setToolTip("메인 창의 저장 폴더·파일명 설정을 그대로 복사해 옵니다.")
        btn_copy.clicked.connect(self._copy_save_settings_from_main)
        header.addWidget(btn_copy)
        layout.addLayout(header)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        layout.addLayout(form)

        folder_row = QWidget()
        folder_lay = QHBoxLayout(folder_row)
        folder_lay.setContentsMargins(0, 0, 0, 0)
        folder_lay.setSpacing(6)
        self._le_main_folder = QLineEdit()
        self._le_main_folder.setFont(_MONO)
        self._le_main_folder.setPlaceholderText(r"예: D:\data")
        folder_lay.addWidget(self._le_main_folder, stretch=1)
        self._btn_browse = QPushButton("찾아보기…")
        self._btn_browse.setFixedHeight(22)
        self._btn_browse.clicked.connect(self._browse_main_folder)
        folder_lay.addWidget(self._btn_browse)
        form.addRow("Main Folder:", folder_row)

        self._le_sub_folder = QLineEdit()
        self._le_sub_folder.setFont(_MONO)
        self._le_sub_folder.setPlaceholderText("예: sampleA/cycle2d")
        form.addRow("Sub Folder:", self._le_sub_folder)

        self._le_file_name = QLineEdit()
        self._le_file_name.setFont(_MONO)
        self._le_file_name.setPlaceholderText("예: sampleA")
        self._le_file_name.setToolTip(
            "파일명 앞부분입니다. 뒤에 second 값과 cycle 번호가 자동으로 붙습니다.")
        form.addRow("File Name:", self._le_file_name)

        self._cb_include_date = QCheckBox("날짜 포함 (YYYY-MM-DD 폴더 + 파일명에 YYYYMMDD)")
        self._cb_include_date.setFont(_MONO)
        form.addRow("", self._cb_include_date)

        self._cb_save_enabled = QCheckBox("파일로 저장")
        self._cb_save_enabled.setFont(_MONO)
        self._cb_save_enabled.setToolTip(
            "끄면 측정은 하되 .dat 파일을 만들지 않습니다 (그래프·Data 창에만 표시).")
        form.addRow("", self._cb_save_enabled)

        layout.addLayout(self._build_save_path_row())

        for field_ in (self._le_main_folder, self._le_sub_folder, self._le_file_name):
            field_.textChanged.connect(self._update_save_path)
        self._cb_include_date.stateChanged.connect(self._update_save_path)

        self._save_frame = frame
        return frame

    def _build_save_path_row(self) -> QHBoxLayout:
        """저장 경로 미리보기 + 폴더 열기 버튼."""
        row = QHBoxLayout()
        label = QLabel("→")
        label.setFont(_MONO)
        label.setStyleSheet("color: #888888;")
        row.addWidget(label, alignment=Qt.AlignmentFlag.AlignTop)

        self._lbl_save_path = QLabel("—")
        self._lbl_save_path.setFont(QFont("Consolas", 8))
        self._lbl_save_path.setStyleSheet("color: #555555;")
        self._lbl_save_path.setWordWrap(True)
        self._lbl_save_path.setMinimumHeight(28)
        self._lbl_save_path.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        row.addWidget(self._lbl_save_path, stretch=1)

        btn_open = QPushButton("📂")
        btn_open.setFixedSize(28, 20)
        btn_open.setFont(_MONO)
        btn_open.setToolTip("저장 폴더 열기")
        btn_open.clicked.connect(self._open_save_folder)
        row.addWidget(btn_open, alignment=Qt.AlignmentFlag.AlignTop)
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

        hint = QLabel("(second 채널의 이동·안정화 시간은 빠져 있습니다)")
        hint.setStyleSheet("color: #555555; font-size: 9px;")
        row.addWidget(hint)
        row.addStretch()
        return row

    def _build_status_box(self) -> QFrame:
        """진행 상태 한 줄 — phase / second 값 / array·cycle·segment 진척."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 4, 8, 4)

        self._lbl_phase = QLabel("IDLE")
        self._lbl_phase.setFont(_MONO)
        self._lbl_phase.setStyleSheet("color: #888888;")
        layout.addWidget(QLabel("Phase:"))
        layout.addWidget(self._lbl_phase)

        layout.addSpacing(10)
        layout.addWidget(QLabel("Second:"))
        self._lbl_second_val = QLabel("—")
        self._lbl_second_val.setFont(_MONO)
        self._lbl_second_val.setStyleSheet("color: #f78166;")
        layout.addWidget(self._lbl_second_val)

        layout.addSpacing(10)
        layout.addWidget(QLabel("Array:"))
        self._lbl_array_progress = QLabel("—/—")
        self._lbl_array_progress.setFont(_MONO)
        self._lbl_array_progress.setStyleSheet("color: #79c0ff;")
        layout.addWidget(self._lbl_array_progress)

        layout.addSpacing(10)
        layout.addWidget(QLabel("Cycle:"))
        self._lbl_cycle_progress = QLabel("—/—")
        self._lbl_cycle_progress.setFont(_MONO)
        self._lbl_cycle_progress.setStyleSheet("color: #79c0ff;")
        layout.addWidget(self._lbl_cycle_progress)

        layout.addSpacing(10)
        layout.addWidget(QLabel("Seg:"))
        self._lbl_seg_progress = QLabel("—/—")
        self._lbl_seg_progress.setFont(_MONO)
        self._lbl_seg_progress.setStyleSheet("color: #79c0ff;")
        layout.addWidget(self._lbl_seg_progress)

        layout.addSpacing(10)
        self._lbl_countdown = QLabel("")
        self._lbl_countdown.setFont(_MONO)
        self._lbl_countdown.setStyleSheet("color: #d7ba7d; font-weight: bold;")
        layout.addWidget(self._lbl_countdown)
        layout.addStretch()
        return frame

    def _build_action_row(self) -> QHBoxLayout:
        """Start / Stop / Resume."""
        self._btn_start = QPushButton("Start Double Sweep+")
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

        self._btn_resume = QPushButton("Resume")
        self._btn_resume.setMinimumHeight(40)
        self._btn_resume.setToolTip(
            "통신 오류/Stop 으로 중단된 측정을 저장된 array 지점부터 다시 시작합니다.\n"
            "재개 단위는 array 값이며, 그 값의 cycle 파일은 새로 씁니다.")
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

    @staticmethod
    def _help_html() -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>Double Sweep+ — 온도/자기장 한 점마다 Cycle Sweep 한 세트</b><hr>"
            "바깥 축(<b>Second</b>)을 한 점씩 옮기고, 그 점에서 안쪽 축(<b>First</b>)으로 "
            "<b>Cycle Sweep</b> 을 통째로 돕니다.<hr>"

            "<b>■ 한 second 값의 진행 순서</b> (모든 값에서 동일)<br>"
            "&nbsp;① <b>First → Initial Value</b> 이동 (데이터 없음)<br>"
            "&nbsp;② <b>Second 설정</b> (advance type 대로)<br>"
            "&nbsp;③ <b>Settle Wait</b> 만큼 대기 — 실제 온도/자기장이 따라올 시간<br>"
            "&nbsp;④ <b>Cycle 1..N</b> — targets 를 위에서 아래로 훑음<br>"
            "예) targets <code>30 / -30 / 30 / 0</code>, array <code>300,280,260 K</code>, "
            "Settle Wait <code>1시간</code> 이면<br>"
            "&nbsp;&nbsp;300K 설정 → 1h 대기 → 300K cycle → 280K 설정 → 1h 대기 → "
            "280K cycle → …<hr>"

            "<b>■ ① Second Channel</b><br>"
            "장비 라디오로 <b>ITC(온도) / IPS(자기장)</b> 를 고르면 그 장비로 등록된 "
            "Second Sweep Channel 만 목록에 남습니다. 목록이 비면 Instrument Settings 의 "
            "드라이버 지정(OxfordITC / OxfordIPS)과 Parameter Manager 등록을 확인하세요.<br>"
            "advance type(simple_hop / sweep / feedback / wait_for_time / threshold_time)은 "
            "<b>Parameter Manager 에서 채널을 등록할 때</b> 정하고, 세부 값만 이 창에서 고칩니다.<hr>"

            "<b>■ ② Array + Settle Wait</b><br>"
            "From/To/Step 으로 second 값 목록을 만듭니다. 내려가는 방향이면 <b>Step 을 음수</b>로 "
            "(예: 300 → 100, Step −20). <code>Array 값 테이블…</code> 에서 임의의 목록을 직접 "
            "넣을 수도 있고, 측정 중에도 아직 측정 안 한 행은 고칠 수 있습니다.<br>"
            "<b>Settle Wait</b> 는 second 를 설정한 뒤 cycle 시작까지 무조건 기다리는 시간입니다. "
            "advance type 자체의 대기(feedback 도달 판정, wait_for_time)와는 <b>별개로 둘 다</b> "
            "적용됩니다. 이미 그 지점에 있다면 <code>첫 값은 대기 건너뛰기</code> 를 켜세요.<hr>"

            "<b>■ ③④⑤ First Channel / Targets / Cycle</b><br>"
            "Cycle Sweep 창과 같은 방식입니다. <b>Initial Value</b> 에서 출발해 targets 를 "
            "순서대로 훑는 것이 한 cycle 이고, 2회차부터는 직전 cycle 의 마지막 target 에서 "
            "이어집니다. Sweep Rate·Time/Point 는 cycle 전체 공통입니다.<hr>"

            "<b>■ ⑥ 저장</b><br>"
            "저장 폴더·파일명은 <b>이 창에서 직접</b> 정합니다 (메인 창 설정과 별개). "
            "<b>second 값 하나 × cycle 하나당 .dat 파일 하나</b>가 생깁니다:<br>"
            "<code>…/{File Name}{YYYYMMDD}_{axis}_{second값}_cycleNNN.dat</code><br>"
            "같은 이름·같은 날짜로 다시 돌리면 <b>기존 파일을 덮어씁니다</b> — File Name 을 바꾸세요.<hr>"

            "<b>■ 참고</b><br>"
            "&nbsp;• 측정 항목과 미분 채널은 <b>Main 화면 설정</b>을 그대로 씁니다.<br>"
            "&nbsp;• 통신 오류 시 10초 뒤 자동 재시도, 다시 실패하면 멈추고 그 array 지점을 저장 → "
            "<b>Resume</b> 로 재개.<br>"
            "&nbsp;• Stop 으로 멈춰도 그 지점이 저장되어 나중에 Resume 할 수 있습니다."
            "</body></html>"
        )

    # ------------------------------------------------------------------
    # 채널 라디오
    # ------------------------------------------------------------------

    def _device_kind(self) -> str:
        idx = self._kind_radio_group.checkedId()
        if 0 <= idx < len(_DEVICE_KINDS):
            return _DEVICE_KINDS[idx][0]
        return "all"

    def _on_device_kind_toggled(self, _btn_id: int, checked: bool):
        if checked:
            self._rebuild_second_channel_radios()

    def _rebuild_second_channel_radios(self):
        """선택한 장비 종류에 해당하는 second channel 만 라디오로 만든다.

        버튼 id 는 second_sweep_channels 의 **원본 인덱스**를 그대로 쓴다 —
        selected_channel_idx 저장과 목록 조회가 이 값에 묶여 있다.
        """
        for btn in self._second_radio_group.buttons():
            self._second_radio_group.removeButton(btn)
        while self._second_ch_layout.count():
            item = self._second_ch_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        channels = self._param_reg.main_ui_profile.second_sweep_channels
        kind = self._device_kind()
        registry = self._main_win._registry
        indices = [i for i, ch in enumerate(channels)
                   if matches_device_kind(registry, ch.alias, kind)]

        if not indices:
            label = dict((k, lbl) for k, lbl, _c in _DEVICE_KINDS).get(kind, kind)
            lbl = QLabel(f"({label} 로 등록된 Second Sweep Channel 이 없습니다 — "
                         "Instrument Settings 의 드라이버 지정과 "
                         "Parameter Manager 를 확인하세요)")
            lbl.setStyleSheet("color: #555555; font-size: 11px;")
            lbl.setWordWrap(True)
            self._second_ch_layout.addWidget(lbl)
            self._second_channel = None
            self._sweep_adv_frame.setVisible(False)
            self._feedback_frame.setVisible(False)
            self._wait_frame.setVisible(False)
            return

        for idx in indices:
            ch = channels[idx]
            rb = QRadioButton(
                f"[{ch.alias}]  {ch.description}  ({ch.unit})  — {ch.advance_type.value}")
            rb.setFont(_MONO)
            rb.setStyleSheet("color: #f78166;")
            self._second_radio_group.addButton(rb, idx)
            self._second_ch_layout.addWidget(rb)

        saved = self._param_reg.cycle_double_sweep_config.selected_channel_idx
        btn = self._second_radio_group.button(saved)
        if btn is None:
            btn = self._second_radio_group.button(indices[0])
        btn.setChecked(True)
        # 이미 체크돼 있던 버튼을 다시 setChecked 하면 idToggled 가 안 온다 → 직접 반영
        self._on_second_radio_toggled(self._second_radio_group.checkedId(), True)

    def _on_second_radio_toggled(self, btn_id: int, checked: bool):
        if not checked:
            return
        channels = self._param_reg.main_ui_profile.second_sweep_channels
        if 0 <= btn_id < len(channels):
            self._second_channel = channels[btn_id]
            adv = self._second_channel.advance_type
            # THRESHOLD_TIME = 도달(feedback 필드) + 고정 시간 대기(wait_time) → 두 구획 모두
            is_tt = adv == SecondSweepAdvanceType.THRESHOLD_TIME
            self._sweep_adv_frame.setVisible(adv == SecondSweepAdvanceType.SWEEP)
            self._feedback_frame.setVisible(
                adv == SecondSweepAdvanceType.FEEDBACK or is_tt)
            self._wait_frame.setVisible(
                adv == SecondSweepAdvanceType.WAIT_FOR_TIME or is_tt)
            self._update_feedback_read_cmd_state()
        else:
            self._second_channel = None
        self._update_unit_labels()

    def _rebuild_first_channel_radios(self):
        """등록된 sweep value 전체를 first(cycle) 채널 후보로 나열한다."""
        for btn in self._first_radio_group.buttons():
            self._first_radio_group.removeButton(btn)
        while self._first_ch_layout.count():
            item = self._first_ch_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        sweep_values = self._param_reg.main_ui_profile.sweep_values
        if not sweep_values:
            lbl = QLabel("(Parameter Manager 에서 Sweep Value 를 등록하세요)")
            lbl.setStyleSheet("color: #555555; font-size: 11px;")
            lbl.setWordWrap(True)
            self._first_ch_layout.addWidget(lbl)
            self._first_channel = None
            self._first_alias = ""
            return

        for idx, sv in enumerate(sweep_values):
            rb = QRadioButton(f"[{sv.alias}]  {sv.description}  ({sv.unit})")
            rb.setFont(_MONO)
            rb.setStyleSheet("color: #f78166;")
            self._first_radio_group.addButton(rb, idx)
            self._first_ch_layout.addWidget(rb)

        saved = self._param_reg.cycle_double_sweep_config.first_channel_idx
        btn = self._first_radio_group.button(saved)
        if btn is None:
            btn = self._first_radio_group.button(0)
        btn.setChecked(True)
        self._on_first_radio_toggled(self._first_radio_group.checkedId(), True)

    def _on_first_radio_toggled(self, btn_id: int, checked: bool):
        if not checked:
            return
        sweep_values = self._param_reg.main_ui_profile.sweep_values
        if 0 <= btn_id < len(sweep_values):
            sv = sweep_values[btn_id]
            self._first_channel = sweep_channel_from_instantiated(sv)
            self._first_alias = sv.alias
        else:
            self._first_channel = None
            self._first_alias = ""
        self._update_unit_labels()

    def selected_aliases(self) -> List[str]:
        """이 창이 고른 first/second 채널의 alias 목록 (연결 테스트용)."""
        out = []
        if self._first_alias:
            out.append(self._first_alias)
        if self._second_channel is not None:
            out.append(self._second_channel.alias)
        return out

    def is_idle(self) -> bool:
        """측정 중이 아닌지. main_window 가 UI 잠금 복원 여부를 판단할 때 쓴다."""
        return self._phase == Phase.IDLE

    def shutdown_threads(self):
        """앱 종료 시 워커 스레드를 정지·대기한다 (VNA/MFLI 창과 같은 규약)."""
        self._sweep_timer.stop()
        self._retry_timer.stop()
        self._settle_timer.stop()
        self._sweep_worker.request_stop()
        self._second_worker.request_stop()
        if self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait()
        if self._second_thread.isRunning():
            self._second_thread.quit()
            self._second_thread.wait()

    # ------------------------------------------------------------------
    # Config load / save
    # ------------------------------------------------------------------

    def _load_config(self):
        cfg = self._param_reg.cycle_double_sweep_config

        kind_idx = next((i for i, (k, _l, _c) in enumerate(_DEVICE_KINDS)
                         if k == cfg.device_kind), len(_DEVICE_KINDS) - 1)
        btn = self._kind_radio_group.button(kind_idx)
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)

        self._le_arr_from.setText(f"{cfg.array_from:g}")
        self._le_arr_to.setText(f"{cfg.array_to:g}")
        self._le_arr_step.setText(f"{cfg.array_step:g}")
        self._cb_keep_table.setChecked(cfg.keep_table)
        self._cb_to_zero_at_last.setChecked(cfg.to_zero_at_last)
        self._set_settle_fields(cfg.settle_wait_s)
        self._cb_skip_first_wait.setChecked(cfg.skip_first_wait)

        self._le_second_rate.setText(f"{cfg.second_sweep_rate:g}")
        self._cb_second_safety.setChecked(cfg.second_use_safety)
        self._sb_second_steps.setValue(cfg.second_safety_steps)
        self._le_second_interval.setText(f"{cfg.second_safety_interval_ms:g}")
        # FEEDBACK — 프로파일에 cmd 가 있으면 상태 먼저 반영 후 저장값으로 덮지 않는다
        self._update_feedback_read_cmd_state()
        if self._le_fb_read_cmd.isEnabled():
            self._le_fb_read_cmd.setText(cfg.second_feedback_read_cmd)
        self._le_fb_poll.setText(f"{cfg.second_feedback_poll_interval:g}")
        self._le_fb_tol.setText(f"{cfg.second_feedback_tolerance_pct:g}")
        self._sb_fb_std_window.setValue(cfg.second_feedback_std_window)
        self._le_fb_noisefloor.setText(f"{cfg.second_feedback_noisefloor:g}")
        self._le_fb_std_thresh.setText(f"{cfg.second_feedback_std_threshold:g}")
        self._le_wait_time.setText(f"{cfg.second_wait_time:g}")

        self._clear_target_rows()
        for value in cfg.targets:
            self._add_target_row(value)
        if not self._target_rows:
            self._add_target_row()      # 빈 칸 하나는 남겨 둔다 (어디에 적는지 보이게)
        self._le_initial.setText(f"{cfg.initial_value:g}")
        self._le_rate.setText(f"{cfg.sweep_rate:g}")
        self._le_tpp.setText(f"{cfg.time_per_point:g}")
        self._sb_cycles.setValue(max(1, cfg.cycles))
        self._cb_return_to_zero.setChecked(cfg.return_to_zero)

        if cfg.main_folder.strip():
            self._le_main_folder.setText(cfg.main_folder)
            self._le_sub_folder.setText(cfg.sub_folder)
            self._le_file_name.setText(cfg.file_name)
            self._cb_include_date.setChecked(cfg.include_date)
            self._cb_save_enabled.setChecked(cfg.save_enabled)
        else:
            # 아직 한 번도 설정하지 않은 프로파일 — 메인 창 값으로 시작한다
            self._copy_save_settings_from_main()

        self._update_settle_label()
        self._update_n_points()
        self._update_cycle_preview()
        self._update_est_time()
        self._update_save_path()

    def _save_config(self):
        self._param_reg.save_cycle_double_sweep_config(
            self._current_cfg(with_selection=True))

    def _current_cfg(self, with_selection: bool = False) -> CycleDoubleSweepConfig:
        """위젯 현재 값으로 만든 설정 객체.

        실행 중에는 위젯을 다시 읽지 않도록 시작 시점에 한 번 만들어 고정한다.
        """
        cfg = CycleDoubleSweepConfig(
            device_kind=self._device_kind(),
            array_from=self._parse_float(self._le_arr_from.text(), 0.0),
            array_to=self._parse_float(self._le_arr_to.text(), 0.0),
            array_step=self._parse_float(self._le_arr_step.text(), 0.0),
            keep_table=self._cb_keep_table.isChecked(),
            settle_wait_s=self._settle_seconds(),
            skip_first_wait=self._cb_skip_first_wait.isChecked(),
            to_zero_at_last=self._cb_to_zero_at_last.isChecked(),
            second_sweep_rate=self._parse_float(self._le_second_rate.text(), 1.0),
            second_use_safety=self._cb_second_safety.isChecked(),
            second_safety_steps=self._sb_second_steps.value(),
            second_safety_interval_ms=self._parse_float(
                self._le_second_interval.text(), 0.0),
            second_feedback_read_cmd=self._le_fb_read_cmd.text().strip(),
            second_feedback_poll_interval=self._parse_float(self._le_fb_poll.text(), 1.0),
            second_feedback_tolerance_pct=self._parse_float(self._le_fb_tol.text(), 95.0),
            second_feedback_std_window=self._sb_fb_std_window.value(),
            second_feedback_noisefloor=self._parse_float(
                self._le_fb_noisefloor.text(), 0.0),
            second_feedback_std_threshold=self._parse_float(
                self._le_fb_std_thresh.text(), 0.01),
            second_wait_time=self._parse_float(self._le_wait_time.text(), 1.0),
            initial_value=self._parse_float(self._le_initial.text(), 0.0),
            targets=self._collect_targets() or [],
            sweep_rate=self._parse_float(self._le_rate.text(), 1.0),
            time_per_point=self._parse_float(self._le_tpp.text(), 1.0),
            cycles=self._sb_cycles.value(),
            return_to_zero=self._cb_return_to_zero.isChecked(),
            main_folder=self._le_main_folder.text(),
            sub_folder=self._le_sub_folder.text(),
            file_name=self._le_file_name.text(),
            include_date=self._cb_include_date.isChecked(),
            save_enabled=self._cb_save_enabled.isChecked(),
        )
        if with_selection:
            cfg.selected_channel_idx = max(0, self._second_radio_group.checkedId())
            cfg.first_channel_idx = max(0, self._first_radio_group.checkedId())
        return cfg

    @staticmethod
    def _parse_float(text: str, default: float = 0.0) -> float:
        try:
            return float(text.strip())
        except (ValueError, AttributeError):
            return default

    def reload_from_profile(self):
        """활성 프로파일이 바뀌면 이 창 설정도 새 프로파일 값으로 다시 읽는다.

        보관형 창이라 showEvent 만으로는 동기화되지 않는다 — 갱신하지 않으면
        처음 열었던 프로파일 값에 고정되어 다른 프로파일을 덮어쓴다.
        측정 중에는 설정이 꼬이지 않도록 무시한다.
        """
        if self._phase != Phase.IDLE:
            return
        self._load_config()
        self._rebuild_first_channel_radios()
        self._rebuild_second_channel_radios()
        self._update_unit_labels()
        self._update_save_path()

    # ------------------------------------------------------------------
    # 파생 표시 (단위·미리보기·예상 시간·저장 경로)
    # ------------------------------------------------------------------

    def _first_unit(self) -> str:
        sweep_values = self._param_reg.main_ui_profile.sweep_values
        idx = self._first_radio_group.checkedId()
        if 0 <= idx < len(sweep_values):
            return sweep_values[idx].unit
        return ""

    def _update_unit_labels(self):
        unit1 = self._first_unit()
        self._lbl_initial_unit.setText(unit1)
        self._lbl_rate_unit.setText(f"{unit1}/min" if unit1 else "units/min")
        for _row, _edit, _no, lbl_unit in self._target_rows:
            lbl_unit.setText(unit1)

        unit2 = self._second_channel.unit if self._second_channel else ""
        self._lbl_arr_from_unit.setText(unit2)
        self._lbl_arr_to_unit.setText(unit2)
        self._lbl_arr_step_unit.setText(unit2)
        self._lbl_second_rate_unit.setText(f"{unit2}/min" if unit2 else "units/min")
        self._update_cycle_preview()

    def _update_feedback_read_cmd_state(self) -> None:
        """profile 에 Read Cmd 가 이미 설정된 경우 입력칸을 잠근다."""
        ch = self._second_channel
        if ch is None:
            return
        if ch.advance_type not in (SecondSweepAdvanceType.FEEDBACK,
                                   SecondSweepAdvanceType.THRESHOLD_TIME):
            return
        profile_cmd = ch.feedback_read_cmd.strip()
        if profile_cmd:
            self._le_fb_read_cmd.setEnabled(False)
            self._le_fb_read_cmd.setText(profile_cmd)
            self._lbl_fb_read_hint.setText("(profile 설정)")
        else:
            self._le_fb_read_cmd.setEnabled(True)
            self._lbl_fb_read_hint.setText("")

    def _settle_seconds(self) -> float:
        hours = self._parse_float(self._le_settle_h.text(), 0.0)
        minutes = self._parse_float(self._le_settle_m.text(), 0.0)
        return max(0.0, hours * 3600.0 + minutes * 60.0)

    def _set_settle_fields(self, seconds: float):
        seconds = max(0.0, seconds)
        hours = int(seconds // 3600)
        minutes = (seconds - hours * 3600) / 60.0
        self._le_settle_h.setText(f"{hours:g}")
        self._le_settle_m.setText(f"{minutes:g}")

    def _update_settle_label(self):
        self._lbl_settle_total.setText(f"= {_fmt_hms(self._settle_seconds())}")

    def _on_array_inputs_changed(self):
        self._update_settle_label()
        self._update_n_points()
        self._update_est_time()

    def _on_cycle_inputs_changed(self):
        self._update_cycle_preview()
        self._update_est_time()

    def _update_n_points(self):
        self._lbl_n_points.setText(f"→ {len(generate_array(self._current_cfg()))} pts")

    def _update_cycle_preview(self):
        """'(현재값) → 0(초기값) → 30 → -30 → 0' 식으로 한 cycle 을 보여 준다."""
        targets = self._collect_targets()
        if targets is None:
            self._lbl_cycle_preview.setText("숫자로 읽을 수 없는 값이 있습니다.")
            self._lbl_cycle_preview.setStyleSheet("color: #f44747; font-size: 10px;")
            return
        self._lbl_cycle_preview.setStyleSheet("color: #888888; font-size: 10px;")
        if not targets:
            self._lbl_cycle_preview.setText("목표값을 하나 이상 입력하세요.")
            return
        unit = self._first_unit()
        initial = self._parse_float(self._le_initial.text(), 0.0)
        chain = "  →  ".join(f"{v:g}{unit}" for v in targets)
        self._lbl_cycle_preview.setText(
            f"{initial:g}{unit}(초기값)  →  {chain}     "
            f"[{len(targets)} 구간 / cycle]")

    def _update_est_time(self):
        cfg = self._current_cfg()
        targets = self._collect_targets() or []
        n_array = len(generate_array(cfg))
        seconds = estimate_total_seconds(cfg, targets, n_array)
        self._lbl_est_time.setText(_fmt_hms(seconds))
        eta = _datetime.now() + _timedelta(seconds=seconds)
        if seconds < 86400:
            self._lbl_eta.setText(eta.strftime("%H:%M"))
        else:
            self._lbl_eta.setText(eta.strftime("%m/%d %H:%M"))

    @staticmethod
    def _step_file_name(second_value, cycle_no, fig_axis: str,
                        file_name: str, include_date: bool) -> str:
        """second 값과 cycle 번호가 들어간 파일명.

        second_value / cycle_no 가 숫자면 실제 이름, 문자열이면 미리보기용 자리표시자.
        앞부분(file_name·날짜)이 전부 비면 밑줄 없이 시작한다.
        """
        stem_parts = []
        if file_name.strip():
            stem_parts.append(file_name.strip())
        if include_date:
            stem_parts.append(_date.today().strftime("%Y%m%d"))
        stem = "".join(stem_parts)

        val = (f"{second_value:.6g}".replace('+', '')
               if isinstance(second_value, (int, float)) else str(second_value))
        axis = (fig_axis or "").strip()
        id_part = f"{axis}_{val}" if axis else val
        tag = f"cycle{cycle_no:03d}" if isinstance(cycle_no, int) else f"cycle{cycle_no}"
        body = f"{id_part}_{tag}"
        return f"{stem}_{body}.dat" if stem else f"{body}.dat"

    def _second_fig_axis(self) -> str:
        ch = self._second_channel
        return (ch.figure_axis or "").strip() if ch else ""

    def _save_target_dir(self, main_folder: str, sub_folder: str,
                         include_date: bool) -> Path:
        """DataSaver 와 같은 규칙으로 저장 폴더를 조립한다."""
        directory = Path(main_folder.strip())
        if sub_folder.strip():
            directory = directory / sub_folder.strip()
        if include_date:
            directory = directory / _date.today().strftime("%Y-%m-%d")
        return directory

    def _update_save_path(self):
        """저장 경로 미리보기 갱신 (second 값·cycle 번호는 자리표시자)."""
        main_folder = self._le_main_folder.text().strip()
        if not main_folder:
            self._lbl_save_path.setText("(Main Folder 미지정 — 저장할 수 없습니다)")
            self._lbl_save_path.setStyleSheet("color: #d7ba7d;")
            return
        self._lbl_save_path.setStyleSheet("color: #555555;")
        include_date = self._cb_include_date.isChecked()
        directory = self._save_target_dir(
            main_folder, self._le_sub_folder.text(), include_date)
        name = self._step_file_name("<2nd>", "NNN", self._second_fig_axis(),
                                    self._le_file_name.text(), include_date)
        self._lbl_save_path.setText(str(directory / name))

    def _browse_main_folder(self):
        start = self._le_main_folder.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Main Folder 선택", start)
        if chosen:
            self._le_main_folder.setText(chosen)

    def _copy_save_settings_from_main(self):
        """메인 창의 저장 설정을 그대로 복사해 온다."""
        main_win = self._main_win
        self._le_main_folder.setText(main_win._le_main_folder.text())
        sub = main_win._le_custom_folder.text().strip()
        self._le_sub_folder.setText(f"{sub}/cycle2d" if sub else "cycle2d")
        self._le_file_name.setText(main_win._le_custom_word.text())
        self._cb_include_date.setChecked(main_win._cb_save_date.isChecked())
        self._cb_save_enabled.setChecked(main_win._cb_save_enable.isChecked())
        self._update_save_path()

    def _open_save_folder(self):
        """미리보기 경로의 폴더를 연다 (아직 없으면 존재하는 상위 폴더까지 거슬러 올라간다)."""
        path_text = self._lbl_save_path.text()
        if not path_text or path_text.startswith("("):
            return
        p = Path(path_text).parent
        while p and not p.is_dir():
            p = p.parent
        if p and p.is_dir():
            subprocess.Popen(f'explorer "{p}"')

    # ------------------------------------------------------------------
    # Array 값 테이블
    # ------------------------------------------------------------------

    def _open_second_table(self):
        """Array 값 테이블 편집 창을 연다(없으면 생성)."""
        win = self._second_table_win
        if win is None:
            from pythonization.ui.panels.second_channel_table_window import (
                SecondChannelTableWindow,
            )
            win = SecondChannelTableWindow(self._second_table_model, self, parent=self)
            self._second_table_win = win
        busy = self._phase != Phase.IDLE
        if not busy and self._second_table_model.count() == 0:
            self._reset_second_table_from_controls()
        win.set_running(busy)
        win.refresh()
        win.show()
        win.raise_()
        win.activateWindow()

    def _reset_second_table_from_controls(self):
        """From/To/Step 입력값으로 array 테이블을 재생성한다 (테이블 창이 호출)."""
        arr = generate_array(self._current_cfg())
        if not arr:
            QMessageBox.warning(self, "Array 오류", "From/To/Step 으로 값을 만들 수 없습니다.")
            return
        self._second_table_model.reset_from(arr)
        self._refresh_second_table_win()

    def _refresh_second_table_win(self):
        win = self._second_table_win
        if win is not None:
            try:
                win.refresh()
            except Exception:
                pass

    def _set_table_win_running(self, running: bool):
        win = self._second_table_win
        if win is not None:
            try:
                win.set_running(running)
            except Exception:
                pass
        self._cb_keep_table.setEnabled(not running)

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def lock_ui(self, locked: bool) -> None:
        """측정 중 설정 UI 잠금/해제 (Start/Stop/Resume 버튼 제외)."""
        for frame in (self._second_frame, self._sweep_adv_frame, self._feedback_frame,
                      self._wait_frame, self._first_frame, self._targets_frame,
                      self._param_frame):
            frame.setEnabled(not locked)
        # array 구획은 잠그되, 테이블 창은 계속 열어 미래 행을 고칠 수 있게 둔다
        for widget in (self._le_arr_from, self._le_arr_to, self._le_arr_step,
                       self._le_settle_h, self._le_settle_m,
                       self._cb_skip_first_wait, self._cb_to_zero_at_last):
            widget.setEnabled(not locked)
        # 저장 설정은 잠그되, 경로 미리보기·폴더 열기 버튼은 계속 쓸 수 있게 둔다
        for widget in (self._le_main_folder, self._btn_browse, self._le_sub_folder,
                       self._le_file_name, self._cb_include_date, self._cb_save_enabled):
            widget.setEnabled(not locked)
        self._set_table_win_running(locked)

    def _lock_ui_for_run(self):
        """측정 중 구성 변경 차단 — 이 창과 메인 창 양쪽."""
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_resume.setEnabled(False)
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

    def _unlock_main_ui(self) -> None:
        """측정 종료 시 Main Window UI 복원."""
        self.lock_ui(False)
        main_win = self._main_win
        main_win._btn_start.setEnabled(True)
        main_win._sweep_channel_panel.setEnabled(True)
        main_win._meas_panel.setEnabled(True)
        main_win._set_save_inputs_enabled(True)
        for suffix in ("", "2", "3"):
            cb = getattr(main_win, f"_cb_deriv{suffix}_enable")
            cb.setEnabled(True)
            for widget in getattr(main_win, f"_deriv{suffix}_setting_widgets"):
                widget.setEnabled(cb.isChecked())
        if main_win._meta_data_window is not None:
            main_win._meta_data_window.lock_ui(False)

    def _on_start(self):
        if self._phase != Phase.IDLE:
            return
        if not self._check_start_preconditions():
            return
        if not self._main_win._run_connection_test(include_cycle2d=True,
                                                   show_success=False):
            return
        if not self._prepare_run():
            return
        self._main_win._log(
            f"Double Sweep+ started. {len(self._array)} second points × "
            f"{self._cfg.cycles} cycles × {len(self._targets)} segments. "
            f"Settle wait {_fmt_hms(self._cfg.settle_wait_s)}.", color="#4ec9b0")
        self._begin_array_index(0)

    def _check_start_preconditions(self) -> bool:
        """시작 전 확인. 하나라도 걸리면 안내하고 False."""
        main_win = self._main_win
        if main_win._running:
            QMessageBox.warning(self, "Sweep 실행 중",
                                "Main sweep 이 실행 중입니다. 먼저 중단하세요.")
            return False
        if self._first_channel is None:
            QMessageBox.warning(
                self, "No First Channel",
                "cycle 을 돌릴 First Channel 을 선택하세요.\n"
                "목록이 비어 있으면 Parameter Manager 에 Sweep Value 를 등록하세요.")
            return False
        if self._second_channel is None:
            QMessageBox.warning(
                self, "No Second Channel",
                "Second Channel 을 선택하세요.\n"
                "목록이 비어 있으면 장비 라디오(ITC/IPS/전체)를 바꿔 보거나, "
                "Parameter Manager 에 Second Sweep Channel 을 등록하세요.")
            return False

        targets = self._collect_targets()
        if targets is None:
            QMessageBox.warning(self, "Targets 오류",
                                "Cycle Targets 에 숫자로 읽을 수 없는 값이 있습니다.")
            return False
        if not targets:
            QMessageBox.warning(self, "Targets 오류",
                                "Cycle Targets 에 목표값을 하나 이상 입력하세요.\n"
                                "[+ target 추가] 로 칸을 늘릴 수 있습니다.")
            return False

        rate = self._parse_float(self._le_rate.text(), 0.0)
        tpp = self._parse_float(self._le_tpp.text(), 0.0)
        if rate <= 0 or tpp <= 0:
            QMessageBox.warning(
                self, "잘못된 Sweep 파라미터",
                f"Rate({rate:g})와 Time/Point({tpp:g})는 0보다 커야 합니다.")
            return False

        if self._cb_save_enabled.isChecked() and not self._le_main_folder.text().strip():
            QMessageBox.warning(
                self, "저장 폴더 미지정",
                "Save Settings 의 Main Folder 가 비어 있습니다.\n"
                "폴더를 지정하거나 '파일로 저장'을 끄세요.")
            return False

        if not self._cb_save_enabled.isChecked():
            answer = QMessageBox.question(
                self, "저장 꺼짐",
                "Save Settings 의 '파일로 저장'이 꺼져 있습니다.\n"
                "측정 데이터가 파일로 저장되지 않습니다.\n\n"
                "저장 없이 진행하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        return True

    def _prepare_run(self) -> bool:
        """측정 실행 준비 — _on_start(신규)와 _resume_from_point(재개) 공통 셋업."""
        self._save_config()
        self._cfg = self._current_cfg()
        self._targets = list(self._cfg.targets)
        if not self._targets:
            return False
        if not self._prepare_second_array():
            return False

        self._reset_run_state()
        self._prepare_derivative_channels()
        self._ctx = self._build_context()
        self._configure_metadata()

        self._lock_ui_for_run()
        self._sync_data_window_columns()
        self._update_graph_map_base()
        return True

    def _prepare_second_array(self) -> bool:
        """second 채널이 훑을 값 목록을 확정한다.

        테이블 모델이 source of truth 다 — '테이블 초기화 안 함'이 켜져 있고 모델에
        값이 있으면 사용자가 직접 넣은 목록을 그대로 쓰고, 아니면 From/To/Step 으로
        새로 만든다.
        """
        keep = (self._cb_keep_table.isChecked()
                and self._second_table_model.count() > 0)
        if keep:
            self._second_table_model.rearm()   # 값은 두고 상태만 PENDING 으로
        else:
            self._second_table_model.reset_from(generate_array(self._cfg))

        self._array = self._second_table_model.values()
        if not self._array:
            QMessageBox.warning(self, "Array 오류",
                                "Array 생성 실패: 포인트 수가 0입니다.")
            return False
        return True

    def _reset_run_state(self):
        """이전 측정의 잔여 상태를 지운다."""
        self._array_idx = 0
        self._cycle_idx = 0
        self._seg_idx = 0
        self._seg_phase_name = ""
        self._last_write_value = None
        self._cycle_filepath = None
        self._auto_retry_used = False
        self._last_emit = None
        self._retry_timer.stop()
        self._settle_timer.stop()
        self._lbl_countdown.setText("")
        self._btn_resume.setEnabled(False)

    def _prepare_derivative_channels(self):
        """미분 채널을 현재 UI 설정으로 다시 만들고 버퍼를 비운다 (단일 sweep 과 동일)."""
        main_win = self._main_win
        for order, (channel, _key) in enumerate(main_win._deriv_channels(), start=1):
            channel.reconfigure(main_win._build_deriv_config(order))
            channel.reset()

    def _build_context(self) -> CycleDoubleSweepContext:
        """시작 시점의 MainWindow 상태 + 이 창이 고른 채널을 스냅샷한다."""
        main_win = self._main_win
        profile = main_win._active_profile

        active_meas_indices = [
            i for i, (_, cb) in enumerate(
                zip(profile.measurements, main_win._meas_checkboxes))
            if cb.isChecked()
        ]
        active_measurements = [
            (row, m.alias, m.description, m.resolved_cmd)
            for row, m in enumerate(profile.measurements)
            if row in active_meas_indices
        ]
        meas_cols = [
            (main_win._meas_label_for(row, m), m.unit)
            for row, m in enumerate(profile.measurements)
            if row in active_meas_indices
        ]

        # sweep 열 헤더 + safety 파라미터는 이 창이 고른 first 채널에서 가져온다
        sv_id = self._first_radio_group.checkedId()
        sweep_values = profile.sweep_values
        sv = sweep_values[sv_id] if 0 <= sv_id < len(sweep_values) else None
        sweep_col = (sv.figure_axis or "target", sv.unit) if sv else ("target", "")

        return CycleDoubleSweepContext(
            sweep_channel=self._first_channel,
            sv_safety_steps=sv.safety_steps if sv else 0,
            sv_safety_interval_ms=sv.safety_interval_ms if sv else 0.0,
            active_meas_indices=active_meas_indices,
            active_measurements=active_measurements,
            sweep_col=sweep_col,
            meas_cols=meas_cols,
            main_folder=self._le_main_folder.text(),
            sub_folder=self._le_sub_folder.text(),
            file_name=self._le_file_name.text(),
            include_date=self._cb_include_date.isChecked(),
            save_enabled=self._cb_save_enabled.isChecked(),
            first_alias=self._first_alias,
            targets=list(self._targets),
        )

    def _configure_metadata(self):
        """T/B 버퍼 설정 — cycle 마다 clear 하므로 통계는 cycle 단위가 된다."""
        labels = [label for label, _unit in self._ctx.meas_cols]
        self._main_win._meta_manager.configure(
            self._ctx.active_meas_indices,
            self._main_win._active_profile.measurements,
            labels,
        )

    def _sync_data_window_columns(self):
        """측정값을 메인 창의 Data 창에 표시하기 위한 열 구성."""
        ctx = self._ctx
        columns = [(ctx.sweep_col[0], ctx.sweep_col[1])]
        columns.extend(ctx.meas_cols)
        self._main_win._data_window.configure_columns(columns)
        self._main_win._data_window.clear_values()

    def _prepare_graph_session(self):
        """그래프 세션을 새로 연다 — second 값이 바뀔 때마다 새 세션을 시작한다.

        cycle 경계에서는 부르지 않는다. 같은 second 값의 cycle 들은 값이 이어지므로
        한 그래프에 겹쳐 보는 편이 hysteresis 확인에 낫다 (파일만 cycle 별로 나뉜다).
        """
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

        for channel, _key in main_win._deriv_channels():
            channel.reset()

    def _update_graph_map_base(self):
        """2D map 패널이 스캔할 최상위 폴더를 알려 준다 (이 창의 저장 설정 기준)."""
        main_win = self._main_win
        if main_win._graph_window is None:
            return
        base = self._le_main_folder.text().strip()
        if not base:
            return
        sub = self._le_sub_folder.text().strip()
        main_win._graph_window.update_map_base(f"{base}/{sub}" if sub else base)

    def _on_stop_clicked(self):
        """사용자가 Stop 을 누른 경우 — 현재 array 지점을 재개 로그에 남기고 중단."""
        if self._phase != Phase.IDLE and self._array:
            self._save_resume_point("사용자 중단(Stop)")
        self._on_stop()

    def _on_stop(self):
        if self._phase == Phase.IDLE:
            return
        self._sweep_timer.stop()
        self._retry_timer.stop()
        self._settle_timer.stop()
        self._sweep_worker.request_stop()
        self._second_worker.request_stop()
        self._set_phase(Phase.IDLE)
        self._lbl_countdown.setText("")
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._stop_glow()
        self.sweep_finished.emit()
        self._main_win._log("Double Sweep+ stopped.", color="#ce9178")
        self._unlock_main_ui()
        self._second_table_model.clear_running()
        self._refresh_second_table_win()
        self._set_table_win_running(False)
        self._update_resume_btn_enabled()

    def _finish(self):
        self._set_phase(Phase.IDLE)
        self._lbl_countdown.setText("")
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._stop_glow()
        self.sweep_finished.emit()
        cycles = self._cfg.cycles if self._cfg else 0
        self._main_win._log(
            f"Double Sweep+ complete. ({len(self._array)} second points × "
            f"{cycles} cycles)", color="#4ec9b0")
        self._unlock_main_ui()
        self._second_table_model.clear_running()
        self._refresh_second_table_win()
        self._set_table_win_running(False)
        self._update_resume_btn_enabled()

    # ------------------------------------------------------------------
    # 진행 — array → (pre-init → advance → settle → cycles)
    # ------------------------------------------------------------------

    def _set_phase(self, phase: Phase):
        self._phase = phase
        self._lbl_phase.setText(phase.name)

    def _begin_array_index(self, idx: int) -> bool:
        """array 의 idx 번째 second 값 처리를 시작한다.

        모델에서 최신 array 를 다시 읽는다 → 측정 중 편집/추가된 미래 행을 반영한다
        (완료 행은 앞쪽 prefix 로 고정이라 안전). 범위를 벗어나면 False.
        """
        self._array = self._second_table_model.values()
        if idx < 0 or idx >= len(self._array):
            return False
        self._array_idx = idx
        self._second_table_model.mark_current(idx)
        self._refresh_second_table_win()

        ch = self._second_channel
        unit = ch.unit if ch else ""
        self._lbl_second_val.setText(f"{self._array[idx]:.6g} {unit}")
        self._lbl_array_progress.setText(f"{idx + 1}/{len(self._array)}")
        self._lbl_cycle_progress.setText(f"—/{self._cfg.cycles}")
        self._lbl_seg_progress.setText(f"—/{len(self._targets)}")

        # 이 second 값의 그래프 세션을 새로 연다 (cycle 들은 한 세션에 겹쳐 그린다)
        self._prepare_graph_session()
        self._start_pre_init()
        return True

    def _start_pre_init(self):
        """first 채널을 Initial Value 로 옮긴다 — 데이터는 기록하지 않는다.

        second 를 바꾸기 전에 먼저 돌려놓는다: 온도/자기장이 움직이는 동안 시료에
        직전 cycle 의 마지막 target 값이 계속 걸려 있는 상황을 피한다.
        """
        self._set_phase(Phase.PRE_INIT)
        self._sweep_timer.start(0)

    def _handle_pre_init_step(self, result: StepResult):
        """초기값으로 이동하는 구간 — 데이터는 기록하지 않는다."""
        self._last_write_value = result.next_v
        if result.is_done:
            self._advance_second()
        else:
            self._sweep_timer.start(self._next_interval_ms(result))

    def _advance_second(self):
        """second 채널을 현재 array 값으로 옮긴다."""
        idx = self._array_idx
        next_val = self._array[idx]
        prev = self._array[idx - 1] if idx > 0 else None
        try:
            ch = self._make_effective_second_channel()
        except RuntimeError as exc:
            # 실행 중 채널 구성이 비었다 — 슬롯에서 예외를 던지지 말고 정상 중단한다
            self._main_win._log(f"  [DoubleSweep+] {exc}", color="#f44747")
            self._on_stop()
            QMessageBox.critical(self, "Second Channel 없음", str(exc))
            return

        self._set_phase(Phase.ADVANCING_SECOND)
        self._lbl_fb_metric.setText("—")
        self._lbl_fb_metric.setStyleSheet("color: #888888;")
        self._main_win._log(
            f"  [{idx + 1}/{len(self._array)}] Second → {next_val:.6g} {ch.unit} "
            f"({ch.advance_type.value})", color="#4ec9b0")

        # 새 second 값 — T/B 버퍼 초기화
        self._cycle_filepath = None
        self._main_win._meta_manager.clear()

        self._emit_advance(SecondChannelRequest(
            channel=ch,
            next_value=next_val,
            prev_value=prev,
            time_per_point=self._cfg.time_per_point,
        ))

    def _make_effective_second_channel(self) -> InstantiatedSecondSweepChannel:
        """프로파일의 second 채널에 이 창의 입력값을 덧씌운 사본.

        실행 중 second 채널이 사라진 경우(파라미터 매니저에서 비움 등) None 을
        역참조해 크래시하지 말고 명확한 오류로 끊는다.
        """
        ch = self._second_channel
        if ch is None:
            raise RuntimeError("Second sweep channel 이 없습니다 (구성이 비었거나 변경됨).")
        cfg = self._cfg
        if cfg is None:
            return ch
        at = ch.advance_type
        if at == SecondSweepAdvanceType.SWEEP:
            use_safety = cfg.second_use_safety
            return ch.model_copy(update={
                "sweep_rate": cfg.second_sweep_rate,
                "safety_steps": cfg.second_safety_steps if use_safety else 0,
                "safety_interval_ms": (cfg.second_safety_interval_ms
                                       if use_safety else 0.0),
            })
        if at == SecondSweepAdvanceType.FEEDBACK:
            read_cmd = cfg.second_feedback_read_cmd
            return ch.model_copy(update={
                "feedback_read_cmd":      read_cmd or ch.feedback_read_cmd,
                "feedback_poll_interval": cfg.second_feedback_poll_interval,
                "feedback_tolerance_pct": cfg.second_feedback_tolerance_pct,
                "feedback_std_window":    cfg.second_feedback_std_window,
                "feedback_noisefloor":    cfg.second_feedback_noisefloor,
                "feedback_std_threshold": cfg.second_feedback_std_threshold,
            })
        if at == SecondSweepAdvanceType.WAIT_FOR_TIME:
            return ch.model_copy(update={"wait_time": cfg.second_wait_time})
        if at == SecondSweepAdvanceType.THRESHOLD_TIME:
            read_cmd = cfg.second_feedback_read_cmd
            return ch.model_copy(update={
                "feedback_read_cmd":      read_cmd or ch.feedback_read_cmd,
                "feedback_poll_interval": cfg.second_feedback_poll_interval,
                "feedback_tolerance_pct": cfg.second_feedback_tolerance_pct,
                "feedback_noisefloor":    cfg.second_feedback_noisefloor,
                "wait_time":              cfg.second_wait_time,
            })
        return ch

    # ── Settle 대기 ─────────────────────────────────────────────────────

    def _start_settling(self):
        """second 설정 후 대기 — 남은 시간을 1초마다 갱신해 보여 준다."""
        wait_s = self._cfg.settle_wait_s
        if self._cfg.skip_first_wait and self._array_idx == 0:
            self._main_win._log("  첫 array 값 — Settle Wait 건너뜀.", color="#d7ba7d")
            self._begin_cycle(0)
            return
        if wait_s <= 0:
            self._begin_cycle(0)
            return

        self._set_phase(Phase.SETTLING)
        self._settle_deadline = time.monotonic() + wait_s
        self._main_win._log(
            f"  Settle wait {_fmt_hms(wait_s)} — "
            f"{(_datetime.now() + _timedelta(seconds=wait_s)).strftime('%H:%M:%S')} 에 "
            f"cycle 을 시작합니다.", color="#d7ba7d")
        self._on_settle_tick()
        self._settle_timer.start()

    def _on_settle_tick(self):
        """1초마다 남은 대기 시간을 갱신하고, 다 되면 cycle 을 시작한다."""
        if self._phase != Phase.SETTLING:
            self._settle_timer.stop()
            return
        remaining = self._settle_deadline - time.monotonic()
        if remaining <= 0:
            self._settle_timer.stop()
            self._lbl_countdown.setText("")
            self._begin_cycle(0)
            return
        self._lbl_countdown.setText(f"대기 {_fmt_hms(remaining)} 남음")

    # ── Cycle 진행 ──────────────────────────────────────────────────────

    def _begin_cycle(self, cycle_idx: int):
        """cycle 하나를 시작한다 — 새 파일 + 초기점 측정 + 첫 구간."""
        self._cycle_idx = cycle_idx
        self._set_phase(Phase.CYCLING)
        self._lbl_cycle_progress.setText(f"{cycle_idx + 1}/{self._cfg.cycles}")

        # cycle 마다 T/B 버퍼를 비운다 → 메타 JSON 통계가 cycle 단위가 된다
        self._main_win._meta_manager.clear()
        if not self._setup_datasaver_for_cycle():
            return

        # 이동 없이 현재 위치에서 한 번 측정 → cycle 시작점이 파일 첫 행에 남는다
        self._seg_idx = 0
        self._update_segment_labels()
        ctx = self._ctx
        self._emit_step(StepRequest(
            sweep_channel=ctx.sweep_channel,
            sweep_to=0.0,
            sweep_rate=1.0,
            time_per_point=self._cfg.time_per_point,
            t_emit=time.perf_counter(),
            last_write_value=self._last_write_value,
            active_measurements=ctx.active_measurements,
            measure_only=True,
        ))

    def _begin_segment(self, seg_idx: int) -> bool:
        """다음 구간 시작. 남은 구간이 없으면 False."""
        if seg_idx < 0 or seg_idx >= len(self._targets):
            return False
        self._seg_idx = seg_idx
        target = self._targets[seg_idx]
        # 상승/하강을 그래프 곡선 색으로 구분한다 (graph_window 의 phase 스타일).
        if self._last_write_value is not None:
            self._seg_phase_name = "trace" if target >= self._last_write_value else "retrace"
        else:
            self._seg_phase_name = "trace"
        self._update_segment_labels()
        self._sweep_timer.start(0)
        return True

    def _update_segment_labels(self):
        self._lbl_seg_progress.setText(f"{self._seg_idx + 1}/{len(self._targets)}")

    def _advance_segment(self):
        """한 구간 완료 → 다음 구간, 없으면 cycle 마감."""
        if self._begin_segment(self._seg_idx + 1):
            return
        self._finish_cycle()

    def _finish_cycle(self):
        """cycle 하나 완료 — 메타 JSON 저장 후 다음 cycle 또는 다음 array 값."""
        main_win = self._main_win
        main_win._meta_manager.save(
            main_win._param_manager_reg.meta_data_config,
            self._cycle_filepath,
            extra=self._build_meta_extra(),
        )
        main_win._log(
            f"  [second {self._array_idx + 1}/{len(self._array)} · "
            f"cycle {self._cycle_idx + 1}/{self._cfg.cycles}] complete.",
            color="#4ec9b0")

        if self._cycle_idx + 1 < self._cfg.cycles:
            self._begin_cycle(self._cycle_idx + 1)
            return

        # 이 second 값의 모든 cycle 완료 → 다음 array 값
        self._second_table_model.mark_done(self._array_idx)
        self._refresh_second_table_win()
        if self._begin_array_index(self._array_idx + 1):
            return
        self._start_tail_sequence()

    def _start_tail_sequence(self):
        """모든 array 값 완료 후 마무리 — second → 0, first → 0 (옵션)."""
        if self._cfg.to_zero_at_last and self._second_channel is not None:
            self._return_second_to_zero()
            return
        self._start_first_return_or_finish()

    def _return_second_to_zero(self):
        """모든 array 값 완료 후 second channel 을 0 으로 보낸다."""
        ch = self._make_effective_second_channel()
        last_val = self._array[-1] if self._array else None
        self._set_phase(Phase.RETURNING_SECOND_ZERO)
        self._main_win._log("  Second → 0 (no data).", color="#d7ba7d")

        if ch.advance_type != SecondSweepAdvanceType.SWEEP:
            # SWEEP 외 타입은 도달을 기다릴 이유가 없다 — 0 을 한 번 write 하고 끝낸다
            ch = ch.model_copy(update={
                "advance_type": SecondSweepAdvanceType.SIMPLE_HOP})
        self._emit_advance(SecondChannelRequest(
            channel=ch,
            next_value=0.0,
            prev_value=last_val,
            time_per_point=self._cfg.time_per_point,
        ))

    def _start_first_return_or_finish(self):
        """first 채널 0 복귀(옵션) 후 종료."""
        if not self._cfg.return_to_zero:
            self._finish()
            return
        self._set_phase(Phase.RETURNING_ZERO)
        self._main_win._log("  First → 0 (no data).", color="#d7ba7d")
        self._sweep_timer.start(0)

    def _handle_return_step(self, result: StepResult):
        """0 복귀 구간 — 데이터는 기록하지 않는다."""
        self._last_write_value = result.next_v
        if result.is_done:
            self._finish()
        else:
            self._sweep_timer.start(self._next_interval_ms(result))

    def _build_meta_extra(self) -> dict:
        """메타 데이터 JSON 에 기록할 second/cycle 정보."""
        cfg = self._cfg
        ch = self._second_channel
        second_value = (self._array[self._array_idx]
                        if self._array and self._array_idx < len(self._array) else None)
        info = {
            "second_channel": {
                "alias": ch.alias if ch else "",
                "description": ch.description if ch else "",
                "advance_type": ch.advance_type.value if ch else "",
                "unit": ch.unit if ch else "",
                "value": second_value,
                "index": self._array_idx + 1,
                "count": len(self._array),
                "settle_wait_s": cfg.settle_wait_s if cfg else 0.0,
            },
            "cycle_sweep": {
                "alias": self._ctx.first_alias if self._ctx else "",
                "cycle_index": self._cycle_idx + 1,
                "cycles": cfg.cycles if cfg else 0,
                "initial_value": cfg.initial_value if cfg else 0.0,
                "targets": list(self._targets),
                "sweep_rate": cfg.sweep_rate if cfg else 0.0,
                "time_per_point": cfg.time_per_point if cfg else 0.0,
                "unit": self._ctx.sweep_col[1] if self._ctx else "",
            },
        }
        return info

    # ------------------------------------------------------------------
    # DataSaver
    # ------------------------------------------------------------------

    def _setup_datasaver_for_cycle(self) -> bool:
        """(second 값, cycle) 하나에 해당하는 .dat 세션을 시작한다.

        반환: 측정을 계속해도 되는지 여부.
          - 저장이 의도적으로 비활성: True (저장 없이 진행)
          - 저장 활성 + 성공: True
          - 저장 활성 + 실패: False (경고 후 측정 중단 — 데이터 유실 방지)
        """
        ctx = self._ctx
        cycle_no = self._cycle_idx + 1
        second_value = self._array[self._array_idx]
        fixed_name = self._step_file_name(second_value, cycle_no,
                                          self._second_fig_axis(),
                                          ctx.file_name, ctx.include_date)

        self._data_saver.set_main_folder(ctx.main_folder)
        self._data_saver.set_custom_folder(ctx.sub_folder)
        self._data_saver.set_custom_word(ctx.file_name)
        self._data_saver.set_include_date(ctx.include_date)
        self._data_saver.set_enabled(ctx.save_enabled)
        self._data_saver.set_fixed_name(fixed_name)
        deriv_cols = [
            (channel.col_info()[1], channel.col_info()[2])
            for channel, _key in self._main_win._deriv_channels()
            if channel._cfg.enabled
        ]
        self._data_saver.set_columns([ctx.sweep_col] + ctx.meas_cols + deriv_cols)

        tag = f"second {second_value:.6g} · cycle {cycle_no}"
        filepath = self._data_saver.start_session()
        if filepath:
            self._main_win._log(f"  [{tag}] Data -> {filepath}", color="#888888")
        elif self._data_saver.start_error() is not None:
            err = self._data_saver.start_error()
            self._main_win._log(
                f"  [{tag}] 데이터 저장 시작 실패 — 측정 중단: {err}", color="#f44747")
            self._on_stop()
            QMessageBox.critical(
                self, "데이터 저장 실패 — 측정 중단",
                f"[{tag}] 의 데이터 파일을 시작할 수 없어 측정을 중단했습니다.\n\n"
                f"사유: {err}\n\n"
                "데이터 유실을 막기 위해 저장이 정상화될 때까지 측정하지 않습니다.\n"
                "Main Folder 경로·권한·디스크 공간을 확인하세요.",
            )
            return False
        self._cycle_filepath = self._data_saver.get_filepath()
        self._update_save_path()
        return True

    # ------------------------------------------------------------------
    # Sweep tick / step 처리
    # ------------------------------------------------------------------

    def _emit_step(self, req: StepRequest):
        """워커에 스텝 요청. 자동 재개 때 그대로 다시 보내기 위해 기억해 둔다."""
        self._last_emit = ("step", req)
        self.request_step.emit(req)

    def _emit_advance(self, req: SecondChannelRequest):
        self._last_emit = ("advance", req)
        self.request_advance.emit(req)

    def _sweep_tick(self):
        if self._phase not in (Phase.PRE_INIT, Phase.CYCLING, Phase.RETURNING_ZERO):
            return
        cfg = self._cfg
        ctx = self._ctx
        if self._phase == Phase.PRE_INIT:
            sweep_to = cfg.initial_value
            active_meas = []                       # 초기값 이동 구간은 데이터 없음
        elif self._phase == Phase.RETURNING_ZERO:
            sweep_to = 0.0
            active_meas = []                       # 복귀 구간은 데이터 없음
        else:
            sweep_to = self._targets[self._seg_idx]
            active_meas = ctx.active_measurements

        self._emit_step(StepRequest(
            sweep_channel=ctx.sweep_channel,
            sweep_to=sweep_to,
            sweep_rate=cfg.sweep_rate,             # cycle 전체 공통 rate
            time_per_point=cfg.time_per_point,
            t_emit=time.perf_counter(),
            last_write_value=self._last_write_value,
            safety_steps=ctx.sv_safety_steps,
            safety_interval_ms=ctx.sv_safety_interval_ms,
            active_measurements=active_meas,
        ))

    @Slot(object)
    def _on_step_done(self, result: StepResult):
        """워커 스텝 완료 → 기록·표시 후 다음 스텝 또는 다음 구간으로 넘어간다.

        MainWindow._on_step_done 과 같은 순서를 따른다: 파일 기록 → 메타데이터 →
        그래프. 기록에 실패하면 뒤 단계로 가지 않고 측정을 멈춘다.
        """
        if self._phase == Phase.PRE_INIT:
            self._handle_pre_init_step(result)
            return
        if self._phase == Phase.RETURNING_ZERO:
            self._handle_return_step(result)
            return
        if self._phase != Phase.CYCLING:
            return
        # is_done 인데 측정값이 없다 = 이미 목표에 있었다 → 기록 없이 다음 구간
        if result.is_done and not result.meas_results:
            self._advance_segment()
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
        main_win._timing_window.update_timing(result.timing, t_recv,
                                              time.perf_counter())
        self._schedule_next_step(result)

    def _next_interval_ms(self, result: StepResult) -> int:
        """이번 스텝 처리에 쓴 시간을 빼서 time_per_point 주기를 맞춘다."""
        elapsed_ms = int((time.perf_counter() - result.timing.t_emit) * 1000)
        return max(0, int(self._cfg.time_per_point * 1000) - elapsed_ms)

    def _schedule_next_step(self, result: StepResult):
        if result.is_done:
            self._advance_segment()
        elif result.measure_only:
            self._begin_segment(0)       # 초기점 측정 완료 → cycle 의 첫 구간 시작
        else:
            self._sweep_timer.start(self._next_interval_ms(result))

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
        """통신 오류면 자동 재개 경로로, 그 외(파싱 등)는 즉시 중단."""
        descs = []
        for idx in failed:
            desc = self._measurement_description(idx)
            detail = result.meas_errors.get(idx, "")
            descs.append(f"{desc}: {detail}" if detail else desc)

        err_msg = (f"ERR @ second {self._array_idx + 1} "
                   f"cycle {self._cycle_idx + 1} seg {self._seg_idx + 1}\n"
                   f"실패 채널: {', '.join(descs)}")
        self._main_win._log(f"  [DoubleSweep+] {err_msg}", color="#f44747")

        if any(is_comm_error(result.meas_errors.get(idx, "")) for idx in failed):
            self._handle_comm_error("; ".join(descs))
            return

        self._on_stop()
        QMessageBox.critical(self, "Measurement Error", err_msg)

    def _push_derivatives(self, result: StepResult, meas_map: dict) -> list:
        """1·2·3차 미분값 계산. 미분 채널은 MainWindow 가 들고 있다."""
        main_win = self._main_win
        return [main_win._deriv_val_for_order(result, meas_map, channel)
                for channel, _key in main_win._deriv_channels()]

    def _derivative_row_cells(self, deriv_vals: list) -> list:
        """활성화된 미분 채널만 저장 행에 덧붙인다."""
        return [f"{val:.6g}" if val is not None else "—"
                for (channel, _key), val in zip(self._main_win._deriv_channels(), deriv_vals)
                if channel._cfg.enabled]

    def _record_row(self, row_vals: list) -> bool:
        """.dat 에 한 줄 기록. 저장이 켜져 있는데 실패하면 측정을 멈추고 False."""
        if self._data_saver.append_row(row_vals) or not self._data_saver.is_enabled():
            return True
        self._main_win._log(
            "  [DoubleSweep+] 데이터 기록 실패 — 측정 중단 (디스크/권한 확인).",
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

        읽기 실패한 채널은 nan 으로 넣는다 — 키를 빼면 곡선이 밀린다.
        """
        try:
            main_win = self._main_win
            values = {"__sweep__": result.next_v}
            for row, _alias, desc, _cmd in self._ctx.active_measurements:
                val = meas_map.get(row)
                values[desc] = val if val is not None else float("nan")
            for (channel, key), val in zip(main_win._deriv_channels(), deriv_vals):
                if channel._cfg.enabled:
                    values[key] = val if val is not None else float("nan")

            point = GraphDataPoint(values=values, phase=self._seg_phase_name)
            main_win._graph_history.append(point)
            if main_win._graph_window is not None:
                main_win._graph_window.append_point(point)
        except Exception as exc:
            self._main_win._log(f"  [Graph] append_point failed: {exc}", color="#f44747")

    @Slot(str)
    def _on_step_error(self, msg: str):
        self._main_win._log(
            f"  [DoubleSweep+] Step error — {humanize_error(msg)}  [상세] {msg}",
            color="#f44747")
        if is_comm_error(msg):
            self._handle_comm_error(f"Step error: {msg}")
        else:
            self._on_stop()

    # ------------------------------------------------------------------
    # Second channel 워커 응답
    # ------------------------------------------------------------------

    @Slot()
    def _on_advance_done(self):
        self._lbl_fb_metric.setText("—")
        self._lbl_fb_metric.setStyleSheet("color: #888888;")
        if self._phase == Phase.RETURNING_SECOND_ZERO:
            self._start_first_return_or_finish()
            return
        if self._phase != Phase.ADVANCING_SECOND:
            return
        self._auto_retry_used = False      # 정상 advance — 자동 재개 예산 리셋
        self._start_settling()

    @Slot(str)
    def _on_advance_error(self, msg: str):
        self._main_win._log(
            f"  [DoubleSweep+] Second channel error — {humanize_error(msg)}  [상세] {msg}",
            color="#f44747")
        if is_comm_error(msg):
            self._handle_comm_error(f"Second channel error: {msg}")
        else:
            self._on_stop()

    @Slot(str)
    def _on_advance_timeout(self, msg: str):
        """second advance 워치독 타임아웃 — 자동 재개 없이 즉시 중지.

        threshold 미도달(20분, 재전송 1회 후)·feedback 안정화 지연(5분) 모두 여기로 온다.
        comm 오류와 달리 재시도하지 않는다 (이미 워커가 재전송을 시도했다).
        """
        self._main_win._log(
            f"  [DoubleSweep+] Second channel 타임아웃 — 측정 중지: {msg}", color="#f44747")
        if self._phase != Phase.IDLE:
            self._save_resume_point(f"Second timeout: {msg}")
        self._on_stop()
        QMessageBox.warning(
            self, "Second channel 타임아웃 — 측정 중단",
            f"second channel 이 목표에 도달하지 못해 측정을 중단했습니다.\n\n{msg}\n\n"
            "현재 array 지점이 재개 로그에 저장되었습니다.")

    @Slot(str)
    def _on_second_status(self, msg: str) -> None:
        """second 워커 진행 메시지를 콘솔에 기록 (메인 스레드 보장용 bound 슬롯)."""
        self._main_win._log(f"  [DoubleSweep+] {msg}", color="#d7ba7d")

    @Slot(float)
    def _on_feedback_metric(self, metric: float) -> None:
        """Phase 2 metric 값을 실시간으로 Std Threshold 옆 레이블에 표시."""
        threshold = self._parse_float(self._le_fb_std_thresh.text(), 0.01)
        if metric >= 1e30:
            self._lbl_fb_metric.setText("∞")
            self._lbl_fb_metric.setStyleSheet("color: #f44747;")
        else:
            color = "#4ec9b0" if metric < threshold else "#f78166"
            self._lbl_fb_metric.setText(f"{metric:.4g}")
            self._lbl_fb_metric.setStyleSheet(f"color: {color};")

    # ------------------------------------------------------------------
    # 통신 오류 자동 재개 / 수동 재개
    # ------------------------------------------------------------------

    _AUTO_RESUME_DELAY_MS = 10_000   # 통신 오류 후 자동 재개 대기 (10초)

    def _handle_comm_error(self, reason: str):
        """통신 오류: 1회는 10초 후 자동 재개, 재차 발생 시 중단 + 재개 지점 저장."""
        if self._phase == Phase.IDLE:
            return
        if not self._auto_retry_used:
            self._auto_retry_used = True
            self._sweep_timer.stop()
            self._main_win._log(
                f"  [DoubleSweep+] 통신 오류 — {self._AUTO_RESUME_DELAY_MS // 1000}초 후 "
                f"자동 재개합니다.  ({reason})",
                color="#d7ba7d")
            self._retry_timer.start(self._AUTO_RESUME_DELAY_MS)
            return

        self._save_resume_point(reason)
        cause = humanize_error(reason)
        self._main_win._log(
            "  [DoubleSweep+] 자동 재개 후 재차 통신 오류 — 측정 중단. "
            "[Resume] 버튼으로 저장된 지점부터 재개하세요.",
            color="#f44747")
        self._main_win._log(f"     원인: {cause}", color="#f44747")
        self._on_stop()
        QMessageBox.warning(
            self, "통신 오류 — Double Sweep+ 중단",
            "자동 재개 후에도 통신 오류가 반복되어 측정을 중단했습니다.\n\n"
            f"원인: {cause}\n\n"
            "현재 array 지점이 재개 로그에 저장되었습니다.\n"
            "[Resume] 버튼으로 그 지점부터 다시 시작할 수 있습니다.",
        )

    def _auto_resume(self):
        """10초 경과 후 마지막 요청을 재전송하여 측정을 이어간다."""
        if self._phase == Phase.IDLE or self._last_emit is None:
            return
        kind, req = self._last_emit
        self._main_win._log("  [DoubleSweep+] 자동 재개 — 측정을 재시작합니다.",
                            color="#4ec9b0")
        if kind == "step":
            self.request_step.emit(req)
        else:
            self.request_advance.emit(req)

    def _save_resume_point(self, reason: str):
        """현재 array 위치를 재개 로그에 저장 (재개 단위는 array 값)."""
        second_val = (self._array[self._array_idx]
                      if self._array and self._array_idx < len(self._array) else None)
        unit = self._second_channel.unit if self._second_channel else ""
        cycles = self._cfg.cycles if self._cfg else 0
        where = (f"2nd={second_val:.6g}{unit}" if second_val is not None else "2nd=?")
        label = (f"array {self._array_idx + 1}/{len(self._array)}  {where}  "
                 f"cycle {self._cycle_idx + 1}/{cycles}  phase={self._phase.name}  "
                 f"({reason[:40]})")
        self._resume_log.add(ResumePoint(
            sweep_type=_RESUME_TYPE,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            label=label,
            data_filepath="",   # cycle 파일은 재개 시 새로 만든다
            payload={
                "array_idx": self._array_idx,
                "cycle_idx": self._cycle_idx,
                "second_value": second_val,
            },
        ))
        self._update_resume_btn_enabled()

    def _update_resume_btn_enabled(self):
        """재개 로그에 이 모듈의 지점이 있고 IDLE 이면 Resume 활성화."""
        if not hasattr(self, "_btn_resume"):
            return
        has_point = self._resume_log.latest(_RESUME_TYPE) is not None
        self._btn_resume.setEnabled(has_point and self._phase == Phase.IDLE)

    def _on_resume_clicked(self):
        """[Resume] 버튼: 저장된 지점 목록에서 선택 후 해당 array 값부터 재개."""
        if self._phase != Phase.IDLE:
            return
        dlg = ResumePickerDialog(self._resume_log.all(), parent=self,
                                 sweep_type=_RESUME_TYPE)
        if dlg.exec() and dlg.selected_point is not None:
            self._resume_from_point(dlg.selected_point)

    def _resume_from_point(self, point):
        """선택된 array 값부터 다시 시작한다.

        재개 단위는 array 값이다 — 그 값의 second 설정·Settle Wait·cycle 전부를
        처음부터 다시 하고, cycle 파일도 새로 쓴다. (중단 시점의 물리 상태를 알 수
        없으므로 cycle 중간부터 이어붙이면 데이터가 어긋난다.)
        """
        if not self._check_start_preconditions():
            return
        if not self._main_win._run_connection_test(include_cycle2d=True,
                                                   show_success=False):
            return
        if not self._prepare_run():
            return
        idx = int(point.payload.get("array_idx", 0))
        idx = max(0, min(idx, len(self._array) - 1))
        # 재개 지점 이전 행을 DONE 으로 표시 (테이블 색상·잠금 일관성)
        self._second_table_model.reset_from(self._array, done_prefix=idx)
        self._main_win._log(
            f"  [DoubleSweep+] 수동 재개 — array {idx + 1}/{len(self._array)} 부터 재시작.",
            color="#4ec9b0")
        self._begin_array_index(idx)

    # ------------------------------------------------------------------
    # Glow animation
    # ------------------------------------------------------------------

    def _update_glow(self):
        self._glow_phase += 0.07
        intensity = (math.sin(self._glow_phase) + 1) / 2
        alpha = int(80 + intensity * 140)
        green = int(140 + intensity * 80)
        self._glow_frame.setStyleSheet(
            f"QFrame#cdsGlowFrame {{"
            f"border: 3px solid rgba(40, {green}, 70, {alpha});"
            f"border-radius: 6px; }}"
        )

    def _stop_glow(self):
        self._glow_timer.stop()
        self._glow_frame.setStyleSheet(
            "QFrame#cdsGlowFrame { border: 3px solid transparent; border-radius: 6px; }"
        )

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def showEvent(self, event):
        # closeEvent 에서 워커 스레드를 종료했을 수 있다 — 필요하면 다시 켠다.
        if not self._worker_thread.isRunning():
            self._worker_thread.start()
        if not self._second_thread.isRunning():
            self._second_thread.start()
        # 실행 중에는 라디오를 재빌드하지 않는다 — 상태머신이 채널 객체를 라이브로
        # 읽는데, 재빌드가 이를 None 으로 만들면 다음 스텝에서 깨진다.
        if self._phase == Phase.IDLE:
            self._rebuild_first_channel_radios()
            self._rebuild_second_channel_radios()
            self._btn_start.setEnabled(not self._main_win._running)
        self._update_unit_labels()
        self._update_save_path()
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
        if self._phase != Phase.IDLE:
            reply = QMessageBox.question(
                self, "종료 확인",
                "Double Sweep+ 가 실행 중입니다. 중단하시겠습니까?",
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
