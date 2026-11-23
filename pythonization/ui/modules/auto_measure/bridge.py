"""AI 에이전트 스레드 ↔ 메인 창 (GUI 스레드) 연결.

에이전트는 자기 스레드에서 돈다. 메인 창 위젯은 GUI 스레드에서만 만질 수 있으므로,
메인 창 조작은 전부 call() 로 GUI 스레드에 넘겨 실행하고 결과를 기다린다.

run_main_sweep 은 사람이 하는 것과 똑같이 메인 창을 설정한다:
  sweep channel 라디오 → Sweep To / Rate / Time/Point → measurement 체크 →
  Sub Folder / Filename / Auto-save → start_sweep_unattended()
그 뒤 sweep_stopped 신호가 올 때까지 기다린다. 그동안 sweep_step_recorded 로 매 행을
받아 즉시 중단 조건(|값| 상한)과 안정화 조건을 GUI 스레드에서 바로 판정한다 —
LLM 의 다음 호출을 기다리지 않는다.
"""
import math
import threading
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from pythonization.automation.executor import (
    TIME_CHANNEL_ID,
    MainSweepOutcome,
    MainSweepSpec,
)

_CALL_TIMEOUT_S = 180.0      # 메인 창 설정·시작 (장비 연결 포함)


class _ActiveSweep:
    def __init__(self, spec: MainSweepSpec):
        self.spec = spec
        self.future: Future = Future()
        self.points = 0
        self.filepath = ""
        self.override: Optional[tuple] = None      # (reason, detail)
        self.stop_requested = False
        self.recent = deque(maxlen=spec.stable[1]) if spec.stable else None


