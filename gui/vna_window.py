"""
VnaWindow: VNA 제어 창 (병렬 동작).

좌측: 섹션 Execute 패널 + Acquire 패널
우측: 두 개의 플롯 (좌우, 각각 x/y source 선택 가능, multi-y 지원)
"""
import os
import time as _time
from pathlib import Path
from typing import List, Optional, Tuple, TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QScrollArea,
    QSplitter, QVBoxLayout, QWidget,
)

from core.app_dirs import SETTINGS_DIR
from gui.vna_models import (
    VnaAcquireConfig, VnaCommandEntry, VnaConfigData,
    VnaPlotCurveConfig, VnaSectionConfig,
    VnaDoubleSweepControl, VnaPreCmdValue, VnaFieldTimeConfig, VnaPreAdvanceCmd,
    VnaResumeState, build_cmd, format_label, get_figure_axis, get_template,
    load_vna_config, next_dat_path, next_sweep_folder,
    parse_vna_array, save_vna_config,
    resume_path_for, load_resume_state, save_resume_state, clear_resume_state,
)

if TYPE_CHECKING:
    from core.instrument_session import InstrumentSession
    from core.visa_library_registry import VisaLibraryRegistry
    from core.profile_registry import ProfileRegistry

_MONO   = QFont("Consolas", 9)
_COLORS = ["#4ec9b0", "#ce9178", "#79c0ff", "#d2a8ff",
           "#ffa657", "#7ee787", "#f78166", "#a5d6ff"]
_DEFAULT_CONFIG_PATH = SETTINGS_DIR / "vna_config.yaml"

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
    opc_started = Signal()      # 묶음 명령을 다 보내고 완료 대기에 들어감

    #: OPC 폴링 간격(초) / 최대 대기(초). 장비가 영영 1 을 안 주면 창이 잠긴 채로
    #: 남으므로 상한을 둔다.
    _OPC_POLL_S = 0.1
    _OPC_TIMEOUT_S = 600.0

    def __init__(self, session, commands: List[Tuple[str, str, bool]],
                 opc_commands: Optional[List[Tuple[str, str]]] = None):
        super().__init__()
        self._session  = session
        self._commands = commands
        self._opc      = opc_commands or []
        self._stop     = False

    def stop(self):
        """OPC 대기를 중단시킨다 (창을 닫거나 사용자가 멈출 때)."""
        self._stop = True

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
            if self._opc:
                self.opc_started.emit()
                self._wait_opc()
            self.done.emit(results)
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")

    def _wait_opc(self):
        """OPC 쿼리 응답이 1 이 될 때까지 폴링한다 (acquire 의 Wait 단계와 같은 규칙)."""
        for alias, cmd in self._opc:
            if not self._session.is_open(alias):
                self._session.open(alias)
            deadline = _time.monotonic() + self._OPC_TIMEOUT_S
            while not self._stop:
                response = str(self._session.query(alias, cmd)).strip().lstrip("+")
                if response == "1":
                    break
                if _time.monotonic() > deadline:
                    raise TimeoutError(
                        f"[{alias}] OPC 응답을 {int(self._OPC_TIMEOUT_S)}초 동안 받지 "
                        f"못했습니다 (마지막 응답: {response!r}). 명령: {cmd}")
                _time.sleep(self._OPC_POLL_S)


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

    def stop(self):
        self._stop_flag = True
        if self._sec_stop is not None:
            self._sec_stop.set()

    @Slot()
    def run(self):
        from core.applog import get_logger
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
        uni = (p["direction"] == "uni")
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
                self.progress.emit(f"{ctx}Second 채널 이동 중 → {sv:.6g}")
                self._advance(p["second_cmd"], p["second_adv"], sv, second_prev)
                second_prev = sv

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
            if uni and not self._stop_flag:
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
                self.progress.emit(f"{ctx}② First {k}/{nf} 이동 중 → {fv:.6g}")
                try:
                    self._advance(p["first_cmd"], p["first_adv"], fv, first_pos)
                    first_pos = fv
                    self.progress.emit(f"{ctx}③ First {k}/{nf} 측정 중 (val={fv:.6g})")
                    extra = [fv] + ([sv] if (p["second_enabled"] and sv is not None) else [])
                    self._acquire_once(idx, first_step=(idx == 0), extra_values=extra)
                except Exception as e:
                    self.error.emit(f"Step {idx+1}: {type(e).__name__}: {e}")
                    return
                idx += 1
            self._finish_second(use_tbl, tbl, gidx)
            si += 1

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
            from core.second_channel_worker import SecondChannelWorker
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
        from config.config_models import SecondSweepAdvanceType as A
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
        from config.config_models import SecondSweepAdvanceType as A
        if adv.advance_type in (A.FEEDBACK, A.THRESHOLD_TIME):
            read_cmd = (adv.feedback_read_cmd or "").strip()
        elif adv.advance_type == A.SWEEP:
            read_cmd = (adv.paired_read_cmd or "").strip()
        else:
            read_cmd = ""
        if not read_cmd:
            return None
        try:
            from core.instrument_parameter import _parse_float
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
        from config.config_models import InstantiatedSecondSweepChannel
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
    execute_requested     = Signal(list, list)   # (write 명령, OPC 명령)
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
        self.execute_requested.emit([c for c in cmds if c], self._resolve_opc())

    def _resolve_opc(self) -> list:
        """섹션의 OPC 쿼리를 (alias, cmd) 로 해석한다.

        OPC 는 Control 화면에 행으로 뜨지 않고 Config 에만 있으므로 여기서 직접
        템플릿을 채운다. 해석에 실패한 항목은 건너뛰고 실행을 막지는 않는다.
        """
        out = []
        for entry in getattr(self._sec_cfg, "opc_cmds", []) or []:
            if not entry.enabled:
                continue
            try:
                lib = self._lib_reg.get_library(entry.alias)
                template = get_template(lib, entry)
                if template is None:
                    continue
                out.append((entry.alias, build_cmd(template, entry.params, "")))
            except Exception:
                continue
        return out

    def get_updated_config(self) -> VnaSectionConfig:
        """현재 UI 상태를 반영한 VnaSectionConfig 반환 (저장용)."""
        updated_cmds = [row.get_entry_state() for row in self._cmd_rows]
        return self._sec_cfg.model_copy(update={"commands": updated_cmds})


# ---------------------------------------------------------------------------
# Y-curve row (one y-axis selector + curve per row in _PlotPanel)
# ---------------------------------------------------------------------------

