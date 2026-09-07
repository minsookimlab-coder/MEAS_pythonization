"""
VnaWindow: VNA 제어 창 (병렬 동작).

좌측: 섹션 Execute 패널 + Acquire 패널
우측: 두 개의 플롯 (좌우, 각각 x/y source 선택 가능, multi-y 지원)
"""
import os
import time as _time
from dataclasses import dataclass
from pathlib import Path, Path as _P
from typing import List, Optional, TYPE_CHECKING, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from pythonization.app.paths import SETTINGS_DIR
from pythonization.ui.widgets.plot_panel import MONO as _MONO, PlotPanel, PlotPanelState
from pythonization.ui.modules.vna.models import (
    VnaAcquireConfig,
    VnaCommandEntry,
    VnaDoubleSweepControl,
    VnaFieldTimeConfig,
    VnaFinishReturnConfig,
    VnaPlotCurveConfig,
    VnaPowerSweepConfig,
    VnaPreCmdValue,
    VnaResumeState,
    VnaSectionConfig,
    build_cmd,
    clear_resume_state,
    format_label,
    get_figure_axis,
    get_template,
    load_resume_state,
    load_vna_config,
    next_dat_path,
    next_sweep_folder,
    parse_vna_array,
    resume_path_for,
    save_resume_state,
    save_vna_config,
)
from pythonization.app.logging_setup import get_logger
from pythonization.config.models import (
    InstantiatedSecondSweepChannel,
    SecondSweepAdvanceType as A,
)
from pythonization.measurement.second_channel_model import SecondChannelModel
from pythonization.notify.alarm_manager import AlarmManager
from pythonization.ui.dialogs.alarm_config import AlarmConfigWindow
from pythonization.ui.modules.vna.config_window import (
    VnaConfigWindow,
    _PreAdvanceCmdDialog,
)
from pythonization.ui.widgets.help_button import make_help_button

if TYPE_CHECKING:
    from pythonization.instruments.session import InstrumentSession
    from pythonization.instruments.command_library import VisaLibraryRegistry
    from pythonization.profiles.registry import ProfileRegistry

_DEFAULT_CONFIG_PATH = SETTINGS_DIR / "vna_config.yaml"


@dataclass
class _FirstChannel:
    """double sweep 의 안쪽 축 — 매 second 값마다 이 값들을 훑는다."""
    cmd: object
    values: list
    advance: object
    field_time: bool    # True 면 N 단계가 아니라 시간 기반(ramp → HOLD 대기)


@dataclass
class _SecondChannel:
    """double sweep 의 바깥 축 (bias/온도). enabled=False 면 단일 sweep 이다."""
    enabled: bool
    cmd: object
    values: list
    advance: object

    @property
    def is_active(self) -> bool:
        """실제로 값을 훑는 second 채널이 있는가 (하위폴더·데이터 열 판단용)."""
        return self.enabled and self.cmd is not None


#: _resolve_second_channel 이 '입력 오류' 를 알리는 표식.
#: None 은 '채널을 안 씀'이라는 정상 상태라서 구분이 필요하다.
_INVALID = object()

# unit_type → [(label, multiplier), ...]
_UNIT_OPTIONS: dict = {
    "Hz":  [("Hz", 1.0), ("kHz", 1e3), ("MHz", 1e6), ("GHz", 1e9)],
    "sec": [("ns", 1e-9), ("us", 1e-6), ("ms", 1e-3), ("s", 1.0)],
    "T":   [("uT", 1e-6), ("mT", 1e-3), ("T", 1.0)],          # 자기장
    "K":   [("mK", 1e-3), ("K", 1.0)],                        # 온도
}
_UNIT_DEFAULT = {"Hz": "MHz", "sec": "ms", "T": "T", "K": "K"}

# Role button styles: key = role name (or "" for inactive)
_ROLE_BTN_INACTIVE = (
    "QPushButton{background:#252525;color:#555;border:1px solid #3a3a3a;"
    "border-radius:2px;font-size:8px;font-weight:bold;padding:0px;}"
    "QPushButton:hover{background:#333;color:#888;}"
)
_ROLE_BTN_STYLES = {
    "start":    ("QPushButton{background:#1f4e1b;color:#7ee787;border:1px solid #3d7a31;"
                 "border-radius:2px;font-size:8px;font-weight:bold;padding:0px;}"),
    "stop":     ("QPushButton{background:#4e1b1b;color:#f78166;border:1px solid #7a3131;"
                 "border-radius:2px;font-size:8px;font-weight:bold;padding:0px;}"),
    "n_points": ("QPushButton{background:#1b334e;color:#79c0ff;border:1px solid #315280;"
                 "border-radius:2px;font-size:8px;font-weight:bold;padding:0px;}"),
}


def _qthread_running(t) -> bool:
    """QThread가 살아있고 실행 중인지 안전 확인.
    PySide6는 삭제된 QObject 접근 시 RuntimeError를 던지므로(네이티브 크래시 아님)
    그걸 잡아 False로 처리한다."""
    try:
        return bool(t) and t.isRunning()
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# Section execute worker
# ---------------------------------------------------------------------------

class _VnaWorker(QThread):
    done  = Signal(list)
    error = Signal(str)

    def __init__(self, session, commands: List[Tuple[str, str, bool]]):
        super().__init__()
        self._session  = session
        self._commands = commands

    def run(self):
        results = []
        try:
            for alias, cmd, is_query in self._commands:
                if not self._session.is_open(alias):
                    self._session.open(alias)
                if is_query:
                    r = self._session.query(alias, cmd)
                    results.append((True, str(r).strip()))
                else:
                    self._session.write(alias, cmd)
                    results.append((False, None))
            self.done.emit(results)
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Acquire worker  (Single & Sweep)
# ---------------------------------------------------------------------------

