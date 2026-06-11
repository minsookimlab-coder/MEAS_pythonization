"""
SweepWorker: VISA I/O를 전담하는 워커 객체.
QThread 위에서 동작하며 메인 스레드의 UI를 블로킹하지 않습니다.
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import threading
import time as _time

# TSP: print(<expr>) 패턴에서 내부 표현식 추출
_TSP_PRINT_RE = re.compile(r"^\s*print\((.+)\)\s*$", re.DOTALL)

from PySide6.QtCore import QObject, Signal, Slot

from core.instrument_parameter import MeasurementParameter
from core.sweep import calculate_next_step
from core.sweep_channel import SweepChannel

@dataclass
class StepTiming:
    t_emit: float = 0.0          # main: request_step.emit 직전
    t_worker_start: float = 0.0  # worker: run_step 진입
    t_source_read: float = 0.0   # worker: source 값 확정 (첫 스텝만 실제 readback)
    t_write_done: float = 0.0    # worker: set_value 완료
    t_meas_done: float = 0.0     # worker: 모든 measurement 완료


@dataclass
class StepRequest:
    sweep_channel: SweepChannel
    sweep_to: float
    sweep_rate: float
    time_per_point: float
    t_emit: float = 0.0
    # None이면 첫 스텝 → 기기에서 실제 readback
    # 값이 있으면 이전 스텝에서 쓴 값을 그대로 사용 (readback 생략)
    last_write_value: Optional[float] = None
    safety_steps: int = 0           # 0 = safety 없음
    safety_interval_ms: float = 0.0 # sub-step 사이 대기 시간 (ms)
    # 체크된 measurement 행: (table row index, alias, description, resolved_cmd)
    active_measurements: List[Tuple[int, str, str, str]] = field(default_factory=list)
    # 초기 상태 측정 전용 — write 없이 현재 위치에서 바로 measurement만 수행
    measure_only: bool = False


@dataclass
class StepResult:
    current: float
    next_v: float
    is_done: bool
    timing: StepTiming = field(default_factory=StepTiming)
    # 읽은 값: (table row index, value)  value=None 이면 read 실패
    meas_results: List[Tuple[int, Optional[float]]] = field(default_factory=list)
    # 실패한 row별 오류 메시지: {row: "ExceptionType: message"}
    meas_errors: Dict[int, str] = field(default_factory=dict)
    measure_only: bool = False


class SweepWorker(QObject):
    """
    메인 스레드가 request_step Signal을 emit하면
    run_step Slot이 워커 스레드에서 호출됩니다.
    완료 또는 오류 시 step_done / step_error Signal을 emit합니다.
    """

    step_done = Signal(object)   # StepResult
    step_error = Signal(str)

    def __init__(self):
        super().__init__()
        self._session = None
        self._stop_event = threading.Event()
        self._threshold: float = 1e38  # |value| > threshold → None (nan)
        self._parallel: bool = False   # True → 서로 다른 계측기 동시 측정

    def set_session(self, session) -> None:
        self._session = session

    def set_threshold(self, value: float) -> None:
        """전역 임계값 설정. |측정값| > value 이면 None(→ nan) 반환."""
        self._threshold = value

    def set_parallel(self, enabled: bool) -> None:
        """병렬 측정 토글. True면 서로 다른 계측기(alias)를 동시에 측정한다.

        한 계측기 내 측정은 항상 한 번의 batch 또는 순차로 처리되고,
        서로 다른 계측기끼리만 병렬화된다 (각자 독립 TCP 연결 → 안전).
        """
        self._parallel = enabled

    def request_stop(self) -> None:
        """메인 스레드에서 호출 — safety ramp 루프를 중단시킵니다."""
        self._stop_event.set()

    def _measure_one_alias(
        self, alias: str, entries: List[Tuple[int, str, str]], mode: str = ""
    ) -> Tuple[List[Tuple[int, Optional[float]]], Dict[int, str]]:
        """한 계측기(alias)의 측정 항목들을 읽어 (results, errors) 로 반환.

        모든 항목이 TSP print() 패턴이면 한 번의 batch query로 묶고,
        그렇지 않으면 항목별 순차 read. 같은 계측기 내부는 직렬(같은 TCP 연결).

        mode: 에러 메시지 접두어 ("parallel"|"seq"). 어느 단계/계측기에서 났는지 식별용.
        에러 메시지는 항상 [mode|alias] + 명령어 + 예외형/메시지를 포함한다.
        """
        results: List[Tuple[int, Optional[float]]] = []
        errors: Dict[int, str] = {}
        tag = f"[{mode}|{alias}]" if mode else f"[{alias}]"
        exprs = [_TSP_PRINT_RE.match(cmd) for _, _, cmd in entries]
        if all(m is not None for m in exprs):
            batched = "print(" + ", ".join(m.group(1).strip() for m in exprs) + ")"
            try:
                if not self._session.is_open(alias):
                    self._session.open(alias)
                raw = self._session.query(alias, batched).strip()
                parts = raw.split("\t")
                for i, (row, desc, _) in enumerate(entries):
                    try:
                        val = float(parts[i])
                        if abs(val) > self._threshold:
                            val = float("nan")
                        results.append((row, val))
                    except (IndexError, ValueError):
                        got = parts[i] if i < len(parts) else "missing"
                        results.append((row, None))
                        errors[row] = (
                            f"{tag} batch 파싱 실패 — '{desc}' (part {i}='{got}'); "
                            f"응답='{raw[:80]}'")
            except Exception as e:
                # batch query 자체 실패 (통신/타임아웃 등) → 이 계측기의 모든 항목 실패
                err_msg = f"{tag} batch query 실패 (cmd='{batched}'): {type(e).__name__}: {e}"
                for row, _, _ in entries:
                    results.append((row, None))
                    errors[row] = err_msg
        else:
            for row, desc, cmd in entries:
                try:
                    val = MeasurementParameter(name=desc, cmd_query=cmd).read(
                        self._session, alias
                    )
                    if val is not None and abs(val) > self._threshold:
                        val = float("nan")
                    results.append((row, val))
                except Exception as e:
                    results.append((row, None))
                    errors[row] = (
                        f"{tag} read 실패 — '{desc}' (cmd='{cmd}'): "
                        f"{type(e).__name__}: {e}")
        return results, errors

    def _do_measurements(
        self, active_measurements: List[Tuple[int, str, str, str]]
    ) -> Tuple[List[Tuple[int, Optional[float]]], Dict[int, str]]:
        """active_measurements 목록을 읽어 (results, errors) 로 반환.

        계측기(alias)별로 그룹화한 뒤:
          - _parallel=False: 그룹을 순차 측정 (기존 동작)
          - _parallel=True : 그룹을 스레드로 동시 측정 (서로 다른 계측기 병렬)
        결과 순서는 무의미 (소비측이 {row: val} dict로 변환).

        에러 발생 시 각 항목의 errors[row] 에 [mode|alias] + 명령어 + 예외가 기록되어
        어느 계측기·명령에서 났는지 정확히 식별할 수 있다.
        """
        groups: Dict[str, List[Tuple[int, str, str]]] = defaultdict(list)
        for row, alias, desc, cmd in active_measurements:
            groups[alias].append((row, desc, cmd))

        meas_results: List[Tuple[int, Optional[float]]] = []
        meas_errors: Dict[int, str] = {}

        if self._parallel and len(groups) > 1:
            # 서로 다른 계측기를 동시에 측정 — alias별 락이 같은 계측기는 직렬화한다.
            from concurrent.futures import ThreadPoolExecutor
            items = list(groups.items())
            with ThreadPoolExecutor(max_workers=len(items)) as ex:
                # future → alias 매핑을 유지해 예외 발생 시 어느 계측기인지 식별
                fut_alias = {
                    ex.submit(self._measure_one_alias, alias, entries, "parallel"): alias
                    for alias, entries in items
                }
                for fut, alias in fut_alias.items():
                    try:
                        res, err = fut.result()
                    except Exception as e:
                        # _measure_one_alias가 잡지 못한 예외 (이론상 없음) —
                        # 해당 계측기 항목만 실패 처리하고 다른 계측기 결과는 보존.
                        msg = f"[parallel|{alias}] 병렬 실행 중 예외: {type(e).__name__}: {e}"
                        res = [(row, None) for row, _, _ in groups[alias]]
                        err = {row: msg for row, _, _ in groups[alias]}
                    meas_results.extend(res)
                    meas_errors.update(err)
        else:
            for alias, entries in groups.items():
                res, err = self._measure_one_alias(alias, entries, "seq")
                meas_results.extend(res)
                meas_errors.update(err)
        return meas_results, meas_errors

    @Slot(object)
    def run_step(self, req: StepRequest) -> None:
        self._stop_event.clear()
        timing = StepTiming(t_emit=req.t_emit)
        try:
            timing.t_worker_start = _time.perf_counter()

            # 1. 소스값 결정: 첫 스텝만 실제 readback, 이후는 마지막 쓴 값 재사용
            # TimeChannel이 아닌 경우 auto-open (SecondChannelWorker와 동일 정책)
            sweep_alias = getattr(req.sweep_channel, "alias", None)
            if sweep_alias and not self._session.is_open(sweep_alias):
                self._session.open(sweep_alias)

            if req.last_write_value is None:
                current = req.sweep_channel.read_value(self._session)
            else:
                current = req.last_write_value
            timing.t_source_read = _time.perf_counter()

            # 초기 상태 측정 전용: write 없이 현재 위치에서 measurement만 수행
            if req.measure_only:
                meas_results, meas_errors = self._do_measurements(req.active_measurements)
                timing.t_write_done = timing.t_source_read
                timing.t_meas_done = _time.perf_counter()
                self.step_done.emit(StepResult(
                    current=current, next_v=current, is_done=False,
                    timing=timing, meas_results=meas_results, meas_errors=meas_errors,
                    measure_only=True,
                ))
                return

            # 2. 다음 스텝 계산
            next_v, is_done = calculate_next_step(
                current, req.sweep_to, req.sweep_rate, req.time_per_point
            )

            # 이미 목표 도달 — write/measure 생략 (이전 스텝에서 이미 기록됨)
            if is_done:
                timing.t_write_done = _time.perf_counter()
                timing.t_meas_done = timing.t_write_done
                self.step_done.emit(StepResult(
                    current=current, next_v=next_v, is_done=True, timing=timing,
                ))
                return

            # 3. 쓰기 — safety 여부에 따라 단계적 ramp 또는 직접 write
            if req.safety_steps > 0 and abs(next_v - current) > 1e-11:
                sub_vs = [
                    current + (next_v - current) * i / req.safety_steps
                    for i in range(1, req.safety_steps + 1)
                ]
                interval_s = req.safety_interval_ms / 1000.0
                for sub_v in sub_vs:
                    if self._stop_event.is_set():
                        break
                    req.sweep_channel.set_value(self._session, sub_v)
                    if interval_s > 0:
                        _time.sleep(interval_s)
            else:
                req.sweep_channel.set_value(self._session, next_v)
            timing.t_write_done = _time.perf_counter()

            # 4. 체크된 Measurement 읽기 (write 이후 → 새 출력값에 대한 응답 측정)
            meas_results, meas_errors = self._do_measurements(req.active_measurements)
            timing.t_meas_done = _time.perf_counter()

            self.step_done.emit(StepResult(
                current=current,
                next_v=next_v,
                is_done=is_done,
                timing=timing,
                meas_results=meas_results,
                meas_errors=meas_errors,
            ))

        except Exception as e:
            # 어느 단계에서 났는지 식별 가능하도록 sweep alias + 예외형 포함
            _sa = getattr(req.sweep_channel, "alias", "?")
            self.step_error.emit(f"[sweep:{_sa}] {type(e).__name__}: {e}")