class _YCurveRow(QWidget):
    remove_requested = Signal(object)   # emits self
    source_changed   = Signal()

    def __init__(self, color: str, sources: List[str], y_src: str,
                 plot_widget: pg.PlotWidget, parent=None):
        super().__init__(parent)
        self._color = color
        self._plot  = plot_widget

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        # color swatch
        swatch = QLabel("●")
        swatch.setStyleSheet(f"color: {color}; font-size: 14px;")
        swatch.setFixedWidth(18)
        lay.addWidget(swatch)

        # y combo
        self._cb_y = QComboBox()
        self._cb_y.setFont(_MONO)
        for s in sources:
            self._cb_y.addItem(s, s)
        if y_src:
            idx = self._cb_y.findData(y_src)
            if idx >= 0:
                self._cb_y.setCurrentIndex(idx)
        self._cb_y.currentIndexChanged.connect(self.source_changed.emit)
        lay.addWidget(self._cb_y, stretch=1)

        # remove button
        btn_rm = QPushButton("✕")
        btn_rm.setFixedSize(20, 20)
        btn_rm.clicked.connect(lambda: self.remove_requested.emit(self))
        lay.addWidget(btn_rm)

        # pyqtgraph curve
        self._curve = plot_widget.plot(pen=pg.mkPen(color, width=1.5))
        self._curve.setDownsampling(auto=True, method='peak')
        self._curve.setClipToView(True)

    def y_source(self) -> str:
        return self._cb_y.currentData() or ""

    def update_sources(self, sources: List[str]):
        prev = self._cb_y.currentData()
        self._cb_y.blockSignals(True)
        self._cb_y.clear()
        for s in sources:
            self._cb_y.addItem(s, s)
        idx = self._cb_y.findData(prev)
        if idx >= 0:
            self._cb_y.setCurrentIndex(idx)
        self._cb_y.blockSignals(False)

    def set_data(self, x: np.ndarray, y: np.ndarray):
        # NaN/Inf가 섞인 데이터를 skipFiniteCheck로 그리면 pyqtgraph 렌더 레이어에서
        # 네이티브 크래시가 날 수 있다. float 배열로 정규화 + Inf→NaN(=gap) 치환 후,
        # 유한성 검사를 켠 채로(skipFiniteCheck 미사용) 안전하게 그린다.
        try:
            xa = np.asarray(x, dtype=float).ravel()
            ya = np.asarray(y, dtype=float).ravel()
            mn = min(xa.size, ya.size)
            if mn > 0:
                xa, ya = xa[:mn], ya[:mn]
                if not np.isfinite(xa).all():
                    xa = np.where(np.isfinite(xa), xa, np.nan)
                if not np.isfinite(ya).all():
                    ya = np.where(np.isfinite(ya), ya, np.nan)
                self._curve.setData(xa, ya)
            else:
                self._curve.setData([], [])
        except Exception:
            try:
                self._curve.setData([], [])
            except Exception:
                pass

    def clear_data(self):
        self._curve.setData([], [])

    def remove_from_plot(self):
        self._plot.removeItem(self._curve)


# ---------------------------------------------------------------------------
# Plot panel  (x-selector + multi-y rows + PlotWidget, 1:1 aspect hint)
# ---------------------------------------------------------------------------