class _AcquireWorker(QObject):
    step_done   = Signal(int, list)   # (step_idx, [np.ndarray, ...])  — 정규화된 배열
    step_timing = Signal(int, float)  # (step_idx, remaining_sec)  양수=idle, 음수=overrun
    step_elapsed = Signal(int, float)  # (step_idx, elapsed_sec)  한 스텝 acquire 소요 시간
    second_changed = Signal(object)   # second 채널 값(또는 None) — 저장 하위폴더 분기용
    second_done = Signal(int)         # second 한 값의 full sweep 완료 — 전역 인덱스 (resume용)
    second_row  = Signal(int, str)    # (전역 인덱스, "current"|"done") — 테이블 행 색칠
    finished    = Signal()
    error       = Signal(str)
    progress    = Signal(str)
    #: 데이터는 다 모았는데 뒷정리(복귀)만 실패 — 측정을 '오류'로 표시하면 안 되지만
    #: 장비가 엉뚱한 값에 남았을 수 있어 조용히 넘기면 안 되는 경우.
    warn        = Signal(str)

    def __init__(self, session, lib_reg, acq_cfg: VnaAcquireConfig,
                 sweep_values=None,
                 sweep_cmd: Optional[VnaCommandEntry] = None,
                 time_mode: bool = False,
                 time_interval: float = 1.0,
                 time_count: int = 1,
                 ds_plan: Optional[dict] = None):
        super().__init__()
        self._session      = session
        self._lib_reg      = lib_reg
        self._acq          = acq_cfg
        self._sweep_values = sweep_values
        self._sweep_cmd    = sweep_cmd   # 선택된 단일 sweep 명령어
        self._time_mode    = time_mode
        self._time_interval = time_interval
        self._time_count    = time_count
        self._ds_plan      = ds_plan     # double sweep 계획 (stage D)
        self._stop_flag    = False
        self._ref_len: Optional[int] = None   # 첫 스텝에서 확정된 기준 array 길이
        self._sec = None                 # advance 재사용용 SecondChannelWorker
        self._sec_stop = None
        #: 값이 없어 건너뛴 sweep 명령 (같은 것을 매 스텝 알리지 않기 위한 기록)
        self._skipped_blank: set = set()

    def stop(self):
        self._stop_flag = True
        if self._sec_stop is not None:
            self._sec_stop.set()

    @Slot()
    def run(self):
        log = get_logger()
        mode = ("double-sweep" if self._ds_plan is not None
                else "time" if self._time_mode else "value")
        log.info("AcquireWorker start (mode=%s)", mode)
        try:
            if self._ds_plan is not None:
                self._run_double_sweep()
            elif self._time_mode:
                self._run_time_mode()
            else:
                self._run_value_mode()
        except Exception as e:
            log.exception("AcquireWorker fatal")   # 전체 트레이스백을 로그파일에 기록
            self.error.emit(f"Fatal: {type(e).__name__}: {e}")
        finally:
            # _sec(SecondChannelWorker)은 이 워커 스레드에서 생성됐다. 여기(아직 워커
            # 스레드)서 연결 해제 후 파괴해야 한다 — 메인 스레드 GC로 cross-thread 파괴되면
            # QObject 파괴가 undefined가 되어 드물게 네이티브 크래시가 날 수 있다.
            if self._sec is not None:
                try:
                    self._sec.status.disconnect()
                    self._sec.feedback_progress.disconnect()
                except Exception:
                    pass
                try:
                    self._sec.deleteLater()
                except Exception:
                    pass
                self._sec = None
            log.info("AcquireWorker finished (mode=%s)", mode)
            self.finished.emit()   # 항상 emit — 정상/에러/중단 모두

    def _run_value_mode(self):
        values = self._sweep_values if self._sweep_values is not None else [None]
        for i, sv in enumerate(values):
            if self._stop_flag:
                break
            sv_str = f"{sv:.6g}" if sv is not None else ""
            self.progress.emit(
                f"Step {i + 1}/{len(values)}"
                + (f"  val={sv_str}" if sv_str else ""))
            t0 = _time.perf_counter()
            try:
                sweep_list = ([self._sweep_cmd] if self._sweep_cmd is not None
                              else self._acq.sweep_cmds)
                self._exec_write_cmds(sweep_list, sv_str)
                self._exec_write_cmds(self._acq.start_cmds, sv_str)
                self._exec_wait_cmds(self._acq.wait_cmds)
                arrays = self._exec_read_cmds(self._acq.read_cmds)
                # sweep acquire: write한 값도 데이터 열로 (길이 맞춰 array화)
                if self._sweep_cmd is not None and sv is not None:
                    arrays.append(np.array([float(sv)]))
                arrays = self._normalize_arrays(arrays, first_step=(i == 0))
                self.step_elapsed.emit(i, _time.perf_counter() - t0)
                self.step_done.emit(i, arrays)
            except Exception as e:
                self.error.emit(f"Step {i + 1}: {type(e).__name__}: {e}")
                break

    def _run_time_mode(self):
        """일정 간격마다 acquire 1회씩 time_count번 반복.

        각 스텝의 read 완료까지 소요 시간을 측정해:
          remaining = interval - elapsed
          remaining > 0 → idle (그만큼 대기), remaining < 0 → overrun(부족분)
        """
        n = max(1, self._time_count)
        interval = max(0.0, self._time_interval)
        for i in range(n):
            if self._stop_flag:
                break
            self.progress.emit(f"Time step {i + 1}/{n}")
            t0 = _time.perf_counter()
            try:
                # 단일 acquire와 동일한 cycle (sweep_cmds[빈값] → start → wait → read)
                self._exec_write_cmds(self._acq.sweep_cmds, "")
                self._exec_write_cmds(self._acq.start_cmds, "")
                self._exec_wait_cmds(self._acq.wait_cmds)
                arrays = self._exec_read_cmds(self._acq.read_cmds)
                arrays = self._normalize_arrays(arrays, first_step=(i == 0))
                self.step_done.emit(i, arrays)
            except Exception as e:
                self.error.emit(f"Time step {i + 1}: {type(e).__name__}: {e}")
                break
            elapsed   = _time.perf_counter() - t0
            remaining = interval - elapsed
            self.step_timing.emit(i, remaining)
            # 남는 시간만큼 대기 (마지막 스텝 제외). stop을 빠르게 반영하도록 분할 sleep.
            if i < n - 1 and remaining > 0:
                deadline = _time.perf_counter() + remaining
                while not self._stop_flag and _time.perf_counter() < deadline:
                    # max(0.0, ...) 필수: while 조건과 sleep 인자의 perf_counter() 호출 사이에
                    # deadline을 넘기면 음수가 되어 sleep()이 ValueError로 죽는다. 0으로 클램프.
                    _time.sleep(max(0.0, min(0.05, deadline - _time.perf_counter())))

    # ---- Double sweep engine (stage D) -------------------------------

    def _finish_second(self, use_tbl, tbl, gidx):
        """한 second 값 완료 처리 — 테이블 done 표시 + second_done emit."""
        if self._stop_flag:
            return
        if use_tbl and tbl is not None:
            tbl.mark_done(gidx)
            self.second_row.emit(gidx, "done")
        self.second_done.emit(gidx)

    def _run_double_sweep(self):
        p = self._ds_plan
        ft = p.get("field_time")
        ft_on = bool(ft and ft.get("enabled"))
        first_vals  = p["first_values"]
        pw = p.get("power")          # Power Sweep 모드 설정 (아니면 None)
        # Power 모드는 항상 power Start→Stop 순서로 훑는다 (방향 교대 없음)
        uni = (p["direction"] == "uni") or pw is not None
        offset = p.get("second_index_offset", 0)   # resume: 건너뛴 second 개수 (전역 인덱스)
        tbl = p.get("second_table")                # SecondChannelModel (Feature 1) or None
        use_tbl = bool(p["second_enabled"] and tbl is not None)
        static_second = p["second_values"] if p["second_enabled"] else [None]
        idx = 0
        second_prev = None
        first_pos = None   # first(자기장) 채널의 마지막 알려진 위치 — 전체 스윕에 걸쳐 추적

        # ── Feature 2: field-time 즉시 시작 — 첫 실행(offset==0)에만, second를 목표로
        #    이동(대기)하지 않고 '현재 온도에서' 정상 field sweep을 1회 선행한다. ──
        if ft_on and p.get("field_initial_sweep") and offset == 0 and not self._stop_flag:
            self.second_changed.emit(("__initial__", None))
            try:
                idx, first_pos = self._run_field_time(
                    p, idx, None, si=0, uni=uni, is_last=False,
                    first_pos=first_pos, ctx="[초기 sweep@현재온도] ")
            except Exception as e:
                self.error.emit(f"초기 field sweep 실패: {type(e).__name__}: {e}")
                return
            # second_done은 emit하지 않는다 (resume 인덱스는 실제 second 행 기준 유지)

        si = 0
        while not self._stop_flag:
            # 매 행 값을 (테이블이면) 모델에서 '새로' 읽는다 → 측정 중 미래 행 편집/추가 반영
            if use_tbl:
                n2 = tbl.count()
                gidx = offset + si
                sv, ok = tbl.value_at(gidx)
                if not ok:
                    break
            else:
                if si >= len(static_second):
                    break
                n2 = len(static_second)
                gidx = offset + si
                sv = static_second[si]

            if use_tbl:
                tbl.mark_current(gidx)
                self.second_row.emit(gidx, "current")   # 색칠은 second_changed보다 먼저
            self.second_changed.emit(sv)
            ctx = (f"[Second {si+1}/{n2}={sv:.6g}] "
                   if (p["second_enabled"] and sv is not None) else "")
            if p["second_enabled"]:
                if pw is not None:
                    # Power 모드: 속도 → 목표 → ramp 시작 → 도달 → 안정화 대기
                    self._pw_advance_field(p, sv, second_prev, ctx)
                else:
                    self.progress.emit(f"{ctx}Second 채널 이동 중 → {sv:.6g}")
                    self._advance(p["second_cmd"], p["second_adv"], sv, second_prev)
                second_prev = sv
                if self._stop_flag:
                    break

            # ── Double Sweep with Time ──
            if ft_on:
                try:
                    idx, first_pos = self._run_field_time(
                        p, idx, sv, si, uni, is_last=False, first_pos=first_pos, ctx=ctx)
                except Exception as e:
                    self.error.emit(f"Field-Time sweep 실패: {type(e).__name__}: {e}")
                    return
                self._finish_second(use_tbl, tbl, gidx)
                si += 1
                continue

            # ── Standard double sweep ──
            order = (list(first_vals) if (uni or si % 2 == 0)
                     else list(reversed(first_vals)))
            nf = len(order)
            # Power 모드는 아래 루프의 첫 스텝이 이미 Start 로 이동하므로 '시작점 복귀'가
            # 필요 없다. pre_cmds 도 first 축이 자기장일 때를 위한 것이라 여기선 건너뛴다.
            if uni and pw is None and not self._stop_flag:
                self.progress.emit(f"{ctx}① 시작점 복귀 → {first_vals[0]:.6g} (dummy 속도)")
                self._run_pre_cmds("dummy")
                try:
                    self._advance(p["first_cmd"], p["first_adv"], first_vals[0], first_pos)
                    first_pos = first_vals[0]
                except Exception as e:
                    self.error.emit(f"시작점 복귀 실패: {type(e).__name__}: {e}")
                    return
                self._run_pre_cmds("sweep")

            for k, fv in enumerate(order, 1):
                if self._stop_flag:
                    break
                axis = "Power" if pw is not None else "First"
                self.progress.emit(f"{ctx}② {axis} {k}/{nf} 이동 중 → {fv:.6g}")
                try:
                    self._advance(p["first_cmd"], p["first_adv"], fv, first_pos)
                    first_pos = fv
                    if pw is not None:
                        # 이동 직후엔 아직 값이 안정되지 않았을 수 있다 → 측정 전 대기
                        self._sleep_progress(pw["pre_measure_s"],
                                             f"{ctx}{axis} {fv:.6g} — 측정 전 대기")
                        if self._stop_flag:
                            break
                    self.progress.emit(f"{ctx}③ {axis} {k}/{nf} 측정 중 (val={fv:.6g})")
                    extra = [fv] + ([sv] if (p["second_enabled"] and sv is not None) else [])
                    self._acquire_once(idx, first_step=(idx == 0), extra_values=extra)
                    if pw is not None:
                        self._sleep_progress(pw["post_measure_s"],
                                             f"{ctx}{axis} {fv:.6g} — 측정 후 대기")
                except Exception as e:
                    self.error.emit(f"Step {idx+1}: {type(e).__name__}: {e}")
                    return
                idx += 1
            self._finish_second(use_tbl, tbl, gidx)
            si += 1

        if not self._stop_flag:
            self._run_finish_return(p, first_pos, second_prev)

    def _sleep_progress(self, seconds: float, label: str):
        """남은 시간을 알리며 대기한다. Stop 이 걸리면 즉시 빠져나온다.

        1분·5초 같은 고정 대기가 '멈춘 것처럼' 보이지 않도록 초 단위로 남은 시간을
        상태줄에 찍는다.
        """
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return
        if seconds <= 0:
            return
        deadline = _time.perf_counter() + seconds
        next_tick = 0.0
        while not self._stop_flag:
            now = _time.perf_counter()
            remaining = deadline - now
            if remaining <= 0:
                return
            if now >= next_tick:
                self.progress.emit(f"⏳ {label} — {remaining:.0f}s 남음")
                next_tick = now + 1.0
            # max(0.0, ...) 필수: 두 perf_counter() 호출 사이에 deadline 을 넘기면
            # 음수가 되어 sleep 이 ValueError 로 죽는다.
            _time.sleep(max(0.0, min(0.05, remaining)))

    # ---- Power Sweep 모드: 자기장 이동 ---------------------------------

    def _pw_advance_field(self, p, target, prev, ctx=""):
        """Power 모드의 자기장 한 점 이동.

        ① 변화 속도 설정  ② 목표값 전송  ③ ramp 시작 트리거
        ④ 도달 + 안정화까지 폴링  ⑤ 그 뒤 고정 시간 대기 — 항상 이 순서로 밟는다.

        ②를 advance 방식(feedback 등)에 맡기지 않고 단순 write 로 하는 이유: Mercury iPS
        처럼 '목표 설정 → RTOS 트리거' 가 따로인 장비는 write 와 폴링 **사이에** ③이
        들어가야 하는데, advance 안에서는 그 자리를 만들 수 없다.
        """
        pw = p["power"]
        if pw.get("rate_cmds"):
            self.progress.emit(f"{ctx}① 자기장 변화 속도 {pw['field_rate']:g} 설정")
            self._run_cmd_list(pw["rate_cmds"], pw["field_rate"])
        if self._stop_flag:
            return
        self.progress.emit(f"{ctx}② 자기장 목표 {target:.6g} 전송")
        self._advance(p["second_cmd"], None, target, prev)   # adv=None → 단순 write
        if pw.get("go_cmds"):
            self.progress.emit(f"{ctx}③ 자기장 ramp 시작")
            self._run_cmd_list(pw["go_cmds"], target)
        if self._stop_flag:
            return
        self._pw_wait_arrival(p, target, prev, ctx)
        if self._stop_flag:
            return
        self._sleep_progress(pw["field_settle_s"],
                             f"{ctx}자기장 {target:.6g} 안정 — 추가 대기")

    def _pw_wait_arrival(self, p, target, prev, ctx=""):
        """자기장이 target 에 도달하고 **안정될 때까지** 기다린다 (값을 다시 쓰지 않는다).

        판정 기준은 second 채널의 Controlled advance 설정을 그대로 쓴다 —
        일반 double sweep 의 feedback 과 같은 2단계다:
          Phase 1  |읽은값 − 목표| 가 tolerance band 안에 들어올 때까지 폴링
          Phase 2  최근 std_window 개 샘플의 흔들림이 std_threshold 아래로 내려갈 때까지

        Phase 2 는 std_window·std_threshold 가 둘 다 설정돼 있을 때만 돈다(0 이면 생략).
        **이미 목표값에 있어도 Phase 2 는 확인한다** — 도달했다고 안정된 것은 아니다.
        read cmd 가 없으면 도달을 확인할 방법이 없으므로 통과한다(시작 전에 경고한다).
        """
        adv = p["second_adv"]
        cmd = p["second_cmd"]
        read_cmd = (getattr(adv, "feedback_read_cmd", "") or "").strip() if adv else ""
        if not read_cmd or cmd is None:
            return
        alias = cmd.alias
        if not self._session.is_open(alias):
            self._session.open(alias)
        if prev is None:
            prev = self._read_current(cmd, adv)
        distance = abs(target - (prev if prev is not None else 0.0))

        sec = self._ensure_sec()
        ch = self._make_sec_channel(cmd, adv)
        use_std = ch.feedback_std_window > 0 and ch.feedback_std_threshold > 0.0

        if distance >= 1e-12:
            self.progress.emit(f"{ctx}④ 자기장 {target:.6g} 도달 대기 중…")
            # 값을 다시 쓰지 않는 '도달 판정' 전용 경로 (워치독·통신오류 처리 포함)
            if not sec._await_feedback_target(alias, ch, target, distance):
                return          # Stop 요청
        if use_std and not self._stop_flag:
            self.progress.emit(
                f"{ctx}④ 자기장 {target:.6g} 안정화 대기 중… "
                f"(최근 {ch.feedback_std_window}개 std < {ch.feedback_std_threshold:g})")
            sec._await_feedback_stability(alias, ch, target)

    def _run_finish_return(self, p, first_pos, second_prev):
        """모든 측정이 끝난 뒤 각 축을 지정값으로 되돌린다 (측정 없음).

        **First 를 먼저** 내리고 그다음 Second 를 옮긴다 — 시료에 신호(power/bias)를
        걸어 둔 채 마그넷을 움직이지 않기 위해서다. Second 이동은 그 채널의 advance
        방식을 그대로 쓰므로, feedback 이면 실제 도달까지 기다린다.

        Stop 으로 끊겼을 때는 부르지 않는다 — 그 경우엔 사용자가 등록한
        'Stop 시 실행 명령'(예: HOLD)이 대신 나가야 한다.
        """
        ret = p.get("finish_return") or {}

        def _go(label, cmd, adv, value, prev):
            # 복귀 도중 Stop 을 누를 수도 있다 (자기장 하강은 몇 분씩 걸린다).
            # 그때는 남은 축을 건드리지 않고 빠져나가 Stop 명령에 맡긴다.
            if self._stop_flag:
                return
            self.progress.emit(f"⑤ 측정 완료 — {label} → {value:.6g} 복귀 중")
            try:
                self._advance(cmd, adv, value, prev)
            except Exception as e:
                # 데이터는 이미 다 모였다 → error 가 아니라 warn.
                # error 로 올리면 정상 완료가 '중단'으로 표시되고 resume 상태가 남는다.
                self.warn.emit(f"{label} 복귀 실패 ({value:.6g}): {type(e).__name__}: {e}")

        if ret.get("first_enabled") and p["first_cmd"] is not None:
            _go("First", p["first_cmd"], p["first_adv"],
                float(ret.get("first_value", 0.0)), first_pos)
        if (ret.get("second_enabled") and p["second_enabled"]
                and p["second_cmd"] is not None):
            _go("Second", p["second_cmd"], p["second_adv"],
                float(ret.get("second_value", 0.0)), second_prev)

    def _run_field_time(self, p, idx, sv, si, uni, is_last, first_pos, ctx=""):
        """first 채널을 시간 기반으로 측정 (Double Sweep with Time).

        단방향: (0) dummy 속도로 시작점 복귀(첫 회=초기 이동 포함) → (1) sweep 속도로
        목표까지 ramp하며 interval마다 acquire → status HOLD면 완료.
        Feature 3(dummy_measure): 복귀도 '측정'하며 별도 폴더에 저장한다.
        반환값: (갱신된 step index, 갱신된 first_pos).
        """
        ft = p["field_time"]
        first = p["first_cmd"]
        start_v = p["first_values"][0]
        stop_v  = p["first_values"][-1]
        forward = uni or (si % 2 == 0)     # 다중방향이면 second 스텝마다 방향 교대
        target  = stop_v if forward else start_v

        # 0) 단방향: 시작점 복귀 (dummy 속도).
        if uni and not self._stop_flag:
            self._run_pre_cmds("dummy")
            if (p.get("dummy_measure") and first_pos is not None
                    and abs(first_pos - start_v) > 1e-12):
                # Feature 3: 복귀도 측정 — dummy 전용 하위폴더에 저장
                self.second_changed.emit(("__dummy__", sv))
                self.progress.emit(f"{ctx}① dummy 복귀 측정 → {start_v:.6g}")
                idx = self._field_time_ramp_acquire(p, idx, start_v, sv, ctx + "[dummy] ")
                self.second_changed.emit(sv)   # 원래 second 폴더로 복원
            else:
                self.progress.emit(f"{ctx}① 시작점 복귀 → {start_v:.6g} (dummy 속도)")
                self._advance(first, p["first_adv"], start_v, first_pos)
            first_pos = start_v
            self._run_pre_cmds("sweep")
        if self._stop_flag:
            return idx, first_pos

        # 1) 목표로 ramp 시작 + time-based acquire (HOLD까지)
        self.progress.emit(f"{ctx}② {target:.6g} 로 ramp 시작 (sweep 속도)")
        idx = self._field_time_ramp_acquire(p, idx, target, sv, ctx)
        first_pos = target   # ramp 목표 (실제 도달은 HOLD status로 판정)
        return idx, first_pos

    def _field_time_ramp_acquire(self, p, idx, target, sv, ctx):
        """first 채널을 target으로 ramp 시작 후, interval마다 acquire하고 status가
        HOLD면 종료한다. forward sweep과 (Feature 3) dummy 복귀 측정이 공유. → 갱신된 idx."""
        ft = p["field_time"]
        first = p["first_cmd"]
        self._advance(first, None, target, None)            # 단순 write로 목표값 설정
        self._run_cmd_list(ft.get("forward_cmds", []), target)

        interval   = max(0.05, float(ft.get("interval", 5.0)))
        stat_intv  = max(0.05, float(ft.get("status_interval", 2.0)))
        alias      = ft.get("status_alias") or first.alias
        status_cmd = (ft.get("status_cmd") or "").strip()
        hold       = (ft.get("hold_token") or "HOLD").strip().upper()
        acq_count   = 0
        ramp_t0     = _time.perf_counter()
        last_status = ""
        now = ramp_t0
        next_acq  = now            # 첫 acquire는 즉시
        next_stat = now + stat_intv
        while not self._stop_flag:
            now = _time.perf_counter()
            if now >= next_acq:
                acq_count += 1
                el = now - ramp_t0
                self.progress.emit(
                    f"{ctx}③ Field-Time 측정 #{acq_count} | 경과 {el:.0f}s"
                    + (f" | 상태:{last_status}" if last_status else ""))
                # second 값 열: sv가 None(초기 sweep)이면 nan으로 열 수 맞춤
                if p["second_enabled"]:
                    extra = [sv if sv is not None else float("nan")]
                else:
                    extra = []
                self._acquire_once(idx, first_step=(idx == 0), extra_values=extra)
                idx += 1
                next_acq = _time.perf_counter() + interval
            if status_cmd and now >= next_stat:
                try:
                    raw = str(self._session.query(alias, status_cmd)).strip()
                    last_status = (raw.split(":")[-1] or raw)[:16]   # 표시용 짧은 토큰
                    if hold in raw.upper():
                        self.progress.emit(
                            f"{ctx}④ HOLD 도달 — sweep 완료 (총 {acq_count}회 측정)")
                        break
                except Exception:
                    pass               # 일시적 읽기 실패 → 계속 폴링
                next_stat = _time.perf_counter() + stat_intv
            wait = max(0.0, min(next_acq, next_stat) - _time.perf_counter())
            _time.sleep(min(0.1, wait) if wait > 0 else 0.01)
        return idx

    def _run_cmd_list(self, cmds, value):
        """라이브러리 write 명령 목록을 실행. user_input 파라미터엔 value를 채운다."""
        vstr = f"{value:.6g}" if isinstance(value, (int, float)) else str(value)
        for pc in cmds:
            if self._stop_flag:
                return
            try:
                lib = self._lib_reg.get_library(pc.alias)
                template = None
                for e in list(lib.write_cmds) + list(lib.sweep_values):
                    if e.description == pc.description:
                        template = e.cmd_set
                        break
                if template is None:
                    continue
                cmd = build_cmd(template, pc.params, vstr)
                if not self._session.is_open(pc.alias):
                    self._session.open(pc.alias)
                self._session.write(pc.alias, cmd)
            except Exception as e:
                self.error.emit(f"field-time 명령 실패: {type(e).__name__}: {e}")

    def _acquire_once(self, idx: int, first_step: bool, extra_values=()):
        t0 = _time.perf_counter()
        self._exec_write_cmds(self._acq.start_cmds, "")
        self.progress.emit("   · 측정 완료 대기(OPC)…")
        self._exec_wait_cmds(self._acq.wait_cmds)
        self.progress.emit("   · 곡선 데이터 읽는 중…")
        arrays = self._exec_read_cmds(self._acq.read_cmds)
        for v in extra_values:   # first/second 채널 값 → 열 (길이 맞춤)
            arrays.append(np.array([float(v)]))
        arrays = self._normalize_arrays(arrays, first_step=first_step)
        self.step_elapsed.emit(idx, _time.perf_counter() - t0)
        self.step_done.emit(idx, arrays)

    def _ensure_sec(self):
        if self._sec is None:
            import threading
            from pythonization.measurement.second_channel_worker import SecondChannelWorker
            self._sec = SecondChannelWorker()
            self._sec.set_session(self._session)
            self._sec_stop = threading.Event()
            self._sec._stop_event = self._sec_stop   # stop() 공유
            # advance 내부 진행(도달 중 현재값 / 안정화 metric / 재전송)을 상태줄로 중계.
            # _sec은 이 워커와 같은 스레드에서 동기 호출되므로 직접 연결로 동작한다.
            self._sec.status.connect(lambda m: self.progress.emit(f"   · {m}"))
            self._sec.feedback_progress.connect(
                lambda mt: self.progress.emit(f"   · 안정화 중 (metric={mt:.3g})"))
        return self._sec

    def _advance(self, cmd_entry, adv, value, prev):
        """채널을 value로 이동. controlled면 advance 방식(feedback/wait/sweep)을 사용한다."""
        if cmd_entry is None:
            return
        if adv is None or cmd_entry.sweep_kind != "controlled":
            # general sweep: 값 직접 쓰기
            self._exec_write_cmds([cmd_entry], f"{value:.6g}")
            return
        sec = self._ensure_sec()
        ch = self._make_sec_channel(cmd_entry, adv)
        alias = cmd_entry.alias
        if not self._session.is_open(alias):
            self._session.open(alias)
        at = adv.advance_type
        # prev가 None(초기 시작점 이동 등)이면 현재값을 읽어 prev로 사용한다.
        # FEEDBACK은 prev=None이면 denom=|value-0|이 되어, 목표가 0일 때 '이미 도달'로
        # 오판하고 즉시 리턴(=대기 안 함)하는 버그가 있다. 현재값을 읽어 막는다.
        if prev is None and at in (A.FEEDBACK, A.SWEEP, A.THRESHOLD_TIME):
            prev = self._read_current(cmd_entry, adv)
        if at == A.SIMPLE_HOP:
            sec._do_simple_hop(alias, ch.cmd_set, value)
        elif at == A.SWEEP:
            sec._do_sweep(alias, ch, value, prev, 0.1)
        elif at == A.FEEDBACK:
            sec._do_feedback(alias, ch, value, prev)   # 워치독 포함
        elif at == A.WAIT_FOR_TIME:
            sec._do_wait_for_time(alias, ch.cmd_set, value, adv.wait_time)
        elif at == A.THRESHOLD_TIME:
            sec._do_threshold_time(alias, ch, value, prev)   # band 도달 후 시간 대기
        else:
            self._exec_write_cmds([cmd_entry], f"{value:.6g}")

    def _read_current(self, cmd_entry, adv):
        """controlled advance에서 prev가 None(초기 이동)일 때 현재값을 읽어 반환.
        읽기 명령이 없거나 실패하면 None (호출부가 폴백). FEEDBACK은 feedback_read_cmd,
        SWEEP은 paired_read_cmd를 사용한다."""
        if adv.advance_type in (A.FEEDBACK, A.THRESHOLD_TIME):
            read_cmd = (adv.feedback_read_cmd or "").strip()
        elif adv.advance_type == A.SWEEP:
            read_cmd = (adv.paired_read_cmd or "").strip()
        else:
            read_cmd = ""
        if not read_cmd:
            return None
        try:
            from pythonization.instruments.parameter import _parse_float
            alias = cmd_entry.alias
            if not self._session.is_open(alias):
                self._session.open(alias)
            raw = str(self._session.query(alias, read_cmd)).strip()
            return _parse_float(raw)
        except Exception:
            return None

    def _make_sec_channel(self, cmd_entry, adv):
        """VnaCommandEntry + VnaAdvanceConfig → InstantiatedSecondSweepChannel.
        sweep 파라미터 자리를 {v}로 남겨 SecondChannelWorker가 채우게 한다."""
        lib = self._lib_reg.get_library(cmd_entry.alias)
        template = get_template(lib, cmd_entry) or "{v}"
        d = {pp.name: ("{v}" if pp.is_user_input else pp.value)
             for pp in cmd_entry.params}
        try:
            cmd_set = template.format(**d)
        except Exception:
            cmd_set = template
        return InstantiatedSecondSweepChannel(
            alias=cmd_entry.alias, description=cmd_entry.description,
            source_type="write_cmd", advance_type=adv.advance_type, cmd_set=cmd_set,
            paired_read_cmd=adv.paired_read_cmd, sweep_rate=adv.sweep_rate,
            safety_steps=0, safety_interval_ms=0.0,
            feedback_read_cmd=adv.feedback_read_cmd,
            feedback_poll_interval=adv.feedback_poll_interval,
            feedback_tolerance_pct=adv.feedback_tolerance_pct,
            feedback_std_window=adv.feedback_std_window,
            feedback_noisefloor=adv.feedback_noisefloor,
            feedback_std_threshold=adv.feedback_std_threshold,
            wait_time=adv.wait_time)

    def _run_pre_cmds(self, phase: str):
        """pre-advance 명령 실행. phase='sweep'|'dummy' 에 따라 조절 파라미터 값 선택.

        전송 여부·내용을 progress로 남겨 'pre-advance가 안 먹는' 상황을 진단할 수 있게 한다.
        """
        specs = self._ds_plan.get("pre_specs", [])
        if not specs:
            self.progress.emit(
                f"  pre-advance({phase}): 실행할 명령 없음 "
                "(Control에서 행 체크 + 값 입력 필요)")
            return
        for spec in specs:
            if self._stop_flag:
                return
            pc = spec["cmd"]
            val = spec["sweep"] if phase == "sweep" else spec["dummy"]
            name = pc.label or pc.description
            try:
                lib = self._lib_reg.get_library(pc.alias)
                template = None
                for e in list(lib.write_cmds) + list(lib.sweep_values):
                    if e.description == pc.description:
                        template = e.cmd_set
                        break
                if template is None:
                    self.progress.emit(
                        f"  pre-advance({phase}): '{pc.description}' 템플릿을 "
                        f"'{pc.alias}'에서 못 찾음 — 건너뜀")
                    continue
                if not val:
                    self.progress.emit(
                        f"  pre-advance({phase}): '{name}' {phase} 값이 비어 건너뜀")
                    continue
                cmd = build_cmd(template, pc.params, val)
                if not self._session.is_open(pc.alias):
                    self._session.open(pc.alias)
                self._session.write(pc.alias, cmd)
                self.progress.emit(f"  pre-advance({phase}): {pc.alias} ← {cmd}")
            except Exception as e:
                self.error.emit(f"pre-advance 명령 실패: {type(e).__name__}: {e}")

    def _normalize_arrays(self, arrays: List[np.ndarray],
                          first_step: bool) -> List[np.ndarray]:
        """단일값은 기준 길이로 broadcast, array 크기 불일치는 첫 스텝에서만 감지.

        - len>1 인 array들이 서로 크기가 다르면 (첫 스텝) → ValueError (측정 중단).
        - len<=1 (단일값) → 기준 길이로 복사 확장.
        - 기준 길이는 첫 스텝에서 확정되어 이후 스텝에 재사용 (재감지 안 함).
        """
        if first_step:
            multi = [len(a) for a in arrays if len(a) > 1]
            if multi:
                ref = multi[0]
                if any(m != ref for m in multi):
                    raise ValueError(
                        f"read array 크기 불일치: {sorted(set(multi))} — "
                        "array 컬럼들의 길이가 같아야 합니다.")
            else:
                ref = max((len(a) for a in arrays), default=1) or 1
            self._ref_len = ref
        ref = self._ref_len or 1
        out: List[np.ndarray] = []
        for a in arrays:
            if len(a) == ref:
                out.append(a)
            elif len(a) <= 1:
                val = float(a[0]) if len(a) == 1 else float("nan")
                out.append(np.full(ref, val))
            else:
                # 첫 스텝 이후 길이가 기준과 다른 multi array: 단순 자르기/패딩 없이 그대로 둠
                out.append(a)
        return out

    # ------------------------------------------------------------------
    def _exec_write_cmds(self, cmds: List[VnaCommandEntry], sv_str: str):
        for entry in cmds:
            if not entry.enabled:
                continue
            # 값을 받아야 하는 명령인데 넘길 값이 없으면 **보내지 않는다**.
            # 빈 값으로 write 하면 ':CALC1:FILT:TIME:STAR ' 나
            # 'SET:…:FSET:;…:ACTN:RTOS' 처럼 인자가 빠진 명령이 나가서, 측정과 무관하게
            # 장비 상태(게이팅 시작점·자기장 목표 등)가 조용히 바뀐다.
            # Single Acquire 와 Time 모드가 sweep 명령 전체를 빈 값으로 부르는 경로다.
            if not sv_str and any(p.is_user_input for p in entry.params):
                if entry.description not in self._skipped_blank:
                    self._skipped_blank.add(entry.description)
                    self.progress.emit(
                        f"   · sweep 값이 없어 '{entry.description}' 은 보내지 않습니다")
                continue
            lib      = self._lib_reg.get_library(entry.alias)
            template = get_template(lib, entry)
            if template is None:
                raise ValueError(f"Template not found: '{entry.description}'")
            cmd = build_cmd(template, entry.params, sv_str)
            try:
                if not self._session.is_open(entry.alias):
                    self._session.open(entry.alias)
                self._session.write(entry.alias, cmd)
            except Exception as e:
                raise RuntimeError(
                    f"[{entry.alias}] '{entry.description}' 실패 — {type(e).__name__}: {e}"
                ) from e

    def _exec_wait_cmds(self, cmds: List[VnaCommandEntry]):
        """OPC 쿼리: 응답에 '1'이 포함될 때까지 polling."""
        for entry in cmds:
            if not entry.enabled:
                continue
            lib      = self._lib_reg.get_library(entry.alias)
            template = get_template(lib, entry)
            if template is None:
                raise ValueError(f"Template not found: '{entry.description}'")
            cmd = build_cmd(template, entry.params, "")
            try:
                if not self._session.is_open(entry.alias):
                    self._session.open(entry.alias)
                while not self._stop_flag:
                    response = str(self._session.query(entry.alias, cmd)).strip().lstrip('+')
                    if response == "1":
                        break
                    _time.sleep(0.1)
            except Exception as e:
                raise RuntimeError(
                    f"[{entry.alias}] '{entry.description}' 실패 — {type(e).__name__}: {e}"
                ) from e

    def _exec_read_cmds(self, cmds: List[VnaCommandEntry]) -> List[np.ndarray]:
        arrays = []
        for entry in cmds:
            if not entry.enabled:
                continue          # 비활성 명령은 측정·데이터 열에서 완전히 제외

            lib      = self._lib_reg.get_library(entry.alias)
            template = get_template(lib, entry)
            if template is None:
                raise ValueError(f"Template not found: '{entry.description}'")
            cmd = build_cmd(template, entry.params, "")
            try:
                if not self._session.is_open(entry.alias):
                    self._session.open(entry.alias)
                raw = self._session.query(entry.alias, cmd)
            except Exception as e:
                raise RuntimeError(
                    f"[{entry.alias}] '{entry.description}' 실패 — {type(e).__name__}: {e}"
                ) from e
            arr = parse_vna_array(str(raw).strip())
            if entry.read_stride > 1:
                arr = arr[::entry.read_stride]
            arrays.append(arr)
        return arrays


# ---------------------------------------------------------------------------
# Per-command row (control section panel)
# ---------------------------------------------------------------------------

