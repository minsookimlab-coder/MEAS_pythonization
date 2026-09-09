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
    QFormLayout,
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
        self.disp_bufs: Dict[str, "_DisplayBuf"] = {}  # phase → pre-scaled display buffer
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


class _DisplayBuf:
    """GraphPanel의 phase별 pre-scaled 표시 버퍼.

    구조:
      history [0 .. n-1] : step 압축 이력 (오래된 데이터일수록 해상도 낮아짐)
      live    [n]         : 항상 최신 포인트 (step에 무관하게 매 push 갱신)

    get()은 history + live를 합쳐서 반환 → 오래된 데이터 손실 없음 + 최신값 항상 표시.

    live_committed 플래그:
      True  → 최신 포인트가 이미 history에 들어 있음 → get()은 history[:n]만 반환
      False → history에 없음 → get()은 history[:n] + live 한 점 추가

    복잡도:
      push()     : O(1) amortized  (버퍼 만참 시 O(max_pts) 압축, O(log N)회)
      get()      : O(1) view
      rescale()  : O(max_pts), 극히 드물게 실행
      push_bulk(): O(N) numpy, 축 변경 시에만 실행
    """
    __slots__ = (
        '_max', '_x', '_y', '_n', '_step', '_total',
        '_live_x', '_live_y', '_has_live', '_live_committed',
    )

    def __init__(self, max_pts: int):
        self._max  = max_pts
        # +1: get()이 live 포인트를 overflow 슬롯에 쓰기 위한 여유 공간
        self._x    = np.empty(max_pts + 1, dtype=np.float64)
        self._y    = np.empty(max_pts + 1, dtype=np.float64)
        self._n    = 0
        self._step = 1
        self._total = 0
        self._live_x: float = 0.0
        self._live_y: float = 0.0
        self._has_live: bool = False
        self._live_committed: bool = True   # 초기: live 없음 → history만 반환

    def push(self, x: float, y: float) -> bool:
        """O(1) amortized. 항상 live 갱신; history는 step 스케줄에 따라 커밋."""
        self._total += 1
        self._live_x = x
        self._live_y = y
        self._has_live = True

        if self._total % self._step == 0:
            # 이번 포인트를 history에 커밋
            if self._n >= self._max:
                half = self._max // 2
                self._x[:half] = self._x[::2][:half]
                self._y[:half] = self._y[::2][:half]
                self._n   = half
                self._step *= 2
            self._x[self._n] = x
            self._y[self._n] = y
            self._n += 1
            self._live_committed = True   # history에 포함됨
        else:
            self._live_committed = False  # history에 없음 → get()이 덧붙임

        return True   # live는 항상 갱신 → 항상 dirty

    def get(self) -> Tuple[np.ndarray, np.ndarray]:
        """O(1) view. history[:n] + (live if not committed)."""
        n = self._n
        if not self._has_live or self._live_committed:
            return self._x[:n], self._y[:n]
        # live 포인트를 overflow 슬롯(index n)에 기록해 view로 반환
        self._x[n] = self._live_x
        self._y[n] = self._live_y
        return self._x[:n + 1], self._y[:n + 1]

    def reset(self) -> None:
        self._n = 0
        self._step = 1
        self._total = 0
        self._has_live = False
        self._live_committed = True

    def push_bulk(self, xs: np.ndarray, ys: np.ndarray) -> None:
        """축 변경 시 DataStore 전체를 일괄 로드. O(N) numpy 벡터 연산."""
        n = len(xs)
        if n == 0:
            return
        if n <= self._max:
            self._x[:n] = xs
            self._y[:n] = ys
            self._n     = n
            self._step  = 1
            self._total = n
        else:
            step = n // self._max
            idx  = np.arange(0, n, step)[:self._max]
            m    = len(idx)
            self._x[:m] = xs[idx]
            self._y[:m] = ys[idx]
            self._n     = m
            self._step  = step
            self._total = n
        if n > 0:
            self._live_x = float(xs[-1])
            self._live_y = float(ys[-1])
            self._has_live = True
            self._live_committed = True   # 마지막 점은 이미 history에 있음

    def rescale(self, x_factor: float, y_factor: float) -> None:
        """SI prefix 변경 시 호출. O(max_pts)."""
        if self._n > 0:
            self._x[:self._n] *= x_factor
            self._y[:self._n] *= y_factor
        if self._has_live:
            self._live_x *= x_factor
            self._live_y *= y_factor


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


