"""
MfliWindow: Zurich MFLI noise-vs-frequency 측정 창 (VNA Control과 동일한 독립 모듈 패턴).

동작: 주파수를 LINEAR로 sweep하며 각 포인트에서
  (1) MFLI 노드에서 noise level(스칼라)을 읽고,
  (2) 보조 계측기(예: Oxford iTC probe 온도, M81 DC voltage)를 함께 읽어
같은 행에 기록한다. 한 sweep = .dat 파일 1개(주파수당 한 행).

VNA 창(gui/vna_window.py)의 취득 워커/스레드·플롯 패널·저장·스레드 안전 패턴을 복사·축소했다.
double-sweep / time-mode / section / second-channel / alarm 등 VNA 전용 기능은 뺐다.

스레드 안전 불변식(VNA와 동일):
  - 워커(다른 스레드) → GUI 갱신은 반드시 bound @Slot으로 연결(lambda 금지 → 워커 스레드에서
    직접 실행되어 GUI 접근 시 크래시).
  - QThread는 parent 없이 생성(창이 sweep 중 파괴돼도 std::terminate 방지). 수명은 참조 +
    deleteLater로 관리. 앱 종료 전 shutdown_threads()로 stop→quit→wait.
"""
import time as _time
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

import numpy as np
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from pythonization.app.paths import SETTINGS_DIR
from pythonization.instruments.parameter import _parse_float
from pythonization.ui.widgets.plot_panel import (
    MONO as _MONO,
    PlotPanel,
    PlotPanelState,
)
from pythonization.ui.modules.mfli.models import (
    MfliAcquireConfig, MfliAuxRead, MfliConfigData, MfliPlotCurveConfig,
    load_mfli_config, save_mfli_config,
)
from pythonization.ui.modules.vna.models import next_dat_path, next_sweep_folder  # 재사용 (중복 정의 방지)

if TYPE_CHECKING:
    from pythonization.instruments.session import InstrumentSession
    from pythonization.instruments.command_library import VisaLibraryRegistry
    from pythonization.profiles.registry import ProfileRegistry

_DEFAULT_CONFIG_PATH = SETTINGS_DIR / "mfli_config.yaml"

# alias별 기본 aux 읽기 명령. 로드된 프로파일의 명령이 비었거나 옛 placeholder("R1")면 이걸로 시드.
# (모델 MfliAcquireConfig.aux_reads 기본값과 동일 문자열을 유지할 것)
_DEFAULT_AUX_CMD = {
    "ITC": "READ:DEV:DB8.T1:TEMP:SIG:TEMP",
    "M81": "FETCh:SENSe{M}:DC?",
}