class _CmdRowWidget(QWidget):
    role_toggle = Signal(object, str)   # (self, role: "start"|"stop"|"n_points")

    def __init__(self, entry: VnaCommandEntry, lib, parent=None):
        super().__init__(parent)
        self._entry        = entry
        self._lib          = lib
        self._le_user:     Optional[QLineEdit] = None
        self._cb_unit:     Optional[QComboBox] = None
        self._role_btns:   dict = {}
        self._current_role: str = entry.sweep_role
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._cb = QCheckBox()
        self._cb.setChecked(self._entry.enabled)
        self._cb.setFixedWidth(18)
        lay.addWidget(self._cb)

        lbl = QLabel(format_label(
            get_figure_axis(self._lib, self._entry), self._entry.params, ""))
        lbl.setFont(_MONO)
        lay.addWidget(lbl, stretch=1)

        if any(p.is_user_input for p in self._entry.params):
            self._le_user = QLineEdit()
            self._le_user.setFont(_MONO)
            self._le_user.setFixedWidth(80)
            up = next(p for p in self._entry.params if p.is_user_input)
            self._le_user.setPlaceholderText(f"[{up.name}]")
            # 저장된 값 복원
            if up.value:
                self._le_user.setText(up.value)
            lay.addWidget(self._le_user)

            # Unit combo (unit_type이 설정된 경우만)
            if self._entry.unit_type in _UNIT_OPTIONS:
                self._cb_unit = QComboBox()
                self._cb_unit.setFont(_MONO)
                self._cb_unit.setFixedWidth(54)
                for label, mult in _UNIT_OPTIONS[self._entry.unit_type]:
                    self._cb_unit.addItem(label, mult)
                # 저장된 단위 복원, 없으면 기본값
                restore_lbl = self._entry.unit_label or _UNIT_DEFAULT.get(self._entry.unit_type, "")
                idx = self._cb_unit.findText(restore_lbl)
                if idx >= 0:
                    self._cb_unit.setCurrentIndex(idx)
                lay.addWidget(self._cb_unit)

        # Role buttons — user_input이 있는 entry에만 표시
        if any(p.is_user_input for p in self._entry.params):
            _tooltips = {"start": "Start로 지정", "stop": "Stop으로 지정", "n_points": "N Points로 지정"}
            for role, label in [("start", "S"), ("stop", "E"), ("n_points", "N")]:
                btn = QPushButton(label)
                btn.setFixedSize(18, 18)
                btn.setToolTip(_tooltips[role])
                btn.clicked.connect(lambda _checked, r=role: self.role_toggle.emit(self, r))
                self._role_btns[role] = btn
                lay.addWidget(btn)
            self._update_role_buttons()

    # ------------------------------------------------------------------
    # Role helpers
    # ------------------------------------------------------------------

    def set_role(self, role: str):
        """외부에서 role 할당/해제 시 호출."""
        self._current_role = role
        self._update_role_buttons()

    def _update_role_buttons(self):
        for r, btn in self._role_btns.items():
            btn.setStyleSheet(
                _ROLE_BTN_STYLES[r] if r == self._current_role else _ROLE_BTN_INACTIVE
            )

    def get_command(self) -> Optional[Tuple[str, str, bool]]:
        if not self._cb.isChecked():
            return None
        template = get_template(self._lib, self._entry)
        if template is None:
            return None

        raw = self._le_user.text().strip() if self._le_user else ""
        if self._cb_unit is not None and raw:
            try:
                multiplier = self._cb_unit.currentData()
                user_val = f"{float(raw) * multiplier:.10g}"
            except ValueError:
                user_val = raw
        else:
            user_val = raw

        try:
            cmd = build_cmd(template, self._entry.params, user_val)
        except ValueError:
            return None
        return (self._entry.alias, cmd, self._entry.cmd_type == "query")

    def get_entry_state(self) -> "VnaCommandEntry":
        """현재 UI 상태(체크박스, 입력값, 단위)를 entry에 반영하여 반환."""
        raw = self._le_user.text().strip() if self._le_user else ""
        unit_label = self._cb_unit.currentText() if self._cb_unit else ""
        new_params = [
            p.model_copy(update={"value": raw}) if p.is_user_input else p
            for p in self._entry.params
        ]
        return self._entry.model_copy(update={
            "enabled":    self._cb.isChecked(),
            "params":     new_params,
            "unit_label": unit_label,
            "sweep_role": self._current_role,
        })


# ---------------------------------------------------------------------------
# Section widget (control panel) — supports bind_user_inputs
# ---------------------------------------------------------------------------