def _plot_subsample(
    xs: np.ndarray, ys: np.ndarray, max_pts: int
) -> Tuple[np.ndarray, np.ndarray]:
    """Return (xs, ys) subsampled to at most max_pts points for display."""
    n = len(xs)
    if n <= max_pts:
        return xs, ys
    idx = np.round(np.linspace(0, n - 1, max_pts)).astype(np.intp)
    return xs[idx], ys[idx]


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

    _INIT_CAP = 256  # 초기 프리알로케이션 크기

    def append(self, point: GraphDataPoint) -> None:
        tag = point.phase or "_"
        bucket = self._data.setdefault(tag, {})
        for k, v in point.values.items():
            fv = float(v) if v is not None else float("nan")
            av = abs(fv) if fv == fv else 0.0  # nan check: nan != nan
            entry = bucket.get(k)
            if entry is None:
                arr = np.empty(self._INIT_CAP, dtype=np.float64)
                arr[0] = fv
                bucket[k] = {"arr": arr, "len": 1, "cap": self._INIT_CAP, "abs_max": av}
            else:
                n = entry["len"]
                if n >= entry["cap"]:
                    # capacity 두 배 확장 — amortized O(1)
                    new_cap = entry["cap"] * 2
                    new_arr = np.empty(new_cap, dtype=np.float64)
                    new_arr[:n] = entry["arr"][:n]
                    entry["arr"] = new_arr
                    entry["cap"] = new_cap
                entry["arr"][n] = fv
                entry["len"] = n + 1
                if av > entry["abs_max"]:
                    entry["abs_max"] = av

    def col_abs_max(self, key: str) -> float:
        """컬럼 key의 전체 phase에 걸친 최대 절댓값을 O(phases) 시간에 반환."""
        result = 0.0
        for bucket in self._data.values():
            entry = bucket.get(key)
            if entry and entry["abs_max"] > result:
                result = entry["abs_max"]
        return result

    def _get_arr(self, bucket: dict, key: str) -> np.ndarray:
        """유효 범위 numpy view 반환 — O(1), 변환 없음."""
        entry = bucket.get(key)
        if entry is None:
            return self._EMPTY
        return entry["arr"][:entry["len"]]

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
        # Display buffers: phase → _DisplayBuf (pre-scaled, bounded O(max_pts))
        self._disp_bufs: Dict[str, _DisplayBuf] = {}
        self._disp_buf_keys: Tuple[Optional[str], Optional[str]] = (None, None)
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
        # 앱이 자체적으로 SI 접두어를 계산해 단위에 붙이므로 pyqtgraph의
        # autoSIPrefix를 끈다. (끄지 않으면 'mV'에 또 'k'가 붙어 'kmV'가 됨)
        self._pi.getAxis("bottom").enableAutoSIPrefix(False)
        self._pi.getAxis("left").enableAutoSIPrefix(False)
        self._pi.addLegend(offset=(10, 10))
        root.addWidget(self._pw)
        self._setup_hover_readout()

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
        state.disp_bufs.clear()   # 키 변경 시 기존 display buffer 폐기
        if not new_uses_main and new_unit:
            state.vb = self._get_or_create_unit_vb(new_unit)
        else:
            state.vb = None

        self._ensure_phases_for_extra(state, list(self._curves.keys()))
        # 이미 측정된 기존 이력을 메모리(DataStore)에서 즉시 백필 →
        # 측정 도중 추가해도 처음부터의 데이터가 모두 보인다 (파일 읽기·측정 지연 없음).
        self._backfill_extra_y(state)
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
        axis.enableAutoSIPrefix(False)   # 앱이 직접 접두어를 붙이므로 이중 접두어 방지
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
            # SI divisor는 여기서 리셋하지 않는다.
            # 표시 버퍼는 항상 현재 _x_div/_y_div 로 미리 나눠진 값을 담는 불변식을 갖는다.
            # divisor만 1.0으로 바꾸고 버퍼를 그대로 두면 스케일이 어긋나,
            # Resume 이후 들어오는 새 점이 0 근처로 찍히는 버그가 생긴다.
            # prefix 재적응은 redraw()의 auto SI 블록이 rescale로 안전하게 처리한다.
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

    # Display buffer ----------------------------------------------------------

    def on_new_point(self, phase: str, values: Dict[str, float]) -> None:
        """새 데이터 포인트 — display buffer에 O(1) 삽입.

        push()가 True를 반환(=화면에 표시할 버퍼가 바뀐 경우)할 때만 _dirty=True.
        """
        x_key, y_key = self._x_key, self._y_key
        if not x_key or not y_key:
            self._dirty = True
            return

        if self._disp_buf_keys != (x_key, y_key):
            self._rebuild_disp_bufs()

        x_raw = values.get(x_key)
        y_raw = values.get(y_key)
        if x_raw is None or y_raw is None:
            return

        max_pts = max(self._pw.width(), 200)
        x_scaled = x_raw / self._x_div

        # 메인 Y 버퍼
        buf = self._disp_bufs.get(phase)
        if buf is None:
            buf = _DisplayBuf(max_pts)
            self._disp_bufs[phase] = buf
        if buf.push(x_scaled, y_raw / self._y_div):
            self._dirty = True

        # Extra Y 버퍼 (같은 x_scaled 재사용)
        for state in self._extra_ys:
            if not state.key:
                continue
            ey_raw = values.get(state.key)
            if ey_raw is None:
                continue
            y_div = self._y_div if state.uses_main_vb else state.y_div
            ey_buf = state.disp_bufs.get(phase)
            if ey_buf is None:
                ey_buf = _DisplayBuf(max_pts)
                state.disp_bufs[phase] = ey_buf
            if ey_buf.push(x_scaled, ey_raw / y_div):
                self._dirty = True

    def _backfill_extra_y(self, state: "_ExtraYState") -> None:
        """Extra Y 한 개를 DataStore의 기존 이력 전체로 채운다 (모든 phase 포함).

        - 데이터는 이미 메모리(DataStore)에 있으므로 .dat 파일 읽기가 전혀 필요 없다.
        - 측정 루프(on_new_point)가 아니라 '축 변경·Extra Y 추가/변경' 같은
          사용자 조작 시에만 호출된다 → 측정 주기/측정 시간에 영향 없음.
        - 비용은 해당 컬럼 1개에 대한 O(N) 1회뿐 (push_bulk = numpy 벡터 연산).
        """
        state.disp_bufs.clear()
        x_key = self._x_key
        if not x_key or not state.key:
            return
        max_pts = max(self._pw.width(), 200)
        x_div = self._x_div or 1.0
        # uses_main_vb면 메인 Y와 같은 divisor, 아니면 자기 divisor 사용
        ey_y_div = self._y_div if state.uses_main_vb else (state.y_div or 1.0)
        for phase in self._store.phases():
            xs, ys = self._store.get_xy(x_key, state.key, phase)
            n = min(len(xs), len(ys))
            if n == 0:
                continue
            ey_buf = _DisplayBuf(max_pts)
            ey_buf.push_bulk(xs[:n] / x_div, ys[:n] / ey_y_div)
            state.disp_bufs[phase] = ey_buf

    def _rebuild_disp_bufs(self) -> None:
        """축 변경 / 세션 재시작 시 DataStore 전체를 읽어 버퍼 재구성.
        O(N) 이지만 사용자 인터랙션 시에만 호출됨.
        push_bulk()로 numpy 벡터 연산 사용 → Python 루프 제거.
        """
        self._disp_bufs.clear()
        for state in self._extra_ys:
            state.disp_bufs.clear()

        x_key, y_key = self._x_key, self._y_key
        self._disp_buf_keys = (x_key, y_key)
        if not x_key or not y_key:
            return

        max_pts = max(self._pw.width(), 200)
        x_div = self._x_div or 1.0
        y_div = self._y_div or 1.0

        for phase in self._store.phases():
            xs, ys = self._store.get_xy(x_key, y_key, phase)
            if len(xs) == 0:
                continue
            buf = _DisplayBuf(max_pts)
            buf.push_bulk(xs / x_div, ys / y_div)   # numpy 벡터 연산, O(N) 이지만 빠름
            self._disp_bufs[phase] = buf

        # Extra Y들도 같은 DataStore 이력으로 백필 (각 state별 1회, trace/retrace/dummy 포함)
        for state in self._extra_ys:
            self._backfill_extra_y(state)

    def _rescale_disp_bufs(self, x_factor: float, y_factor: float) -> None:
        """SI prefix 변경 시 메인 Y + Extra Y 버퍼 재스케일. O(max_pts), 드물게 실행."""
        for buf in self._disp_bufs.values():
            buf.rescale(x_factor, y_factor)
        # Extra Y는 X 축이 메인과 동일하므로 x_factor만 적용
        if x_factor != 1.0:
            for state in self._extra_ys:
                for buf in state.disp_bufs.values():
                    buf.rescale(x_factor, 1.0)

    # Redraw ------------------------------------------------------------------

    def mark_dirty(self) -> None:
        self._dirty = True

    def redraw(self) -> None:
        """30 fps 렌더링 타이머에서 호출. display buffer 기반 O(1) 렌더링.

        측정 루프(on_new_point)와 렌더링이 완전히 분리되어 있어
        N이 아무리 커져도 렌더링 비용이 일정(O(max_pts))을 유지한다.
        """
        if self._held:
            return
        if not self._dirty:
            return
        self._dirty = False

        x_key, y_key = self._x_key, self._y_key
        if not x_key or not y_key:
            return

        # 축이 바뀐 경우 버퍼 재구성 (on_new_point가 호출되지 않아도 즉시 반영)
        if self._disp_buf_keys != (x_key, y_key):
            self._x_div, self._x_pfx = 1.0, ""
            self._y_div, self._y_pfx = 1.0, ""
            self._rebuild_disp_bufs()

        auto = self._cb_auto.isChecked()

        # ── SI prefix 갱신 (auto-range 시) ──────────────────────────────────
        # col_abs_max()는 append() 시 incremental로 관리 → O(phases) = O(1)
        if auto:
            _, x_unit = self._store.col_meta(x_key)
            _, y_unit = self._store.col_meta(y_key)
            x_div, x_pfx = _best_si(np.array([self._store.col_abs_max(x_key)])) if x_unit else (1.0, "")
            y_div, y_pfx = _best_si(np.array([self._store.col_abs_max(y_key)])) if y_unit else (1.0, "")

            if x_pfx != self._x_pfx or y_pfx != self._y_pfx:
                # 기존 버퍼를 새 prefix에 맞게 인플레이스 재스케일 — O(max_pts)
                x_factor = self._x_div / x_div if x_div else 1.0
                y_factor = self._y_div / y_div if y_div else 1.0
                self._x_div, self._x_pfx = x_div, x_pfx
                self._y_div, self._y_pfx = y_div, y_pfx
                self._rescale_disp_bufs(x_factor, y_factor)
                self._update_axis_labels(x_pfx, y_pfx)

        # ── 메인 Y 커브: display buffer → setData (O(max_pts) = O(const)) ──
        for phase, curve in self._curves.items():
            buf = self._disp_bufs.get(phase)
            if buf is not None and buf._n > 0:
                curve.setData(*buf.get())   # buf.get()은 numpy view, 복사 없음
            else:
                curve.setData([], [])

        if auto:
            self._pw.enableAutoRange()

        # ── Extra Y 커브 ─────────────────────────────────────────────────────
        for state in self._extra_ys:
            if not state.key:
                continue
            # separate-scale Extra Y: SI prefix 갱신
            if auto and not state.uses_main_vb:
                _, ey_unit = self._store.col_meta(state.key)
                ey_div, ey_pfx = _best_si(np.array([self._store.col_abs_max(state.key)])) if ey_unit else (1.0, "")
                if ey_pfx != state.y_pfx:
                    ey_factor = state.y_div / ey_div if ey_div else 1.0
                    state.y_div, state.y_pfx = ey_div, ey_pfx
                    for buf in state.disp_bufs.values():
                        buf.rescale(1.0, ey_factor)
                    if state.unit in self._unit_vbs:
                        _, axis, _ = self._unit_vbs[state.unit]
                        axis.setLabel(f"{ey_pfx}{state.unit}" if ey_pfx else state.unit)

            for phase, curve in state.curves.items():
                buf = state.disp_bufs.get(phase)
                if buf is not None and buf._n > 0:
                    curve.setData(*buf.get())
                else:
                    curve.setData([], [])

        if auto:
            for vb, _axis, _col in self._unit_vbs.values():
                vb.enableAutoRange()
        self._sync_extra_vb_geometry()

        # 선형 회귀: 정확도를 위해 DataStore 전체 데이터 사용 (사용자 드래그 시에만 호출)
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

    # ------------------------------------------------------------------
    # Hover readout — 측정 포인트에 마우스를 올리면 값 표시
    # ------------------------------------------------------------------

    #: 커서에서 이 픽셀 반경 안에 점이 있어야 표시한다.
    _HOVER_RADIUS_PX = 14

    def _setup_hover_readout(self) -> None:
        """마우스 위치에서 가장 가까운 측정 포인트의 값을 띄운다.

        비용은 마우스가 플롯 위에 있을 때만 든다. SignalProxy 로 초당 30회로 묶어
        (마우스 이동 신호는 그보다 훨씬 자주 온다) 매번 하는 일은 표시 버퍼
        (화면 폭 만큼, 보통 1000점 미만) 에 대한 numpy 거리 계산 한 번뿐이다.
        측정 루프(append_point)나 렌더 타이머에는 아무것도 더하지 않는다.
        """
        self._hover_marker = pg.ScatterPlotItem(
            size=11, pen=pg.mkPen("#d0021b", width=2), brush=pg.mkBrush(255, 255, 255, 0))
        self._hover_marker.setZValue(100)
        self._hover_marker.hide()
        self._pi.addItem(self._hover_marker, ignoreBounds=True)

        self._hover_text = pg.TextItem(anchor=(0, 1), color="#111111",
                                       fill=pg.mkBrush(255, 255, 255, 225),
                                       border=pg.mkPen("#888888"))
        self._hover_text.setZValue(101)
        self._hover_text.hide()
        self._pi.addItem(self._hover_text, ignoreBounds=True)

        self._hover_proxy = pg.SignalProxy(
            self._pw.scene().sigMouseMoved, rateLimit=30, slot=self._on_hover)
        self._pw.scene().sigMouseHover.connect(self._on_scene_hover)

    def _on_scene_hover(self, items) -> None:
        # 플롯 밖으로 나가면 표시를 지운다
        if not items:
            self._hide_hover()

    def _hide_hover(self) -> None:
        if getattr(self, "_hover_marker", None) is not None:
            self._hover_marker.hide()
            self._hover_text.hide()

    def _hover_sources(self):
        """(이름, 표시버퍼 dict, 뷰박스, y 단위접두어, y 키) 목록.

        메인 Y 와 Extra Y 를 같은 방식으로 훑기 위한 어댑터. Extra Y 는 자기
        축(vb)을 쓸 수 있으므로 뷰박스를 따로 들고 다닌다.
        """
        main_vb = self._pi.getViewBox()
        out = [(self._y_key or "Y", self._disp_bufs, main_vb, self._y_pfx, self._y_key)]
        for state in self._extra_ys:
            if not state.key or not state.disp_bufs:
                continue
            vb = main_vb if state.uses_main_vb else state.vb
            pfx = self._y_pfx if state.uses_main_vb else getattr(state, "y_pfx", "")
            out.append((state.key, state.disp_bufs, vb, pfx, state.key))
        return out

    def _on_hover(self, evt) -> None:
        pos = evt[0]
        if not self._pi.sceneBoundingRect().contains(pos):
            self._hide_hover()
            return

        best = None   # (픽셀거리^2, x, y, phase, y_key, y_pfx, vb)
        for _name, bufs, vb, y_pfx, y_key in self._hover_sources():
            try:
                mp = vb.mapSceneToView(pos)
                px, py = vb.viewPixelSize()
            except Exception:
                continue
            if not px or not py:
                continue
            mx, my = mp.x(), mp.y()
            for phase, buf in (bufs or {}).items():
                if buf is None or buf._n <= 0:
                    continue
                xs, ys = buf.get()
                if len(xs) == 0:
                    continue
                dx = (xs - mx) / px
                dy = (ys - my) / py
                d2 = dx * dx + dy * dy
                i = int(np.argmin(d2))
                if best is None or d2[i] < best[0]:
                    best = (float(d2[i]), float(xs[i]), float(ys[i]),
                            phase, y_key, y_pfx, vb)

        if best is None or best[0] > self._HOVER_RADIUS_PX ** 2:
            self._hide_hover()
            return

        _d2, x, y, phase, y_key, y_pfx, vb = best
        self._hover_marker.setData([x], [y])
        self._hover_marker.show()
        self._hover_text.setText(self._hover_label(x, y, phase, y_key, y_pfx))
        self._hover_text.setPos(x, y)
        self._hover_text.show()

    def _hover_label(self, x: float, y: float, phase: str,
                     y_key: str, y_pfx: str) -> str:
        """표시 문자열. 값은 화면에 그려진 것과 같은 단위(SI 접두어 포함)로 쓴다."""
        x_lbl, x_unit = self._store.col_meta(self._x_key) if self._x_key else ("X", "")
        y_lbl, y_unit = self._store.col_meta(y_key) if y_key else ("Y", "")
        lines = []
        if phase and phase != "_":
            lines.append(phase)
        lines.append(f"{x_lbl}: {x:.6g} {self._x_pfx}{x_unit}".rstrip())
        lines.append(f"{y_lbl}: {y:.6g} {y_pfx}{y_unit}".rstrip())
        return "\n".join(lines)

    def clear_curves(self) -> None:
        self._stop_regression()
        for curve in self._curves.values():
            curve.setData([], [])
        for state in self._extra_ys:
            for curve in state.curves.values():
                curve.setData([], [])
            state.y_div, state.y_pfx = 1.0, ""
            state.disp_bufs.clear()
        self._x_div, self._x_pfx = 1.0, ""
        self._y_div, self._y_pfx = 1.0, ""
        self._disp_bufs.clear()
        self._disp_buf_keys = (None, None)
        self._update_axis_labels("", "")
        self._set_held(False)
        self._dirty = False

    def reset_phases(self) -> None:
        """Remove all phase curves (for new session with different phases)."""
        self._stop_regression()
        for item in self._curves.values():
            self._pi.removeItem(item)
        self._curves.clear()
        for state in self._extra_ys:
            self._remove_extra_y_curves(state)
            state.y_div, state.y_pfx = 1.0, ""
            state.disp_bufs.clear()
        self._x_div, self._x_pfx = 1.0, ""
        self._y_div, self._y_pfx = 1.0, ""
        self._disp_bufs.clear()
        self._disp_buf_keys = (None, None)
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

        # ── 컨트롤 영역: 좌(X/Y/Z 넓게, 한 줄씩) | 우(자잘한 컨트롤, 우측 정렬) ──
        ctrl_area = QHBoxLayout()
        ctrl_area.setSpacing(12)

        # 좌측 컬럼 — X / Y / Z 축 선택 (각 한 줄, 콤보 넓게)
        left_col = QFormLayout()
        left_col.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        left_col.setHorizontalSpacing(8)
        left_col.setVerticalSpacing(6)
        left_col.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._cb_x = QComboBox(); self._cb_x.setFont(_MONO); self._cb_x.setMinimumWidth(220)
        self._cb_y = QComboBox(); self._cb_y.setFont(_MONO); self._cb_y.setMinimumWidth(220)
        self._cb_z = QComboBox(); self._cb_z.setFont(_MONO); self._cb_z.setMinimumWidth(220)
        for cb in (self._cb_x, self._cb_y, self._cb_z):
            cb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        left_col.addRow("X:", self._cb_x)
        left_col.addRow("Y:", self._cb_y)
        left_col.addRow("Z:", self._cb_z)
        ctrl_area.addLayout(left_col, stretch=1)

        # 우측 컬럼 — phase / Z min·max / Auto Z / Colormap (우측 정렬, 컴팩트)
        right_col = QVBoxLayout()
        right_col.setSpacing(4)

        phase_row = QHBoxLayout()
        phase_row.addStretch()
        phase_row.addWidget(QLabel("Phase:"))
        self._cb_phase = QComboBox()
        self._cb_phase.setFont(_MONO)
        self._cb_phase.setMinimumWidth(90)
        # date 콤보 제거: phase만 선택, 모든 date 폴더의 .dat를 한꺼번에 로드
        phase_row.addWidget(self._cb_phase)
        btn_refresh = QPushButton("↻")
        btn_refresh.setFixedWidth(28)
        btn_refresh.setToolTip("폴더 다시 스캔")
        btn_refresh.clicked.connect(lambda: self._on_base_changed(self._le_base.text()))
        phase_row.addWidget(btn_refresh)
        right_col.addLayout(phase_row)

        z_row = QHBoxLayout()
        z_row.addStretch()
        z_row.addWidget(QLabel("Z min:"))
        self._le_zmin = QLineEdit(); self._le_zmin.setFont(_MONO); self._le_zmin.setFixedWidth(72)
        z_row.addWidget(self._le_zmin)
        z_row.addWidget(QLabel("max:"))
        self._le_zmax = QLineEdit(); self._le_zmax.setFont(_MONO); self._le_zmax.setFixedWidth(72)
        z_row.addWidget(self._le_zmax)
        self._cb_auto_z = QCheckBox("Auto Z")
        self._cb_auto_z.setFont(_MONO)
        self._cb_auto_z.setChecked(True)
        self._cb_auto_z.setToolTip("자동 범위: 2~98 퍼센타일 (이상치에 강건). 끄면 아래 min/max 수동 사용.")
        self._cb_auto_z.stateChanged.connect(self._on_auto_z_changed)
        z_row.addWidget(self._cb_auto_z)
        self._cb_sym_z = QCheckBox("Sym")
        self._cb_sym_z.setFont(_MONO)
        self._cb_sym_z.setToolTip("0 중심 대칭 범위 (±max). 파랑-흰색-빨강 diverging colormap에 적합.")
        z_row.addWidget(self._cb_sym_z)
        right_col.addLayout(z_row)

        cmap_row = QHBoxLayout()
        cmap_row.addStretch()
        cmap_row.addWidget(QLabel("Colormap:"))
        self._cb_cmap = QComboBox()
        self._cb_cmap.setFont(_MONO)
        for name in _CMAP_NAMES:
            self._cb_cmap.addItem(name)
        cmap_row.addWidget(self._cb_cmap)
        right_col.addLayout(cmap_row)

        ctrl_area.addLayout(right_col)
        root.addLayout(ctrl_area)

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
        self._cb_sym_z.setEnabled(not manual)   # Sym은 Auto Z일 때만 의미 있음

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
        prev_phase = self._cb_phase.currentText()
        self._cb_phase.blockSignals(True)
        self._cb_phase.clear()
        if base and base.is_dir():
            for name in ("trace", "retrace", "dummy"):
                if (base / name).is_dir():
                    self._cb_phase.addItem(name)
        if self._cb_phase.count() == 0:
            self._cb_phase.addItem("(없음)")
        idx = self._cb_phase.findText(prev_phase)
        if idx >= 0:
            self._cb_phase.setCurrentIndex(idx)
        self._cb_phase.blockSignals(False)

    def _get_target_folder(self) -> str:
        """현재 선택된 base/phase 폴더 반환. 로더가 하위 모든 date 폴더를 재귀 스캔한다."""
        base = self._le_base.text().strip()
        phase = self._cb_phase.currentText()
        if not base or phase.startswith("("):
            return ""
        return str(Path(base) / phase)

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
        auto_label = ""
        if self._cb_auto_z.isChecked():
            # robust auto-scale: 2~98 퍼센타일로 이상치 영향 제거 (min/max보다 강건).
            # Symmetric 옵션이 켜져 있고 diverging 계열 colormap이면 0 중심 대칭 범위.
            lo, hi = np.percentile(finite, [2.0, 98.0])
            z_min, z_max = float(lo), float(hi)
            if z_min == z_max:   # 퍼센타일이 같으면 전체 min/max로 후퇴
                z_min, z_max = float(finite.min()), float(finite.max())
            if self._cb_sym_z.isChecked():
                m = max(abs(z_min), abs(z_max))
                z_min, z_max = -m, m
                auto_label = " (sym)"
            else:
                auto_label = " (p2–98)"
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
            f"{n_y} 파일 × {n_x} 포인트 | Z [{z_min:.4g}, {z_max:.4g}]{auto_label}"
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

    # 렌더 모드 상수
    MODE_SYNC = "sync"   # 측정마다 즉시 렌더 — 완전 동기화 (slow)
    MODE_FAST = "fast"   # 5 fps 독립 타이머 — 측정 지연 없음 (fast)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Graph")
        self.resize(920, 720)

        self._store = DataStore()
        self._panels: List[GraphPanel] = []
        self._render_mode: str = self.MODE_FAST   # 기본: Fast

        self._build_ui()
        self._add_panel()   # Start with one panel by default

        self._redraw_timer = QTimer(self)
        self._redraw_timer.timeout.connect(self._redraw_all)
        self._apply_render_mode(self._render_mode)

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
        bar.addSpacing(16)

        # ── Render mode toggle ────────────────────────────
        self._btn_render_mode = QPushButton()
        self._btn_render_mode.setCheckable(True)
        self._btn_render_mode.setFixedWidth(90)
        self._btn_render_mode.setToolTip(
            "Sync: 측정 포인트마다 즉시 렌더 (그래프·측정 완전 동기화)\n"
            "Fast: 5 fps 독립 렌더 타이머 — 측정 타이밍 우선"
        )
        self._btn_render_mode.toggled.connect(self._on_render_mode_toggled)
        bar.addWidget(self._btn_render_mode)
        self._update_render_mode_btn()   # 버튼 라벨 초기화

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
        """Append one measurement step.  Must be called from the GUI thread.

        Sync mode: 이 호출 내에서 즉시 렌더 (측정 ↔ 그래프 완전 동기화).
        Fast mode: on_new_point()로 버퍼만 갱신, 렌더는 독립 타이머가 담당.
        """
        phase = point.phase or "_"
        self._store.append(point)
        for panel in self._panels:
            panel.ensure_phases([phase])
            panel.on_new_point(phase, point.values)

        if self._render_mode == self.MODE_SYNC:
            self._redraw_all()   # 즉시 렌더 — 측정 흐름과 동기

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

    # Render mode -------------------------------------------------------------

    def _apply_render_mode(self, mode: str) -> None:
        """타이머를 모드에 맞게 설정."""
        self._redraw_timer.stop()
        if mode == self.MODE_SYNC:
            # 타이머 없음: append_point() 호출마다 inline 렌더
            pass
        else:  # MODE_FAST
            # 5 fps 독립 렌더 타이머: 측정 루프와 완전 비동기
            self._redraw_timer.setInterval(200)
            self._redraw_timer.start()

    def _on_render_mode_toggled(self, sync_active: bool) -> None:
        self._render_mode = self.MODE_SYNC if sync_active else self.MODE_FAST
        self._apply_render_mode(self._render_mode)
        self._update_render_mode_btn()

    def _update_render_mode_btn(self) -> None:
        sync = (self._render_mode == self.MODE_SYNC)
        self._btn_render_mode.blockSignals(True)
        self._btn_render_mode.setChecked(sync)
        self._btn_render_mode.blockSignals(False)
        if sync:
            self._btn_render_mode.setText("🔄 Sync")
            self._btn_render_mode.setStyleSheet(
                "QPushButton { background-color: #1a3a1a; color: #4ec9b0; "
                "border: 1px solid #4ec9b0; border-radius: 3px; font-weight: bold; }"
            )
        else:
            self._btn_render_mode.setText("⚡ Fast")
            self._btn_render_mode.setStyleSheet(
                "QPushButton { background-color: #1a1a3a; color: #79c0ff; "
                "border: 1px solid #79c0ff; border-radius: 3px; font-weight: bold; }"
            )

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
