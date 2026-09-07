"""
CycleSweepWindow: Cycle Sweep 전용 창.

측정을 시작하면 먼저 2636A 의 현재값에서 Initial Value 까지 이동한 뒤(데이터 없음),
거기서부터 target 값들을 순서대로 훑는다. 이 한 바퀴가 cycle 이다.
  예) initial = 0 V, targets = [30, -30, 0] 이면
      0→30 V, 30→-30 V, -30→0 V 세 구간이 한 cycle 이다.
Sweep rate 와 Time/Point 는 cycle 전체에 공통으로 적용되고, Cycles 로 지정한
횟수만큼 같은 cycle 을 반복한다. 2회차부터는 직전 cycle 의 마지막 target 에서
이어서 시작하므로 값이 끊기지 않는다.

Sweep channel 은 Keithley 2636A 로 등록된 항목만 고를 수 있다 (§_is_keithley_2636a).
측정 항목과 미분 채널은 메인 창 설정을 시작 시점에 스냅샷해서 쓰고(Double Sweep 과
같은 방식), 저장 폴더·파일명은 이 창의 Save Settings 구획이 따로 갖는다.

실행 흐름:
  IDLE
  → PRE_INIT           현재값 → Initial Value (데이터 없음)
  → SWEEPING  cycle 1 : 초기점 측정 → seg 1 → seg 2 → … → cycle 파일 메타 저장
  → SWEEPING  cycle 2 : …
  → RETURNING_ZERO     (Return to zero 체크 시에만. 데이터 기록 없음)
  → IDLE
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

from pythonization.config.models import CycleSweepConfig
from pythonization.instruments.errors import humanize_error, is_comm_error
from pythonization.instruments.factory import resolve_class_path
from pythonization.measurement.channel import sweep_channel_from_instantiated
from pythonization.measurement.data_saver import DataSaver
from pythonization.measurement.resume_log import ResumeLog, ResumePoint
from pythonization.measurement.sweep_worker import StepRequest, StepResult, SweepWorker
from pythonization.ui.dialogs.resume import ResumePickerDialog
from pythonization.ui.panels.graph_window import GraphDataPoint
from pythonization.ui.widgets.help_button import make_help_button

if TYPE_CHECKING:
    from pythonization.ui.main_window import MainWindow

_MONO = QFont("Consolas", 10)

#: Keithley 2636A 드라이버 클래스 이름. 저장된 class_name 은 구 레이아웃일 수 있으므로
#: resolve_class_path 로 옮긴 뒤 마지막 조각(클래스명)만 비교한다 —
#: 드라이버 파일이 다른 패키지로 옮겨져도 이 판정은 살아남는다.
_K2636_CLASS = "Keithley2636A"


def _is_keithley_2636a(inst_registry, alias: str) -> bool:
    """alias 가 Keithley 2636A 로 등록돼 있는지.

    config 계층에는 모델 문자열이 없고 드라이버 클래스 경로가 유일한 정적 단서다.
    (Instrument Settings 에서 드라이버를 잘못 고르면 여기서 걸러진다.)
    """
    cfg = inst_registry.get_config(alias)
    if cfg is None:
        return False
    return resolve_class_path(cfg.class_name).rpartition(".")[2] == _K2636_CLASS


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


def estimate_total_seconds(cfg: CycleSweepConfig, targets: List[float]) -> float:
    """예상 소요 시간(초).

    이동 시간 = 거리 * 60 / rate (Time/Point 는 측정 간격일 뿐 이동 속도가 아니다).
    cycle 1 은 initial_value 에서, 2회차부터는 직전 cycle 의 마지막 target 에서
    시작한다. 시작 직전의 '현재값 → initial_value' 구간은 현재값을 알 수 없어
    빠져 있다.
    """
    if not targets or cfg.sweep_rate <= 0 or cfg.cycles <= 0:
        return 0.0

    def _chain(start: float) -> float:
        total = 0.0
        prev = start
        for target in targets:
            total += abs(target - prev) * 60.0 / cfg.sweep_rate
            prev = target
        return total

    seconds = _chain(cfg.initial_value) + (cfg.cycles - 1) * _chain(targets[-1])
    if cfg.return_to_zero:
        seconds += abs(targets[-1]) * 60.0 / cfg.sweep_rate
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
class CycleSweepContext:
    """Cycle Sweep 시작 시 MainWindow 에서 스냅샷한 실행 컨텍스트.

    측정 도중 메인 창 위젯을 다시 읽지 않기 위해 시작 시점의 값을 고정한다.
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
    alias: str = ""
    targets: List[float] = field(default_factory=list)


class CycleSweepPhase(Enum):
    IDLE           = auto()
    PRE_INIT       = auto()   # cycle 시작 전 initial value 로 이동 (데이터 없음)
    SWEEPING       = auto()   # cycle 안의 한 구간 진행 중
    RETURNING_ZERO = auto()   # 모든 cycle 종료 후 0 복귀 (데이터 없음)


