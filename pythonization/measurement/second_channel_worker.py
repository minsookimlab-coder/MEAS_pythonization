"""
SecondChannelWorker: double sweep의 second channel을 설정하는 워커.
QThread 위에서 동작하며 advance_type에 따라 4가지 전략 중 하나를 실행합니다.
"""
import math
import threading
import time as _time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QObject, Signal, Slot

from pythonization.config.models import (
    InstantiatedSecondSweepChannel,
    SecondSweepAdvanceType,
)
from pythonization.measurement.sweep import calculate_next_step
from pythonization.measurement.channel import SweepChannel
from pythonization.instruments.errors import is_comm_error
from pythonization.instruments.parameter import (
    MeasurementParameter,
    SweepParameter,
    _parse_float,
)

if TYPE_CHECKING:
    from pythonization.instruments.session import InstrumentSession


# ── second advance 무한 지속 감지용 워치독 타임아웃 ───────────────────────────
# threshold(목표 도달)까지 너무 오래 걸리면 명령이 무시됐을 가능성을 의심해 1회
# 재전송 후 다시 대기하고, 그래도 안 되면 실패 처리한다. feedback(안정화) 과정이
# 너무 오래 지속돼도 실패 처리한다. 실패 시 측정 중지 + 알람.
_PHASE1_TIMEOUT_S = 20 * 60   # threshold 도달 최대 대기 (재전송 시 1회 더 → 총 최대 40분)
_PHASE2_TIMEOUT_S = 5 * 60    # feedback 안정화 과정 최대 지속

# 폴링 중 '연속' 실패 임계. 워치독(20~40분)까지 묵히지 않고 여기서 끊어 상위의
# 자동 재개를 빨리 돌린다. 한 번 성공적으로 읽으면 카운터는 0으로 돌아간다.
_MAX_COMM_FAILS = 3          # 통신 오류 — 장비가 사라졌을 가능성
_MAX_NONFINITE = 5           # NaN/Inf — 읽기 명령이 잘못됐을 가능성
_PROGRESS_INTERVAL_S = 1.5   # '도달 중' 상태 표시 최소 간격


def _feedback_band(tolerance_pct: float, distance: float, noisefloor: float) -> float:
    """목표에 도달했다고 볼 허용 오차.

    큰 이동은 비율 허용오차((1-tol%)·이동거리)로 판정한다. 시작점이 목표에 이미
    가까운 작은 이동에서는 그 값이 노이즈보다 작아져 영영 도달 판정이 나지 않으므로
    noisefloor 를 절대 하한으로 둔다.
    """
    return max((1.0 - tolerance_pct / 100.0) * distance, noisefloor)


def _stability_metric(samples, target: float, noisefloor: float) -> float:
    """정규화한 흔들림 지표 = 표준편차 / (|목표값| + noisefloor).

    평균이 아니라 목표값으로 정규화한다 — overshoot 로 평균이 치우쳐도 척도가
    일정하게 유지된다. 목표가 0 근처면 noisefloor 를 반드시 설정해야 한다
    (아니면 분모가 0에 가까워져 영영 안정 판정이 나지 않는다).
    """
    if not samples:
        return float("inf")
    mean = sum(samples) / len(samples)
    variance = sum((v - mean) ** 2 for v in samples) / len(samples)
    scale = abs(target) + noisefloor
    return math.sqrt(variance) / scale if scale > 1e-30 else float("inf")