class MainWindowBridge(QObject):
    """GUI 스레드에서 만든다. 에이전트 스레드는 call / run_main_sweep 만 부른다."""

    _invoke = Signal(object)

    def __init__(self, main_win, emit: Optional[Callable] = None):
        super().__init__()
        self._main_win = main_win
        self._emit = emit or (lambda _ev: None)
        self._stop: threading.Event = threading.Event()
        self._active: Optional[_ActiveSweep] = None
        self._snapshot: Optional[dict] = None
        # 에이전트 스레드에서 emit → 이 객체(GUI 스레드)로 큐잉된다 (bound @Slot)
        self._invoke.connect(self._on_invoke)
        main_win.sweep_step_recorded.connect(self._on_step_recorded)
        main_win.sweep_stopped.connect(self._on_sweep_stopped)

    def set_stop_event(self, stop_event: threading.Event) -> None:
        self._stop = stop_event

    # ── 스레드 간 호출 ─────────────────────────────────────────────────
    @Slot(object)
    def _on_invoke(self, item):
        fn, fut = item
        if fut.set_running_or_notify_cancel():
            try:
                fut.set_result(fn())
            except BaseException as exc:     # noqa: BLE001 — 호출한 쪽에서 다시 던진다
                fut.set_exception(exc)

    def call(self, fn: Callable, timeout: float = _CALL_TIMEOUT_S):
        """fn 을 GUI 스레드에서 실행하고 결과를 돌려준다."""
        if QThread.currentThread() is self.thread():
            return fn()
        fut: Future = Future()
        self._invoke.emit((fn, fut))
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            fut.cancel()
            raise RuntimeError("메인 창이 응답하지 않습니다 (대화상자가 열려 있는지 확인).")

    # ── LabBackend ─────────────────────────────────────────────────────
    def busy_reason(self) -> str:
        return self.call(self._main_win.measurement_busy_reason)

    def main_state(self) -> dict:
        return self.call(self._main_state)

    def _main_state(self) -> dict:
        mw = self._main_win
        ch_id = mw._sweep_radio_group.checkedId()
        return {
            "main_sweep_running": mw._running,
            "selected_sweep_channel": "time" if ch_id == TIME_CHANNEL_ID else ch_id,
            "sweep_to": mw._sweep_config.sweep_to,
            "rate_per_min": mw._sweep_config.sweep_rate,
            "time_per_point_s": mw._sweep_config.time_per_point,
            "main_folder": mw._le_main_folder.text(),
            "include_date": mw._cb_save_date.isChecked(),
            "other_measurement_running": mw.measurement_busy_reason(),
        }

    def run_main_sweep(self, spec: MainSweepSpec) -> MainSweepOutcome:
        """메인 창 sweep 을 구성·시작하고 끝날 때까지 기다린다 (에이전트 스레드)."""
        active = _ActiveSweep(spec)
        try:
            err = self.call(lambda: self._start(active))
        except Exception as exc:     # noqa: BLE001
            return MainSweepOutcome("start_failed", str(exc))
        if err:
            return MainSweepOutcome("start_failed", err)
        while True:
            try:
                return active.future.result(timeout=0.5)
            except FutureTimeout:
                if self._stop.is_set() and not active.stop_requested:
                    active.stop_requested = True
                    try:
                        self.call(lambda: self._stop_active(
                            active, "agent_stop", "사용자가 AI 실행을 중지했습니다."))
                    except Exception:
                        pass


    # ── VNA 창 (부가 측정 모듈) ────────────────────────────────────────
    # 메인 sweep 과 달리 단계별 데이터를 받아 판정할 필요가 없어서, 완료는
    # 신호 대신 **폴링**으로 본다 — 새 신호를 만들지 않고 GUI 스레드에서
    # _acq_thread 상태만 물어보면 된다.
    _VNA_POLL_S = 0.5

    def _vna_win(self, create: bool = False):
        """VNA Control 창 (없으면 create 일 때만 연다). GUI 스레드에서 부를 것."""
        mw = self._main_win
        win = getattr(mw, "_vna_window", None)
        if win is None and create:
            mw._open_vna_window()
            win = getattr(mw, "_vna_window", None)
        return win

    def vna_state(self) -> dict:
        return self.call(self._vna_state)

    def _vna_state(self) -> dict:
        win = self._vna_win()
        if win is None:
            return {"open": False,
                    "hint": "VNA Control 창이 아직 열려 있지 않습니다. "
                            "vna_acquire 를 부르면 자동으로 엽니다."}
        cfg = win._cfg
        ds = cfg.ds_control
        acq = cfg.acquire
        th = getattr(win, "_acq_thread", None)
        if ds.power.enabled:
            mode = "power_sweep"
        elif ds.field_time.enabled:
            mode = "double_sweep_with_time"
        else:
            mode = "double_sweep"

        def axis(idx):
            if 0 <= idx < len(acq.sweep_cmds):
                e = acq.sweep_cmds[idx]
                return "[" + e.alias + "] " + e.description
            return ""

        return {
            "open": True,
            "running": bool(th is not None and th.isRunning()),
            "mode": mode,
            "first_axis": axis(ds.first_idx),
            "first_range": [ds.first_start, ds.first_stop, ds.first_n],
            "second_enabled": bool(ds.second_enabled),
            "second_axis": axis(ds.second_idx) if ds.second_enabled else "",
            "second_range": ([ds.second_start, ds.second_stop, ds.second_n]
                             if ds.second_enabled else []),
            "sections": [s.name for s in cfg.sections],
            "read_cmds_enabled": sum(1 for e in acq.read_cmds if e.enabled),
            "sweep_cmds_enabled": sum(1 for e in acq.sweep_cmds if e.enabled),
            "save_enabled": bool(cfg.save_enabled),
            "save_folder": str(win._get_save_dir()),
            "filename": win._le_filename.text().strip(),
            "last_saved": str(getattr(win, "_last_saved_path", "") or ""),
            "problems": self._vna_problems(win),
        }

    @staticmethod
    def _vna_problems(win) -> list:
        """시작 전에 걸릴 설정 문제 — 대화상자를 띄우지 않고 미리 본다.

        VNA 창의 검사는 QMessageBox 를 띄우는데, AI 실행 중에 모달이 뜨면 GUI
        스레드가 막혀 call() 이 통째로 타임아웃된다. 그래서 같은 판정을
        대화상자 없이 먼저 돌려 **문제가 있으면 시작 자체를 하지 않는다.**
        """
        try:
            from pythonization.ui.modules.vna.models import collect_param_problems
            acq = win._cfg.acquire
            out = []
            for group in (acq.sweep_cmds, acq.start_cmds, acq.wait_cmds, acq.read_cmds):
                out += collect_param_problems(win._lib_reg, group)
            return out
        except Exception as e:                    # noqa: BLE001
            return ["설정 검사 실패: " + type(e).__name__ + ": " + str(e)]

    def vna_run_section(self, name: str) -> dict:
        return self.call(lambda: self._vna_run_section(name))

    def _vna_run_section(self, name: str) -> dict:
        win = self._vna_win(create=True)
        if win is None:
            return {"ok": False, "error": "VNA Control 창을 열 수 없습니다."}
        for sec in win._cfg.sections:
            if sec.name == name:
                cmds = [c for c in sec.commands if c.enabled]
                if not cmds:
                    return {"ok": False,
                            "error": "섹션 '" + name + "' 에 켜진 명령이 없습니다."}
                win._on_execute_section(cmds, list(sec.opc_cmds))
                return {"ok": True, "section": name, "commands": len(cmds),
                        "opc_wait": bool(sec.opc_cmds)}
        return {"ok": False,
                "error": "'" + name + "' 섹션이 없습니다. 있는 것: "
                         + ", ".join(s.name for s in win._cfg.sections)}

    def vna_stop(self) -> dict:
        return self.call(self._vna_stop)

    def _vna_stop(self) -> dict:
        win = self._vna_win()
        if win is None:
            return {"ok": False, "error": "VNA Control 창이 없습니다."}
        th = getattr(win, "_acq_thread", None)
        if th is None or not th.isRunning():
            return {"ok": True, "note": "실행 중인 VNA 측정이 없습니다."}
        win._on_stop_acquire()
        return {"ok": True, "note": "중지를 요청했습니다."}

    def vna_acquire(self, mode: str = "single", wait: bool = True,
                    timeout_s: float = 3600.0) -> dict:
        """VNA 측정을 시작한다 (mode: single | sweep). wait 면 끝날 때까지 기다린다."""
        import time as _t
        start = self.call(lambda: self._vna_start(mode))
        if not start.get("ok"):
            return start
        if not wait:
            return dict(start, waited=False)

        deadline = _t.monotonic() + max(1.0, float(timeout_s))
        stop_sent = False
        while True:
            if not self.call(self._vna_running, timeout=30.0):
                break
            if self._stop.is_set() and not stop_sent:
                stop_sent = True
                try:
                    self.call(self._vna_stop, timeout=30.0)
                except Exception:
                    pass
            if _t.monotonic() > deadline:
                try:
                    self.call(self._vna_stop, timeout=30.0)
                except Exception:
                    pass
                out = dict(start, waited=True, ok=False)
                out["error"] = ("VNA 측정이 %.0f분 안에 끝나지 않아 중지했습니다."
                                % (timeout_s / 60.0))
                return out
            _t.sleep(self._VNA_POLL_S)
        done = self.call(self._vna_result)
        return dict(start, waited=True, **done)

    def _vna_running(self) -> bool:
        win = self._vna_win()
        th = getattr(win, "_acq_thread", None) if win is not None else None
        return bool(th is not None and th.isRunning())

    def _vna_result(self) -> dict:
        win = self._vna_win()
        if win is None:
            return {}
        return {"steps": int(getattr(win, "_step_count", 0) or 0),
                "last_saved": str(getattr(win, "_last_saved_path", "") or ""),
                "stopped": bool(getattr(win, "_acq_stopped", False)),
                "errored": bool(getattr(win, "_acq_errored", False))}

    def _vna_start(self, mode: str) -> dict:
        win = self._vna_win(create=True)
        if win is None:
            return {"ok": False, "error": "VNA Control 창을 열 수 없습니다."}
        th = getattr(win, "_acq_thread", None)
        if th is not None and th.isRunning():
            return {"ok": False, "error": "VNA 측정이 이미 실행 중입니다."}
        problems = self._vna_problems(win)
        if problems:
            return {"ok": False,
                    "error": "VNA 설정에 문제가 있어 시작하지 않았습니다.",
                    "problems": problems}
        if mode == "single":
            win._on_single_acquire()
        elif mode == "sweep":
            win._on_sweep_acquire()
        else:
            return {"ok": False, "error": "알 수 없는 mode: " + str(mode) + " (single | sweep)"}
        th = getattr(win, "_acq_thread", None)
        if th is None or not th.isRunning():
            return {"ok": False,
                    "error": "VNA 측정이 시작되지 않았습니다 "
                             "(설정·장비 연결을 확인하세요)."}
        return {"ok": True, "mode": mode}

    # ── GUI 스레드 ─────────────────────────────────────────────────────
    def _verify_identity(self, spec: MainSweepSpec) -> str:
        """인덱스가 여전히 같은 장비 항목을 가리키는지 (프로파일 변경 방어)."""
        profile = self._main_win._active_profile
        if spec.channel != TIME_CHANNEL_ID and spec.channel_key is not None:
            svs = profile.sweep_values
            if not 0 <= spec.channel < len(svs) or \
                    (svs[spec.channel].alias, svs[spec.channel].description) != spec.channel_key:
                return "메인 창 프로파일이 바뀌어 sweep channel 이 달라졌습니다. 다시 확인하세요."
        ms = profile.measurements
        for idx, key in zip(spec.measurements, spec.measurement_keys):
            if not 0 <= idx < len(ms) or (ms[idx].alias, ms[idx].description) != key:
                return "메인 창 프로파일이 바뀌어 measurement 구성이 달라졌습니다."
        return ""

    def _start(self, active: _ActiveSweep) -> str:
        mw = self._main_win
        spec = active.spec
        if mw._running:
            return "Main sweep 이 이미 실행 중입니다."
        busy = mw.measurement_busy_reason()
        if busy:
            return f"다른 측정이 실행 중입니다: {busy}"
        err = self._verify_identity(spec)
        if err:
            return err
        if not mw._le_main_folder.text().strip():
            return ("메인 창 Save Settings 의 Main Folder 가 비어 있습니다. "
                    "사용자가 지정해야 합니다.")
        self.take_snapshot()

        btn = mw._sweep_radio_group.button(spec.channel)
        if btn is None or not btn.isEnabled():
            return f"메인 창에서 sweep channel {spec.channel} 을(를) 고를 수 없습니다."
        btn.setChecked(True)
        mw._on_sweep_radio_toggled(spec.channel, True)   # 이미 선택돼 있던 경우도 갱신

        for field, value in ((mw._le_sweep_to, spec.sweep_to),
                             (mw._le_sweep_rate, spec.rate),
                             (mw._le_time_per_point, spec.time_per_point)):
            field.setText(f"{value:.12g}")
        mw._on_sweep_params_confirmed()

        wanted = set(spec.measurements)
        for i, cb in enumerate(mw._meas_checkboxes):
            if i in wanted and not cb.isEnabled():
                return f"measurement {i} 은(는) 메인 창에서 쓸 수 없는 상태입니다."
            cb.setChecked(i in wanted)

        mw._le_custom_folder.setText(spec.sub_folder)
        mw._le_custom_word.setText(spec.file_prefix)
        mw._cb_save_enable.setChecked(True)
        mw._update_save_preview()

        self._active = active
        err = mw.start_sweep_unattended()
        if err:
            self._active = None
            return err
        fp = mw._data_saver.get_filepath()
        active.filepath = str(fp) if fp else ""
        return ""

    def _stop_active(self, active: _ActiveSweep, reason: str, detail: str) -> None:
        mw = self._main_win
        if self._active is not active:
            return
        active.override = (reason, detail)
        if mw._running:
            if mw._last_write_value is not None:
                mw._save_resume_point(f"AI: {reason}")   # 사람이 [Resume] 으로 이어 갈 수 있게
            mw._on_stop()
        else:
            self._on_sweep_stopped(reason, detail)

    @Slot(float, object)
    def _on_step_recorded(self, x: float, values: object):
        active = self._active
        if active is None or active.override is not None:
            return
        active.points += 1
        spec = active.spec
        for idx, limit in spec.abort_limits.items():
            v = values.get(idx) if isinstance(values, dict) else None
            if v is not None and math.isfinite(v) and abs(v) > limit:
                name = spec.labels.get(idx, f"measurement {idx}")
                self._stop_active(active, "abort",
                                  f"{name} |{v:.4g}| > {limit:g} 이라 즉시 중단 (x={x:.6g})")
                return
        if spec.stable is not None:
            idx, window, max_std = spec.stable
            v = values.get(idx) if isinstance(values, dict) else None
            if v is not None and math.isfinite(v):
                active.recent.append(v)
                if len(active.recent) == window:
                    mean = sum(active.recent) / window
                    std = math.sqrt(sum((s - mean) ** 2 for s in active.recent) / window)
                    if std < max_std:
                        self._stop_active(active, "stable",
                                          f"최근 {window}점 std {std:.3g} < {max_std:g} — 안정화")

    @Slot(str, str)
    def _on_sweep_stopped(self, reason: str, detail: str):
        active = self._active
        if active is None:
            return
        self._active = None
        if active.override is not None:
            reason, detail = active.override
        if not active.filepath:
            fp = self._main_win._data_saver.get_filepath()
            active.filepath = str(fp) if fp else ""
        if not active.future.done():
            active.future.set_result(
                MainSweepOutcome(reason, detail, active.filepath, active.points))

    # ── 메인 창 설정 백업/복원 ──────────────────────────────────────────
    def take_snapshot(self) -> None:
        """AI 가 처음 메인 창을 건드리기 직전의 sweep·저장 설정 (실행당 1회)."""
        if self._snapshot is not None:
            return
        mw = self._main_win
        self._snapshot = {
            "channel": mw._sweep_radio_group.checkedId(),
            "sweep_to": mw._le_sweep_to.text(),
            "rate": mw._le_sweep_rate.text(),
            "tpp": mw._le_time_per_point.text(),
            "checked": [cb.isChecked() for cb in mw._meas_checkboxes],
            "custom_folder": mw._le_custom_folder.text(),
            "custom_word": mw._le_custom_word.text(),
            "save_enabled": mw._cb_save_enable.isChecked(),
        }

    def restore_snapshot(self) -> bool:
        """메인 창을 AI 실행 전 설정으로 되돌린다 (GUI 스레드, sweep 이 멈춘 뒤)."""
        snap, mw = self._snapshot, self._main_win
        if snap is None or mw._running:
            return False
        self._snapshot = None
        btn = mw._sweep_radio_group.button(snap["channel"])
        if btn is not None and btn.isEnabled():
            btn.setChecked(True)
            mw._on_sweep_radio_toggled(snap["channel"], True)
        mw._le_sweep_to.setText(snap["sweep_to"])
        mw._le_sweep_rate.setText(snap["rate"])
        mw._le_time_per_point.setText(snap["tpp"])
        mw._on_sweep_params_confirmed()
        if len(snap["checked"]) == len(mw._meas_checkboxes):
            for cb, checked in zip(mw._meas_checkboxes, snap["checked"]):
                if cb.isEnabled():
                    cb.setChecked(checked)
        mw._le_custom_folder.setText(snap["custom_folder"])
        mw._le_custom_word.setText(snap["custom_word"])
        mw._cb_save_enable.setChecked(snap["save_enabled"])
        mw._update_save_preview()
        return True

    def forget_snapshot(self) -> None:
        self._snapshot = None