def _qthread_running(t) -> bool:
    """QThread가 살아있고 실행 중인지 안전 확인 (삭제된 QObject 접근 시 RuntimeError→False)."""
    try:
        return bool(t) and t.isRunning()
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# Acquire worker — 주파수 LINEAR sweep + per-point noise/aux read
# ---------------------------------------------------------------------------
class _MfliAcquireWorker(QObject):
    step_done    = Signal(int, list)    # (step_idx, [np.ndarray, ...])  각 열 길이 1
    step_elapsed = Signal(int, float)   # (step_idx, elapsed_sec)
    finished     = Signal()             # 항상 emit (정상/에러/중단)
    error        = Signal(str)
    progress     = Signal(str)

    # follow 모드: MFLI 읽기가 이 시간(초) 이상 '연속' 실패하면 장비 소실로 보고 중단.
    # 그보다 짧은 일시적 실패는 그 포인트만 건너뛰고 계속(장시간 무인 측정이 한 번의 글리치로
    # 멈추지 않도록). 튜닝하려면 이 값만 바꾸면 된다.
    _FAIL_ABORT_S = 300.0

    def __init__(self, session, cfg: MfliAcquireConfig, freq_values: Optional[List[float]] = None):
        super().__init__()
        self._session = session
        self._cfg     = cfg
        self._freqs   = list(freq_values) if freq_values else []
        self._aux     = [a for a in cfg.aux_reads if a.enabled and a.alias and a.query_cmd]
        self._follow  = bool(cfg.follow_external)
        self._interval = max(0.02, float(cfg.poll_interval))   # follow: 주파수 확인 간격(촘촘히)
        self._count    = int(cfg.poll_count)
        self._read_noise = bool((cfg.noise_node or "").strip())  # 비면 noise 열 생략(Option A)
        self._m = str(getattr(cfg, "m_value", "") or "")          # '{M}' 치환값 (사용자 입력)
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def _prep(self, cmd: str) -> str:
        """전송 직전 '{M}'를 사용자 m_value로 치환. ('{dev}'는 MFLI 드라이버가 처리하므로 건드리지 않음)"""
        return cmd.replace("{M}", self._m) if self._m else cmd

    def _q_float(self, alias: str, cmd: str) -> float:
        if not self._session.is_open(alias):
            self._session.open(alias)
        return _parse_float(str(self._session.query(alias, self._prep(cmd))).strip())

    def _read_aux(self) -> List[float]:
        """활성 보조 계측기를 순차로 읽는다. 한 읽기가 실패해도 NaN으로 대체하고 sweep을 계속."""
        out: List[float] = []
        for a in self._aux:
            try:
                out.append(self._q_float(a.alias, a.query_cmd))
            except Exception:
                out.append(float("nan"))
        return out

    @Slot()
    def run(self):
        from pythonization.app.logging_setup import get_logger
        log = get_logger()
        mode = "follow" if self._follow else "driven"
        log.info("MfliAcquireWorker start (mode=%s, %d points)", mode, len(self._freqs))
        try:
            if self._follow:
                self._run_follow()
            else:
                self._run_value_mode()
        except Exception as e:
            log.exception("MfliAcquireWorker fatal")
            self.error.emit(f"Fatal: {type(e).__name__}: {e}")
        finally:
            log.info("MfliAcquireWorker finished (mode=%s)", mode)
            self.finished.emit()

    @staticmethod
    def _freq_changed(f, last) -> bool:
        """주파수가 직전 기록값과 달라졌는지. 부동소수 지터 방지용 미세 허용오차."""
        if last is None:
            return True
        return abs(f - last) > (1e-6 + 1e-9 * abs(last))

    def _sleep_poll(self, t0):
        """폴 간격(t0 + _interval)까지 stop-반응형 분할 sleep.
        max(0.0, ...) 필수: while 조건과 sleep 인자가 perf_counter()를 각각 호출하므로 그 사이에
        deadline을 넘기면 (deadline-now)가 음수가 되어 sleep()이 ValueError로 죽는다(장시간 측정
        중 드물게 발생 → 워커 중단). 음수는 0으로 클램프."""
        deadline = t0 + self._interval
        while not self._stop_flag and _time.perf_counter() < deadline:
            _time.sleep(max(0.0, min(0.02, deadline - _time.perf_counter())))

    def _run_follow(self):
        """LabOne이 주파수를 몰고, 우리는 '주파수 노드만' 촘촘히 폴링한다. 주파수가 직전 기록값과
        달라진 경우(또는 sweep 첫 폴)에만 noise+aux(ITC/M81)를 읽어 한 행을 기록한다.
        → 한 주파수당 데이터 1개. count<=0이면 Stop까지.

        견고성: MFLI 읽기(freq/noise)가 일시적으로 실패하면 그 포인트만 건너뛰고 계속한다(장시간
        무인 측정이 한 번의 글리치로 멈추지 않도록). '연속' 실패가 _FAIL_ABORT_S를 넘어야 중단한다
        (장비 소실 판단). aux(ITC/M81)는 _read_aux가 개별 실패를 NaN 처리하므로 애초에 안 죽는다.

        행 순서: [time(s), frequency, noise, *aux] — 창의 _extra_cols(follow)와 일치."""
        from pythonization.app.logging_setup import get_logger
        log = get_logger()
        c = self._cfg
        freq_node = c.freq_write_cmd     # driven의 set 노드를 follow에선 read 노드로 재사용
        t_start = _time.perf_counter()
        last_freq = None                 # 마지막으로 '기록한' 주파수
        i = 0                            # 기록한 행 수
        fail_since = None                # MFLI 읽기 연속 실패 시작 시각(None=실패 중 아님)

        def _note_fail(where, e):
            """일시 실패 처리: 로그+상태 남기고, 연속 실패가 _FAIL_ABORT_S 넘으면 True(중단)."""
            nonlocal fail_since
            now = _time.perf_counter()
            if fail_since is None:
                fail_since = now
            dur = now - fail_since
            log.warning("MFLI follow %s fail: %s: %s [streak %.0fs]",
                        where, type(e).__name__, e, dur)
            if dur >= self._FAIL_ABORT_S:
                self.error.emit(f"{where} 연속 실패 {dur:.0f}s 초과 — 측정 중단: "
                                f"{type(e).__name__}: {e}")
                return True
            self.progress.emit(f"{where} 실패, 건너뛰고 계속 ({dur:.0f}s 째): "
                               f"{type(e).__name__}: {e}")
            return False

        while not self._stop_flag:
            if self._count > 0 and i >= self._count:
                break
            t0 = _time.perf_counter()
            # (1) 매 폴: 주파수만 읽는다(가볍게). 실패해도 그 폴만 건너뛰고 계속.
            try:
                freq = self._q_float(c.mfli_alias, freq_node)
            except Exception as e:
                if _note_fail("freq read", e):
                    break
                self._sleep_poll(t0)
                continue
            fail_since = None            # MFLI 응답 정상 → 연속 실패 streak 해제
            # (2) 주파수가 바뀐 경우(또는 첫 폴)만 온도/M81(+noise) 읽어 기록
            if self._freq_changed(freq, last_freq):
                try:
                    noise = self._q_float(c.mfli_alias, c.noise_node) if self._read_noise else None
                except Exception as e:
                    if _note_fail("noise read", e):
                        break
                    self._sleep_poll(t0)   # last_freq 유지 → 다음 폴에 자동 재시도
                    continue
                aux = self._read_aux()     # 개별 실패는 NaN (예외 안 남 → sweep 계속)
                elapsed = _time.perf_counter() - t_start
                row = [np.array([float(elapsed)]), np.array([float(freq)])] \
                    + ([np.array([float(noise)])] if noise is not None else []) \
                    + [np.array([float(v)]) for v in aux]
                self.step_elapsed.emit(i, _time.perf_counter() - t0)
                self.step_done.emit(i, row)
                n_label = f"{i + 1}/{self._count}" if self._count > 0 else f"{i + 1}"
                self.progress.emit(f"Follow #{n_label}  f={freq:.6g} Hz (new)  t={elapsed:.1f}s")
                last_freq = freq
                i += 1
            # (3) 폴 간격 대기 (같은 주파수면 그냥 계속 폴링)
            self._sleep_poll(t0)

    def _run_value_mode(self):
        c = self._cfg
        n = len(self._freqs)
        for i, fv in enumerate(self._freqs):
            if self._stop_flag:
                break
            self.progress.emit(f"Step {i + 1}/{n}  freq={fv:.6g} Hz")
            t0 = _time.perf_counter()
            try:
                # (1) 주파수 설정 — 드라이버가 setDouble 후 settle_s 대기(락인 정착)
                self._session.write(c.mfli_alias, f"{self._prep(c.freq_write_cmd)} = {fv:.10g}")
                # (2) noise 노드 스칼라 읽기
                noise = self._q_float(c.mfli_alias, c.noise_node)
                # (3) 보조값 (온도·M81 전압 등)
                aux = self._read_aux()
            except Exception as e:
                self.error.emit(f"Step {i + 1}: {type(e).__name__}: {e}")
                break
            # (4) 행 조립: [freq, noise, *aux] — 창의 _extra_cols 순서와 일치
            row = [np.array([float(fv)]), np.array([float(noise)])] \
                + [np.array([float(v)]) for v in aux]
            self.step_elapsed.emit(i, _time.perf_counter() - t0)
            self.step_done.emit(i, row)



# ---------------------------------------------------------------------------
# Plot panel  (공용 위젯 — pythonization/ui/widgets/plot_panel.py)
# ---------------------------------------------------------------------------

def _plot_state(cfg: MfliPlotCurveConfig) -> PlotPanelState:
    """저장된 곡선 설정 → 패널 상태. y_sources 가 비면 구 버전 단일 y_source 로 폴백."""
    y_srcs = list(cfg.y_sources) if cfg.y_sources else (
        [cfg.y_source] if cfg.y_source else [])
    return PlotPanelState(x_source=cfg.x_source, y_sources=y_srcs,
                          log_x=cfg.log_x, log_y=cfg.log_y)


