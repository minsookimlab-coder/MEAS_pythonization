"""
GraphWindow: 실시간 측정 그래프 창.

설계 원칙:
- 렌더링 타이머(~30 fps) + dirty 플래그로 측정 속도와 UI 속도를 완전 분리
- DataStore에 모든 phase 데이터를 누적 → 창을 측정 중간에 열어도 전체 이력 표시
- GraphPanel: 한 플롯 패널 단위 (X/Y 축 자유 선택, 측정 중 변경 가능)
- GraphWindow: 패널 목록 컨테이너 (패널 추가/제거 측정 중 가능)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# ──────────────────────────────────────────────────────────
# pyqtgraph global configuration
# ──────────────────────────────────────────────────────────
pg.setConfigOptions(antialias=True, useOpenGL=False)

_MONO = QFont("Consolas", 9)

# Phase → (hex color, Qt.PenStyle)
_PHASE_STYLE: Dict[str, Tuple[str, Qt.PenStyle]] = {
    "_":        ("#1f77b4", Qt.PenStyle.SolidLine),   # single sweep — blue
    "trace":    ("#1f77b4", Qt.PenStyle.SolidLine),   # double trace — blue
    "retrace":  ("#d62728", Qt.PenStyle.SolidLine),   # double retrace — red
    "dummy":    ("#9467bd", Qt.PenStyle.DashLine),    # double dummy — purple dash
}

_PHASE_LABEL: Dict[str, str] = {
    "_": "single", "trace": "trace", "retrace": "retrace", "dummy": "dummy",
}


def _make_pen(phase: str) -> pg.mkPen:
    color, style = _PHASE_STYLE.get(phase, ("#888888", Qt.PenStyle.SolidLine))
    return pg.mkPen(color=color, width=2, style=style)


# ──────────────────────────────────────────────────────────
# SI prefix helpers
# ──────────────────────────────────────────────────────────

# (threshold, divisor, prefix symbol)
_SI_TABLE: List[Tuple[float, float, str]] = [
    (1e9,   1e9,   "G"),
    (1e6,   1e6,   "M"),
    (1e3,   1e3,   "k"),
    (1e0,   1e0,   ""),
    (1e-3,  1e-3,  "m"),
    (1e-6,  1e-6,  "µ"),
    (1e-9,  1e-9,  "n"),
    (1e-12, 1e-12, "p"),
]


def _best_si(arr: np.ndarray) -> Tuple[float, str]:
    """Return (divisor, prefix) that best represents the data magnitude.

    Only meaningful when the column has a physical unit (unit != "").
    For unitless columns call with arr=empty to get (1.0, "").
    """
    if len(arr) == 0:
        return 1.0, ""
    max_abs = float(np.max(np.abs(arr)))
    if max_abs == 0.0:
        return 1.0, ""
    for threshold, divisor, prefix in _SI_TABLE:
        if max_abs >= threshold:
            return divisor, prefix
    return 1e-12, "p"    # below pico → still show as pico


# ──────────────────────────────────────────────────────────
# Public data types
# ──────────────────────────────────────────────────────────

@dataclass
class GraphDataPoint:
    """One measurement step's data pushed to the graph.

    values: {col_key: numeric_value}
    phase:  "" for single sweep  |  "trace"/"retrace"/"dummy" for double sweep
    """
    values: Dict[str, float]
    phase: str = ""


# ──────────────────────────────────────────────────────────
# DataStore
# ──────────────────────────────────────────────────────────

class DataStore:
    """GUI-thread-only accumulator.

    Structure:
      _meta : {col_key: (display_label, unit)}
      _data : {phase_tag: {col_key: [float, ...]}}
    """

    def __init__(self) -> None:
        self._meta: Dict[str, Tuple[str, str]] = {}
        self._data: Dict[str, Dict[str, List[float]]] = {}

    # Schema ------------------------------------------------------------------

    def set_columns(self, columns: List[Tuple[str, str, str]]) -> None:
        """(key, label, unit) per column.  Does NOT clear existing data."""
        self._meta = {k: (lbl, unit) for k, lbl, unit in columns}

    def col_keys(self) -> List[str]:
        return list(self._meta)

    def col_display(self, key: str) -> str:
        lbl, unit = self._meta.get(key, (key, ""))
        return f"{lbl} [{unit}]" if unit else lbl

    def col_display_list(self) -> List[Tuple[str, str]]:
        return [(k, self.col_display(k)) for k in self._meta]

    def col_meta(self, key: str) -> Tuple[str, str]:
        """Return raw (label, unit) for a column key."""
        return self._meta.get(key, (key, ""))

    # Data --------------------------------------------------------------------

    def append(self, point: GraphDataPoint) -> None:
        tag = point.phase or "_"
        bucket = self._data.setdefault(tag, {})
        for k, v in point.values.items():
            bucket.setdefault(k, []).append(v)

    def get_xy(self, x_key: str, y_key: str, phase: str) -> Tuple[np.ndarray, np.ndarray]:
        d = self._data.get(phase, {})
        xs = np.asarray(d.get(x_key, []), dtype=np.float64)
        ys = np.asarray(d.get(y_key, []), dtype=np.float64)
        n = min(len(xs), len(ys))
        return xs[:n], ys[:n]

    def phases(self) -> List[str]:
        return list(self._data)

    def clear(self) -> None:
        self._data.clear()


# ──────────────────────────────────────────────────────────
# GraphPanel
# ──────────────────────────────────────────────────────────

class GraphPanel(QFrame):
    """Single resizable plot panel.

    Features:
    - X / Y axis selectable from measurement parameters (live-changeable)
    - Per-panel Hold/Resume: right-click → Hold; freezes the display while
      measurements continue accumulating.  Auto-holds on manual zoom/pan.
    - Linear Regression: right-click → Linear Regression.  Shows a draggable
      LinearRegionItem; slope, intercept, R² updated in real-time.
      Activating regression auto-holds the panel.
    - SI prefix auto-scaling when Auto-range is enabled.
    """

    remove_requested = Signal(object)   # emits self

    def __init__(self, store: DataStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._x_key: Optional[str] = None
        self._y_key: Optional[str] = None
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._dirty = False
        # SI divisors — persist across redraws so manual zoom stays valid
        self._x_div: float = 1.0
        self._y_div: float = 1.0
        self._x_pfx: str = ""
        self._y_pfx: str = ""
        # Hold state
        self._held: bool = False
        # Regression state
        self._regression_active: bool = False
        self._lr_region: Optional[pg.LinearRegionItem] = None
        self._lr_line: Optional[pg.PlotDataItem] = None
        self._lr_text: Optional[pg.TextItem] = None

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._build_ui()

    # UI ----------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # ── Header row ──────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setSpacing(6)

        hdr.addWidget(QLabel("X:"))
        self._cmb_x = QComboBox()
        self._cmb_x.setFont(_MONO)
        self._cmb_x.setMinimumWidth(150)
        self._cmb_x.currentIndexChanged.connect(self._on_axis_changed)
        hdr.addWidget(self._cmb_x)

        hdr.addWidget(QLabel("Y:"))
        self._cmb_y = QComboBox()
        self._cmb_y.setFont(_MONO)
        self._cmb_y.setMinimumWidth(150)
        self._cmb_y.currentIndexChanged.connect(self._on_axis_changed)
        hdr.addWidget(self._cmb_y)

        self._cb_auto = QCheckBox("Auto-range")
        self._cb_auto.setChecked(True)
        hdr.addWidget(self._cb_auto)

        self._lbl_held = QLabel("● HELD")
        self._lbl_held.setStyleSheet("color: #d62728; font-weight: bold;")
        self._lbl_held.setVisible(False)
        hdr.addWidget(self._lbl_held)

        self._btn_resume = QPushButton("▶ Resume")
        self._btn_resume.setFixedHeight(22)
        self._btn_resume.setVisible(False)
        self._btn_resume.clicked.connect(lambda: self._set_held(False))
        hdr.addWidget(self._btn_resume)

        hdr.addStretch()

        btn_rm = QPushButton("✕")
        btn_rm.setFixedWidth(26)
        btn_rm.setToolTip("Remove this panel")
        btn_rm.clicked.connect(lambda: self.remove_requested.emit(self))
        hdr.addWidget(btn_rm)

        root.addLayout(hdr)

        # ── Plot widget ─────────────────────────────────────────────────────
        self._pw = pg.PlotWidget(background="w")
        self._pw.showGrid(x=True, y=True, alpha=0.25)
        self._pw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pw.setMinimumHeight(220)
        self._pi = self._pw.getPlotItem()
        self._pi.addLegend(offset=(10, 10))
        root.addWidget(self._pw)

        # ── Right-click context menu (added to pyqtgraph's ViewBox menu) ─────
        vb = self._pi.getViewBox()
        vb.menu.addSeparator()
        self._act_hold = QAction("Hold", vb.menu)
        self._act_hold.setCheckable(True)
        self._act_hold.triggered.connect(self._set_held)
        vb.menu.addAction(self._act_hold)

        self._act_regression = QAction("Linear Regression", vb.menu)
        self._act_regression.setCheckable(True)
        self._act_regression.triggered.connect(self._toggle_regression)
        vb.menu.addAction(self._act_regression)

        # ── Auto-hold on manual zoom / pan ───────────────────────────────────
        vb.sigRangeChangedManually.connect(self._on_manual_range_change)

    # Columns -----------------------------------------------------------------

    def update_columns(self, col_display: List[Tuple[str, str]]) -> None:
        """Repopulate combos.  Preserves current key selection."""
        prev_x = self._cmb_x.currentData()
        prev_y = self._cmb_y.currentData()

        self._cmb_x.blockSignals(True)
        self._cmb_y.blockSignals(True)
        self._cmb_x.clear()
        self._cmb_y.clear()
        for key, display in col_display:
            self._cmb_x.addItem(display, userData=key)
            self._cmb_y.addItem(display, userData=key)

        x_idx = self._cmb_x.findData(prev_x) if prev_x else -1
        y_idx = self._cmb_y.findData(prev_y) if prev_y else -1

        self._cmb_x.setCurrentIndex(x_idx if x_idx >= 0 else 0)
        default_y = y_idx if y_idx >= 0 else (1 if self._cmb_y.count() > 1 else 0)
        self._cmb_y.setCurrentIndex(default_y)

        self._cmb_x.blockSignals(False)
        self._cmb_y.blockSignals(False)

        self._x_key = self._cmb_x.currentData()
        self._y_key = self._cmb_y.currentData()
        self._update_axis_labels()

    def _on_axis_changed(self) -> None:
        self._x_key = self._cmb_x.currentData()
        self._y_key = self._cmb_y.currentData()
        self._x_div, self._x_pfx = 1.0, ""
        self._y_div, self._y_pfx = 1.0, ""
        self._update_axis_labels(self._x_pfx, self._y_pfx)
        self._dirty = True

    def _update_axis_labels(self, x_pfx: str = "", y_pfx: str = "") -> None:
        if self._x_key:
            lbl, unit = self._store.col_meta(self._x_key)
            full_unit = f"{x_pfx}{unit}" if (unit and x_pfx) else unit
            self._pi.setLabel("bottom", lbl, units=full_unit or None)
        if self._y_key:
            lbl, unit = self._store.col_meta(self._y_key)
            full_unit = f"{y_pfx}{unit}" if (unit and y_pfx) else unit
            self._pi.setLabel("left", lbl, units=full_unit or None)

    # Phases ------------------------------------------------------------------

    def ensure_phases(self, phases: List[str]) -> None:
        """Lazily create a PlotDataItem for each unseen phase."""
        for phase in phases:
            if phase not in self._curves:
                item = pg.PlotDataItem(
                    [], [],
                    pen=_make_pen(phase),
                    name=_PHASE_LABEL.get(phase, phase),
                    symbol=None,
                    antialias=True,
                )
                self._pi.addItem(item)
                self._curves[phase] = item

    # Hold / Resume -----------------------------------------------------------

    def _set_held(self, held: bool) -> None:
        self._held = held
        self._lbl_held.setVisible(held)
        self._btn_resume.setVisible(held)
        self._act_hold.setChecked(held)
        self._act_hold.setText("Resume" if held else "Hold")
        if not held:
            # Reset SI divisors so the next redraw recomputes the prefix fresh
            self._x_div, self._x_pfx = 1.0, ""
            self._y_div, self._y_pfx = 1.0, ""
            self._dirty = True

    def _on_manual_range_change(self, _axes) -> None:
        """Auto-hold when the user manually zooms or pans."""
        if not self._held:
            self._cb_auto.setChecked(False)
            self._set_held(True)

    # Linear Regression -------------------------------------------------------

    def _toggle_regression(self, active: bool) -> None:
        if active:
            self._set_held(True)        # regression implies hold
            self._start_regression()
        else:
            self._stop_regression()

    def _start_regression(self) -> None:
        self._regression_active = True

        # Default region: middle third of current view
        vb = self._pi.getViewBox()
        x0, x1 = vb.viewRange()[0]
        span = x1 - x0 if x1 > x0 else 1.0
        center = (x0 + x1) / 2
        region_vals = (center - span * 0.15, center + span * 0.15)

        self._lr_region = pg.LinearRegionItem(
            values=region_vals,
            brush=pg.mkBrush(255, 165, 0, 35),
            pen=pg.mkPen("#ff7f0e", width=1.5),
            movable=True,
        )
        self._lr_region.sigRegionChanged.connect(self._update_regression)
        self._pi.addItem(self._lr_region)

        self._lr_line = pg.PlotDataItem(
            [], [],
            pen=pg.mkPen("#ff7f0e", width=2, style=Qt.PenStyle.DashLine),
        )
        self._pi.addItem(self._lr_line)

        self._lr_text = pg.TextItem(
            "", anchor=(0.0, 1.0), color="#222222",
            fill=pg.mkBrush(255, 255, 255, 200),
        )
        self._lr_text.setZValue(10)
        self._pi.addItem(self._lr_text)

        self._update_regression()

    def _stop_regression(self) -> None:
        self._regression_active = False
        self._act_regression.setChecked(False)
        for item in (self._lr_region, self._lr_line, self._lr_text):
            if item is not None:
                self._pi.removeItem(item)
        self._lr_region = None
        self._lr_line = None
        self._lr_text = None

    def _update_regression(self) -> None:
        """Recompute linear fit inside the selected region and refresh display."""
        if not self._lr_region or not self._x_key or not self._y_key:
            return

        rmin, rmax = self._lr_region.getRegion()
        if rmax <= rmin:
            return

        # Collect all phase data (SI-scaled) within the region
        all_xs: List[np.ndarray] = []
        all_ys: List[np.ndarray] = []
        for phase in self._curves:
            xs, ys = self._store.get_xy(self._x_key, self._y_key, phase)
            if len(xs) == 0:
                continue
            xs_s = xs / self._x_div
            ys_s = ys / self._y_div
            mask = (xs_s >= rmin) & (xs_s <= rmax)
            if mask.sum() >= 2:
                all_xs.append(xs_s[mask])
                all_ys.append(ys_s[mask])

        if not all_xs:
            if self._lr_line:
                self._lr_line.setData([], [])
            if self._lr_text:
                self._lr_text.setText("(no data in region)")
            return

        xs_all = np.concatenate(all_xs)
        ys_all = np.concatenate(all_ys)
        n = len(xs_all)
        if n < 2:
            return

        # Linear fit
        coeffs = np.polyfit(xs_all, ys_all, 1)
        slope, intercept = coeffs

        # R²
        y_pred = np.polyval(coeffs, xs_all)
        ss_res = np.sum((ys_all - y_pred) ** 2)
        ss_tot = np.sum((ys_all - np.mean(ys_all)) ** 2)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0

        # Draw fit line
        x_plot = np.array([rmin, rmax])
        y_plot = np.polyval(coeffs, x_plot)
        if self._lr_line:
            self._lr_line.setData(x_plot, y_plot)

        # Build annotation text
        _, x_unit = self._store.col_meta(self._x_key)
        _, y_unit = self._store.col_meta(self._y_key)
        xu = f"{self._x_pfx}{x_unit}" if x_unit else ""
        yu = f"{self._y_pfx}{y_unit}" if y_unit else ""
        slope_unit = f" {yu}/{xu}" if (xu or yu) else ""
        int_unit   = f" {yu}" if yu else ""

        # Inverse slope  (flip numerator/denominator units)
        if abs(slope) > 1e-300:
            inv_slope_unit = f" {xu}/{yu}" if (xu or yu) else ""
            inv_line = f"\n1/slope   = {1.0/slope:.5g}{inv_slope_unit}"
        else:
            inv_line = "\n1/slope   = ∞"

        text = (
            f"slope      = {slope:.5g}{slope_unit}\n"
            f"intercept = {intercept:.5g}{int_unit}\n"
            f"R²          = {r2:.6f}   (N={n})"
            f"{inv_line}"
        )
        if self._lr_text:
            self._lr_text.setText(text)
            # Anchor text at the top of the fit line inside the region
            self._lr_text.setPos(rmin, float(np.max(y_plot)))

    # Redraw ------------------------------------------------------------------

    def mark_dirty(self) -> None:
        self._dirty = True

    def redraw(self) -> None:
        # When held: curves are frozen; regression still tracks region drags
        # (those come directly via sigRegionChanged, no action needed here)
        if self._held:
            return

        if not self._dirty:
            return
        self._dirty = False

        x_key, y_key = self._x_key, self._y_key
        if not x_key or not y_key:
            return

        auto = self._cb_auto.isChecked()

        # Collect all phase arrays
        all_xy = {
            phase: self._store.get_xy(x_key, y_key, phase)
            for phase in self._curves
        }

        # ── SI prefix (only when auto-range is on) ───────────────────────
        if auto:
            _, x_unit = self._store.col_meta(x_key)
            _, y_unit = self._store.col_meta(y_key)

            if x_unit:
                all_x = np.concatenate(
                    [xs for xs, _ in all_xy.values() if len(xs) > 0] or [np.empty(0)]
                )
                x_div, x_pfx = _best_si(all_x)
            else:
                x_div, x_pfx = 1.0, ""

            if y_unit:
                all_y = np.concatenate(
                    [ys for _, ys in all_xy.values() if len(ys) > 0] or [np.empty(0)]
                )
                y_div, y_pfx = _best_si(all_y)
            else:
                y_div, y_pfx = 1.0, ""

            if x_pfx != self._x_pfx or y_pfx != self._y_pfx:
                self._x_div, self._x_pfx = x_div, x_pfx
                self._y_div, self._y_pfx = y_div, y_pfx
                self._update_axis_labels(x_pfx, y_pfx)

        # ── Push scaled data to curves ───────────────────────────────────
        for phase, curve in self._curves.items():
            xs, ys = all_xy[phase]
            if len(xs) > 0 and len(ys) > 0:
                curve.setData(xs / self._x_div, ys / self._y_div)
            else:
                curve.setData([], [])

        if auto:
            self._pw.enableAutoRange()

        # Regression line tracks new data even when not held
        if self._regression_active and self._lr_region is not None:
            self._update_regression()

    def clear_curves(self) -> None:
        self._stop_regression()
        for curve in self._curves.values():
            curve.setData([], [])
        self._x_div, self._x_pfx = 1.0, ""
        self._y_div, self._y_pfx = 1.0, ""
        self._update_axis_labels("", "")
        self._set_held(False)
        self._dirty = False

    def reset_phases(self) -> None:
        """Remove all phase curves (for new session with different phases)."""
        self._stop_regression()
        for item in self._curves.values():
            self._pi.removeItem(item)
        self._curves.clear()
        self._x_div, self._x_pfx = 1.0, ""
        self._y_div, self._y_pfx = 1.0, ""
        self._set_held(False)
        self._dirty = False


# ──────────────────────────────────────────────────────────
# GraphWindow
# ──────────────────────────────────────────────────────────

class GraphWindow(QWidget):
    """Floating real-time graph window.

    Workflow:
        # At sweep start (single or double):
        graph_win.begin_session(columns)
            # columns: [(key, label, unit), ...]

        # Each measurement step (GUI thread):
        graph_win.append_point(GraphDataPoint(values={...}, phase="trace"))

    The window accumulates all data so opening it mid-sweep shows full history.
    Rendering is decoupled from measurement rate via a 30-fps QTimer.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Graph")
        self.resize(920, 720)

        self._store = DataStore()
        self._panels: List[GraphPanel] = []

        self._build_ui()
        self._add_panel()   # Start with one panel by default

        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(33)      # ~30 fps
        self._redraw_timer.timeout.connect(self._redraw_all)
        self._redraw_timer.start()

    # UI ----------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Toolbar
        bar = QHBoxLayout()
        btn_add = QPushButton("＋ Add Graph")
        btn_add.setToolTip("Add a new graph panel")
        btn_add.clicked.connect(self._add_panel)
        btn_clear = QPushButton("Clear Data")
        btn_clear.setToolTip("Clear all stored data (does not stop sweep)")
        btn_clear.clicked.connect(self._clear_data)
        bar.addWidget(btn_add)
        bar.addWidget(btn_clear)
        bar.addStretch()
        root.addLayout(bar)

        # Scrollable panel container
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll_content = QWidget()
        self._panel_layout = QVBoxLayout(self._scroll_content)
        self._panel_layout.setSpacing(6)
        self._panel_layout.setContentsMargins(0, 0, 0, 0)
        self._panel_layout.addStretch()     # keeps panels top-aligned
        self._scroll.setWidget(self._scroll_content)
        root.addWidget(self._scroll)

    # Session -----------------------------------------------------------------

    def begin_session(self, columns: List[Tuple[str, str, str]]) -> None:
        """Call once at sweep start to reset data and set column schema.

        columns: [(key, label, unit), ...]
          key   — stable identifier used in GraphDataPoint.values
          label — human-readable axis label
          unit  — physical unit string
        """
        self._store.clear()
        self._store.set_columns(columns)
        col_display = self._store.col_display_list()
        for panel in self._panels:
            panel.reset_phases()
            panel.update_columns(col_display)
            panel.ensure_phases(["_"])      # pre-create single-sweep curve

    # Data entry point --------------------------------------------------------

    def append_point(self, point: GraphDataPoint) -> None:
        """Append one measurement step.  Must be called from the GUI thread."""
        phase = point.phase or "_"
        self._store.append(point)
        for panel in self._panels:
            panel.ensure_phases([phase])   # idempotent: no-op if curve exists
            panel.mark_dirty()

    # Panel management --------------------------------------------------------

    def _add_panel(self) -> None:
        panel = GraphPanel(self._store)
        panel.remove_requested.connect(self._remove_panel)
        col_display = self._store.col_display_list()
        if col_display:
            panel.update_columns(col_display)
        panel.ensure_phases(self._store.phases() or ["_"])
        # Insert before the trailing stretch item
        self._panel_layout.insertWidget(self._panel_layout.count() - 1, panel)
        self._panels.append(panel)

    def _remove_panel(self, panel: GraphPanel) -> None:
        if len(self._panels) <= 1:
            return     # always keep at least one
        self._panels.remove(panel)
        self._panel_layout.removeWidget(panel)
        panel.deleteLater()

    def _clear_data(self) -> None:
        self._store.clear()
        for panel in self._panels:
            panel.reset_phases()
            panel.ensure_phases(["_"])

    # Redraw ------------------------------------------------------------------

    def _redraw_all(self) -> None:
        for panel in self._panels:
            panel.redraw()
