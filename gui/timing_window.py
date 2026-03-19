"""
TimingWindow: Sweep 루프의 각 단계별 소요 시간을 실시간으로 표시합니다.
"""
from typing import Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QHeaderView, QPushButton, QLabel, QFrame,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor

from core.sweep_worker import StepTiming


_MONO = QFont("Consolas", 10)

# (행 이름, 색상)
_ROWS = [
    ("Period  (tick → tick)",    "#c9d1d9"),
    ("Dispatch → Worker",        "#e3b341"),
    ("Source Read  (1st step only)", "#79c0ff"),
    ("Write",                    "#ff7b72"),
    ("Measurements",             "#56d364"),
    ("Worker → Main",            "#e3b341"),
    ("UI Update",                "#c9d1d9"),
    ("─────────────────",        "#444444"),
    ("Total VISA (worker)",      "#79c0ff"),
    ("Idle  (wait for next tick)","#888888"),
]

_COL_LAST, _COL_MIN, _COL_MAX, _COL_AVG = 1, 2, 3, 4
_N_ROWS = len(_ROWS)


class TimingWindow(QDialog):
    """Sweep step 단계별 타이밍을 표시하는 창."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Timing")
        self.resize(540, 380)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        self._prev_t_emit: Optional[float] = None
        self._time_per_point: float = 1.0
        self._counts = [0] * _N_ROWS
        self._sums   = [0.0] * _N_ROWS
        self._mins   = [float("inf")] * _N_ROWS
        self._maxs   = [float("-inf")] * _N_ROWS

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        title = QLabel("Step Timing  (ms)")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #30363d;")
        layout.addWidget(sep)

        self._table = QTableWidget(_N_ROWS, 5)
        self._table.setHorizontalHeaderLabels(["Stage", "Last", "Min", "Max", "Avg"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (_COL_LAST, _COL_MIN, _COL_MAX, _COL_AVG):
            self._table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents
            )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setFont(_MONO)
        self._table.setStyleSheet(
            "QTableWidget { background:#0d1117; color:#c9d1d9; gridline-color:#30363d; }"
            "QHeaderView::section { background:#161b22; color:#8b949e; border:none; padding:4px; }"
        )

        for row, (label, color) in enumerate(_ROWS):
            item = QTableWidgetItem(label)
            item.setForeground(QColor(color))
            self._table.setItem(row, 0, item)
            for col in (_COL_LAST, _COL_MIN, _COL_MAX, _COL_AVG):
                cell = QTableWidgetItem("—")
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table.setItem(row, col, cell)

        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_clear = QPushButton("Clear")
        btn_clear.setFixedWidth(70)
        btn_clear.clicked.connect(self.clear)
        btn_row.addWidget(btn_clear)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_time_per_point(self, tpp: float):
        self._time_per_point = tpp

    def update_timing(self, timing: StepTiming, t_recv: float, t_ui_done: float):
        """
        _on_step_done 에서 호출.
        timing  : worker 스레드 타임스탬프
        t_recv  : _on_step_done 진입 시각
        t_ui_done: UI 갱신 완료 직후 시각
        """
        t = timing

        # 각 구간 (ms) — 실행 순서: source → write → measure
        period     = (t.t_emit - self._prev_t_emit) * 1000 if self._prev_t_emit else None
        dispatch   = (t.t_worker_start - t.t_emit)   * 1000
        src_read   = (t.t_source_read  - t.t_worker_start) * 1000
        write      = (t.t_write_done   - t.t_source_read)  * 1000
        meas       = (t.t_meas_done    - t.t_write_done)   * 1000
        w2m        = (t_recv           - t.t_meas_done)    * 1000
        ui_upd     = (t_ui_done        - t_recv)            * 1000
        total_visa = (t.t_meas_done    - t.t_worker_start)  * 1000
        idle       = self._time_per_point * 1000 - (t_ui_done - t.t_emit) * 1000

        values = [period, dispatch, src_read, write, meas, w2m, ui_upd, None, total_visa, idle]

        for row, val in enumerate(values):
            if val is None:
                continue
            self._update_row(row, val)

        self._prev_t_emit = t.t_emit

    def clear(self):
        self._prev_t_emit = None
        self._counts = [0] * _N_ROWS
        self._sums   = [0.0] * _N_ROWS
        self._mins   = [float("inf")] * _N_ROWS
        self._maxs   = [float("-inf")] * _N_ROWS
        for row in range(_N_ROWS):
            for col in (_COL_LAST, _COL_MIN, _COL_MAX, _COL_AVG):
                self._table.item(row, col).setText("—")

    # ------------------------------------------------------------------

    def _update_row(self, row: int, val_ms: float):
        self._counts[row] += 1
        self._sums[row]   += val_ms
        if val_ms < self._mins[row]:
            self._mins[row] = val_ms
        if val_ms > self._maxs[row]:
            self._maxs[row] = val_ms
        avg = self._sums[row] / self._counts[row]

        # 색상: idle이 음수면 빨간색 경고
        color = "#f44747" if (row == 9 and val_ms < 0) else None

        def _set(col, v):
            item = self._table.item(row, col)
            item.setText(f"{v:.1f}")
            if color:
                item.setForeground(QColor(color))
            else:
                item.setForeground(QColor(_ROWS[row][1]))

        _set(_COL_LAST, val_ms)
        _set(_COL_MIN,  self._mins[row])
        _set(_COL_MAX,  self._maxs[row])
        _set(_COL_AVG,  avg)

    # ------------------------------------------------------------------

    def closeEvent(self, event):
        event.ignore()
        self.hide()