def _plot_config(panel: PlotPanel) -> MfliPlotCurveConfig:
    """패널 상태 → 저장용 곡선 설정. y_source 는 구 버전 호환으로 계속 채운다."""
    state = panel.to_state()
    return MfliPlotCurveConfig(
        x_source=state.x_source,
        y_source=state.y_sources[0] if state.y_sources else "",
        y_sources=state.y_sources,
        log_x=state.log_x,
        log_y=state.log_y,
    )

# ---------------------------------------------------------------------------
# Aux read row (per-point 보조 계측기 1개 편집)
# ---------------------------------------------------------------------------
class _AuxRow(QWidget):
    remove_requested = Signal(object)

    def __init__(self, aux: MfliAuxRead, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._cb_en = QCheckBox()
        self._cb_en.setChecked(aux.enabled)
        self._cb_en.setToolTip("이 보조값을 각 주파수 포인트에서 읽기")
        lay.addWidget(self._cb_en)
        self._le_alias = QLineEdit(aux.alias); self._le_alias.setFont(_MONO)
        self._le_alias.setFixedWidth(60); self._le_alias.setPlaceholderText("alias")
        lay.addWidget(self._le_alias)
        self._le_cmd = QLineEdit(aux.query_cmd); self._le_cmd.setFont(_MONO)
        self._le_cmd.setPlaceholderText("query cmd")
        lay.addWidget(self._le_cmd, stretch=1)
        self._le_label = QLineEdit(aux.label); self._le_label.setFont(_MONO)
        self._le_label.setFixedWidth(80); self._le_label.setPlaceholderText("label")
        lay.addWidget(self._le_label)
        self._le_unit = QLineEdit(aux.unit); self._le_unit.setFont(_MONO)
        self._le_unit.setFixedWidth(46); self._le_unit.setPlaceholderText("unit")
        lay.addWidget(self._le_unit)
        btn_rm = QPushButton("✕"); btn_rm.setFixedSize(20, 20)
        btn_rm.clicked.connect(lambda: self.remove_requested.emit(self))
        lay.addWidget(btn_rm)

    def to_model(self) -> MfliAuxRead:
        return MfliAuxRead(
            alias=self._le_alias.text().strip(),
            query_cmd=self._le_cmd.text().strip(),
            label=self._le_label.text().strip() or "aux",
            unit=self._le_unit.text().strip(),
            enabled=self._cb_en.isChecked(),
        )

    def set_enabled_editing(self, editable: bool):
        for w in (self._cb_en, self._le_alias, self._le_cmd, self._le_label, self._le_unit):
            w.setEnabled(editable)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MfliWindow(QDialog):
    """MFLI noise-vs-frequency 측정 창 (VNA Control과 동일한 런처/프로파일 계약)."""

    def __init__(self, session: "InstrumentSession",
                 lib_reg: "VisaLibraryRegistry",
                 param_manager_reg: Optional["ProfileRegistry"] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("MFLI Noise Sweep")
        self.resize(1200, 720)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self._session           = session
        self._lib_reg           = lib_reg           # parity용 (MFLI는 명령을 하드코딩)
        self._param_manager_reg = param_manager_reg

        self._cfg = load_mfli_config(self._config_path())
        self._acq_worker: Optional[_MfliAcquireWorker] = None
        self._acq_thread: Optional[QThread] = None
        self._aux_rows: List[_AuxRow] = []
        self._extra_cols: List[tuple] = []          # [(label, unit), ...] 열 순서
        self._acc: List[List[float]] = []           # 현재 sweep 저장 버퍼 — wrap마다 파일로 flush 후 비움
        self._plot_acc: List[List[float]] = []      # 현재 sweep만(플롯용) — follow에서 wrap 시 비움
        self._sweep_min = None                      # 현재 sweep의 주파수 min/max (wrap 감지)
        self._sweep_max = None
        self._sweep_dir = 0                          # 현재 sweep 진행 방향(+1/-1, 0=미정)
        self._last_plot_freq = None
        self._step_count = 0
        self._saved_files = 0                        # 이번 run에서 저장한 sweep 파일 수
        self._acq_stopped = False

        self._build_ui()
        self._apply_cfg_to_ui()
        self._refresh_plot_sources()
        self._restore_plot_config()

    # ------------------------------------------------------------------
    # Config path / persistence
    # ------------------------------------------------------------------
    def _config_path(self) -> Path:
        if self._param_manager_reg is not None:
            name = self._param_manager_reg.active_name
            d = SETTINGS_DIR / "profiles" / "mfli"
            d.mkdir(parents=True, exist_ok=True)
            return d / f"{name}.yaml"
        return _DEFAULT_CONFIG_PATH

    def on_profile_changed(self):
        """main_window에서 프로파일 변경 시 호출 — 새 프로파일 설정 로드."""
        self._cfg = load_mfli_config(self._config_path())
        self._apply_cfg_to_ui()
        self._refresh_plot_sources()
        self._restore_plot_config()

    def _save_ui_state(self):
        """현재 UI → 설정으로 수집·저장 (main_window 프로파일 저장 훅에서 호출)."""
        try:
            self._cfg = self._collect_cfg()
            save_mfli_config(self._cfg, self._config_path())
        except Exception as e:
            print(f"[MFLI] save_ui_state failed: {e}")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, stretch=1)

        # ── 좌측: 설정 ──
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        lv.setSpacing(8)

        # 측정 방식 (driven vs follow)
        from PySide6.QtWidgets import QRadioButton, QButtonGroup
        gb_mode = QGroupBox("측정 방식")
        mo = QVBoxLayout(gb_mode)
        self._rb_driven = QRadioButton("이 프로그램이 주파수를 직접 sweep")
        self._rb_follow = QRadioButton("LabOne이 sweep하고, 나는 따라 읽기 (time-based)")
        self._rb_follow.setToolTip(
            "LabOne(Sweeper 등)이 주파수를 몰 때, 주파수를 쓰지 않고 일정 간격마다\n"
            "현재 주파수 노드 + noise + 보조값을 읽어 기록합니다.")
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._rb_driven, 0)
        self._mode_group.addButton(self._rb_follow, 1)
        self._rb_driven.setChecked(True)
        mo.addWidget(self._rb_driven)
        mo.addWidget(self._rb_follow)
        self._mode_group.idToggled.connect(self._on_mode_changed)
        lv.addWidget(gb_mode)

        # Frequency sweep (driven) + follow 폴링 설정
        gb_freq = QGroupBox("Frequency / Sampling")
        gf = QVBoxLayout(gb_freq)
        # driven 행
        self._row_driven = QWidget()
        fl = QHBoxLayout(self._row_driven); fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(QLabel("Start(Hz):"))
        self._le_start = QLineEdit(); self._le_start.setFont(_MONO); self._le_start.setFixedWidth(90)
        fl.addWidget(self._le_start)
        fl.addWidget(QLabel("Stop(Hz):"))
        self._le_stop = QLineEdit(); self._le_stop.setFont(_MONO); self._le_stop.setFixedWidth(90)
        fl.addWidget(self._le_stop)
        fl.addWidget(QLabel("Points:"))
        self._le_n = QLineEdit(); self._le_n.setFont(_MONO); self._le_n.setFixedWidth(50)
        fl.addWidget(self._le_n)
        fl.addStretch()
        gf.addWidget(self._row_driven)
        # follow 행
        self._row_follow = QWidget()
        pl = QHBoxLayout(self._row_follow); pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(QLabel("Poll interval(s):"))
        self._le_poll = QLineEdit(); self._le_poll.setFont(_MONO); self._le_poll.setFixedWidth(60)
        self._le_poll.setToolTip("주파수 노드를 확인하는 간격(초). 촘촘히(예: 0.1~0.5) 두어\n"
                                 "LabOne dwell보다 짧게 하면 주파수를 놓치지 않습니다.\n"
                                 "온도/M81은 매 폴이 아니라 '주파수가 바뀔 때만' 읽습니다.")
        pl.addWidget(self._le_poll)
        pl.addWidget(QLabel("Samples(0=until Stop):"))
        self._le_polln = QLineEdit(); self._le_polln.setFont(_MONO); self._le_polln.setFixedWidth(60)
        self._le_polln.setToolTip("기록할 데이터 포인트(=서로 다른 주파수) 개수. 0이면 Stop까지.")
        pl.addWidget(self._le_polln)
        pl.addStretch()
        gf.addWidget(self._row_follow)
        lv.addWidget(gb_freq)

        # MFLI node config
        gb_mfli = QGroupBox("MFLI")
        ml = QVBoxLayout(gb_mfli)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("alias:"))
        self._le_alias = QLineEdit(); self._le_alias.setFont(_MONO); self._le_alias.setFixedWidth(80)
        r1.addWidget(self._le_alias)
        r1.addWidget(QLabel("freq node:"))
        self._le_freq_cmd = QLineEdit(); self._le_freq_cmd.setFont(_MONO)
        self._le_freq_cmd.setToolTip("주파수 설정 노드 (worker가 ' = {값}'을 덧붙임). {dev}는 device_id로 치환")
        r1.addWidget(self._le_freq_cmd, stretch=1)
        ml.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("noise node:"))
        self._le_noise = QLineEdit(); self._le_noise.setFont(_MONO)
        self._le_noise.setToolTip("LabOne에서 구성한 스칼라 noise/PSD 노드 경로. {dev}는 device_id로 치환")
        r2.addWidget(self._le_noise, stretch=1)
        ml.addLayout(r2)
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("noise label:"))
        self._le_noise_label = QLineEdit(); self._le_noise_label.setFont(_MONO); self._le_noise_label.setFixedWidth(100)
        r3.addWidget(self._le_noise_label)
        r3.addWidget(QLabel("unit:"))
        self._le_noise_unit = QLineEdit(); self._le_noise_unit.setFont(_MONO); self._le_noise_unit.setFixedWidth(80)
        r3.addWidget(self._le_noise_unit)
        r3.addStretch()
        ml.addLayout(r3)
        lv.addWidget(gb_mfli)

        # Aux reads
        gb_aux = QGroupBox("Aux reads (per frequency point)")
        al = QVBoxLayout(gb_aux)
        self._aux_container = QWidget()
        self._aux_lay = QVBoxLayout(self._aux_container)
        self._aux_lay.setContentsMargins(0, 0, 0, 0)
        self._aux_lay.setSpacing(2)
        al.addWidget(self._aux_container)
        ab = QHBoxLayout()
        btn_aux_add = QPushButton("+ 보조값"); btn_aux_add.clicked.connect(lambda: self._add_aux_row())
        ab.addWidget(btn_aux_add)
        ab.addSpacing(12)
        ab.addWidget(QLabel("명령의 {M} ="))
        self._le_m = QLineEdit(); self._le_m.setFont(_MONO); self._le_m.setFixedWidth(46)
        self._le_m.setToolTip("명령어 안의 '{M}' 자리에 넣을 값 (예: M81 sense 모듈 번호).\n"
                              "전송 직전 치환됩니다. 예: FETCh:SENSe{M}:DC? + M=2 → FETCh:SENSe2:DC?")
        ab.addWidget(self._le_m)
        ab.addStretch()
        al.addLayout(ab)
        lv.addWidget(gb_aux)

        # Save
        gb_save = QGroupBox("Save")
        sl = QVBoxLayout(gb_save)
        sr1 = QHBoxLayout()
        self._cb_save = QCheckBox("Auto-save (.dat)")
        sr1.addWidget(self._cb_save); sr1.addStretch()
        sl.addLayout(sr1)
        sr2 = QHBoxLayout()
        sr2.addWidget(QLabel("main:"))
        self._le_main = QLineEdit(); self._le_main.setFont(_MONO)
        sr2.addWidget(self._le_main, stretch=1)
        btn_browse = QPushButton("…"); btn_browse.setFixedWidth(28); btn_browse.clicked.connect(self._browse_main)
        sr2.addWidget(btn_browse)
        sl.addLayout(sr2)
        sr3 = QHBoxLayout()
        sr3.addWidget(QLabel("sub:"))
        self._le_sub = QLineEdit(); self._le_sub.setFont(_MONO)
        sr3.addWidget(self._le_sub, stretch=1)
        sr3.addWidget(QLabel("file:"))
        self._le_filename = QLineEdit(); self._le_filename.setFont(_MONO); self._le_filename.setFixedWidth(120)
        sr3.addWidget(self._le_filename)
        sl.addLayout(sr3)
        lv.addWidget(gb_save)

        # Merge with LabOne (pythonization.analysis.mfli_merge) — CSV·dat·저장폴더 선택 후 sweep별 병합
        gb_merge = QGroupBox("Merge with LabOne sweep (sweep_N_merged_data.dat)")
        mg = QVBoxLayout(gb_merge)
        def _path_row(label, browse_slot):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            le = QLineEdit(); le.setFont(_MONO)
            row.addWidget(le, stretch=1)
            b = QPushButton("…"); b.setFixedWidth(28); b.clicked.connect(browse_slot)
            row.addWidget(b)
            mg.addLayout(row)
            return le
        self._le_merge_csv = _path_row("LabOne CSV:", self._browse_merge_csv)
        self._le_merge_csv.setToolTip("LabOne sweeper autosave CSV 파일, 또는 그 폴더(자동 탐색)")
        self._le_merge_dat = _path_row("측정 폴더/.dat:", self._browse_merge_dat)
        self._le_merge_dat.setToolTip("측정 sweep 폴더(per-sweep test_xNNN.dat 들이 있는 폴더) "
                                      "또는 단일 .dat. 폴더면 안의 .dat를 각 sweep으로 병합.")
        self._le_merge_out = _path_row("저장 폴더:", self._browse_merge_out)
        mrow = QHBoxLayout()
        self._btn_merge = QPushButton("Merge sweeps")
        self._btn_merge.setToolTip("두 파일을 sweep별로 분리·대응해 sweep_N_merged_data.dat 저장 "
                                   "(Noise level 컬럼 추가)")
        self._btn_merge.clicked.connect(self._run_merge)
        mrow.addWidget(self._btn_merge); mrow.addStretch()
        mg.addLayout(mrow)
        lv.addWidget(gb_merge)

        # Buttons + status
        bl = QHBoxLayout()
        self._btn_start = QPushButton("▶ Start Sweep")
        self._btn_start.setMinimumHeight(34)
        self._btn_start.clicked.connect(self._start_acquire)
        self._btn_stop = QPushButton("■ Stop")
        self._btn_stop.setMinimumHeight(34)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop_acquire)
        self._btn_clear = QPushButton("Clear plot")
        self._btn_clear.setMinimumHeight(34)
        self._btn_clear.setToolTip("플롯에 표시된 데이터를 즉시 비웁니다(저장된 .dat엔 영향 없음).\n"
                                   "follow 모드는 새 주파수 sweep 시작 시 자동으로도 비워집니다.")
        self._btn_clear.clicked.connect(self._clear_plot)
        bl.addWidget(self._btn_start); bl.addWidget(self._btn_stop); bl.addWidget(self._btn_clear)
        lv.addLayout(bl)

        self._lbl_status = QLabel("Ready.")
        self._lbl_status.setWordWrap(True)
        lv.addWidget(self._lbl_status)
        lv.addStretch()

        left.setMaximumWidth(560)
        splitter.addWidget(left)

        # ── 우측: 플롯 ──
        self._panel = PlotPanel(start_color_idx=0, log_toggles=True)
        splitter.addWidget(self._panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    def _add_aux_row(self, aux: Optional[MfliAuxRead] = None):
        row = _AuxRow(aux if aux is not None else MfliAuxRead(label="aux"))
        row.remove_requested.connect(self._remove_aux_row)
        self._aux_rows.append(row)
        self._aux_lay.addWidget(row)

    def _remove_aux_row(self, row: _AuxRow):
        if row in self._aux_rows:
            self._aux_rows.remove(row)
            row.setParent(None)
            row.deleteLater()

    def _browse_main(self):
        d = QFileDialog.getExistingDirectory(self, "저장 폴더 선택", self._le_main.text().strip() or ".")
        if d:
            self._le_main.setText(d)

    # ------------------------------------------------------------------
    # LabOne 병합 (pythonization.analysis.mfli_merge)
    # ------------------------------------------------------------------
    def _browse_merge_csv(self):
        start = self._le_merge_csv.text().strip() or "."
        f, _ = QFileDialog.getOpenFileName(self, "LabOne CSV 선택", start,
                                           "CSV (*.csv);;All files (*.*)")
        if f:
            self._le_merge_csv.setText(f)

    def _browse_merge_dat(self):
        # per-sweep 저장은 폴더 하나에 test_xNNN.dat 여러 개 → 기본은 '폴더' 선택.
        # (analysis.mfli_merge는 폴더/단일 .dat 둘 다 받는다. 단일 파일은 경로를 직접 입력.)
        start = self._le_merge_dat.text().strip() or self._le_main.text().strip() or "."
        d = QFileDialog.getExistingDirectory(self, "측정 sweep 폴더 선택 (per-sweep .dat)", start)
        if d:
            self._le_merge_dat.setText(d)

    def _browse_merge_out(self):
        start = self._le_merge_out.text().strip() or self._le_main.text().strip() or "."
        d = QFileDialog.getExistingDirectory(self, "병합 결과 저장 폴더", start)
        if d:
            self._le_merge_out.setText(d)

    def _run_merge(self):
        labone = self._le_merge_csv.text().strip()
        ours   = self._le_merge_dat.text().strip()
        outdir = self._le_merge_out.text().strip()
        if not (labone and ours and outdir):
            self._set_status("병합: LabOne CSV·측정 .dat·저장 폴더를 모두 지정하세요.", "#f78166")
            return
        # 경로 기억
        try:
            self._cfg = self._collect_cfg()
            save_mfli_config(self._cfg, self._config_path())
        except Exception:
            pass
        self._set_status("병합 중…", "#888")
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        try:
            from pythonization.analysis.mfli_merge import merge_sweeps
            res = merge_sweeps(labone, ours, outdir)
        except Exception as e:
            self._set_status(f"병합 실패: {type(e).__name__}: {e}", "#f78166")
            return
        msg = (f"병합 완료: {res['n_paired']}개 파일 → {res['outdir']} "
               f"(LabOne {res['n_labone']} / 측정 {res['n_ours']} sweeps)")
        if res["warning"]:
            msg += f"  [경고: {res['warning']}]"
        self._set_status(msg, "#7ee787" if res["n_paired"] else "#d7ba7d")

    # ------------------------------------------------------------------
    # Config <-> UI
    # ------------------------------------------------------------------
    def _apply_cfg_to_ui(self):
        a = self._cfg.acquire
        self._le_start.setText(f"{a.sweep_start:g}")
        self._le_stop.setText(f"{a.sweep_stop:g}")
        self._le_n.setText(str(a.sweep_n))
        self._le_alias.setText(a.mfli_alias)
        self._le_freq_cmd.setText(a.freq_write_cmd)
        self._le_noise.setText(a.noise_node)
        self._le_noise_label.setText(a.noise_label)
        self._le_noise_unit.setText(a.noise_unit)
        self._le_filename.setText(a.filename)
        self._le_poll.setText(f"{a.poll_interval:g}")
        self._le_polln.setText(str(a.poll_count))
        self._le_m.setText(str(getattr(a, "m_value", "1")))
        self._le_main.setText(self._cfg.main_folder)
        self._le_sub.setText(self._cfg.sub_folder)
        self._cb_save.setChecked(self._cfg.save_enabled)
        self._le_merge_csv.setText(getattr(self._cfg, "merge_labone", ""))
        self._le_merge_dat.setText(getattr(self._cfg, "merge_ours", ""))
        self._le_merge_out.setText(getattr(self._cfg, "merge_outdir", ""))
        # aux rows 재구성. 예전 placeholder(빈칸/"R1")는 alias별 기본 명령으로 시드한다
        # → 기존 프로파일도 재시작만 하면 기본 명령이 뜬다(한 번 저장하면 유지).
        for row in list(self._aux_rows):
            row.setParent(None); row.deleteLater()
        self._aux_rows = []
        for aux in a.aux_reads:
            if aux.query_cmd.strip() in ("", "R1") and aux.alias in _DEFAULT_AUX_CMD:
                aux = aux.model_copy(update={"query_cmd": _DEFAULT_AUX_CMD[aux.alias]})
            self._add_aux_row(aux)
        # 모드 라디오 복원(aux 재구성 뒤 → _on_mode_changed의 소스 계산이 최신 aux 반영)
        (self._rb_follow if a.follow_external else self._rb_driven).setChecked(True)
        self._on_mode_changed()

    def _ds_f(self, le: QLineEdit, default: float) -> float:
        try:
            return float(le.text().strip())
        except (ValueError, AttributeError):
            return default

    def _ds_i(self, le: QLineEdit, default: int) -> int:
        try:
            return int(float(le.text().strip()))
        except (ValueError, AttributeError):
            return default

    def _collect_cfg(self) -> MfliConfigData:
        acq = MfliAcquireConfig(
            mfli_alias=self._le_alias.text().strip() or "MFLI",
            follow_external=self._rb_follow.isChecked(),
            poll_interval=max(0.05, self._ds_f(self._le_poll, 1.0)),
            poll_count=max(0, self._ds_i(self._le_polln, 0)),
            sweep_start=self._ds_f(self._le_start, 1e6),
            sweep_stop=self._ds_f(self._le_stop, 1e7),
            sweep_n=max(1, self._ds_i(self._le_n, 1)),
            freq_write_cmd=self._le_freq_cmd.text().strip() or "/{dev}/oscs/0/freq",
            noise_node=self._le_noise.text().strip(),
            noise_label=self._le_noise_label.text().strip() or "noise",
            noise_unit=self._le_noise_unit.text().strip(),
            aux_reads=[r.to_model() for r in self._aux_rows],
            m_value=self._le_m.text().strip(),
            filename=self._le_filename.text().strip() or "mfli_noise",
        )
        return MfliConfigData(
            acquire=acq,
            plot_curves=[_plot_config(self._panel)],
            main_folder=self._le_main.text().strip(),
            sub_folder=self._le_sub.text().strip(),
            save_enabled=self._cb_save.isChecked(),
            merge_labone=self._le_merge_csv.text().strip(),
            merge_ours=self._le_merge_dat.text().strip(),
            merge_outdir=self._le_merge_out.text().strip(),
        )

    # ------------------------------------------------------------------
    # Plot sources
    # ------------------------------------------------------------------
    def _source_labels(self) -> List[str]:
        labels = ["index"]
        for label, _u in self._extra_cols:
            labels.append(label)
        return labels

    def _current_col_meta(self) -> List[tuple]:
        """현재 UI 기준 열 메타 (label, unit) 목록: frequency, noise, *활성 aux.

        라벨은 유일해야 한다 — 플롯 data dict와 소스 콤보가 라벨을 key로 쓰므로 중복 라벨은
        곡선/열이 서로 덮어써 사라진다. 중복이면 _2, _3… 접미사로 유일화한다(저장 .dat 열 순서는
        위치 기반이라 영향 없음)."""
        cols: List[tuple] = []
        seen = set()

        def add(label: str, unit: str):
            label = label or "col"
            if label in seen:
                k = 2
                while f"{label}_{k}" in seen:
                    k += 1
                label = f"{label}_{k}"
            seen.add(label)
            cols.append((label, unit))

        if self._rb_follow.isChecked():
            add("time", "s")          # follow 모드: 워커가 elapsed time을 첫 열로 emit
        add("frequency", "Hz")
        # noise 열은 noise node가 지정됐을 때만 (Option A follow: 비우면 생략, LabOne이 담당)
        if self._le_noise.text().strip():
            add(self._le_noise_label.text().strip() or "noise",
                self._le_noise_unit.text().strip())
        for r in self._aux_rows:
            m = r.to_model()
            if m.enabled and m.alias and m.query_cmd:
                add(m.label, m.unit)
        return cols

    def _refresh_plot_sources(self):
        # 항상 현재 UI 기준으로 재계산한다 — 프로파일 전환 시 이전 프로파일의 stale 라벨이
        # 남아 restore_config의 findData가 조용히 실패하는 것을 막는다. (측정 중엔 호출 안 됨)
        self._extra_cols = self._current_col_meta()
        self._panel.update_sources(self._source_labels())

    def _restore_plot_config(self):
        self._panel.set_default_x("frequency")
        curves = self._cfg.plot_curves
        if curves:
            self._panel.restore_state(_plot_state(curves[0]))
        # y가 유효/의미있게 선택됐는지 보장 (follow Option A는 noise 컬럼이 없어 index로 떨어짐)
        sources = self._source_labels()
        ys = [y for y in self._panel.y_sources() if y in sources]
        if not ys or ys == ["index"]:
            # 우선순위: noise(노드 있을 때) → 첫 aux(probe_T 등) → frequency
            cands = ([self._le_noise_label.text().strip()] if self._le_noise.text().strip() else []) \
                + [l for (l, _u) in self._extra_cols if l not in ("time", "frequency")] \
                + ["frequency"]
            for c in cands:
                if c and c in sources:
                    self._panel.set_default_y(c)
                    break

    def _on_mode_changed(self, *args):
        """driven ↔ follow 전환 — 관련 입력 행 표시/숨김 + 플롯 소스(time 열) 갱신."""
        follow = self._rb_follow.isChecked()
        if hasattr(self, "_row_driven"):
            self._row_driven.setVisible(not follow)
            self._row_follow.setVisible(follow)
        self._refresh_plot_sources()

    # ------------------------------------------------------------------
    # Acquire
    # ------------------------------------------------------------------
    def _start_acquire(self):
        if _qthread_running(self._acq_thread):
            self._set_status("Acquire already running.", "#888")
            return
        # 최신 UI → 설정 저장 (재시작 후에도 유지)
        self._cfg = self._collect_cfg()
        save_mfli_config(self._cfg, self._config_path())
        a = self._cfg.acquire

        if a.follow_external:
            # follow(Option A): LabOne이 주파수를 몰고 우리는 읽기만. noise는 선택(비우면 생략).
            freqs = None
            if not (a.freq_write_cmd or "").strip():
                self._set_status("현재 주파수를 읽을 freq node 경로가 필요합니다.", "#f78166")
                return
        else:
            # driven: 우리가 직접 sweep — noise node 필수.
            if not a.noise_node:
                self._set_status("noise node 경로를 입력하세요.", "#f78166")
                return
            if a.sweep_n < 1:
                self._set_status("Points는 1 이상이어야 합니다.", "#f78166")
                return
            freqs = list(np.linspace(a.sweep_start, a.sweep_stop, a.sweep_n))

        # 열 메타 + 누적 버퍼 초기화 (워커 행 순서와 동일: freq, noise, *활성 aux)
        self._extra_cols = self._current_col_meta()
        self._acc = [[] for _ in self._extra_cols]        # 현재 sweep 저장 버퍼 (wrap마다 flush)
        self._plot_acc = [[] for _ in self._extra_cols]   # 플롯용(현재 sweep)
        self._sweep_min = self._sweep_max = self._last_plot_freq = None
        self._sweep_dir = 0
        self._step_count = 0
        self._saved_files = 0
        self._acq_stopped = False
        self._panel.clear_data()
        self._refresh_plot_sources()

        # 사용할 alias 미리 open (실패 시 중단)
        aliases = [a.mfli_alias] + [r.to_model().alias for r in self._aux_rows
                                    if r.to_model().enabled and r.to_model().alias
                                    and r.to_model().query_cmd]
        for alias in dict.fromkeys(aliases):     # 중복 제거·순서 유지
            try:
                self._session.open(alias)
            except Exception as e:
                self._set_status(f"'{alias}' 연결 실패: {type(e).__name__}: {e}", "#f78166")
                return

        # 저장 폴더 (한 sweep = 파일 1개; 폴더는 run별로 묶음)
        self._sweep_folder: Optional[Path] = None
        if self._cb_save.isChecked():
            base = Path(self._le_main.text().strip() or ".")
            sub = self._le_sub.text().strip()
            base_dir = base / sub if sub else base
            self._sweep_folder = next_sweep_folder(base_dir, a.filename)

        worker = _MfliAcquireWorker(self._session, a, freqs)
        thread = QThread()   # parent 없음 (창 파괴 시 std::terminate 방지)
        worker.moveToThread(thread)
        # 워커→GUI는 반드시 bound @Slot (lambda 금지)
        thread.started.connect(worker.run)
        worker.step_done.connect(self._on_acq_step_done)
        worker.step_elapsed.connect(self._on_step_elapsed)
        worker.progress.connect(self._on_acq_progress)
        worker.error.connect(self._on_acq_error)
        worker.finished.connect(self._on_acq_finished_slot)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_acq_refs)

        self._acq_worker = worker
        self._acq_thread = thread
        self._set_acquire_busy(True)
        thread.start()

    def _clear_acq_refs(self):
        self._acq_worker = None
        self._acq_thread = None

    def _freq_col_index(self):
        for k, (label, _u) in enumerate(self._extra_cols):
            if label == "frequency":
                return k
        return None

    @Slot(int, list)
    def _on_acq_step_done(self, step_idx: int, arrays: List[np.ndarray]):
        self._step_count += 1

        # follow 모드: 주파수 wrap(새 sweep 시작) 감지 → 직전 sweep을 '즉시 파일로 저장'하고
        # 저장/플롯 버퍼를 비운다. (긴 측정에서 Stop 전에 데이터가 날아가지 않도록: 한 sweep이
        # 끝날 때마다 새 .dat 파일 생성. 마지막 부분 sweep은 finished 슬롯에서 저장.)
        if getattr(self, "_rb_follow", None) is not None and self._rb_follow.isChecked():
            fi = self._freq_col_index()
            if fi is not None and fi < len(arrays) and len(arrays[fi]):
                freq = float(arrays[fi][0])
                if self._last_plot_freq is None:
                    self._sweep_min = self._sweep_max = freq
                    self._sweep_dir = 0
                else:
                    diff = freq - self._last_plot_freq
                    span = (self._sweep_max - self._sweep_min) if self._sweep_max is not None else 0.0
                    # wrap = sweep 진행방향과 '반대'로 범위의 절반 넘게 점프(정상 스텝은 같은 방향·소폭)
                    if (self._sweep_dir != 0 and diff * self._sweep_dir < 0
                            and span > 0 and abs(diff) > 0.5 * span):
                        saved = self._save_sweep_file(self._acc)   # 직전 sweep flush
                        if saved:
                            self._set_status(
                                f"sweep {self._saved_files} 저장 → {saved}  (측정 계속)", "#7ee787")
                        self._acc = [[] for _ in self._extra_cols]
                        self._plot_acc = [[] for _ in self._extra_cols]
                        self._sweep_min = self._sweep_max = freq
                        self._sweep_dir = 0
                    else:
                        if freq < self._sweep_min: self._sweep_min = freq
                        if freq > self._sweep_max: self._sweep_max = freq
                        if self._sweep_dir == 0 and diff != 0:
                            self._sweep_dir = 1 if diff > 0 else -1
                self._last_plot_freq = freq

        # 현재 점을 저장 버퍼(_acc)와 플롯 버퍼(_plot_acc) 양쪽에 추가.
        # (버퍼 분리 유지: 수동 'Clear plot'은 _plot_acc만 비우고 _acc(저장)는 보존.)
        try:
            for i in range(min(len(self._acc), len(arrays))):
                arr = arrays[i]
                self._acc[i].append(float(arr[0]) if len(arr) else float("nan"))
        except Exception:
            from pythonization.app.logging_setup import get_logger
            get_logger().exception("mfli accumulate failed (step %d)", step_idx)
        try:
            for i in range(min(len(self._plot_acc), len(arrays))):
                arr = arrays[i]
                self._plot_acc[i].append(float(arr[0]) if len(arr) else float("nan"))
        except Exception:
            pass

        # 플롯 갱신 (현재 sweep만) — 새 sweep 첫 점이면 뷰 재맞춤
        try:
            n = len(self._plot_acc[0]) if self._plot_acc else 0
            data = {"index": np.arange(n)}
            for i, (label, _u) in enumerate(self._extra_cols):
                if i < len(self._plot_acc):
                    data[label] = np.asarray(self._plot_acc[i], dtype=float)
            # MFLI는 한 점씩 스트리밍 → 매 스텝 뷰를 자동으로 맞춘다. 안 하면 뷰 범위가 데이터
            # (예: 1e5~2e5 Hz)를 못 따라가 ClipToView로 곡선이 화면 밖으로 잘려 '안 보인다'.
            self._panel.push_data(data, first_step=True)
        except Exception:
            from pythonization.app.logging_setup import get_logger
            get_logger().exception("mfli plot update failed (step %d)", step_idx)

    @Slot(int, float)
    def _on_step_elapsed(self, step_idx: int, elapsed: float):
        pass  # (여유: 필요 시 진행률 표시)

    @Slot(str)
    def _on_acq_progress(self, msg: str):
        self._set_status(msg, "#888")

    @Slot(str)
    def _on_acq_error(self, msg: str):
        self._set_status(f"Error: {msg}", "#f78166")

    def _save_sweep_file(self, buf) -> Optional[str]:
        """완료된(또는 마지막) sweep 1개를 새 .dat 파일로 저장하고 파일명을 반환.
        저장 off·폴더 없음·빈 버퍼면 None. (한 sweep = 파일 1개 → 긴 측정 중 데이터 유실 방지.)"""
        if not self._cb_save.isChecked() or self._sweep_folder is None:
            return None
        if not buf or not any(len(c) for c in buf):
            return None
        try:
            path = next_dat_path(self._sweep_folder, self._cfg.acquire.filename)
            self._write_dat(path, buf)
            self._saved_files += 1
            return path.name
        except Exception as e:
            self._set_status(f"Save error: {e}", "#f78166")
            return None

    @Slot()
    def _on_acq_finished_slot(self):
        # 마지막(부분 포함) sweep 저장. follow에선 앞선 sweep들은 wrap 시점에 이미 저장됨.
        if self._cb_save.isChecked() and self._sweep_folder is not None:
            saved = self._save_sweep_file(self._acc)
            if saved:
                self._set_status(
                    f"Done — 총 {self._saved_files}개 sweep 파일 저장 (마지막: {saved})", "#7ee787")
            elif self._saved_files > 0:
                self._set_status(f"Done — 총 {self._saved_files}개 sweep 파일 저장.", "#7ee787")
            else:
                self._set_status(f"Done — {self._step_count} pts (저장할 데이터 없음).", "#7ee787")
        else:
            self._set_status(f"Done — {self._step_count} pts.", "#7ee787")
        self._set_acquire_busy(False)

    def _write_dat(self, path: Path, buf: List[List[float]]):
        """한 sweep의 열 데이터를 탭 구분 .dat로 저장 (헤더 2줄: 라벨/단위)."""
        cols = buf
        meta = self._extra_cols
        long_names = [m[0] for m in meta]
        units = [m[1] for m in meta]
        n = max((len(c) for c in cols), default=0)
        lines = ["\t".join(long_names), "\t".join(units)]
        for r in range(n):
            vals = [f"{cols[c][r]:.10E}" if r < len(cols[c]) else "" for c in range(len(cols))]
            lines.append("\t".join(vals))
        path.write_text("\n".join(lines), encoding="utf-8")

    def _on_stop_acquire(self):
        self._acq_stopped = True
        if self._acq_worker is not None:
            self._acq_worker.stop()
        self._set_status("Stopping…", "#888")

    def _clear_plot(self):
        """플롯 표시를 즉시 비운다(저장 버퍼 _acc는 건드리지 않음). wrap 감지 상태도 리셋."""
        self._plot_acc = [[] for _ in self._extra_cols] if self._extra_cols else []
        self._sweep_min = self._sweep_max = self._last_plot_freq = None
        self._sweep_dir = 0
        try:
            self._panel.clear_data()
        except Exception:
            pass
        self._set_status("플롯을 비웠습니다.", "#888")

    def _set_acquire_busy(self, busy: bool):
        self._btn_start.setEnabled(not busy)
        self._btn_stop.setEnabled(busy)

    def _set_status(self, msg: str, color: str = "#888"):
        self._lbl_status.setStyleSheet(f"color: {color};")
        self._lbl_status.setText(msg)

    # ------------------------------------------------------------------
    # Shutdown (main_window.closeEvent 에서 session.shutdown() 이전에 호출)
    # ------------------------------------------------------------------
    def shutdown_threads(self, timeout_ms: int = 5000):
        try:
            if self._acq_worker is not None:
                self._acq_worker.stop()
        except RuntimeError:
            pass
        th = self._acq_thread
        if _qthread_running(th):
            try:
                th.quit()
                th.wait(timeout_ms)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # Close / hide (VNA 창과 동일 — 실수로 닫아 측정 워커가 방치되지 않도록)
    # ------------------------------------------------------------------
    def _clear_graph_data(self):
        self._step_count = 0
        self._panel.clear_data()

    def _save_and_hide(self):
        """저장 + 그래프 초기화 + 숨김 (닫기/Escape 공통). 창은 파괴하지 않고 유지 →
        실제 스레드 정지는 main_window.closeEvent의 shutdown_threads()가 담당."""
        self._save_ui_state()
        self._clear_graph_data()
        self.hide()

    def closeEvent(self, event):
        event.ignore()
        self._save_and_hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            event.accept()           # Esc로 닫지 않음 (측정 중 실수 방지)
            return
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_S):
            self._save_ui_state()    # Ctrl+S = 설정 저장(닫지 않음)
            event.accept()
            return
        super().keyPressEvent(event)
