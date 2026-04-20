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
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
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


# Extra-Y trace colors (cycles when > 8 extra Ys are added)
_EXTRA_Y_COLORS: List[str] = [
    "#ff7f0e",   # orange
    "#2ca02c",   # green
    "#9467bd",   # purple
    "#8c564b",   # brown
    "#e377c2",   # pink
    "#bcbd22",   # yellow-green
    "#17becf",   # cyan
    "#7f7f7f",   # gray
]

# Extra Y uses the same phase line-styles but in its own color
_EXTRA_PHASE_STYLES: Dict[str, Qt.PenStyle] = {
    "_":       Qt.PenStyle.SolidLine,
    "trace":   Qt.PenStyle.SolidLine,
    "retrace": Qt.PenStyle.DashLine,
    "dummy":   Qt.PenStyle.DotLine,
}


def _make_extra_pen(color: str, phase: str) -> pg.mkPen:
    style = _EXTRA_PHASE_STYLES.get(phase, Qt.PenStyle.SolidLine)
    return pg.mkPen(color=color, width=2, style=style)


class _ExtraYState:
    """State for one additional Y trace row inside GraphPanel."""
    def __init__(self, color: str) -> None:
        self.key: Optional[str] = None
        self.unit: str = ""
        self.uses_main_vb: bool = True   # True → same unit as main Y, shares left axis
        self.vb: Optional[pg.ViewBox] = None   # right-side VB when uses_main_vb=False
        self.curves: Dict[str, pg.PlotDataItem] = {}   # phase → PlotDataItem
        self.y_div: float = 1.0
        self.y_pfx: str = ""
        self.color: str = color
        # UI references (set in _add_extra_y)
        self.combo: Optional[QComboBox] = None
        self.row_widget: Optional[QWidget] = None


# ──────────────────────────────────────────────────────────
# 2D Map colormaps  (Origin-style + common scientific)
# ──────────────────────────────────────────────────────────

def _make_cm(pos, colors) -> pg.ColorMap:
    return pg.ColorMap(
        pos=np.array(pos, dtype=np.float64),
        color=np.array(colors, dtype=np.uint8),
    )

_CMAPS: Dict[str, pg.ColorMap] = {
    # Origin "Warming": Blue → White → Red  (diverging, 빨강-흰색-파랑)
    "Warming":  _make_cm(
        [0.0,           0.5,              1.0],
        [[0, 0, 200, 255], [255, 255, 255, 255], [200, 0, 0, 255]],
    ),
    # Jet / Rainbow
    "Jet":      _make_cm(
        [0.0,               0.25,              0.5,             0.75,              1.0],
        [[0, 0, 255, 255], [0, 255, 255, 255], [0, 255, 0, 255], [255, 255, 0, 255], [255, 0, 0, 255]],
    ),
    # Viridis (approximation)
    "Viridis":  _make_cm(
        [0.0,                  0.25,                  0.5,                   0.75,                  1.0],
        [[68, 1, 84, 255], [59, 82, 139, 255], [33, 145, 140, 255], [94, 201, 98, 255], [253, 231, 37, 255]],
    ),
    # Inferno (approximation)
    "Inferno":  _make_cm(
        [0.0,              0.25,                  0.5,                   0.75,                   1.0],
        [[0, 0, 4, 255], [87, 16, 110, 255], [188, 55, 84, 255], [249, 142, 9, 255], [252, 255, 164, 255]],
    ),
    # Grayscale
    "Gray":     _make_cm([0.0, 1.0], [[0, 0, 0, 255], [255, 255, 255, 255]]),
    # RdBu diverging (Red → White → Blue)
    "RdBu":     _make_cm(
        [0.0,                    0.25,                      0.5,                      0.75,                       1.0],
        [[178, 24, 43, 255], [239, 138, 98, 255], [255, 255, 255, 255], [103, 169, 207, 255], [33, 102, 172, 255]],
    ),
}
_CMAP_NAMES = list(_CMAPS.keys())

def _get_lut(name: str) -> np.ndarray:
    cm = _CMAPS.get(name, _CMAPS["Warming"])
    return cm.getLookupTable(nPts=512, alpha=False)


# ──────────────────────────────────────────────────────────
# .dat file reader  (shared utility)
# ──────────────────────────────────────────────────────────