class _SectionWidget(QGroupBox):
    execute_requested     = Signal(list)
    role_toggle_requested = Signal(object, str)   # (row, role) — _CmdRowWidget에서 pass-through

    def __init__(self, sec_cfg: VnaSectionConfig,
                 lib_reg: "VisaLibraryRegistry", parent=None):
        super().__init__(sec_cfg.name, parent)
        self._sec_cfg = sec_cfg
        self._lib_reg = lib_reg
        self._cmd_rows: List[_CmdRowWidget] = []
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(2)
        lay.setContentsMargins(6, 10, 6, 6)

        self._rows_lay = QVBoxLayout()
        self._rows_lay.setSpacing(2)
        lay.addLayout(self._rows_lay)

        # Build rows and apply bind_id grouping
        # bind_id > 0: first occurrence per group is leader (active),
        #              subsequent occurrences are followers (read-only, synced)
        leaders: dict = {}   # bind_id → _CmdRowWidget (leader)

        for entry in self._sec_cfg.commands:
            try:
                lib = self._lib_reg.get_library(entry.alias)
            except Exception:
                continue
            row = _CmdRowWidget(entry, lib, self)
            row.role_toggle.connect(self.role_toggle_requested)
            self._cmd_rows.append(row)
            self._rows_lay.addWidget(row)

            bid = entry.bind_id
            if bid > 0 and row._le_user is not None:
                if bid not in leaders:
                    leaders[bid] = row
                else:
                    # Follower — read-only input, synced from leader
                    row._le_user.setReadOnly(True)
                    row._le_user.setStyleSheet(
                        "color: #888; background: #1a1a1a;")
                    leader = leaders[bid]

                    def _make_sync(follower):
                        def _sync(text):
                            follower._le_user.setText(text)
                        return _sync
                    leader._le_user.textChanged.connect(_make_sync(row))

                    # Unit combo도 동기화 (둘 다 unit이 있는 경우)
                    if leader._cb_unit is not None and row._cb_unit is not None:
                        row._cb_unit.setEnabled(False)

                        def _make_unit_sync(follower_cb):
                            def _sync_unit(idx):
                                follower_cb.setCurrentIndex(idx)
                            return _sync_unit
                        leader._cb_unit.currentIndexChanged.connect(
                            _make_unit_sync(row._cb_unit))

        btn_bar = QHBoxLayout()
        btn_bar.addStretch()
        btn_exec = QPushButton("▶ Execute")
        btn_exec.setFixedHeight(24)
        btn_exec.setStyleSheet(
            "QPushButton{background:#1f4e8c;color:white;"
            "border-radius:3px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        btn_exec.clicked.connect(self._on_execute)
        btn_bar.addWidget(btn_exec)
        lay.addLayout(btn_bar)

    def _on_execute(self):
        cmds = [row.get_command() for row in self._cmd_rows]
        self.execute_requested.emit([c for c in cmds if c])

    def get_updated_config(self) -> VnaSectionConfig:
        """현재 UI 상태를 반영한 VnaSectionConfig 반환 (저장용)."""
        updated_cmds = [row.get_entry_state() for row in self._cmd_rows]
        return self._sec_cfg.model_copy(update={"commands": updated_cmds})



# ---------------------------------------------------------------------------
# Plot panel  (공용 위젯 — pythonization/ui/widgets/plot_panel.py)
# ---------------------------------------------------------------------------

def _plot_state(cfg: VnaPlotCurveConfig) -> PlotPanelState:
    """저장된 곡선 설정 → 패널 상태. y_sources 가 비면 구 버전 단일 y_source 로 폴백."""
    y_srcs = list(cfg.y_sources) if cfg.y_sources else (
        [cfg.y_source] if cfg.y_source else [])
    return PlotPanelState(x_source=cfg.x_source, y_sources=y_srcs)


def _plot_config(panel: PlotPanel) -> VnaPlotCurveConfig:
    """패널 상태 → 저장용 곡선 설정. y_source 는 구 버전 호환으로 계속 채운다."""
    state = panel.to_state()
    return VnaPlotCurveConfig(
        x_source=state.x_source,
        y_source=state.y_sources[0] if state.y_sources else "",
        y_sources=state.y_sources,
    )

# ---------------------------------------------------------------------------
# VNA Window
# ---------------------------------------------------------------------------

class VnaWindow(QDialog):

    def __init__(self, session: "InstrumentSession",
                 lib_reg: "VisaLibraryRegistry",
                 param_manager_reg: Optional["ProfileRegistry"] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("VNA Control")
        self.resize(1500, 780)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self._session            = session
        self._lib_reg            = lib_reg
        self._param_manager_reg  = param_manager_reg

        self._cfg      = load_vna_config(self._config_path())

        self._sec_worker: Optional[_VnaWorker]     = None
        self._acq_worker: Optional[_AcquireWorker] = None
        self._acq_thread: Optional[QThread]        = None
        self._section_widgets: List[_SectionWidget] = []
        self._current_sweep_values = None
        self._ds_step_labels = None
        self._ds_field_time = False    # Double Sweep with Time 진행 중 여부 (파일명용)
        self._extra_cols: list = []   # sweep acquire: write한 값 열 (label, unit)
        self._step_count: int = 0
        self._sweep_folder: Optional[Path] = None
        self._sweep_figure_axis: str = ""
        self._config_win = None
        self._resume_state: Optional[VnaResumeState] = None   # double sweep 재개 상태
        self._bg_workers: list = []   # 백그라운드 _VnaWorker(stop-cmd 등) 추적 (종료 시 wait)
        self._second_table = SecondChannelModel()   # Feature 1: second 값 테이블 (GUI 소유)
        self._second_table_win = None
        # Section role management  (start / stop / n_points)
        self._role_rows: dict = {"start": None, "stop": None, "n_points": None}
        self._linspace_data: Optional[Tuple[str, str, np.ndarray]] = None
        # 알람 (VNA sweep 전용 — 텔레그램)
        self._alarm_manager = AlarmManager()
        self._time_mode_running: bool = False
        # Sweep 콤보에 '⏱ Time' 항목을 두고, 저장된 time_mode면 최초 1회 그 항목을 선택
        self._pending_time_select: bool = bool(self._cfg.acquire.time_mode)

        self._build_ui()
        self._rebuild_sections()
        self._restore_plot_config()
        self._apply_ds_control()

    # ------------------------------------------------------------------
    # Profile-based config path
    # ------------------------------------------------------------------

    def _config_path(self) -> Path:
        if self._param_manager_reg is not None:
            name = self._param_manager_reg.active_name
            vna_dir = SETTINGS_DIR / "profiles" / "vna"
            vna_dir.mkdir(parents=True, exist_ok=True)
            return vna_dir / f"{name}.yaml"
        return _DEFAULT_CONFIG_PATH

    def on_profile_changed(self):
        """main_window에서 프로파일 변경 시 호출 — 새 프로파일 로드.
        저장은 caller(_save_current_to_active_profile)에서 이미 처리함."""
        self._cfg = load_vna_config(self._config_path())
        self._le_filename.setText(self._cfg.acquire.filename)
        self._le_sw_start.setText(str(self._cfg.acquire.sweep_start))
        self._le_sw_stop.setText(str(self._cfg.acquire.sweep_stop))
        self._le_sw_n.setText(str(self._cfg.acquire.sweep_n))
        self._le_main.setText(self._cfg.main_folder)
        self._le_sub.setText(self._cfg.sub_folder)
        self._cb_save.setChecked(self._cfg.save_enabled)
        self._rebuild_sections()
        self._populate_sweep_cmds()
        self._refresh_plot_sources()
        self._restore_plot_config()
        self._apply_ds_control()
        # 그래프/데이터 초기화 (프로파일 전환 시)
        self._step_count = 0
        self._panel_L.clear_data()
        self._panel_R.clear_data()
        self._resume_state = None
        self._update_resume_button()   # 프로파일별 resume 상태 반영

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        root.addWidget(self._build_top_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_acquire_column())    # 좌: Sweep/Acquire 설정
        splitter.addWidget(self._build_sections_column())   # 중: 섹션 Execute 패널
        splitter.addWidget(self._build_right_panel())       # 우: 플롯
        splitter.setSizes([360, 300, 1000])
        root.addWidget(splitter)

    def _build_top_bar(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setMaximumHeight(34)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(6)

        self._btn_cfg = QPushButton("⚙ Config")
        self._btn_cfg.setFixedHeight(22)
        self._btn_cfg.clicked.connect(self._open_config)
        lay.addWidget(self._btn_cfg)

        self._btn_alarm = QPushButton("⚙ Alarm")
        self._btn_alarm.setFixedHeight(22)
        self._btn_alarm.setToolTip("VNA sweep 알람 설정 (텔레그램) — 측정 오류·완료 시 알림")
        self._btn_alarm.clicked.connect(self._open_alarm_config)
        lay.addWidget(self._btn_alarm)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #30363d;")
        lay.addWidget(sep)

        self._cb_save = QCheckBox("Save")
        self._cb_save.setChecked(self._cfg.save_enabled)
        lay.addWidget(self._cb_save)

        lay.addWidget(QLabel("Main:"))
        self._le_main = QLineEdit(self._cfg.main_folder)
        self._le_main.setFont(_MONO)
        self._le_main.setFixedHeight(22)
        self._le_main.setMinimumWidth(180)
        lay.addWidget(self._le_main, stretch=1)

        btn_browse = QPushButton("…")
        btn_browse.setFixedSize(24, 22)
        btn_browse.setToolTip("Browse main folder")
        btn_browse.clicked.connect(self._on_browse)
        lay.addWidget(btn_browse)

        lay.addWidget(QLabel("Sub:"))
        self._le_sub = QLineEdit(self._cfg.sub_folder)
        self._le_sub.setFont(_MONO)
        self._le_sub.setFixedHeight(22)
        self._le_sub.setFixedWidth(110)
        lay.addWidget(self._le_sub)

        btn_open = QPushButton("📂")
        btn_open.setFixedSize(24, 22)
        btn_open.setToolTip("Open save folder")
        btn_open.clicked.connect(self._on_open_folder)
        lay.addWidget(btn_open)

        lay.addWidget(make_help_button(self._control_help_html(), "VNA Control 도움말"))
        return frame

    @staticmethod
    def _control_help_html() -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>VNA Control — 한 번에 여러 점(배열) 측정</b><hr>"
            "보통 측정은 한 점씩 읽지만, VNA처럼 <b>한 번에 곡선(여러 점)을 통째로</b> 받아오는 "
            "장비를 다루는 창입니다. 받은 곡선을 그래프로 보고 파일로 저장합니다.<hr>"

            "<b>■ 측정 한 번(acquire)의 진행</b><br>"
            "&nbsp;① 필요한 설정값을 장비에 보냄 → ② 측정 시작 명령 → "
            "③ <b>측정이 끝날 때까지 기다림</b>(OPC) → ④ 곡선(S11·S21 등)을 읽어 옴.<br>"
            "이 순서와 명령들은 <b>⚙ Config</b>에서 미리 정합니다.<hr>"

            "<b>■ Single / Sweep / Time 모드</b><br>"
            "&nbsp;• <b>Single Acquire</b>: 위 한 번만 실행.<br>"
            "&nbsp;• <b>Sweep Acquire</b>: <b>Sweep:</b> 칸에서 고른 값을 Start→Stop으로 N번 "
            "바꿔가며 매번 한 번씩 측정 (예: 자기장을 바꿔가며 곡선 여러 장).<br>"
            "&nbsp;• <b>Sweep: 칸에서 ⏱ Time 선택</b> 시: 값을 바꾸지 않고 <b>일정 시간 간격</b>마다 "
            "한 번씩 정해진 횟수(Count)만큼 측정 (버튼이 ‘▶ Time Sweep’으로 바뀜).<br>"
            "&nbsp;&nbsp;– 측정이 간격보다 빨리 끝나면 남는 시간을 <b>Idle</b>(초록)로,<br>"
            "&nbsp;&nbsp;– 더 오래 걸리면 부족분을 <b>Overrun</b>(빨강, 음수)으로 표시합니다.<hr>"

            "<b>■ 단일값 자동 맞춤</b><br>"
            "곡선(예: 201점)과 함께 자기장·온도 같은 <b>한 개짜리 값</b>을 같이 읽으면, "
            "그 값을 곡선 길이에 맞춰 자동으로 채워 길이를 맞춥니다.<br>"
            "단, 곡선이 여러 개인데 <b>서로 길이가 다르면</b> 첫 측정에서 오류로 알리고 멈춥니다.<hr>"

            "<b>■ 상단 버튼/입력</b><br>"
            "&nbsp;• <b>⚙ Config</b>: 측정 순서·명령·그래프 설정 (자세한 도움말은 그 창에 있음).<br>"
            "&nbsp;• <b>⚙ Alarm</b>: 측정 오류·완료 시 텔레그램 알림.<br>"
            "&nbsp;• <b>Save</b> / <b>Main·Sub</b>: 저장 켜기와 저장 폴더.<hr>"

            "<b>■ 그래프(우측)</b><br>"
            "왼쪽·오른쪽 두 개의 그래프에 각각 가로축·세로축을 골라 곡선을 그립니다. "
            "한 그래프에 여러 곡선을 겹쳐 볼 수 있습니다."
            "</body></html>"
        )

    @staticmethod
    def _double_sweep_help_html() -> str:
        return (
            "<html><body style='white-space:normal;'>"
            "<b>◆ Double Sweep — 두 축을 겹쳐 쓸기</b><hr>"
            "Sweep Acquire를 <b>First 축</b>으로 쓸면서, 그 바깥에 <b>Second 축</b>을 한 겹 더 "
            "두를 수 있습니다. Second 한 점마다 First 전체 sweep이 1회씩 돕니다.<br>"
            "(예: Second=온도 5점, First=자기장 → 각 온도에서 자기장 sweep을 측정)<hr>"

            "<b>■ Second sweep channel 사용</b><br>"
            "바깥 루프 축. 체크하면 ch(명령)·Start·Stop·N을 정합니다. "
            "끄면 First 축만 한 번 쓸고 끝납니다.<hr>"

            "<b>■ Power Sweep 모드</b><br>"
            "안쪽(First) 축을 <b>VNA power</b>로 대체합니다. Second(자기장) 한 점에 "
            "<b>자기장을 고정</b>한 채 power를 Start→Stop으로 <b>N점</b>(처음·끝 포함) 바꾸며 "
            "매 점에서 acquire하고, 끝나면 다음 자기장 점으로 넘어가 power를 처음부터 다시 훑습니다.<br>"
            "&nbsp;– 자기장 한 점의 순서: <b>①속도 → ②목표 → ③ramp 시작 → "
            "④도달(tolerance)·안정화(std) → ⑤고정 대기</b>. ④의 판정 기준은 Second 채널의 "
            "Controlled advance 설정(Read Cmd·Tolerance·Std Window·Std Threshold)을 "
            "그대로 씁니다 — 일반 double sweep 의 feedback 과 같은 2단계입니다.<br>"
            "&nbsp;– 켜면 <b>First sweep ch 콤보와 그 Start/Stop/N은 쓰이지 않습니다</b> "
            "(power ch·Start·Stop·N을 대신 씁니다).<br>"
            "&nbsp;– <b>Double Sweep with Time과 함께 켤 수 없습니다</b> (둘 다 First 축을 대체).<br>"
            "&nbsp;– 저장: 자기장 값이 <b>하위폴더</b>, power 값이 <b>파일명</b>이 됩니다 "
            "(<code>&lt;field&gt;_300/&lt;power&gt;_-20.dat</code> 형식).<hr>"

            "<b>■ First 방향</b><br>"
            "&nbsp;• <b>단방향</b>: 매 sweep을 항상 <b>시작점→끝점</b>으로. 다음 Second 스텝 전에 "
            "<b>dummy로 시작점에 복귀</b>(측정 안 함)합니다. 자기장처럼 방향에 민감한 축에 적합.<br>"
            "&nbsp;• <b>다중방향</b>: Second 스텝마다 <b>방향을 교대</b>(↑↓↑↓)해 복귀 시간을 아낍니다.<hr>"

            "<b>■ Advance 전 명령 값 (pre-advance)</b><br>"
            "First가 <b>Controlled Sweep</b>(⚙ Config에서 지정)이고 <b>단방향</b>일 때 나타납니다. "
            "advance 직전에 보낼 값(예: 자기장 ramp 속도 RFST)을 <b>sweep 단계</b>와 "
            "<b>dummy 복귀 단계</b>에 각각 다르게 줄 수 있습니다.<br>"
            "&nbsp;– 체크 + 값 입력해야 적용됩니다. 매 sweep 시작 전: "
            "<b>dummy 속도→시작점 복귀→sweep 속도→측정</b> 순으로 진행됩니다.<hr>"

            "<b>■ Double Sweep with Time</b><br>"
            "First(자기장)를 N단계가 아니라 <b>시간 기반</b>으로 측정합니다. 목표로 ramp를 시작한 뒤 "
            "<b>acquire 간격</b>마다 측정하고, 상태 읽기 cmd 응답에 <b>HOLD 토큰</b>이 보이면 완료로 "
            "판단합니다. 단방향이면 매 sweep 전 시작점으로 controlled 복귀합니다.<hr>"

            "<b>■ ⏎ 측정 완료 후 복귀</b><br>"
            "체크한 축만 <b>정상 완료 후</b> 지정한 값으로 되돌립니다. "
            "<b>First 를 먼저</b> 내리고 그다음 Second 를 옮깁니다 — 시료에 신호를 걸어 둔 채 "
            "마그넷을 움직이지 않기 위해서입니다. Second 는 그 채널의 advance 방식을 그대로 쓰므로 "
            "feedback 이면 실제 도달까지 기다립니다.<br>"
            "&nbsp;– <b>체크하지 않으면 마지막으로 쓴 값에 그대로 멈춥니다</b> "
            "(First=Stop 값, Second=마지막 array 값. 다중방향이면 First 는 Start 일 수도 있습니다).<br>"
            "&nbsp;– <b>Stop·오류로 끊긴 경우에는 적용되지 않습니다</b> — 그때는 아래 "
            "'Stop 시 실행 명령'이 대신 나갑니다.<br>"
            "&nbsp;– 복귀에만 실패하면 측정 데이터는 그대로 두고 경고만 띄웁니다 "
            "(측정이 '중단됨'으로 바뀌지 않습니다).<hr>"

            "<b>■ ⏹ Stop 시 실행 명령</b><br>"
            "측정 중 <b>Stop</b>(또는 오류 중단) 시 자동 전송할 명령. "
            "예: 자기장 ramp를 멈추는 <b>HOLD</b>. 여러 개 등록 시 위→아래 순으로 보냅니다.<hr>"

            "<b>■ 진행 표시</b><br>"
            "현재 어느 단계(Second 이동 / 시작점 복귀 / pre-advance / First 이동 / acquire)인지 "
            "창 <b>최하단 상태줄</b>에 실시간으로 표시됩니다."
            "</body></html>"
        )

    # ---- Left panel ---------------------------------------------------

    def _build_acquire_column(self) -> QWidget:
        """좌측 독립 컬럼: Sweep/Acquire 설정 (+ 상태줄). 길어질 수 있어 세로 스크롤."""
        outer = QWidget()
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._build_acquire_group())
        lay.addWidget(scroll, stretch=1)

        self._lbl_status = QLabel("")
        self._lbl_status.setFont(QFont("Consolas", 8))
        self._lbl_status.setStyleSheet("color: #888;")
        self._lbl_status.setWordWrap(True)
        lay.addWidget(self._lbl_status)
        return outer

    def _build_sections_column(self) -> QWidget:
        """중간 컬럼: 섹션 Execute 패널들 (스크롤)."""
        outer = QWidget()
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._sections_content = QWidget()
        self._sections_lay = QVBoxLayout(self._sections_content)
        self._sections_lay.setSpacing(6)
        self._sections_lay.setContentsMargins(4, 4, 4, 4)
        self._sections_lay.addStretch()
        scroll.setWidget(self._sections_content)
        lay.addWidget(scroll, stretch=1)
        return outer

    def _build_acquire_group(self) -> QGroupBox:
        """측정 실행 패널 — 파일명, 단발 acquire, sweep 설정, double sweep, 실행 버튼."""
        group = QGroupBox("Acquire")
        layout = QVBoxLayout(group)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 10, 8, 8)

        layout.addLayout(self._build_filename_row())
        layout.addWidget(self._build_single_acquire_button())
        layout.addWidget(self._hline())
        layout.addLayout(self._build_sweep_cmd_row())
        layout.addWidget(self._build_sweep_param_row())
        layout.addWidget(self._build_time_mode_row())

        # Idle / Overrun 표시 (시간 모드 전용)
        self._lbl_idle = QLabel("Idle: —")
        self._lbl_idle.setFont(_MONO)
        self._lbl_idle.setStyleSheet("color: #555;")
        layout.addWidget(self._lbl_idle)

        self._ds_section = self._build_double_sweep_section()
        layout.addWidget(self._ds_section)
        layout.addLayout(self._build_sweep_action_row())
        layout.addWidget(self._build_resume_button())

        # 파라미터 행 / 시간 행 중 무엇을 보일지는 콤보 선택에 달렸다
        self._populate_sweep_cmds()
        self._update_resume_button()
        return group

    @staticmethod
    def _hline() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #30363d;")
        return line

    def _mono_edit(self, text: str = "", width: int = 0) -> QLineEdit:
        edit = QLineEdit(text)
        edit.setFont(_MONO)
        if width:
            edit.setFixedWidth(width)
        return edit

    def _build_filename_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Filename:"))
        self._le_filename = self._mono_edit(self._cfg.acquire.filename)
        self._le_filename.setPlaceholderText("vna_data")
        row.addWidget(self._le_filename, stretch=1)
        return row

    def _build_single_acquire_button(self) -> QPushButton:
        self._btn_single = QPushButton("▶ Single Acquire")
        self._btn_single.setFixedHeight(26)
        self._btn_single.setStyleSheet(
            "QPushButton{background:#2d5a1b;color:white;"
            "border-radius:3px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        self._btn_single.clicked.connect(self._on_single_acquire)
        return self._btn_single

    def _build_sweep_cmd_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("First sweep ch:"))
        self._combo_sweep_cmd = QComboBox()
        self._combo_sweep_cmd.setFont(_MONO)
        self._combo_sweep_cmd.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._combo_sweep_cmd.currentIndexChanged.connect(self._on_sweep_cmd_changed)
        row.addWidget(self._combo_sweep_cmd, stretch=1)
        return row

    def _build_sweep_param_row(self) -> QWidget:
        """Start / Stop / N. 단위 콤보는 선택한 명령에 단위가 있을 때만 나타난다.

        '⏱ Time' 을 고르면 이 행 대신 _time_row_widget 이 보인다.
        """
        row = QHBoxLayout()
        row.setSpacing(4)

        self._le_sw_start = self._mono_edit(str(self._cfg.acquire.sweep_start), 60)
        self._cb_sw_start_unit = QComboBox()
        self._cb_sw_start_unit.setFont(_MONO)
        self._cb_sw_start_unit.setFixedWidth(52)
        self._cb_sw_start_unit.setVisible(False)
        row.addWidget(QLabel("Start:"))
        row.addWidget(self._le_sw_start)
        row.addWidget(self._cb_sw_start_unit)

        self._le_sw_stop = self._mono_edit(str(self._cfg.acquire.sweep_stop), 60)
        self._cb_sw_stop_unit = QComboBox()
        self._cb_sw_stop_unit.setFont(_MONO)
        self._cb_sw_stop_unit.setFixedWidth(52)
        self._cb_sw_stop_unit.setVisible(False)
        row.addWidget(QLabel("Stop:"))
        row.addWidget(self._le_sw_stop)
        row.addWidget(self._cb_sw_stop_unit)

        self._le_sw_n = self._mono_edit(str(self._cfg.acquire.sweep_n), 44)
        row.addWidget(QLabel("N:"))
        row.addWidget(self._le_sw_n)
        row.addStretch()

        self._sweep_param_widget = QWidget()
        self._sweep_param_widget.setLayout(row)
        return self._sweep_param_widget

    def _build_time_mode_row(self) -> QWidget:
        """시간 기반 sweep — Interval(초)마다 acquire 를 Count 번 반복."""
        row = QHBoxLayout()
        row.setSpacing(4)

        self._le_time_interval = self._mono_edit(
            str(self._cfg.acquire.time_interval), 56)
        row.addWidget(QLabel("Interval:"))
        row.addWidget(self._le_time_interval)
        row.addWidget(QLabel("s"))
        row.addSpacing(8)

        self._le_time_count = self._mono_edit(str(self._cfg.acquire.time_count), 44)
        row.addWidget(QLabel("Count:"))
        row.addWidget(self._le_time_count)
        row.addStretch()

        self._time_row_widget = QWidget()
        self._time_row_widget.setLayout(row)
        return self._time_row_widget

    def _build_sweep_action_row(self) -> QHBoxLayout:
        self._btn_sweep = QPushButton("▶ Sweep Acquire")
        self._btn_sweep.setFixedHeight(26)
        self._btn_sweep.setStyleSheet(
            "QPushButton{background:#4b2d5a;color:white;"
            "border-radius:3px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        self._btn_sweep.clicked.connect(self._on_sweep_acquire)

        self._btn_stop_acq = QPushButton("■ Stop")
        self._btn_stop_acq.setFixedHeight(26)
        self._btn_stop_acq.setMinimumWidth(90)
        self._btn_stop_acq.setEnabled(False)
        self._btn_stop_acq.clicked.connect(self._on_stop_acquire)

        row = QHBoxLayout()
        row.addWidget(self._btn_sweep)
        row.addWidget(self._btn_stop_acq, stretch=1)
        row.addStretch(1)
        return row

    def _build_resume_button(self) -> QPushButton:
        """중단된 double sweep 을 마지막 second 값부터 재개."""
        self._btn_resume = QPushButton("▶ Resume (중단된 측정 재개)")
        self._btn_resume.setFixedHeight(24)
        self._btn_resume.setStyleSheet(
            "QPushButton{background:#2d4b2d;color:#7ee787;"
            "border-radius:3px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        self._btn_resume.setEnabled(False)
        self._btn_resume.clicked.connect(self._on_resume_clicked)
        return self._btn_resume

    # ---- Double Sweep section ----------------------------------------

    def _build_double_sweep_section(self) -> QWidget:
        """2차 축(bias/온도) 루프 설정. 위에서 아래로 진행 순서대로 배치한다."""
        section = QFrame()
        section.setObjectName("dsSec")
        section.setStyleSheet(
            "#dsSec{border:1px solid #30363d;border-radius:4px;background:#0f1117;}")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(5)

        layout.addLayout(self._build_ds_title_row())

        # Second sweep channel — bias loop 을 먼저 정한다
        self._cb_second_enable = QCheckBox("Second sweep channel 사용")
        self._cb_second_enable.toggled.connect(self._on_second_enable_toggled)
        layout.addWidget(self._cb_second_enable)
        layout.addWidget(self._build_second_channel_widget())

        # Power Sweep — First 축을 power 로 대체 (Second=자기장 × power)
        self._cb_power_mode = QCheckBox("Power Sweep 모드 (First 축을 power 로 대체)")
        self._cb_power_mode.setToolTip(
            "켜면 Second(자기장) 값 하나마다 power 를 Start→Stop 으로 N 점 바꾸며\n"
            "매 점에서 acquire 합니다. First sweep ch 콤보와 그 Start/Stop/N 은\n"
            "이 모드에서 쓰이지 않습니다.")
        self._cb_power_mode.toggled.connect(self._on_power_mode_toggled)
        layout.addWidget(self._cb_power_mode)
        layout.addWidget(self._build_power_sweep_widget())

        layout.addLayout(self._build_direction_row())
        layout.addWidget(self._build_pre_advance_widget())

        self._cb_field_time = QCheckBox("Double Sweep with Time (First=자기장 시간측정)")
        self._cb_field_time.setToolTip(
            "First 채널을 N단계가 아니라 '시간 기반'으로 측정합니다.\n"
            "목표로 ramp 시작 → acquire 간격마다 측정 → 상태가 HOLD면 완료.\n"
            "단방향이면 완료 후 controlled로 시작점 복귀·안정화.")
        self._cb_field_time.toggled.connect(self._on_field_time_toggled)
        layout.addWidget(self._cb_field_time)
        layout.addWidget(self._build_field_time_widget())

        layout.addWidget(self._build_finish_return_widget())
        self._build_stop_cmd_section(layout)
        layout.addLayout(self._build_time_estimate_row())

        # first channel N 이 바뀌어도 예상 시간을 다시 계산한다
        self._le_sw_n.textChanged.connect(self._update_time_estimate)
        return section

    def _build_ds_title_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        title = QLabel("◆ Double Sweep")
        title.setStyleSheet("color:#c586c0; font-weight:bold;")
        row.addWidget(title)
        row.addStretch()
        row.addWidget(make_help_button(self._double_sweep_help_html(),
                                       "Double Sweep 도움말"))
        return row

    def _build_second_channel_widget(self) -> QWidget:
        """second 채널 선택 + 값 범위 + 테이블 편집. 체크박스로 접힌다."""
        self._second_widget = QWidget()
        layout = QVBoxLayout(self._second_widget)
        layout.setContentsMargins(14, 0, 0, 0)
        layout.setSpacing(4)

        self._combo_second_cmd = QComboBox()
        self._combo_second_cmd.setFont(_MONO)
        # Power 모드의 '속도 명령' 출처가 이 선택에 딸려 있다 (그 채널의 pre_cmds)
        self._combo_second_cmd.currentIndexChanged.connect(
            lambda _i: self._pw_update_rate_source_hint())
        cmd_row = QHBoxLayout()
        cmd_row.setSpacing(6)
        cmd_row.addWidget(QLabel("ch:"))
        cmd_row.addWidget(self._combo_second_cmd, 1)
        layout.addLayout(cmd_row)

        self._le_2_start = self._mono_edit("0", 54)
        self._le_2_stop = self._mono_edit("1", 54)
        self._le_2_n = self._mono_edit("10", 40)
        range_row = QHBoxLayout()
        range_row.setSpacing(4)
        range_row.addWidget(QLabel("Start:"))
        range_row.addWidget(self._le_2_start)
        range_row.addWidget(QLabel("Stop:"))
        range_row.addWidget(self._le_2_stop)
        range_row.addWidget(QLabel("N:"))
        range_row.addWidget(self._le_2_n)
        range_row.addStretch()
        for field in (self._le_2_start, self._le_2_stop, self._le_2_n):
            field.textChanged.connect(self._update_time_estimate)
        layout.addLayout(range_row)

        self._btn_second_table = QPushButton("Second 값 테이블…")
        self._btn_second_table.setToolTip(
            "Second 채널 값 배열을 표로 편집하는 창을 엽니다.\n"
            "측정 중에도 아직 측정 안 한(대기) 행은 값 수정·추가·삭제할 수 있습니다.")
        self._btn_second_table.clicked.connect(self._open_second_table)
        table_row = QHBoxLayout()
        table_row.setSpacing(6)
        table_row.addWidget(self._btn_second_table)
        table_row.addStretch()
        layout.addLayout(table_row)

        self._cb_second_keep_table = QCheckBox(
            "테이블 초기화 안 함 (커스텀 테이블 그대로 측정)")
        self._cb_second_keep_table.setToolTip(
            "체크 시 시작할 때 Start/Stop/N으로 테이블을 새로 만들지 않고,\n"
            "테이블 창에서 직접 넣은 값 목록 그대로 측정합니다.")
        layout.addWidget(self._cb_second_keep_table)

        self._second_widget.setVisible(False)
        return self._second_widget

    def _build_power_sweep_widget(self) -> QWidget:
        """Power Sweep 모드 설정 — power 명령 + 처음/끝/점 개수. 체크박스로 접힌다."""
        self._power_widget = QWidget()
        layout = QVBoxLayout(self._power_widget)
        layout.setContentsMargins(14, 0, 0, 0)
        layout.setSpacing(4)

        self._combo_power_cmd = QComboBox()
        self._combo_power_cmd.setFont(_MONO)
        self._combo_power_cmd.setToolTip(
            "power 를 설정하는 sweep 명령 (Config 의 Sweep 명령 목록에서 고릅니다).")
        cmd_row = QHBoxLayout()
        cmd_row.setSpacing(6)
        cmd_row.addWidget(QLabel("power ch:"))
        cmd_row.addWidget(self._combo_power_cmd, 1)
        layout.addLayout(cmd_row)

        self._le_pw_start = self._mono_edit("-20", 54)
        self._le_pw_stop = self._mono_edit("0", 54)
        self._le_pw_n = self._mono_edit("11", 40)
        self._le_pw_n.setToolTip("처음과 끝을 포함한 point 개수 (2 이면 처음·끝 두 점).")
        range_row = QHBoxLayout()
        range_row.setSpacing(4)
        range_row.addWidget(QLabel("Start:"))
        range_row.addWidget(self._le_pw_start)
        range_row.addWidget(QLabel("Stop:"))
        range_row.addWidget(self._le_pw_stop)
        range_row.addWidget(QLabel("N:"))
        range_row.addWidget(self._le_pw_n)
        range_row.addStretch()
        for field in (self._le_pw_start, self._le_pw_stop, self._le_pw_n):
            field.textChanged.connect(self._update_time_estimate)
        layout.addLayout(range_row)

        hint = QLabel("→ Second(자기장) 한 점마다 power 를 Start→Stop 으로 N 점 훑습니다.")
        hint.setStyleSheet("color:#888; font-size:10px;")
        layout.addWidget(hint)

        # 'power ch' 콤보에는 등록된 sweep 명령이 **전부** 나온다 — 주파수나 게이팅
        # 명령을 잘못 골라도 측정은 그대로 돌고 데이터도 저장된다. 실제로 나가는
        # VISA 문자열을 보여 줘서 그걸 눈으로 잡게 한다.
        self._lbl_pw_cmd_preview = QLabel("")
        self._lbl_pw_cmd_preview.setFont(QFont("Consolas", 8))
        self._lbl_pw_cmd_preview.setWordWrap(True)
        self._lbl_pw_cmd_preview.setStyleSheet("color:#888;")
        layout.addWidget(self._lbl_pw_cmd_preview)
        self._combo_power_cmd.currentIndexChanged.connect(
            lambda _i: self._pw_update_cmd_preview())
        self._le_pw_start.textChanged.connect(self._pw_update_cmd_preview)

        layout.addWidget(self._hline())
        layout.addLayout(self._build_power_timing_row())
        layout.addWidget(self._hline())
        self._build_power_field_section(layout)

        self._power_widget.setVisible(False)
        return self._power_widget

    def _build_power_timing_row(self) -> QHBoxLayout:
        """power 한 점의 타이밍 — 이동 후 측정까지 / 측정 후 다음 점까지."""
        self._le_pw_pre = self._mono_edit("5", 46)
        self._le_pw_pre.setToolTip(
            "power 를 옮긴 뒤 실제 측정을 시작하기까지 기다리는 시간.\n"
            "값이 안정될 시간을 줍니다.")
        self._le_pw_post = self._mono_edit("5", 46)
        self._le_pw_post.setToolTip(
            "측정이 끝난 뒤 다음 power 점으로 넘어가기까지 기다리는 시간.")

        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(QLabel("이동 후 측정까지:"))
        row.addWidget(self._le_pw_pre)
        row.addWidget(QLabel("s"))
        row.addSpacing(8)
        row.addWidget(QLabel("측정 후 다음 점까지:"))
        row.addWidget(self._le_pw_post)
        row.addWidget(QLabel("s"))
        row.addStretch()
        for field in (self._le_pw_pre, self._le_pw_post):
            field.textChanged.connect(self._update_time_estimate)
        return row

    def _build_power_field_section(self, layout: QVBoxLayout) -> None:
        """자기장(Second) 이동 — 변화 속도·ramp 시작·도달 후 대기."""
        title = QLabel("자기장(Second) 이동")
        title.setStyleSheet("color:#79c0ff; font-size:10px; font-weight:bold;")
        layout.addWidget(title)

        self._le_pw_rate = self._mono_edit("0.3", 54)
        self._le_pw_rate.setToolTip(
            "다음 자기장 점으로 넘어갈 때의 변화 속도.\n"
            "아래 '속도 명령'의 [parameter] 자리에 이 값이 채워져 전송됩니다.\n"
            "Mercury iPS 는 보통 최대 0.3 T/min 입니다.")
        self._lbl_pw_rate_warn = QLabel("")
        self._lbl_pw_rate_warn.setStyleSheet("color:#d7ba7d; font-size:10px;")
        rate_row = QHBoxLayout()
        rate_row.setSpacing(4)
        rate_row.addWidget(QLabel("변화 속도:"))
        rate_row.addWidget(self._le_pw_rate)
        rate_row.addWidget(QLabel("(T/min)"))
        rate_row.addWidget(self._lbl_pw_rate_warn)
        rate_row.addStretch()
        self._le_pw_rate.textChanged.connect(self._on_pw_rate_changed)
        layout.addLayout(rate_row)

        rate_label = QLabel("속도 명령 — 목표 전송 **전**에 실행:")
        rate_label.setStyleSheet("color:#888; font-size:10px;")
        layout.addWidget(rate_label)
        self._pw_rate_list = QListWidget()
        self._pw_rate_list.setFont(_MONO)
        self._pw_rate_list.setMaximumHeight(44)
        layout.addWidget(self._pw_rate_list)
        layout.addLayout(self._cmd_button_row(self._pw_rate_add, self._pw_rate_del))
        self._pw_rate_cmds: list = []

        # 속도 명령은 보통 Config 에서 그 채널의 'Advance 전 명령'으로 이미 등록해 둔다.
        # 여기서 또 등록하게 하지 않고, 비어 있으면 그것을 그대로 쓴다.
        self._lbl_pw_rate_src = QLabel("")
        self._lbl_pw_rate_src.setStyleSheet("color:#79c0ff; font-size:10px;")
        self._lbl_pw_rate_src.setWordWrap(True)
        layout.addWidget(self._lbl_pw_rate_src)

        go_label = QLabel("ramp 시작 명령 — 목표 전송 **후**에 실행 (예: …:ACTN:RTOS):")
        go_label.setStyleSheet("color:#888; font-size:10px;")
        go_label.setToolTip(
            "목표값을 write 하는 것만으로 ramp 가 시작되는 장비면 비워 두세요.\n"
            "Mercury iPS 는 RTOS(ramp-to-set) 트리거가 필요합니다.")
        layout.addWidget(go_label)
        self._pw_go_list = QListWidget()
        self._pw_go_list.setFont(_MONO)
        self._pw_go_list.setMaximumHeight(44)
        layout.addWidget(self._pw_go_list)
        layout.addLayout(self._cmd_button_row(self._pw_go_add, self._pw_go_del))
        self._pw_go_cmds: list = []

        self._le_pw_settle = self._mono_edit("60", 54)
        self._le_pw_settle.setToolTip(
            "자기장이 도달·안정된 뒤 power sweep 을 시작하기까지 더 기다리는 시간.\n"
            "도달(tolerance)과 안정화(std) 판정은 Second 채널의 Controlled advance\n"
            "설정을 그대로 쓰고, 그 둘을 모두 통과한 다음 이 시간이 시작됩니다.")
        settle_row = QHBoxLayout()
        settle_row.setSpacing(4)
        settle_row.addWidget(QLabel("도달·안정화 후 대기:"))
        settle_row.addWidget(self._le_pw_settle)
        settle_row.addWidget(QLabel("s"))
        settle_row.addStretch()
        self._le_pw_settle.textChanged.connect(self._update_time_estimate)
        layout.addLayout(settle_row)

    def _on_pw_rate_changed(self, *_):
        """IPS 한계(0.3 T/min)를 넘으면 눈에 띄게 알린다 (막지는 않는다)."""
        rate = self._ds_f(self._le_pw_rate, 0.0)
        if rate > 0.3:
            self._lbl_pw_rate_warn.setText("⚠ IPS 한계(0.3) 초과")
        elif rate <= 0:
            self._lbl_pw_rate_warn.setText("⚠ 0 보다 커야 합니다")
        else:
            self._lbl_pw_rate_warn.setText("")
        self._update_time_estimate()

    def _pw_resolved_cmd(self, value: str) -> str:
        """선택한 power 명령이 value 에 대해 실제로 보낼 VISA 문자열.

        찾지 못하거나 조립에 실패하면 그 사유를 문자열로 돌려준다 — 조용히 빈 값을
        내면 '잘못 골랐다'는 것을 알 방법이 없다.
        """
        i = self._combo_power_cmd.currentIndex()
        cmds = self._cfg.acquire.sweep_cmds
        if not (0 <= i < len(cmds)):
            return ""
        entry = cmds[i]
        try:
            template = get_template(self._lib_reg.get_library(entry.alias), entry)
            if template is None:
                return f"[{entry.alias}] (라이브러리에서 명령을 찾을 수 없습니다)"
            return f"[{entry.alias}] {build_cmd(template, entry.params, value)}"
        except Exception as e:
            return f"[{entry.alias}] (명령 조립 실패: {type(e).__name__}: {e})"

    def _pw_update_cmd_preview(self, *_):
        """첫 power 점에서 실제로 나갈 명령을 그대로 보여 준다."""
        if not hasattr(self, "_lbl_pw_cmd_preview"):
            return
        resolved = self._pw_resolved_cmd(self._le_pw_start.text().strip() or "0")
        if not resolved:
            self._lbl_pw_cmd_preview.setText("↳ power 명령을 선택하세요")
            self._lbl_pw_cmd_preview.setStyleSheet("color:#d7ba7d;")
            return
        self._lbl_pw_cmd_preview.setText(f"↳ 첫 점에 보낼 명령:  {resolved}")
        self._lbl_pw_cmd_preview.setStyleSheet(
            "color:#d7ba7d;" if "(" in resolved.split("] ", 1)[-1][:1] else "color:#888;")

    def _pw_effective_rate_cmds(self) -> list:
        """실제로 전송할 속도 명령.

        직접 등록한 게 없으면 **Second 채널의 'Advance 전 명령'(pre_cmds)** 을 쓴다 —
        자기장 램프 속도 명령은 보통 ⚙ Config 에서 그 채널에 이미 달아 두기 때문에,
        같은 것을 여기서 또 등록하게 하지 않는다.
        """
        if self._pw_rate_cmds:
            return list(self._pw_rate_cmds)
        adv = self._selected_advance(self._combo_second_cmd)
        return list(getattr(adv, "pre_cmds", None) or []) if adv else []

    def _pw_refresh_rate_list(self):
        self._pw_rate_list.clear()
        for c in self._pw_rate_cmds:
            self._pw_rate_list.addItem(f"{c.alias}  {c.label or c.description}")
        self._pw_update_rate_source_hint()

    def _pw_update_rate_source_hint(self):
        """속도 명령을 직접 등록하지 않았을 때 무엇이 대신 쓰이는지 보여 준다."""
        if not hasattr(self, "_lbl_pw_rate_src"):
            return
        if self._pw_rate_cmds:
            self._lbl_pw_rate_src.setText("")
            return
        fallback = self._pw_effective_rate_cmds()
        if fallback:
            names = ", ".join(c.label or c.description for c in fallback)
            self._lbl_pw_rate_src.setText(
                f"↳ 비어 있음 — Second 채널의 'Advance 전 명령'을 씁니다: {names}")
            self._lbl_pw_rate_src.setStyleSheet("color:#79c0ff; font-size:10px;")
        else:
            self._lbl_pw_rate_src.setText(
                "↳ 비어 있고 Second 채널에도 없음 — 변화 속도가 장비로 전송되지 않습니다.")
            self._lbl_pw_rate_src.setStyleSheet("color:#d7ba7d; font-size:10px;")

    def _pw_rate_add(self):
        dlg = _PreAdvanceCmdDialog(self._lib_reg, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_cmd():
            self._pw_rate_cmds.append(dlg.result_cmd())
            self._pw_refresh_rate_list()

    def _pw_rate_del(self):
        i = self._pw_rate_list.currentRow()
        if 0 <= i < len(self._pw_rate_cmds):
            self._pw_rate_cmds.pop(i)
            self._pw_refresh_rate_list()

    def _pw_refresh_go_list(self):
        self._pw_go_list.clear()
        for c in self._pw_go_cmds:
            self._pw_go_list.addItem(f"{c.alias}  {c.label or c.description}")

    def _pw_go_add(self):
        dlg = _PreAdvanceCmdDialog(self._lib_reg, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_cmd():
            self._pw_go_cmds.append(dlg.result_cmd())
            self._pw_refresh_go_list()

    def _pw_go_del(self):
        i = self._pw_go_list.currentRow()
        if 0 <= i < len(self._pw_go_cmds):
            self._pw_go_cmds.pop(i)
            self._pw_refresh_go_list()

    def _build_direction_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("First 방향:"))
        self._combo_direction = QComboBox()
        self._combo_direction.addItem("단방향 (시작→끝 고정, dummy 복귀)", "uni")
        self._combo_direction.addItem("다중방향 (스텝마다 방향 교대)", "multi")
        self._combo_direction.currentIndexChanged.connect(self._refresh_ds_dynamic)
        row.addWidget(self._combo_direction, 1)
        return row

    def _build_pre_advance_widget(self) -> QWidget:
        """first=controlled + 단방향 + pre_cmds 가 있을 때만 _refresh_pre_advance 가 채운다."""
        self._pre_adv_widget = QWidget()
        self._pre_adv_lay = QVBoxLayout(self._pre_adv_widget)
        self._pre_adv_lay.setContentsMargins(0, 0, 0, 0)
        self._pre_adv_lay.setSpacing(3)
        self._pre_adv_rows: list = []
        self._pre_adv_widget.setVisible(False)
        return self._pre_adv_widget

    def _build_field_time_widget(self) -> QWidget:
        """First 채널을 시간 기반으로 측정할 때의 설정 (ramp 시작 → HOLD 감지)."""
        self._ft_widget = QWidget()
        layout = QVBoxLayout(self._ft_widget)
        layout.setContentsMargins(14, 0, 0, 0)
        layout.setSpacing(4)

        self._ft_interval = self._mono_edit("5", 46)
        self._ft_stat_intv = self._mono_edit("2", 46)
        interval_row = QHBoxLayout()
        interval_row.setSpacing(6)
        interval_row.addWidget(QLabel("acquire 간격(s):"))
        interval_row.addWidget(self._ft_interval)
        interval_row.addWidget(QLabel("상태폴링(s):"))
        interval_row.addWidget(self._ft_stat_intv)
        interval_row.addStretch()
        layout.addLayout(interval_row)

        self._ft_status_cmd = self._mono_edit("READ:DEV:GRPZ:PSU:ACTN")
        self._ft_status_cmd.setToolTip(
            "First 채널(자기장) 장비로 보내 HOLD/RTOS 상태를 읽는 명령")
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        status_row.addWidget(QLabel("상태 읽기 cmd:"))
        status_row.addWidget(self._ft_status_cmd, 1)
        layout.addLayout(status_row)

        self._ft_hold = self._mono_edit("HOLD", 80)
        self._ft_hold.setToolTip(
            "상태 응답에 이 문자열이 있으면 sweep 완료로 판단 "
            "(예: STAT:DEV:GRPZ:PSU:ACTN:HOLD → 'HOLD')")
        hold_row = QHBoxLayout()
        hold_row.setSpacing(6)
        hold_row.addWidget(QLabel("완료(HOLD) 토큰:"))
        hold_row.addWidget(self._ft_hold)
        hold_row.addStretch()
        layout.addLayout(hold_row)

        forward_label = QLabel("ramp 시작 명령 (선택):")
        forward_label.setToolTip(
            "목표값을 write한 뒤 실제 ramp를 '시작'시키는 트리거 명령.\n"
            "예: Mercury iPS는 목표 설정만으로 안 움직이므로 RTOS(ramp-to-set) 명령이 필요.\n"
            "First 명령 자체가 ramp까지 시작시키는 장비면 비워두세요.")
        layout.addWidget(forward_label)
        self._ft_fwd_list = QListWidget()
        self._ft_fwd_list.setFont(_MONO)
        self._ft_fwd_list.setMaximumHeight(56)
        layout.addWidget(self._ft_fwd_list)
        layout.addLayout(self._cmd_button_row(self._ft_add_cmd, self._ft_del_cmd))
        self._ft_fwd_cmds: list = []

        self._cb_ft_initial = QCheckBox("기다리지 않고 현재 온도에서 먼저 sweep")
        self._cb_ft_initial.setToolTip(
            "체크 시: 첫 second(온도) 목표 도달을 기다리지 않고, 현재 온도에서 정상 field\n"
            "sweep을 먼저 1회 수행합니다. 그다음 T1 도달→sweep→T2 도달→sweep… 로 이어갑니다.\n"
            "(재개 시에는 적용되지 않습니다.)")
        layout.addWidget(self._cb_ft_initial)

        self._cb_dummy_measure = QCheckBox("dummy(복귀) 구간도 측정")
        self._cb_dummy_measure.setToolTip(
            "체크 시: First 채널을 시작점으로 되돌리는 dummy 램프 동안에도 acquire 하여\n"
            "'<second>_dummy' 하위폴더에 저장합니다. (Double Sweep with Time에서 동작)")
        layout.addWidget(self._cb_dummy_measure)

        self._ft_widget.setVisible(False)
        return self._ft_widget

    def _build_finish_return_widget(self) -> QWidget:
        """측정이 정상 완료된 뒤 각 축을 어디로 되돌릴지.

        끄면 지금까지처럼 마지막 값에 그대로 멈춘다. Stop·오류로 끊긴 경우에는
        적용하지 않는다 — 그때는 아래 'Stop 시 실행 명령'이 나가야 한다.
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        label = QLabel("⏎ 측정 완료 후 복귀 (선택):")
        label.setStyleSheet("color:#7ee787; font-size:10px;")
        label.setToolTip(
            "체크한 축만 측정이 끝난 뒤 그 값으로 되돌립니다.\n"
            "First 를 먼저 내리고 그다음 Second 를 옮깁니다 — 시료에 신호를 걸어 둔 채\n"
            "마그넷을 움직이지 않기 위해서입니다.\n"
            "Second 는 그 채널의 advance 방식을 그대로 쓰므로 feedback 이면 도달까지 기다립니다.\n"
            "체크하지 않으면 마지막으로 쓴 값에 그대로 멈춥니다.\n"
            "Stop 이나 오류로 끊긴 경우에는 적용되지 않습니다 (아래 Stop 명령이 대신 나갑니다).")
        layout.addWidget(label)

        row = QHBoxLayout()
        row.setSpacing(4)
        self._cb_ret_first = QCheckBox("First")
        self._cb_ret_first.setToolTip(
            "Power Sweep 모드에서는 이 First 가 power 축입니다.")
        self._le_ret_first = self._mono_edit("0", 54)
        row.addWidget(self._cb_ret_first)
        row.addWidget(QLabel("→"))
        row.addWidget(self._le_ret_first)

        row.addSpacing(10)
        self._cb_ret_second = QCheckBox("Second")
        self._cb_ret_second.setToolTip(
            "Second sweep channel 을 쓸 때만 적용됩니다 (보통 자기장).")
        self._le_ret_second = self._mono_edit("0", 54)
        row.addWidget(self._cb_ret_second)
        row.addWidget(QLabel("→"))
        row.addWidget(self._le_ret_second)
        row.addStretch()
        layout.addLayout(row)

        # 체크한 축만 값 입력을 연다 (꺼진 칸의 값이 의미 있어 보이지 않게)
        self._cb_ret_first.toggled.connect(self._le_ret_first.setEnabled)
        self._cb_ret_second.toggled.connect(self._le_ret_second.setEnabled)
        self._le_ret_first.setEnabled(False)
        self._le_ret_second.setEnabled(False)
        return widget

    @staticmethod
    def _cmd_button_row(add_slot, del_slot) -> QHBoxLayout:
        """명령 목록에 딸린 '+ 명령' / '✕' 버튼 한 쌍."""
        row = QHBoxLayout()
        btn_add = QPushButton("+ 명령")
        btn_add.clicked.connect(add_slot)
        btn_del = QPushButton("✕")
        btn_del.setFixedWidth(26)
        btn_del.clicked.connect(del_slot)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        row.addStretch()
        return row

    def _build_stop_cmd_section(self, layout: QVBoxLayout) -> None:
        """Stop(또는 오류 중단) 시 자동 전송할 명령 목록."""
        label = QLabel("⏹ Stop 시 실행 명령 (선택):")
        label.setStyleSheet("color:#f78166; font-size:10px;")
        label.setToolTip(
            "측정 중 Stop(또는 오류 중단) 시 자동 전송할 명령.\n"
            "예: 자기장 ramp를 멈추려면 IPS의 HOLD(SET:DEV:GRPZ:PSU:ACTN:HOLD)를 등록.\n"
            "여러 개 등록 가능하며 위에서 아래 순서로 전송됩니다.")
        layout.addWidget(label)

        self._stop_cmd_list = QListWidget()
        self._stop_cmd_list.setFont(_MONO)
        self._stop_cmd_list.setMaximumHeight(56)
        layout.addWidget(self._stop_cmd_list)
        layout.addLayout(self._cmd_button_row(self._stop_add_cmd, self._stop_del_cmd))
        self._stop_cmds: list = []

    def _build_time_estimate_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("초/스텝:"))

        self._le_sec_per_step = self._mono_edit("1.0", 54)
        self._le_sec_per_step.setToolTip(
            "1회 acquire 예상 소요(초) — 전체 측정 시간 추정용")
        self._le_sec_per_step.textChanged.connect(self._update_time_estimate)
        row.addWidget(self._le_sec_per_step)

        self._lbl_time_est = QLabel("예상: —")
        self._lbl_time_est.setStyleSheet("color:#7ee787;")
        row.addWidget(self._lbl_time_est, 1)
        return row

    def _on_second_enable_toggled(self, checked: bool):
        self._second_widget.setVisible(checked)
        self._update_time_estimate()

    def _on_power_mode_toggled(self, checked: bool):
        """Power 모드 on/off — First 축 입력을 잠그고 field-time 과 배타 처리한다.

        둘 다 First 축을 대체하므로 동시에 켤 수 없다. Power 를 켜면 field-time 을 끈다.
        """
        self._power_widget.setVisible(checked)
        if checked and self._cb_field_time.isChecked():
            self._cb_field_time.setChecked(False)   # → _on_field_time_toggled 가 정리
        self._cb_field_time.setEnabled(not checked)
        self._sync_first_axis_enabled()
        self._refresh_ds_dynamic()

    def _on_field_time_toggled(self, checked: bool):
        self._ft_widget.setVisible(checked)
        self._sync_first_axis_enabled()
        self._refresh_ds_dynamic()

    def _power_mode_on(self) -> bool:
        """Power Sweep 모드가 켜져 있는지 (Time sweep 선택 시에는 항상 False)."""
        return (hasattr(self, "_cb_power_mode")
                and self._cb_power_mode.isChecked()
                and not self._is_time_sweep_selected())

    def _sync_first_axis_enabled(self):
        """First 축 입력의 활성 상태 — power/field-time 이 First 를 대체하면 잠근다."""
        power_on = hasattr(self, "_cb_power_mode") and self._cb_power_mode.isChecked()
        ft_on = hasattr(self, "_cb_field_time") and self._cb_field_time.isChecked()
        self._le_sw_n.setEnabled(not (ft_on or power_on))   # field-time·power는 First N 불필요
        # 콤보는 잠그지 않는다 — '⏱ Time' 전환 통로라서 잠그면 빠져나갈 길이 막힌다
        for w in (self._le_sw_start, self._le_sw_stop):
            w.setEnabled(not power_on)

    def _ft_refresh_list(self):
        self._ft_fwd_list.clear()
        for c in self._ft_fwd_cmds:
            self._ft_fwd_list.addItem(f"{c.alias}  {c.label or c.description}")

    def _ft_add_cmd(self):
        dlg = _PreAdvanceCmdDialog(self._lib_reg, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_cmd():
            self._ft_fwd_cmds.append(dlg.result_cmd())
            self._ft_refresh_list()

    def _ft_del_cmd(self):
        i = self._ft_fwd_list.currentRow()
        if 0 <= i < len(self._ft_fwd_cmds):
            self._ft_fwd_cmds.pop(i)
            self._ft_refresh_list()

    # ---- Stop 시 실행 명령 ----------------------------------------------
    def _stop_refresh_list(self):
        self._stop_cmd_list.clear()
        for c in self._stop_cmds:
            self._stop_cmd_list.addItem(f"{c.alias}  {c.label or c.description}")

    def _stop_add_cmd(self):
        dlg = _PreAdvanceCmdDialog(self._lib_reg, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_cmd():
            self._stop_cmds.append(dlg.result_cmd())
            self._stop_refresh_list()

    def _stop_del_cmd(self):
        i = self._stop_cmd_list.currentRow()
        if 0 <= i < len(self._stop_cmds):
            self._stop_cmds.pop(i)
            self._stop_refresh_list()

    def _refresh_ds_dynamic(self, *_):
        self._refresh_pre_advance()
        self._update_time_estimate()

    def _refresh_pre_advance(self):
        # 기존 행 제거
        while self._pre_adv_lay.count():
            item = self._pre_adv_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._pre_adv_rows = []
        first = self._get_selected_sweep_cmd()
        is_uni = self._combo_direction.currentData() == "uni"
        pre_cmds = (first.advance.pre_cmds
                    if (first and first.sweep_kind == "controlled" and first.advance)
                    else [])
        show = bool(pre_cmds) and is_uni and not self._is_time_sweep_selected()
        self._pre_adv_widget.setVisible(show)
        if not show:
            return
        hdr = QLabel("Advance 전 명령 값  (sweep 단계 / dummy 복귀 단계)")
        hdr.setStyleSheet("color:#79c0ff; font-size:10px;")
        self._pre_adv_lay.addWidget(hdr)
        for pc in pre_cmds:
            row = QWidget(); rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(4)
            cb = QCheckBox(pc.label or pc.description); cb.setMinimumWidth(120)
            le_s = QLineEdit(); le_s.setPlaceholderText("sweep")
            le_s.setFixedWidth(64); le_s.setFont(_MONO)
            le_d = QLineEdit(); le_d.setPlaceholderText("dummy")
            le_d.setFixedWidth(64); le_d.setFont(_MONO)
            rl.addWidget(cb); rl.addWidget(le_s); rl.addWidget(le_d); rl.addStretch()
            self._pre_adv_lay.addWidget(row)
            self._pre_adv_rows.append({"cmd": pc, "cb": cb, "sweep": le_s, "dummy": le_d})

    @staticmethod
    def _fmt_dur(s: float) -> str:
        s = int(s); h = s // 3600; m = (s % 3600) // 60; sec = s % 60
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {sec}s"
        return f"{sec}s"

    @staticmethod
    def _advance_min_time(adv) -> float:
        """controlled advance의 최소 소요(초) 추정. feedback/wait만 산정(이동시간 제외)."""
        if adv is None:
            return 0.0
        if adv.advance_type == A.FEEDBACK:
            # poll_interval × (Phase1 1회 + Phase2 std_window회)
            return adv.feedback_poll_interval * (1 + max(0, adv.feedback_std_window))
        if adv.advance_type == A.WAIT_FOR_TIME:
            return adv.wait_time
        return 0.0   # SWEEP/SIMPLE_HOP: 이동시간은 거리/rate라 불확실 → 제외

    def _selected_advance(self, combo) -> object:
        """콤보의 현재 sweep 명령이 controlled면 advance 설정 반환, 아니면 None."""
        i = combo.currentIndex()
        cmds = self._cfg.acquire.sweep_cmds
        if 0 <= i < len(cmds) and cmds[i].sweep_kind == "controlled":
            return cmds[i].advance
        return None

    def _update_time_estimate(self, *_):
        if self._is_time_sweep_selected():
            self._lbl_time_est.setText("예상: (Time 모드)")
            return
        n2 = 1
        second_on = self._cb_second_enable.isChecked()
        uni = self._combo_direction.currentData() == "uni"
        if second_on:
            try:
                n2 = max(1, int(float(self._le_2_n.text())))
            except ValueError:
                n2 = 0
        power_on = self._power_mode_on()
        # power 모드면 First 축 자리를 power 콤보가 대신한다
        first_combo = self._combo_power_cmd if power_on else self._combo_sweep_cmd
        first_min  = self._advance_min_time(self._selected_advance(first_combo))
        second_min = self._advance_min_time(self._selected_advance(self._combo_second_cmd)) if second_on else 0.0
        # 단방향이면 second 스텝 사이마다 first를 시작점으로 복귀(controlled) → (n2-1)회
        returns = (n2 - 1) if (uni and second_on) else 0

        # Field-Time: 자기장 ramp 시간은 알 수 없음 → 안정화/복귀 최소 + ramp 시간 안내
        if hasattr(self, "_cb_field_time") and self._cb_field_time.isChecked():
            stab = second_min * n2 + first_min * returns
            base = f"+ ~{self._fmt_dur(stab)} (안정화)" if stab > 0 else ""
            self._lbl_time_est.setText(f"예상: 자기장 ramp 시간 × {n2} {base}".strip())
            return

        try:
            n1 = max(1, int(float(
                (self._le_pw_n if power_on else self._le_sw_n).text())))
        except ValueError:
            n1 = 0
        try:
            sps = float(self._le_sec_per_step.text())
        except ValueError:
            sps = 0.0
        total = n1 * n2
        if total <= 0:
            self._lbl_time_est.setText("예상: —")
            return
        if power_on:
            self._lbl_time_est.setText(
                f"예상: {total} step · 약 {self._fmt_dur(self._power_mode_seconds(n1, n2, sps))}")
            return
        # acquire + first advance(매 스텝) + second advance + dummy 복귀
        secs = (total * sps
                + first_min * total
                + second_min * n2
                + first_min * returns)
        self._lbl_time_est.setText(f"예상: {total} step · 약 {self._fmt_dur(secs)}")

    def _power_mode_seconds(self, n1: int, n2: int, sps: float) -> float:
        """Power 모드 예상 소요 — 측정 대기와 자기장 ramp·안정화까지 더한다.

        자기장 ramp 시간은 '한 점 간격 / 변화 속도'로 잡는다. 첫 점(현재값 → 시작점)은
        현재 자기장을 알 수 없어 빠져 있으므로 실제로는 이보다 조금 더 걸린다.
        """
        pre = self._ds_f(self._le_pw_pre, 0.0)
        post = self._ds_f(self._le_pw_post, 0.0)
        settle = self._ds_f(self._le_pw_settle, 0.0)
        per_point = sps + max(0.0, pre) + max(0.0, post)
        secs = n1 * n2 * per_point + n2 * max(0.0, settle)

        rate = self._ds_f(self._le_pw_rate, 0.0)
        if rate > 0 and n2 > 1:
            span = abs(self._ds_f(self._le_2_stop, 0.0) - self._ds_f(self._le_2_start, 0.0))
            secs += (n2 - 1) * (span / (n2 - 1)) * 60.0 / rate
        return secs

    # ---- Sweep command helpers ---------------------------------------

    def _populate_sweep_cmds(self):
        """cfg.acquire.sweep_cmds로 콤보박스 갱신. 마지막에 '⏱ Time' 항목을 추가한다."""
        prev_idx  = self._combo_sweep_cmd.currentIndex()
        old_count = self._combo_sweep_cmd.count()
        # Time 항목은 항상 마지막 → 이전 선택이 마지막이면 Time이 선택돼 있던 것
        was_time = old_count > 0 and prev_idx == old_count - 1
        self._combo_sweep_cmd.blockSignals(True)
        self._combo_sweep_cmd.clear()
        for cmd in self._cfg.acquire.sweep_cmds:
            label = cmd.figure_axis or cmd.description or "(unnamed)"
            if not cmd.enabled:
                label = "✗OFF " + label
            self._combo_sweep_cmd.addItem(label)
        self._combo_sweep_cmd.addItem("⏱ Time (반복 측정)")   # 시간 기반 sweep
        self._combo_sweep_cmd.setToolTip(
            "측정하며 쓸어갈 축을 고릅니다.\n"
            "• 등록된 sweep 명령어: 그 값을 Start→Stop으로 N단계 바꾸며 측정\n"
            "• ⏱ Time: 값을 바꾸지 않고 Interval(초)마다 Count번 반복 측정")
        self._combo_sweep_cmd.blockSignals(False)
        time_index = self._combo_sweep_cmd.count() - 1
        if self._pending_time_select:
            new_idx = time_index
            self._pending_time_select = False
        elif was_time:
            new_idx = time_index
        elif 0 <= prev_idx < self._combo_sweep_cmd.count():
            new_idx = prev_idx
        else:
            new_idx = 0
        self._combo_sweep_cmd.setCurrentIndex(new_idx)
        self._populate_second_cmds()
        self._populate_power_cmds()
        self._on_sweep_cmd_changed(new_idx)

    def _populate_power_cmds(self):
        """Power Sweep 콤보를 sweep_cmds 로 갱신 (Time 항목 없음)."""
        if not hasattr(self, "_combo_power_cmd"):
            return
        cur = self._combo_power_cmd.currentIndex()
        self._combo_power_cmd.blockSignals(True)
        self._combo_power_cmd.clear()
        for cmd in self._cfg.acquire.sweep_cmds:
            label = cmd.figure_axis or cmd.description or "(unnamed)"
            if not cmd.enabled:
                label = "✗OFF " + label
            self._combo_power_cmd.addItem(label)
        self._combo_power_cmd.blockSignals(False)
        if 0 <= cur < self._combo_power_cmd.count():
            self._combo_power_cmd.setCurrentIndex(cur)
        self._pw_update_cmd_preview()

    def _populate_second_cmds(self):
        """Second sweep channel 콤보를 sweep_cmds로 갱신 (Time 항목 없음)."""
        if not hasattr(self, "_combo_second_cmd"):
            return
        cur = self._combo_second_cmd.currentIndex()
        self._combo_second_cmd.blockSignals(True)
        self._combo_second_cmd.clear()
        for cmd in self._cfg.acquire.sweep_cmds:
            label = cmd.figure_axis or cmd.description or "(unnamed)"
            if cmd.sweep_kind == "controlled" and cmd.advance is not None:
                label += f"  [CTRL:{cmd.advance.advance_type.value}]"
            if not cmd.enabled:
                label = "✗OFF " + label
            self._combo_second_cmd.addItem(label)
        self._combo_second_cmd.blockSignals(False)
        if 0 <= cur < self._combo_second_cmd.count():
            self._combo_second_cmd.setCurrentIndex(cur)

    def _collect_ds_control(self) -> VnaDoubleSweepControl:
        def _f(le, d=0.0):
            try:
                return float(le.text())
            except ValueError:
                return d

        def _i(le, d=0):
            try:
                return int(float(le.text()))
            except ValueError:
                return d
        pre_vals = [
            VnaPreCmdValue(enabled=r["cb"].isChecked(),
                           sweep_value=r["sweep"].text().strip(),
                           dummy_value=r["dummy"].text().strip())
            for r in self._pre_adv_rows
        ]
        field_time = VnaFieldTimeConfig(
            enabled=self._cb_field_time.isChecked(),
            interval=_f(self._ft_interval, 5.0),
            status_interval=_f(self._ft_stat_intv, 2.0),
            status_alias="",   # 상태는 First 채널 장비로 폴링 (워커 폴백)
            status_cmd=self._ft_status_cmd.text().strip(),
            hold_token=self._ft_hold.text().strip() or "HOLD",
            forward_cmds=list(self._ft_fwd_cmds),
        )
        return VnaDoubleSweepControl(
            first_idx=self._combo_sweep_cmd.currentIndex(),
            first_start=_f(self._le_sw_start),
            first_stop=_f(self._le_sw_stop, 1.0),
            first_n=_i(self._le_sw_n, 10),
            direction=self._combo_direction.currentData() or "uni",
            second_enabled=self._cb_second_enable.isChecked(),
            second_idx=self._combo_second_cmd.currentIndex(),
            second_start=_f(self._le_2_start),
            second_stop=_f(self._le_2_stop, 1.0),
            second_n=_i(self._le_2_n, 10),
            sec_per_step=_f(self._le_sec_per_step, 1.0),
            pre_values=pre_vals,
            field_time=field_time,
            stop_cmds=list(self._stop_cmds),
            second_use_custom_table=self._cb_second_keep_table.isChecked(),
            field_initial_sweep=self._cb_ft_initial.isChecked(),
            dummy_measure=self._cb_dummy_measure.isChecked(),
            power=VnaPowerSweepConfig(
                enabled=self._cb_power_mode.isChecked(),
                cmd_idx=max(0, self._combo_power_cmd.currentIndex()),
                start=_f(self._le_pw_start, -20.0),
                stop=_f(self._le_pw_stop, 0.0),
                n=_i(self._le_pw_n, 11),
                pre_measure_s=_f(self._le_pw_pre, 5.0),
                post_measure_s=_f(self._le_pw_post, 5.0),
                field_rate=_f(self._le_pw_rate, 0.3),
                rate_cmds=list(self._pw_rate_cmds),
                go_cmds=list(self._pw_go_cmds),
                field_settle_s=_f(self._le_pw_settle, 60.0),
            ),
            finish_return=VnaFinishReturnConfig(
                first_enabled=self._cb_ret_first.isChecked(),
                first_value=_f(self._le_ret_first, 0.0),
                second_enabled=self._cb_ret_second.isChecked(),
                second_value=_f(self._le_ret_second, 0.0),
            ),
        )

    def _apply_ds_control(self):
        c = self._cfg.ds_control
        di = self._combo_direction.findData(c.direction)
        if di >= 0:
            self._combo_direction.setCurrentIndex(di)
        self._cb_second_enable.setChecked(c.second_enabled)
        self._second_widget.setVisible(c.second_enabled)
        self._le_2_start.setText(f"{c.second_start:g}")
        self._le_2_stop.setText(f"{c.second_stop:g}")
        self._le_2_n.setText(str(c.second_n))
        self._le_sec_per_step.setText(f"{c.sec_per_step:g}")
        if 0 <= c.second_idx < self._combo_second_cmd.count():
            self._combo_second_cmd.setCurrentIndex(c.second_idx)
        if 0 <= c.first_idx < self._combo_sweep_cmd.count():
            self._combo_sweep_cmd.setCurrentIndex(c.first_idx)
        self._refresh_pre_advance()
        for row, pv in zip(self._pre_adv_rows, c.pre_values):
            row["cb"].setChecked(pv.enabled)
            row["sweep"].setText(pv.sweep_value)
            row["dummy"].setText(pv.dummy_value)
        # Double Sweep with Time 복원
        ft = c.field_time
        self._ft_interval.setText(f"{ft.interval:g}")
        self._ft_stat_intv.setText(f"{ft.status_interval:g}")
        self._ft_status_cmd.setText(ft.status_cmd)
        self._ft_hold.setText(ft.hold_token)
        self._ft_fwd_cmds = [pc.model_copy(deep=True) for pc in ft.forward_cmds]
        self._ft_refresh_list()
        self._cb_field_time.setChecked(ft.enabled)
        self._ft_widget.setVisible(ft.enabled)
        # Power Sweep 모드 복원 (field-time 과 배타 — 저장된 값이 둘 다 켜져 있으면
        # field-time 을 우선하고 power 는 끈다)
        pw = c.power
        self._le_pw_start.setText(f"{pw.start:g}")
        self._le_pw_stop.setText(f"{pw.stop:g}")
        self._le_pw_n.setText(str(pw.n))
        self._le_pw_pre.setText(f"{pw.pre_measure_s:g}")
        self._le_pw_post.setText(f"{pw.post_measure_s:g}")
        self._le_pw_rate.setText(f"{pw.field_rate:g}")
        self._le_pw_settle.setText(f"{pw.field_settle_s:g}")
        self._pw_rate_cmds = [pc.model_copy(deep=True) for pc in pw.rate_cmds]
        self._pw_refresh_rate_list()
        self._pw_go_cmds = [pc.model_copy(deep=True) for pc in pw.go_cmds]
        self._pw_refresh_go_list()
        self._on_pw_rate_changed()
        if 0 <= pw.cmd_idx < self._combo_power_cmd.count():
            self._combo_power_cmd.setCurrentIndex(pw.cmd_idx)
        power_on = pw.enabled and not ft.enabled
        self._cb_power_mode.setChecked(power_on)
        self._power_widget.setVisible(power_on)
        self._cb_field_time.setEnabled(not power_on)
        self._sync_first_axis_enabled()
        # 완료 후 복귀 복원
        fr = c.finish_return
        self._le_ret_first.setText(f"{fr.first_value:g}")
        self._le_ret_second.setText(f"{fr.second_value:g}")
        self._cb_ret_first.setChecked(fr.first_enabled)
        self._cb_ret_second.setChecked(fr.second_enabled)
        self._le_ret_first.setEnabled(fr.first_enabled)
        self._le_ret_second.setEnabled(fr.second_enabled)
        # Stop 시 실행 명령 복원
        self._stop_cmds = [pc.model_copy(deep=True) for pc in c.stop_cmds]
        self._stop_refresh_list()
        # Feature 1/2/3 체크박스 복원
        self._cb_second_keep_table.setChecked(c.second_use_custom_table)
        self._cb_ft_initial.setChecked(c.field_initial_sweep)
        self._cb_dummy_measure.setChecked(c.dummy_measure)
        self._update_time_estimate()

    def _is_time_sweep_selected(self) -> bool:
        """Sweep 콤보에서 '⏱ Time' 항목(항상 마지막)이 선택됐는지."""
        return self._combo_sweep_cmd.currentIndex() == len(self._cfg.acquire.sweep_cmds)

    def _on_sweep_cmd_changed(self, idx: int):
        """선택에 따라 파라미터 sweep 행 / 시간 행을 전환하고 단위 콤보를 갱신."""
        is_time = self._is_time_sweep_selected()
        self._sweep_param_widget.setVisible(not is_time)
        self._time_row_widget.setVisible(is_time)
        self._lbl_idle.setVisible(is_time)
        self._btn_sweep.setText("▶ Time Sweep" if is_time else "▶ Sweep Acquire")
        if hasattr(self, "_ds_section"):
            self._ds_section.setVisible(not is_time)
            self._refresh_ds_dynamic()
        if is_time:
            self._cb_sw_start_unit.setVisible(False)
            self._cb_sw_stop_unit.setVisible(False)
            return
        cmd = self._get_selected_sweep_cmd()
        ut  = cmd.unit_type if cmd else ""
        opts = _UNIT_OPTIONS.get(ut, [])
        default_lbl = _UNIT_DEFAULT.get(ut, "")
        for cb in (self._cb_sw_start_unit, self._cb_sw_stop_unit):
            cb.blockSignals(True)
            cb.clear()
            for label, mult in opts:
                cb.addItem(label, mult)
            idx2 = cb.findText(default_lbl)
            if idx2 >= 0:
                cb.setCurrentIndex(idx2)
            cb.blockSignals(False)
            cb.setVisible(bool(opts))

    def _get_selected_sweep_cmd(self) -> Optional[VnaCommandEntry]:
        idx = self._combo_sweep_cmd.currentIndex()
        if 0 <= idx < len(self._cfg.acquire.sweep_cmds):
            return self._cfg.acquire.sweep_cmds[idx]
        return None

    def _sweep_unit_mult(self, cb: QComboBox) -> float:
        """unit 콤보가 보이면 배수 반환, 아니면 1.0."""
        if cb.isVisible() and cb.count() > 0:
            return cb.currentData() or 1.0
        return 1.0

    # ---- Right panel: two plots side by side -------------------------

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(8)

        pg.setConfigOptions(antialias=False,
                            background="#0d1117", foreground="#c9d1d9")

        self._panel_L = PlotPanel(start_color_idx=0, square_hint=True)
        self._panel_R = PlotPanel(start_color_idx=2, square_hint=True)
        lay.addWidget(self._panel_L, stretch=1)
        lay.addWidget(self._panel_R, stretch=1)
        return w

    # ------------------------------------------------------------------
    # Section rebuilding
    # ------------------------------------------------------------------

    def _rebuild_sections(self):
        for sw in self._section_widgets:
            sw.setParent(None)
            sw.deleteLater()
        self._section_widgets.clear()
        self._role_rows  = {"start": None, "stop": None, "n_points": None}
        self._linspace_data = None

        count = self._sections_lay.count()
        for sec_cfg in self._cfg.sections:
            sw = _SectionWidget(sec_cfg, self._lib_reg)
            sw.execute_requested.connect(self._on_execute_section)
            sw.role_toggle_requested.connect(self._handle_role_toggle)
            self._section_widgets.append(sw)
            self._sections_lay.insertWidget(count - 1, sw)
            count += 1

        self._init_role_map()

    # ------------------------------------------------------------------
    # Role management
    # ------------------------------------------------------------------

    def _init_role_map(self):
        """섹션 재빌드 후 sweep_role이 저장된 rows를 찾아 _role_rows에 등록."""
        for sw in self._section_widgets:
            for row in sw._cmd_rows:
                role = row._current_role
                if not role:
                    continue
                if role in self._role_rows and self._role_rows[role] is None:
                    self._role_rows[role] = row
                    row.set_role(role)      # 버튼 시각 동기화
                else:
                    # 중복 저장된 role → 초기화
                    row._current_role = ""
                    row.set_role("")

    @Slot(object, str)
    def _handle_role_toggle(self, row: "_CmdRowWidget", role: str):
        """role 버튼 클릭 처리 — 전역 단일 할당 관리."""
        # 이미 이 row에 이 role이 있으면 → 해제 (토글 오프)
        if self._role_rows.get(role) is row:
            self._role_rows[role] = None
            row.set_role("")
            self._refresh_plot_sources()
            return

        # 다른 row에 이미 이 role이 할당된 경우 → 재할당 확인
        old_row = self._role_rows.get(role)
        if old_row is not None:
            role_name = {"start": "Start", "stop": "Stop", "n_points": "N Points"}[role]
            ret = QMessageBox.question(
                self, "역할 재할당",
                f"'{role_name}'이 이미 다른 항목에 할당되어 있습니다.\n이 항목으로 교체하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            old_row.set_role("")

        # 이 row가 다른 role을 갖고 있으면 먼저 해제
        old_role = row._current_role
        if old_role and old_role in self._role_rows and self._role_rows[old_role] is row:
            self._role_rows[old_role] = None

        self._role_rows[role] = row
        row.set_role(role)
        self._refresh_plot_sources()

    def _roles_complete(self) -> bool:
        """start / stop / n_points 3개 모두 할당됐는지 확인."""
        return all(self._role_rows.get(k) is not None
                   for k in ("start", "stop", "n_points"))

    def _get_role_value_si(self, row: "_CmdRowWidget") -> float:
        """row의 user_input 값을 SI 단위로 변환하여 반환."""
        if row._le_user is None:
            return 0.0
        raw = row._le_user.text().strip()
        try:
            val = float(raw)
        except ValueError:
            return 0.0
        if row._cb_unit is not None and row._cb_unit.isVisible():
            mult = row._cb_unit.currentData() or 1.0
            val *= mult
        return val

    def _compute_linspace(self) -> bool:
        """3개 role이 모두 할당된 경우 linspace를 계산하여 _linspace_data에 저장."""
        if not self._roles_complete():
            self._linspace_data = None
            return False

        start_row = self._role_rows["start"]
        stop_row  = self._role_rows["stop"]
        npts_row  = self._role_rows["n_points"]

        start_val = self._get_role_value_si(start_row)
        stop_val  = self._get_role_value_si(stop_row)
        npts_raw  = npts_row._le_user.text().strip() if npts_row._le_user else "2"
        try:
            n = max(2, int(float(npts_raw or "2")))
        except ValueError:
            n = 2

        arr  = np.linspace(start_val, stop_val, n)
        unit = (start_row._cb_unit.currentText()
                if (start_row._cb_unit and start_row._cb_unit.isVisible()) else "")
        self._linspace_data = ("time", unit, arr)
        return True

    # ------------------------------------------------------------------
    # Section execute
    # ------------------------------------------------------------------

    def _on_execute_section(self, cmds: list):
        if not cmds:
            self._set_status("No enabled commands.", color="#888")
            return
        if self._sec_worker and self._sec_worker.isRunning():
            self._set_status("Busy — please wait.", color="#888")
            return
        self._sec_worker = _VnaWorker(self._session, cmds)
        # bound 슬롯으로 연결 (워커 스레드 GUI 접근 크래시 방지 — 메인 스레드 큐잉)
        self._sec_worker.done.connect(self._on_section_done)
        self._sec_worker.error.connect(self._on_section_error)
        self._set_status("Running...", color="#888")
        self._sec_worker.start()

    @Slot(list)
    def _on_section_done(self, results: list):
        self._set_status(f"Section executed ({len(results)} cmd(s)).", color="#7ee787")

    @Slot(str)
    def _on_section_error(self, msg: str):
        self._set_status(f"Error: {msg}", color="#f78166")

    # ------------------------------------------------------------------
    # Acquire
    # ------------------------------------------------------------------

    def _on_single_acquire(self):
        self._start_acquire(sweep_values=None)

    def _on_sweep_acquire(self):
        if self._is_time_sweep_selected():
            self._on_time_sweep()
            return
        if not self._cfg.acquire.sweep_cmds:
            self._set_status("Sweep 명령어가 등록되지 않았습니다. Config를 확인하세요.",
                             color="#f78166")
            return
        # Power 모드는 First 콤보 대신 power 명령을 쓴다 → 그 유효성은 _resolve_power_channel 이 본다
        if self._power_mode_on():
            if not self._confirm_power_mode_ready():
                return
            self._start_double_sweep()
            return
        sel = self._get_selected_sweep_cmd()
        if sel is not None and not sel.enabled:
            self._set_status("선택한 Sweep 명령이 비활성(OFF) 상태입니다. "
                             "활성화하거나 다른 명령을 선택하세요.", color="#f78166")
            return
        # double sweep 조건: second 채널 사용 또는 first가 Controlled Sweep
        if self._cb_second_enable.isChecked() or (sel is not None and sel.sweep_kind == "controlled"):
            self._start_double_sweep()
            return
        try:
            start_raw = float(self._le_sw_start.text())
            stop_raw  = float(self._le_sw_stop.text())
            n         = int(self._le_sw_n.text())
            if n < 1:
                raise ValueError
        except ValueError:
            self._set_status("Invalid sweep parameters.", color="#f78166")
            return
        start = start_raw * self._sweep_unit_mult(self._cb_sw_start_unit)
        stop  = stop_raw  * self._sweep_unit_mult(self._cb_sw_stop_unit)
        values = ([start] if n == 1
                  else [start + (stop - start) * i / (n - 1)
                        for i in range(n)])
        self._start_acquire(sweep_values=values)

    def _on_time_sweep(self):
        """시간 기반 sweep 시작 — Interval(초)마다 acquire 1회씩 Count번."""
        try:
            interval = float(self._le_time_interval.text())
            count    = int(self._le_time_count.text())
            if interval < 0 or count < 1:
                raise ValueError
        except ValueError:
            self._set_status("Invalid time-sweep parameters.", color="#f78166")
            return
        self._lbl_idle.setText("Idle: —")
        self._lbl_idle.setStyleSheet("color: #555;")
        self._start_acquire(sweep_values=None,
                            time_mode=True, time_interval=interval, time_count=count)

    @staticmethod
    def _sweep_col_meta(cmd) -> tuple:
        """sweep 명령 → 데이터 열 (label, unit).

        값은 SI 기준으로 기록되므로 unit은 units/unit_label, 없으면 unit_type의 기본 SI 단위.
        """
        label = cmd.figure_axis or cmd.description or "sweep"
        _BASE = {"Hz": "Hz", "sec": "s", "T": "T", "K": "K"}
        unit = cmd.units or cmd.unit_label or _BASE.get(cmd.unit_type, "")
        return (label, unit)

    @staticmethod
    def _ds_f(le, default: float = 0.0) -> float:
        try:
            return float(le.text())
        except ValueError:
            return default

    @staticmethod
    def _linspace(a: float, b: float, n: int):
        n = max(1, n)
        if n == 1:
            return [a]
        return [a + (b - a) * i / (n - 1) for i in range(n)]

    def _start_double_sweep(self, resume_offset: int = 0,
                            resume_second_vals: Optional[list] = None,
                            resume_folder: Optional[Path] = None,
                            resume_full_vals: Optional[list] = None):
        """First/Second 채널·방향·pre-advance 를 묶어 double sweep 을 실행한다.

        resume_*: 재개 시 — 남은 second 값 목록(resume_second_vals), 전역 인덱스
        오프셋(resume_offset), 이어서 저장할 폴더(resume_folder)를 받아 그 지점부터
        시작한다.

        입력이 하나라도 잘못되면 상태줄에 알리고 아무것도 시작하지 않는다.
        """
        first = self._resolve_first_channel()
        if first is None:
            return

        second = self._resolve_second_channel(
            resume_offset, resume_second_vals, resume_full_vals)
        if second is _INVALID:
            return

        plan = self._build_ds_plan(first, second, resume_offset)
        self._configure_ds_columns(first, second)
        self._persist_ds_control()

        self._start_acquire(sweep_values=None, ds_plan=plan,
                            resume_folder=resume_folder)
        self._init_resume_state(second, resume_second_vals)

    # ── _start_double_sweep 의 단계별 처리 ────────────────────────────────

    def _power_mode_warnings(self) -> List[str]:
        """Power 모드 시작 전 짚어야 할 것들. 비어 있으면 그대로 시작해도 된다.

        전부 '측정은 돌지만 결과가 조용히 틀어지는' 종류라 막지는 않고 확인만 받는다.
        """
        warnings: List[str] = []
        if not self._cb_second_enable.isChecked():
            warnings.append(
                "Second sweep channel(자기장)이 꺼져 있습니다 — 자기장을 바꾸지 않고 "
                "power sweep 만 1회 수행합니다.")
            return warnings      # 자기장을 안 쓰면 아래 항목은 볼 필요가 없다

        adv = self._selected_advance(self._combo_second_cmd)
        read_cmd = (getattr(adv, "feedback_read_cmd", "") or "").strip() if adv else ""
        if not read_cmd:
            warnings.append(
                "자기장 채널에 도달 확인용 Read Cmd 가 없습니다 — 도달을 기다리지 않고 "
                "곧바로 '도달 후 대기'로 넘어갑니다 (ramp 중에 측정이 시작될 수 있습니다).\n"
                "    ⚙ Config 에서 그 명령을 Controlled Sweep(feedback)으로 등록하고 "
                "Read Cmd 를 넣으세요.")
        elif not (getattr(adv, "feedback_std_window", 0) > 0
                  and getattr(adv, "feedback_std_threshold", 0.0) > 0.0):
            warnings.append(
                "자기장 채널의 안정화 검사(Std Window / Std Threshold)가 꺼져 있습니다 — "
                "tolerance band 안에 들어오기만 하면 바로 '도달 후 대기'로 넘어갑니다.\n"
                "    램프 직후 출렁임까지 가라앉히려면 ⚙ Config 에서 그 값들을 설정하세요.")
        rate_cmds = self._pw_effective_rate_cmds()
        if not rate_cmds:
            warnings.append(
                "자기장 '속도 명령'이 없습니다 (이 창에도, Second 채널의 Advance 전 "
                "명령에도) — 입력한 변화 속도가 장비로 전송되지 않고, 장비에 이미 "
                "설정된 속도로 움직입니다.")
        rate = self._ds_f(self._le_pw_rate, 0.0)
        if rate_cmds and rate > 0.3:
            warnings.append(
                f"변화 속도 {rate:g} 가 Mercury iPS 한계(0.3 T/min)를 넘습니다 — "
                "장비가 명령을 거부하거나 자체 한계로 잘라낼 수 있습니다.")
        return warnings

    def _confirm_power_mode_ready(self) -> bool:
        """경고가 있으면 목록을 보여 주고 계속할지 묻는다. 없으면 바로 True."""
        warnings = self._power_mode_warnings()
        if not warnings:
            return True
        body = "\n\n".join(f"• {w}" for w in warnings)
        answer = QMessageBox.question(
            self, "Power Sweep — 시작 전 확인",
            f"{body}\n\n이대로 시작하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _resolve_power_channel(self) -> Optional["_FirstChannel"]:
        """Power Sweep 모드의 First 채널 — power 명령 + 처음~끝 N 점.

        Second(자기장) 한 값마다 이 목록을 순서대로 훑는다. power 는 명령을 쓰는
        즉시 반영되므로 advance(도달 대기)는 쓰지 않는다.
        """
        idx = self._combo_power_cmd.currentIndex()
        cmds = self._cfg.acquire.sweep_cmds
        if not (0 <= idx < len(cmds)):
            self._set_status("Power sweep 명령을 선택하세요.", color="#f78166")
            return None
        cmd = cmds[idx]
        if not cmd.enabled:
            self._set_status("선택한 Power 명령이 비활성(OFF) 상태입니다.", color="#f78166")
            return None
        try:
            start = float(self._le_pw_start.text())
            stop = float(self._le_pw_stop.text())
            count = int(self._le_pw_n.text())
            if count < 1:
                raise ValueError
        except ValueError:
            self._set_status("Power 범위가 올바르지 않습니다.", color="#f78166")
            return None
        return _FirstChannel(
            cmd=cmd,
            values=self._linspace(start, stop, count),
            advance=cmd.advance if cmd.sweep_kind == "controlled" else None,
            field_time=False,
        )

    def _resolve_first_channel(self) -> Optional["_FirstChannel"]:
        """First 채널 명령 + 값 목록. 입력이 잘못되면 상태줄에 알리고 None."""
        if self._power_mode_on():
            return self._resolve_power_channel()

        cmd = self._get_selected_sweep_cmd()
        if cmd is None:
            self._set_status("First sweep 명령을 선택하세요.", color="#f78166")
            return None

        field_time = self._cb_field_time.isChecked()
        try:
            start = float(self._le_sw_start.text())
            stop = float(self._le_sw_stop.text())
            # field-time 모드는 스텝 수를 미리 알 수 없어 N 을 쓰지 않는다
            count = 2 if field_time else int(self._le_sw_n.text())
            if count < 1:
                raise ValueError
        except ValueError:
            self._set_status("First 범위가 올바르지 않습니다.", color="#f78166")
            return None

        start *= self._sweep_unit_mult(self._cb_sw_start_unit)
        stop *= self._sweep_unit_mult(self._cb_sw_stop_unit)
        return _FirstChannel(
            cmd=cmd,
            values=self._linspace(start, stop, count),
            advance=cmd.advance if cmd.sweep_kind == "controlled" else None,
            field_time=field_time,
        )

    def _resolve_second_channel(self, resume_offset, resume_second_vals,
                                resume_full_vals):
        """Second 채널 설정. 꺼져 있으면 비활성 _SecondChannel, 오류면 _INVALID.

        second 값 목록의 source of truth 는 테이블 모델이다 — 워커가 실행 중 매 행을
        모델에서 새로 읽으므로 아직 측정하지 않은 행은 도중에 고칠 수 있다.
        """
        if not self._cb_second_enable.isChecked():
            return _SecondChannel(enabled=False, cmd=None, values=[], advance=None)

        index = self._combo_second_cmd.currentIndex()
        if not (0 <= index < len(self._cfg.acquire.sweep_cmds)):
            self._set_status("Second sweep 명령을 선택하세요.", color="#f78166")
            return _INVALID
        cmd = self._cfg.acquire.sweep_cmds[index]

        try:
            start = float(self._le_2_start.text())
            stop = float(self._le_2_stop.text())
            count = int(self._le_2_n.text())
            if count < 1:
                raise ValueError
        except ValueError:
            self._set_status("Second 범위가 올바르지 않습니다.", color="#f78166")
            return _INVALID

        if resume_second_vals is not None:
            full = list(resume_full_vals if resume_full_vals is not None
                        else resume_second_vals)
            self._second_table.reset_from(full, done_prefix=resume_offset)
            values = self._second_table.values()[resume_offset:]
        elif self._cb_second_keep_table.isChecked() and self._second_table.count() > 0:
            self._second_table.rearm()   # 커스텀 테이블 유지, 상태만 PENDING 으로
            values = self._second_table.values()
        else:
            self._second_table.reset_from(self._linspace(start, stop, count))
            values = self._second_table.values()

        return _SecondChannel(
            enabled=True, cmd=cmd, values=values,
            advance=cmd.advance if cmd.sweep_kind == "controlled" else None)

    def _configure_ds_columns(self, first: "_FirstChannel", second: "_SecondChannel"):
        """저장 파일의 열 구성과 파일명 토큰을 정한다.

        second 값은 하위폴더 이름이 되고(_ds_second_label), first 값은 파일명 토큰이
        된다. field-time 모드는 스텝 수를 모르므로 인덱스 기반 파일명을 쓴다.
        """
        self._ds_second_label = (self._sweep_col_meta(second.cmd)[0]
                                 if second.is_active else "")
        self._ds_field_time = first.field_time

        if first.field_time:
            self._ds_step_labels = None
            # 자기장은 read 명령으로 기록되므로 열에는 second(온도) 값만 넣는다
            self._extra_cols = ([self._sweep_col_meta(second.cmd)]
                                if second.is_active else [])
            return

        self._ds_step_labels = self._build_step_labels(first, second)
        # worker 의 extra_values 순서와 일치해야 한다
        self._extra_cols = [self._sweep_col_meta(first.cmd)]
        if second.is_active:
            self._extra_cols.append(self._sweep_col_meta(second.cmd))

    def _build_step_labels(self, first: "_FirstChannel",
                           second: "_SecondChannel") -> list:
        """스텝별 파일명 토큰 = first 값.

        다중방향이면 second 스텝마다 first 진행 방향이 뒤집힌다.
        """
        direction = self._combo_direction.currentData() or "uni"
        outer = second.values if second.enabled else [None]
        labels = []
        for si, _ in enumerate(outer):
            forward = direction == "uni" or si % 2 == 0
            order = first.values if forward else list(reversed(first.values))
            labels.extend(f"{value:.6g}".replace('+', '') for value in order)
        return labels

    def _build_ds_plan(self, first: "_FirstChannel", second: "_SecondChannel",
                       resume_offset: int) -> dict:
        pre_specs = [
            {"cmd": row["cmd"], "sweep": row["sweep"].text().strip(),
             "dummy": row["dummy"].text().strip()}
            for row in self._pre_adv_rows if row["cb"].isChecked()
        ]
        return {
            "first_cmd": first.cmd,
            "first_values": first.values,
            "first_adv": first.advance,
            "second_enabled": second.enabled,
            "second_cmd": second.cmd,
            "second_values": second.values,
            "second_adv": second.advance,
            "direction": self._combo_direction.currentData() or "uni",
            "pre_specs": pre_specs,
            "field_time": self._build_field_time_plan() if first.field_time else None,
            "second_index_offset": resume_offset,
            "second_table": (self._second_table if second.enabled else None),
            # 첫 second 목표 도달을 기다리지 않고 현재 상태에서 한 번 먼저 sweep
            "field_initial_sweep": (first.field_time and second.enabled
                                    and resume_offset == 0
                                    and self._cb_ft_initial.isChecked()),
            "dummy_measure": self._cb_dummy_measure.isChecked(),
            # Power Sweep 모드 전용 타이밍·자기장 이동 (모드가 꺼져 있으면 None)
            "power": self._build_power_plan() if self._power_mode_on() else None,
            # 정상 완료 후 복귀 (Stop/오류로 끊기면 워커가 건너뛴다)
            "finish_return": {
                "first_enabled":  self._cb_ret_first.isChecked(),
                "first_value":    self._ds_f(self._le_ret_first, 0.0),
                "second_enabled": self._cb_ret_second.isChecked(),
                "second_value":   self._ds_f(self._le_ret_second, 0.0),
            },
        }

    def _build_power_plan(self) -> dict:
        """Power Sweep 모드에서 워커가 쓸 타이밍·자기장 이동 설정."""
        return {
            "pre_measure_s":  self._ds_f(self._le_pw_pre, 5.0),
            "post_measure_s": self._ds_f(self._le_pw_post, 5.0),
            "field_rate":     self._ds_f(self._le_pw_rate, 0.3),
            "rate_cmds":      self._pw_effective_rate_cmds(),
            "go_cmds":        list(self._pw_go_cmds),
            "field_settle_s": self._ds_f(self._le_pw_settle, 60.0),
        }

    def _build_field_time_plan(self) -> dict:
        return {
            "enabled": True,
            "interval": self._ds_f(self._ft_interval, 5.0),
            "status_interval": self._ds_f(self._ft_stat_intv, 2.0),
            "status_alias": "",   # First 채널 장비로 폴링 (워커 폴백)
            "status_cmd": self._ft_status_cmd.text().strip(),
            "hold_token": self._ft_hold.text().strip() or "HOLD",
            "forward_cmds": list(self._ft_fwd_cmds),
        }

    def _persist_ds_control(self):
        """방금 돌린 설정이 재시작 후에도 남도록 프로파일에 저장한다.

        저장 실패가 측정을 막지는 않는다 — 경고만 남기고 진행.
        """
        try:
            self._cfg.ds_control = self._collect_ds_control()
            save_vna_config(self._cfg, self._config_path())
        except Exception as e:
            self._set_status(f"설정 저장 경고: {type(e).__name__}: {e}", color="#888")

    def _init_resume_state(self, second: "_SecondChannel", resume_second_vals):
        """중단 시 이어갈 지점을 기록한다. second 채널을 쓰고 저장이 켜져 있을 때만.

        재개로 들어온 경우에는 _resume_double_sweep 이 이미 상태를 세팅해 뒀다.
        """
        can_resume = (second.enabled and self._cb_save.isChecked()
                      and self._sweep_folder is not None)
        if not can_resume:
            self._resume_state = None
            return
        if resume_second_vals is not None:
            return

        self._resume_state = VnaResumeState(
            active=True, sweep_folder=str(self._sweep_folder),
            second_values=[float(v) for v in second.values],
            completed_second_idx=-1, field_time=self._ds_field_time,
            filename=self._le_filename.text().strip() or "vna_data",
            figure_axis=self._sweep_figure_axis,
            control=self._cfg.ds_control)
        self._save_resume()

    @Slot(int, float)
    def _on_step_elapsed(self, step_idx: int, elapsed: float):
        """각 acquire 스텝의 실제 소요 시간을 상태줄에 기록 (Single·Sweep 공통)."""
        self._set_status(f"Step {step_idx + 1} acquire 완료 — 소요 {elapsed:.3f} s", color="#7ee787")

    @Slot(int, float)
    def _on_step_timing(self, step_idx: int, remaining: float):
        """시간 모드: 스텝 read 완료 후 남은/부족 시간 표시."""
        if remaining >= 0:
            self._lbl_idle.setText(f"Idle: {remaining:.3f} s")
            self._lbl_idle.setStyleSheet("color: #7ee787;")
        else:
            self._lbl_idle.setText(f"Overrun: {remaining:.3f} s (부족)")
            self._lbl_idle.setStyleSheet("color: #f78166;")

    def _start_acquire(self, sweep_values, time_mode: bool = False,
                       time_interval: float = 1.0, time_count: int = 1,
                       ds_plan: Optional[dict] = None,
                       resume_folder: Optional[Path] = None):
        if self._acq_thread and self._acq_thread.isRunning():
            self._set_status("Acquire already running.", color="#888")
            return

        is_sweep = sweep_values is not None or ds_plan is not None
        if ds_plan is not None:
            # double sweep: 스텝 수만큼 placeholder (is_sweep 판정용), 파일명은 _ds_step_labels
            self._current_sweep_values = [None] * len(self._ds_step_labels or [])
        else:
            self._current_sweep_values = sweep_values
            self._ds_step_labels = None
            self._ds_field_time = False   # 단일 sweep: 직전 double-sweep 상태 잔존 방지
        self._step_count = 0

        # Sweep 전용: 저장 폴더 생성 + figure_axis 확보
        self._sweep_folder: Optional[Path] = None
        self._sweep_figure_axis: str = ""
        if is_sweep and self._cb_save.isChecked():
            if ds_plan is not None:
                fc = ds_plan["first_cmd"]
                self._sweep_figure_axis = (fc.figure_axis or fc.description) if fc else "sweep"
            else:
                selected_cmd = self._get_selected_sweep_cmd()
                self._sweep_figure_axis = (
                    (selected_cmd.figure_axis or selected_cmd.description)
                    if selected_cmd else "sweep")
            filename = self._le_filename.text().strip() or "vna_data"
            if resume_folder is not None:
                self._sweep_folder = Path(resume_folder)   # 재개: 같은 폴더에 이어 저장
            else:
                base_dir = self._get_save_dir()
                self._sweep_folder = next_sweep_folder(base_dir, filename)
            self._sweep_folder.mkdir(parents=True, exist_ok=True)
        # second 채널 값별 하위폴더 상태 (second_changed 신호로 갱신)
        self._current_second_dir: Optional[str] = None
        self._second_local_idx: int = 0

        selected_cmd = (self._get_selected_sweep_cmd()
                        if (is_sweep and ds_plan is None) else None)
        # swept value를 데이터 열로 추가 (single sweep). double sweep은 _start_double_sweep에서 설정.
        if ds_plan is None:
            self._extra_cols = ([self._sweep_col_meta(selected_cmd)]
                                if selected_cmd is not None else [])
        # section role(start/stop/n_points)이 모두 할당됐으면 linspace "time" 축 계산.
        _ls_was_none = self._linspace_data is None
        if self._compute_linspace() and _ls_was_none:
            # 최초 계산 시 x-source가 "index"이면 "time"으로 자동 전환
            for _panel in (self._panel_L, self._panel_R):
                if _panel.x_source() == "index":
                    _panel.set_default_x("time")
        self._refresh_plot_sources()   # 추가된 swept value/time 열을 플롯 소스에도 반영
        worker = _AcquireWorker(
            self._session, self._lib_reg, self._cfg.acquire,
            sweep_values, sweep_cmd=selected_cmd,
            time_mode=time_mode, time_interval=time_interval, time_count=time_count,
            ds_plan=ds_plan)
        # parent를 주지 않는다(QThread(self)면 창이 sweep 중 파괴될 때 running thread가
        # 함께 파괴돼 std::terminate). 수명은 self._acq_thread 참조 + deleteLater로 관리.
        thread = QThread()
        worker.moveToThread(thread)

        # 워커(다른 스레드) → GUI 갱신은 반드시 bound 슬롯으로 연결한다.
        # lambda로 연결하면 수신 QObject가 없어 큐잉되지 않고 '워커 스레드'에서 직접
        # 실행되어 GUI(QLabel 등)를 만지다 access violation으로 크래시한다.
        self._acq_is_sweep = is_sweep
        thread.started.connect(worker.run)
        worker.step_done.connect(self._on_acq_step_done)
        worker.second_changed.connect(self._on_second_changed)
        worker.second_done.connect(self._on_second_done)
        worker.second_row.connect(self._on_second_row)
        worker.step_timing.connect(self._on_step_timing)
        worker.step_elapsed.connect(self._on_step_elapsed)
        worker.progress.connect(self._on_acq_progress)
        worker.warn.connect(self._on_acq_warn)
        worker.error.connect(self._on_acq_error)
        worker.finished.connect(self._on_acq_finished_slot)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_acq_refs)

        self._acq_worker = worker
        self._acq_thread = thread
        self._time_mode_running = time_mode
        self._acq_errored = False
        self._acq_stopped = False
        self._set_acquire_busy(True)
        thread.start()

    def _clear_acq_refs(self):
        self._acq_worker = None
        self._acq_thread = None

    @Slot(int, list)
    def _on_acq_step_done(self, step_idx: int, arrays: List[np.ndarray]):
        # 메인 스레드 슬롯 — 예외가 Qt 이벤트 루프로 전파되면 프로세스가 abort될 수 있어
        # 플롯/저장을 각각 방어한다(실패해도 측정은 계속, 원인은 로그로 남김).
        self._step_count += 1
        try:
            self._update_plots(arrays, first_step=(self._step_count == 1))
        except Exception:
            get_logger().exception("update_plots failed (step %d)", step_idx)
        if self._cb_save.isChecked():
            try:
                is_sweep = self._current_sweep_values is not None
                self._save_step(step_idx, arrays, is_sweep)
            except Exception:
                get_logger().exception("save_step failed (step %d)", step_idx)

    @Slot(str)
    def _on_acq_progress(self, msg: str):
        """워커 진행 메시지를 상태줄에 표시 (메인 스레드 보장용 bound 슬롯)."""
        self._set_status(msg, color="#888")

    @Slot()
    def _on_acq_finished_slot(self):
        """워커 종료 처리 (메인 스레드 보장용 bound 슬롯)."""
        self._on_acq_finished(getattr(self, "_acq_is_sweep", False))

    @Slot(object)
    def _on_second_changed(self, sv):
        """second 채널 값이 바뀔 때 저장 하위폴더 토큰을 갱신 (해당 second의 스텝들이
        filename_xxx/<second폴더>/ 안에 저장되도록). 신호가 step_done보다 먼저 처리됨.

        센티넬: ("__initial__", None) → Feature 2 선행 sweep(현재온도) 전용 폴더,
                ("__dummy__", sv)     → Feature 3 dummy 복귀 측정 전용 폴더.
        """
        label = getattr(self, "_ds_second_label", "")
        # Feature 2/3 센티넬 처리 (튜플로 도착)
        if isinstance(sv, tuple) and len(sv) == 2:
            tag, real = sv
            if tag == "__initial__":
                self._current_second_dir = self._sanitize_name(
                    f"{label}_initial" if label else "initial")
            elif tag == "__dummy__":
                base = (f"{label}_{real:.6g}".replace('+', '')
                        if (label and real is not None) else "dummy")
                self._current_second_dir = self._sanitize_name(f"{base}_dummy")
            else:
                self._current_second_dir = None
            self._second_local_idx = 0
            return
        if sv is None or not label:
            self._current_second_dir = None
        else:
            tok = f"{label}_{sv:.6g}".replace('+', '')
            self._current_second_dir = self._sanitize_name(tok)
        self._second_local_idx = 0   # 새 폴더 → field-time 파일 인덱스 리셋

    @Slot(int, str)
    def _on_second_row(self, gidx: int, state: str):
        """워커가 알린 테이블 행 상태 변화(current/done)를 테이블 창에 반영 (메인 스레드)."""
        win = getattr(self, "_second_table_win", None)
        if win is None:
            return
        try:
            if state == "current":
                win.on_current(gidx)
            elif state == "done":
                win.on_done(gidx)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Second 값 테이블 창 (Feature 1)
    # ------------------------------------------------------------------
    def _open_second_table(self):
        """Second 값 테이블 편집 창을 연다(없으면 생성). 측정 중이 아니고 아직 테이블이
        비어 있으면 현재 Start/Stop/N으로 미리 채워 보여준다."""
        win = getattr(self, "_second_table_win", None)
        if win is None:
            from pythonization.ui.panels.second_channel_table_window import SecondChannelTableWindow
            win = SecondChannelTableWindow(self._second_table, self, parent=self)
            self._second_table_win = win
        # 측정 중이 아니고 비어 있으면 컨트롤 값으로 미리보기 시드
        busy = getattr(self, "_acq_thread", None) is not None
        if not busy and self._second_table.count() == 0:
            self._reset_second_table_from_controls()
        win.set_running(busy)
        win.refresh()
        win.show()
        win.raise_()
        win.activateWindow()

    def _reset_second_table_from_controls(self):
        """Start/Stop/N 입력값으로 second 테이블을 재생성한다(측정 중이 아닐 때만 의미)."""
        try:
            s0 = float(self._le_2_start.text())
            s1 = float(self._le_2_stop.text())
            sn = int(self._le_2_n.text())
            if sn < 1:
                raise ValueError
        except ValueError:
            self._set_status("Second 범위가 올바르지 않습니다.", color="#f78166")
            return
        self._second_table.reset_from(self._linspace(s0, s1, sn))
        win = getattr(self, "_second_table_win", None)
        if win is not None:
            win.refresh()

    @staticmethod
    def _sanitize_name(s: str) -> str:
        for ch in (' ', '/', '\\', '(', ')', ':', '*', '?', '"', '<', '>', '|'):
            s = s.replace(ch, '_')
        return s

    # ------------------------------------------------------------------
    # Resume (double sweep — second 채널 기준 재개)
    # ------------------------------------------------------------------

    def _resume_path(self) -> Path:
        return resume_path_for(self._config_path())

    def _save_resume(self):
        """현재 resume 상태를 프로파일 옆 파일에 저장 (크래시에도 남도록)."""
        st = getattr(self, "_resume_state", None)
        if st is not None:
            save_resume_state(st, self._resume_path())

    @Slot(int)
    def _on_second_done(self, global_si: int):
        """second 한 값의 full sweep 완료 → resume 상태의 완료 인덱스 갱신·저장.

        측정 중 테이블의 미래 행이 편집/추가됐을 수 있으므로, 라이브 모델의 값으로
        resume의 second_values를 다시 동기화한다(그래야 재개 시 최신 목록으로 이어감)."""
        st = getattr(self, "_resume_state", None)
        if st is not None:
            st.completed_second_idx = global_si
            try:
                live = self._second_table.values()
                if live:
                    st.second_values = [float(v) for v in live]
            except Exception:
                pass
            self._save_resume()
            get_logger().info("second %d done (resume saved)", global_si)

    def _on_resume_clicked(self):
        st = load_resume_state(self._resume_path())
        if not st.active or not st.second_values:
            self._set_status("재개할 측정이 없습니다.", color="#888")
            self._update_resume_button()
            return
        start_si = st.completed_second_idx + 1
        if start_si >= len(st.second_values):
            clear_resume_state(self._resume_path())
            self._set_status("이미 모든 second 값을 측정했습니다 — resume 종료.", color="#7ee787")
            self._update_resume_button()
            return
        remaining = st.second_values[start_si:]
        done = start_si
        total = len(st.second_values)
        ans = QMessageBox.question(
            self, "측정 재개",
            f"중단된 측정을 재개합니다.\n\n"
            f"완료: {done}/{total} second 값\n"
            f"재개 지점: {start_si+1}번째 (값 {remaining[0]:.6g})\n"
            f"저장 폴더: {st.sweep_folder}\n\n계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        # 저장된 제어 스냅샷을 UI/설정에 복원 → 같은 plan 구성
        self._cfg.ds_control = st.control
        self._apply_ds_control()
        self._resume_state = st            # 워커 second_done로 갱신될 객체
        self._set_status(f"재개: {start_si+1}/{total}번째 second부터…", color="#79c0ff")
        self._start_double_sweep(resume_offset=start_si,
                                 resume_second_vals=remaining,
                                 resume_folder=_P(st.sweep_folder),
                                 resume_full_vals=list(st.second_values))

    def _update_resume_button(self):
        """resume 파일 상태에 따라 Resume 버튼 활성/툴팁 갱신."""
        btn = getattr(self, "_btn_resume", None)
        if btn is None:
            return
        try:
            st = load_resume_state(self._resume_path())
        except Exception:
            st = VnaResumeState()
        can = bool(st.active and st.second_values
                   and st.completed_second_idx + 1 < len(st.second_values))
        running = bool(self._acq_thread and self._acq_thread.isRunning())
        btn.setEnabled(can and not running)
        if can:
            done = st.completed_second_idx + 1
            btn.setToolTip(f"중단된 double sweep 재개 "
                           f"({done}/{len(st.second_values)} 완료, "
                           f"{done+1}번째부터)")
        else:
            btn.setToolTip("재개할 중단된 측정이 없습니다.")

    @Slot(str)
    def _on_acq_warn(self, msg: str):
        """데이터는 다 모였지만 뒷정리(복귀)가 실패한 경우.

        `_acq_errored` 를 세우지 않는다 — 측정 자체는 정상 완료라 resume 상태를
        남기거나 '중단됨'으로 표시하면 안 된다. 다만 장비가 엉뚱한 값에 남아 있을 수
        있으므로 상태줄과 창으로 분명히 알린다.
        """
        get_logger().warning("VNA finish-return failed: %s", msg)
        self._set_status(f"⚠ {msg}", color="#d7ba7d")
        QMessageBox.warning(
            self, "복귀 실패 — 측정 데이터는 정상",
            f"측정은 정상적으로 끝났지만 축을 되돌리지 못했습니다.\n\n{msg}\n\n"
            "장비가 마지막 값에 그대로 있을 수 있으니 직접 확인하세요.")

    @Slot(str)
    def _on_acq_error(self, msg: str):
        self._acq_errored = True
        self._set_status(f"Acquire error: {msg}", color="#f78166")
        # 알람: 측정 오류 트리거 (meas_error)
        self._fire_alarm_meas_error(msg)
        QMessageBox.critical(self, "Acquire Error", msg)

    # ------------------------------------------------------------------
    # Alarm (VNA sweep — 텔레그램)
    # ------------------------------------------------------------------

    def _open_alarm_config(self):
        dlg = AlarmConfigWindow(self._cfg.alarm, self._alarm_manager, parent=self,
                                title="VNA Sweep — Alarm Config")
        dlg.apply_requested.connect(self._on_alarm_cfg_applied)
        dlg.exec()

    def _on_alarm_cfg_applied(self, cfg):
        self._cfg.alarm = cfg
        save_vna_config(self._cfg, self._config_path())

    def _fire_alarm_meas_error(self, detail: str):
        cfg = self._cfg.alarm
        if not cfg.enabled:
            return
        if self._alarm_manager.has_meas_error_trigger(cfg.triggers):
            self._alarm_manager.fire(cfg, f"VNA 측정 오류 — {detail}")

    def _fire_alarm_complete(self, n: int):
        cfg = self._cfg.alarm
        if not cfg.enabled:
            return
        if cfg.fire_on_complete:
            self._alarm_manager.fire(cfg, f"VNA sweep 완료 — {n} step(s)")

    def _on_acq_finished(self, is_sweep: bool):
        n = self._step_count
        self._set_status(
            f"{'Sweep ' if is_sweep else ''}Acquire done — {n} step(s).",
            color="#7ee787")
        self._set_acquire_busy(False)
        # Feature 1: CURRENT로 멈춘 행을 PENDING으로 되돌리고 테이블 갱신
        try:
            self._second_table.clear_running()
            win = getattr(self, "_second_table_win", None)
            if win is not None:
                win.refresh()
        except Exception:
            pass
        # Stop(또는 오류 중단) 시 등록된 명령 실행 (예: 자기장 HOLD로 ramp 정지).
        # acquire 워커가 완전히 종료된 뒤(여기) 보내므로 VISA 세션 경합이 없다.
        if self._acq_stopped or self._acq_errored:
            self._run_stop_cmds()
        # resume 상태 정리: 정상 완료면 제거, 중단(stop/오류)이면 유지(재개 가능)
        if getattr(self, "_resume_state", None) is not None:
            if not self._acq_errored and not self._acq_stopped:
                clear_resume_state(self._resume_path())
                self._resume_state = None
            else:
                self._set_status(
                    "측정 중단됨 — ‘▶ Resume’으로 마지막 second 값부터 재개할 수 있습니다.",
                    color="#d7ba7d")
        self._update_resume_button()
        # 알람: 정상 완료(오류·중단 아님)에만 완료 트리거
        if not self._acq_errored and not self._acq_stopped:
            self._fire_alarm_complete(n)

    def _build_stop_cmd_tuples(self) -> List[Tuple[str, str, bool]]:
        """등록된 stop_cmds → _VnaWorker용 (alias, cmd, is_query=False) 목록.
        템플릿을 못 찾거나 빌드 실패한 항목은 건너뛴다."""
        out: List[Tuple[str, str, bool]] = []
        for pc in getattr(self, "_stop_cmds", []):
            try:
                lib = self._lib_reg.get_library(pc.alias)
                template = None
                for e in list(lib.write_cmds) + list(lib.sweep_values):
                    if e.description == pc.description:
                        template = e.cmd_set
                        break
                if template is None:
                    continue
                out.append((pc.alias, build_cmd(template, pc.params, ""), False))
            except Exception:
                continue
        return out

    def _run_stop_cmds(self):
        """Stop 명령을 워커 스레드에서 전송 (UI 블로킹 방지)."""
        cmds = self._build_stop_cmd_tuples()
        if not cmds:
            return
        self._set_status(f"Stop 명령 전송 중… ({len(cmds)}개)", color="#f78166")
        worker = _VnaWorker(self._session, cmds)
        # bound 슬롯 연결 (워커 스레드 GUI 접근 크래시 방지)
        worker.done.connect(self._on_stopcmd_done)
        worker.error.connect(self._on_stopcmd_error)
        worker.finished.connect(worker.deleteLater)
        # 종료 시 wait할 수 있도록 추적 (덮어쓰지 않고 리스트로). 끝난 것은 정리.
        self._bg_workers = [w for w in self._bg_workers if _qthread_running(w)]
        self._bg_workers.append(worker)
        worker.start()

    @Slot(list)
    def _on_stopcmd_done(self, results: list):
        self._set_status(f"Stop 명령 전송 완료 ({len(results)}개).", color="#7ee787")

    @Slot(str)
    def _on_stopcmd_error(self, msg: str):
        self._set_status(f"Stop 명령 실패: {msg}", color="#f78166")

    def shutdown_threads(self, timeout_ms: int = 5000):
        """앱 종료 전 호출 — 실행 중인 워커 스레드를 안전하게 정지·대기한다.
        (running QThread가 파괴되면 std::terminate; VISA 세션이 먼저 닫히면 use-after-free.)
        MainWindow.closeEvent에서 session.shutdown() '이전에' 호출되어야 한다."""
        # 1) acquire 워커: stop 플래그 → 루프가 곧 빠져나옴 → quit/wait
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
        # 2) 섹션 실행 워커
        if _qthread_running(self._sec_worker):
            try:
                self._sec_worker.wait(timeout_ms)
            except RuntimeError:
                pass
        # 3) stop-cmd 등 백그라운드 _VnaWorker들
        for w in list(self._bg_workers):
            if _qthread_running(w):
                try:
                    w.wait(timeout_ms)
                except RuntimeError:
                    pass
        self._bg_workers = []

    def _set_acquire_busy(self, busy: bool):
        # sweep 중에는 충돌·설정변경을 막기 위해 동작 버튼을 모두 비활성화 (Stop만 활성)
        self._btn_single.setEnabled(not busy)
        self._btn_sweep.setEnabled(not busy)
        self._btn_stop_acq.setEnabled(busy)
        self._btn_cfg.setEnabled(not busy)
        self._btn_alarm.setEnabled(not busy)
        if getattr(self, "_btn_resume", None) is not None and busy:
            self._btn_resume.setEnabled(False)   # 해제는 _update_resume_button이 판단
        # 섹션 Execute(같은 VISA 세션으로 VNA 제어) — sweep 중 누르면 충돌하므로 비활성화
        for sw in self._section_widgets:
            sw.setEnabled(not busy)
        # Feature 1: 측정 중엔 테이블의 '재생성'만 잠근다(값 편집·행 추가/삭제는 유지).
        #            '커스텀 테이블 유지' 체크박스는 측정 중 변경 금지(잠금).
        win = getattr(self, "_second_table_win", None)
        if win is not None:
            win.set_running(busy)
        if hasattr(self, "_cb_second_keep_table"):
            self._cb_second_keep_table.setEnabled(not busy)

    def _on_stop_acquire(self):
        self._acq_stopped = True
        if self._acq_worker:
            self._acq_worker.stop()
        self._set_status("Stopping…", color="#888")

    # ------------------------------------------------------------------
    # Status helper
    # ------------------------------------------------------------------

    def _set_status(self, msg: str, color: str = "#888"):
        self._lbl_status.setStyleSheet(f"color: {color};")
        self._lbl_status.setText(msg)

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def _source_labels(self) -> List[str]:
        names = ["index"]
        for i, cmd in enumerate(c for c in self._cfg.acquire.read_cmds if c.enabled):
            label = cmd.figure_axis or cmd.description or f"arr_{i}"
            names.append(label)
        for label, _u in getattr(self, "_extra_cols", []):
            names.append(label)
        # section role(start/stop/n_points) linspace "time" 축
        if (self._roles_complete() or self._linspace_data is not None) \
                and "time" not in names:
            names.append("time")
        return names

    def _build_data_dict(self, arrays: List[np.ndarray]) -> dict:
        read_cmds = [c for c in self._cfg.acquire.read_cmds if c.enabled]
        extra = getattr(self, "_extra_cols", [])
        nr = len(read_cmds)
        max_len   = max((len(a) for a in arrays), default=0)
        data = {"index": np.arange(max_len)}
        for i, arr in enumerate(arrays):
            if i < nr:
                label = (read_cmds[i].figure_axis
                         or read_cmds[i].description
                         or f"arr_{i}")
            elif i - nr < len(extra):
                label = extra[i - nr][0]      # swept value 열 (first/second figure_axis)
            else:
                label = f"arr_{i}"
            data[label] = arr
        if self._linspace_data:
            data["time"] = self._linspace_data[2]
        return data

    def _update_plots(self, arrays: List[np.ndarray], first_step: bool = False):
        data = self._build_data_dict(arrays)
        self._panel_L.push_data(data, first_step=first_step)
        self._panel_R.push_data(data, first_step=first_step)

    def _restore_plot_config(self):
        sources = self._source_labels()
        self._panel_L.update_sources(sources)
        self._panel_R.update_sources(sources)

        curves = self._cfg.plot_curves
        if len(curves) >= 1:
            self._panel_L.restore_state(_plot_state(curves[0]))
        if len(curves) >= 2:
            self._panel_R.restore_state(_plot_state(curves[1]))

        # Sensible defaults when no saved config
        if len(sources) >= 2 and not curves:
            self._panel_L.set_default_x("index")
            self._panel_L.set_default_y(sources[1])
        if len(sources) >= 3 and len(curves) < 2:
            self._panel_R.set_default_x("index")
            self._panel_R.set_default_y(sources[2])
        elif len(sources) >= 2 and len(curves) < 2:
            self._panel_R.set_default_x("index")
            self._panel_R.set_default_y(sources[1])

    def _refresh_plot_sources(self):
        sources = self._source_labels()
        self._panel_L.update_sources(sources)
        self._panel_R.update_sources(sources)

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def _get_save_dir(self) -> Path:
        base = Path(self._le_main.text().strip() or ".")
        sub  = self._le_sub.text().strip()
        return base / sub if sub else base

    def _save_step(self, step_idx: int,
                   arrays: List[np.ndarray], is_sweep: bool):
        try:
            if is_sweep and self._sweep_folder is not None:
                # 저장 폴더: filename_xxx/ (+ second 값별 하위폴더)
                folder = self._sweep_folder
                if self._current_second_dir:
                    folder = folder / self._current_second_dir
                    folder.mkdir(parents=True, exist_ok=True)
                # 파일명: figure_axis_stepvalue.dat (second 값은 폴더가 담당)
                labels = getattr(self, "_ds_step_labels", None)
                if getattr(self, "_ds_field_time", False):
                    # 시간 기반: 스텝 수 미정 → 하위폴더별 인덱스(0000부터)
                    sv_str = f"{self._second_local_idx:04d}"
                    self._second_local_idx += 1
                elif labels:
                    sv_str = labels[step_idx] if step_idx < len(labels) else str(step_idx)
                else:
                    sv = self._current_sweep_values[step_idx]
                    sv_str = f"{sv:.6g}".replace('+', '')
                safe_fa = self._sanitize_name(self._sweep_figure_axis)
                fname = f"{safe_fa}_{sv_str}.dat"
                path  = folder / fname
                self._write_dat(path, arrays)
            else:
                # Single acquire: filename_x001.dat 넘버링
                filename = self._le_filename.text().strip() or "vna_data"
                base_dir = self._get_save_dir()
                path = next_dat_path(base_dir, filename)
                self._write_dat(path, arrays)
        except Exception as e:
            self._set_status(f"Save error: {e}", color="#f78166")

    def _write_dat(self, path: Path, arrays: List[np.ndarray]):
        if not arrays:
            return
        read_cmds  = [c for c in self._cfg.acquire.read_cmds if c.enabled]
        extra      = getattr(self, "_extra_cols", [])
        nr         = len(read_cmds)
        long_names = []
        units_row  = []
        all_arrays = list(arrays)   # copy — 원본 변경 방지

        for i in range(len(arrays)):
            if i < nr:
                fa = (read_cmds[i].figure_axis
                      or read_cmds[i].description
                      or f"col_{i}")
                u  = read_cmds[i].units
            elif i - nr < len(extra):
                fa, u = extra[i - nr]        # swept value 열 (label, unit)
            else:
                fa, u = f"col_{i}", ""
            long_names.append(fa)
            units_row.append(u)

        # section role linspace "time" 컬럼 (read_cmds + swept value 열 뒤에)
        if self._linspace_data:
            _, unit, arr = self._linspace_data
            long_names.append("time")
            units_row.append(unit)
            all_arrays.append(arr)

        max_len = max(len(a) for a in all_arrays)
        lines   = [
            "\t".join(long_names),
            "\t".join(units_row),
        ]
        for row_i in range(max_len):
            vals = [f"{arr[row_i]:.10E}" if row_i < len(arr) else ""
                    for arr in all_arrays]
            lines.append("\t".join(vals))
        path.write_text("\n".join(lines), encoding="utf-8")

    # ------------------------------------------------------------------
    # Config reload
    # ------------------------------------------------------------------

    def _open_config(self):
        # Config 창이 읽어 갈 파일에 이 창의 현재 상태를 먼저 반영한다.
        # Config 는 통째로 저장(Save & Apply)하므로, 여기 체크박스·입력값·저장
        # 폴더가 파일에 없으면 그 값들이 옛것으로 되돌아간다.
        self._save_ui_state()
        cfg_path = self._config_path()
        if self._config_win is None:
            self._config_win = VnaConfigWindow(
                self._lib_reg, config_path=cfg_path, parent=self)
            self._config_win.saved.connect(self._on_config_saved)
        else:
            # 경로 갱신 겸 재로드. 이미 떠 있어서 showEvent 가 안 오는 경우도 덮는다.
            self._config_win.set_config_path(cfg_path)
        self._config_win.show()
        self._config_win.raise_()

    def _on_config_saved(self):
        self._cfg = load_vna_config(self._config_path())
        self._le_filename.setText(self._cfg.acquire.filename)
        self._le_sw_start.setText(str(self._cfg.acquire.sweep_start))
        self._le_sw_stop.setText(str(self._cfg.acquire.sweep_stop))
        self._le_sw_n.setText(str(self._cfg.acquire.sweep_n))
        self._rebuild_sections()
        self._populate_sweep_cmds()
        self._refresh_plot_sources()
        self._set_status("Config reloaded.", color="#7ee787")

    # ------------------------------------------------------------------
    # File ops
    # ------------------------------------------------------------------

    def _on_browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Main Folder")
        if folder:
            self._le_main.setText(folder)

    def _on_open_folder(self):
        path = self._get_save_dir()
        if path.exists():
            os.startfile(str(path))
        else:
            self._set_status("Folder does not exist.", color="#888")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_ui_state(self):
        # 섹션 UI 상태(체크박스, 입력값, 단위) → _cfg에 반영
        # section_widget 수와 _cfg.sections 수가 불일치하면 유령 데이터 발생 가능 —
        # widget 기준으로만 flush하여 매칭 없는 항목은 자동 제거됨.
        if self._section_widgets:
            self._cfg.sections = [sw.get_updated_config()
                                  for sw in self._section_widgets]

        self._cfg.main_folder  = self._le_main.text().strip()
        self._cfg.sub_folder   = self._le_sub.text().strip()
        self._cfg.save_enabled = self._cb_save.isChecked()
        self._cfg.acquire.filename = self._le_filename.text().strip()
        try:
            self._cfg.acquire.sweep_start = float(self._le_sw_start.text())
            self._cfg.acquire.sweep_stop  = float(self._le_sw_stop.text())
            self._cfg.acquire.sweep_n     = int(self._le_sw_n.text())
        except ValueError:
            pass
        # 시간 기반 sweep 파라미터 영속화 (Sweep 콤보의 '⏱ Time' 선택 여부)
        self._cfg.acquire.time_mode = self._is_time_sweep_selected()
        try:
            self._cfg.acquire.time_interval = float(self._le_time_interval.text())
            self._cfg.acquire.time_count    = int(self._le_time_count.text())
        except ValueError:
            pass
        self._cfg.plot_curves = [
            _plot_config(self._panel_L),
            _plot_config(self._panel_R),
        ]
        if hasattr(self, "_combo_direction"):
            self._cfg.ds_control = self._collect_ds_control()
        save_vna_config(self._cfg, self._config_path())

    def _clear_graph_data(self):
        """그래프/데이터 초기화 (window 종료/숨김 시)."""
        self._step_count = 0
        self._panel_L.clear_data()
        self._panel_R.clear_data()

    def _save_and_hide(self):
        """저장 + 그래프 초기화 + 숨김 (닫기/Escape 공통)."""
        self._save_ui_state()
        self._clear_graph_data()
        self.hide()

    def closeEvent(self, event):
        event.ignore()
        self._save_and_hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            event.accept()           # VNA Control은 Esc로 닫지 않음 (실수로 닫힘 방지)
            return
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_S):
            self._save_ui_state()    # Ctrl+S = 설정 저장(닫지 않음)
            event.accept()
            return
        super().keyPressEvent(event)