class SecondAdvanceTimeout(Exception):
    """second channel advance가 워치독 시간 내에 완료되지 못함 (측정 중지 + 알람 대상)."""
    pass


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

    done             = Signal()
    error            = Signal(str)
    feedback_progress = Signal(float)   # Phase 2 안정화 metric (std / scale); 매 평가 시 emit
    advance_failed   = Signal(str)      # 워치독 타임아웃 → 측정 중지 + 알람 (comm 오류와 구분)
    status           = Signal(str)      # 진행 상태 메시지 (예: 명령 재전송)

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

            elif ch.advance_type == SecondSweepAdvanceType.THRESHOLD_TIME:
                self._do_threshold_time(alias, ch, next_v, prev_v)

            self.done.emit()

        except SecondAdvanceTimeout as te:
            # 워치독 타임아웃 — comm 오류 자동재개가 아니라 즉시 중지 + 알람 경로로
            self.advance_failed.emit(str(te))
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
                sub_vs = [
                    current + (step_v - current) * i / ch.safety_steps
                    for i in range(1, ch.safety_steps + 1)
                ]
                if ch.safety_interval_ms <= 0:
                    # 배치 최적화: N회 round-trip → 1회
                    batch_cmd = "\n".join(ch.cmd_set.format(v=v) for v in sub_vs)
                    if not self._session.is_open(alias):
                        self._session.open(alias)
                    self._session.write(alias, batch_cmd)
                else:
                    # 인터벌 있음: VISA write 시간 차감 보정 sleep
                    interval_s = ch.safety_interval_ms / 1000.0
                    for i, sub_v in enumerate(sub_vs):
                        if self._stop_event.is_set():
                            break
                        t0 = _time.perf_counter()
                        sweep_ch.set_value(self._session, sub_v)
                        if i < len(sub_vs) - 1:
                            elapsed = _time.perf_counter() - t0
                            remaining = interval_s - elapsed
                            if remaining > 0:
                                _time.sleep(remaining)
            else:
                sweep_ch.set_value(self._session, step_v)
            current = step_v
            if is_done:
                break

    def _do_feedback(self, alias: str, ch: InstantiatedSecondSweepChannel,
                     next_v: float, prev_v: Optional[float]) -> None:
        """값을 설정한 뒤 실제 값이 목표에 도달하고 안정될 때까지 기다린다.

        Phase 1 — 목표와의 거리가 허용 band 안에 들어올 때까지 폴링
        Phase 2 — (std_window > 0일 때) 흔들림이 임계 아래로 내려갈 때까지 폴링

        자기장처럼 명령을 줘도 실제 도달까지 시간이 걸리고 출렁이는 값에 쓴다.
        """
        self._session.write(alias, ch.cmd_set.format(v=next_v))

        distance = abs(next_v - (prev_v or 0.0))
        use_std = ch.feedback_std_window > 0 and ch.feedback_std_threshold > 0.0

        if distance < 1e-12:
            # 이미 목표값에 있다 — 도달 판정은 건너뛴다
            if not use_std:
                return
        elif not self._await_feedback_target(alias, ch, next_v, distance):
            return      # Stop 요청

        if use_std:
            self._await_feedback_stability(alias, ch, next_v)

    def _await_feedback_target(self, alias: str, ch: InstantiatedSecondSweepChannel,
                               next_v: float, distance: float) -> bool:
        """Phase 1 — 목표 도달까지 대기. 도달하면 True, Stop 이면 False."""
        band = _feedback_band(ch.feedback_tolerance_pct, distance, ch.feedback_noisefloor)
        last_progress = 0.0

        for value in self._poll_feedback(alias, ch,
                                         self._phase1_watchdog(alias, ch, next_v)):
            if abs(value - next_v) <= band:
                return True
            # 도달 중 현재값 표시 (너무 자주 찍지 않도록 간격 제한)
            now = _time.monotonic()
            if now - last_progress >= _PROGRESS_INTERVAL_S:
                self.status.emit(f"도달 중 {value:.4g} → {next_v:.4g} "
                                 f"(남음 {abs(value - next_v):.3g})")
                last_progress = now
        return False

    def _await_feedback_stability(self, alias: str,
                                  ch: InstantiatedSecondSweepChannel,
                                  next_v: float) -> None:
        """Phase 2 — 최근 std_window 개 샘플의 흔들림이 임계 아래로 내려갈 때까지."""
        deadline = _time.monotonic() + _PHASE2_TIMEOUT_S

        def watchdog():
            if _time.monotonic() > deadline:
                raise SecondAdvanceTimeout(
                    f"second channel '{alias}': feedback 안정화가 "
                    f"{_PHASE2_TIMEOUT_S // 60}분 내 수렴하지 않음")

        window: deque = deque(maxlen=ch.feedback_std_window)
        for value in self._poll_feedback(alias, ch, watchdog):
            window.append(value)
            if len(window) < ch.feedback_std_window:
                continue
            metric = _stability_metric(window, next_v, ch.feedback_noisefloor)
            self.feedback_progress.emit(metric)
            if metric < ch.feedback_std_threshold:
                return

    def _phase1_watchdog(self, alias: str, ch: InstantiatedSecondSweepChannel,
                         next_v: float):
        """Phase 1 시간 감시 — 늦으면 명령을 한 번 다시 보내 만회를 시도한다.

        장비가 명령 하나를 흘렸을 뿐인데 20분을 버리는 일을 막는다. 재전송 후에도
        도달하지 못하면 실패로 끊어 상위의 자동 재개 경로로 보낸다.
        """
        state = {"deadline": _time.monotonic() + _PHASE1_TIMEOUT_S, "retried": False}

        def check():
            if _time.monotonic() <= state["deadline"]:
                return
            if not state["retried"]:
                state["retried"] = True
                self.status.emit(
                    f"second '{alias}' threshold {_PHASE1_TIMEOUT_S // 60}분 미도달 "
                    f"— 명령 재전송 후 재대기")
                self._session.write(alias, ch.cmd_set.format(v=next_v))
                state["deadline"] = _time.monotonic() + _PHASE1_TIMEOUT_S
                return
            raise SecondAdvanceTimeout(
                f"second channel '{alias}': 명령 재전송 후에도 "
                f"{_PHASE1_TIMEOUT_S // 60}분 내 목표값({next_v:g})에 도달하지 못함")

        return check

    def _poll_feedback(self, alias: str, ch: InstantiatedSecondSweepChannel,
                       watchdog):
        """feedback 값을 하나씩 내놓는다. Stop 이 걸리면 조용히 끝난다.

        일시적 통신 오류나 비유한값은 그 폴만 건너뛴다. 다만 **연속** 실패가
        임계를 넘으면 바로 끊는다 — 워치독(20~40분)까지 기다리면 상위의 자동
        재개가 너무 늦게 돈다.

        watchdog 은 매 폴 직전에 불린다(시간 초과 시 예외 또는 재전송). query 실패로
        건너뛰는 경우에도 반드시 평가되도록 루프 맨 앞에 둔다.
        """
        comm_fails = 0
        nonfinite = 0

        while not self._stop_event.is_set():
            watchdog()
            # uninterruptible sleep 대신 wait → Stop 이 폴 간격 안에 반영된다
            if self._stop_event.wait(ch.feedback_poll_interval):
                return
            try:
                raw = self._session.query(alias, ch.feedback_read_cmd).strip()
                value = _parse_float(raw)
            except Exception as e:
                if is_comm_error(e):
                    comm_fails += 1
                    if comm_fails >= _MAX_COMM_FAILS:
                        raise
                continue
            comm_fails = 0

            if not math.isfinite(value):
                nonfinite += 1
                if nonfinite >= _MAX_NONFINITE:
                    raise SecondAdvanceTimeout(
                        f"second channel '{alias}': 측정값이 계속 비유한값(NaN/Inf) "
                        f"(read='{ch.feedback_read_cmd}').")
                continue
            nonfinite = 0

            yield value

    def _do_threshold_time(self, alias: str, ch: InstantiatedSecondSweepChannel,
                           next_v: float, prev_v: Optional[float]) -> None:
        """값 설정 → 목표 band 도달까지 폴링(Phase 1) → wait_time 초 대기 후 반환.

        Feedback의 std 안정화(Phase 2) 대신, 도달 후 '고정 시간'만 기다린다.
        도달 판정 band = max((1-tol%)·denom, noisefloor) 로 Feedback Phase 1과 동일.
        threshold 미도달 워치독은 Feedback과 동일하게 적용(1회 재전송 후 실패).
        """
        self._session.write(alias, ch.cmd_set.format(v=next_v))
        denom = abs(next_v - (prev_v or 0.0))

        # ── Phase 1: 목표 band 도달까지 폴링 ──
        if denom >= 1e-12:
            phase1_deadline = _time.monotonic() + _PHASE1_TIMEOUT_S
            retried = False
            last_prog = 0.0
            comm_fails = 0
            nonfinite = 0
            while True:
                if self._stop_event.is_set():
                    return
                if _time.monotonic() > phase1_deadline:
                    if not retried:
                        retried = True
                        self.status.emit(
                            f"second '{alias}' threshold {_PHASE1_TIMEOUT_S // 60}분 미도달 "
                            f"— 명령 재전송 후 재대기")
                        self._session.write(alias, ch.cmd_set.format(v=next_v))
                        phase1_deadline = _time.monotonic() + _PHASE1_TIMEOUT_S
                        continue
                    raise SecondAdvanceTimeout(
                        f"second channel '{alias}': 명령 재전송 후에도 "
                        f"{_PHASE1_TIMEOUT_S // 60}분 내 목표값({next_v:g})에 도달하지 못함")
                if self._stop_event.wait(ch.feedback_poll_interval):
                    return
                try:
                    raw = self._session.query(alias, ch.feedback_read_cmd).strip()
                    v_read = _parse_float(raw)
                except Exception as e:
                    if is_comm_error(e):
                        comm_fails += 1
                        if comm_fails >= _MAX_COMM_FAILS:
                            raise
                    continue
                comm_fails = 0
                if not math.isfinite(v_read):
                    nonfinite += 1
                    if nonfinite >= 5:
                        raise SecondAdvanceTimeout(
                            f"second channel '{alias}': 측정값이 계속 비유한값(NaN/Inf).")
                    continue
                nonfinite = 0
                band = max((1.0 - ch.feedback_tolerance_pct / 100.0) * denom,
                           ch.feedback_noisefloor)
                if abs(v_read - next_v) <= band:
                    break
                nowp = _time.monotonic()
                if nowp - last_prog >= 1.5:   # 도달 중 진행 표시(스로틀)
                    self.status.emit(
                        f"도달 중 {v_read:.4g} → {next_v:.4g} (남음 {abs(v_read-next_v):.3g})")
                    last_prog = nowp

        # ── Phase 2 대체: 고정 시간 대기 (stop 빠르게 반영) ──
        self.status.emit(f"도달 — {ch.wait_time:g}s 대기")
        deadline = _time.monotonic() + ch.wait_time
        while _time.monotonic() < deadline:
            if self._stop_event.is_set():
                break
            _time.sleep(0.05)

    def _do_wait_for_time(self, alias: str, cmd_set: str,
                          value: float, wait_time: float) -> None:
        """값 설정 후 wait_time 초 대기."""
        self._session.write(alias, cmd_set.format(v=value))
        deadline = _time.monotonic() + wait_time
        while _time.monotonic() < deadline:
            if self._stop_event.is_set():
                break
            _time.sleep(0.05)