def _read_dat_file(path: Path):
    """Return (col_names: list[str], rows: list[list[float]]).
    Line 1 = column names, Line 2 = units (skipped), Line 3+ = data.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) < 3:
            return [], []
        names = lines[0].rstrip("\n").split("\t")
        rows = []
        for line in lines[2:]:
            parts = line.rstrip("\n").split("\t")
            if not any(parts):
                continue
            try:
                row = [float(p) for p in parts]
                if len(row) == len(names):
                    rows.append(row)
            except ValueError:
                pass
        return names, rows
    except Exception:
        return [], []


# ──────────────────────────────────────────────────────────
# Background file-load worker
# ──────────────────────────────────────────────────────────

class _MapLoadWorker(QObject):
    """Scans a folder for .dat files and loads them in a background thread."""
    finished = Signal(dict)   # {"col_names": [...], "files": [(fname, rows), ...]}
    error    = Signal(str)
    progress = Signal(str)

    def __init__(self, folder: str):
        super().__init__()
        self._folder = folder

    @Slot()
    def run(self):
        try:
            result = self._load()
            self.finished.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))

    def _load(self) -> dict:
        folder = Path(self._folder)
        files = sorted(folder.rglob("*.dat"))
        if not files:
            raise ValueError(f"No .dat files found in:\n{folder}")
        col_names = None
        file_data = []
        for fpath in files:
            names, rows = _read_dat_file(fpath)
            if not names or not rows:
                continue
            if col_names is None:
                col_names = names
            file_data.append((fpath.name, rows))
        if not col_names:
            raise ValueError("Valid .dat files not found (check format).")
        return {"col_names": col_names, "files": file_data}


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
      _data : {phase_tag: {col_key: {"list": [float, ...], "arr": ndarray|None}}}

    numpy 캐시 전략:
      append() 시 해당 키의 "arr" 캐시를 None으로 무효화.
      get_xy() 시 캐시가 None이면 한 번만 np.asarray()로 재생성.
      → redraw 타이머(30fps)가 데이터 변경 없이 반복 호출해도 O(1) 반환.
      → 스텝당 데이터 증가에 따른 GIL 점유 시간 증가 문제 해결.
    """

    _EMPTY = np.empty(0, dtype=np.float64)

    def __init__(self) -> None:
        self._meta: Dict[str, Tuple[str, str]] = {}
        self._data: Dict[str, Dict[str, dict]] = {}

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
            entry = bucket.get(k)
            if entry is None:
                bucket[k] = {"list": [v], "arr": None}
            else:
                entry["list"].append(v)
                entry["arr"] = None  # 캐시 무효화 — 다음 get_xy()에서 재생성

    def _get_arr(self, bucket: dict, key: str) -> np.ndarray:
        """캐시된 numpy 배열 반환. 캐시 없으면 재생성 (스텝당 최대 1회)."""
        entry = bucket.get(key)
        if entry is None:
            return self._EMPTY
        if entry["arr"] is None:
            entry["arr"] = np.asarray(entry["list"], dtype=np.float64)
        return entry["arr"]

    def get_xy(self, x_key: str, y_key: str, phase: str) -> Tuple[np.ndarray, np.ndarray]:
        d = self._data.get(phase, {})
        xs = self._get_arr(d, x_key)
        ys = self._get_arr(d, y_key)
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
        # Extra Y state
        self._extra_ys: List[_ExtraYState] = []
        # unit → (ViewBox, AxisItem, layout_col)  for separate-scale right axes
        self._unit_vbs: Dict[str, Tuple[pg.ViewBox, pg.AxisItem, int]] = {}
        self._next_axis_col: int = 3   # cols: 0=left-axis, 1=viewbox, 2=pg right-axis; extra start at 3
        self._free_axis_cols: List[int] = []

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

        self._btn_add_y = QPushButton("+ Y")
        self._btn_add_y.setFixedHeight(22)
        self._btn_add_y.setFixedWidth(38)
        self._btn_add_y.setToolTip("Add another Y-axis trace (same unit → shared scale; different unit → separate right axis)")
        self._btn_add_y.clicked.connect(self._add_extra_y)
        hdr.addWidget(self._btn_add_y)

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

        # ── Extra Y rows container (populated dynamically) ───────────────────
        self._extra_y_container = QWidget()
        self._extra_y_vlayout = QVBoxLayout(self._extra_y_container)
        self._extra_y_vlayout.setContentsMargins(0, 0, 0, 0)
        self._extra_y_vlayout.setSpacing(2)
        root.addWidget(self._extra_y_container)

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

        vb.menu.addSeparator()
        self._act_save_img = QAction("Save Panel Image…", vb.menu)
        self._act_save_img.triggered.connect(self._save_panel_image)
        vb.menu.addAction(self._act_save_img)

        # ── Auto-hold on manual zoom / pan ───────────────────────────────────
        vb.sigRangeChangedManually.connect(self._on_manual_range_change)

        # Keep extra ViewBoxes aligned when the plot is resized
        vb.sigResized.connect(self._sync_extra_vb_geometry)

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

        # Refresh extra Y combos (preserve selections)
        for state in self._extra_ys:
            if state.combo is None:
                continue
            prev_key = state.combo.currentData()
            state.combo.blockSignals(True)
            state.combo.clear()
            for key, display in col_display:
                state.combo.addItem(display, userData=key)
            idx = state.combo.findData(prev_key) if prev_key else -1
            state.combo.setCurrentIndex(idx if idx >= 0 else 0)
            state.combo.blockSignals(False)
            if state.combo.currentData() != state.key:
                self._on_extra_y_key_changed(state)

    def _on_axis_changed(self) -> None:
        self._x_key = self._cmb_x.currentData()
        self._y_key = self._cmb_y.currentData()
        self._x_div, self._x_pfx = 1.0, ""
        self._y_div, self._y_pfx = 1.0, ""
        self._update_axis_labels(self._x_pfx, self._y_pfx)
        self._dirty = True
        self._reconfigure_extra_y_vbs()

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
        """Lazily create a PlotDataItem for each unseen phase (main Y + all extra Ys)."""
        for phase in phases:
            if phase not in self._curves:
                color, _ = _PHASE_STYLE.get(phase, ("#888888", Qt.PenStyle.SolidLine))
                item = pg.PlotDataItem(
                    [], [],
                    pen=_make_pen(phase),
                    name=_PHASE_LABEL.get(phase, phase),
                    symbol='o',
                    symbolSize=5,
                    symbolBrush=pg.mkBrush(color),
                    symbolPen=None,
                    antialias=True,
                )
                self._pi.addItem(item)
                self._curves[phase] = item
        # Also create curves for each extra Y
        for state in self._extra_ys:
            self._ensure_phases_for_extra(state, phases)

    # Extra Y management -------------------------------------------------------

    def _add_extra_y(self) -> None:
        color = _EXTRA_Y_COLORS[len(self._extra_ys) % len(_EXTRA_Y_COLORS)]
        state = _ExtraYState(color)

        row_w = QWidget()
        row_l = QHBoxLayout(row_w)
        row_l.setContentsMargins(28, 0, 0, 0)
        row_l.setSpacing(6)

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 10px;")
        row_l.addWidget(dot)
        row_l.addWidget(QLabel("Y+:"))

        combo = QComboBox()
        combo.setFont(_MONO)
        combo.setMinimumWidth(150)
        for key, display in self._store.col_display_list():
            combo.addItem(display, userData=key)
        main_idx = combo.findData(self._y_key)
        default_idx = (main_idx + 1) % combo.count() if combo.count() > 1 else 0
        combo.setCurrentIndex(default_idx)
        state.combo = combo

        btn_del = QPushButton("✕")
        btn_del.setFixedWidth(26)
        btn_del.setToolTip("Remove this Y trace")
        btn_del.clicked.connect(lambda _=False, s=state: self._remove_extra_y(s))

        row_l.addWidget(combo)
        row_l.addWidget(btn_del)
        row_l.addStretch()
        state.row_widget = row_w
        self._extra_y_vlayout.addWidget(row_w)

        self._extra_ys.append(state)
        self._on_extra_y_key_changed(state)   # initialize key/unit/vb
        # Connect after init to avoid double-trigger
        combo.currentIndexChanged.connect(lambda _idx=None, s=state: self._on_extra_y_key_changed(s))
        self._dirty = True

    def _remove_extra_y(self, state: _ExtraYState) -> None:
        if state not in self._extra_ys:
            return
        self._extra_ys.remove(state)
        self._remove_extra_y_curves(state)
        if not state.uses_main_vb and state.unit:
            self._release_unit_vb(state.unit)
        if state.row_widget is not None:
            self._extra_y_vlayout.removeWidget(state.row_widget)
            state.row_widget.deleteLater()
        self._dirty = True

    def _on_extra_y_key_changed(self, state: _ExtraYState) -> None:
        new_key = state.combo.currentData() if state.combo else None
        if new_key == state.key:
            return
        _, main_unit = self._store.col_meta(self._y_key) if self._y_key else ("", "")
        _, new_unit = self._store.col_meta(new_key) if new_key else ("", "")
        old_unit = state.unit
        old_uses_main = state.uses_main_vb
        new_uses_main = (new_unit == main_unit)

        self._remove_extra_y_curves(state)

        # Release old separate VB only if we're switching away from that unit
        if not old_uses_main and old_unit and old_unit != new_unit:
            state.unit = new_unit          # update BEFORE release so check sees new state
            state.uses_main_vb = new_uses_main
            self._release_unit_vb(old_unit)
        else:
            state.unit = new_unit
            state.uses_main_vb = new_uses_main

        state.key = new_key
        state.y_div, state.y_pfx = 1.0, ""
        if not new_uses_main and new_unit:
            state.vb = self._get_or_create_unit_vb(new_unit)
        else:
            state.vb = None

        self._ensure_phases_for_extra(state, list(self._curves.keys()))
        self._dirty = True

    def _ensure_phases_for_extra(self, state: _ExtraYState, phases: List[str]) -> None:
        legend = self._pi.legend
        lbl, _ = self._store.col_meta(state.key) if state.key else (str(state.key), "")
        for phase in phases:
            if phase in state.curves:
                continue
            phase_label = _PHASE_LABEL.get(phase, phase)
            name = f"{lbl} ({phase_label})" if phase_label not in ("single", "_") else lbl
            curve = pg.PlotDataItem(
                [], [],
                pen=_make_extra_pen(state.color, phase),
                name=name,
                symbol='o',
                symbolSize=5,
                symbolBrush=pg.mkBrush(state.color),
                symbolPen=None,
                antialias=True,
            )
            if state.uses_main_vb:
                self._pi.addItem(curve)
            elif state.vb is not None:
                state.vb.addItem(curve)
                if legend is not None:
                    try:
                        legend.addItem(curve, name)
                    except Exception:
                        pass
            state.curves[phase] = curve

    def _remove_extra_y_curves(self, state: _ExtraYState) -> None:
        legend = self._pi.legend
        for phase, curve in state.curves.items():
            if state.uses_main_vb:
                try:
                    self._pi.removeItem(curve)
                except Exception:
                    pass
            elif state.vb is not None:
                try:
                    state.vb.removeItem(curve)
                except Exception:
                    pass
                if legend is not None:
                    try:
                        legend.removeItem(curve)
                    except Exception:
                        pass
        state.curves.clear()

    def _get_or_create_unit_vb(self, unit: str) -> pg.ViewBox:
        if unit in self._unit_vbs:
            return self._unit_vbs[unit][0]
        col = self._free_axis_cols.pop() if self._free_axis_cols else self._next_axis_col
        if not self._free_axis_cols:
            self._next_axis_col = max(self._next_axis_col, col + 1)
        vb = pg.ViewBox()
        self._pi.scene().addItem(vb)
        vb.setXLink(self._pi.getViewBox())
        axis = pg.AxisItem('right')
        axis.linkToView(vb)
        self._pi.layout.addItem(axis, 2, col)
        axis.setLabel(unit)
        vb.setGeometry(self._pi.getViewBox().sceneBoundingRect())
        self._unit_vbs[unit] = (vb, axis, col)
        return vb

    def _release_unit_vb(self, unit: str) -> None:
        still_used = any(
            ey.unit == unit and not ey.uses_main_vb
            for ey in self._extra_ys
        )
        if still_used or unit not in self._unit_vbs:
            return
        vb, axis, col = self._unit_vbs.pop(unit)
        try:
            self._pi.layout.removeItem(axis)
            self._pi.scene().removeItem(vb)
        except Exception:
            pass
        self._free_axis_cols.append(col)

    def _reconfigure_extra_y_vbs(self) -> None:
        """Reassign extra Y ViewBoxes after main Y unit changes."""
        _, main_unit = self._store.col_meta(self._y_key) if self._y_key else ("", "")
        for state in self._extra_ys:
            if state.key is None:
                continue
            _, ey_unit = self._store.col_meta(state.key)
            should_use_main = (ey_unit == main_unit)
            if should_use_main == state.uses_main_vb:
                continue
            self._remove_extra_y_curves(state)
            old_unit = state.unit
            state.uses_main_vb = should_use_main
            state.unit = ey_unit
            state.y_div, state.y_pfx = 1.0, ""
            if should_use_main:
                state.vb = None
                self._release_unit_vb(old_unit)
            else:
                state.vb = self._get_or_create_unit_vb(ey_unit)
            self._ensure_phases_for_extra(state, list(self._curves.keys()))
        self._dirty = True

    def _sync_extra_vb_geometry(self) -> None:
        """Keep right-side ViewBoxes aligned with the main plot area."""
        try:
            rect = self._pi.getViewBox().sceneBoundingRect()
            for vb, axis, col in self._unit_vbs.values():
                vb.setGeometry(rect)
        except Exception:
            pass

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

        # ── Push scaled data to main Y curves ───────────────────────────────
        for phase, curve in self._curves.items():
            xs, ys = all_xy[phase]
            if len(xs) > 0 and len(ys) > 0:
                curve.setData(xs / self._x_div, ys / self._y_div)
            else:
                curve.setData([], [])

        if auto:
            self._pw.enableAutoRange()

        # ── Extra Y curves ───────────────────────────────────────────────
        for state in self._extra_ys:
            if not state.key:
                continue
            all_xy_extra = {
                phase: self._store.get_xy(x_key, state.key, phase)
                for phase in state.curves
            }
            if auto and not state.uses_main_vb:
                _, ey_unit = self._store.col_meta(state.key)
                if ey_unit:
                    all_ey = np.concatenate(
                        [ys for _, ys in all_xy_extra.values() if len(ys) > 0]
                        or [np.empty(0)]
                    )
                    ey_div, ey_pfx = _best_si(all_ey)
                else:
                    ey_div, ey_pfx = 1.0, ""
                if ey_pfx != state.y_pfx:
                    state.y_div, state.y_pfx = ey_div, ey_pfx
                    if state.unit in self._unit_vbs:
                        _, axis, _ = self._unit_vbs[state.unit]
                        full = f"{ey_pfx}{state.unit}" if ey_pfx else state.unit
                        axis.setLabel(full)

            # Same-VB extra Ys reuse the main Y's SI divisor (same unit)
            y_div = self._y_div if state.uses_main_vb else state.y_div
            for phase, curve in state.curves.items():
                xs, ys = all_xy_extra.get(phase, (np.empty(0), np.empty(0)))
                if len(xs) > 0 and len(ys) > 0:
                    curve.setData(xs / self._x_div, ys / y_div)
                else:
                    curve.setData([], [])

        # Auto-range + geometry sync for right-side ViewBoxes
        if auto:
            for vb, axis, col in self._unit_vbs.values():
                vb.enableAutoRange()
        self._sync_extra_vb_geometry()

        # Regression line tracks new data even when not held
        if self._regression_active and self._lr_region is not None:
            self._update_regression()

    def _save_panel_image(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            None, "Save Panel Image", "", "PNG Image (*.png);;All Files (*)"
        )
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        pixmap = self._pw.grab()
        if not pixmap.save(path, "PNG"):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(None, "Save Failed", f"Could not save image to:\n{path}")

    def clear_curves(self) -> None:
        self._stop_regression()
        for curve in self._curves.values():
            curve.setData([], [])
        for state in self._extra_ys:
            for curve in state.curves.values():
                curve.setData([], [])
            state.y_div, state.y_pfx = 1.0, ""
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
        # Also reset extra Y curves (remove from their VBs, then recreate empty)
        for state in self._extra_ys:
            self._remove_extra_y_curves(state)
            state.y_div, state.y_pfx = 1.0, ""
        self._x_div, self._x_pfx = 1.0, ""
        self._y_div, self._y_pfx = 1.0, ""
        self._set_held(False)
        self._dirty = False