class CycleSweepWindow(QDialog):
    """
    Cycle Sweep 창.

    - main_win 의 measurement 체크·저장 설정·미분 채널을 읽어 쓴다 (읽기 전용)
    - sweep channel 만 이 창이 직접 고른다 (2636A 로 필터)
    - 자체 SweepWorker + QThread + QTimer + DataSaver
    """

    # main_window 에 sweep lock 신호
    sweep_started  = Signal()
    sweep_finished = Signal()

    # 워커에 요청
    request_step = Signal(object)   # StepRequest → _sweep_worker

    def __init__(self, main_win: "MainWindow", param_reg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cycle Sweep")
        self.setMinimumWidth(760)
        self.resize(900, 800)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._main_win  = main_win
        self._param_reg = param_reg

        self._phase: CycleSweepPhase = CycleSweepPhase.IDLE
        self._targets: List[float] = []
        # (행 위젯, 값 입력칸, 번호 라벨, 단위 라벨) — '+' 로 늘리고 '−' 로 지운다
        self._target_rows: List[tuple] = []
        self._cycle_idx: int = 0
        self._seg_idx: int = 0
        self._pre_init_target: float = 0.0   # PRE_INIT 이 향하는 값
        self._pending_cycle_idx: int = 0     # PRE_INIT 이 끝나면 시작할 cycle
        self._seg_phase_name: str = ""      # 그래프 곡선 구분용 ("trace"/"retrace")
        self._channel = None                # 이 창이 고른 SweepChannel
        self._channel_alias: str = ""
        self._last_write_value: Optional[float] = None
        self._ctx: Optional[CycleSweepContext] = None
        self._cfg: Optional[CycleSweepConfig] = None
        self._cycle_filepath = None         # 현재 cycle 의 .dat 경로 (메타 JSON 기준)

        # 통신 오류 자동 재개 상태
        self._resume_log = ResumeLog()
        self._auto_retry_used = False
        self._last_step_request: Optional[StepRequest] = None
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._auto_resume)

        # DataSaver
        self._data_saver = DataSaver()
        self._data_saver.set_error_callback(
            lambda msg: main_win._log(f"  [CycleSweep DataSaver] {msg}", color="#f44747")
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
        self._rebuild_channel_radios()
        self._update_unit_labels()
        self._update_save_path()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        """창 전체 조립. 위에서 아래로 진행 순서대로 놓는다."""
        dialog_layout = QVBoxLayout(self)
        dialog_layout.setContentsMargins(0, 0, 0, 0)
        self._glow_frame = QFrame()
        self._glow_frame.setObjectName("csGlowFrame")
        self._glow_frame.setStyleSheet(
            "QFrame#csGlowFrame { border: 3px solid transparent; border-radius: 6px; }"
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
        outer.addWidget(self._build_channel_box())
        outer.addWidget(self._build_targets_box())
        outer.addWidget(self._build_cycle_params_box())
        outer.addWidget(self._build_save_box())
        outer.addLayout(self._build_estimate_row())
        outer.addWidget(self._build_status_box())
        outer.addLayout(self._build_action_row())
        outer.addStretch()

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

    def _build_title_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title = QLabel("Cycle Sweep")
        title.setStyleSheet("font-weight: bold; font-size: 13px; color: #f78166;")
        row.addWidget(title)
        row.addStretch()
        row.addWidget(make_help_button(self._cs_help_html(), "Cycle Sweep 도움말"))
        return row

    def _build_channel_box(self) -> QFrame:
        """sweep channel 선택 — Keithley 2636A 로 등록된 항목만 나열한다."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("Sweep Channel  (Keithley 2636A)")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #f78166;")
        layout.addWidget(title)

        self._channel_layout = QVBoxLayout()
        layout.addLayout(self._channel_layout)
        self._channel_radio_group = QButtonGroup(self)
        self._channel_radio_group.setExclusive(True)
        self._channel_radio_group.idToggled.connect(self._on_channel_radio_toggled)

        self._ch_frame = frame
        return frame

    def _build_targets_box(self) -> QFrame:
        """한 cycle 의 목표값 목록 — 한 줄에 하나, '+' 로 아래에 계속 추가한다."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("Cycle Targets")
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
        scroll.setMaximumHeight(220)
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

        lbl_unit = QLabel(self._channel_unit())
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

        title = QLabel("Cycle Parameters")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        layout.addLayout(form)

        self._le_initial, self._lbl_initial_unit, cnt_initial = self._make_value_field()
        self._le_initial.setToolTip(
            "측정을 시작하면 2636A 의 현재값에서 이 값까지 먼저 이동한 뒤\n"
            "cycle 을 시작합니다. 이 이동 구간은 데이터를 기록하지 않습니다.")
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
        self._sb_cycles.setToolTip("같은 cycle 을 몇 번 반복할지. cycle 마다 .dat 파일이 하나 생깁니다.")
        form.addRow("Cycles:", self._sb_cycles)

        self._cb_return_to_zero = QCheckBox("Return to zero")
        self._cb_return_to_zero.setFont(_MONO)
        self._cb_return_to_zero.setToolTip(
            "체크: 모든 cycle 이 끝난 뒤 같은 Sweep Rate 로 0 까지 되돌립니다 "
            "(이 구간은 데이터를 기록하지 않습니다).\n"
            "해제: 마지막 target 값에 그대로 둡니다.")
        form.addRow("", self._cb_return_to_zero)

        self._le_initial.textChanged.connect(self._on_cycle_inputs_changed)
        self._le_rate.textChanged.connect(self._update_est_time)
        self._le_tpp.textChanged.connect(self._update_est_time)
        self._sb_cycles.valueChanged.connect(self._update_est_time)
        self._cb_return_to_zero.stateChanged.connect(self._update_est_time)

        self._param_frame = frame
        return frame

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

    def _build_status_box(self) -> QFrame:
        """진행 상태 한 줄 — phase / cycle 진척 / 구간 진척 / 목표값."""
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
        layout.addWidget(QLabel("Cycle:"))
        self._lbl_cycle_progress = QLabel("—/—")
        self._lbl_cycle_progress.setFont(_MONO)
        self._lbl_cycle_progress.setStyleSheet("color: #79c0ff;")
        layout.addWidget(self._lbl_cycle_progress)

        layout.addSpacing(12)
        layout.addWidget(QLabel("Segment:"))
        self._lbl_seg_progress = QLabel("—/—")
        self._lbl_seg_progress.setFont(_MONO)
        self._lbl_seg_progress.setStyleSheet("color: #79c0ff;")
        layout.addWidget(self._lbl_seg_progress)

        layout.addSpacing(12)
        layout.addWidget(QLabel("Target:"))
        self._lbl_target_val = QLabel("—")
        self._lbl_target_val.setFont(_MONO)
        self._lbl_target_val.setStyleSheet("color: #f78166;")
        layout.addWidget(self._lbl_target_val)
        layout.addStretch()
        return frame

    def _build_action_row(self) -> QHBoxLayout:
        """Start / Stop / Resume."""
        self._btn_start = QPushButton("Start Cycle Sweep")
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
            "통신 오류/Stop 으로 중단된 Cycle Sweep 을 저장된 cycle 부터 다시 시작합니다.\n"
            "재개 단위는 cycle 이며, 그 cycle 의 파일은 새로 씁니다.")
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

    def _build_save_box(self) -> QFrame:
        """저장 위치·파일명 — 이 모듈이 직접 갖는다 (메인 창 설정과 별개)."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)

        header = QHBoxLayout()
        title = QLabel("Save Settings")
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
        self._le_sub_folder.setPlaceholderText("예: sampleA/cycle  (비우면 Main Folder 바로 아래)")
        form.addRow("Sub Folder:", self._le_sub_folder)

        self._le_file_name = QLineEdit()
        self._le_file_name.setFont(_MONO)
        self._le_file_name.setPlaceholderText("예: sampleA_10K")
        self._le_file_name.setToolTip(
            "파일명 앞부분입니다. 뒤에 cycle 번호가 자동으로 붙습니다.\n"
            "cycle 마다 파일이 하나씩 생기므로 이 번호는 뺄 수 없습니다.")
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

        for field in (self._le_main_folder, self._le_sub_folder, self._le_file_name):
            field.textChanged.connect(self._update_save_path)
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
        self._le_sub_folder.setText(f"{sub}/cycle" if sub else "cycle")
        self._le_file_name.setText(main_win._le_custom_word.text())
        self._cb_include_date.setChecked(main_win._cb_save_date.isChecked())
        self._cb_save_enabled.setChecked(main_win._cb_save_enable.isChecked())
        self._update_save_path()

    @staticmethod
    def _cs_help_html() -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>Cycle Sweep — 목표값 여러 개를 한 묶음으로 반복</b><hr>"
            "<b>Cycle Targets</b> 에 목표값을 <b>한 칸에 하나씩</b>, 위에서 아래 순서로 "
            "적습니다. <code>[+ target 추가]</code> 로 칸을 늘리고 <code>−</code> 로 지웁니다. "
            "빈 칸은 무시합니다.<hr>"
            "<b>Initial Value</b> — 시작하면 2636A 의 현재값에서 이 값까지 <b>먼저 이동</b>한 뒤 "
            "cycle 을 시작합니다. 이 이동 구간은 기록하지 않습니다.<br>"
            "초기값 0, targets <code>30 / -30 / 0</code> 이면<br>"
            "&nbsp;&nbsp;① 0→30 V &nbsp; ② 30→-30 V &nbsp; ③ -30→0 V &nbsp; = 한 cycle<br>"
            "2회차부터는 직전 cycle 의 마지막 target 에서 이어집니다.<hr>"
            "<b>Sweep Rate / Time / Point</b> 는 cycle 전체에 공통 적용됩니다.<br>"
            "한 스텝의 이동량 = rate/60 × Time/Point 이고, 측정은 매 스텝 write 직후 합니다.<hr>"
            "<b>Cycles</b> — 같은 cycle 을 몇 번 반복할지. <b>cycle 하나당 .dat 파일 하나</b>가 "
            "<code>…_cycle001.dat</code>, <code>…_cycle002.dat</code> 형식으로 생깁니다.<hr>"
            "<b>Save Settings</b> — 저장 폴더와 파일명은 <b>이 창에서 직접</b> 정합니다 "
            "(메인 창 설정과 별개). 아래 미리보기 줄이 실제로 만들어질 경로입니다.<br>"
            "<code>Main Folder / Sub Folder / [YYYY-MM-DD] / {File Name}{YYYYMMDD}_cycleNNN.dat</code><br>"
            "<code>Main 창 설정 가져오기</code> 로 메인 창 값을 한 번에 복사할 수 있습니다.<br>"
            "같은 이름·같은 날짜로 다시 돌리면 <b>기존 파일을 덮어씁니다</b> — File Name 을 바꾸세요.<hr>"
            "<b>Return to zero</b> — 모든 cycle 이 끝난 뒤 같은 rate 로 0 까지 되돌립니다. "
            "이 구간은 데이터를 기록하지 않습니다. 해제하면 마지막 target 값에 그대로 둡니다.<hr>"
            "<b>Sweep Channel</b> 은 Keithley 2636A 로 등록된 Paired Command 만 나옵니다.<br>"
            "목록이 비어 있으면 Instrument Settings 의 드라이버 지정과 Parameter Manager 를 확인하세요.<hr>"
            "&nbsp;• 측정 항목과 미분 채널은 <b>Main 화면 설정</b>을 그대로 씁니다 "
            "(저장 경로만 이 창 것이 쓰입니다).<br>"
            "&nbsp;• 통신 오류 시 10초 뒤 자동 재시도, 다시 실패하면 멈추고 그 cycle 을 저장 → "
            "<b>Resume</b> 로 재개.<br>"
            "&nbsp;• Stop 으로 멈춰도 그 지점이 저장되어 나중에 Resume 할 수 있습니다."
            "</body></html>"
        )

    # ------------------------------------------------------------------
    # Sweep channel radio buttons (2636A 만)
    # ------------------------------------------------------------------

    def _rebuild_channel_radios(self):
        """2636A 로 등록된 sweep value 만 라디오로 만든다.

        버튼 id 는 sweep_values 의 **원본 인덱스**를 그대로 쓴다 —
        selected_channel_idx 저장과 sweep_values[i] 조회가 이 값에 묶여 있다.
        """
        for btn in self._channel_radio_group.buttons():
            self._channel_radio_group.removeButton(btn)
        while self._channel_layout.count():
            item = self._channel_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        sweep_values = self._param_reg.main_ui_profile.sweep_values
        registry = self._main_win._registry
        indices = [i for i, sv in enumerate(sweep_values)
                   if _is_keithley_2636a(registry, sv.alias)]

        if not indices:
            lbl = QLabel("(Keithley 2636A 로 등록된 Paired Command 가 없습니다 — "
                         "Instrument Settings 의 드라이버 지정과 "
                         "Parameter Manager 를 확인하세요)")
            lbl.setStyleSheet("color: #555555; font-size: 11px;")
            lbl.setWordWrap(True)
            self._channel_layout.addWidget(lbl)
            self._channel = None
            self._channel_alias = ""
            return

        for idx in indices:
            sv = sweep_values[idx]
            rb = QRadioButton(f"[{sv.alias}]  {sv.description}  ({sv.unit})")
            rb.setFont(_MONO)
            rb.setStyleSheet("color: #f78166;")
            self._channel_radio_group.addButton(rb, idx)
            self._channel_layout.addWidget(rb)

        saved = self._param_reg.cycle_sweep_config.selected_channel_idx
        btn = self._channel_radio_group.button(saved)
        if btn is None:
            btn = self._channel_radio_group.button(indices[0])
        btn.setChecked(True)

    def _on_channel_radio_toggled(self, btn_id: int, checked: bool):
        if not checked:
            return
        sweep_values = self._param_reg.main_ui_profile.sweep_values
        if 0 <= btn_id < len(sweep_values):
            sv = sweep_values[btn_id]
            self._channel = sweep_channel_from_instantiated(sv)
            self._channel_alias = sv.alias
        else:
            self._channel = None
            self._channel_alias = ""
        self._update_unit_labels()

    def selected_alias(self) -> str:
        """이 창이 고른 sweep channel 의 alias (없으면 빈 문자열).

        main_window.collect_active_aliases 가 연결 테스트 대상에 넣기 위해 읽는다.
        """
        return self._channel_alias

    def is_idle(self) -> bool:
        """측정 중이 아닌지. main_window 가 UI 잠금 복원 여부를 판단할 때 쓴다."""
        return self._phase == CycleSweepPhase.IDLE

    def shutdown_threads(self):
        """앱 종료 시 워커 스레드를 정지·대기한다 (VNA/MFLI 창과 같은 규약)."""
        self._sweep_timer.stop()
        self._retry_timer.stop()
        self._sweep_worker.request_stop()
        if self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait()

    # ------------------------------------------------------------------
    # Config load / save
    # ------------------------------------------------------------------

    def _load_config(self):
        cfg = self._param_reg.cycle_sweep_config
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
        self._update_cycle_preview()
        self._update_est_time()
        self._update_save_path()

    def _save_config(self):
        self._param_reg.save_cycle_sweep_config(
            self._current_cfg(with_channel=True))

    def _current_cfg(self, with_channel: bool = False) -> CycleSweepConfig:
        """위젯 현재 값으로 만든 설정 객체.

        실행 중에는 위젯을 다시 읽지 않도록 시작 시점에 한 번 만들어 고정한다.
        """
        cfg = CycleSweepConfig(
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
        if with_channel:
            cfg.selected_channel_idx = max(0, self._channel_radio_group.checkedId())
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
        if self._phase != CycleSweepPhase.IDLE:
            return
        self._load_config()
        self._rebuild_channel_radios()
        self._update_unit_labels()
        self._update_save_path()

    # ------------------------------------------------------------------
    # 파생 표시 (단위·미리보기·예상 시간·저장 경로)
    # ------------------------------------------------------------------

    def _channel_unit(self) -> str:
        sweep_values = self._param_reg.main_ui_profile.sweep_values
        idx = self._channel_radio_group.checkedId()
        if 0 <= idx < len(sweep_values):
            return sweep_values[idx].unit
        return ""

    def _update_unit_labels(self):
        unit = self._channel_unit()
        self._lbl_initial_unit.setText(unit)
        self._lbl_rate_unit.setText(f"{unit}/min" if unit else "units/min")
        for _row, _edit, _no, lbl_unit in self._target_rows:
            lbl_unit.setText(unit)
        self._update_cycle_preview()

    def _on_cycle_inputs_changed(self):
        self._update_cycle_preview()
        self._update_est_time()

    def _update_cycle_preview(self):
        """'현재값 → (초기값) 0 → 30 → -30 → 0' 식으로 한 cycle 을 보여 준다."""
        targets = self._collect_targets()
        if targets is None:
            self._lbl_cycle_preview.setText("숫자로 읽을 수 없는 값이 있습니다.")
            self._lbl_cycle_preview.setStyleSheet("color: #f44747; font-size: 10px;")
            return
        self._lbl_cycle_preview.setStyleSheet("color: #888888; font-size: 10px;")
        if not targets:
            self._lbl_cycle_preview.setText("목표값을 하나 이상 입력하세요.")
            return
        unit = self._channel_unit()
        initial = self._parse_float(self._le_initial.text(), 0.0)
        chain = "  →  ".join(f"{v:g}{unit}" for v in targets)
        self._lbl_cycle_preview.setText(
            f"(현재값)  →  {initial:g}{unit}(초기값)  →  {chain}"
            f"     [{len(targets)} 구간 / cycle]")

    def _update_est_time(self):
        targets = self._collect_targets() or []
        seconds = estimate_total_seconds(self._current_cfg(), targets)
        self._lbl_est_time.setText(_fmt_hms(seconds))
        eta = _datetime.now() + _timedelta(seconds=seconds)
        if seconds < 86400:
            self._lbl_eta.setText(eta.strftime("%H:%M"))
        else:
            self._lbl_eta.setText(eta.strftime("%m/%d %H:%M"))

    @staticmethod
    def _cycle_file_name(cycle_no, file_name: str, include_date: bool) -> str:
        """cycle 번호가 들어간 파일명.

        cycle_no 가 정수면 실제 이름, 문자열이면 미리보기용 자리표시자로 쓴다.
        앞부분(file_name·날짜)이 전부 비면 'cycle001.dat' 처럼 밑줄 없이 만든다.
        """
        stem_parts = []
        if file_name.strip():
            stem_parts.append(file_name.strip())
        if include_date:
            stem_parts.append(_date.today().strftime("%Y%m%d"))
        stem = "".join(stem_parts)
        tag = f"cycle{cycle_no:03d}" if isinstance(cycle_no, int) else f"cycle{cycle_no}"
        return f"{stem}_{tag}.dat" if stem else f"{tag}.dat"

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
        """저장 경로 미리보기 갱신 (cycle 번호는 자리표시자 NNN)."""
        main_folder = self._le_main_folder.text().strip()
        if not main_folder:
            self._lbl_save_path.setText("(Main Folder 미지정 — 저장할 수 없습니다)")
            self._lbl_save_path.setStyleSheet("color: #d7ba7d;")
            return
        self._lbl_save_path.setStyleSheet("color: #555555;")
        include_date = self._cb_include_date.isChecked()
        directory = self._save_target_dir(
            main_folder, self._le_sub_folder.text(), include_date)
        name = self._cycle_file_name("NNN", self._le_file_name.text(), include_date)
        self._lbl_save_path.setText(str(directory / name))

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
    # Start / Stop
    # ------------------------------------------------------------------

    def lock_ui(self, locked: bool) -> None:
        """측정 중 설정 UI 잠금/해제 (Start/Stop/Resume 버튼 제외)."""
        self._ch_frame.setEnabled(not locked)
        self._targets_frame.setEnabled(not locked)
        self._param_frame.setEnabled(not locked)
        # 저장 설정은 잠그되, 경로 미리보기·폴더 열기 버튼은 계속 쓸 수 있게 둔다
        for widget in (self._le_main_folder, self._btn_browse, self._le_sub_folder,
                       self._le_file_name, self._cb_include_date, self._cb_save_enabled):
            widget.setEnabled(not locked)

    def _unlock_main_ui(self) -> None:
        """Cycle Sweep 종료 시 Main Window UI 복원."""
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
        if self._phase != CycleSweepPhase.IDLE:
            return
        if not self._check_start_preconditions():
            return
        if not self._main_win._run_connection_test(include_cycle=True, show_success=False):
            return
        if not self._prepare_run():
            return
        self._main_win._log(
            f"Cycle Sweep started. {len(self._targets)} segments x "
            f"{self._cfg.cycles} cycles. Moving to initial value "
            f"{self._cfg.initial_value:g} first.", color="#4ec9b0")
        self._start_pre_init(self._cfg.initial_value, then_cycle=0)

    def _check_start_preconditions(self) -> bool:
        """시작 전 확인. 하나라도 걸리면 안내하고 False."""
        main_win = self._main_win
        if main_win._running:
            QMessageBox.warning(self, "Sweep 실행 중",
                                "Main sweep 이 실행 중입니다. 먼저 중단하세요.")
            return False
        if self._channel is None:
            QMessageBox.warning(
                self, "No Sweep Channel",
                "Keithley 2636A 의 Sweep Channel 을 선택하세요.\n"
                "목록이 비어 있으면 Instrument Settings 에서 해당 장비의 드라이버가 "
                "Keithley2636A 인지, Parameter Manager 에 Paired Command 가 "
                "등록돼 있는지 확인하세요.")
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
                "Cycle Sweep 데이터가 파일로 저장되지 않습니다.\n\n"
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

        self._reset_run_state()
        self._prepare_derivative_channels()
        self._ctx = self._build_context()
        self._configure_metadata()

        self._lock_ui_for_run()
        self._sync_data_window_columns()
        self._prepare_graph_session()
        return True

    def _reset_run_state(self):
        """이전 측정의 잔여 상태를 지운다."""
        self._cycle_idx = 0
        self._seg_idx = 0
        self._seg_phase_name = ""
        self._pre_init_target = 0.0
        self._pending_cycle_idx = 0
        self._last_write_value = None
        self._cycle_filepath = None
        self._auto_retry_used = False
        self._last_step_request = None
        self._retry_timer.stop()
        self._btn_resume.setEnabled(False)

    def _prepare_derivative_channels(self):
        """미분 채널을 현재 UI 설정으로 다시 만들고 버퍼를 비운다 (단일 sweep 과 동일)."""
        main_win = self._main_win
        for order, (channel, _key) in enumerate(main_win._deriv_channels(), start=1):
            channel.reconfigure(main_win._build_deriv_config(order))
            channel.reset()

    def _build_context(self) -> CycleSweepContext:
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

        # sweep 열 헤더 + safety 파라미터는 이 창이 고른 채널에서 가져온다
        sv_id = self._channel_radio_group.checkedId()
        sweep_values = profile.sweep_values
        sv = sweep_values[sv_id] if 0 <= sv_id < len(sweep_values) else None
        sweep_col = (sv.figure_axis or "target", sv.unit) if sv else ("target", "")

        return CycleSweepContext(
            sweep_channel=self._channel,
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
            alias=self._channel_alias,
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

    def _sync_data_window_columns(self):
        """측정값을 메인 창의 Data 창에 표시하기 위한 열 구성."""
        ctx = self._ctx
        columns = [(ctx.sweep_col[0], ctx.sweep_col[1])]
        columns.extend(ctx.meas_cols)
        self._main_win._data_window.configure_columns(columns)
        self._main_win._data_window.clear_values()

    def _prepare_graph_session(self):
        """그래프 세션을 새로 연다 — 창이 떠 있지 않아도 history/columns 는 갱신한다.

        cycle 경계에서는 다시 부르지 않는다. cycle 은 값이 이어지므로 모든 cycle 을
        한 그래프에 겹쳐 보는 편이 hysteresis 확인에 낫다 (파일만 cycle 별로 나뉜다).
        """
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
        """사용자가 Stop 을 누른 경우 — 현재 cycle 을 재개 로그에 남기고 중단."""
        if self._phase != CycleSweepPhase.IDLE:
            self._save_resume_point("사용자 중단(Stop)")
        self._on_stop()

    def _on_stop(self):
        if self._phase == CycleSweepPhase.IDLE:
            return
        self._sweep_timer.stop()
        self._retry_timer.stop()
        self._sweep_worker.request_stop()
        self._set_phase(CycleSweepPhase.IDLE)
        self._lbl_target_val.setText("—")
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._stop_glow()
        self.sweep_finished.emit()
        self._main_win._log("Cycle Sweep stopped.", color="#ce9178")
        self._unlock_main_ui()
        self._update_resume_btn_enabled()

    def _finish(self):
        self._set_phase(CycleSweepPhase.IDLE)
        self._lbl_target_val.setText("—")
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._stop_glow()
        self.sweep_finished.emit()
        cycles = self._cfg.cycles if self._cfg else 0
        self._main_win._log(
            f"Cycle Sweep complete. ({cycles} cycles x {len(self._targets)} segments)",
            color="#4ec9b0")
        self._unlock_main_ui()
        self._update_resume_btn_enabled()

    # ------------------------------------------------------------------
    # Cycle / segment 진행
    # ------------------------------------------------------------------

    def _set_phase(self, phase: CycleSweepPhase):
        self._phase = phase
        self._lbl_phase.setText(phase.name)

    def _start_pre_init(self, target: float, then_cycle: int):
        """cycle 시작 전 채널을 target 으로 옮긴다 — 데이터는 기록하지 않는다.

        신규 시작이면 target = initial value. 재개(cycle k>0)면 그 cycle 이 원래
        시작했을 위치(직전 cycle 의 마지막 target)로 맞춘 뒤 이어간다.
        """
        self._pre_init_target = target
        self._pending_cycle_idx = then_cycle
        self._set_phase(CycleSweepPhase.PRE_INIT)
        self._lbl_target_val.setText(f"{target:.6g}")
        self._lbl_cycle_progress.setText(f"—/{self._cfg.cycles}")
        self._lbl_seg_progress.setText(f"—/{len(self._targets)}")
        self._sweep_timer.start(0)

    def _handle_pre_init_step(self, result: StepResult):
        """초기값으로 이동하는 구간 — 데이터는 기록하지 않는다."""
        self._last_write_value = result.next_v
        if result.is_done:
            self._main_win._log(
                f"  At initial value {self._pre_init_target:g}. Beginning cycle "
                f"{self._pending_cycle_idx + 1}.", color="#4ec9b0")
            self._begin_cycle(self._pending_cycle_idx)
        else:
            self._sweep_timer.start(self._next_interval_ms(result))

    def _begin_cycle(self, cycle_idx: int):
        """cycle 하나를 시작한다 — 새 파일 + 초기점 측정 + 첫 구간."""
        self._cycle_idx = cycle_idx
        self._set_phase(CycleSweepPhase.SWEEPING)
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
        self._lbl_seg_progress.setText(
            f"{self._seg_idx + 1}/{len(self._targets)}")
        if 0 <= self._seg_idx < len(self._targets):
            unit = self._ctx.sweep_col[1] if self._ctx else ""
            self._lbl_target_val.setText(f"{self._targets[self._seg_idx]:.6g} {unit}")

    def _advance_segment(self):
        """한 구간 완료 → 다음 구간, 없으면 cycle 마감."""
        if self._begin_segment(self._seg_idx + 1):
            return
        self._finish_cycle()

    def _finish_cycle(self):
        """cycle 하나 완료 — 메타 JSON 저장 후 다음 cycle 또는 종료."""
        main_win = self._main_win
        main_win._meta_manager.save(
            main_win._param_manager_reg.meta_data_config,
            self._cycle_filepath,
            extra=self._build_meta_extra(),
        )
        main_win._log(f"  [cycle {self._cycle_idx + 1}] complete.", color="#4ec9b0")

        if self._cycle_idx + 1 < self._cfg.cycles:
            self._begin_cycle(self._cycle_idx + 1)
        elif self._cfg.return_to_zero:
            self._start_return_zero()
        else:
            self._finish()

    def _start_return_zero(self):
        """모든 cycle 종료 후 0 으로 복귀 — 데이터는 기록하지 않는다."""
        self._set_phase(CycleSweepPhase.RETURNING_ZERO)
        self._lbl_phase.setText("RETURNING 0")
        self._lbl_target_val.setText("0")
        self._main_win._log("  Returning to zero (no data).", color="#d7ba7d")
        self._sweep_timer.start(0)

    def _build_meta_extra(self) -> dict:
        """메타 데이터 JSON 에 기록할 cycle 정보."""
        cfg = self._cfg
        return {
            "cycle_sweep": {
                "alias": self._ctx.alias if self._ctx else "",
                "cycle_index": self._cycle_idx + 1,
                "cycles": cfg.cycles if cfg else 0,
                "initial_value": cfg.initial_value if cfg else 0.0,
                "targets": list(self._targets),
                "sweep_rate": cfg.sweep_rate if cfg else 0.0,
                "time_per_point": cfg.time_per_point if cfg else 0.0,
                "return_to_zero": cfg.return_to_zero if cfg else False,
                "unit": self._ctx.sweep_col[1] if self._ctx else "",
            }
        }

    # ------------------------------------------------------------------
    # DataSaver
    # ------------------------------------------------------------------

    def _setup_datasaver_for_cycle(self) -> bool:
        """cycle 하나에 해당하는 .dat 세션을 시작한다.

        반환: 측정을 계속해도 되는지 여부.
          - 저장이 의도적으로 비활성: True (저장 없이 진행)
          - 저장 활성 + 성공: True
          - 저장 활성 + 실패: False (경고 후 측정 중단 — 데이터 유실 방지)
        """
        ctx = self._ctx
        cycle_no = self._cycle_idx + 1
        fixed_name = self._cycle_file_name(cycle_no, ctx.file_name, ctx.include_date)

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

        filepath = self._data_saver.start_session()
        if filepath:
            self._main_win._log(f"  [cycle {cycle_no}] Data -> {filepath}",
                                color="#888888")
        elif self._data_saver.start_error() is not None:
            err = self._data_saver.start_error()
            self._main_win._log(
                f"  [cycle {cycle_no}] 데이터 저장 시작 실패 — 측정 중단: {err}",
                color="#f44747")
            self._on_stop()
            QMessageBox.critical(
                self, "데이터 저장 실패 — 측정 중단",
                f"cycle {cycle_no} 의 데이터 파일을 시작할 수 없어 측정을 중단했습니다.\n\n"
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
        self._last_step_request = req
        self.request_step.emit(req)

    def _sweep_tick(self):
        if self._phase not in (CycleSweepPhase.PRE_INIT,
                               CycleSweepPhase.SWEEPING,
                               CycleSweepPhase.RETURNING_ZERO):
            return
        cfg = self._cfg
        ctx = self._ctx
        if self._phase == CycleSweepPhase.PRE_INIT:
            sweep_to = self._pre_init_target
            active_meas = []                       # 초기값 이동 구간은 데이터 없음
        elif self._phase == CycleSweepPhase.RETURNING_ZERO:
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
        if self._phase == CycleSweepPhase.PRE_INIT:
            self._handle_pre_init_step(result)
            return
        if self._phase == CycleSweepPhase.RETURNING_ZERO:
            self._handle_return_step(result)
            return
        if self._phase != CycleSweepPhase.SWEEPING:
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

    def _handle_return_step(self, result: StepResult):
        """0 복귀 구간 — 데이터는 기록하지 않는다."""
        self._last_write_value = result.next_v
        if result.is_done:
            self._finish()
        else:
            self._sweep_timer.start(self._next_interval_ms(result))

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

        err_msg = (f"ERR @ cycle {self._cycle_idx + 1} seg {self._seg_idx + 1}\n"
                   f"실패 채널: {', '.join(descs)}")
        self._main_win._log(f"  [CycleSweep] {err_msg}", color="#f44747")

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
            "  [CycleSweep] 데이터 기록 실패 — 측정 중단 (디스크/권한 확인).",
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
            f"  [CycleSweep] Step error — {humanize_error(msg)}  [상세] {msg}",
            color="#f44747")
        if is_comm_error(msg):
            self._handle_comm_error(f"Step error: {msg}")
        else:
            self._on_stop()

    # ------------------------------------------------------------------
    # 통신 오류 자동 재개 / 수동 재개
    # ------------------------------------------------------------------

    _AUTO_RESUME_DELAY_MS = 10_000   # 통신 오류 후 자동 재개 대기 (10초)

    def _handle_comm_error(self, reason: str):
        """통신 오류: 1회는 10초 후 자동 재개, 재차 발생 시 중단 + 재개 지점 저장."""
        if self._phase == CycleSweepPhase.IDLE:
            return
        if not self._auto_retry_used:
            self._auto_retry_used = True
            self._sweep_timer.stop()
            self._main_win._log(
                f"  [CycleSweep] 통신 오류 — {self._AUTO_RESUME_DELAY_MS // 1000}초 후 "
                f"자동 재개합니다.  ({reason})",
                color="#d7ba7d")
            self._retry_timer.start(self._AUTO_RESUME_DELAY_MS)
            return

        self._save_resume_point(reason)
        cause = humanize_error(reason)
        self._main_win._log(
            "  [CycleSweep] 자동 재개 후 재차 통신 오류 — 측정 중단. "
            "[Resume] 버튼으로 저장된 cycle 부터 재개하세요.",
            color="#f44747")
        self._main_win._log(f"     원인: {cause}", color="#f44747")
        self._on_stop()
        QMessageBox.warning(
            self, "통신 오류 — Cycle Sweep 중단",
            "자동 재개 후에도 통신 오류가 반복되어 측정을 중단했습니다.\n\n"
            f"원인: {cause}\n\n"
            "현재 cycle 이 재개 로그에 저장되었습니다.\n"
            "[Resume] 버튼으로 해당 cycle 부터 다시 시작할 수 있습니다.",
        )

    def _auto_resume(self):
        """10초 경과 후 마지막 요청을 재전송하여 측정을 이어간다."""
        if self._phase == CycleSweepPhase.IDLE or self._last_step_request is None:
            return
        self._main_win._log("  [CycleSweep] 자동 재개 — 측정을 재시작합니다.",
                            color="#4ec9b0")
        self.request_step.emit(self._last_step_request)

    def _save_resume_point(self, reason: str):
        """현재 cycle 위치를 재개 로그에 저장 (재개 단위는 cycle).

        PRE_INIT 중이면 아직 cycle 이 시작되지 않았으므로 곧 시작할 cycle 을 남긴다.
        """
        cycles = self._cfg.cycles if self._cfg else 0
        if self._phase == CycleSweepPhase.PRE_INIT:
            cycle_idx = self._pending_cycle_idx
            where = "초기값 이동 중"
        else:
            cycle_idx = self._cycle_idx
            where = f"seg {self._seg_idx + 1}/{len(self._targets)}"
        label = (f"cycle {cycle_idx + 1}/{cycles}  {where}  ({reason[:40]})")
        self._resume_log.add(ResumePoint(
            sweep_type="cycle",
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            label=label,
            data_filepath="",   # cycle 파일은 재개 시 새로 만든다
            payload={
                "cycle_idx": cycle_idx,
                "segment_idx": self._seg_idx,
            },
        ))
        self._update_resume_btn_enabled()

    def _update_resume_btn_enabled(self):
        """재개 로그에 cycle 지점이 있고 IDLE 이면 Resume 활성화."""
        if not hasattr(self, "_btn_resume"):
            return
        has_point = self._resume_log.latest("cycle") is not None
        self._btn_resume.setEnabled(has_point and self._phase == CycleSweepPhase.IDLE)

    def _on_resume_clicked(self):
        """[Resume] 버튼: 저장된 지점 목록에서 선택 후 해당 cycle 부터 재개."""
        if self._phase != CycleSweepPhase.IDLE:
            return
        dlg = ResumePickerDialog(self._resume_log.all(), parent=self, sweep_type="cycle")
        if dlg.exec() and dlg.selected_point is not None:
            self._resume_from_point(dlg.selected_point)

    def _resume_from_point(self, point):
        """선택된 cycle 부터 다시 시작한다 (그 cycle 의 파일은 새로 쓴다)."""
        if not self._check_start_preconditions():
            return
        if not self._main_win._run_connection_test(include_cycle=True, show_success=False):
            return
        if not self._prepare_run():
            return
        idx = int(point.payload.get("cycle_idx", 0))
        idx = max(0, min(idx, self._cfg.cycles - 1))
        # 그 cycle 이 원래 출발했을 위치로 먼저 맞춘다 — cycle 1 은 초기값,
        # 2회차부터는 직전 cycle 의 마지막 target.
        start_at = self._cfg.initial_value if idx == 0 else self._targets[-1]
        self._main_win._log(
            f"  [CycleSweep] 수동 재개 — {start_at:g} 로 이동 후 "
            f"cycle {idx + 1}/{self._cfg.cycles} 부터 재시작.",
            color="#4ec9b0")
        self._start_pre_init(start_at, then_cycle=idx)

    # ------------------------------------------------------------------
    # Glow animation
    # ------------------------------------------------------------------

    def _update_glow(self):
        self._glow_phase += 0.07
        intensity = (math.sin(self._glow_phase) + 1) / 2
        alpha = int(80 + intensity * 140)
        green = int(140 + intensity * 80)
        self._glow_frame.setStyleSheet(
            f"QFrame#csGlowFrame {{"
            f"border: 3px solid rgba(40, {green}, 70, {alpha});"
            f"border-radius: 6px; }}"
        )

    def _stop_glow(self):
        self._glow_timer.stop()
        self._glow_frame.setStyleSheet(
            "QFrame#csGlowFrame { border: 3px solid transparent; border-radius: 6px; }"
        )

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def showEvent(self, event):
        # closeEvent 에서 워커 스레드를 종료했을 수 있다 — 필요하면 다시 켠다.
        if not self._worker_thread.isRunning():
            self._worker_thread.start()
        # 실행 중에는 라디오를 재빌드하지 않는다 — 상태머신이 self._channel 을
        # 라이브로 읽는데, 재빌드가 이를 None 으로 만들면 다음 스텝에서 깨진다.
        if self._phase == CycleSweepPhase.IDLE:
            self._rebuild_channel_radios()
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
        if self._phase != CycleSweepPhase.IDLE:
            reply = QMessageBox.question(
                self, "종료 확인",
                "Cycle Sweep 이 실행 중입니다. 중단하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            self._on_stop()
        self._save_config()
        self._worker_thread.quit()
        self._worker_thread.wait()
        event.accept()