class _PlotPanel(QFrame):
    """독립적인 x source 선택 + multi-y curve + pyqtgraph PlotWidget."""

    def __init__(self, start_color_idx: int = 0, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._data: dict = {}
        self._sources: List[str] = []
        self._y_rows: List[_YCurveRow] = []
        self._color_idx = start_color_idx
        self._build()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return w

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)

        # x selector + Add Y button
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(4)
        ctrl_row.addWidget(QLabel("x:"))
        self._cb_x = QComboBox()
        self._cb_x.setFont(_MONO)
        self._cb_x.setMinimumWidth(80)
        self._cb_x.currentIndexChanged.connect(self._replot_all)
        ctrl_row.addWidget(self._cb_x, stretch=1)

        btn_add = QPushButton("＋Y")
        btn_add.setFixedHeight(20)
        btn_add.setFixedWidth(36)
        btn_add.setToolTip("y 곡선 추가")
        btn_add.clicked.connect(lambda: self._add_y_row())
        ctrl_row.addWidget(btn_add)

        btn_fit = QPushButton("Fit")
        btn_fit.setFixedHeight(20)
        btn_fit.setFixedWidth(36)
        btn_fit.setToolTip("현재 표시된 데이터에 맞춰 X·Y 범위를 한 번 자동 맞춤\n"
                           "(sweep 중 신호가 화면 밖으로 나갔을 때 사용)")
        btn_fit.clicked.connect(self.auto_range)
        ctrl_row.addWidget(btn_fit)
        lay.addLayout(ctrl_row)

        # y rows container
        self._y_container = QWidget()
        self._y_lay = QVBoxLayout(self._y_container)
        self._y_lay.setContentsMargins(0, 0, 0, 0)
        self._y_lay.setSpacing(2)
        lay.addWidget(self._y_container)

        # plot
        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        lay.addWidget(self._plot, stretch=1)

        # start with one y row (after plot is created)
        self._add_y_row()

    def _next_color(self) -> str:
        c = _COLORS[self._color_idx % len(_COLORS)]
        self._color_idx += 1
        return c

    def _add_y_row(self, y_src: str = ""):
        color = self._next_color()
        row = _YCurveRow(color, self._sources, y_src, self._plot, self._y_container)
        row.remove_requested.connect(self._remove_y_row)
        row.source_changed.connect(self._replot_all)
        self._y_rows.append(row)
        self._y_lay.addWidget(row)
        self._replot_all()

    def _remove_y_row(self, row: _YCurveRow):
        if len(self._y_rows) <= 1:
            return   # keep at least one
        self._y_rows.remove(row)
        row.remove_from_plot()
        row.setParent(None)
        row.deleteLater()
        self._replot_all()

    # ------------------------------------------------------------------
    def update_sources(self, sources: List[str]):
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

    def set_default_x(self, x_src: str):
        ix = self._cb_x.findData(x_src)
        if ix >= 0:
            self._cb_x.setCurrentIndex(ix)

    def set_default_y(self, y_src: str):
        """첫 번째 y row의 y source 설정."""
        if self._y_rows:
            idx = self._y_rows[0]._cb_y.findData(y_src)
            if idx >= 0:
                self._y_rows[0]._cb_y.setCurrentIndex(idx)

    def push_data(self, data: dict, first_step: bool = False):
        self._data = data
        if first_step:
            # 뷰 범위를 먼저 설정 → setData 시 ClipToView가 올바른 범위로 클리핑함
            self._fit_view()
        self._replot_all()

    def auto_range(self):
        self._fit_view()

    @staticmethod
    def _finite(arr) -> np.ndarray:
        a = np.asarray(arr, dtype=float).ravel()
        return a[np.isfinite(a)] if a.size else a

    def _fit_view(self):
        """raw self._data에서 직접 min/max 계산 → x, y 독립적으로 뷰 범위 설정.
        enableAutoRange()는 ClipToView와 충돌(닭-달걀)하므로 사용하지 않음.
        비유한값(NaN/Inf)은 제외하고 계산해 setXRange/setYRange 크래시를 막는다."""
        try:
            x_src = self._cb_x.currentData() or ""
            xd = self._finite(self._data.get(x_src, np.array([])))

            all_y: List[np.ndarray] = []
            for row in self._y_rows:
                yd = self._finite(self._data.get(row.y_source(), np.array([])))
                if yd.size:
                    all_y.append(yd)

            if xd.size:
                xmin, xmax = float(xd.min()), float(xd.max())
                if xmin != xmax and np.isfinite(xmin) and np.isfinite(xmax):
                    self._plot.setXRange(xmin, xmax, padding=0.05)

            if all_y:
                yd_cat = np.concatenate(all_y)
                if yd_cat.size:
                    ymin, ymax = float(yd_cat.min()), float(yd_cat.max())
                    if ymin != ymax and np.isfinite(ymin) and np.isfinite(ymax):
                        self._plot.setYRange(ymin, ymax, padding=0.05)
        except Exception:
            pass

    def clear_data(self):
        self._data = {}
        for row in self._y_rows:
            row.clear_data()
        self._plot.setLabel("bottom", "")
        self._plot.setLabel("left", "")

    def _replot_all(self):
        x_src = self._cb_x.currentData() or ""
        xd = self._data.get(x_src, np.array([]))
        y_labels = []
        for row in self._y_rows:
            y_src = row.y_source()
            yd = self._data.get(y_src, np.array([]))
            row.set_data(xd, yd)
            if y_src:
                y_labels.append(y_src)
        self._plot.setLabel("bottom", x_src)
        if y_labels:
            self._plot.setLabel("left", y_labels[0])

    def x_source(self) -> str:
        return self._cb_x.currentData() or "index"

    def y_sources(self) -> List[str]:
        return [row.y_source() for row in self._y_rows]

    def to_config(self) -> VnaPlotCurveConfig:
        srcs = self.y_sources()
        return VnaPlotCurveConfig(
            x_source=self.x_source(),
            y_source=srcs[0] if srcs else "",
            y_sources=srcs,
        )

    def restore_config(self, cfg: VnaPlotCurveConfig):
        ix = self._cb_x.findData(cfg.x_source)
        if ix >= 0:
            self._cb_x.blockSignals(True)
            self._cb_x.setCurrentIndex(ix)
            self._cb_x.blockSignals(False)

        y_srcs = cfg.y_sources if cfg.y_sources else (
            [cfg.y_source] if cfg.y_source else [])
        if not y_srcs:
            self._replot_all()
            return

        # Remove excess rows (keep at least 1)
        while len(self._y_rows) > 1:
            row = self._y_rows.pop()
            row.remove_from_plot()
            row.setParent(None)
            row.deleteLater()

        # Set first row
        if self._y_rows:
            row0 = self._y_rows[0]
            row0.update_sources(self._sources)
            idx0 = row0._cb_y.findData(y_srcs[0])
            if idx0 >= 0:
                row0._cb_y.blockSignals(True)
                row0._cb_y.setCurrentIndex(idx0)
                row0._cb_y.blockSignals(False)

        # Add additional rows
        for src in y_srcs[1:]:
            self._add_y_row(src)

        self._replot_all()


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
        self.resize(1500, 780)   # 최대화를 푼 뒤의 복원 크기
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        # 처음 열 때는 최대화 상태로 — 섹션·그래프·데이터가 한 화면에 들어와야 한다.
        # show() 가 이 상태를 그대로 따르므로 여기서 지정하면 된다.
        # (setWindowFlags 가 상태를 초기화할 수 있어 반드시 그 뒤에 둔다.)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
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
        self._cal_win = None          # Calibration 창 (보관형)
        self._cal_callback = None     # 실행 완료를 알릴 콜백
        self._resume_state: Optional[VnaResumeState] = None   # double sweep 재개 상태
        self._bg_workers: list = []   # 백그라운드 _VnaWorker(stop-cmd 등) 추적 (종료 시 wait)
        from gui.second_channel_model import SecondChannelModel
        self._second_table = SecondChannelModel()   # Feature 1: second 값 테이블 (GUI 소유)
        self._second_table_win = None
        # Section role management  (start / stop / n_points)
        self._role_rows: dict = {"start": None, "stop": None, "n_points": None}
        self._linspace_data: Optional[Tuple[str, str, np.ndarray]] = None
        # 알람 (VNA sweep 전용 — 텔레그램)
        from core.alarm_manager import AlarmManager
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

        self._btn_cal = QPushButton("⚙ Calibration")
        self._btn_cal.setFixedHeight(22)
        self._btn_cal.setToolTip(
            "교정 명령을 버튼으로 모아 둔 창. 버튼을 누르면 명령을 보내고 "
            "OPC 응답이 올 때까지 기다린다. 설정은 프로파일과 무관한 전역 설정.")
        self._btn_cal.clicked.connect(self._open_calibration)
        lay.addWidget(self._btn_cal)

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

        from gui.help_button import make_help_button
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
        grp = QGroupBox("Acquire")
        lay = QVBoxLayout(grp)
        lay.setSpacing(4)
        lay.setContentsMargins(8, 10, 8, 8)

        # Filename
        fn_row = QHBoxLayout()
        fn_row.addWidget(QLabel("Filename:"))
        self._le_filename = QLineEdit(self._cfg.acquire.filename)
        self._le_filename.setFont(_MONO)
        self._le_filename.setPlaceholderText("vna_data")
        fn_row.addWidget(self._le_filename, stretch=1)
        lay.addLayout(fn_row)

        # Single acquire button
        self._btn_single = QPushButton("▶ Single Acquire")
        self._btn_single.setFixedHeight(26)
        self._btn_single.setStyleSheet(
            "QPushButton{background:#2d5a1b;color:white;"
            "border-radius:3px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        self._btn_single.clicked.connect(self._on_single_acquire)
        lay.addWidget(self._btn_single)

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet("color: #30363d;")
        lay.addWidget(div)

        # Sweep command selector (first sweep channel)
        sw_cmd_row = QHBoxLayout()
        sw_cmd_row.addWidget(QLabel("First sweep ch:"))
        self._combo_sweep_cmd = QComboBox()
        self._combo_sweep_cmd.setFont(_MONO)
        self._combo_sweep_cmd.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._combo_sweep_cmd.currentIndexChanged.connect(
            self._on_sweep_cmd_changed)
        sw_cmd_row.addWidget(self._combo_sweep_cmd, stretch=1)
        lay.addLayout(sw_cmd_row)

        # Sweep params (Start / Stop / N) with optional unit combos
        sw_row = QHBoxLayout()
        sw_row.setSpacing(4)

        sw_row.addWidget(QLabel("Start:"))
        self._le_sw_start = QLineEdit(str(self._cfg.acquire.sweep_start))
        self._le_sw_start.setFont(_MONO)
        self._le_sw_start.setFixedWidth(60)
        sw_row.addWidget(self._le_sw_start)
        self._cb_sw_start_unit = QComboBox()
        self._cb_sw_start_unit.setFont(_MONO)
        self._cb_sw_start_unit.setFixedWidth(52)
        self._cb_sw_start_unit.setVisible(False)
        sw_row.addWidget(self._cb_sw_start_unit)

        sw_row.addWidget(QLabel("Stop:"))
        self._le_sw_stop = QLineEdit(str(self._cfg.acquire.sweep_stop))
        self._le_sw_stop.setFont(_MONO)
        self._le_sw_stop.setFixedWidth(60)
        sw_row.addWidget(self._le_sw_stop)
        self._cb_sw_stop_unit = QComboBox()
        self._cb_sw_stop_unit.setFont(_MONO)
        self._cb_sw_stop_unit.setFixedWidth(52)
        self._cb_sw_stop_unit.setVisible(False)
        sw_row.addWidget(self._cb_sw_stop_unit)

        sw_row.addWidget(QLabel("N:"))
        self._le_sw_n = QLineEdit(str(self._cfg.acquire.sweep_n))
        self._le_sw_n.setFont(_MONO)
        self._le_sw_n.setFixedWidth(44)
        sw_row.addWidget(self._le_sw_n)
        sw_row.addStretch()
        # Start/Stop/N 입력은 파라미터 sweep 선택 시에만 표시 (Time 선택 시 숨김)
        self._sweep_param_widget = QWidget()
        self._sweep_param_widget.setLayout(sw_row)
        lay.addWidget(self._sweep_param_widget)

        # 시간 기반 sweep: Sweep 콤보에서 '⏱ Time' 선택 시 사용
        #   → Interval(초)마다 VNA acquire를 1회씩 Count번 반복
        time_row = QHBoxLayout()
        time_row.setSpacing(4)
        time_row.addWidget(QLabel("Interval:"))
        self._le_time_interval = QLineEdit(str(self._cfg.acquire.time_interval))
        self._le_time_interval.setFont(_MONO)
        self._le_time_interval.setFixedWidth(56)
        time_row.addWidget(self._le_time_interval)
        time_row.addWidget(QLabel("s"))
        time_row.addSpacing(8)
        time_row.addWidget(QLabel("Count:"))
        self._le_time_count = QLineEdit(str(self._cfg.acquire.time_count))
        self._le_time_count.setFont(_MONO)
        self._le_time_count.setFixedWidth(44)
        time_row.addWidget(self._le_time_count)
        time_row.addStretch()
        self._time_row_widget = QWidget()
        self._time_row_widget.setLayout(time_row)
        lay.addWidget(self._time_row_widget)

        # Idle / Overrun 표시 (시간 모드 전용)
        self._lbl_idle = QLabel("Idle: —")
        self._lbl_idle.setFont(_MONO)
        self._lbl_idle.setStyleSheet("color: #555;")
        lay.addWidget(self._lbl_idle)
        # 시간 행/파라미터 행의 초기 표시 여부는 _populate_sweep_cmds() →
        # _on_sweep_cmd_changed()에서 콤보 선택에 따라 결정한다.

        # Double Sweep 섹션 (방향 / second channel / pre-advance 값 / 시간 예상)
        self._ds_section = self._build_double_sweep_section()
        lay.addWidget(self._ds_section)

        sweep_row = QHBoxLayout()
        self._btn_sweep = QPushButton("▶ Sweep Acquire")
        self._btn_sweep.setFixedHeight(26)
        self._btn_sweep.setStyleSheet(
            "QPushButton{background:#4b2d5a;color:white;"
            "border-radius:3px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        self._btn_sweep.clicked.connect(self._on_sweep_acquire)
        sweep_row.addWidget(self._btn_sweep)

        self._btn_stop_acq = QPushButton("■ Stop")
        self._btn_stop_acq.setFixedHeight(26)
        self._btn_stop_acq.setMinimumWidth(90)
        self._btn_stop_acq.setEnabled(False)
        self._btn_stop_acq.clicked.connect(self._on_stop_acquire)
        sweep_row.addWidget(self._btn_stop_acq, stretch=1)
        sweep_row.addStretch(1)
        lay.addLayout(sweep_row)

        # Resume (중단된 double sweep을 마지막 second 값부터 재개)
        self._btn_resume = QPushButton("▶ Resume (중단된 측정 재개)")
        self._btn_resume.setFixedHeight(24)
        self._btn_resume.setStyleSheet(
            "QPushButton{background:#2d4b2d;color:#7ee787;"
            "border-radius:3px;font-weight:bold;}"
            "QPushButton:disabled{background:#222;color:#555;}"
        )
        self._btn_resume.setEnabled(False)
        self._btn_resume.clicked.connect(self._on_resume_clicked)
        lay.addWidget(self._btn_resume)

        # Populate sweep command combo
        self._populate_sweep_cmds()
        self._update_resume_button()
        return grp

    # ---- Double Sweep section ----------------------------------------

    def _build_double_sweep_section(self) -> QWidget:
        sec = QFrame()
        sec.setObjectName("dsSec")
        sec.setStyleSheet(
            "#dsSec{border:1px solid #30363d;border-radius:4px;background:#0f1117;}")
        v = QVBoxLayout(sec)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(5)

        from gui.help_button import make_help_button
        title_row = QHBoxLayout(); title_row.setSpacing(6)
        title = QLabel("◆ Double Sweep")
        title.setStyleSheet("color:#c586c0; font-weight:bold;")
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(
            make_help_button(self._double_sweep_help_html(), "Double Sweep 도움말"))
        v.addLayout(title_row)

        # Second sweep channel (가장 위 — bias loop을 먼저 정한다)
        self._cb_second_enable = QCheckBox("Second sweep channel 사용")
        self._cb_second_enable.toggled.connect(self._on_second_enable_toggled)
        v.addWidget(self._cb_second_enable)

        self._second_widget = QWidget()
        sw2 = QVBoxLayout(self._second_widget)
        sw2.setContentsMargins(14, 0, 0, 0); sw2.setSpacing(4)
        c2 = QHBoxLayout(); c2.setSpacing(6)
        c2.addWidget(QLabel("ch:"))
        self._combo_second_cmd = QComboBox(); self._combo_second_cmd.setFont(_MONO)
        c2.addWidget(self._combo_second_cmd, 1)
        sw2.addLayout(c2)
        r2 = QHBoxLayout(); r2.setSpacing(4)
        r2.addWidget(QLabel("Start:")); self._le_2_start = QLineEdit("0")
        self._le_2_start.setFixedWidth(54); self._le_2_start.setFont(_MONO); r2.addWidget(self._le_2_start)
        r2.addWidget(QLabel("Stop:"));  self._le_2_stop = QLineEdit("1")
        self._le_2_stop.setFixedWidth(54); self._le_2_stop.setFont(_MONO); r2.addWidget(self._le_2_stop)
        r2.addWidget(QLabel("N:"));     self._le_2_n = QLineEdit("10")
        self._le_2_n.setFixedWidth(40); self._le_2_n.setFont(_MONO); r2.addWidget(self._le_2_n)
        r2.addStretch()
        for le in (self._le_2_start, self._le_2_stop, self._le_2_n):
            le.textChanged.connect(self._update_time_estimate)
        sw2.addLayout(r2)

        # Feature 1: second 값 테이블 편집 창 + '커스텀 테이블 유지' 옵션
        tbl_row = QHBoxLayout(); tbl_row.setSpacing(6)
        self._btn_second_table = QPushButton("Second 값 테이블…")
        self._btn_second_table.setToolTip(
            "Second 채널 값 배열을 표로 편집하는 창을 엽니다.\n"
            "측정 중에도 아직 측정 안 한(대기) 행은 값 수정·추가·삭제할 수 있습니다.")
        self._btn_second_table.clicked.connect(self._open_second_table)
        tbl_row.addWidget(self._btn_second_table)
        tbl_row.addStretch()
        sw2.addLayout(tbl_row)
        self._cb_second_keep_table = QCheckBox("테이블 초기화 안 함 (커스텀 테이블 그대로 측정)")
        self._cb_second_keep_table.setToolTip(
            "체크 시 시작할 때 Start/Stop/N으로 테이블을 새로 만들지 않고,\n"
            "테이블 창에서 직접 넣은 값 목록 그대로 측정합니다.")
        sw2.addWidget(self._cb_second_keep_table)

        self._second_widget.setVisible(False)
        v.addWidget(self._second_widget)

        # First sweep 방향
        dir_row = QHBoxLayout(); dir_row.setSpacing(6)
        dir_row.addWidget(QLabel("First 방향:"))
        self._combo_direction = QComboBox()
        self._combo_direction.addItem("단방향 (시작→끝 고정, dummy 복귀)", "uni")
        self._combo_direction.addItem("다중방향 (스텝마다 방향 교대)", "multi")
        self._combo_direction.currentIndexChanged.connect(self._refresh_ds_dynamic)
        dir_row.addWidget(self._combo_direction, 1)
        v.addLayout(dir_row)

        # Pre-advance 값 (first=controlled + 단방향 + pre_cmds 있을 때만)
        self._pre_adv_widget = QWidget()
        self._pre_adv_lay = QVBoxLayout(self._pre_adv_widget)
        self._pre_adv_lay.setContentsMargins(0, 0, 0, 0); self._pre_adv_lay.setSpacing(3)
        self._pre_adv_rows: list = []
        self._pre_adv_widget.setVisible(False)
        v.addWidget(self._pre_adv_widget)

        # ── Double Sweep with Time (First 채널 시간 기반 측정) ──
        self._cb_field_time = QCheckBox("Double Sweep with Time (First=자기장 시간측정)")
        self._cb_field_time.setToolTip(
            "First 채널을 N단계가 아니라 '시간 기반'으로 측정합니다.\n"
            "목표로 ramp 시작 → acquire 간격마다 측정 → 상태가 HOLD면 완료.\n"
            "단방향이면 완료 후 controlled로 시작점 복귀·안정화.")
        self._cb_field_time.toggled.connect(self._on_field_time_toggled)
        v.addWidget(self._cb_field_time)

        self._ft_widget = QWidget()
        ftl = QVBoxLayout(self._ft_widget)
        ftl.setContentsMargins(14, 0, 0, 0); ftl.setSpacing(4)
        row_i = QHBoxLayout(); row_i.setSpacing(6)
        row_i.addWidget(QLabel("acquire 간격(s):"))
        self._ft_interval = QLineEdit("5"); self._ft_interval.setFixedWidth(46); self._ft_interval.setFont(_MONO)
        row_i.addWidget(self._ft_interval)
        row_i.addWidget(QLabel("상태폴링(s):"))
        self._ft_stat_intv = QLineEdit("2"); self._ft_stat_intv.setFixedWidth(46); self._ft_stat_intv.setFont(_MONO)
        row_i.addWidget(self._ft_stat_intv); row_i.addStretch()
        ftl.addLayout(row_i)
        row_s = QHBoxLayout(); row_s.setSpacing(6)
        row_s.addWidget(QLabel("상태 읽기 cmd:"))
        self._ft_status_cmd = QLineEdit("READ:DEV:GRPZ:PSU:ACTN"); self._ft_status_cmd.setFont(_MONO)
        self._ft_status_cmd.setToolTip(
            "First 채널(자기장) 장비로 보내 HOLD/RTOS 상태를 읽는 명령")
        row_s.addWidget(self._ft_status_cmd, 1)
        ftl.addLayout(row_s)
        row_h = QHBoxLayout(); row_h.setSpacing(6)
        row_h.addWidget(QLabel("완료(HOLD) 토큰:"))
        self._ft_hold = QLineEdit("HOLD"); self._ft_hold.setFixedWidth(80); self._ft_hold.setFont(_MONO)
        self._ft_hold.setToolTip("상태 응답에 이 문자열이 있으면 sweep 완료로 판단 "
                                 "(예: STAT:DEV:GRPZ:PSU:ACTN:HOLD → 'HOLD')")
        row_h.addWidget(self._ft_hold); row_h.addStretch()
        ftl.addLayout(row_h)
        _ft_fwd_lbl = QLabel("ramp 시작 명령 (선택):")
        _ft_fwd_lbl.setToolTip(
            "목표값을 write한 뒤 실제 ramp를 '시작'시키는 트리거 명령.\n"
            "예: Mercury iPS는 목표 설정만으로 안 움직이므로 RTOS(ramp-to-set) 명령이 필요.\n"
            "First 명령 자체가 ramp까지 시작시키는 장비면 비워두세요.")
        ftl.addWidget(_ft_fwd_lbl)
        self._ft_fwd_list = QListWidget(); self._ft_fwd_list.setFont(_MONO)
        self._ft_fwd_list.setMaximumHeight(56)
        ftl.addWidget(self._ft_fwd_list)
        fb = QHBoxLayout()
        b_add = QPushButton("+ 명령"); b_add.clicked.connect(self._ft_add_cmd)
        b_del = QPushButton("✕"); b_del.setFixedWidth(26); b_del.clicked.connect(self._ft_del_cmd)
        fb.addWidget(b_add); fb.addWidget(b_del); fb.addStretch()
        ftl.addLayout(fb)
        self._ft_fwd_cmds: list = []

        # Feature 2: 기다리지 않고 바로 시작 — 첫 실행 시 온도 도달을 기다리지 않고
        #            '현재 온도에서' 정상 field sweep을 1회 선행한 뒤 T1→T2… 진행.
        self._cb_ft_initial = QCheckBox("기다리지 않고 현재 온도에서 먼저 sweep")
        self._cb_ft_initial.setToolTip(
            "체크 시: 첫 second(온도) 목표 도달을 기다리지 않고, 현재 온도에서 정상 field\n"
            "sweep을 먼저 1회 수행합니다. 그다음 T1 도달→sweep→T2 도달→sweep… 로 이어갑니다.\n"
            "(재개 시에는 적용되지 않습니다.)")
        ftl.addWidget(self._cb_ft_initial)

        # Feature 3: dummy(복귀) 램프도 측정 — 시작점 복귀 구간을 별도 폴더에 저장.
        self._cb_dummy_measure = QCheckBox("dummy(복귀) 구간도 측정")
        self._cb_dummy_measure.setToolTip(
            "체크 시: First 채널을 시작점으로 되돌리는 dummy 램프 동안에도 acquire 하여\n"
            "'<second>_dummy' 하위폴더에 저장합니다. (Double Sweep with Time에서 동작)")
        ftl.addWidget(self._cb_dummy_measure)

        self._ft_widget.setVisible(False)
        v.addWidget(self._ft_widget)

        # ── Stop 시 실행 명령 (예: 자기장 HOLD로 ramp 정지) ──
        stop_lbl = QLabel("⏹ Stop 시 실행 명령 (선택):")
        stop_lbl.setStyleSheet("color:#f78166; font-size:10px;")
        stop_lbl.setToolTip(
            "측정 중 Stop(또는 오류 중단) 시 자동 전송할 명령.\n"
            "예: 자기장 ramp를 멈추려면 IPS의 HOLD(SET:DEV:GRPZ:PSU:ACTN:HOLD)를 등록.\n"
            "여러 개 등록 가능하며 위에서 아래 순서로 전송됩니다.")
        v.addWidget(stop_lbl)
        self._stop_cmd_list = QListWidget(); self._stop_cmd_list.setFont(_MONO)
        self._stop_cmd_list.setMaximumHeight(56)
        v.addWidget(self._stop_cmd_list)
        sb = QHBoxLayout()
        sb_add = QPushButton("+ 명령"); sb_add.clicked.connect(self._stop_add_cmd)
        sb_del = QPushButton("✕"); sb_del.setFixedWidth(26); sb_del.clicked.connect(self._stop_del_cmd)
        sb.addWidget(sb_add); sb.addWidget(sb_del); sb.addStretch()
        v.addLayout(sb)
        self._stop_cmds: list = []

        # 시간 예상
        te = QHBoxLayout(); te.setSpacing(6)
        te.addWidget(QLabel("초/스텝:"))
        self._le_sec_per_step = QLineEdit("1.0")
        self._le_sec_per_step.setFixedWidth(54); self._le_sec_per_step.setFont(_MONO)
        self._le_sec_per_step.setToolTip("1회 acquire 예상 소요(초) — 전체 측정 시간 추정용")
        self._le_sec_per_step.textChanged.connect(self._update_time_estimate)
        te.addWidget(self._le_sec_per_step)
        self._lbl_time_est = QLabel("예상: —")
        self._lbl_time_est.setStyleSheet("color:#7ee787;")
        te.addWidget(self._lbl_time_est, 1)
        v.addLayout(te)

        # first channel N 변경도 시간 예상에 반영
        self._le_sw_n.textChanged.connect(self._update_time_estimate)
        return sec

    def _on_second_enable_toggled(self, checked: bool):
        self._second_widget.setVisible(checked)
        self._update_time_estimate()

    def _on_field_time_toggled(self, checked: bool):
        self._ft_widget.setVisible(checked)
        self._le_sw_n.setEnabled(not checked)   # field-time은 First N 불필요
        self._refresh_ds_dynamic()

    def _ft_refresh_list(self):
        self._ft_fwd_list.clear()
        for c in self._ft_fwd_cmds:
            self._ft_fwd_list.addItem(f"{c.alias}  {c.label or c.description}")

    def _ft_add_cmd(self):
        from gui.vna_config_window import _PreAdvanceCmdDialog
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
        from gui.vna_config_window import _PreAdvanceCmdDialog
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
        from config.config_models import SecondSweepAdvanceType as A
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
        first_min  = self._advance_min_time(self._selected_advance(self._combo_sweep_cmd))
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
            n1 = max(1, int(float(self._le_sw_n.text())))
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
        # acquire + first advance(매 스텝) + second advance + dummy 복귀
        secs = (total * sps
                + first_min * total
                + second_min * n2
                + first_min * returns)
        self._lbl_time_est.setText(f"예상: {total} step · 약 {self._fmt_dur(secs)}")

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
        self._on_sweep_cmd_changed(new_idx)

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
        self._le_sw_n.setEnabled(not ft.enabled)
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

        self._panel_L = _PlotPanel(start_color_idx=0)
        self._panel_R = _PlotPanel(start_color_idx=2)
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

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def _open_calibration(self):
        from gui.vna_calibration_window import VnaCalibrationWindow
        if getattr(self, "_cal_win", None) is None:
            self._cal_win = VnaCalibrationWindow(self._lib_reg, self, parent=self)
        self._cal_win.show()
        self._cal_win.raise_()

    def run_calibration(self, cmds: list, opc_cmds: list, on_finished):
        """Calibration 창이 부르는 실행 진입점.

        섹션 실행과 같은 워커·같은 잠금을 쓴다 — 같은 VISA 세션으로 같은 장비를
        건드리므로, 교정 중에는 측정 Start 와 섹션 Execute 도 함께 잠겨야 한다.

        반환: (시작했는가, 못 한 이유)
        """
        if self._sec_worker and self._sec_worker.isRunning():
            return False, "다른 명령이 실행 중입니다. 끝난 뒤 다시 누르세요."
        if getattr(self, "_acq_busy", False):
            return False, "측정 중에는 교정을 실행할 수 없습니다."
        worker = _VnaWorker(self._session, cmds, opc_cmds or [])
        self._sec_worker = worker
        self._cal_callback = on_finished

        def _done(_results):
            self._set_section_busy(False)
            self._set_status("Calibration 완료.", color="#7ee787")
            cb = getattr(self, "_cal_callback", None)
            if cb:
                cb(None)

        def _error(msg):
            self._set_section_busy(False)
            self._set_status(f"Calibration 오류: {msg}", color="#f78166")
            cb = getattr(self, "_cal_callback", None)
            if cb:
                cb(msg)

        worker.done.connect(_done)
        worker.error.connect(_error)
        worker.opc_started.connect(self._on_section_opc_started)
        self._set_section_busy(True)
        self._set_status("Calibration 실행 중…", color="#888")
        worker.start()
        return True, ""

    def _on_execute_section(self, cmds: list, opc_cmds: list = None):
        if not cmds:
            self._set_status("No enabled commands.", color="#888")
            return
        if self._sec_worker and self._sec_worker.isRunning():
            self._set_status("Busy — please wait.", color="#888")
            return
        self._sec_worker = _VnaWorker(self._session, cmds, opc_cmds or [])
        # bound 슬롯으로 연결 (워커 스레드 GUI 접근 크래시 방지 — 메인 스레드 큐잉)
        self._sec_worker.done.connect(self._on_section_done)
        self._sec_worker.error.connect(self._on_section_error)
        self._sec_worker.opc_started.connect(self._on_section_opc_started)
        # 명령을 보내는 동안, 그리고 OPC 응답이 올 때까지 측정 Start 와 Execute 를 잠근다.
        # 장비가 아직 이전 동작을 끝내지 않았는데 측정을 시작하면 값이 뒤섞인다.
        self._set_section_busy(True)
        self._set_status("Running...", color="#888")
        self._sec_worker.start()

    @Slot()
    def _on_section_opc_started(self):
        self._set_status("완료 대기 중 (OPC)…", color="#d7ba7d")

    @Slot(list)
    def _on_section_done(self, results: list):
        self._set_section_busy(False)
        self._set_status(f"Section executed ({len(results)} cmd(s)).", color="#7ee787")

    @Slot(str)
    def _on_section_error(self, msg: str):
        self._set_section_busy(False)
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
        """First/Second 채널·방향·pre-advance를 묶어 double sweep 실행.

        resume_*: 재개 시 — 남은 second 값 목록(resume_second_vals), 전역 인덱스 오프셋
        (resume_offset), 이어서 저장할 폴더(resume_folder)를 받아 그 지점부터 시작한다.
        """
        first = self._get_selected_sweep_cmd()
        if first is None:
            self._set_status("First sweep 명령을 선택하세요.", color="#f78166")
            return
        ft_enabled = self._cb_field_time.isChecked()
        try:
            f0 = float(self._le_sw_start.text())
            f1 = float(self._le_sw_stop.text())
            fn = 2 if ft_enabled else int(self._le_sw_n.text())  # field-time은 N 불필요
            if fn < 1:
                raise ValueError
        except ValueError:
            self._set_status("First 범위가 올바르지 않습니다.", color="#f78166")
            return
        f0 *= self._sweep_unit_mult(self._cb_sw_start_unit)
        f1 *= self._sweep_unit_mult(self._cb_sw_stop_unit)
        first_vals = self._linspace(f0, f1, fn)
        first_adv = first.advance if first.sweep_kind == "controlled" else None

        second_enabled = self._cb_second_enable.isChecked()
        second = None
        second_vals: list = []
        second_adv = None
        if second_enabled:
            si = self._combo_second_cmd.currentIndex()
            if not (0 <= si < len(self._cfg.acquire.sweep_cmds)):
                self._set_status("Second sweep 명령을 선택하세요.", color="#f78166")
                return
            second = self._cfg.acquire.sweep_cmds[si]
            try:
                s0 = float(self._le_2_start.text())
                s1 = float(self._le_2_stop.text())
                sn = int(self._le_2_n.text())
                if sn < 1:
                    raise ValueError
            except ValueError:
                self._set_status("Second 범위가 올바르지 않습니다.", color="#f78166")
                return
            # Feature 1: second 값 테이블(모델)이 source of truth. 워커는 실행 중 이 모델에서
            # 매 행을 새로 읽는다 → 미래 행 편집/추가가 즉시 반영됨.
            if resume_second_vals is not None:
                full = list(resume_full_vals) if resume_full_vals is not None else list(resume_second_vals)
                self._second_table.reset_from(full, done_prefix=resume_offset)
                second_vals = self._second_table.values()[resume_offset:]
            elif (self._cb_second_keep_table.isChecked()
                  and self._second_table.count() > 0):
                self._second_table.rearm()   # 커스텀 테이블 유지, 상태만 PENDING로
                second_vals = self._second_table.values()
            else:
                self._second_table.reset_from(self._linspace(s0, s1, sn))
                second_vals = self._second_table.values()
            second_adv = second.advance if second.sweep_kind == "controlled" else None

        pre_specs = [
            {"cmd": r["cmd"], "sweep": r["sweep"].text().strip(),
             "dummy": r["dummy"].text().strip()}
            for r in self._pre_adv_rows if r["cb"].isChecked()
        ]
        direction = self._combo_direction.currentData() or "uni"

        # Double Sweep with Time: field-time 설정 구성
        field_time = None
        if ft_enabled:
            field_time = {
                "enabled": True,
                "interval": self._ds_f(self._ft_interval, 5.0),
                "status_interval": self._ds_f(self._ft_stat_intv, 2.0),
                "status_alias": "",   # First 채널 장비로 폴링 (워커 폴백)
                "status_cmd": self._ft_status_cmd.text().strip(),
                "hold_token": self._ft_hold.text().strip() or "HOLD",
                "forward_cmds": list(self._ft_fwd_cmds),
            }

        # second 채널 값으로 만들 하위폴더 라벨(figure_axis). 비면 하위폴더 없음.
        self._ds_second_label = (self._sweep_col_meta(second)[0]
                                 if (second_enabled and second is not None) else "")

        if ft_enabled:
            # 시간 기반: 스텝 수를 미리 알 수 없음 → 인덱스 기반 파일명
            self._ds_step_labels = None
            self._ds_field_time = True
            # 데이터 열: second(온도) 값만 (자기장은 read 명령으로 기록됨)
            self._extra_cols = ([self._sweep_col_meta(second)]
                                if (second_enabled and second is not None) else [])
        else:
            self._ds_field_time = False
            # 스텝별 파일명 토큰 = first 값 (second 값은 이제 하위폴더가 담당)
            labels = []
            sv_list = second_vals if second_enabled else [None]
            for si2, sv in enumerate(sv_list):
                order = (first_vals if (direction == "uni" or si2 % 2 == 0)
                         else list(reversed(first_vals)))
                for fv in order:
                    labels.append(f"{fv:.6g}".replace('+', ''))
            self._ds_step_labels = labels
            # swept value 데이터 열: first (+ second) — worker의 extra_values 순서와 일치
            self._extra_cols = [self._sweep_col_meta(first)]
            if second_enabled and second is not None:
                self._extra_cols.append(self._sweep_col_meta(second))

        plan = {
            "first_cmd": first, "first_values": first_vals, "first_adv": first_adv,
            "second_enabled": second_enabled, "second_cmd": second,
            "second_values": second_vals, "second_adv": second_adv,
            "direction": direction, "pre_specs": pre_specs,
            "field_time": field_time, "second_index_offset": resume_offset,
            # Feature 1/2/3
            "second_table": (self._second_table if second_enabled else None),
            "field_initial_sweep": (ft_enabled and second_enabled
                                    and resume_offset == 0
                                    and self._cb_ft_initial.isChecked()),
            "dummy_measure": self._cb_dummy_measure.isChecked(),
        }
        # 실행 시점에 double-sweep 설정(특히 pre-advance sweep/dummy 값·체크박스, stop 명령)을
        # 프로파일에 영속화 → '방금 돌린 설정'이 재시작 후에도 유지된다.
        try:
            self._cfg.ds_control = self._collect_ds_control()
            save_vna_config(self._cfg, self._config_path())
        except Exception as e:
            self._set_status(f"설정 저장 경고: {type(e).__name__}: {e}", color="#888")
        self._start_acquire(sweep_values=None, ds_plan=plan, resume_folder=resume_folder)

        # ── resume 상태 설정 (second 채널 사용 시에만) ──
        if second_enabled and self._cb_save.isChecked() and self._sweep_folder is not None:
            if resume_second_vals is None:
                # 새 측정: 전체 second 시퀀스로 resume 상태 새로 생성·저장
                self._resume_state = VnaResumeState(
                    active=True, sweep_folder=str(self._sweep_folder),
                    second_values=[float(v) for v in second_vals],
                    completed_second_idx=-1, field_time=ft_enabled,
                    filename=self._le_filename.text().strip() or "vna_data",
                    figure_axis=self._sweep_figure_axis,
                    control=self._cfg.ds_control)
                self._save_resume()
            # 재개일 땐 _resume_double_sweep에서 이미 self._resume_state를 설정해 둠
        else:
            self._resume_state = None

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
            from core.applog import get_logger
            get_logger().exception("update_plots failed (step %d)", step_idx)
        if self._cb_save.isChecked():
            try:
                is_sweep = self._current_sweep_values is not None
                self._save_step(step_idx, arrays, is_sweep)
            except Exception:
                from core.applog import get_logger
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
            from gui.second_channel_table_window import SecondChannelTableWindow
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
            from core.applog import get_logger
            get_logger().info("second %d done (resume saved)", global_si)

    def _on_resume_clicked(self):
        from pathlib import Path as _P
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
        from gui.alarm_config_window import AlarmConfigWindow
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
        # 2) 섹션 실행 워커 — OPC 대기 중이면 최대 10분까지 잡고 있으므로 먼저 끊는다
        try:
            if self._sec_worker is not None:
                self._sec_worker.stop()
        except RuntimeError:
            pass
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

    def _set_section_busy(self, busy: bool):
        """섹션 실행(묶음 명령 + OPC 완료 대기) 동안 측정 Start 와 Execute 를 잠근다.

        장비가 아직 이전 동작을 끝내지 않았는데 측정을 시작하면 값이 뒤섞이므로,
        OPC 응답이 올 때까지는 시작할 수 없어야 한다. acquire 쪽 잠금과 서로
        덮어쓰지 않도록 두 상태를 각각 두고 합쳐서 적용한다.
        """
        self._section_busy = busy
        self._apply_busy_state()

    def _apply_busy_state(self):
        busy = getattr(self, "_acq_busy", False) or getattr(self, "_section_busy", False)
        self._btn_single.setEnabled(not busy)
        self._btn_sweep.setEnabled(not busy)
        self._btn_cfg.setEnabled(not busy)
        self._btn_alarm.setEnabled(not busy)
        if getattr(self, "_btn_cal", None) is not None:
            self._btn_cal.setEnabled(not busy)
        for sw in self._section_widgets:
            sw.setEnabled(not busy)

    def _set_acquire_busy(self, busy: bool):
        # sweep 중에는 충돌·설정변경을 막기 위해 동작 버튼을 모두 비활성화 (Stop만 활성)
        self._acq_busy = busy
        self._apply_busy_state()
        self._btn_stop_acq.setEnabled(busy)
        if getattr(self, "_btn_resume", None) is not None and busy:
            self._btn_resume.setEnabled(False)   # 해제는 _update_resume_button이 판단
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
            self._panel_L.restore_config(curves[0])
        if len(curves) >= 2:
            self._panel_R.restore_config(curves[1])

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
        from gui.vna_config_window import VnaConfigWindow
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
            self._panel_L.to_config(),
            self._panel_R.to_config(),
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
