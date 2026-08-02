"""측정 모듈 창이 공유하는 라이브 플롯 패널.

VNA 창과 MFLI 창이 각각 거의 같은 _PlotPanel/_YCurveRow 를 325줄씩 복사해서
들고 있었다(diff 149줄). 한쪽만 고쳐지는 일이 실제로 벌어져서 — 예컨대 y 소스
변경 시그널 연결 버그는 MFLI 쪽에서만 수정돼 있었다 — 하나로 합친다.

두 창의 UI 차이는 생성자 플래그로 남긴다:
  square_hint    VNA 는 패널을 1:1 비율로 잡는다.
  log_toggles    MFLI 는 log x / log y 체크박스를 쓴다.

설정 저장 타입(VnaPlotCurveConfig / MfliPlotCurveConfig)은 모듈마다 다르므로
이 위젯은 그 타입을 모른다. 대신 PlotPanelState 로 주고받고, 변환은 각 창이
한다 — 위젯이 특정 모듈에 묶이지 않게 하기 위함.
"""
from dataclasses import dataclass, field
from typing import List

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)

MONO = QFont("Consolas", 9)

#: 곡선 색상 순환표 (두 창이 같은 팔레트를 쓴다)
CURVE_COLORS = ["#4ec9b0", "#ce9178", "#79c0ff", "#d2a8ff",
                "#ffa657", "#7ee787", "#f78166", "#a5d6ff"]


@dataclass
class PlotPanelState:
    """패널의 저장/복원 상태 — 모듈별 pydantic 설정과 이 위젯 사이의 중립 표현."""
    x_source: str = ""
    y_sources: List[str] = field(default_factory=list)
    log_x: bool = False
    log_y: bool = False


def _finite(arr) -> np.ndarray:
    """비유한값(NaN/Inf)을 걸러낸 1차원 float 배열."""
    a = np.asarray(arr, dtype=float).ravel()
    return a[np.isfinite(a)] if a.size else a


class YCurveRow(QWidget):
    """y 소스 하나 = 색 스와치 + 콤보박스 + 삭제 버튼 + pyqtgraph 곡선."""

    remove_requested = Signal(object)   # emits self
    source_changed = Signal()

    def __init__(self, color: str, sources: List[str], y_src: str,
                 plot_widget: pg.PlotWidget, parent=None):
        super().__init__(parent)
        self._color = color
        self._plot = plot_widget

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        swatch = QLabel("●")
        swatch.setStyleSheet(f"color: {color}; font-size: 14px;")
        swatch.setFixedWidth(18)
        lay.addWidget(swatch)

        self._cb_y = QComboBox()
        self._cb_y.setFont(MONO)
        for s in sources:
            self._cb_y.addItem(s, s)
        if y_src:
            idx = self._cb_y.findData(y_src)
            if idx >= 0:
                self._cb_y.setCurrentIndex(idx)
        # currentIndexChanged(int) → 0-arg source_changed. int 을 흘려보내지 않으면
        # emit 이 TypeError 를 낸다(VNA 창에 남아 있던 버그). 같은 GUI 스레드 신호라
        # lambda 를 써도 워커→GUI 크래시 규칙과 무관하다.
        self._cb_y.currentIndexChanged.connect(lambda *_: self.source_changed.emit())
        lay.addWidget(self._cb_y, stretch=1)

        btn_rm = QPushButton("✕")
        btn_rm.setFixedSize(20, 20)
        btn_rm.clicked.connect(lambda: self.remove_requested.emit(self))
        lay.addWidget(btn_rm)

        self._curve = plot_widget.plot(pen=pg.mkPen(color, width=1.5))
        self._curve.setDownsampling(auto=True, method="peak")
        self._curve.setClipToView(True)

    def y_source(self) -> str:
        return self._cb_y.currentData() or ""

    def set_y_source(self, y_src: str) -> None:
        """시그널을 막고 y 소스만 바꾼다 (복원 중 연쇄 replot 방지)."""
        idx = self._cb_y.findData(y_src)
        if idx < 0:
            return
        self._cb_y.blockSignals(True)
        self._cb_y.setCurrentIndex(idx)
        self._cb_y.blockSignals(False)

    def update_sources(self, sources: List[str]) -> None:
        prev = self._cb_y.currentData()
        self._cb_y.blockSignals(True)
        self._cb_y.clear()
        for s in sources:
            self._cb_y.addItem(s, s)
        idx = self._cb_y.findData(prev)
        if idx >= 0:
            self._cb_y.setCurrentIndex(idx)
        self._cb_y.blockSignals(False)

    def set_data(self, x: np.ndarray, y: np.ndarray) -> None:
        """곡선 데이터 갱신.

        NaN/Inf 가 섞인 데이터를 skipFiniteCheck 로 그리면 pyqtgraph 렌더 레이어에서
        네이티브 크래시가 날 수 있다. float 배열로 정규화하고 Inf→NaN(=gap)으로
        바꾼 뒤, 유한성 검사를 켠 채 그린다.
        """
        try:
            xa = np.asarray(x, dtype=float).ravel()
            ya = np.asarray(y, dtype=float).ravel()
            n = min(xa.size, ya.size)
            if n == 0:
                self._curve.setData([], [])
                return
            xa, ya = xa[:n], ya[:n]
            if not np.isfinite(xa).all():
                xa = np.where(np.isfinite(xa), xa, np.nan)
            if not np.isfinite(ya).all():
                ya = np.where(np.isfinite(ya), ya, np.nan)
            self._curve.setData(xa, ya)
        except Exception:
            # 그리기 실패가 측정을 멈추면 안 된다 — 빈 곡선으로 두고 넘어간다.
            try:
                self._curve.setData([], [])
            except Exception:
                pass

    def clear_data(self) -> None:
        self._curve.setData([], [])

    def remove_from_plot(self) -> None:
        self._plot.removeItem(self._curve)


