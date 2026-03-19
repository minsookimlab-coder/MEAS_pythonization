"""
SecondChannelWorker: double sweep의 second channel을 설정하는 워커.
QThread 위에서 동작하며 advance_type에 따라 4가지 전략 중 하나를 실행합니다.
"""
import threading
import time as _time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QObject, Signal, Slot

from config.config_models import InstantiatedSecondSweepChannel, SecondSweepAdvanceType
from core.sweep import calculate_next_step
from core.sweep_channel import SweepChannel
from core.instrument_parameter import MeasurementParameter

if TYPE_CHECKING:
    from core.instrument_session import InstrumentSession


@dataclass
class SecondChannelRequest:
    channel: InstantiatedSecondSweepChannel
    next_value: float
    prev_value: Optional[float]   # None on first advance
    time_per_point: float


class SecondChannelWorker(QObject):
    """
    메인 스레드가 request_advance Signal을 emit하면
    advance Slot이 워커 스레드에서 호출됩니다.
    완료 시 done, 오류 시 error Signal을 emit합니다.
    """

    done  = Signal()
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self._session = None
        self._stop_event = threading.Event()

    def set_session(self, session) -> None:
        self._session = session

    def request_stop(self) -> None:
        self._stop_event.set()

    @Slot(object)
    def advance(self, req: SecondChannelRequest) -> None:
        self._stop_event.clear()
        try:
            ch = req.channel
            alias = ch.alias
            next_v = req.next_value
            prev_v = req.prev_value

            if not self._session.is_open(alias):
                self._session.open(alias)

            if ch.advance_type == SecondSweepAdvanceType.SIMPLE_HOP:
                self._do_simple_hop(alias, ch.cmd_set, next_v)

            elif ch.advance_type == SecondSweepAdvanceType.SWEEP:
                self._do_sweep(alias, ch, next_v, prev_v, req.time_per_point)

            elif ch.advance_type == SecondSweepAdvanceType.FEEDBACK:
                self._do_feedback(alias, ch, next_v, prev_v)

            elif ch.advance_type == SecondSweepAdvanceType.WAIT_FOR_TIME:
                self._do_wait_for_time(alias, ch.cmd_set, next_v, ch.wait_time)

            self.done.emit()

        except Exception as e:
            self.error.emit(str(e))

    # ------------------------------------------------------------------

    def _do_simple_hop(self, alias: str, cmd_set: str, value: float) -> None:
        """단순히 값을 설정하고 즉시 반환."""
        self._session.write(alias, cmd_set.format(v=value))

    def _do_sweep(self, alias: str, ch: InstantiatedSecondSweepChannel,
                  next_v: float, prev_v: Optional[float],
                  time_per_point: float) -> None:
        """calculate_next_step 루프로 점진적으로 sweep (측정 없음)."""
        from core.sweep_channel import SweepChannel
        from core.instrument_parameter import MeasurementParameter, SweepParameter

        sweep_ch = SweepChannel(
            alias=alias,
            parameter=SweepParameter(
                name=ch.description,
                cmd_set=ch.cmd_set,
                readback=MeasurementParameter(
                    name=ch.description + " readback",
                    cmd_query=ch.paired_read_cmd,
                ),
            ),
        )

        if prev_v is None:
            current = sweep_ch.read_value(self._session)
        else:
            current = prev_v

        while True:
            if self._stop_event.is_set():
                break
            step_v, is_done = calculate_next_step(
                current, next_v, ch.sweep_rate, time_per_point
            )
            # Safety sub-steps
            if ch.safety_steps > 0 and abs(step_v - current) > 1e-11:
                for i in range(1, ch.safety_steps + 1):
                    if self._stop_event.is_set():
                        break
                    sub_v = current + (step_v - current) * i / ch.safety_steps
                    sweep_ch.set_value(self._session, sub_v)
                    if i < ch.safety_steps and ch.safety_interval_ms > 0:
                        _time.sleep(ch.safety_interval_ms / 1000.0)
            else:
                sweep_ch.set_value(self._session, step_v)
            current = step_v
            if is_done:
                break

    def _do_feedback(self, alias: str, ch: InstantiatedSecondSweepChannel,
                     next_v: float, prev_v: Optional[float]) -> None:
        """값 설정 후 feedback_read_cmd로 폴링, 목표 도달 시 반환."""
        self._session.write(alias, ch.cmd_set.format(v=next_v))

        denom = abs(next_v - (prev_v or 0.0))
        if denom < 1e-12:
            # 이미 목표값 → 즉시 반환
            return

        while True:
            if self._stop_event.is_set():
                break
            _time.sleep(ch.feedback_poll_interval)
            try:
                raw = self._session.query(alias, ch.feedback_read_cmd).strip()
                v_read = float(raw)
            except Exception:
                continue
            ratio = abs(v_read - (prev_v or 0.0)) / denom
            if ratio >= ch.feedback_tolerance_pct / 100.0:
                break

    def _do_wait_for_time(self, alias: str, cmd_set: str,
                          value: float, wait_time: float) -> None:
        """값 설정 후 wait_time 초 대기."""
        self._session.write(alias, cmd_set.format(v=value))
        deadline = _time.monotonic() + wait_time
        while _time.monotonic() < deadline:
            if self._stop_event.is_set():
                break
            _time.sleep(0.05)
