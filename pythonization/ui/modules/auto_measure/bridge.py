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