class PlotPanel(QFrame):
    """x 소스 선택 + 여러 y 곡선 + pyqtgraph PlotWidget 한 벌."""

    def __init__(self, start_color_idx: int = 0, *,
                 square_hint: bool = False, log_toggles: bool = False,
                 parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._data: dict = {}
        self._sources: List[str] = []
        self._y_rows: List[YCurveRow] = []
        self._color_idx = start_color_idx
        self._square_hint = square_hint
        self._log_toggles = log_toggles
        self._cb_logx = None
        self._cb_logy = None
        self._build()

    # ── 레이아웃 ────────────────────────────────────────────────────────
    def hasHeightForWidth(self) -> bool:
        return self._square_hint

    def heightForWidth(self, w: int) -> int:
        return w

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)
        lay.addLayout(self._build_control_row())

        self._y_container = QWidget()
        self._y_lay = QVBoxLayout(self._y_container)
        self._y_lay.setContentsMargins(0, 0, 0, 0)
        self._y_lay.setSpacing(2)
        lay.addWidget(self._y_container)

        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        lay.addWidget(self._plot, stretch=1)

        self._add_y_row()   # 플롯이 생긴 뒤에 첫 y row 를 만든다

    def _build_control_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(QLabel("x:"))

        self._cb_x = QComboBox()
        self._cb_x.setFont(MONO)
        self._cb_x.setMinimumWidth(80)
        self._cb_x.currentIndexChanged.connect(self._replot_all)
        row.addWidget(self._cb_x, stretch=1)

        btn_add = QPushButton("＋Y")
        btn_add.setFixedSize(36, 20)
        btn_add.setToolTip("y 곡선 추가")
        btn_add.clicked.connect(lambda: self._add_y_row())
        row.addWidget(btn_add)

        btn_fit = QPushButton("Fit")
        btn_fit.setFixedSize(36, 20)
        btn_fit.setToolTip("현재 표시된 데이터에 맞춰 X·Y 범위를 한 번 자동 맞춤\n"
                           "(sweep 중 신호가 화면 밖으로 나갔을 때 사용)")
        btn_fit.clicked.connect(self.auto_range)
        row.addWidget(btn_fit)

        if self._log_toggles:
            self._cb_logx = QCheckBox("log x")
            self._cb_logx.setToolTip("x축 로그 스케일 (양수 값만 표시됨)")
            self._cb_logx.toggled.connect(self._on_log_toggled)
            row.addWidget(self._cb_logx)
            self._cb_logy = QCheckBox("log y")
            self._cb_logy.setToolTip("y축 로그 스케일 (양수 값만 표시됨)")
            self._cb_logy.toggled.connect(self._on_log_toggled)
            row.addWidget(self._cb_logy)
        return row

    # ── y row 관리 ──────────────────────────────────────────────────────
    def _next_color(self) -> str:
        c = CURVE_COLORS[self._color_idx % len(CURVE_COLORS)]
        self._color_idx += 1
        return c

    def _add_y_row(self, y_src: str = "") -> None:
        row = YCurveRow(self._next_color(), self._sources, y_src,
                        self._plot, self._y_container)
        row.remove_requested.connect(self._remove_y_row)
        row.source_changed.connect(self._replot_all)
        self._y_rows.append(row)
        self._y_lay.addWidget(row)
        self._replot_all()

    def _remove_y_row(self, row: YCurveRow) -> None:
        if len(self._y_rows) <= 1:
            return   # 최소 한 줄은 남긴다
        self._y_rows.remove(row)
        self._drop_row(row)
        self._replot_all()

    @staticmethod
    def _drop_row(row: YCurveRow) -> None:
        row.remove_from_plot()
        row.setParent(None)
        row.deleteLater()

    # ── 데이터/소스 ─────────────────────────────────────────────────────
    def update_sources(self, sources: List[str]) -> None:
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

    def set_default_x(self, x_src: str) -> None:
        ix = self._cb_x.findData(x_src)
        if ix >= 0:
            self._cb_x.setCurrentIndex(ix)

    def set_default_y(self, y_src: str) -> None:
        """첫 번째 y row 의 소스를 설정한다."""
        if self._y_rows:
            self._y_rows[0].set_y_source(y_src)

    def push_data(self, data: dict, first_step: bool = False) -> None:
        self._data = data
        if first_step:
            # 뷰 범위를 먼저 잡아야 setData 의 ClipToView 가 올바른 범위로 자른다
            self._fit_view()
        self._replot_all()

    def clear_data(self) -> None:
        self._data = {}
        for row in self._y_rows:
            row.clear_data()
        self._plot.setLabel("bottom", "")
        self._plot.setLabel("left", "")

    def _replot_all(self) -> None:
        x_src = self._cb_x.currentData() or ""
        xd = self._data.get(x_src, np.array([]))
        y_labels = []
        for row in self._y_rows:
            y_src = row.y_source()
            row.set_data(xd, self._data.get(y_src, np.array([])))
            if y_src:
                y_labels.append(y_src)
        self._plot.setLabel("bottom", x_src)
        if y_labels:
            self._plot.setLabel("left", y_labels[0])

    # ── 뷰 범위 ─────────────────────────────────────────────────────────
    def auto_range(self) -> None:
        self._fit_view()

    def log_x(self) -> bool:
        return bool(self._cb_logx and self._cb_logx.isChecked())

    def log_y(self) -> bool:
        return bool(self._cb_logy and self._cb_logy.isChecked())

    def _on_log_toggled(self, *_) -> None:
        self._apply_log_mode()
        self._fit_view()

    def _apply_log_mode(self) -> None:
        try:
            self._plot.setLogMode(x=self.log_x(), y=self.log_y())
        except Exception:
            pass

    def _fit_view(self) -> None:
        """raw 데이터에서 직접 min/max 를 계산해 x, y 범위를 따로 잡는다.

        enableAutoRange() 는 ClipToView 와 닭-달걀 관계라 쓰지 않는다. 비유한값은
        제외해야 setXRange/setYRange 가 죽지 않는다. 로그 모드에서는 양수만 쓰고
        뷰 좌표가 log10(값)이라 변환해서 넣는다.
        """
        try:
            self._fit_axis(
                self._finite_values(self._cb_x.currentData() or ""),
                self.log_x(), self._plot.setXRange)

            all_y = [ys for row in self._y_rows
                     if (ys := self._finite_values(row.y_source())).size]
            if all_y:
                self._fit_axis(np.concatenate(all_y), self.log_y(),
                               self._plot.setYRange)
        except Exception:
            pass

    def _finite_values(self, source: str) -> np.ndarray:
        return _finite(self._data.get(source, np.array([])))

    @staticmethod
    def _fit_axis(values: np.ndarray, log_mode: bool, set_range) -> None:
        if log_mode:
            values = values[values > 0]     # 로그 축은 양수만
        if not values.size:
            return
        lo, hi = float(values.min()), float(values.max())
        if log_mode:
            lo, hi = np.log10(lo), np.log10(hi)
        if lo != hi and np.isfinite(lo) and np.isfinite(hi):
            set_range(lo, hi, padding=0.05)

    # ── 상태 저장/복원 ──────────────────────────────────────────────────
    def x_source(self) -> str:
        return self._cb_x.currentData() or "index"

    def y_sources(self) -> List[str]:
        return [row.y_source() for row in self._y_rows]

    def to_state(self) -> PlotPanelState:
        return PlotPanelState(
            x_source=self.x_source(),
            y_sources=self.y_sources(),
            log_x=self.log_x(),
            log_y=self.log_y(),
        )

    def restore_state(self, state: PlotPanelState) -> None:
        self._restore_log_toggles(state)

        ix = self._cb_x.findData(state.x_source)
        if ix >= 0:
            self._cb_x.blockSignals(True)
            self._cb_x.setCurrentIndex(ix)
            self._cb_x.blockSignals(False)

        if not state.y_sources:
            self._replot_all()
            return

        while len(self._y_rows) > 1:        # 여분 row 정리 (최소 1개 유지)
            self._drop_row(self._y_rows.pop())

        if self._y_rows:
            first = self._y_rows[0]
            first.update_sources(self._sources)
            first.set_y_source(state.y_sources[0])
        for src in state.y_sources[1:]:
            self._add_y_row(src)

        self._replot_all()

    def _restore_log_toggles(self, state: PlotPanelState) -> None:
        if not self._log_toggles:
            return
        for cb, value in ((self._cb_logx, state.log_x),
                          (self._cb_logy, state.log_y)):
            cb.blockSignals(True)
            cb.setChecked(value)
            cb.blockSignals(False)
        self._apply_log_mode()
