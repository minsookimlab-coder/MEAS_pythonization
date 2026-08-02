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

from config.config_models import InstantiatedSecondSweepChannel, SecondSweepAdvanceType
from core.sweep import calculate_next_step
from core.sweep_channel import SweepChannel
from core.instrument_parameter import MeasurementParameter, _parse_float

if TYPE_CHECKING:
    from core.instrument_session import InstrumentSession


# ── second advance 무한 지속 감지용 워치독 타임아웃 ───────────────────────────
# threshold(목표 도달)까지 너무 오래 걸리면 명령이 무시됐을 가능성을 의심해 1회
# 재전송 후 다시 대기하고, 그래도 안 되면 실패 처리한다. feedback(안정화) 과정이
# 너무 오래 지속돼도 실패 처리한다. 실패 시 측정 중지 + 알람.
_PHASE1_TIMEOUT_S = 20 * 60   # threshold 도달 최대 대기 (재전송 시 1회 더 → 총 최대 40분)
_PHASE2_TIMEOUT_S = 5 * 60    # feedback 안정화 과정 최대 지속


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
        """값 설정 후 feedback_read_cmd로 폴링, 목표 도달 시 반환.

        Phase 1: 목표와의 거리가 band = max((1-tol%)·denom, noisefloor) 이하가 될 때까지 poll.
          noisefloor가 절대 허용오차 하한이라, 시작점이 목표에 매우 가까운 작은 이동에서도
          판정이 노이즈보다 빡빡해지지 않는다.
        Phase 2 (std_window > 0): 도달 후 추가 폴링 —
          최근 std_window 개 측정값의 std dev < std_threshold 가 되면 반환
        """
        self._session.write(alias, ch.cmd_set.format(v=next_v))

        denom = abs(next_v - (prev_v or 0.0))
        use_std = ch.feedback_std_window > 0 and ch.feedback_std_threshold > 0.0

        # denom == 0 이면 이미 목표값에 있음 — std 체크만 남아있을 수 있음
        if denom < 1e-12:
            if not use_std:
                return
            # threshold는 이미 충족, Phase 2만 실행
            threshold_reached = True
        else:
            threshold_reached = False

        # sliding window for Phase 2
        window: deque = deque(maxlen=ch.feedback_std_window) if use_std else deque(maxlen=1)

        # ── 워치독 타이머 ────────────────────────────────────────────────────
        # Phase 1: threshold 도달까지 _PHASE1_TIMEOUT_S 내. 초과 시 명령 무시를
        #          의심해 1회 재전송 후 재대기, 그래도 초과면 실패(SecondAdvanceTimeout).
        # Phase 2: 안정화가 _PHASE2_TIMEOUT_S 내 수렴하지 않으면 실패.
        phase1_deadline = _time.monotonic() + _PHASE1_TIMEOUT_S
        phase2_deadline = (_time.monotonic() + _PHASE2_TIMEOUT_S
                           if threshold_reached else None)
        retried = False
        last_prog = 0.0   # Phase 1 '도달 중' 진행 표시 스로틀(초)
        comm_fails = 0    # 연속 통신 실패 카운트 (조기 에스컬레이션용)
        nonfinite = 0     # 연속 비유한값(NaN/Inf) 카운트

        while True:
            if self._stop_event.is_set():
                break

            # ── 워치독 점검 (query 실패로 continue되더라도 항상 평가되도록 루프 상단에) ──
            now = _time.monotonic()
            if not threshold_reached:
                if now > phase1_deadline:
                    if not retried:
                        # 명령이 한 번 무시됐을 가능성 → 다시 한 번 전송 후 재대기
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
            elif phase2_deadline is not None and now > phase2_deadline:
                raise SecondAdvanceTimeout(
                    f"second channel '{alias}': feedback 안정화가 "
                    f"{_PHASE2_TIMEOUT_S // 60}분 내 수렴하지 않음")

            # uninterruptible sleep 대신 stop_event.wait → Stop이 poll 간격 내 즉시 반영
            if self._stop_event.wait(ch.feedback_poll_interval):
                break
            try:
                raw = self._session.query(alias, ch.feedback_read_cmd).strip()
                v_read = _parse_float(raw)
            except Exception as e:
                # 통신 오류는 20분 워치독까지 묵히지 말고 ~3회 연속이면 끊어,
                # 상위(advance)의 error 경로 → 빠른 자동재개로 보낸다.
                from core.visa_errors import is_comm_error
                if is_comm_error(e):
                    comm_fails += 1
                    if comm_fails >= 3:
                        raise
                continue
            comm_fails = 0
            if not math.isfinite(v_read):
                # 비유한값(NaN/Inf)이 계속 오면 40분 워치독 대신 즉시 명확한 실패로.
                nonfinite += 1
                if nonfinite >= 5:
                    raise SecondAdvanceTimeout(
                        f"second channel '{alias}': 측정값이 계속 비유한값(NaN/Inf) "
                        f"(read='{ch.feedback_read_cmd}').")
                continue
            nonfinite = 0

            # Phase 1: 목표 근접 판정 (목표까지 남은 거리가 허용 band 이하인가).
            #   band = max((1 - tol%)·denom, noisefloor)
            #   - 큰 이동: (1-tol%)·denom — 기존 비율 허용오차와 사실상 동일
            #   - 작은 이동(시작점이 목표에 매우 가까움): noisefloor가 절대 하한이 되어
            #     도달 판정이 과도하게 빡빡(노이즈보다 작은 오차 요구)해지는 것을 막는다.
            #   또한 '이동량' 대신 '목표와의 거리'로 보므로 overshoot 시 조기 도달 오판도 없다.
            if not threshold_reached:
                band = max((1.0 - ch.feedback_tolerance_pct / 100.0) * denom,
                           ch.feedback_noisefloor)
                if abs(v_read - next_v) <= band:
                    threshold_reached = True
                    phase2_deadline = _time.monotonic() + _PHASE2_TIMEOUT_S  # Phase 2 타이머 시작
                    window.clear()   # reset window for Phase 2
                    if not use_std:
                        break        # no std check needed — done
                    continue         # Phase 2 fresh start: threshold-crossing sample 제외
                else:
                    # 진행 표시(스로틀): 도달 중 현재값 → 목표
                    if now - last_prog >= 1.5:
                        self.status.emit(
                            f"도달 중 {v_read:.4g} → {next_v:.4g} (남음 {abs(v_read-next_v):.3g})")
                        last_prog = now
                    continue         # still in Phase 1, don't accumulate yet

            # Phase 2: accumulate + normalized stability metric
            # metric = SD / (|next_v| + noisefloor) < std_threshold
            # next_v를 고정 기준으로 사용: mean(측정값)은 overshoot 등으로 편향될 수 있으므로
            # 목표값(next_v)을 정규화 기준으로 삼아 일관된 상대적 척도를 제공.
            # next_v ≈ 0 일 때는 feedback_noisefloor 를 반드시 설정해야 함.
            window.append(v_read)
            if len(window) < ch.feedback_std_window:
                continue             # not enough samples yet
            mean = sum(window) / len(window)
            std = math.sqrt(sum((v - mean) ** 2 for v in window) / len(window))
            scale = abs(next_v) + ch.feedback_noisefloor
            metric = std / scale if scale > 1e-30 else float("inf")
            self.feedback_progress.emit(metric)
            if metric < ch.feedback_std_threshold:
                break                # stable enough — advance

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
                    from core.visa_errors import is_comm_error
                    if is_comm_error(e):
                        comm_fails += 1
                        if comm_fails >= 3:
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