# ──────────────────────────────────────────────────────────
# MapPanel  — 2D colour-map from saved .dat files
# ──────────────────────────────────────────────────────────

class MapPanel(QFrame):
    """Right panel: 2D colour-map loaded from saved .dat files.

    Workflow:
      1. Browse to a folder containing .dat files (e.g. trace/YYYY-MM-DD/).
      2. Select X / Y / Z columns.
      3. Adjust range / colormap.
      4. Click "Plot" — background thread loads files, UI renders.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._col_names: List[str] = []
        self._worker: Optional[_MapLoadWorker] = None
        self._worker_thread: Optional[QThread] = None
        self._build_ui()

    # ── UI ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # ── Base folder row ──────────────────────────────
        base_row = QHBoxLayout()
        base_row.addWidget(QLabel("Base:"))
        self._le_base = QLineEdit()
        self._le_base.setFont(_MONO)
        self._le_base.setPlaceholderText("main_folder/custom_folder 경로")
        self._le_base.textChanged.connect(self._on_base_changed)
        base_row.addWidget(self._le_base)
        btn_browse = QPushButton("…")
        btn_browse.setFixedWidth(28)
        btn_browse.clicked.connect(self._browse_base)
        base_row.addWidget(btn_browse)
        root.addLayout(base_row)

        # ── Phase / Date row ─────────────────────────────
        phase_row = QHBoxLayout()
        phase_row.addWidget(QLabel("Phase:"))
        self._cb_phase = QComboBox()
        self._cb_phase.setFont(_MONO)
        self._cb_phase.setMinimumWidth(90)
        self._cb_phase.currentTextChanged.connect(self._on_phase_changed)
        phase_row.addWidget(self._cb_phase)
        phase_row.addSpacing(8)
        phase_row.addWidget(QLabel("Date:"))
        self._cb_date = QComboBox()
        self._cb_date.setFont(_MONO)
        self._cb_date.setMinimumWidth(110)
        phase_row.addWidget(self._cb_date)
        btn_refresh = QPushButton("↻")
        btn_refresh.setFixedWidth(28)
        btn_refresh.setToolTip("폴더 다시 스캔")
        btn_refresh.clicked.connect(lambda: self._on_base_changed(self._le_base.text()))
        phase_row.addWidget(btn_refresh)
        phase_row.addStretch()
        root.addLayout(phase_row)

        # ── Column selectors ────────────────────────────
        col_row = QHBoxLayout()
        col_row.addWidget(QLabel("X:"))
        self._cb_x = QComboBox(); self._cb_x.setFont(_MONO); self._cb_x.setMinimumWidth(100)
        col_row.addWidget(self._cb_x)
        col_row.addSpacing(8)
        col_row.addWidget(QLabel("Y:"))
        self._cb_y = QComboBox(); self._cb_y.setFont(_MONO); self._cb_y.setMinimumWidth(100)
        col_row.addWidget(self._cb_y)
        col_row.addSpacing(8)
        col_row.addWidget(QLabel("Z:"))
        self._cb_z = QComboBox(); self._cb_z.setFont(_MONO); self._cb_z.setMinimumWidth(100)
        col_row.addWidget(self._cb_z)
        col_row.addStretch()
        root.addLayout(col_row)

        # ── Range / colormap controls ────────────────────
        ctrl_row = QHBoxLayout()

        ctrl_row.addWidget(QLabel("Z min:"))
        self._le_zmin = QLineEdit(); self._le_zmin.setFont(_MONO); self._le_zmin.setFixedWidth(72)
        ctrl_row.addWidget(self._le_zmin)
        ctrl_row.addWidget(QLabel("max:"))
        self._le_zmax = QLineEdit(); self._le_zmax.setFont(_MONO); self._le_zmax.setFixedWidth(72)
        ctrl_row.addWidget(self._le_zmax)
        self._cb_auto_z = QCheckBox("Auto Z")
        self._cb_auto_z.setFont(_MONO)
        self._cb_auto_z.setChecked(True)
        self._cb_auto_z.stateChanged.connect(self._on_auto_z_changed)
        ctrl_row.addWidget(self._cb_auto_z)

        ctrl_row.addSpacing(12)
        ctrl_row.addWidget(QLabel("Colormap:"))
        self._cb_cmap = QComboBox()
        self._cb_cmap.setFont(_MONO)
        for name in _CMAP_NAMES:
            self._cb_cmap.addItem(name)
        ctrl_row.addWidget(self._cb_cmap)

        ctrl_row.addStretch()
        root.addLayout(ctrl_row)

        # ── Action row ──────────────────────────────────
        act_row = QHBoxLayout()
        self._btn_plot = QPushButton("Plot")
        self._btn_plot.setFont(_MONO)
        self._btn_plot.setFixedWidth(80)
        self._btn_plot.clicked.connect(self._on_plot)
        act_row.addWidget(self._btn_plot)
        self._lbl_status = QLabel("—")
        self._lbl_status.setFont(_MONO)
        self._lbl_status.setStyleSheet("color: #888888;")
        act_row.addWidget(self._lbl_status)
        act_row.addStretch()
        btn_save = QPushButton("Save Image…")
        btn_save.setFont(_MONO)
        btn_save.clicked.connect(self._save_image)
        act_row.addWidget(btn_save)
        root.addLayout(act_row)

        # ── pyqtgraph display ───────────────────────────
        self._gv = pg.GraphicsLayoutWidget()
        self._gv.setBackground("#0d1117")
        root.addWidget(self._gv, stretch=1)

        self._plot = self._gv.addPlot(row=0, col=0)
        self._plot.setDefaultPadding(0.02)
        self._img = pg.ImageItem()
        self._plot.addItem(self._img)

        self._cbar = pg.ColorBarItem(
            colorMap=_CMAPS["Warming"],
            label="",
            interactive=True,
        )
        self._cbar.setImageItem(self._img, insert_in=self._plot)

        # Initial state
        self._on_auto_z_changed()

    # ── Helpers ───────────────────────────────────────────

    def _on_auto_z_changed(self) -> None:
        manual = not self._cb_auto_z.isChecked()
        self._le_zmin.setEnabled(manual)
        self._le_zmax.setEnabled(manual)

    def _browse_base(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Base 폴더 선택 (main_folder/custom_folder)", self._le_base.text() or ""
        )
        if folder:
            self._le_base.setText(folder)

    def set_base_folder(self, base: str) -> None:
        """외부(DoubleSweepWindow)에서 base 경로 자동 설정."""
        if base:
            self._le_base.setText(base)

    def _on_base_changed(self, text: str) -> None:
        """Base 폴더가 바뀌면 phase 콤보박스를 재스캔."""
        base = Path(text.strip()) if text.strip() else None
        self._cb_phase.blockSignals(True)
        self._cb_phase.clear()
        if base and base.is_dir():
            for name in ("trace", "retrace", "dummy"):
                if (base / name).is_dir():
                    self._cb_phase.addItem(name)
        if self._cb_phase.count() == 0:
            self._cb_phase.addItem("(없음)")
        self._cb_phase.blockSignals(False)
        self._on_phase_changed(self._cb_phase.currentText())

    def _on_phase_changed(self, phase: str) -> None:
        """Phase 콤보박스가 바뀌면 date 서브폴더를 재스캔."""
        base = Path(self._le_base.text().strip())
        phase_dir = base / phase if base and base.is_dir() else None
        self._cb_date.blockSignals(True)
        prev_date = self._cb_date.currentText()
        self._cb_date.clear()
        if phase_dir and phase_dir.is_dir():
            date_dirs = sorted(
                [d.name for d in phase_dir.iterdir() if d.is_dir()],
                reverse=True,   # 최신 날짜 먼저
            )
            for d in date_dirs:
                self._cb_date.addItem(d)
        if self._cb_date.count() == 0:
            self._cb_date.addItem("(없음)")
        # Restore previous selection if still available
        idx = self._cb_date.findText(prev_date)
        if idx >= 0:
            self._cb_date.setCurrentIndex(idx)
        self._cb_date.blockSignals(False)

    def _get_target_folder(self) -> str:
        """현재 선택된 base/phase/date로부터 실제 .dat 폴더 경로 반환."""
        base = self._le_base.text().strip()
        phase = self._cb_phase.currentText()
        date = self._cb_date.currentText()
        if not base or phase.startswith("(") or date.startswith("("):
            return ""
        return str(Path(base) / phase / date)

    def _update_column_combos(self, col_names: List[str]) -> None:
        if col_names == self._col_names:
            return
        self._col_names = col_names
        prev = {
            "x": self._cb_x.currentText(),
            "y": self._cb_y.currentText(),
            "z": self._cb_z.currentText(),
        }
        for cb in (self._cb_x, self._cb_y, self._cb_z):
            cb.blockSignals(True)
            cb.clear()
            for name in col_names:
                cb.addItem(name)
            cb.blockSignals(False)
        # Restore or smart-default
        def _restore(cb, prev_val, default_idx):
            idx = cb.findText(prev_val)
            cb.setCurrentIndex(idx if idx >= 0 else min(default_idx, cb.count() - 1))
        _restore(self._cb_x, prev["x"], 0)
        _restore(self._cb_y, prev["y"], 1 if len(col_names) > 1 else 0)
        _restore(self._cb_z, prev["z"], len(col_names) - 1)

    # ── Plot action ───────────────────────────────────────

    def _on_plot(self) -> None:
        folder = self._get_target_folder()
        if not folder:
            self._lbl_status.setText("Base 폴더 / Phase / Date를 선택하세요.")
            return

        # Stop any running worker
        if self._worker_thread and self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait()

        self._btn_plot.setEnabled(False)
        self._lbl_status.setText("Loading files…")

        self._worker = _MapLoadWorker(folder)
        self._worker_thread = QThread()
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_data_loaded)
        self._worker.error.connect(self._on_load_error)
        self._worker_thread.start()

    @Slot(dict)
    def _on_data_loaded(self, data: dict) -> None:
        if self._worker_thread:
            self._worker_thread.quit()
        self._btn_plot.setEnabled(True)

        col_names: List[str] = data["col_names"]
        file_data = data["files"]   # [(fname, rows), ...]

        self._update_column_combos(col_names)

        x_col = self._cb_x.currentText()
        y_col = self._cb_y.currentText()
        z_col = self._cb_z.currentText()

        for col in (x_col, y_col, z_col):
            if col not in col_names:
                self._lbl_status.setText(f"Column '{col}' not found.")
                return

        xi = col_names.index(x_col)
        yi = col_names.index(y_col)
        zi = col_names.index(z_col)

        # Build per-file arrays; sort files by mean-Y
        file_rows = []
        for fname, rows in file_data:
            if not rows:
                continue
            arr = np.array(rows, dtype=np.float64)   # (n_pts, n_cols)
            file_rows.append((float(np.mean(arr[:, yi])), arr[:, xi], arr[:, zi]))

        if not file_rows:
            self._lbl_status.setText("데이터 없음.")
            return

        file_rows.sort(key=lambda t: t[0])  # sort by Y value

        # Use the longest x array as the common grid
        x_ref = max(file_rows, key=lambda t: len(t[1]))[1]
        x_ref = np.sort(x_ref)

        rows_2d = []
        y_vals = []
        for y_val, x_arr, z_arr in file_rows:
            sort_i = np.argsort(x_arr)
            z_interp = np.interp(x_ref, x_arr[sort_i], z_arr[sort_i],
                                 left=np.nan, right=np.nan)
            rows_2d.append(z_interp)
            y_vals.append(y_val)

        img = np.array(rows_2d, dtype=np.float64)   # (n_y, n_x)
        y_arr = np.array(y_vals, dtype=np.float64)

        self._render_image(img, x_ref, y_arr, x_col, y_col, z_col)

    @Slot(str)
    def _on_load_error(self, msg: str) -> None:
        if self._worker_thread:
            self._worker_thread.quit()
        self._btn_plot.setEnabled(True)
        self._lbl_status.setText(f"Error: {msg}")

    # ── Rendering ─────────────────────────────────────────

    def _render_image(
        self,
        img: np.ndarray,
        x_vals: np.ndarray,
        y_vals: np.ndarray,
        x_col: str,
        y_col: str,
        z_col: str,
    ) -> None:
        finite = img[np.isfinite(img)]
        if finite.size == 0:
            self._lbl_status.setText("유한한 데이터 없음.")
            return

        # Z range
        if self._cb_auto_z.isChecked():
            z_min, z_max = float(finite.min()), float(finite.max())
        else:
            try:
                z_min = float(self._le_zmin.text())
                z_max = float(self._le_zmax.text())
            except ValueError:
                z_min, z_max = float(finite.min()), float(finite.max())
        if z_min == z_max:
            z_max = z_min + 1.0

        # Update manual range fields
        self._le_zmin.setText(f"{z_min:.5g}")
        self._le_zmax.setText(f"{z_max:.5g}")

        # Apply colormap LUT
        lut = _get_lut(self._cb_cmap.currentText())
        self._img.setLookupTable(lut)
        self._cbar.setColorMap(_CMAPS.get(self._cb_cmap.currentText(), _CMAPS["Warming"]))

        # Scale image to [0, 255] for LUT
        img_disp = np.where(np.isfinite(img), img, z_min)
        img_scaled = ((img_disp - z_min) / (z_max - z_min) * 255).clip(0, 255).astype(np.float32)

        # img shape: (n_y, n_x) — ImageItem expects (width, height) = (n_x, n_y)
        self._img.setImage(img_scaled.T, autoLevels=False, levels=(0, 255))

        # Physical extent
        n_y, n_x = img.shape
        if n_x > 1:
            x0, x1 = float(x_vals[0]), float(x_vals[-1])
            dx = (x1 - x0) / (n_x - 1)
        else:
            x0, dx = float(x_vals[0]) if len(x_vals) else 0.0, 1.0
        if n_y > 1:
            y0, y1 = float(y_vals[0]), float(y_vals[-1])
            dy = (y1 - y0) / (n_y - 1)
        else:
            y0, dy = float(y_vals[0]) if len(y_vals) else 0.0, 1.0

        self._img.setRect(
            x0 - dx / 2, y0 - dy / 2,
            n_x * dx, n_y * dy,
        )

        self._plot.setLabel("bottom", x_col)
        self._plot.setLabel("left", y_col)
        self._cbar.setLevels((z_min, z_max))
        self._cbar.setLabel("right", z_col)

        self._lbl_status.setText(
            f"{n_y} 파일 × {n_x} 포인트 | Z [{z_min:.4g}, {z_max:.4g}]"
        )

    # ── Save image ────────────────────────────────────────

    def _save_image(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Map Image", "", "PNG Image (*.png);;All Files (*)"
        )
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        pixmap = self._gv.grab()
        if not pixmap.save(path, "PNG"):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Save Failed", f"Could not save:\n{path}")

    def closeEvent(self, event) -> None:
        if self._worker_thread and self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait()
        super().closeEvent(event)


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

        # ── Toolbar ──────────────────────────────────────
        bar = QHBoxLayout()
        btn_add = QPushButton("＋ Add Graph")
        btn_add.setToolTip("Add a new XY graph panel (left side)")
        btn_add.clicked.connect(self._add_panel)
        btn_clear = QPushButton("Clear Data")
        btn_clear.setToolTip("Clear all stored data (does not stop sweep)")
        btn_clear.clicked.connect(self._clear_data)
        btn_save_img = QPushButton("Save Image…")
        btn_save_img.setToolTip("Save the current graph as a PNG image (Ctrl+S)")
        btn_save_img.clicked.connect(self._save_image)
        bar.addWidget(btn_add)
        bar.addWidget(btn_clear)
        bar.addSpacing(12)
        self._btn_map = QPushButton("2D Map")
        self._btn_map.setCheckable(True)
        self._btn_map.setToolTip("오른쪽 패널을 2D colour-map 모드로 전환")
        self._btn_map.toggled.connect(self._toggle_map)
        bar.addWidget(self._btn_map)
        bar.addStretch()
        bar.addWidget(btn_save_img)
        root.addLayout(bar)

        # ── Splitter: left (XY panels) | right (MapPanel) ──
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left side: scrollable XY panels
        left_w = QWidget()
        left_lay = QVBoxLayout(left_w)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll_content = QWidget()
        self._panel_layout = QVBoxLayout(self._scroll_content)
        self._panel_layout.setSpacing(6)
        self._panel_layout.setContentsMargins(0, 0, 0, 0)
        self._panel_layout.addStretch()
        self._scroll.setWidget(self._scroll_content)
        left_lay.addWidget(self._scroll)
        self._splitter.addWidget(left_w)

        # Right side: MapPanel (hidden by default)
        self._map_panel = MapPanel()
        self._map_panel.setVisible(False)
        self._splitter.addWidget(self._map_panel)

        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        root.addWidget(self._splitter)

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

    def update_map_base(self, base_folder: str) -> None:
        """Double Sweep 시작 시 2D Map 패널의 base 폴더 자동 설정."""
        self._map_panel.set_base_folder(base_folder)

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

    def _toggle_map(self, checked: bool) -> None:
        self._map_panel.setVisible(checked)
        if checked:
            w = self.width()
            self._splitter.setSizes([w * 55 // 100, w * 45 // 100])
        else:
            self._splitter.setSizes([self.width(), 0])

    # Redraw ------------------------------------------------------------------

    def _redraw_all(self) -> None:
        for panel in self._panels:
            panel.redraw()

    def _save_image(self) -> None:
        """Export visible panels to a PNG file."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Graph Image", "", "PNG Image (*.png);;All Files (*)"
        )
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"

        pixmap = self._splitter.grab()
        if not pixmap.save(path, "PNG"):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Save Failed", f"Could not save image to:\n{path}")

    def keyPressEvent(self, event) -> None:
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_S):
            self._save_image()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)
